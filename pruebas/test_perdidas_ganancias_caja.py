"""Pérdidas/ganancias de caja (Karla, 2026-09-16): un pago con tarjeta
que el POS declaró y que no tiene (o no calza con) un cobro real del
proveedor se registra contra una cuenta de pérdida/ganancia,
identificando a la cajera responsable para poder cobrarle. Sobre
$50.000 exige el visto bueno del Jefe de Contabilidad antes de poder
cerrar el día (dos pasos, igual que guías de despacho y otros ingresos).
"""
from datetime import date, datetime, time

import pytest
from nucleo.permisos import PermisoDenegado
from pruebas.perfiles_de_prueba import GRUPO_ENCARGADO, crear_usuario
from django.contrib.auth.models import Group

from nucleo.arqueo_servicio import AsignacionCajeraInvalida, asignar_cajera
from nucleo.cierre_servicio import puede_aprobar_usuario
from nucleo.documentos_servicio import GRUPO_JEFE_CONTABILIDAD
from nucleo.models import (
    ArqueoEfectivo, Cajera, Coincidencia, CuentaContable, DiaArqueo, Excepcion, LoteCarga,
    MotivoResolucion, MovimientoProveedor, PagoDeclarado, Venta, VersionMotor,
)
from nucleo.perdidas_servicio import (
    UMBRAL_VISADO_PERDIDA, VisadoInvalido, perdidas_pendientes_de_visado,
    puede_cerrar_con_perdidas_pendientes, visar_perdida,
)
from nucleo.resolucion_servicio import ResolucionInvalida, resolver_excepcion

FECHA = date(2026, 9, 2)


@pytest.fixture
def usuario(django_user_model):
    return crear_usuario(django_user_model, "operador", GRUPO_ENCARGADO, password="x")


@pytest.fixture
def jefe(django_user_model):
    u = django_user_model.objects.create_user(username="jefe", password="x")
    grupo, _ = Group.objects.get_or_create(name=GRUPO_JEFE_CONTABILIDAD)
    u.groups.add(grupo)
    return u


@pytest.fixture
def motivo():
    return MotivoResolucion.objects.get(codigo="perdida_ganancia_caja")


@pytest.fixture
def cuenta_perdida():
    return CuentaContable.objects.get(codigo="6501004")


@pytest.fixture
def cuenta_ganancia():
    return CuentaContable.objects.get(codigo="6501003")


@pytest.fixture
def cajera():
    return Cajera.objects.create(nombre="Juanita Pérez")


@pytest.fixture
def dia(db):
    return DiaArqueo.objects.create(fecha=FECHA)


@pytest.fixture
def lote_pos(dia, usuario):
    return LoteCarga.objects.create(
        dia=dia, fuente=LoteCarga.Fuente.POS, nombre_archivo="x.xlsx", huella_sha256="a",
        ruta_almacenamiento="/tmp/x", fecha_declarada=FECHA, cargado_por=usuario,
    )


def _venta_con_pago(dia, lote, monto, n_interno=1):
    venta = Venta.objects.create(
        dia=dia, lote=lote, n_interno=n_interno, nro_comprobante=f"B{n_interno}", tipo_doc="FAC",
        nro_comanda=str(n_interno), estado="COBRADA", caja="CAJA 4", turno="TURNO CAJA 4",
        hora_cierre=time(16, 9), total_comanda=monto, cliente="CLIENTE OCASIONAL",
    )
    return PagoDeclarado.objects.create(venta=venta, medio="credito", monto=monto)


@pytest.fixture
def excepcion_pago_sin_movimiento(dia, lote_pos):
    pago = _venta_con_pago(dia, lote_pos, 32680)
    return Excepcion.objects.create(
        dia=dia, tipo=Excepcion.Tipo.PAGO_SIN_MOVIMIENTO, monto_en_juego=32680, pago=pago,
        decision_propuesta="Sin cobro del proveedor para esta comanda.",
    )


def test_perdida_total_sin_ningun_movimiento(excepcion_pago_sin_movimiento, motivo, cuenta_perdida, cajera, usuario):
    resolver_excepcion(
        excepcion_pago_sin_movimiento, resolucion="perdida_ganancia", motivo_id=motivo.pk,
        comentario="", usuario=usuario, cuenta_contable_id=cuenta_perdida.pk, cajera_id=cajera.pk,
    )
    excepcion_pago_sin_movimiento.refresh_from_db()
    assert excepcion_pago_sin_movimiento.abierta is False
    assert excepcion_pago_sin_movimiento.cuenta_contable == cuenta_perdida
    assert excepcion_pago_sin_movimiento.cajera_responsable == cajera
    assert excepcion_pago_sin_movimiento.monto_perdida_ganancia == 32680


def test_requiere_cuenta_contable(excepcion_pago_sin_movimiento, motivo, cajera, usuario):
    with pytest.raises(ResolucionInvalida):
        resolver_excepcion(
            excepcion_pago_sin_movimiento, resolucion="perdida_ganancia", motivo_id=motivo.pk,
            comentario="", usuario=usuario, cajera_id=cajera.pk,
        )


def test_requiere_cajera(excepcion_pago_sin_movimiento, motivo, cuenta_perdida, usuario):
    with pytest.raises(ResolucionInvalida):
        resolver_excepcion(
            excepcion_pago_sin_movimiento, resolucion="perdida_ganancia", motivo_id=motivo.pk,
            comentario="", usuario=usuario, cuenta_contable_id=cuenta_perdida.pk,
        )


def test_vincular_con_movimiento_de_monto_menor_es_perdida(dia, lote_pos, motivo, cuenta_perdida, cuenta_ganancia, cajera, usuario):
    pago = _venta_con_pago(dia, lote_pos, 32680)
    excepcion = Excepcion.objects.create(
        dia=dia, tipo=Excepcion.Tipo.PAGO_SIN_MOVIMIENTO, monto_en_juego=32680, pago=pago,
        decision_propuesta="x",
    )
    lote_tbk = LoteCarga.objects.create(
        dia=dia, fuente=LoteCarga.Fuente.TRANSBANK, nombre_archivo="t.xlsx", huella_sha256="b",
        ruta_almacenamiento="/tmp/t", fecha_declarada=FECHA, cargado_por=usuario,
    )
    movimiento = MovimientoProveedor.objects.create(
        dia=dia, lote=lote_tbk, proveedor="transbank", tipo="Venta", estado="PENDIENTE",
        fecha_hora=datetime(2026, 9, 2, 16, 9), medio="credito", tipo_tarjeta="credito",
        monto_original=29000, monto_venta_valido_abono=29000, autorizacion="AUTH1", terminal="A1",
    )
    VersionMotor.objects.create(dia=dia, version_motor="test", parametros={}, ejecutado_por=usuario)

    # La cuenta de ganancia no calza con el signo real (POS > proveedor = pérdida).
    with pytest.raises(ResolucionInvalida):
        resolver_excepcion(
            excepcion, resolucion="perdida_ganancia", motivo_id=motivo.pk, comentario="",
            usuario=usuario, movimiento_id_alternativo=movimiento.pk,
            cuenta_contable_id=cuenta_ganancia.pk, cajera_id=cajera.pk,
        )

    resolver_excepcion(
        excepcion, resolucion="perdida_ganancia", motivo_id=motivo.pk, comentario="",
        usuario=usuario, movimiento_id_alternativo=movimiento.pk,
        cuenta_contable_id=cuenta_perdida.pk, cajera_id=cajera.pk,
    )
    excepcion.refresh_from_db()
    assert excepcion.monto_perdida_ganancia == 3680  # 32680 - 29000
    assert Coincidencia.objects.filter(dia=dia, movimientos=movimiento, pagos=pago).exists()


def test_diferencia_monto_usa_el_movimiento_ya_propuesto(dia, lote_pos, motivo, cuenta_ganancia, cajera, usuario):
    pago = _venta_con_pago(dia, lote_pos, 10000)
    lote_tbk = LoteCarga.objects.create(
        dia=dia, fuente=LoteCarga.Fuente.TRANSBANK, nombre_archivo="t.xlsx", huella_sha256="c",
        ruta_almacenamiento="/tmp/t2", fecha_declarada=FECHA, cargado_por=usuario,
    )
    movimiento = MovimientoProveedor.objects.create(
        dia=dia, lote=lote_tbk, proveedor="transbank", tipo="Venta", estado="PENDIENTE",
        fecha_hora=datetime(2026, 9, 2, 16, 9), medio="credito", tipo_tarjeta="credito",
        monto_original=15000, monto_venta_valido_abono=15000, autorizacion="AUTH2", terminal="A1",
    )
    excepcion = Excepcion.objects.create(
        dia=dia, tipo=Excepcion.Tipo.DIFERENCIA_MONTO, monto_en_juego=5000, pago=pago, movimiento=movimiento,
        decision_propuesta="x",
    )
    VersionMotor.objects.create(dia=dia, version_motor="test", parametros={}, ejecutado_por=usuario)

    resolver_excepcion(
        excepcion, resolucion="perdida_ganancia", motivo_id=motivo.pk, comentario="",
        usuario=usuario, cuenta_contable_id=cuenta_ganancia.pk, cajera_id=cajera.pk,
    )
    excepcion.refresh_from_db()
    assert excepcion.monto_perdida_ganancia == 5000  # 15000 - 10000, el proveedor pagó más


def test_perdida_bajo_umbral_no_requiere_visado(excepcion_pago_sin_movimiento, motivo, cuenta_perdida, cajera, usuario):
    resolver_excepcion(
        excepcion_pago_sin_movimiento, resolucion="perdida_ganancia", motivo_id=motivo.pk,
        comentario="", usuario=usuario, cuenta_contable_id=cuenta_perdida.pk, cajera_id=cajera.pk,
    )
    dia = excepcion_pago_sin_movimiento.dia
    assert not perdidas_pendientes_de_visado(dia).exists()
    puede, _ = puede_cerrar_con_perdidas_pendientes(dia, usuario)
    assert puede is True


def test_perdida_sobre_umbral_requiere_visado_de_jefe_contabilidad(dia, lote_pos, motivo, cuenta_perdida, cajera, usuario, jefe):
    pago = _venta_con_pago(dia, lote_pos, UMBRAL_VISADO_PERDIDA + 1)
    excepcion = Excepcion.objects.create(
        dia=dia, tipo=Excepcion.Tipo.PAGO_SIN_MOVIMIENTO, monto_en_juego=UMBRAL_VISADO_PERDIDA + 1, pago=pago,
        decision_propuesta="x",
    )
    resolver_excepcion(
        excepcion, resolucion="perdida_ganancia", motivo_id=motivo.pk, comentario="",
        usuario=usuario, cuenta_contable_id=cuenta_perdida.pk, cajera_id=cajera.pk,
    )

    assert perdidas_pendientes_de_visado(dia).count() == 1
    puede_operador, _ = puede_cerrar_con_perdidas_pendientes(dia, usuario)
    assert puede_operador is False
    puede_jefe, _ = puede_cerrar_con_perdidas_pendientes(dia, jefe)
    assert puede_jefe is True

    with pytest.raises(PermisoDenegado):
        visar_perdida(excepcion, usuario)  # la Encargada no visa: eso es del Jefe de Contabilidad

    visar_perdida(excepcion, jefe)
    excepcion.refresh_from_db()
    assert excepcion.visado_por == jefe
    assert not perdidas_pendientes_de_visado(dia).exists()

    with pytest.raises(VisadoInvalido):
        visar_perdida(excepcion, jefe)  # ya visada


def test_asignar_cajera_a_una_caja(dia, usuario, cajera):
    arqueo = ArqueoEfectivo.objects.create(dia=dia, caja="CAJA 4", contado=0, diferencia=0)
    asignar_cajera(arqueo, cajera_id=cajera.pk, usuario=usuario)
    arqueo.refresh_from_db()
    assert arqueo.cajera == cajera


def test_asignar_cajera_invalida(dia, usuario):
    arqueo = ArqueoEfectivo.objects.create(dia=dia, caja="CAJA 4", contado=0, diferencia=0)
    with pytest.raises(AsignacionCajeraInvalida):
        asignar_cajera(arqueo, cajera_id=None, usuario=usuario)
