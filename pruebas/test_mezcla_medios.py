"""Mezcla de medios (Karla, 2026-09-18): Resto declaró un solo medio,
pero la conciliación comprobó que en realidad se recibió repartido en
más de uno (ej. Transbank $45.000 + Efectivo $5.000 de una venta de
$50.000). Es un traspaso entre cuentas — no afecta el resultado — y el
pago declarado nunca se modifica.
"""
from datetime import date, datetime, time
from decimal import Decimal

import pytest
from pruebas.perfiles_de_prueba import GRUPO_ENCARGADO, crear_usuario

from nucleo.conciliacion_servicio import ejecutar_conciliacion
from nucleo.models import (
    DiaArqueo, Excepcion, LineaConciliacion, LoteCarga, MotivoResolucion,
    MovimientoProveedor, PagoDeclarado, Venta, VersionMotor,
)
from nucleo.resolucion_servicio import ResolucionInvalida, resolver_excepcion

FECHA = date(2026, 9, 2)


@pytest.fixture
def usuario(django_user_model):
    return crear_usuario(django_user_model, "operador", GRUPO_ENCARGADO, password="x")


@pytest.fixture
def motivo():
    return MotivoResolucion.objects.get(codigo="mezcla_medios_pago")


@pytest.fixture
def dia(db):
    return DiaArqueo.objects.create(fecha=FECHA)


@pytest.fixture
def lote_pos(dia, usuario):
    return LoteCarga.objects.create(
        dia=dia, fuente=LoteCarga.Fuente.POS, nombre_archivo="x.xlsx", huella_sha256="a",
        ruta_almacenamiento="/tmp/x", fecha_declarada=FECHA, cargado_por=usuario,
    )


@pytest.fixture
def excepcion_pago_sin_movimiento(dia, lote_pos):
    venta = Venta.objects.create(
        dia=dia, lote=lote_pos, n_interno=1, nro_comprobante="B0000001", tipo_doc="FAC",
        nro_comanda="1", estado="COBRADA", caja="CAJA 1", turno="TURNO CAJA 1",
        hora_cierre=time(12, 0), total_comanda=50000, cliente="CLIENTE OCASIONAL",
    )
    pago = PagoDeclarado.objects.create(venta=venta, medio="debito", monto=50000)
    return Excepcion.objects.create(
        dia=dia, tipo=Excepcion.Tipo.PAGO_SIN_MOVIMIENTO, monto_en_juego=50000, pago=pago,
        decision_propuesta="x",
    )


def test_mezcla_de_medios_camino_feliz(dia, lote_pos, excepcion_pago_sin_movimiento, motivo, usuario):
    lote_tbk = LoteCarga.objects.create(
        dia=dia, fuente=LoteCarga.Fuente.TRANSBANK, nombre_archivo="t.xlsx", huella_sha256="b",
        ruta_almacenamiento="/tmp/t", fecha_declarada=FECHA, cargado_por=usuario,
    )
    movimiento = MovimientoProveedor.objects.create(
        dia=dia, lote=lote_tbk, proveedor="transbank", tipo="Venta", estado="PENDIENTE",
        fecha_hora=datetime(2026, 9, 2, 12, 0), medio="debito", tipo_tarjeta="debito",
        monto_original=45000, monto_venta_valido_abono=45000, autorizacion="AUTH1", terminal="A1",
    )
    VersionMotor.objects.create(dia=dia, version_motor="test", parametros={}, ejecutado_por=usuario)

    resolver_excepcion(
        excepcion_pago_sin_movimiento, resolucion="mezcla_medios", motivo_id=motivo.pk, comentario="",
        usuario=usuario,
        lineas=[
            {"medio": "debito", "monto": "45000", "movimiento_id": movimiento.pk},
            {"medio": "efectivo", "monto": "5000"},
        ],
    )
    excepcion_pago_sin_movimiento.refresh_from_db()
    assert excepcion_pago_sin_movimiento.abierta is False
    assert excepcion_pago_sin_movimiento.resolucion == "mezcla_medios"
    assert excepcion_pago_sin_movimiento.cuenta_contable_id is None  # no es pérdida/ganancia
    assert excepcion_pago_sin_movimiento.cajera_responsable_id is None

    lineas = list(excepcion_pago_sin_movimiento.lineas_conciliacion.order_by("monto_conciliado"))
    assert len(lineas) == 2
    assert lineas[0].medio_conciliado == "efectivo"
    assert lineas[0].monto_conciliado == 5000
    assert lineas[0].movimiento_id is None
    assert lineas[1].medio_conciliado == "debito"
    assert lineas[1].monto_conciliado == 45000
    assert lineas[1].movimiento_id == movimiento.pk

    # El pago declarado original nunca se toca.
    excepcion_pago_sin_movimiento.pago.refresh_from_db()
    assert excepcion_pago_sin_movimiento.pago.medio == "debito"
    assert excepcion_pago_sin_movimiento.pago.monto == 50000
    assert excepcion_pago_sin_movimiento.pago.medio_aprobado == ""


def test_lineas_deben_sumar_exacto(excepcion_pago_sin_movimiento, motivo, usuario):
    with pytest.raises(ResolucionInvalida):
        resolver_excepcion(
            excepcion_pago_sin_movimiento, resolucion="mezcla_medios", motivo_id=motivo.pk, comentario="",
            usuario=usuario,
            lineas=[
                {"medio": "debito", "monto": "45000"},
                {"medio": "efectivo", "monto": "4000"},  # falta $1.000
            ],
        )
    assert not LineaConciliacion.objects.exists()


def test_requiere_al_menos_dos_lineas(excepcion_pago_sin_movimiento, motivo, usuario):
    with pytest.raises(ResolucionInvalida):
        resolver_excepcion(
            excepcion_pago_sin_movimiento, resolucion="mezcla_medios", motivo_id=motivo.pk, comentario="",
            usuario=usuario,
            lineas=[{"medio": "debito", "monto": "50000"}],
        )


def test_reconciliar_de_nuevo_no_toca_la_mezcla_ya_resuelta(dia, excepcion_pago_sin_movimiento, motivo, usuario):
    resolver_excepcion(
        excepcion_pago_sin_movimiento, resolucion="mezcla_medios", motivo_id=motivo.pk, comentario="",
        usuario=usuario,
        lineas=[{"medio": "debito", "monto": "45000"}, {"medio": "efectivo", "monto": "5000"}],
    )
    ejecutar_conciliacion(dia, usuario)
    ejecutar_conciliacion(dia, usuario)

    assert Excepcion.objects.filter(dia=dia, resolucion="mezcla_medios").count() == 1
    assert LineaConciliacion.objects.count() == 2
