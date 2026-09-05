"""Importador de la cartola de movimientos de Transbank.

Un importador por proveedor (diseño funcional §3): esta es la única capa
que conoce las columnas y el formato particular de Transbank. El motor de
conciliación (etapa posterior) solo debe recibir `MovimientoProveedor`
normalizados, sin saber nunca que existió una hoja llamada
"CartolaMovimientos" (control obligatorio: el motor no puede mencionar a
ningún proveedor por nombre).

El archivo real trae un bloque de metadatos (RUT, filtros de búsqueda,
totales de control) antes de la tabla de movimientos — la tabla empieza en
la fila 19 (índice 18).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import pandas as pd

HOJA = "CartolaMovimientos"
FILA_ENCABEZADO = 18  # 0-indexado: filas 0-17 son metadatos del reporte

MESES_ES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10, "noviembre": 11,
    "diciembre": 12,
}

# Vocabulario propio del sistema, agnóstico del proveedor.
MEDIOS_PAGO = {
    "Débito": "debito",
    "Crédito": "credito",
    "Prepago": "prepago",
}

TARJETAS = {
    "Debito": "debito",
    "Mastercard": "mastercard",
    "VISA": "visa",
    "Prepago": "prepago",
    "American": "american",
}


def _parse_fecha_hora(texto: str) -> datetime:
    """'02 septiembre 2026 07:30 PM' -> datetime."""
    dia_str, mes_nombre, anio_str, hora_str, ampm = texto.strip().split()
    mes = MESES_ES[mes_nombre.lower()]
    hora, minuto = (int(p) for p in hora_str.split(":"))
    if ampm.upper() == "PM" and hora != 12:
        hora += 12
    if ampm.upper() == "AM" and hora == 12:
        hora = 0
    return datetime(int(anio_str), mes, int(dia_str), hora, minuto)


def _o_ninguno(valor) -> str | None:
    if pd.isna(valor):
        return None
    texto = str(valor).strip()
    return None if texto in ("", "-") else texto


@dataclass
class MovimientoProveedor:
    proveedor: str
    tipo: str
    estado: str
    fecha_hora: datetime
    fecha_abono: str | None
    medio: str
    tipo_tarjeta: str
    numero_tarjeta: str  # ya viene enmascarado por Transbank (ej. 434561XXXXXX3760)
    monto_original: float
    monto_afecto: float
    monto_exento: float
    monto_vuelto: float
    propina: float
    monto_venta_valido_abono: float
    monto_anulado: float
    monto_cuota: float
    autorizacion: str
    terminal: str
    moneda: str


@dataclass
class ResultadoImportacionProveedor:
    fecha: date
    movimientos: list[MovimientoProveedor]
    avisos: list[str]

    @property
    def cantidad_movimientos(self) -> int:
        return len(self.movimientos)

    @property
    def total_venta_valido_abono(self) -> float:
        return sum(m.monto_venta_valido_abono for m in self.movimientos)


def importar_cartola_transbank(path: str | Path, fecha: date) -> ResultadoImportacionProveedor:
    """Lee CartolaMovimientos y arma los movimientos del día operativo
    indicado. Ninguna fila se descarta: lo que no se entiende va a `avisos`.
    """
    df = pd.read_excel(path, sheet_name=HOJA, engine="openpyxl", header=FILA_ENCABEZADO)

    avisos: list[str] = []
    movimientos: list[MovimientoProveedor] = []

    for _, fila in df.iterrows():
        fecha_hora = _parse_fecha_hora(str(fila["Fecha de movimiento"]))
        if fecha_hora.date() != fecha:
            continue

        medio_original = str(fila["Medio de pago"]).strip()
        medio = MEDIOS_PAGO.get(medio_original)
        if medio is None:
            avisos.append(f"Medio de pago no reconocido: {medio_original!r}")
            medio = medio_original.lower()

        tarjeta_original = str(fila["Tipo de tarjeta"]).strip()
        tipo_tarjeta = TARJETAS.get(tarjeta_original)
        if tipo_tarjeta is None:
            avisos.append(f"Tipo de tarjeta no reconocido: {tarjeta_original!r}")
            tipo_tarjeta = tarjeta_original.lower()

        movimientos.append(MovimientoProveedor(
            proveedor="transbank",
            tipo=str(fila["Tipo de movimiento"]).strip(),
            estado=str(fila["Estado de movimiento"]).strip(),
            fecha_hora=fecha_hora,
            fecha_abono=_o_ninguno(fila["Fecha de abono"]),
            medio=medio,
            tipo_tarjeta=tipo_tarjeta,
            numero_tarjeta=str(fila["N° de tarjeta"]),
            monto_original=float(fila["Monto original de la venta"]),
            monto_afecto=float(fila["Monto afecto"]),
            monto_exento=float(fila["Monto exento"]),
            monto_vuelto=float(fila["Monto vuelto"]),
            propina=float(fila["Propina"]),
            monto_venta_valido_abono=float(fila["Monto venta valido para abono"]),
            monto_anulado=float(fila["Monto anulado"]),
            monto_cuota=float(fila["Monto cuota"]),
            autorizacion=str(fila["Código de autorización de la venta"]).strip(),
            terminal=str(fila["Terminal"]).strip(),
            moneda=str(fila["Moneda"]).strip(),
        ))

    return ResultadoImportacionProveedor(fecha=fecha, movimientos=movimientos, avisos=avisos)
