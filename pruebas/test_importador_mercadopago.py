"""Cartola de liquidación de Mercado Pago aportada por Karla (2026-09-16):
único ejemplo real disponible hasta ahora, 3 operaciones del 10-09-2026,
todas "Pago aprobado" con "Tarjeta de débito" vía Point.
"""
from datetime import date
from pathlib import Path

import pytest

from arqueo_diario.importadores.mercadopago import importar_settlement_mercadopago

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "mercado_pago_10-09-2026"
    / "settlement_10-09-2026.xlsx"
)
DIA_REFERENCIA = date(2026, 9, 10)


@pytest.fixture(scope="module")
def resultado():
    return importar_settlement_mercadopago(FIXTURE, DIA_REFERENCIA)


def test_cantidad_de_movimientos(resultado):
    assert resultado.cantidad_movimientos == 3


def test_todos_son_venta_debito(resultado):
    assert {m.tipo for m in resultado.movimientos} == {"Venta"}
    assert {m.medio for m in resultado.movimientos} == {"debito"}


def test_monto_total_valido_para_abono(resultado):
    assert resultado.total_venta_valido_abono == 11_200 + 5_000 + 23_780


def test_proveedor_y_terminal(resultado):
    assert {m.proveedor for m in resultado.movimientos} == {"mercado_pago"}
    assert {m.terminal for m in resultado.movimientos} == {"Point"}


def test_autorizaciones_son_el_id_de_operacion_sin_duplicadas(resultado):
    autorizaciones = [m.autorizacion for m in resultado.movimientos]
    assert autorizaciones == ["177303137843", "178269062084", "178270647474"]
    assert len(autorizaciones) == len(set(autorizaciones))


def test_sin_avisos_de_medio_o_tipo_no_reconocidos(resultado):
    assert resultado.avisos == []


def test_fecha_hora_se_interpreta_en_hora_local_sin_offset(resultado):
    horas = sorted(m.fecha_hora.time().isoformat() for m in resultado.movimientos)
    assert horas == ["10:47:46", "10:52:27", "11:05:09"]


def test_filtra_por_dia_operativo(resultado):
    otro_dia = importar_settlement_mercadopago(FIXTURE, date(2026, 9, 11))
    assert otro_dia.cantidad_movimientos == 0


def test_tipo_de_operacion_no_reconocido_queda_en_avisos(tmp_path):
    import pandas as pd

    df = pd.DataFrame([{
        "ID DE OPERACIÓN EN MERCADO PAGO": "999",
        "TIPO DE MEDIO DE PAGO": "Tarjeta de débito",
        "TIPO DE OPERACIÓN": "Contracargo",
        "VALOR DE LA COMPRA": "5000.00",
        "FECHA DE ORIGEN": "2026-09-10T09:00:00.000-03:00",
        "COMISIONES + IVA": "0.00",
        "FECHA DE APROBACIÓN": "2026-09-10T09:00:01.000-03:00",
        "MONTO NETO DE LA OPERACIÓN": "5000.00",
        "IMPUESTOS COBRADOS POR RETENCIONES DE IIBB": "0.00",
        "CANAL DE VENTA": "Mercado Pago",
        "PLATAFORMA DE COBRO": "Point",
        "FECHA DE LIBERACIÓN DEL DINERO": "2026-09-10T09:00:01.000-03:00",
    }])
    archivo = tmp_path / "contracargo.xlsx"
    df.to_excel(archivo, index=False)

    resultado = importar_settlement_mercadopago(archivo, date(2026, 9, 10))
    assert resultado.cantidad_movimientos == 1
    assert "Contracargo" in resultado.avisos[0]
