"""Regresión del hallazgo crítico de la auditoría QA (2026-09-18, contador
senior/tesorero y desarrollador, de forma independiente): reconciliar dos
veces después de resolver una excepción a mano no debía tocar esa
resolución, pero `ejecutar_conciliacion` borraba TODAS las Coincidencia
(incluidas las de origen manual) y volvía a meter a la bolsa cualquier
pago/movimiento ya resuelto — duplicando pérdidas de caja y "otros
ingresos" ya facturados, o resucitando una excepción ya cerrada.
"""
from datetime import date, datetime, time

import pytest
from pruebas.perfiles_de_prueba import GRUPO_ENCARGADO, crear_usuario

from nucleo.conciliacion_servicio import ejecutar_conciliacion
from nucleo.models import (
    Cajera, Coincidencia, CuentaContable, DiaArqueo, Excepcion, LoteCarga,
    MotivoResolucion, MovimientoProveedor, PagoDeclarado, Venta,
)
from nucleo.resolucion_servicio import resolver_excepcion

FECHA = date(2026, 9, 2)


@pytest.fixture
def usuario(django_user_model):
    return crear_usuario(django_user_model, "operador", GRUPO_ENCARGADO, password="x")


@pytest.fixture
def dia(db):
    return DiaArqueo.objects.create(fecha=FECHA)


@pytest.fixture
def lote_pos(dia, usuario):
    return LoteCarga.objects.create(
        dia=dia, fuente=LoteCarga.Fuente.POS, nombre_archivo="x.xlsx", huella_sha256="a",
        ruta_almacenamiento="/tmp/x", fecha_declarada=FECHA, cargado_por=usuario,
    )


def _venta_con_pago(dia, lote, medio, monto, n_interno):
    venta = Venta.objects.create(
        dia=dia, lote=lote, n_interno=n_interno, nro_comprobante=f"B{n_interno}", tipo_doc="FAC",
        nro_comanda=str(n_interno), estado="COBRADA", caja="CAJA 1", turno="TURNO CAJA 1",
        hora_cierre=time(12, 0), total_comanda=monto, cliente="CLIENTE OCASIONAL",
    )
    return PagoDeclarado.objects.create(venta=venta, medio=medio, monto=monto)


def test_perdida_de_caja_ya_resuelta_no_se_recrea_al_reconciliar_de_nuevo(dia, lote_pos, usuario):
    pago = _venta_con_pago(dia, lote_pos, "credito", 32680, n_interno=1)
    ejecutar_conciliacion(dia, usuario)

    excepcion = Excepcion.objects.get(dia=dia, pago=pago)
    assert excepcion.tipo == "pago_sin_movimiento"

    cuenta = CuentaContable.objects.get(codigo="6501004")
    cajera = Cajera.objects.create(nombre="Juanita Pérez")
    motivo = MotivoResolucion.objects.get(codigo="perdida_ganancia_caja")
    resolver_excepcion(
        excepcion, resolucion="perdida_ganancia", motivo_id=motivo.pk, comentario="",
        usuario=usuario, cuenta_contable_id=cuenta.pk, cajera_id=cajera.pk,
    )

    # Reconciliar de nuevo no debe resucitar el caso ni duplicar la pérdida.
    ejecutar_conciliacion(dia, usuario)
    ejecutar_conciliacion(dia, usuario)

    assert Excepcion.objects.filter(dia=dia, pago=pago).count() == 1
    excepcion.refresh_from_db()
    assert excepcion.abierta is False
    assert excepcion.resolucion == "perdida_ganancia"
    assert Excepcion.objects.filter(dia=dia, resolucion="perdida_ganancia").count() == 1


def test_vinculo_manual_de_otro_monto_sobrevive_a_reconciliar_de_nuevo(dia, lote_pos, usuario):
    pago = _venta_con_pago(dia, lote_pos, "debito", 45000, n_interno=2)
    lote_tbk = LoteCarga.objects.create(
        dia=dia, fuente=LoteCarga.Fuente.TRANSBANK, nombre_archivo="t.xlsx", huella_sha256="b",
        ruta_almacenamiento="/tmp/t", fecha_declarada=FECHA, cargado_por=usuario,
    )
    movimiento = MovimientoProveedor.objects.create(
        dia=dia, lote=lote_tbk, proveedor="transbank", tipo="Venta", estado="PENDIENTE",
        fecha_hora=datetime(2026, 9, 2, 12, 0), medio="debito", tipo_tarjeta="debito",
        monto_original=44200, monto_venta_valido_abono=44200, autorizacion="AUTH1", terminal="A1",
    )

    ejecutar_conciliacion(dia, usuario)
    excepcion = Excepcion.objects.get(dia=dia, pago=pago)

    cuenta = CuentaContable.objects.get(codigo="6501004")
    cajera = Cajera.objects.create(nombre="Juanita Pérez")
    motivo = MotivoResolucion.objects.get(codigo="perdida_ganancia_caja")
    resolver_excepcion(
        excepcion, resolucion="perdida_ganancia", motivo_id=motivo.pk, comentario="",
        usuario=usuario, movimiento_id_alternativo=movimiento.pk,
        cuenta_contable_id=cuenta.pk, cajera_id=cajera.pk,
    )
    coincidencia_manual = Coincidencia.objects.get(dia=dia, pagos=pago)
    assert coincidencia_manual.origen == Coincidencia.Origen.MANUAL

    ejecutar_conciliacion(dia, usuario)

    # La Coincidencia manual sigue existiendo tal cual, no se borró ni se
    # reemplazó por una automática.
    assert Coincidencia.objects.filter(pk=coincidencia_manual.pk).exists()
    assert not Excepcion.objects.filter(dia=dia, pago=pago, abierta=True).exists()
    assert Excepcion.objects.filter(dia=dia, pago=pago).count() == 1


def test_reclasificacion_de_medio_se_respeta_al_reconciliar_de_nuevo(dia, lote_pos, usuario):
    # medio_contradictorio: el POS declaró débito, el proveedor cobró crédito.
    pago = _venta_con_pago(dia, lote_pos, "debito", 23447, n_interno=3)
    lote_tbk = LoteCarga.objects.create(
        dia=dia, fuente=LoteCarga.Fuente.TRANSBANK, nombre_archivo="t2.xlsx", huella_sha256="c",
        ruta_almacenamiento="/tmp/t2", fecha_declarada=FECHA, cargado_por=usuario,
    )
    MovimientoProveedor.objects.create(
        dia=dia, lote=lote_tbk, proveedor="transbank", tipo="Venta", estado="PENDIENTE",
        fecha_hora=datetime(2026, 9, 2, 12, 0), medio="credito", tipo_tarjeta="credito",
        monto_original=23447, monto_venta_valido_abono=23447, autorizacion="AUTH2", terminal="A1",
    )

    ejecutar_conciliacion(dia, usuario)
    excepcion = Excepcion.objects.get(dia=dia, pago=pago)
    assert excepcion.tipo == "medio_contradictorio"

    motivo = MotivoResolucion.objects.get(codigo="medio_mal_declarado")
    resolver_excepcion(excepcion, resolucion="aceptar", motivo_id=motivo.pk, comentario="", usuario=usuario)
    pago.refresh_from_db()
    assert pago.medio_aprobado == "credito"

    # Reconciliar de nuevo: no debe volver a marcar "medio_contradictorio"
    # para el mismo pago ya reclasificado y conciliado.
    ejecutar_conciliacion(dia, usuario)
    assert not Excepcion.objects.filter(dia=dia, pago=pago, abierta=True).exists()
