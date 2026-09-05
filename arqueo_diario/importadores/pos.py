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


@dataclass
class ResultadoImportacionPOS:
    fecha: date
    ventas: list[Venta]
    avisos: list[str]

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


def importar_ventas_por_articulo(path: str | Path, fecha: date) -> ResultadoImportacionPOS:
    """Lee VENTAS POR ARTICULO y arma ventas, lineas y pagos declarados del
    dia operativo indicado, deduplicando por N_INTERNO.

    Ninguna fila se descarta: lo que no cuadra queda en `avisos`, nunca se
    omite en silencio.
    """
    df = pd.read_excel(path, sheet_name=HOJA_VENTAS, engine="openpyxl")
    df = df[df["FECHA"] == pd.Timestamp(fecha)].copy()

    avisos: list[str] = []
    ventas: list[Venta] = []

    for n_interno, grupo in df.groupby("N_INTERNO", sort=False):
        cabecera = grupo.iloc[0]

        venta = Venta(
            n_interno=int(n_interno),
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

        ventas.append(venta)

    return ResultadoImportacionPOS(fecha=fecha, ventas=ventas, avisos=avisos)
