"""Importador de la cartola de liquidación (settlement) de Mercado Pago.

Segundo adquirente de tarjeta, junto a Transbank (diseño funcional §3: un
importador por proveedor, cada uno con su propio formato de columnas). El
motor de conciliación no distingue entre proveedores — solo ve `medio`
("debito"/"credito") y monto, igual que con Transbank (control #11).

Formato real observado (archivo "settlement_v2...xlsx" del Point de
Mercado Pago, único ejemplo disponible hasta ahora — 2026-09-10, 3 filas,
todas "Pago aprobado" con "Tarjeta de débito"): una sola hoja sin bloque
de metadatos, encabezado en la fila 1. No hay ejemplo real todavía de un
reembolso o contracargo, así que solo "Pago aprobado" se interpreta como
venta; cualquier otro tipo de operación queda en `avisos` en vez de
adivinarse (misma disciplina que con las anulaciones de Transbank).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from arqueo_diario.importadores.comun import MovimientoProveedor, ResultadoImportacionProveedor

# Vocabulario propio del sistema, agnóstico del proveedor — mismos nombres
# que usa el importador de Transbank.
MEDIOS_PAGO = {
    "Tarjeta de débito": "debito",
    "Tarjeta de crédito": "credito",
}

# Solo se ha visto "Pago aprobado" en un ejemplo real. Se normaliza al
# mismo vocabulario que Transbank ("Venta") para que anulaciones_servicio
# pueda tratarlas igual el día que aparezca un reembolso real.
TIPOS_OPERACION = {
    "Pago aprobado": "Venta",
}


def _parse_fecha_hora(texto: str) -> datetime:
    """'2026-09-10T11:05:09.000-03:00' -> datetime naive en hora local
    (se descarta el offset, igual que Transbank no trae uno)."""
    return datetime.fromisoformat(str(texto)).replace(tzinfo=None)


def importar_settlement_mercadopago(path: str | Path, fecha: date) -> ResultadoImportacionProveedor:
    """Lee la cartola de Mercado Pago y arma los movimientos del día
    operativo indicado. Ninguna fila se descarta: lo que no se entiende
    va a `avisos`.
    """
    df = pd.read_excel(path, sheet_name=0, engine="openpyxl")

    avisos: list[str] = []
    movimientos: list[MovimientoProveedor] = []

    for _, fila in df.iterrows():
        fecha_hora = _parse_fecha_hora(fila["FECHA DE ORIGEN"])
        if fecha_hora.date() != fecha:
            continue

        tipo_original = str(fila["TIPO DE OPERACIÓN"]).strip()
        tipo = TIPOS_OPERACION.get(tipo_original)
        if tipo is None:
            avisos.append(
                f"Tipo de operación no reconocido, revisar manualmente: {tipo_original!r} "
                f"(operación {fila['ID DE OPERACIÓN EN MERCADO PAGO']})."
            )
            tipo = tipo_original

        medio_original = str(fila["TIPO DE MEDIO DE PAGO"]).strip()
        medio = MEDIOS_PAGO.get(medio_original)
        if medio is None:
            avisos.append(f"Medio de pago no reconocido: {medio_original!r}")
            medio = medio_original.lower()

        monto = float(fila["VALOR DE LA COMPRA"])

        movimientos.append(MovimientoProveedor(
            proveedor="mercado_pago",
            tipo=tipo,
            estado=tipo_original,
            fecha_hora=fecha_hora,
            fecha_abono=str(fila["FECHA DE LIBERACIÓN DEL DINERO"]) if pd.notna(fila["FECHA DE LIBERACIÓN DEL DINERO"]) else None,
            medio=medio,
            tipo_tarjeta=medio,  # Mercado Pago no informa marca de tarjeta, solo débito/crédito.
            numero_tarjeta="",  # No viene en esta cartola.
            monto_original=monto,
            monto_afecto=0,
            monto_exento=0,
            monto_vuelto=0,
            propina=0,
            monto_venta_valido_abono=monto,
            monto_anulado=0,
            monto_cuota=0,
            autorizacion=str(fila["ID DE OPERACIÓN EN MERCADO PAGO"]).strip(),
            terminal=str(fila["PLATAFORMA DE COBRO"]).strip(),
            moneda="CLP",
        ))

    return ResultadoImportacionProveedor(fecha=fecha, movimientos=movimientos, avisos=avisos)
