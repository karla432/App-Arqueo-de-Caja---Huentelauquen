"""Documentos con un código que la app no conoce (Karla, 2026-09-24): no se
descartan ni se les supone un efecto — quedan en Regularización de Documentos
con su código original y la alerta «Requiere clasificación», no mueven caja ni
ventas y bloquean el cierre hasta que el Jefe de Contabilidad los clasifique.
Definido con los agentes de Control, Contador Senior y Tesorero."""
from datetime import date

import pandas as pd
import pytest
from nucleo.permisos import PermisoDenegado
from pruebas.perfiles_de_prueba import GRUPO_ENCARGADO, crear_usuario
from django.contrib.auth.models import Group
from django.urls import reverse

from arqueo_diario.importadores.pos import importar_ventas_con_libro_contable
from nucleo.arqueo_servicio import calcular_teorico_por_caja
from nucleo.carga import cargar_pos_con_libro_contable
from nucleo.cierre_servicio import estado_checklist
from nucleo.documentos_servicio import GRUPO_JEFE_CONTABILIDAD
from nucleo.models import DiaArqueo, EventoAuditoria, RegularizacionDocumento
from nucleo.regularizacion_servicio import RegularizacionInvalida, clasificar_documento

from pruebas.test_documentos_libro import _fila_libro, _ventas_df

FECHA = date(2026, 9, 1)


@pytest.fixture
def usuario(django_user_model):
    return crear_usuario(django_user_model, "operador", GRUPO_ENCARGADO, password="x")


@pytest.fixture
def jefe(django_user_model):
    u = django_user_model.objects.create_user(username="jefe", password="x")
    u.groups.add(Group.objects.get_or_create(name=GRUPO_JEFE_CONTABILIDAD)[0])
    return u


@pytest.fixture
def archivos(tmp_path):
    ventas, libro = tmp_path / "v.xlsx", tmp_path / "l.xlsx"
    _ventas_df().to_excel(ventas, sheet_name="VENTAS POR ARTICULO", index=False)
    pd.DataFrame([
        _fila_libro(100, "FAC", "B0000001", "EFECTIVO", debe=1000),
        _fila_libro(900, "XYZ", "X0000000012345", "EFECTIVO", debe=7000, caja=1),
    ]).to_excel(libro, index=False)
    return ventas, libro


@pytest.fixture
def dia_con_codigo_nuevo(db, usuario, archivos):
    dia = DiaArqueo.objects.create(fecha=FECHA, operado_por=usuario)
    cargar_pos_con_libro_contable(*archivos, FECHA, usuario)
    return dia


def test_un_codigo_desconocido_no_se_descarta_queda_sin_clasificar_con_su_codigo(dia_con_codigo_nuevo):
    doc = RegularizacionDocumento.objects.get(dia=dia_con_codigo_nuevo)
    assert (doc.tipo, doc.codigo_original, doc.monto) == ("SIN", "XYZ", 7000)


def test_el_importador_mantiene_el_aviso_y_ya_no_dice_que_se_descarta(archivos):
    resultado = importar_ventas_con_libro_contable(*archivos, FECHA)
    assert len(resultado.sin_clasificar) == 1
    assert any("'XYZ'" in a and "Sin clasificar" in a for a in resultado.avisos)


def test_un_documento_sin_clasificar_no_mueve_el_efectivo(dia_con_codigo_nuevo):
    # aunque Resto lo haya registrado en efectivo por $7.000
    assert 7000 not in calcular_teorico_por_caja(dia_con_codigo_nuevo).values()
    assert calcular_teorico_por_caja(dia_con_codigo_nuevo) == {"CAJA 1": 1000}


def test_bloquea_el_cierre_hasta_clasificarlo(dia_con_codigo_nuevo, jefe):
    assert estado_checklist(dia_con_codigo_nuevo)["documentos_sin_clasificar"]["ok"] is False
    doc = RegularizacionDocumento.objects.get(dia=dia_con_codigo_nuevo)
    clasificar_documento(doc, tipo="DES", motivo="PDF del SII: guía de despacho", usuario=jefe)
    assert estado_checklist(dia_con_codigo_nuevo)["documentos_sin_clasificar"]["ok"] is True


def test_clasificar_conserva_el_codigo_original_y_deja_bitacora(dia_con_codigo_nuevo, jefe):
    doc = RegularizacionDocumento.objects.get(dia=dia_con_codigo_nuevo)
    clasificar_documento(doc, tipo="CRED", motivo="PDF del SII", usuario=jefe)
    doc.refresh_from_db()
    assert (doc.tipo, doc.codigo_original, doc.clasificado_por, doc.motivo_clasificacion) == ("CRED", "XYZ", jefe, "PDF del SII")
    evento = EventoAuditoria.objects.get(accion="clasificar_documento")
    assert evento.valor_anterior["tipo"] == "SIN" and evento.valor_posterior["tipo"] == "CRED"


def test_solo_el_jefe_o_superior_clasifica_y_con_motivo(dia_con_codigo_nuevo, usuario, jefe):
    doc = RegularizacionDocumento.objects.get(dia=dia_con_codigo_nuevo)
    with pytest.raises(PermisoDenegado):  # la Encargada no clasifica: eso lo valida el Jefe de Contabilidad
        clasificar_documento(doc, tipo="DES", motivo="x", usuario=usuario)
    with pytest.raises(RegularizacionInvalida, match="motivo"):
        clasificar_documento(doc, tipo="DES", motivo="  ", usuario=jefe)
    with pytest.raises(RegularizacionInvalida, match="guía de despacho o una nota"):
        clasificar_documento(doc, tipo="FAC", motivo="x", usuario=jefe)


def test_ya_clasificado_no_se_puede_reclasificar(dia_con_codigo_nuevo, jefe):
    doc = RegularizacionDocumento.objects.get(dia=dia_con_codigo_nuevo)
    clasificar_documento(doc, tipo="DES", motivo="x", usuario=jefe)
    with pytest.raises(RegularizacionInvalida, match="ya está clasificado"):
        clasificar_documento(doc, tipo="CRED", motivo="x", usuario=jefe)


def test_la_pantalla_muestra_el_codigo_y_la_alerta(client, dia_con_codigo_nuevo, jefe):
    client.force_login(jefe)
    html = client.get(reverse("regularizacion_documentos")).content.decode()
    assert "Requiere clasificación" in html and "XYZ" in html and "bloquea el cierre" in html
    doc = RegularizacionDocumento.objects.get(dia=dia_con_codigo_nuevo)
    client.post(reverse("clasificar_documento", args=[doc.pk]), {"tipo": "CRED", "motivo": "PDF SII"})
    doc.refresh_from_db()
    assert doc.tipo == "CRED"
