"""Pruebas de la pantalla de excepciones (Etapa 4): acceso, filtros y las
tres resoluciones (aceptar, rechazar, vincular otro), con su rastro en la
bitácora. Usa un escenario pequeño y armado a mano — no el día completo
(eso ya lo cubre test_motor_dia_referencia.py) — para poder revisar cada
caso con precisión.
"""
from datetime import date, datetime, time

import pytest
from pruebas.perfiles_de_prueba import GRUPO_ENCARGADO, crear_usuario
from django.urls import reverse

from nucleo.models import (
    Coincidencia, DiaArqueo, EventoAuditoria, Excepcion, LoteCarga,
    MotivoResolucion, MovimientoProveedor, PagoDeclarado, Venta, VersionMotor,
)

FECHA = date(2026, 9, 2)


@pytest.fixture
def usuario(django_user_model):
    return crear_usuario(django_user_model, "operador", GRUPO_ENCARGADO, password="clave-segura-123")


@pytest.fixture
def escenario(db, usuario):
    dia = DiaArqueo.objects.create(fecha=FECHA)
    lote = LoteCarga.objects.create(
        dia=dia, fuente=LoteCarga.Fuente.POS, nombre_archivo="x.xlsx",
        huella_sha256="abc", ruta_almacenamiento="/tmp/x.xlsx",
        fecha_declarada=FECHA, cargado_por=usuario,
    )
    corrida = VersionMotor.objects.create(
        dia=dia, version_motor="test", parametros={}, ejecutado_por=usuario,
    )

    venta = Venta.objects.create(
        dia=dia, lote=lote, n_interno=1, nro_comprobante="B1", tipo_doc="FAC",
        nro_comanda="1", estado="COBRADA", caja="CAJA 1", turno="TURNO 1",
        hora_cierre=time(14, 2), total_comanda=17600, cliente="CLIENTE OCASIONAL",
    )
    pago = PagoDeclarado.objects.create(venta=venta, medio="debito", monto=17600)

    movimiento = MovimientoProveedor.objects.create(
        dia=dia, lote=lote, proveedor="transbank", tipo="Venta", estado="PENDIENTE",
        fecha_hora=datetime(2026, 9, 2, 14, 3), medio="credito", tipo_tarjeta="mastercard",
        monto_original=17600, monto_venta_valido_abono=17600, autorizacion="B93179",
        terminal="A2885576",
    )

    excepcion = Excepcion.objects.create(
        dia=dia, tipo="medio_contradictorio", monto_en_juego=17600,
        pago=pago, movimiento=movimiento,
        decision_propuesta="Reclasificar a crédito, con confirmación.",
    )
    motivo = MotivoResolucion.objects.get(codigo="medio_mal_declarado")

    return {
        "dia": dia, "pago": pago, "movimiento": movimiento,
        "excepcion": excepcion, "motivo": motivo, "corrida": corrida,
    }


def test_requiere_login(client, escenario):
    resp = client.get(reverse("bandeja_excepciones", args=[FECHA.isoformat()]))
    assert resp.status_code == 302
    assert "/login/" in resp.url


def test_lista_muestra_la_excepcion_abierta(client, usuario, escenario):
    client.force_login(usuario)
    resp = client.get(reverse("bandeja_excepciones", args=[FECHA.isoformat()]))
    assert resp.status_code == 200
    assert resp.context["total_abiertas"] == 1
    assert resp.context["excepcion_actual"].pk == escenario["excepcion"].pk


def test_filtro_por_tipo_no_afecta_el_total(client, usuario, escenario):
    client.force_login(usuario)
    resp = client.get(
        reverse("bandeja_excepciones", args=[FECHA.isoformat()]), {"tipo": "medio_contradictorio"},
    )
    assert resp.context["total_abiertas"] == 1  # el total real, no el filtrado
    assert list(resp.context["excepciones"]) == [escenario["excepcion"]]

    resp_vacio = client.get(
        reverse("bandeja_excepciones", args=[FECHA.isoformat()]), {"tipo": "pago_sin_movimiento"},
    )
    assert list(resp_vacio.context["excepciones"]) == []
    assert resp_vacio.context["total_abiertas"] == 1  # sigue siendo el total real


def test_aceptar_reclasifica_medio_y_crea_coincidencia(client, usuario, escenario):
    client.force_login(usuario)
    excepcion = escenario["excepcion"]
    resp = client.post(
        reverse("resolver_excepcion", args=[excepcion.pk]),
        {"resolucion": "aceptar", "motivo_id": escenario["motivo"].pk, "comentario": "Confirmado con el cajero."},
    )
    assert resp.status_code == 302

    excepcion.refresh_from_db()
    assert excepcion.abierta is False
    assert excepcion.resolucion == "aceptar"
    assert excepcion.resuelta_por == usuario

    escenario["pago"].refresh_from_db()
    assert escenario["pago"].medio_aprobado == "credito"
    assert escenario["pago"].medio == "debito"  # el dato original no se toca (control C3)

    coincidencia = Coincidencia.objects.get()
    assert coincidencia.origen == "manual"
    assert list(coincidencia.pagos.values_list("pk", flat=True)) == [escenario["pago"].pk]
    assert list(coincidencia.movimientos.values_list("pk", flat=True)) == [escenario["movimiento"].pk]

    evento = EventoAuditoria.objects.get(entidad="Excepcion")
    assert evento.usuario == usuario
    assert evento.valor_anterior == {"abierta": True, "tipo": "medio_contradictorio"}
    assert evento.valor_posterior["resolucion"] == "aceptar"


def test_rechazar_no_crea_coincidencia_pero_queda_en_bitacora(client, usuario, escenario):
    client.force_login(usuario)
    excepcion = escenario["excepcion"]
    client.post(
        reverse("resolver_excepcion", args=[excepcion.pk]),
        {"resolucion": "rechazar", "motivo_id": escenario["motivo"].pk, "comentario": ""},
    )
    excepcion.refresh_from_db()
    assert excepcion.abierta is False
    assert excepcion.resolucion == "rechazar"
    assert Coincidencia.objects.count() == 0

    escenario["pago"].refresh_from_db()
    assert escenario["pago"].medio_aprobado == ""  # nada se reclasificó

    assert EventoAuditoria.objects.filter(entidad="Excepcion", accion="resolver_excepcion:rechazar").exists()


def test_sin_motivo_no_resuelve_nada(client, usuario, escenario):
    client.force_login(usuario)
    excepcion = escenario["excepcion"]
    client.post(reverse("resolver_excepcion", args=[excepcion.pk]), {"resolucion": "aceptar", "motivo_id": ""})
    excepcion.refresh_from_db()
    assert excepcion.abierta is True  # motivo obligatorio: nada se aplicó


def test_vincular_otro_movimiento(client, usuario, escenario):
    client.force_login(usuario)
    dia = escenario["dia"]
    otro_mov = MovimientoProveedor.objects.create(
        dia=dia, lote=escenario["pago"].venta.lote, proveedor="transbank", tipo="Venta",
        estado="PENDIENTE", fecha_hora=datetime(2026, 9, 2, 14, 5), medio="debito",
        tipo_tarjeta="debito", monto_original=17600, monto_venta_valido_abono=17600,
        terminal="A2885575",
    )
    # Esta excepción es "pago sin movimiento": sin candidato propio.
    excepcion_huerfana = Excepcion.objects.create(
        dia=dia, tipo="pago_sin_movimiento", monto_en_juego=17600, pago=escenario["pago"],
        decision_propuesta="Verificar cobro.",
    )
    client.post(
        reverse("resolver_excepcion", args=[excepcion_huerfana.pk]),
        {"resolucion": "vincular_otro", "motivo_id": escenario["motivo"].pk,
         "movimiento_alternativo": otro_mov.pk},
    )
    excepcion_huerfana.refresh_from_db()
    assert excepcion_huerfana.abierta is False
    coincidencia = Coincidencia.objects.get()
    assert list(coincidencia.movimientos.values_list("pk", flat=True)) == [otro_mov.pk]


def test_no_se_puede_resolver_dos_veces(client, usuario, escenario):
    client.force_login(usuario)
    excepcion = escenario["excepcion"]
    client.post(
        reverse("resolver_excepcion", args=[excepcion.pk]),
        {"resolucion": "rechazar", "motivo_id": escenario["motivo"].pk},
    )
    client.post(
        reverse("resolver_excepcion", args=[excepcion.pk]),
        {"resolucion": "aceptar", "motivo_id": escenario["motivo"].pk},
    )
    excepcion.refresh_from_db()
    assert excepcion.resolucion == "rechazar"  # la segunda resolución no pisó la primera
