"""Arqueo de efectivo (Etapa 5): cálculo del teórico por caja, registro del
conteo por denominación (D8), y la autorización de diferencias sobre el
umbral de $5.000 (D3), reservada a un aprobador (D10).
"""
from datetime import date, time
from decimal import Decimal

import pytest
from django.urls import reverse

from nucleo.arqueo_servicio import (
    AutorizacionInvalida, autorizar_diferencia, calcular_teorico_por_caja,
    preparar_arqueos_del_dia, registrar_conteo,
)
from nucleo.models import ArqueoEfectivo, DiaArqueo, LoteCarga, MotivoResolucion, PagoDeclarado, Venta

FECHA = date(2026, 9, 2)


@pytest.fixture
def usuario(django_user_model):
    return django_user_model.objects.create_user(username="operador", password="clave-segura-123")


@pytest.fixture
def aprobador(django_user_model):
    return django_user_model.objects.create_user(
        username="karla", password="clave-segura-123", is_superuser=True, is_staff=True,
    )


def _crear_venta(dia, lote, n_interno, caja, total, medio="efectivo"):
    venta = Venta.objects.create(
        dia=dia, lote=lote, n_interno=n_interno, nro_comprobante=f"B{n_interno}", tipo_doc="FAC",
        nro_comanda=str(n_interno), estado="COBRADA", caja=caja, turno="TURNO 1",
        hora_cierre=time(14, 0), total_comanda=total, cliente="CLIENTE OCASIONAL",
    )
    PagoDeclarado.objects.create(venta=venta, medio=medio, monto=total)
    return venta


@pytest.fixture
def dia_con_ventas(db, usuario):
    dia = DiaArqueo.objects.create(fecha=FECHA)
    lote = LoteCarga.objects.create(
        dia=dia, fuente=LoteCarga.Fuente.POS, nombre_archivo="x.xlsx", huella_sha256="a",
        ruta_almacenamiento="/tmp/x", fecha_declarada=FECHA, cargado_por=usuario,
    )
    _crear_venta(dia, lote, 1, "CAJA 1", 30000, medio="efectivo")
    _crear_venta(dia, lote, 2, "CAJA 1", 20000, medio="efectivo")
    _crear_venta(dia, lote, 3, "CAJA 2", 15000, medio="efectivo")
    _crear_venta(dia, lote, 4, "CAJA 2", 5000, medio="debito")  # no debe contar como efectivo
    return dia


def test_calcula_teorico_por_caja(dia_con_ventas):
    totales = calcular_teorico_por_caja(dia_con_ventas)
    assert totales == {"CAJA 1": Decimal(50000), "CAJA 2": Decimal(15000)}


def test_preparar_arqueos_crea_uno_por_caja(dia_con_ventas):
    arqueos = preparar_arqueos_del_dia(dia_con_ventas)
    assert {a.caja for a in arqueos} == {"CAJA 1", "CAJA 2"}
    caja1 = next(a for a in arqueos if a.caja == "CAJA 1")
    assert caja1.teorico == 50000
    assert caja1.esperado == 50000
    assert caja1.estado == "pendiente"


def test_registrar_conteo_calcula_diferencia_por_denominacion(dia_con_ventas, usuario):
    arqueo = preparar_arqueos_del_dia(dia_con_ventas)[0]  # CAJA 1, esperado 50.000
    registrar_conteo(
        arqueo, fondo_inicial=Decimal(10000), gastos_autorizados=Decimal(0),
        denominaciones={20000: 3, 1000: 2}, usuario=usuario,  # 60.000 + 2.000 = 62.000
    )
    arqueo.refresh_from_db()
    assert arqueo.contado == 62000
    # esperado(50.000) + fondo(10.000) - gastos(0) = 60.000 ; contado 62.000 -> +2.000
    assert arqueo.diferencia == 2000
    assert arqueo.estado == "dentro_de_umbral"


def test_diferencia_sobre_el_umbral_requiere_autorizacion(dia_con_ventas, usuario):
    arqueo = preparar_arqueos_del_dia(dia_con_ventas)[0]
    registrar_conteo(
        arqueo, fondo_inicial=Decimal(0), gastos_autorizados=Decimal(0),
        denominaciones={20000: 3}, usuario=usuario,  # 60.000 vs esperado 50.000 -> +10.000
    )
    arqueo.refresh_from_db()
    assert arqueo.diferencia == 10000
    assert arqueo.estado == "requiere_autorizacion"


def test_autorizar_diferencia_exige_motivo_de_la_lista_cerrada(dia_con_ventas, usuario, aprobador):
    arqueo = preparar_arqueos_del_dia(dia_con_ventas)[0]
    registrar_conteo(arqueo, fondo_inicial=Decimal(0), gastos_autorizados=Decimal(0),
                      denominaciones={20000: 3}, usuario=usuario)
    with pytest.raises(AutorizacionInvalida):
        autorizar_diferencia(arqueo, motivo_id=None, comentario="", usuario=aprobador)

    motivo = MotivoResolucion.objects.get(codigo="faltante_en_investigacion")
    arqueo.refresh_from_db()
    autorizar_diferencia(arqueo, motivo_id=motivo.pk, comentario="Revisado con el cajero.", usuario=aprobador)
    arqueo.refresh_from_db()
    assert arqueo.autorizado_por == aprobador
    assert arqueo.estado == "autorizada"


def test_no_se_puede_autorizar_dentro_del_umbral(dia_con_ventas, usuario, aprobador):
    arqueo = preparar_arqueos_del_dia(dia_con_ventas)[0]
    registrar_conteo(arqueo, fondo_inicial=Decimal(0), gastos_autorizados=Decimal(0),
                      denominaciones={20000: 2, 10000: 1}, usuario=usuario)  # 50.000, diferencia 0
    motivo = MotivoResolucion.objects.get(codigo="faltante_en_investigacion")
    with pytest.raises(AutorizacionInvalida):
        autorizar_diferencia(arqueo, motivo_id=motivo.pk, comentario="", usuario=aprobador)


def test_nueva_cuenta_invalida_autorizacion_anterior(dia_con_ventas, usuario, aprobador):
    arqueo = preparar_arqueos_del_dia(dia_con_ventas)[0]
    registrar_conteo(arqueo, fondo_inicial=Decimal(0), gastos_autorizados=Decimal(0),
                      denominaciones={20000: 3}, usuario=usuario)
    motivo = MotivoResolucion.objects.get(codigo="faltante_en_investigacion")
    autorizar_diferencia(arqueo, motivo_id=motivo.pk, comentario="", usuario=aprobador)

    registrar_conteo(arqueo, fondo_inicial=Decimal(0), gastos_autorizados=Decimal(0),
                      denominaciones={20000: 4}, usuario=usuario)  # cifra distinta
    arqueo.refresh_from_db()
    assert arqueo.autorizado_por is None  # la autorización anterior no se arrastra


def test_vista_arqueo_requiere_login(client, dia_con_ventas):
    resp = client.get(reverse("arqueo_efectivo", args=[FECHA.isoformat()]))
    assert resp.status_code == 302


def test_operador_no_puede_autorizar_diferencia(client, usuario, dia_con_ventas):
    client.force_login(usuario)
    arqueo = preparar_arqueos_del_dia(dia_con_ventas)[0]
    registrar_conteo(arqueo, fondo_inicial=Decimal(0), gastos_autorizados=Decimal(0),
                      denominaciones={20000: 3}, usuario=usuario)
    motivo = MotivoResolucion.objects.get(codigo="faltante_en_investigacion")
    resp = client.post(
        reverse("autorizar_diferencia", args=[arqueo.pk]),
        {"motivo_id": motivo.pk, "comentario": "intento sin ser aprobador"},
    )
    assert resp.status_code == 403
    arqueo.refresh_from_db()
    assert arqueo.autorizado_por is None


def test_aprobador_si_puede_autorizar_por_la_vista(client, aprobador, dia_con_ventas, usuario):
    client.force_login(aprobador)
    arqueo = preparar_arqueos_del_dia(dia_con_ventas)[0]
    registrar_conteo(arqueo, fondo_inicial=Decimal(0), gastos_autorizados=Decimal(0),
                      denominaciones={20000: 3}, usuario=usuario)
    motivo = MotivoResolucion.objects.get(codigo="faltante_en_investigacion")
    resp = client.post(
        reverse("autorizar_diferencia", args=[arqueo.pk]),
        {"motivo_id": motivo.pk, "comentario": "confirmado"},
    )
    assert resp.status_code == 302
    arqueo.refresh_from_db()
    assert arqueo.autorizado_por == aprobador
