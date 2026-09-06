"""Documentos pendientes de emisión al cierre (decisión de Karla,
2026-09-06): guías de despacho (comprobante "GO...") y pagos de
Transbank sin boleta aceptados como otros ingresos. Ambos casos exigen
un aprobador para cerrar el día, con nivel escalado según cuántos casos
haya: Jefe de Contabilidad hasta 10, Gerente de Administración y
Finanzas por sobre eso.
"""
from datetime import date, datetime, time
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from nucleo.cierre_servicio import CierreInvalido, aprobar_y_cerrar_dia, puede_aprobar_usuario
from nucleo.documentos_servicio import (
    AsignacionInvalida, GRUPO_GERENTE_ADM_FINANZAS, GRUPO_JEFE_CONTABILIDAD,
    asignar_factura_a_excepcion, asignar_factura_a_venta, guias_pendientes,
    nivel_requerido, nivel_usuario, otros_ingresos_pendientes, total_documentos_pendientes,
)
from nucleo.models import (
    ArqueoEfectivo, DiaArqueo, Excepcion, LoteCarga, MotivoResolucion,
    MovimientoProveedor, PagoDeclarado, Venta,
)
from nucleo.resolucion_servicio import resolver_excepcion

FECHA = date(2026, 9, 2)


@pytest.fixture
def jefe(django_user_model):
    u = django_user_model.objects.create_user(username="jefe", password="x")
    grupo, _ = Group.objects.get_or_create(name=GRUPO_JEFE_CONTABILIDAD)
    u.groups.add(grupo)
    return u


@pytest.fixture
def gerente(django_user_model):
    u = django_user_model.objects.create_user(username="gerente", password="x", is_superuser=True)
    grupo, _ = Group.objects.get_or_create(name=GRUPO_GERENTE_ADM_FINANZAS)
    u.groups.add(grupo)
    return u


@pytest.fixture
def sin_rol(django_user_model):
    return django_user_model.objects.create_user(username="analista", password="x")


@pytest.fixture
def dia_con_guia(db, sin_rol):
    dia = DiaArqueo.objects.create(fecha=FECHA, operado_por=sin_rol)
    lote = LoteCarga.objects.create(
        dia=dia, fuente=LoteCarga.Fuente.POS, nombre_archivo="x.xlsx", huella_sha256="a",
        ruta_almacenamiento="/tmp/x", fecha_declarada=FECHA, cargado_por=sin_rol,
    )
    venta_guia = Venta.objects.create(
        dia=dia, lote=lote, n_interno=1, nro_comprobante="GO000123", tipo_doc="FAC",
        nro_comanda="1", estado="COBRADA", caja="CAJA 1", turno="TURNO 1",
        hora_cierre=time(10, 0), total_comanda=15000, cliente="CLIENTE OCASIONAL",
    )
    PagoDeclarado.objects.create(venta=venta_guia, medio="debito", monto=15000)
    arqueo = ArqueoEfectivo.objects.create(dia=dia, caja="CAJA 1", contado=0, diferencia=0)
    return dia, venta_guia


def test_venta_con_comprobante_go_se_identifica_como_guia(dia_con_guia):
    dia, venta_guia = dia_con_guia
    assert venta_guia.es_guia_de_despacho is True


def test_guia_pendiente_aparece_hasta_asignar_factura(dia_con_guia, jefe):
    dia, venta_guia = dia_con_guia
    assert list(guias_pendientes(dia)) == [venta_guia]
    asignar_factura_a_venta(venta_guia, "F-9001", jefe)
    assert list(guias_pendientes(dia)) == []
    venta_guia.refresh_from_db()
    assert venta_guia.factura_asociada == "F-9001"


def test_no_se_puede_asignar_factura_vacia(dia_con_guia, jefe):
    _, venta_guia = dia_con_guia
    with pytest.raises(AsignacionInvalida):
        asignar_factura_a_venta(venta_guia, "   ", jefe)


def test_nivel_requerido_escala_sobre_10_casos():
    assert nivel_requerido(0) == 0
    assert nivel_requerido(1) == 1
    assert nivel_requerido(10) == 1
    assert nivel_requerido(11) == 2


def test_nivel_de_usuario_por_grupo(jefe, gerente, sin_rol):
    assert nivel_usuario(jefe) == 1
    assert nivel_usuario(gerente) == 2
    assert nivel_usuario(sin_rol) == 0


def test_no_se_puede_cerrar_con_guia_pendiente_sin_rol(dia_con_guia, sin_rol):
    dia, _ = dia_con_guia
    with pytest.raises(CierreInvalido):
        aprobar_y_cerrar_dia(dia, sin_rol)


def test_jefe_contabilidad_cierra_con_hasta_10_pendientes(dia_con_guia, jefe):
    dia, _ = dia_con_guia
    puede, razon = puede_aprobar_usuario(dia, jefe)
    assert puede is True
    aprobar_y_cerrar_dia(dia, jefe)
    dia.refresh_from_db()
    assert dia.estado == DiaArqueo.Estado.CERRADO


def test_mas_de_10_pendientes_exige_gerente(db, sin_rol, jefe, gerente):
    dia = DiaArqueo.objects.create(fecha=FECHA, operado_por=sin_rol)
    lote = LoteCarga.objects.create(
        dia=dia, fuente=LoteCarga.Fuente.POS, nombre_archivo="x.xlsx", huella_sha256="a",
        ruta_almacenamiento="/tmp/x", fecha_declarada=FECHA, cargado_por=sin_rol,
    )
    for i in range(11):
        v = Venta.objects.create(
            dia=dia, lote=lote, n_interno=i, nro_comprobante=f"GO{i:06d}", tipo_doc="FAC",
            nro_comanda=str(i), estado="COBRADA", caja="CAJA 1", turno="TURNO 1",
            hora_cierre=time(10, 0), total_comanda=1000, cliente="CLIENTE OCASIONAL",
        )
        PagoDeclarado.objects.create(venta=v, medio="debito", monto=1000)
    ArqueoEfectivo.objects.create(dia=dia, caja="CAJA 1", contado=0, diferencia=0)

    assert total_documentos_pendientes(dia) == 11
    puede_jefe, _ = puede_aprobar_usuario(dia, jefe)
    assert puede_jefe is False
    puede_gerente, _ = puede_aprobar_usuario(dia, gerente)
    assert puede_gerente is True


@pytest.fixture
def dia_con_otros_ingresos(db, sin_rol):
    dia = DiaArqueo.objects.create(fecha=FECHA, operado_por=sin_rol)
    lote = LoteCarga.objects.create(
        dia=dia, fuente=LoteCarga.Fuente.POS, nombre_archivo="x.xlsx", huella_sha256="a",
        ruta_almacenamiento="/tmp/x", fecha_declarada=FECHA, cargado_por=sin_rol,
    )
    movimiento = MovimientoProveedor.objects.create(
        dia=dia, lote=lote, proveedor="transbank", tipo="Venta", estado="PENDIENTE",
        fecha_hora=datetime(2026, 9, 2, 12, 0), medio="debito", tipo_tarjeta="debito",
        monto_original=8000, monto_venta_valido_abono=8000, terminal="A1",
    )
    excepcion = Excepcion.objects.create(
        dia=dia, tipo=Excepcion.Tipo.MOVIMIENTO_SIN_PAGO, monto_en_juego=8000, movimiento=movimiento,
        decision_propuesta="Buscar comanda no cerrada.",
    )
    return dia, excepcion


def test_aceptar_movimiento_sin_pago_como_otros_ingresos(dia_con_otros_ingresos, sin_rol):
    dia, excepcion = dia_con_otros_ingresos
    motivo = MotivoResolucion.objects.get(codigo="forma_pago_sin_boleta")
    resolver_excepcion(excepcion, resolucion="aceptar", motivo_id=motivo.pk, comentario="", usuario=sin_rol)
    excepcion.refresh_from_db()
    assert excepcion.abierta is False
    assert excepcion.resolucion == "aceptar"
    assert list(otros_ingresos_pendientes(dia)) == [excepcion]


def test_asignar_boleta_a_otros_ingresos(dia_con_otros_ingresos, sin_rol, jefe):
    dia, excepcion = dia_con_otros_ingresos
    motivo = MotivoResolucion.objects.get(codigo="forma_pago_sin_boleta")
    resolver_excepcion(excepcion, resolucion="aceptar", motivo_id=motivo.pk, comentario="", usuario=sin_rol)
    excepcion.refresh_from_db()

    asignar_factura_a_excepcion(excepcion, "B-500", jefe)
    excepcion.refresh_from_db()
    assert excepcion.factura_asociada == "B-500"
    assert list(otros_ingresos_pendientes(dia)) == []


def test_vista_asignar_factura_requiere_login(client, dia_con_guia):
    dia, venta_guia = dia_con_guia
    resp = client.post(reverse("asignar_factura_venta", args=[venta_guia.pk]), {"factura": "F-1"})
    assert resp.status_code == 302
    assert "/login/" in resp.url
