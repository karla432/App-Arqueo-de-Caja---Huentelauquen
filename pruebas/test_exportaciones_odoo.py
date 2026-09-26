"""Menú «Exportaciones a Odoo» (Karla, 2026-09-24): las cuatro capturas juntas, explicadas y descargables
desde un solo lugar; en las otras pantallas solo queda el enlace."""
from datetime import date
from decimal import Decimal

import pytest
from pruebas.perfiles_de_prueba import GRUPO_GERENTE, crear_usuario
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from nucleo.depositos_servicio import registrar_detalle
from nucleo.models import (
    ArqueoEfectivo, Cajera, CuentaContable, DepositoBancario, DiaArqueo, Exportacion, ReintegroCaja,
)
from nucleo.reintegros_servicio import completar_detalle_reintegro, registrar_reintegro

FECHA = date(2026, 9, 7)


def _pdf(nombre="c.pdf"):
    return SimpleUploadedFile(nombre, b"%PDF-1.4 x", content_type="application/pdf")


@pytest.fixture
def usuario(django_user_model):
    return crear_usuario(django_user_model, "operador", GRUPO_GERENTE)


@pytest.fixture
def banco(db):
    return CuentaContable.objects.create(codigo="1102001", nombre="Banco Estado 10397-7", tipo="banco", diario_odoo="Banco Estado")


@pytest.fixture
def dia_exportado(usuario, tmp_path):
    dia = DiaArqueo.objects.create(fecha=FECHA, operado_por=usuario, estado="exportado")
    ventas, arqueo = tmp_path / "ventas.xlsx", tmp_path / "arqueo.xlsx"
    ventas.write_bytes(b"x"), arqueo.write_bytes(b"x")
    Exportacion.objects.create(dia=dia, version=1, archivo=str(ventas), archivo_arqueo=str(arqueo), generado_por=usuario)
    return dia


def test_las_cuatro_capturas_se_explican_en_una_sola_pantalla(client, usuario, db):
    client.force_login(usuario)
    html = client.get(reverse("exportaciones_odoo"), {"anio": 2026, "mes": 9}).content.decode()
    for titulo in ("Captura Ventas", "Captura Arqueo Caja", "Captura Depósito", "Captura Reintegro Caja"):
        assert titulo in html
    from nucleo.exportaciones_servicio import CAPTURAS
    for c in CAPTURAS:  # la explicación de cada captura está en la pantalla (plegada salvo «para qué sirve»)
        assert c["sirve"] in html and c["cuando"] in html and c["contiene"] in html
    assert "<details" in html
    # el 3 y el 4: «Depósito en Efectivo en Banco» y disponibles antes de la validación del Jefe
    assert html.count("Depósito en Efectivo en Banco") == 2
    assert html.count("sin esperar la validación del Jefe de Contabilidad") == 2
    assert "Archivos para Odoo" in html  # menú lateral


def test_ventas_y_arqueo_se_descargan_desde_la_version_vigente(client, dia_exportado, usuario):
    client.force_login(usuario)
    html = client.get(reverse("exportaciones_odoo"), {"anio": 2026, "mes": 9}).content.decode()
    exp = dia_exportado.exportaciones.get()
    assert reverse("descargar_exportacion", args=[exp.pk]) in html and reverse("descargar_arqueo_caja", args=[exp.pk]) in html
    assert "⬇ Ventas" in html and "⬇ Arqueo" in html  # una sola tabla por día, dos botones


def test_una_version_invalidada_no_se_ofrece_y_el_dia_queda_por_generar(client, dia_exportado, usuario):
    dia_exportado.exportaciones.update(invalidada=True)
    dia_exportado.estado = "aprobado"
    dia_exportado.save()
    client.force_login(usuario)
    html = client.get(reverse("exportaciones_odoo"), {"anio": 2026, "mes": 9}).content.decode()
    assert "⬇ Ventas" not in html and "Sin archivos generados" in html and "Requiere tu atención" in html
    assert "Generar ahora" in html  # red de seguridad: se pueden generar desde aquí


def test_el_deposito_y_el_reintegro_aparecen_con_su_boton_o_con_lo_que_falta(client, dia_exportado, usuario, banco):
    dep = DepositoBancario.objects.create(dia=dia_exportado, monto_esperado=Decimal(50000), registrado_por=usuario)
    client.force_login(usuario)
    html = client.get(reverse("exportaciones_odoo"), {"anio": 2026, "mes": 9}).content.decode()
    assert "Falta ingresar los datos del depósito" in html and reverse("captura_deposito", args=[dep.pk]) not in html
    registrar_detalle(dep, fecha_deposito=date(2026, 9, 24), banco=banco.nombre, cuenta_bancaria="1", monto_depositado=Decimal(50000),
                       numero_operacion="OP", responsable_traslado="Rosa", comprobante=_pdf(), usuario=usuario)
    html = client.get(reverse("exportaciones_odoo"), {"anio": 2026, "mes": 9}).content.decode()
    assert reverse("generar_captura_deposito", args=[dep.pk]) in html  # el detalle está: falta generar el archivo
    client.post(reverse("generar_captura_deposito", args=[dep.pk]))
    html = client.get(reverse("exportaciones_odoo"), {"anio": 2026, "mes": 9}).content.decode()
    assert reverse("captura_deposito", args=[dep.pk]) in html and "⬇ Descargar" in html

    cajera = Cajera.objects.create(nombre="Rosa Rojo")
    arqueo = ArqueoEfectivo.objects.create(dia=dia_exportado, caja="CAJA 4", cajera=cajera, diferencia=Decimal(-2000))
    rein = registrar_reintegro(arqueo, cajera=cajera, monto=Decimal(1000), usuario=usuario)
    html = client.get(reverse("exportaciones_odoo"), {"anio": 2026, "mes": 9}).content.decode()
    assert "Falta completar el detalle del reintegro" in html and reverse("descargar_asiento_reintegro", args=[rein.pk]) not in html
    completar_detalle_reintegro(rein, fecha_reintegro=date(2026, 9, 24), banco=banco.nombre, cuenta_bancaria="1", numero_operacion="OP2",
                                comprobante=_pdf("r.pdf"), observaciones="", usuario=usuario)
    html = client.get(reverse("exportaciones_odoo"), {"anio": 2026, "mes": 9}).content.decode()
    assert reverse("generar_captura_reintegro", args=[rein.pk]) in html
    client.post(reverse("generar_captura_reintegro", args=[rein.pk]))
    html = client.get(reverse("exportaciones_odoo"), {"anio": 2026, "mes": 9}).content.decode()
    assert "Captura Reintegro Caja 4-070926.xlsx" in html and reverse("descargar_asiento_reintegro", args=[rein.pk]) in html


def test_un_banco_sin_diario_aparece_bloqueado_con_su_motivo(client, dia_exportado, usuario, banco):
    dep = DepositoBancario.objects.create(dia=dia_exportado, monto_esperado=Decimal(50000), registrado_por=usuario)
    registrar_detalle(dep, fecha_deposito=date(2026, 9, 24), banco=banco.nombre, cuenta_bancaria="1", monto_depositado=Decimal(50000),
                       numero_operacion="OP", responsable_traslado="Rosa", comprobante=_pdf(), usuario=usuario)
    banco.diario_odoo = ""
    banco.save()
    client.force_login(usuario)
    html = client.get(reverse("exportaciones_odoo"), {"anio": 2026, "mes": 9}).content.decode()
    assert "le falta el Diario" in html and reverse("captura_deposito", args=[dep.pk]) not in html


def test_cierre_ya_no_trae_los_botones_de_descarga_sino_el_enlace(client, dia_exportado, usuario):
    client.force_login(usuario)
    html = client.get(reverse("cierre_y_exportacion", args=[FECHA.isoformat()])).content.decode()
    exp = dia_exportado.exportaciones.get()
    assert reverse("descargar_exportacion", args=[exp.pk]) not in html and reverse("descargar_arqueo_caja", args=[exp.pk]) not in html
    assert reverse("exportaciones_odoo") in html


def test_sin_iniciar_sesion_no_se_accede(client, db):
    assert client.get(reverse("exportaciones_odoo")).status_code == 302


# ------------------------------------------------ escala: un año de datos (consejo de UI)

def _deposito(dia, usuario, fecha_deposito=date(2026, 9, 24)):
    d = DepositoBancario.objects.create(dia=dia, monto_esperado=Decimal(1000), registrado_por=usuario)
    return d


def test_el_mes_filtra_las_tablas_pero_no_la_franja_de_atencion(client, usuario, banco):
    viejo = DiaArqueo.objects.create(fecha=date(2026, 6, 10), operado_por=usuario, estado="cerrado")   # sin versión → por generar
    reciente = DiaArqueo.objects.create(fecha=date(2026, 9, 10), operado_por=usuario, estado="cerrado")
    _deposito(viejo, usuario)  # pendiente de depósito, de junio → bloqueado
    client.force_login(usuario)
    html = client.get(reverse("exportaciones_odoo"), {"anio": 2026, "mes": 9}).content.decode()
    assert "No hay depósitos en septiembre de 2026" in html  # la tabla respeta el mes…
    assert "10-06-2026" in html and "Requiere tu atención · 3" in html  # …pero lo bloqueado de junio sigue a la vista
    assert reverse("cierre_y_exportacion", args=["2026-06-10"]) in html and reverse("cierre_y_exportacion", args=["2026-09-10"]) in html
    junio = client.get(reverse("exportaciones_odoo"), {"anio": 2026, "mes": 6}).content.decode()
    assert "No hay depósitos en" not in junio and "1 del período" in junio


def test_todo_el_anio_y_los_atajos(client, dia_exportado, usuario):
    client.force_login(usuario)
    html = client.get(reverse("exportaciones_odoo"), {"anio": 2026, "mes": "todo"}).content.decode()
    assert "todo 2026" in html and "⬇ Ventas" in html
    sep = client.get(reverse("exportaciones_odoo"), {"anio": 2026, "mes": 9}).content.decode()
    assert "anio=2026&amp;mes=8" in sep or "anio=2026&mes=8" in sep
    assert "anio=2026&amp;mes=10" in sep or "anio=2026&mes=10" in sep


def test_por_defecto_muestra_el_mes_del_ultimo_dia_cargado_si_el_actual_esta_vacio(client, dia_exportado, usuario, monkeypatch):
    import nucleo.views_exportaciones as v
    from datetime import date as _date

    class Hoy(_date):
        @classmethod
        def today(cls):
            return cls(2027, 3, 15)  # marzo de 2027 no tiene días → cae al mes del último día (septiembre de 2026)
    monkeypatch.setattr(v, "date", Hoy)
    client.force_login(usuario)
    assert "septiembre de 2026" in client.get(reverse("exportaciones_odoo")).content.decode()


def test_valores_mal_escritos_no_rompen_la_pantalla(client, usuario, db):
    client.force_login(usuario)
    for q in ({"anio": "abc"}, {"mes": "13"}, {"anio": "1"}, {"mes": "x", "anio": "9999"}, {"p_v": "zz"}):
        assert client.get(reverse("exportaciones_odoo"), q).status_code == 200


def test_las_tablas_se_paginan_de_a_25(client, usuario, banco):
    DiaArqueo.objects.create(fecha=date(2026, 9, 1), operado_por=usuario, estado="cerrado")
    # 30 depósitos del mismo mes (un día con muchos depósitos adicionales)
    dia = DiaArqueo.objects.get(fecha=date(2026, 9, 1))
    for _ in range(29):
        DepositoBancario.objects.create(dia=dia, monto_esperado=Decimal(1000), registrado_por=usuario)
    client.force_login(usuario)
    p1 = client.get(reverse("exportaciones_odoo"), {"anio": 2026, "mes": 9}).content.decode()
    assert "Página 1 de 2" in p1 and "Siguiente →" in p1
    p2 = client.get(reverse("exportaciones_odoo"), {"anio": 2026, "mes": 9, "p_d": 2}).content.decode()
    assert "Página 2 de 2" in p2


def test_los_contadores_muestran_periodo_pendientes_y_total(client, dia_exportado, usuario, banco):
    _deposito(dia_exportado, usuario)
    client.force_login(usuario)
    html = client.get(reverse("exportaciones_odoo"), {"anio": 2026, "mes": 9}).content.decode()
    assert "1 del período" in html and "1 pendientes" in html and "1 en total" in html
