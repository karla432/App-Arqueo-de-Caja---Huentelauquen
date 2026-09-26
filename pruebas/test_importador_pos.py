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

from arqueo_diario.importadores.pos import importar_ventas_con_libro_contable, importar_ventas_por_articulo

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "dia_referencia_02-09-2026"
    / "Captura_Ventas_Sala_de_Ventas_02.09.26.xlsx"
)
DIA_REFERENCIA = date(2026, 9, 2)

FIXTURES_01_09 = Path(__file__).parent / "fixtures" / "dia_referencia_01-09-2026"
FIXTURE_VENTAS_01_09 = FIXTURES_01_09 / "ventas_por_articulo_01-09-2026.xlsx"
FIXTURE_LIBRO_01_09 = FIXTURES_01_09 / "libro_contable_01-09-2026.xlsx"
DIA_01_09 = date(2026, 9, 1)


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


# Caso real del 01-09-2026 aportado por Karla (2026-09-16): dos archivos
# tal cual salen de Resto (VENTAS POR ARTICULO sin N_INTERNO + libro
# contable), sin ninguna consolidación manual — reemplaza el trabajo que
# antes hacía a mano pegando el N_INTERNO ella misma.


@pytest.fixture(scope="module")
def resultado_con_libro_contable():
    return importar_ventas_con_libro_contable(FIXTURE_VENTAS_01_09, FIXTURE_LIBRO_01_09, DIA_01_09)


def test_libro_contable_cantidad_comandas_y_lineas(resultado_con_libro_contable):
    # 240 comprobantes en el reporte de ventas, menos la fila de totales
    # del pie del reporte (sin FECHA, se filtra sola) = 239 comandas.
    assert resultado_con_libro_contable.cantidad_comandas == 239
    assert resultado_con_libro_contable.cantidad_lineas == 558


def test_libro_contable_sin_comprobantes_huerfanos(resultado_con_libro_contable):
    # Las 239 comandas del reporte de ventas calzan una a una contra el
    # libro contable — ninguna se queda sin N_INTERNO.
    assert resultado_con_libro_contable.avisos == []


def test_libro_contable_desglose_por_medio_de_pago(resultado_con_libro_contable):
    totales = resultado_con_libro_contable.total_por_medio
    assert totales["efectivo"] == 714_889
    assert totales["transferencia"] == 0
    assert totales["debito"] == 2_621_233
    assert totales["credito"] == 682_247
    assert sum(totales.values()) == resultado_con_libro_contable.total_venta


def test_libro_contable_n_interno_recuperado_del_cruce(resultado_con_libro_contable):
    # La primera comanda del día: N_INTERNO no viene en el reporte de
    # ventas, se recupera cruzando NRO. COMPROBANTE contra el libro
    # contable (N_COMP en ese archivo).
    venta = next(v for v in resultado_con_libro_contable.ventas if v.nro_comprobante == "B0390301666228")
    assert venta.n_interno == 654506
    assert venta.total_comanda == 5000
    assert [p.medio for p in venta.pagos] == ["debito"]


def test_libro_contable_comprobante_sin_match_queda_en_avisos_y_no_se_importa(tmp_path):
    import pandas as pd

    ventas_df = pd.DataFrame([{
        "SUCURSAL": "Huentelauquen Central", "CLIENTE": "CLIENTE OCASIONAL",
        "NRO. COMPROBANTE": "B999999999999", "TIPO_DOC": "FAC", "NRO. COMANDA": 999999,
        "ESTADO COMANDA": "COBRADA", "HORA APERTURA": "10:00 HS.", "HORA CIERRE": "10:01 HS.",
        "CAJA CIERRE": "CAJA 1", "FECHA": pd.Timestamp(DIA_01_09), "ARTíCULO": "TE",
        "CANTIDAD": 1, "PRECIO UNITARIO": 1000, "TOTAL POR ARTICULO": 1000, "TOTAL_COMADA": 1000,
        "EFECTVO": 1000, "TRANSFERENCIAS": 0, "TARJETA DE DEBITO": 0, "TARJETA DE CREDITO": 0,
        "TURNO": "TURNO CAJA 1",
    }])
    archivo_ventas = tmp_path / "ventas.xlsx"
    ventas_df.to_excel(archivo_ventas, sheet_name="VENTAS POR ARTICULO", index=False)

    libro_df = pd.DataFrame([{
        "N_INTERNO": 1, "FEC_COMPR": pd.Timestamp(DIA_01_09), "COD_COMP": "FAC", "N_COMP": "OTRO-COMPROBANTE",
    }])
    archivo_libro = tmp_path / "libro.xlsx"
    libro_df.to_excel(archivo_libro, index=False)

    resultado = importar_ventas_con_libro_contable(archivo_ventas, archivo_libro, DIA_01_09)
    assert resultado.cantidad_comandas == 0
    assert "B999999999999" in resultado.avisos[0]
