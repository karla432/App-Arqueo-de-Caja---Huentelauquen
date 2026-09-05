"""Caso maestro del 02-09-2026 para la cartola de Transbank (QA Golden Master).

Cifras medidas sobre el archivo real (ver README.md). Criterio de
aceptación N°2 y N°6 del diseño funcional (sección 23).
"""
from collections import Counter
from datetime import date
from pathlib import Path

import pytest

from arqueo_diario.importadores.transbank import importar_cartola_transbank

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "dia_referencia_02-09-2026"
    / "Cartola_Movimientos_02-sep-2026.xlsx"
)
DIA_REFERENCIA = date(2026, 9, 2)


@pytest.fixture(scope="module")
def resultado():
    return importar_cartola_transbank(FIXTURE, DIA_REFERENCIA)


def test_cantidad_de_movimientos(resultado):
    assert resultado.cantidad_movimientos == 295


def test_todos_son_venta_sin_anulaciones(resultado):
    tipos = Counter(m.tipo for m in resultado.movimientos)
    assert tipos == {"Venta": 295}


def test_monto_total_valido_para_abono(resultado):
    assert resultado.total_venta_valido_abono == 4_968_393


def test_desglose_por_tipo_de_tarjeta(resultado):
    conteo = Counter(m.tipo_tarjeta for m in resultado.movimientos)
    assert conteo == {
        "debito": 223,
        "mastercard": 33,
        "visa": 27,
        "prepago": 11,
        "american": 1,
    }


def test_terminales(resultado):
    conteo = Counter(m.terminal for m in resultado.movimientos)
    assert conteo == {
        "A2885575": 94,
        "A2885576": 90,
        "A2380302": 56,
        "A2885577": 55,
    }


def test_estados_pendiente_y_abonada(resultado):
    conteo = Counter(m.estado for m in resultado.movimientos)
    assert conteo == {"PENDIENTE": 233, "ABONADA": 62}


def test_autorizaciones_alfanumericas_y_sin_duplicadas(resultado):
    autorizaciones = [m.autorizacion for m in resultado.movimientos]
    alfanumericas = [a for a in autorizaciones if any(c.isalpha() for c in a)]
    assert len(alfanumericas) == 9
    assert len(autorizaciones) == len(set(autorizaciones))  # 0 duplicadas


def test_sin_avisos_de_medio_o_tarjeta_no_reconocidos(resultado):
    assert resultado.avisos == []


def test_numero_de_tarjeta_ya_viene_enmascarado(resultado):
    # Control de seguridad: nunca debe llegar un PAN completo (16 dígitos
    # seguidos) — Transbank ya entrega el número parcialmente oculto.
    for m in resultado.movimientos:
        assert "X" in m.numero_tarjeta or len(m.numero_tarjeta) <= 6
