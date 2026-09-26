"""Importador de la hoja VENTAS POR ARTICULO del POS.

Regla critica (diseno funcional, seccion 0.1): las columnas de pago y
TOTAL_COMADA se repiten identicas en cada linea de articulo de la misma
comanda. Hay que deduplicar por N_INTERNO antes de sumar cualquier medio de
pago o el total del dia queda multiplicado por la cantidad de lineas. Es el
error numero uno a evitar.

Este importador solo lee columnas; no interpreta reglas de negocio (esas
viven en el motor de conciliacion, en una etapa posterior).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd

from arqueo_diario.importadores.comun import fechas_normalizadas

HOJA_VENTAS = "VENTAS POR ARTICULO"

COLUMNA_ARTICULO = "ARTíCULO"  # tal como viene en el archivo real, con í minuscula

# Nombre de columna en el POS -> nombre normalizado del medio de pago.
MEDIOS_PAGO = {
    "EFECTVO": "efectivo",
    "TRANSFERENCIAS": "transferencia",
    "TARJETA DE DEBITO": "debito",
    "TARJETA DE CREDITO": "credito",
}


@dataclass
class LineaVenta:
    n_interno: int
    articulo: str
    cantidad: float
    precio_unitario: float
    total_linea: float


@dataclass
class PagoDeclarado:
    n_interno: int
    medio: str
    monto: float


@dataclass
class Venta:
    n_interno: int
    nro_comprobante: str
    tipo_doc: str
    nro_comanda: float
    estado: str
    caja: str
    turno: str
    hora_apertura: str
    hora_cierre: str
    fecha: date
    total_comanda: float
    cliente: str
    sucursal: str
    lineas: list[LineaVenta] = field(default_factory=list)
    pagos: list[PagoDeclarado] = field(default_factory=list)


# Tipos de documento que trae el libro contable en COD_COMP (Karla,
# 2026-09-21, confirmado con la Analista de Ingresos).
TIPOS_DOCUMENTO = {
    "FAC": "Boleta electrónica",
    "DES": "Guía de despacho electrónica",
    "CRED": "Nota de crédito electrónica",
    "CRE": "Nota de crédito electrónica",
}
# Resto escribe la nota de crédito como "CRE" en algunos libros (caso real del
# 07-09-2026, folios F0610600008311 y F0610600008312) y como "CRED" en otros:
# son el mismo documento.
ALIAS_TIPO_DOCUMENTO = {"CRE": "CRED"}
# El número de una guía (DES) viene con este prefijo delante: el folio es lo
# que sigue. Ej. G0520500021184 -> folio 21184.
PREFIJO_GUIA = "G05205000"

MEDIOS_LIBRO = {
    "EFECTIVO": "efectivo",
    "TRANSFERENCIAS": "transferencia",
    "TARJETA DE DEBITO": "debito",
    "TARJETA DE CREDITO": "credito",
}


def folio_documento(cod_comp: str, n_comp: str) -> str:
    """Número de documento a partir de COD_COMP y N_COMP del libro contable.

    DES: lo que sigue al prefijo G05205000. FAC: los 7 últimos dígitos, igual
    que en la exportación de boletas. CRED: el prefijo de las notas de
    crédito todavía no está definido — se conserva el N_COMP completo."""
    n_comp = str(n_comp).strip()
    if cod_comp == "DES" and n_comp.startswith(PREFIJO_GUIA):
        return n_comp[len(PREFIJO_GUIA):].lstrip("0") or "0"
    if cod_comp == "FAC":
        return n_comp[-7:]
    return n_comp


@dataclass
class DocumentoLibro:
    """Guía de despacho (DES) o nota de crédito (CRED) del libro contable.
    A diferencia de una boleta (FAC), no trae el detalle de artículos en
    VENTAS POR ARTICULO — solo el documento y cómo se pagó."""
    tipo: str
    n_interno: int
    n_comp: str
    folio: str
    caja: str
    pagos: list[PagoDeclarado] = field(default_factory=list)
    codigo_original: str = ""

    @property
    def total(self) -> float:
        return sum(p.monto for p in self.pagos)


@dataclass
class ResultadoImportacionPOS:
    fecha: date
    ventas: list[Venta]
    avisos: list[str]
    documentos: list[DocumentoLibro] = field(default_factory=list)

    @property
    def guias(self) -> list[DocumentoLibro]:
        return [d for d in self.documentos if d.tipo == "DES"]

    @property
    def notas_credito(self) -> list[DocumentoLibro]:
        return [d for d in self.documentos if d.tipo == "CRED"]

    @property
    def sin_clasificar(self) -> list[DocumentoLibro]:
        return [d for d in self.documentos if d.tipo == "SIN"]

    @property
    def cantidad_comandas(self) -> int:
        return len(self.ventas)

    @property
    def cantidad_lineas(self) -> int:
        return sum(len(v.lineas) for v in self.ventas)

    @property
    def total_venta(self) -> float:
        return sum(v.total_comanda for v in self.ventas)

    @property
    def total_por_medio(self) -> dict[str, float]:
        totales = {medio: 0.0 for medio in MEDIOS_PAGO.values()}
        for venta in self.ventas:
            for pago in venta.pagos:
                totales[pago.medio] = totales.get(pago.medio, 0.0) + pago.monto
        return totales


COLUMNAS_VENTAS = {"NRO. COMPROBANTE", "TIPO_DOC", "FECHA", "TOTAL_COMADA", "CAJA CIERRE"}


def _leer_hoja_ventas(path) -> pd.DataFrame:
    """La hoja de ventas de Resto a veces se llama "VENTAS POR ARTICULO" y a
    veces "Hoja1" (caso real de Karla, 2026-09-24, archivo del 05-09). Se
    busca por nombre y, si no está, se usa la primera hoja que tenga las
    columnas de ventas — así el nombre de la hoja no importa, pero un
    archivo equivocado (ej. el libro contable) sigue rechazándose."""
    libro = pd.ExcelFile(path, engine="openpyxl")
    candidatas = [HOJA_VENTAS] if HOJA_VENTAS in libro.sheet_names else []
    candidatas += [h for h in libro.sheet_names if h != HOJA_VENTAS]
    for hoja in candidatas:
        df = libro.parse(hoja)
        if COLUMNAS_VENTAS.issubset(set(df.columns)):
            return df
    if any(h.lower().startswith("cartola") for h in libro.sheet_names):
        raise ValueError(
            "este archivo es la cartola de Transbank, no las ventas por artículo — "
            "súbela en la Fuente 2 (Transbank) y aquí pon el archivo de ventas"
        )
    if {"N_INTERNO", "COD_COMP", "N_COMP"}.issubset(set(libro.parse(libro.sheet_names[0]).columns)):
        raise ValueError(
            "este archivo es el libro contable, no las ventas por artículo — "
            "ponlo en el segundo campo (Libro contable)"
        )
    raise ValueError(
        f"ninguna hoja del archivo tiene las columnas de ventas por artículo "
        f"({', '.join(sorted(COLUMNAS_VENTAS))}); hojas encontradas: {', '.join(libro.sheet_names)}"
    )


def _armar_venta(n_interno: int, grupo, fecha: date) -> tuple[Venta, list[str]]:
    """Arma una Venta con sus líneas y pagos a partir del grupo de filas
    (todas del mismo comprobante) de VENTAS POR ARTICULO, ya con su
    N_INTERNO resuelto — venga directo de la columna del archivo o del
    cruce con el libro contable."""
    cabecera = grupo.iloc[0]
    avisos: list[str] = []

    venta = Venta(
        n_interno=n_interno,
        nro_comprobante=str(cabecera["NRO. COMPROBANTE"]),
        tipo_doc=str(cabecera["TIPO_DOC"]),
        nro_comanda=cabecera["NRO. COMANDA"],
        estado=str(cabecera["ESTADO COMANDA"]),
        caja=str(cabecera["CAJA CIERRE"]),
        turno=str(cabecera["TURNO"]),
        hora_apertura=str(cabecera["HORA APERTURA"]),
        hora_cierre=str(cabecera["HORA CIERRE"]),
        fecha=fecha,
        total_comanda=float(cabecera["TOTAL_COMADA"]),
        cliente=str(cabecera["CLIENTE"]),
        sucursal=str(cabecera["SUCURSAL"]),
    )

    for _, fila in grupo.iterrows():
        venta.lineas.append(LineaVenta(
            n_interno=venta.n_interno,
            articulo=str(fila[COLUMNA_ARTICULO]).strip(),
            cantidad=float(fila["CANTIDAD"]),
            precio_unitario=float(fila["PRECIO UNITARIO"]),
            total_linea=float(fila["TOTAL POR ARTICULO"]),
        ))

    for columna, medio in MEDIOS_PAGO.items():
        monto = float(cabecera[columna])
        if monto > 0:
            venta.pagos.append(PagoDeclarado(n_interno=venta.n_interno, medio=medio, monto=monto))

    suma_lineas = sum(linea.total_linea for linea in venta.lineas)
    if abs(suma_lineas - venta.total_comanda) > 1:
        avisos.append(
            f"Comanda {venta.n_interno}: total ${venta.total_comanda:,.0f} "
            f"no coincide con la suma de líneas ${suma_lineas:,.0f}."
        )

    suma_pagos = sum(pago.monto for pago in venta.pagos)
    if abs(suma_pagos - venta.total_comanda) > 1:
        avisos.append(
            f"Comanda {venta.n_interno}: total ${venta.total_comanda:,.0f} "
            f"no coincide con la suma de medios declarados ${suma_pagos:,.0f}."
        )

    return venta, avisos


def importar_ventas_por_articulo(path: str | Path, fecha: date) -> ResultadoImportacionPOS:
    """Lee VENTAS POR ARTICULO y arma ventas, lineas y pagos declarados del
    dia operativo indicado, deduplicando por N_INTERNO.

    Ninguna fila se descarta: lo que no cuadra queda en `avisos`, nunca se
    omite en silencio.
    """
    df = _leer_hoja_ventas(path)
    df = df[fechas_normalizadas(df["FECHA"]) == pd.Timestamp(fecha)].copy()

    avisos: list[str] = []
    ventas: list[Venta] = []

    for n_interno, grupo in df.groupby("N_INTERNO", sort=False):
        venta, avisos_venta = _armar_venta(int(n_interno), grupo, fecha)
        avisos.extend(avisos_venta)
        ventas.append(venta)

    return ResultadoImportacionPOS(fecha=fecha, ventas=ventas, avisos=avisos)


def importar_ventas_con_libro_contable(
    archivo_ventas: str | Path, archivo_libro_contable: str | Path, fecha: date,
) -> ResultadoImportacionPOS:
    """Como `importar_ventas_por_articulo`, pero para cuando Resto entrega
    VENTAS POR ARTICULO sin la columna N_INTERNO (caso real de Karla,
    2026-09-16). Se recupera cruzando cada NRO. COMPROBANTE contra el
    libro contable de movimientos ("N_COMP" en ese archivo), que sí trae
    el N_INTERNO — evita la consolidación manual que hacía antes a mano.

    Ninguna fila se descarta: un comprobante de venta que no aparece en
    el libro contable queda en `avisos` y esa comanda no se importa, en
    vez de inventarle un N_INTERNO.
    """
    libro = pd.read_excel(archivo_libro_contable, sheet_name=0, engine="openpyxl")
    libro = libro[fechas_normalizadas(libro["FEC_COMPR"]) == pd.Timestamp(fecha)]
    facturas = libro[libro["COD_COMP"] == "FAC"]
    mapa_n_interno = dict(zip(facturas["N_COMP"], facturas["N_INTERNO"]))

    avisos: list[str] = []
    documentos, avisos_documentos = _documentos_del_libro(libro)
    avisos.extend(avisos_documentos)

    df = _leer_hoja_ventas(archivo_ventas)
    df = df[fechas_normalizadas(df["FECHA"]) == pd.Timestamp(fecha)].copy()

    ventas: list[Venta] = []

    for comprobante, grupo in df.groupby("NRO. COMPROBANTE", sort=False):
        n_interno = mapa_n_interno.get(comprobante)
        if n_interno is None:
            avisos.append(
                f"Comprobante {comprobante}: no aparece en el libro contable — "
                "no se puede asignar N_INTERNO, comanda no importada."
            )
            continue
        venta, avisos_venta = _armar_venta(int(n_interno), grupo, fecha)
        avisos.extend(avisos_venta)
        ventas.append(venta)

    return ResultadoImportacionPOS(fecha=fecha, ventas=ventas, avisos=avisos, documentos=documentos)


def _numero_de_caja(valor) -> str:
    """COD_CAJA llega como número decimal (1.0) cuando Excel lo guarda así:
    se normaliza a "1" para que la caja de una guía coincida con la de las
    boletas ("CAJA 1"), si no el efectivo de la guía cae en una caja
    fantasma "CAJA 1.0" (caso real de Karla, 2026-09-23)."""
    texto = str(valor).strip()
    try:
        numero = float(texto)
        return str(int(numero)) if numero == int(numero) else texto
    except ValueError:
        return "" if texto.lower() == "nan" else texto


def _documentos_del_libro(libro) -> tuple[list[DocumentoLibro], list[str]]:
    """Guías de despacho (DES) y notas de crédito (CRED) del libro contable.
    Las boletas (FAC) llegan por VENTAS POR ARTICULO; cualquier otro código
    queda en `avisos`, nunca se descarta en silencio."""
    documentos: list[DocumentoLibro] = []
    avisos: list[str] = []

    for cod_comp in sorted({str(c) for c in libro["COD_COMP"].dropna()} - {"FAC"}):
        filas = libro[libro["COD_COMP"] == cod_comp]
        conocido = cod_comp in TIPOS_DOCUMENTO
        if not conocido:
            # Karla, 2026-09-24: un código no configurado NO se descarta: queda
            # visible en Regularización como "Sin clasificar", con su código
            # original, y bloquea el cierre hasta que alguien lo clasifique.
            avisos.append(
                f"Tipo de documento {cod_comp!r} no reconocido en el libro contable "
                f"({len(filas)} fila(s)): quedó en Regularización de Documentos como «Sin clasificar»."
            )

        for n_comp, grupo in filas.groupby("N_COMP", sort=False):
            cabecera = grupo.iloc[0]
            pagos = []
            for _, fila in grupo.iterrows():
                medio = MEDIOS_LIBRO.get(str(fila["DESC_CTA"]).strip())
                debe, haber = float(fila["DEBE"] or 0), float(fila["HABER"] or 0)
                monto = debe if cod_comp == "DES" else (haber or debe)  # NC (CRED/CRE): sale dinero → HABER
                if medio is None:
                    avisos.append(f"Documento {n_comp}: medio de pago no reconocido {fila['DESC_CTA']!r}.")
                    continue
                pagos.append(PagoDeclarado(n_interno=int(cabecera["N_INTERNO"]), medio=medio, monto=monto))
            caja = _numero_de_caja(cabecera.get("COD_CAJA", ""))
            documento = DocumentoLibro(
                tipo=ALIAS_TIPO_DOCUMENTO.get(cod_comp, cod_comp) if conocido else "SIN",
                codigo_original=cod_comp, n_interno=int(cabecera["N_INTERNO"]), n_comp=str(n_comp).strip(),
                folio=folio_documento(cod_comp, n_comp), caja=f"CAJA {caja}" if caja and caja != "nan" else "",
                pagos=pagos,
            )
            documentos.append(documento)
    return documentos, avisos
