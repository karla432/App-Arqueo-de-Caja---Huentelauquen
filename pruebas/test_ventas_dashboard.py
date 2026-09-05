"""Tablero de ventas mensual y anual (Etapa 8) — la regla de la fuente
única es lo más importante de probar: si un día no está Cerrado o
Exportado, su venta no puede aparecer en el período, tiene que aparecer
como excluido con su monto provisorio.
"""
from datetime import date, time

import pytest

from nucleo.models import DiaArqueo, LineaVenta, LoteCarga, PagoDeclarado, Venta
from nucleo.ventas_dashboard_servicio import rango_del_atajo, resumen_periodo


@pytest.fixture
def usuario(django_user_model):
    return django_user_model.objects.create_user(username="operador", password="x")


def _crear_dia(fecha, estado, usuario, ventas_data):
    """ventas_data: lista de (caja, total, hora, [(articulo, cant, precio)])."""
    dia = DiaArqueo.objects.create(fecha=fecha, estado=estado)
    lote = LoteCarga.objects.create(
        dia=dia, fuente=LoteCarga.Fuente.POS, nombre_archivo="x.xlsx", huella_sha256=str(fecha),
        ruta_almacenamiento="/tmp/x", fecha_declarada=fecha, cargado_por=usuario,
    )
    for i, (caja, total, hora, lineas) in enumerate(ventas_data):
        venta = Venta.objects.create(
            dia=dia, lote=lote, n_interno=int(fecha.strftime("%Y%m%d")) * 10 + i,
            nro_comprobante=f"B{i}", tipo_doc="FAC", nro_comanda=str(i), estado="COBRADA",
            caja=caja, turno="TURNO 1", hora_cierre=hora, total_comanda=total,
            cliente="CLIENTE OCASIONAL",
        )
        PagoDeclarado.objects.create(venta=venta, medio="debito", monto=total)
        for articulo, cant, precio in lineas:
            LineaVenta.objects.create(venta=venta, articulo=articulo, cantidad=cant,
                                       precio_unitario=precio, total_linea=cant * precio)
    return dia


@pytest.fixture
def periodo_con_datos(db, usuario):
    cerrado_1 = _crear_dia(
        date(2026, 9, 1), DiaArqueo.Estado.CERRADO, usuario,
        [("CAJA 1", 10000, time(10, 0), [("EMPANADA", 4, 2500)])],
    )
    cerrado_2 = _crear_dia(
        date(2026, 9, 2), DiaArqueo.Estado.EXPORTADO, usuario,
        [("CAJA 1", 20000, time(15, 0), [("EMPANADA", 8, 2500)])],
    )
    pendiente = _crear_dia(
        date(2026, 9, 3), DiaArqueo.Estado.CONCILIADO_CON_EXCEPCIONES, usuario,
        [("CAJA 2", 99999, time(12, 0), [("TE", 1, 99999)])],
    )
    anio_anterior = _crear_dia(
        date(2025, 9, 1), DiaArqueo.Estado.EXPORTADO, usuario,
        [("CAJA 1", 5000, time(10, 0), [("EMPANADA", 2, 2500)])],
    )
    return {"cerrado_1": cerrado_1, "cerrado_2": cerrado_2, "pendiente": pendiente, "anio_anterior": anio_anterior}


def test_solo_suma_dias_cerrados_o_exportados(periodo_con_datos):
    resumen = resumen_periodo(date(2026, 9, 1), date(2026, 9, 30))
    assert resumen["kpis"]["venta_neta"]["valor"] == 30000  # 10.000 + 20.000, no los 99.999 pendientes
    assert resumen["kpis"]["comandas"]["valor"] == 2


def test_dia_pendiente_aparece_excluido_con_su_monto(periodo_con_datos):
    resumen = resumen_periodo(date(2026, 9, 1), date(2026, 9, 30))
    assert len(resumen["dias_excluidos"]) == 1
    excluido = resumen["dias_excluidos"][0]
    assert excluido["dia"].fecha == date(2026, 9, 3)
    assert excluido["monto_provisorio"] == 99999


def test_comparacion_contra_mismo_periodo_anio_anterior(periodo_con_datos):
    resumen = resumen_periodo(date(2026, 9, 1), date(2026, 9, 30))
    # 30.000 este período vs 5.000 el año pasado -> +500%
    assert resumen["kpis"]["venta_neta"]["variacion_pct"] == 500.0


def test_top_productos_acumula_porcentaje(periodo_con_datos):
    resumen = resumen_periodo(date(2026, 9, 1), date(2026, 9, 30))
    productos = {p["nombre"]: p for p in resumen["top_productos"]}
    assert "EMPANADA" in productos
    assert productos["EMPANADA"]["pct_acumulado"] == 100.0  # único producto en días cerrados


def test_venta_por_caja(periodo_con_datos):
    resumen = resumen_periodo(date(2026, 9, 1), date(2026, 9, 30))
    por_caja = dict(resumen["por_caja"])
    assert por_caja["CAJA 1"]["venta"] == 30000
    assert "CAJA 2" not in por_caja  # esa venta está en el día pendiente, excluida


def test_periodo_sin_ningun_dia_cerrado_no_falla(db):
    resumen = resumen_periodo(date(2020, 1, 1), date(2020, 1, 31))
    assert resumen["kpis"]["venta_neta"]["valor"] == 0
    assert resumen["kpis"]["ticket_promedio"]["valor"] == 0  # sin división por cero
    assert resumen["dias_excluidos"] == []


def test_rango_del_atajo_mes():
    desde, hasta = rango_del_atajo("mes", referencia=date(2026, 9, 15))
    assert (desde, hasta) == (date(2026, 9, 1), date(2026, 9, 30))


def test_rango_del_atajo_trimestre():
    desde, hasta = rango_del_atajo("trimestre", referencia=date(2026, 9, 15))
    assert (desde, hasta) == (date(2026, 7, 1), date(2026, 9, 30))


def test_rango_del_atajo_anio_en_curso():
    desde, hasta = rango_del_atajo("anio_en_curso", referencia=date(2026, 9, 15))
    assert (desde, hasta) == (date(2026, 1, 1), date(2026, 12, 31))


def test_vista_requiere_login(client):
    resp = client.get("/")
    assert resp.status_code == 302


def test_vista_renderiza_con_atajo(client, usuario, periodo_con_datos):
    client.force_login(usuario)
    resp = client.get("/", {"atajo": "personalizado", "desde": "2026-09-01", "hasta": "2026-09-30"})
    assert resp.status_code == 200
    assert resp.context["kpis"]["venta_neta"]["valor"] == 30000
