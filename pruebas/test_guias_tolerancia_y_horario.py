"""Calce de guías con tarjeta por tolerancia y horario (Karla, 2026-09-24).

Caso real del 07-09: guía 21204 de $459.554 pagada con crédito; el cobro de
Transbank fue de $459.555 (1 peso de diferencia). Con tolerancia de $100 se
calza sola, la diferencia queda registrada como pérdida/ganancia, y el
horario (el N_INTERNO de la guía queda entre el de dos comandas con hora de
cierre) confirma que ese cobro es el de la guía."""
from datetime import date, datetime, time
from decimal import Decimal

import pytest
from pruebas.perfiles_de_prueba import GRUPO_GERENTE, crear_usuario

from nucleo.arqueo_caja_servicio import generar_lineas_arqueo_caja
from nucleo.conciliacion_servicio import ejecutar_conciliacion
from nucleo.configuracion_servicio import actualizar_umbral
from nucleo.models import (
    DiaArqueo, Excepcion, LoteCarga, MovimientoProveedor, PagoDeclarado, RegularizacionDocumento, Venta,
)
from nucleo.regularizacion_servicio import ventana_horaria_de_guia

FECHA = date(2026, 9, 7)


@pytest.fixture
def usuario(django_user_model):
    return crear_usuario(django_user_model, "operador", GRUPO_GERENTE)


@pytest.fixture
def dia(db, usuario):
    dia = DiaArqueo.objects.create(fecha=FECHA, operado_por=usuario)
    lote = LoteCarga.objects.create(
        dia=dia, fuente=LoteCarga.Fuente.POS, nombre_archivo="x", huella_sha256="a",
        ruta_almacenamiento="/x", fecha_declarada=FECHA, cargado_por=usuario,
    )
    for n, hora in ((657101, time(16, 34)), (657103, time(16, 39)), (657095, time(12, 0)), (657200, time(19, 0))):
        Venta.objects.create(
            dia=dia, lote=lote, n_interno=n, nro_comprobante=f"B{n:07d}", tipo_doc="FAC", nro_comanda=str(n),
            estado="COBRADA", caja="CAJA 4", turno="T", hora_cierre=hora, total_comanda=1000, cliente="X",
        )
        PagoDeclarado.objects.create(venta=Venta.objects.get(dia=dia, n_interno=n), medio="efectivo", monto=1000)
    return dia


def _guia(dia, folio, n_interno, monto, medio="credito"):
    return RegularizacionDocumento.objects.create(
        dia=dia, tipo="DES", n_interno=n_interno, folio=folio, caja="CAJA 4", monto=monto, forma_pago=medio,
    )


def _cobro(dia, monto, hora, minuto, medio="credito", auto="A"):
    lote, _ = LoteCarga.objects.get_or_create(
        dia=dia, fuente=LoteCarga.Fuente.TRANSBANK,
        defaults=dict(nombre_archivo="t", huella_sha256="t", ruta_almacenamiento="/t", fecha_declarada=FECHA,
                      cargado_por=dia.operado_por),
    )
    return MovimientoProveedor.objects.create(
        dia=dia, lote=lote, proveedor="transbank", tipo="Venta", estado="PENDIENTE",
        fecha_hora=datetime(2026, 9, 7, hora, minuto), medio=medio, tipo_tarjeta=medio,
        monto_original=monto, monto_venta_valido_abono=monto, autorizacion=auto, terminal="T",
    )


def test_el_horario_de_la_guia_sale_de_las_comandas_vecinas(dia):
    guia = _guia(dia, "21204", 657102, 459554)
    desde, hasta = ventana_horaria_de_guia(guia)
    assert (desde.time(), hasta.time()) == (time(16, 24), time(16, 49))  # 16:34–16:39 ± 10 min


def test_un_peso_de_diferencia_se_calza_solo_y_queda_registrado(dia, usuario):
    guia = _guia(dia, "21204", 657102, 459554)
    cobro = _cobro(dia, 459555, 16, 37)
    ejecutar_conciliacion(dia, usuario)

    guia.refresh_from_db()
    assert guia.movimiento_id == cobro.pk and guia.diferencia_con_cobro == Decimal(-1)
    assert not Excepcion.objects.filter(dia=dia, abierta=True).exists()
    ajuste = Excepcion.objects.get(dia=dia, resolucion="ajuste_sencillo")
    assert ajuste.monto_perdida_ganancia == 1 and ajuste.cuenta_contable.codigo == "6501003"  # ganancia: cobraron $1 más


def test_una_diferencia_de_perdida_va_a_la_cuenta_de_perdida_en_el_asiento(dia, usuario):
    _guia(dia, "21204", 657102, 459554)
    _cobro(dia, 459500, 16, 37)  # el proveedor cobró $54 menos
    ejecutar_conciliacion(dia, usuario)
    filas = generar_lineas_arqueo_caja(dia)
    assert any(f["Apuntes contables/Cuenta"].startswith("6501004") and f["Apuntes contables/Débito"] == 54 for f in filas)
    assert any(f["Apuntes contables/Cuenta"].startswith("1101005") and f["Apuntes contables/Crédito"] == 54 for f in filas)


def test_sobre_la_tolerancia_no_se_calza_queda_como_excepcion(dia, usuario):
    _guia(dia, "21204", 657102, 459554)
    _cobro(dia, 459754, 16, 37)  # $200 de diferencia, tolerancia $100
    ejecutar_conciliacion(dia, usuario)
    assert Excepcion.objects.filter(dia=dia, tipo="movimiento_sin_pago", abierta=True).count() == 1
    assert RegularizacionDocumento.objects.get(dia=dia, folio="21204").movimiento_id is None


def test_la_tolerancia_es_configurable(dia, usuario):
    actualizar_umbral("tolerancia_guias", "300", usuario)
    _guia(dia, "21204", 657102, 459554)
    _cobro(dia, 459754, 16, 37)
    ejecutar_conciliacion(dia, usuario)
    assert RegularizacionDocumento.objects.get(dia=dia, folio="21204").movimiento_id is not None


def test_con_diferencia_un_cobro_fuera_de_horario_no_se_acepta(dia, usuario):
    """Sin el horario, un cobro parecido de otra hora podría ser de una venta
    cualquiera: una diferencia de $1 exige además que el horario calce."""
    _guia(dia, "21204", 657102, 459554)
    _cobro(dia, 459555, 11, 5)  # a otra hora
    ejecutar_conciliacion(dia, usuario)
    assert Excepcion.objects.filter(dia=dia, tipo="movimiento_sin_pago", abierta=True).count() == 1


def test_el_horario_decide_entre_dos_cobros_del_mismo_monto(dia, usuario):
    guia = _guia(dia, "21204", 657102, 459554)
    _cobro(dia, 459554, 11, 5, auto="A1")            # mismo monto, pero de otra hora
    bueno = _cobro(dia, 459554, 16, 37, auto="A2")   # el que cae en el horario de la guía
    ejecutar_conciliacion(dia, usuario)
    guia.refresh_from_db()
    assert guia.movimiento_id == bueno.pk
    assert Excepcion.objects.filter(dia=dia, abierta=True, tipo="movimiento_sin_pago").count() == 1  # el otro sigue abierto


def test_reconciliar_de_nuevo_no_duplica_el_ajuste_de_la_guia(dia, usuario):
    _guia(dia, "21204", 657102, 459554)
    _cobro(dia, 459555, 16, 37)
    ejecutar_conciliacion(dia, usuario)
    ejecutar_conciliacion(dia, usuario)
    assert Excepcion.objects.filter(dia=dia, resolucion="ajuste_sencillo").count() == 1
