"""Caso maestro del 02-09-2026 (QA Golden Master).

Estas cifras son mediciones sobre el archivo real, no estimaciones (ver
README.md). Si este test deja de pasar, algo en el importador cambió el
resultado del día de referencia: hay que explicar la diferencia o aprobar
una nueva línea base antes de seguir. Criterio de aceptación N°1 del diseño
funcional (sección 23).
"""
from datetime import date
from pathlib import Path

import pytest

from arqueo_diario.importadores.pos import importar_ventas_por_articulo

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "dia_referencia_02-09-2026"
    / "Captura_Ventas_Sala_de_Ventas_02.09.26.xlsx"
)
DIA_REFERENCIA = date(2026, 9, 2)


@pytest.fixture(scope="module")
def resultado():
    return importar_ventas_por_articulo(FIXTURE, DIA_REFERENCIA)


def test_cantidad_comandas_y_lineas(resultado):
    assert resultado.cantidad_comandas == 336
    assert resultado.cantidad_lineas == 686


def test_venta_total_deduplicada(resultado):
    # La trampa critica: sumar por linea sin deduplicar da 686 lineas
    # multiplicadas, muy por encima de la venta real.
    assert resultado.total_venta == 5_274_235


def test_desglose_por_medio_de_pago(resultado):
    totales = resultado.total_por_medio
    assert totales["efectivo"] == 501_461
    assert totales["transferencia"] == 0
    assert totales["debito"] == 3_770_006
    assert totales["credito"] == 1_002_768


def test_suma_de_medios_igual_al_total(resultado):
    totales = resultado.total_por_medio
    assert sum(totales.values()) == resultado.total_venta


def test_sin_avisos_de_descuadre_en_el_dia_referencia(resultado):
    # El día de referencia no tiene comandas con total distinto a la suma
    # de sus líneas o de sus medios declarados.
    assert resultado.avisos == []


def test_comanda_654747_caso_documentado(resultado):
    # Caso usado en el diseño funcional (§0.3) para mostrar que
    # VENTA CONSOLIDADA contradice a VENTAS POR ARTICULO: acá se fija lo
    # que VENTAS POR ARTICULO (la fuente que manda) dice de esa comanda.
    venta = next(v for v in resultado.ventas if v.n_interno == 654747)
    assert venta.nro_comprobante == "B0390301666467"
    assert venta.total_comanda == 17_600
    assert venta.caja == "CAJA 4"
    assert [p.medio for p in venta.pagos] == ["debito"]
