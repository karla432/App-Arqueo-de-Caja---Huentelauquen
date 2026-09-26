"""Anulaciones de Transbank (caso de uso pedido por Karla, 2026-09-06).
Sin ningún ejemplo real todavía — armado a mano según la estructura de
columnas real de Transbank (que trae "Monto anulado" como campo propio)
y la regla del diseño funcional (§8: se tratan como movimientos con
signo, no se eliminan). A validar contra un caso real esta semana.
"""
from datetime import date, datetime, time

import pytest
from pruebas.perfiles_de_prueba import GRUPO_ENCARGADO, crear_usuario

from nucleo.anulaciones_servicio import es_movimiento_anulado, procesar_anulaciones
from nucleo.conciliacion_servicio import ejecutar_conciliacion
from nucleo.models import DiaArqueo, Excepcion, LoteCarga, MovimientoProveedor, PagoDeclarado, Venta

FECHA = date(2026, 9, 2)


@pytest.fixture
def usuario(django_user_model):
    return crear_usuario(django_user_model, "operador", GRUPO_ENCARGADO, password="x")


@pytest.fixture
def dia(db):
    return DiaArqueo.objects.create(fecha=FECHA)


@pytest.fixture
def lote(dia, usuario):
    return LoteCarga.objects.create(
        dia=dia, fuente=LoteCarga.Fuente.TRANSBANK, nombre_archivo="x.xlsx", huella_sha256="a",
        ruta_almacenamiento="/tmp/x", fecha_declarada=FECHA, cargado_por=usuario,
    )


def _movimiento(dia, lote, **kwargs):
    base = dict(
        dia=dia, lote=lote, proveedor="transbank", tipo="Venta", estado="PENDIENTE",
        fecha_hora=datetime(2026, 9, 2, 12, 0), medio="debito", tipo_tarjeta="debito",
        monto_original=10000, monto_venta_valido_abono=10000, monto_anulado=0,
        autorizacion="AUTH1", terminal="A1",
    )
    base.update(kwargs)
    return MovimientoProveedor.objects.create(**base)


def test_es_movimiento_anulado_por_tipo():
    m = MovimientoProveedor(tipo="Anulación", monto_anulado=0)
    assert es_movimiento_anulado(m) is True


def test_es_movimiento_anulado_por_monto():
    m = MovimientoProveedor(tipo="Venta", monto_anulado=10000)
    assert es_movimiento_anulado(m) is True


def test_no_anulado():
    m = MovimientoProveedor(tipo="Venta", monto_anulado=0)
    assert es_movimiento_anulado(m) is False


def test_venta_y_anulacion_con_pos_tambien_anulado_se_compensan_sin_excepcion(dia, lote):
    original = _movimiento(dia, lote, autorizacion="AUTH1")
    anulacion = _movimiento(
        dia, lote, tipo="Anulación", autorizacion="AUTH1",
        monto_venta_valido_abono=0, monto_anulado=10000,
    )
    venta = Venta.objects.create(
        dia=dia, lote=lote, n_interno=1, nro_comprobante="B1", tipo_doc="FAC", nro_comanda="1",
        estado="ANULADA", caja="CAJA 1", turno="TURNO 1", hora_cierre=time(12, 0),
        total_comanda=10000, cliente="CLIENTE OCASIONAL",
    )
    PagoDeclarado.objects.create(venta=venta, medio="debito", monto=10000)

    resultado = procesar_anulaciones(dia)
    assert resultado["ids_excluidos"] == {original.pk, anulacion.pk}
    assert resultado["excepciones_creadas"] == 0
    assert not Excepcion.objects.filter(dia=dia).exists()


def test_anulacion_con_pos_activo_genera_excepcion_de_riesgo(dia, lote):
    original = _movimiento(dia, lote, autorizacion="AUTH1")
    _movimiento(dia, lote, tipo="Anulación", autorizacion="AUTH1", monto_venta_valido_abono=0, monto_anulado=10000)
    venta = Venta.objects.create(
        dia=dia, lote=lote, n_interno=1, nro_comprobante="B1", tipo_doc="FAC", nro_comanda="1",
        estado="COBRADA",  # el POS NO la anuló -> riesgo real
        caja="CAJA 1", turno="TURNO 1", hora_cierre=time(12, 0),
        total_comanda=10000, cliente="CLIENTE OCASIONAL",
    )
    PagoDeclarado.objects.create(venta=venta, medio="debito", monto=10000)

    resultado = procesar_anulaciones(dia)
    assert resultado["excepciones_creadas"] == 1
    excepcion = Excepcion.objects.get(dia=dia)
    assert excepcion.tipo == Excepcion.Tipo.ANULACION_DESCALZADA
    assert excepcion.movimiento_id == original.pk
    assert "sigue activa" in excepcion.decision_propuesta


def test_anulacion_sin_original_identificable_genera_excepcion(dia, lote):
    anulacion = _movimiento(
        dia, lote, tipo="Anulación", autorizacion="AUTH-DESCONOCIDA",
        monto_venta_valido_abono=0, monto_anulado=5000,
    )
    resultado = procesar_anulaciones(dia)
    assert resultado["excepciones_creadas"] == 1
    excepcion = Excepcion.objects.get(dia=dia)
    assert excepcion.movimiento_id == anulacion.pk
    assert excepcion.monto_en_juego == 5000


def test_reconciliar_no_duplica_la_excepcion_de_anulacion(dia, lote, usuario):
    _movimiento(dia, lote, autorizacion="AUTH1")
    _movimiento(dia, lote, tipo="Anulación", autorizacion="AUTH1", monto_venta_valido_abono=0, monto_anulado=10000)
    Venta.objects.create(
        dia=dia, lote=lote, n_interno=1, nro_comprobante="B1", tipo_doc="FAC", nro_comanda="1",
        estado="COBRADA", caja="CAJA 1", turno="TURNO 1", hora_cierre=time(12, 0),
        total_comanda=10000, cliente="CLIENTE OCASIONAL",
    ).pagos.create(medio="debito", monto=10000)

    ejecutar_conciliacion(dia, usuario)
    assert Excepcion.objects.filter(dia=dia, tipo="anulacion_descalzada").count() == 1
    ejecutar_conciliacion(dia, usuario)  # reconciliar es idempotente
    assert Excepcion.objects.filter(dia=dia, tipo="anulacion_descalzada").count() == 1


def test_movimientos_anulados_no_llegan_al_motor_normal(dia, lote, usuario):
    _movimiento(dia, lote, autorizacion="AUTH1")
    _movimiento(dia, lote, tipo="Anulación", autorizacion="AUTH1", monto_venta_valido_abono=0, monto_anulado=10000)
    venta = Venta.objects.create(
        dia=dia, lote=lote, n_interno=1, nro_comprobante="B1", tipo_doc="FAC", nro_comanda="1",
        estado="ANULADA", caja="CAJA 1", turno="TURNO 1", hora_cierre=time(12, 0),
        total_comanda=10000, cliente="CLIENTE OCASIONAL",
    )
    PagoDeclarado.objects.create(venta=venta, medio="debito", monto=10000)

    resultado = ejecutar_conciliacion(dia, usuario)
    # Ni coincidencia ni excepción "normal" para el par anulado: quedó
    # resuelto por procesar_anulaciones antes de llegar al motor.
    assert len(resultado.coincidencias) == 0
    assert len(resultado.excepciones) == 0
