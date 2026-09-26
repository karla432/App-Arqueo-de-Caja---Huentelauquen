"""Tipos compartidos entre importadores de adquirente (Transbank, Mercado
Pago, ...). Viven aquí y no en el archivo de un proveedor específico para
no romper el control #11 (el motor no puede depender de ningún proveedor
por nombre) también a nivel de organización del código: un importador
nuevo no debería tener que importar desde el archivo de otro proveedor.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

import pandas as pd


def fechas_normalizadas(serie: pd.Series) -> pd.Series:
    """Resto a veces exporta la fecha como número de serie de Excel (ej.
    46269 = 04-09-2026, caso real de Karla 2026-09-23) en vez de como
    fecha. Se convierte a fecha real; una columna que ya viene como fecha
    o como texto no se altera."""
    if pd.api.types.is_datetime64_any_dtype(serie):
        return serie
    numeros = pd.to_numeric(serie, errors="coerce")
    if numeros.notna().any():
        return pd.to_datetime(numeros, unit="D", origin="1899-12-30", errors="coerce")
    return pd.to_datetime(serie, errors="coerce")


@dataclass
class MovimientoProveedor:
    proveedor: str
    tipo: str
    estado: str
    fecha_hora: datetime
    fecha_abono: str | None
    medio: str
    tipo_tarjeta: str
    numero_tarjeta: str  # enmascarado por el proveedor cuando corresponde (ej. Transbank).
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
