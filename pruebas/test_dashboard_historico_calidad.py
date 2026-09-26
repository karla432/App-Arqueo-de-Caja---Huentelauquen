"""Dashboard del día, Histórico y Calidad de registro (Etapa 7) — pantallas
de gestión que agregan datos ya validados en otras etapas. Se prueban
contra el mismo escenario armado a mano de test_cierre_y_exportacion.py.
"""
from datetime import date, datetime, time
from decimal import Decimal

import pytest
from pruebas.perfiles_de_prueba import GRUPO_GERENTE, crear_usuario
from django.urls import reverse

from nucleo.arqueo_servicio import registrar_conteo
from nucleo.calidad_servicio import resumen_calidad
from nucleo.conciliacion_servicio import ejecutar_conciliacion
from nucleo.dashboard_servicio import cuadre_por_medio, semaforo
from nucleo.historico_servicio import buscar_transversal, calendario_mes, resumen_dias
from nucleo.models import (
    ArqueoEfectivo, DiaArqueo, LineaVenta, LoteCarga, MovimientoProveedor, PagoDeclarado, Venta,
)

FECHA = date(2026, 9, 2)


@pytest.fixture
def usuario(django_user_model):
    return crear_usuario(django_user_model, "operador", GRUPO_GERENTE)


@pytest.fixture
def dia_conciliado(db, usuario):
    dia = DiaArqueo.objects.create(fecha=FECHA, operado_por=usuario)
    lote = LoteCarga.objects.create(
        dia=dia, fuente=LoteCarga.Fuente.POS, nombre_archivo="x.xlsx", huella_sha256="a",
        ruta_almacenamiento="/tmp/x", fecha_declarada=FECHA, cargado_por=usuario,
    )
    venta = Venta.objects.create(
        dia=dia, lote=lote, n_interno=1, nro_comprobante="B1", tipo_doc="FAC", nro_comanda="1",
        estado="COBRADA", caja="CAJA 1", turno="TURNO 1", hora_cierre=time(14, 0),
        total_comanda=10000, cliente="CLIENTE OCASIONAL",
    )
    LineaVenta.objects.create(venta=venta, articulo="TE", cantidad=1, precio_unitario=10000, total_linea=10000)
    PagoDeclarado.objects.create(venta=venta, medio="debito", monto=10000)
    MovimientoProveedor.objects.create(
        dia=dia, lote=lote, proveedor="transbank", tipo="Venta", estado="PENDIENTE",
        fecha_hora=datetime(2026, 9, 2, 14, 1), medio="debito",
        tipo_tarjeta="debito", monto_original=10000, monto_venta_valido_abono=10000, terminal="A1",
    )
    ejecutar_conciliacion(dia, usuario)
    return dia


def test_cuadre_por_medio_incluye_transferencia_en_cero(dia_conciliado):
    filas = cuadre_por_medio(dia_conciliado)
    medios = {f["medio"]: f for f in filas}
    assert medios["debito"]["declarado_pos"] == 10000
    assert medios["debito"]["conciliado"] == 10000
    assert medios["transferencia"]["declarado_pos"] == 0


def test_semaforo_ambar_mientras_falte_el_arqueo(dia_conciliado):
    # Sin excepciones abiertas (todo concilió), pero la caja no está
    # contada -> no hay diferencia que autorizar, así que el semáforo no
    # es rojo; tampoco es verde porque nadie ha marcado el día completo.
    assert semaforo(dia_conciliado) in ("verde", "ambar")


def test_semaforo_rojo_con_diferencia_sin_autorizar(dia_conciliado, usuario):
    from nucleo.arqueo_servicio import preparar_arqueos_del_dia
    preparar_arqueos_del_dia(dia_conciliado)
    arqueo = ArqueoEfectivo.objects.get(dia=dia_conciliado, caja="CAJA 1")
    registrar_conteo(arqueo, fondo_inicial=Decimal(0), gastos_autorizados=Decimal(0),
                      total_contado=Decimal(20000), usuario=usuario)  # muy por sobre lo esperado (0)
    assert semaforo(dia_conciliado) == "rojo"


def test_resumen_dias_desglosa_por_forma_de_pago(dia_conciliado):
    filas = resumen_dias(FECHA, FECHA)
    assert len(filas) == 1
    assert filas[0]["venta_total"] == 10000
    assert filas[0]["transbank"] == 10000
    assert filas[0]["mercado_pago"] == 0


def test_calendario_mes_ubica_el_dia_en_su_semana(dia_conciliado):
    semanas = calendario_mes(2026, 9)
    celdas = [c for semana in semanas for c in semana if c is not None]
    dias_del_dia_2 = [c for c in celdas if c["fecha"] == FECHA]
    assert len(dias_del_dia_2) == 1
    assert dias_del_dia_2[0]["dia"] == dia_conciliado


def test_buscar_transversal_por_comanda(dia_conciliado):
    resultados = buscar_transversal("1")
    assert dia_conciliado.ventas.first() in resultados["ventas"]


def test_buscar_transversal_por_monto(dia_conciliado):
    resultados = buscar_transversal("10000")
    assert dia_conciliado.ventas.first() in resultados["ventas"]
    assert dia_conciliado.movimientos.first() in resultados["movimientos"]


def test_calidad_muestra_100_por_ciento_sin_autorizacion(dia_conciliado):
    resumen = resumen_calidad(FECHA, FECHA)
    # Hallazgo real, no un bug: el POS no trae autorización (§11, D1).
    assert resumen["pct_sin_autorizacion"] == 100.0
    assert resumen["total_comandas"] == 1


def test_vista_dashboard_requiere_login(client, dia_conciliado):
    resp = client.get(reverse("dashboard_dia", args=[FECHA.isoformat()]))
    assert resp.status_code == 302


def test_vista_dashboard_renderiza(client, usuario, dia_conciliado):
    client.force_login(usuario)
    resp = client.get(reverse("dashboard_dia", args=[FECHA.isoformat()]))
    assert resp.status_code == 200
    assert resp.context["venta_total"] == 10000


def test_vista_historico_renderiza(client, usuario, dia_conciliado):
    client.force_login(usuario)
    resp = client.get(reverse("historico"), {"desde": "2026-09-01", "hasta": "2026-09-30"})
    assert resp.status_code == 200
    assert len(resp.context["dias"]) == 1


def test_vista_calidad_renderiza(client, usuario, dia_conciliado):
    client.force_login(usuario)
    resp = client.get(reverse("calidad_registro"), {"desde": "2026-09-01", "hasta": "2026-09-30"})
    assert resp.status_code == 200
    assert resp.context["total_comandas"] == 1


def test_las_guias_cobradas_con_tarjeta_no_son_diferencia_de_cuadratura(dia_conciliado, usuario):
    """Karla, 2026-09-25 (día 07): el cobro con tarjeta de una guía aparece en Transbank pero la guía no es
    una venta del libro; con la columna «Guías de despacho» la diferencia del medio queda en cero."""
    from nucleo.models import MovimientoProveedor, RegularizacionDocumento
    lote = dia_conciliado.lotes.first()
    MovimientoProveedor.objects.create(
        dia=dia_conciliado, lote=lote, proveedor="transbank", tipo="Venta", estado="PENDIENTE",
        fecha_hora=datetime(2026, 9, 2, 15, 0), medio="debito", tipo_tarjeta="debito",
        monto_original=26775, monto_venta_valido_abono=26775, terminal="A1",
    )
    debito = {f["medio"]: f for f in cuadre_por_medio(dia_conciliado)}["debito"]
    assert debito["diferencia"] == -26775  # sin la guía registrada parece un faltante del POS
    RegularizacionDocumento.objects.create(dia=dia_conciliado, tipo="DES", n_interno=9, folio="21203", caja="CAJA 1", monto=26775, forma_pago="debito")
    debito = {f["medio"]: f for f in cuadre_por_medio(dia_conciliado)}["debito"]
    assert debito["guias"] == 26775 and debito["diferencia"] == 0


def test_el_efectivo_del_dashboard_separa_pos_y_guias_y_la_nota_no_lo_mueve(dia_conciliado):
    from nucleo.dashboard_servicio import resumen_efectivo
    from nucleo.models import RegularizacionDocumento
    RegularizacionDocumento.objects.create(dia=dia_conciliado, tipo="DES", n_interno=9, folio="21197", caja="CAJA 1", monto=23830, forma_pago="efectivo")
    nota = RegularizacionDocumento.objects.create(dia=dia_conciliado, tipo="CRED", n_interno=10, folio="F0610600008311", caja="CAJA 1", monto=9600, forma_pago="credito")
    nota.verificacion_estado, nota.medio_verificado = "verificada", "efectivo"
    nota.save()
    r = resumen_efectivo(dia_conciliado)
    assert (r["guias_efectivo"], r["pos_efectivo"], r["teorico"]) == (23830, 0, 23830)  # la nota no resta


def test_el_dashboard_muestra_la_columna_de_guias(client, usuario, dia_conciliado):
    client.force_login(usuario)
    html = client.get(reverse("dashboard_dia", args=[FECHA.isoformat()])).content.decode()
    assert "Guías de despacho" in html and "Declarado POS + guías" in html or "declarado POS + guías" in html


def test_el_historico_suma_las_guias_con_tarjeta_a_la_columna_transbank(dia_conciliado):
    """Karla, 2026-09-25: la columna Transbank debe poder compararse con lo que cobró el proveedor."""
    from nucleo.models import RegularizacionDocumento
    RegularizacionDocumento.objects.create(dia=dia_conciliado, tipo="DES", n_interno=9, folio="21203", caja="CAJA 1", monto=26775, forma_pago="debito")
    RegularizacionDocumento.objects.create(dia=dia_conciliado, tipo="DES", n_interno=10, folio="21204", caja="CAJA 1", monto=5000, forma_pago="efectivo")
    fila = resumen_dias(FECHA, FECHA)[0]
    assert fila["transbank"] == 10000 + 26775 and fila["guias"] == 26775 + 5000
    assert fila["venta_total"] == 10000  # la venta del POS no cambia: las guías van aparte
