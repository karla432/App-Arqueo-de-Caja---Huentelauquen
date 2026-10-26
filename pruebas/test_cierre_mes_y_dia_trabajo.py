"""Cierre de mes, notas de crédito al cierre de mes y día de trabajo del menú (Karla, 2026-10-26)."""
from datetime import date

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from nucleo.cierre_mes_servicio import CierreMesInvalido, cerrar_mes, reabrir_mes, revision
from nucleo.cierre_servicio import CierreInvalido, estado_checklist, reabrir_dia
from nucleo.models import CierreMes, DepositoBancario, DiaArqueo, EventoAuditoria, RegularizacionDocumento
from nucleo.notas_credito_servicio import (
    dias_habiles_entre, notas_atrasadas, registrar_evidencia, registrar_folio_odoo, verificar,
)
from nucleo.permisos import PermisoDenegado
from pruebas.perfiles_de_prueba import GRUPO_ENCARGADO, GRUPO_GERENTE, GRUPO_JEFE, crear_usuario

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"
HOY = date(2026, 11, 3)


def _pdf():
    return SimpleUploadedFile("vale.pdf", PDF, content_type="application/pdf")


@pytest.fixture
def encargada(django_user_model):
    return crear_usuario(django_user_model, "nathaly", GRUPO_ENCARGADO)


@pytest.fixture
def jefe(django_user_model):
    return crear_usuario(django_user_model, "ricardo", GRUPO_JEFE)


@pytest.fixture
def gerente(django_user_model):
    return crear_usuario(django_user_model, "karla", GRUPO_GERENTE)


def _dia(fecha, estado=DiaArqueo.Estado.EXPORTADO):
    return DiaArqueo.objects.create(fecha=fecha, estado=estado)


def _nota(dia, folio="F0610600008335", monto=4500):
    return RegularizacionDocumento.objects.create(
        dia=dia, tipo="CRED", codigo_original="CRE", n_interno=int(folio[-4:]), folio=folio, caja="CAJA 1",
        monto=monto, forma_pago="efectivo",
    )


# ------------------------------------------------------------------ NC: día vs mes

def test_el_dia_exige_solo_la_evidencia_cargada(db, encargada):
    dia = _dia(date(2026, 10, 24), DiaArqueo.Estado.EXCEPCIONES_RESUELTAS)
    nota = _nota(dia)
    assert estado_checklist(dia)["notas_credito_verificadas"]["ok"] is False
    registrar_evidencia(nota, medio="efectivo", boleta_original="1695733", comentario="", comprobante=_pdf(), usuario=encargada)
    assert estado_checklist(dia)["notas_credito_verificadas"]["ok"] is True  # sin esperar la verificación del Jefe


def test_dias_habiles_y_alerta_de_notas_atrasadas(db, encargada):
    assert dias_habiles_entre(date(2026, 10, 23), date(2026, 10, 30)) == 5  # vie → vie: lun..vie
    assert dias_habiles_entre(date(2026, 10, 24), date(2026, 10, 26)) == 1  # sáb → lun
    nota = _nota(_dia(date(2026, 10, 20)))
    assert nota in notas_atrasadas(hoy=date(2026, 10, 28))  # 6 días hábiles
    assert notas_atrasadas(hoy=date(2026, 10, 27)) == []    # 5: todavía en plazo


def test_folio_odoo_lo_registra_contabilidad(db, encargada, jefe):
    nota = _nota(_dia(date(2026, 10, 24)))
    with pytest.raises(PermisoDenegado):
        registrar_folio_odoo(nota, "NC 1234", encargada)
    registrar_folio_odoo(nota, "  NC 1234 ", jefe)
    nota.refresh_from_db()
    assert nota.folio_odoo == "NC 1234" and nota.folio_odoo_por == jefe
    assert EventoAuditoria.objects.filter(accion="registrar_folio_odoo_nc", entidad_id=str(nota.pk)).exists()


# ------------------------------------------------------------------ cierre de mes

@pytest.fixture
def octubre(db):
    for d in range(1, 32):
        _dia(date(2026, 10, d))
    return date(2026, 10, 1)


def test_el_mes_exige_dias_exportados_nc_verificadas_con_folio_y_depositos(octubre, encargada, jefe):
    dia24 = DiaArqueo.objects.get(fecha=date(2026, 10, 24))
    nota = _nota(dia24)
    DepositoBancario.objects.create(dia=dia24, monto_esperado=1000, registrado_por=encargada)
    DiaArqueo.objects.filter(fecha=date(2026, 10, 15)).update(estado=DiaArqueo.Estado.CERRADO)

    r = revision(2026, 10, hoy=HOY)
    faltan = {c["clave"] for c in r["faltan"]}
    assert faltan == {"dias", "nc_verificadas", "nc_folio", "depositos"}
    with pytest.raises(CierreMesInvalido, match="pendientes"):
        cerrar_mes(2026, 10, jefe, hoy=HOY)

    DiaArqueo.objects.filter(fecha=date(2026, 10, 15)).update(estado=DiaArqueo.Estado.EXPORTADO)
    registrar_evidencia(nota, medio="efectivo", boleta_original="1695733", comentario="", comprobante=_pdf(), usuario=encargada)
    verificar(nota, aprobar=True, comentario="", usuario=jefe)
    registrar_folio_odoo(nota, "NC 77", jefe)
    DepositoBancario.objects.filter(dia=dia24).update(estado=DepositoBancario.Estado.APROBADO)

    assert revision(2026, 10, hoy=HOY)["faltan"] == []
    cerrar_mes(2026, 10, jefe, hoy=HOY)
    assert CierreMes.objects.get(anio=2026, mes=10).cerrado
    assert EventoAuditoria.objects.filter(accion="cerrar_mes", entidad_id="2026-10").exists()


def test_solo_el_jefe_cierra_y_no_antes_de_que_termine_el_mes(octubre, gerente, jefe):
    with pytest.raises(PermisoDenegado):
        cerrar_mes(2026, 10, gerente, hoy=HOY)
    with pytest.raises(CierreMesInvalido, match="no termina"):
        cerrar_mes(2026, 10, jefe, hoy=date(2026, 10, 31))


def test_mes_cerrado_bloquea_reabrir_un_dia_y_la_gerente_lo_reabre_con_motivo(octubre, jefe, gerente):
    cerrar_mes(2026, 10, jefe, hoy=HOY)
    dia = DiaArqueo.objects.get(fecha=date(2026, 10, 10))
    with pytest.raises(CierreInvalido, match="mes"):
        reabrir_dia(dia, gerente, motivo="corregir")
    with pytest.raises(PermisoDenegado):
        reabrir_mes(2026, 10, jefe, "x")
    with pytest.raises(CierreMesInvalido, match="motivo"):
        reabrir_mes(2026, 10, gerente, " ")
    reabrir_mes(2026, 10, gerente, "Ajuste de una nota")
    reabrir_dia(dia, gerente, motivo="corregir")
    dia.refresh_from_db()
    assert dia.estado == DiaArqueo.Estado.EXCEPCIONES_RESUELTAS


def test_pantalla_de_cierre_de_mes(client, octubre, jefe, encargada):
    client.force_login(jefe)
    html = client.get(reverse("cierre_mes") + "?mes=2026-10").content.decode()
    assert "Lista de verificación del mes" in html and "Cerrar mes 10/2026" in html
    client.force_login(encargada)
    html = client.get(reverse("cierre_mes") + "?mes=2026-10").content.decode()
    assert "lo hace el Jefe de Contabilidad" in html


# ------------------------------------------------------------------ día de trabajo

def test_el_menu_recuerda_el_dia_al_pasar_por_otra_pantalla(client, db, encargada):
    _dia(date(2026, 10, 13), DiaArqueo.Estado.CONCILIADO_CON_EXCEPCIONES)
    _dia(date(2026, 10, 22))
    client.force_login(encargada)
    resp = client.get(reverse("bandeja_excepciones", args=["2026-10-13"]))
    assert resp.cookies["dia_trabajo"].value == "2026-10-13"
    html = client.get(reverse("control_depositos")).content.decode()
    # Sin la cookie el menú iría al último día con datos (22); con ella, sigue en el 13.
    assert reverse("bandeja_excepciones", args=["2026-10-13"]) in html
    assert reverse("bandeja_excepciones", args=["2026-10-22"]) not in html


def test_el_selector_lleva_a_la_misma_pantalla_y_lista_pendientes(client, db, encargada):
    _dia(date(2026, 10, 13), DiaArqueo.Estado.CONCILIADO_CON_EXCEPCIONES)
    _dia(date(2026, 10, 14))
    client.force_login(encargada)
    resp = client.get(reverse("ir_a_dia") + "?fecha=2026-10-14&destino=arqueo_efectivo")
    assert resp.status_code == 302 and resp["Location"] == reverse("arqueo_efectivo", args=["2026-10-14"])
    assert resp.cookies["dia_trabajo"].value == "2026-10-14"
    resp = client.get(reverse("ir_a_dia") + "?fecha=2026-10-14&destino=configuracion")  # destino no permitido
    assert resp["Location"] == reverse("bandeja_excepciones", args=["2026-10-14"])
    html = client.get(reverse("bandeja_excepciones", args=["2026-10-14"])).content.decode()
    assert "Pendientes · 1" in html and "13-10-2026" in html  # el 13 no está exportado
