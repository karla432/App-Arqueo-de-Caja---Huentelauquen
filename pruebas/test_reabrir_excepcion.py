"""Volver a abrir una excepción ya resuelta (Karla, 2026-10-21).

Caso real: cobros de Transbank aceptados como "otros ingresos" que podían ser
ventas con comanda. Solo el Jefe de Contabilidad o la Gerente, solo en días no
cerrados, con motivo; deshace la resolución completa y la deja en la bitácora."""
from datetime import date, datetime, time
from decimal import Decimal

import pytest
from django.urls import reverse

from nucleo.arqueo_servicio import preparar_arqueos_del_dia
from nucleo.conciliacion_servicio import ejecutar_conciliacion
from nucleo.documentos_servicio import otros_ingresos_pendientes
from nucleo.models import (
    Cajera, Coincidencia, CuentaContable, DiaArqueo, EventoAuditoria, Excepcion, LineaConciliacion, LoteCarga,
    MotivoResolucion, MovimientoProveedor, PagoDeclarado, Reclasificacion, Venta,
)
from nucleo.permisos import PermisoDenegado
from nucleo.reapertura_excepciones_servicio import ReaperturaInvalida, reabrir_excepcion
from nucleo.resolucion_servicio import resolver_excepcion
from pruebas.perfiles_de_prueba import GRUPO_ENCARGADO, GRUPO_GERENTE, GRUPO_JEFE, crear_usuario

FECHA = date(2026, 10, 2)


@pytest.fixture
def encargada(django_user_model):
    return crear_usuario(django_user_model, "nathaly", GRUPO_ENCARGADO)


@pytest.fixture
def jefe(django_user_model):
    return crear_usuario(django_user_model, "ricardo", GRUPO_JEFE)


@pytest.fixture
def gerente(django_user_model):
    return crear_usuario(django_user_model, "karla", GRUPO_GERENTE)


def _lotes(dia, usuario):
    pos = LoteCarga.objects.create(
        dia=dia, fuente=LoteCarga.Fuente.POS, nombre_archivo="p", huella_sha256="p",
        ruta_almacenamiento="/p", fecha_declarada=FECHA, cargado_por=usuario,
    )
    tbk = LoteCarga.objects.create(
        dia=dia, fuente=LoteCarga.Fuente.TRANSBANK, nombre_archivo="t", huella_sha256="t",
        ruta_almacenamiento="/t", fecha_declarada=FECHA, cargado_por=usuario,
    )
    return pos, tbk


def _venta(dia, lote, n, monto, medio, caja="CAJA 2", hora=time(10, 0)):
    venta = Venta.objects.create(
        dia=dia, lote=lote, n_interno=n, nro_comprobante=f"B039030{n:07d}", tipo_doc="FAC", nro_comanda=str(n),
        estado="COBRADA", caja=caja, turno="T", hora_cierre=hora, total_comanda=monto, cliente="CLIENTE OCASIONAL",
    )
    return PagoDeclarado.objects.create(venta=venta, medio=medio, monto=monto)


def _movimiento(dia, lote, hora, monto, autorizacion):
    return MovimientoProveedor.objects.create(
        dia=dia, lote=lote, proveedor="transbank", tipo="Venta", estado="PENDIENTE",
        fecha_hora=datetime(2026, 10, 2, *hora), medio="debito", tipo_tarjeta="debito",
        monto_original=monto, monto_venta_valido_abono=monto, autorizacion=autorizacion, terminal="A2885577",
    )


def _motivo(codigo):
    return MotivoResolucion.objects.get(codigo=codigo)


@pytest.fixture
def otros_ingresos(db, encargada):
    """Un cobro de $10.900 sin comanda, aceptado como otros ingresos."""
    dia = DiaArqueo.objects.create(fecha=FECHA, operado_por=encargada)
    pos, tbk = _lotes(dia, encargada)
    _venta(dia, pos, 670001, 5000, "efectivo")
    mov = _movimiento(dia, tbk, (10, 43), 10900, "100001")
    ejecutar_conciliacion(dia, encargada)
    exc = Excepcion.objects.get(dia=dia, tipo="movimiento_sin_pago", abierta=True)
    resolver_excepcion(
        exc, resolucion="aceptar", motivo_id=_motivo("forma_pago_sin_boleta").pk, comentario="Sin comanda",
        usuario=encargada,
    )
    dia.refresh_from_db()
    exc.refresh_from_db()
    return dia, exc, mov


# ---------------------------------------------------------------- otros ingresos

def test_resolver_la_ultima_excepcion_deja_el_dia_en_excepciones_resueltas(otros_ingresos):
    dia, _, _ = otros_ingresos
    assert dia.estado == DiaArqueo.Estado.EXCEPCIONES_RESUELTAS


def test_volver_a_abrir_otros_ingresos_la_deja_como_recien_conciliada(otros_ingresos, jefe):
    dia, exc, mov = otros_ingresos
    assert otros_ingresos_pendientes(dia).count() == 1

    reabrir_excepcion(exc, jefe, "Puede ser una venta pagada con dos tarjetas")

    exc.refresh_from_db()
    dia.refresh_from_db()
    assert exc.abierta and exc.resolucion == "" and exc.motivo is None and exc.resuelta_por is None
    assert exc.movimiento_id == mov.pk
    assert otros_ingresos_pendientes(dia).count() == 0  # ya no pide boleta
    assert dia.estado == DiaArqueo.Estado.CONCILIADO_CON_EXCEPCIONES

    evento = EventoAuditoria.objects.get(accion="reabrir_excepcion", entidad_id=str(exc.pk))
    assert evento.usuario == jefe
    assert evento.valor_anterior["resolucion"] == "aceptar"
    assert evento.valor_anterior["motivo"] == "forma_pago_sin_boleta"
    assert evento.valor_anterior["resuelta_por"] == "nathaly"
    assert evento.valor_posterior["motivo"] == "Puede ser una venta pagada con dos tarjetas"


def test_la_reabierta_se_puede_resolver_de_nuevo_y_reconciliar_no_la_duplica(otros_ingresos, jefe, encargada):
    dia, exc, _ = otros_ingresos
    reabrir_excepcion(exc, jefe, "Revisar")
    ejecutar_conciliacion(dia, encargada)
    abiertas = Excepcion.objects.filter(dia=dia, abierta=True, tipo="movimiento_sin_pago")
    assert abiertas.count() == 1


def test_la_gerente_tambien_puede_y_la_encargada_no(otros_ingresos, gerente, encargada):
    _, exc, _ = otros_ingresos
    with pytest.raises(PermisoDenegado):
        reabrir_excepcion(exc, encargada, "x")
    reabrir_excepcion(exc, gerente, "Revisión de la Gerente")
    exc.refresh_from_db()
    assert exc.abierta


# ---------------------------------------------------------------- bloqueos

def test_exige_motivo(otros_ingresos, jefe):
    _, exc, _ = otros_ingresos
    with pytest.raises(ReaperturaInvalida, match="motivo"):
        reabrir_excepcion(exc, jefe, "   ")


def test_no_se_reabre_en_un_dia_cerrado(otros_ingresos, jefe):
    dia, exc, _ = otros_ingresos
    DiaArqueo.objects.filter(pk=dia.pk).update(estado=DiaArqueo.Estado.CERRADO)
    with pytest.raises(ReaperturaInvalida, match="reabrir el día"):
        reabrir_excepcion(exc, jefe, "x")


def test_no_se_reabre_si_ya_tiene_boleta_asignada(otros_ingresos, jefe):
    _, exc, _ = otros_ingresos
    Excepcion.objects.filter(pk=exc.pk).update(factura_asociada="1642999")
    with pytest.raises(ReaperturaInvalida, match="1642999"):
        reabrir_excepcion(exc, jefe, "x")


def test_no_se_reabre_una_que_ya_esta_abierta(otros_ingresos, jefe):
    _, exc, _ = otros_ingresos
    reabrir_excepcion(exc, jefe, "x")
    with pytest.raises(ReaperturaInvalida, match="ya está abierta"):
        reabrir_excepcion(exc, jefe, "x")


# ---------------------------------------------------------------- pérdida/ganancia con su gemela

@pytest.fixture
def cobro_menor(db, encargada):
    dia = DiaArqueo.objects.create(fecha=FECHA, operado_por=encargada)
    pos, tbk = _lotes(dia, encargada)
    pago = _venta(dia, pos, 655746, 29307, "debito")
    mov = _movimiento(dia, tbk, (14, 57), 26307, "575281")
    ejecutar_conciliacion(dia, encargada)
    exc = Excepcion.objects.get(dia=dia, tipo="pago_sin_movimiento", abierta=True)
    gemela = Excepcion.objects.get(dia=dia, tipo="movimiento_sin_pago", abierta=True)
    resolver_excepcion(
        exc, resolucion="perdida_ganancia", motivo_id=_motivo("perdida_ganancia_caja").pk, comentario="Cobró de menos",
        usuario=encargada, movimiento_id_alternativo=mov.pk,
        cuenta_contable_id=CuentaContable.objects.get(codigo="6501004").pk,
        cajera_id=Cajera.objects.create(nombre="Yasna Rojo").pk,
    )
    return dia, exc, gemela, pago, mov


def test_la_gemela_arrastrada_no_se_reabre_sola(cobro_menor, jefe):
    _, exc, gemela, _, _ = cobro_menor
    gemela.refresh_from_db()
    assert not gemela.abierta
    with pytest.raises(ReaperturaInvalida, match=f"#{exc.pk}"):
        reabrir_excepcion(gemela, jefe, "x")


def test_reabrir_perdida_deshace_vinculo_perdida_y_reabre_la_gemela(cobro_menor, jefe, encargada):
    dia, exc, gemela, pago, mov = cobro_menor
    assert Coincidencia.objects.filter(dia=dia, origen="manual", pagos=pago).exists()

    reabrir_excepcion(exc, jefe, "No era pérdida: había un segundo cobro")

    exc.refresh_from_db()
    gemela.refresh_from_db()
    assert exc.abierta and gemela.abierta
    assert exc.movimiento_id is None  # el "pago sin movimiento" vuelve a no tener movimiento
    assert exc.monto_perdida_ganancia == 0 and exc.cuenta_contable is None and exc.cajera_responsable is None
    assert not Coincidencia.objects.filter(dia=dia, origen="manual", pagos=pago).exists()

    evento = EventoAuditoria.objects.get(accion="reabrir_excepcion", entidad_id=str(exc.pk))
    assert evento.valor_anterior["monto_perdida_ganancia"] == "3000.00" or Decimal(evento.valor_anterior["monto_perdida_ganancia"]) == 3000
    assert evento.valor_anterior["cajera_responsable"] == "Yasna Rojo"
    assert evento.valor_anterior["arrastradas"] == [gemela.pk]
    assert EventoAuditoria.objects.filter(accion="reabrir_excepcion:arrastrada", entidad_id=str(gemela.pk)).exists()

    # Una nueva conciliación vuelve a ver el par como dos excepciones, sin duplicarlas.
    ejecutar_conciliacion(dia, encargada)
    tipos = sorted(Excepcion.objects.filter(dia=dia, abierta=True).values_list("tipo", flat=True))
    assert tipos == ["movimiento_sin_pago", "pago_sin_movimiento"]


# ---------------------------------------------------------------- mezcla de medios: el arqueo se recalcula

def test_reabrir_una_mezcla_borra_lineas_y_reclasificaciones_y_recalcula_el_arqueo(db, encargada, jefe):
    dia = DiaArqueo.objects.create(fecha=FECHA, operado_por=encargada)
    pos, tbk = _lotes(dia, encargada)
    pago = _venta(dia, pos, 670100, 50000, "debito", caja="CAJA 1")
    mov = _movimiento(dia, tbk, (12, 0), 45000, "200001")
    ejecutar_conciliacion(dia, encargada)
    exc = Excepcion.objects.get(dia=dia, tipo="pago_sin_movimiento", abierta=True)
    resolver_excepcion(
        exc, resolucion="mezcla_medios", motivo_id=_motivo("mezcla_medios_pago").pk, comentario="", usuario=encargada,
        lineas=[
            {"medio": "debito", "monto": "45000", "movimiento_id": mov.pk},
            {"medio": "efectivo", "monto": "5000"},
        ],
    )
    arqueo = {a.caja: a for a in preparar_arqueos_del_dia(dia)}["CAJA 1"]
    assert arqueo.ajustes == Decimal(5000)
    assert Reclasificacion.objects.filter(excepcion=exc).count() == 1

    reabrir_excepcion(exc, jefe, "Era otro caso")

    assert not LineaConciliacion.objects.filter(excepcion=exc).exists()
    assert not Reclasificacion.objects.filter(dia=dia, pago=pago).exists()
    arqueo = {a.caja: a for a in preparar_arqueos_del_dia(dia)}["CAJA 1"]
    assert arqueo.ajustes == 0
    evento = EventoAuditoria.objects.get(accion="reabrir_excepcion", entidad_id=str(exc.pk))
    assert len(evento.valor_anterior["lineas"]) == 2
    assert evento.valor_anterior["reclasificaciones"][0]["medio_corregido"] == "efectivo"


# ---------------------------------------------------------------- pantalla

def test_la_bandeja_muestra_las_resueltas_y_el_boton_solo_a_quien_puede(client, otros_ingresos, jefe, encargada):
    dia, exc, _ = otros_ingresos
    url = reverse("bandeja_excepciones", args=[FECHA.isoformat()]) + f"?estado=resueltas&ver={exc.pk}"

    client.force_login(jefe)
    html = client.get(url).content.decode()
    assert "Resueltas · 1" in html and "Volver a abrir la excepción" in html

    client.force_login(encargada)
    html = client.get(url).content.decode()
    assert "Resueltas · 1" in html and "Volver a abrir la excepción" not in html


def test_la_ruta_vuelve_a_abrir_y_avisa_si_falta_el_motivo(client, otros_ingresos, jefe):
    _, exc, _ = otros_ingresos
    client.force_login(jefe)
    url = reverse("reabrir_excepcion", args=[exc.pk])

    client.post(url, {"motivo_reapertura": ""})
    exc.refresh_from_db()
    assert not exc.abierta

    resp = client.post(url, {"motivo_reapertura": "Revisar con la cajera"})
    exc.refresh_from_db()
    assert exc.abierta and resp.status_code == 302 and f"ver={exc.pk}" in resp["Location"]
