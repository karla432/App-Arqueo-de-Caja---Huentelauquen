"""Caso real de Karla (2026-09-23): la cajera cobró en el POS de Transbank
$26.307 por una comanda de $29.307. Quedan dos excepciones — "pago sin
movimiento" ($29.307) y "movimiento sin pago" ($26.307) — que son el mismo
caso. Se resuelve desde la del pago, registrando los $3.000 como pérdida de
esa cajera; la otra debe cerrarse sola y no volver en una nueva conciliación."""
from datetime import date, datetime, time
from decimal import Decimal

import pytest
from pruebas.perfiles_de_prueba import GRUPO_GERENTE, crear_usuario

from nucleo.conciliacion_servicio import ejecutar_conciliacion
from nucleo.models import (
    Cajera, CuentaContable, DiaArqueo, Excepcion, LoteCarga, MotivoResolucion, MovimientoProveedor, PagoDeclarado,
    Venta,
)
from nucleo.resolucion_servicio import resolver_excepcion

FECHA = date(2026, 9, 4)


@pytest.fixture
def usuario(django_user_model):
    return crear_usuario(django_user_model, "operador", GRUPO_GERENTE)


@pytest.fixture
def caso(db, usuario):
    dia = DiaArqueo.objects.create(fecha=FECHA, operado_por=usuario)
    lote = LoteCarga.objects.create(
        dia=dia, fuente=LoteCarga.Fuente.POS, nombre_archivo="x", huella_sha256="a",
        ruta_almacenamiento="/x", fecha_declarada=FECHA, cargado_por=usuario,
    )
    lote_tbk = LoteCarga.objects.create(
        dia=dia, fuente=LoteCarga.Fuente.TRANSBANK, nombre_archivo="t", huella_sha256="t",
        ruta_almacenamiento="/t", fecha_declarada=FECHA, cargado_por=usuario,
    )
    venta = Venta.objects.create(
        dia=dia, lote=lote, n_interno=655746, nro_comprobante="B0390301667999", tipo_doc="FAC", nro_comanda="1",
        estado="COBRADA", caja="CAJA 2", turno="T", hora_cierre=time(10, 0), total_comanda=29307,
        cliente="CLIENTE OCASIONAL",
    )
    pago = PagoDeclarado.objects.create(venta=venta, medio="debito", monto=29307)
    movimiento = MovimientoProveedor.objects.create(
        dia=dia, lote=lote_tbk, proveedor="transbank", tipo="Venta", estado="PENDIENTE",
        fecha_hora=datetime(2026, 9, 4, 14, 57), medio="debito", tipo_tarjeta="debito",
        monto_original=26307, monto_venta_valido_abono=26307, autorizacion="575281", terminal="A2885577",
    )
    ejecutar_conciliacion(dia, usuario)
    return dia, pago, movimiento


def _resolver_como_perdida(dia, pago, movimiento, usuario):
    exc = Excepcion.objects.get(dia=dia, tipo="pago_sin_movimiento", abierta=True)
    cajera = Cajera.objects.create(nombre="Yasna Rojo")
    motivo = MotivoResolucion.objects.get(codigo="perdida_ganancia_caja")
    cuenta = CuentaContable.objects.get(codigo="6501004")
    resolver_excepcion(
        exc, resolucion="perdida_ganancia", motivo_id=motivo.pk, comentario="Cobró de menos en el POS",
        usuario=usuario, movimiento_id_alternativo=movimiento.pk, cuenta_contable_id=cuenta.pk, cajera_id=cajera.pk,
    )
    return exc


def test_el_caso_parte_como_dos_excepciones(caso):
    dia, _, _ = caso
    tipos = sorted(Excepcion.objects.filter(dia=dia, abierta=True).values_list("tipo", flat=True))
    assert tipos == ["movimiento_sin_pago", "pago_sin_movimiento"]


def test_resolver_la_del_pago_como_perdida_cierra_tambien_la_del_movimiento(caso, usuario):
    dia, pago, movimiento = caso
    exc = _resolver_como_perdida(dia, pago, movimiento, usuario)
    exc.refresh_from_db()
    assert exc.monto_perdida_ganancia == Decimal(3000) and exc.cajera_responsable.nombre == "Yasna Rojo"
    assert not Excepcion.objects.filter(dia=dia, abierta=True).exists()


def test_una_nueva_conciliacion_no_vuelve_a_soltar_el_movimiento(caso, usuario):
    dia, pago, movimiento = caso
    _resolver_como_perdida(dia, pago, movimiento, usuario)
    ejecutar_conciliacion(dia, usuario)
    assert not Excepcion.objects.filter(dia=dia, abierta=True).exists()


def test_el_asiento_usa_el_proveedor_del_movimiento_vinculado(caso, usuario):
    from nucleo.arqueo_caja_servicio import generar_lineas_arqueo_caja
    dia, pago, movimiento = caso
    _resolver_como_perdida(dia, pago, movimiento, usuario)
    filas = generar_lineas_arqueo_caja(dia)
    assert any(f["Apuntes contables/Cuenta"].startswith("1101005") and f["Apuntes contables/Crédito"] == 3000 for f in filas)
    assert any(f["Apuntes contables/Cuenta"].startswith("6501004") and f["Apuntes contables/Débito"] == 3000 for f in filas)
