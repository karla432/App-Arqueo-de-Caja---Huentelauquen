"""Reintegros de Caja (Karla, 2026-09-22): cuando una cajera con una
diferencia negativa devuelve la plata antes del descuento en remuneración.
Total o parcial; saldo pendiente = pérdida original − reintegros ya
Validados; la captura para Odoo (Karla, 2026-09-24) es una línea «Recibir» de la
cajera con el Diario del banco elegido en cada reintegro y la cuenta de pérdida;
se genera al validar y se descarga como «Captura Reintegro Caja ddmmaa»."""
from datetime import date
from decimal import Decimal

import pytest
from nucleo.permisos import PermisoDenegado
from pruebas.perfiles_de_prueba import GRUPO_ENCARGADO, GRUPO_GERENTE, crear_usuario
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from nucleo.models import ArqueoEfectivo, Cajera, CuentaContable, DiaArqueo, EventoAuditoria, ReintegroCaja
from nucleo.reintegros_servicio import (
    ReintegroInvalido, completar_detalle_reintegro, estado_agregado, generar_lineas_reintegro, nombre_captura_reintegro,
    perdidas_menores_ocultas, perdidas_pendientes_de_reintegro, regenerar_asiento_reintegro, registrar_reintegro,
    saldo_pendiente, validar_reintegro,
)

FECHA = date(2026, 9, 1)


def _comprobante():
    return SimpleUploadedFile("comprobante.pdf", b"%PDF-1.4 contenido", content_type="application/pdf")


@pytest.fixture
def usuario(django_user_model):
    return crear_usuario(django_user_model, "operador", GRUPO_GERENTE)


@pytest.fixture
def jefe(django_user_model):
    from django.contrib.auth.models import Group
    from nucleo.documentos_servicio import GRUPO_JEFE_CONTABILIDAD
    u = django_user_model.objects.create_user(username="jefe", password="x")
    u.groups.add(Group.objects.get_or_create(name=GRUPO_JEFE_CONTABILIDAD)[0])
    return u


@pytest.fixture(autouse=True)
def bancos(db):
    """Cada reintegro se deposita en un banco de Configuración; su cuenta va al asiento."""
    return {
        "BCI": CuentaContable.objects.create(codigo="1102002", nombre="BCI", tipo="banco", diario_odoo="BCI"),
        "Banco Estado 10397-7": CuentaContable.objects.create(
            codigo="1102001", nombre="Banco Estado 10397-7", tipo="banco", diario_odoo="Banco Estado"),
    }


@pytest.fixture
def cajera():
    return Cajera.objects.create(nombre="Juan Pérez")


@pytest.fixture
def dia(db, usuario):
    return DiaArqueo.objects.create(fecha=FECHA, operado_por=usuario)


@pytest.fixture
def arqueo_con_perdida(dia, cajera):
    return ArqueoEfectivo.objects.create(
        dia=dia, caja="CAJA 1", cajera=cajera, contado=Decimal(35000), diferencia=Decimal(-15000),
    )


# ------------------------------------------------------------------ orígenes

def test_una_diferencia_negativa_con_cajera_aparece_como_pendiente_de_reintegro(arqueo_con_perdida):
    pendientes = perdidas_pendientes_de_reintegro()
    assert pendientes == [arqueo_con_perdida]
    assert saldo_pendiente(arqueo_con_perdida) == 15000
    assert estado_agregado(arqueo_con_perdida) == "pendiente"


def test_una_diferencia_positiva_o_sin_cajera_no_aparece(dia):
    ArqueoEfectivo.objects.create(dia=dia, caja="CAJA 2", diferencia=Decimal(5000))  # ganancia
    ArqueoEfectivo.objects.create(dia=dia, caja="CAJA 3", diferencia=Decimal(-1000))  # sin cajera
    assert perdidas_pendientes_de_reintegro() == []


# --------------------------------------------------------------- registrar

def test_registrar_un_abono_total_deja_registrado_no_valida_solo(arqueo_con_perdida, cajera, usuario):
    reintegro = registrar_reintegro(arqueo_con_perdida, cajera=cajera, monto=Decimal(15000), usuario=usuario)
    assert reintegro.estado == ReintegroCaja.Estado.REGISTRADO
    # Karla / Tesorero: solo un reintegro VALIDADO reduce el saldo.
    assert saldo_pendiente(arqueo_con_perdida) == 15000
    assert EventoAuditoria.objects.filter(accion="registrar_reintegro").exists()


def test_dos_abonos_parciales_pueden_sumar_el_total(arqueo_con_perdida, cajera, usuario):
    r1 = registrar_reintegro(arqueo_con_perdida, cajera=cajera, monto=Decimal(10000), usuario=usuario)
    r2 = registrar_reintegro(arqueo_con_perdida, cajera=cajera, monto=Decimal(5000), usuario=usuario)
    assert {r1.monto, r2.monto} == {10000, 5000}


def test_no_se_puede_registrar_mas_del_saldo_pendiente(arqueo_con_perdida, cajera, usuario):
    with pytest.raises(ReintegroInvalido, match="supera"):
        registrar_reintegro(arqueo_con_perdida, cajera=cajera, monto=Decimal(20000), usuario=usuario)


def test_la_cajera_debe_coincidir_con_el_origen(arqueo_con_perdida, usuario):
    otra = Cajera.objects.create(nombre="Otra Persona")
    with pytest.raises(ReintegroInvalido, match="no corresponde"):
        registrar_reintegro(arqueo_con_perdida, cajera=otra, monto=Decimal(1000), usuario=usuario)


# ----------------------------------------------------------- flujo completo

@pytest.fixture
def reintegro_registrado(arqueo_con_perdida, cajera, usuario):
    return registrar_reintegro(arqueo_con_perdida, cajera=cajera, monto=Decimal(15000), usuario=usuario)


def test_completar_detalle_pasa_a_pendiente_de_validacion(reintegro_registrado, usuario):
    completar_detalle_reintegro(
        reintegro_registrado, fecha_reintegro=date(2026, 9, 15), banco="BCI", cuenta_bancaria="12345",
        numero_operacion="OP-99", comprobante=_comprobante(), observaciones="", usuario=usuario,
    )
    reintegro_registrado.refresh_from_db()
    assert reintegro_registrado.estado == ReintegroCaja.Estado.PENDIENTE_VALIDACION
    assert reintegro_registrado.comprobante_archivo


def test_el_comprobante_es_obligatorio_para_completar_detalle(reintegro_registrado, usuario):
    with pytest.raises(ReintegroInvalido, match="[Cc]omprobante"):
        completar_detalle_reintegro(
            reintegro_registrado, fecha_reintegro=date(2026, 9, 15), banco="BCI", cuenta_bancaria="12345",
            numero_operacion="OP-99", comprobante=None, observaciones="", usuario=usuario,
        )


def test_una_encargada_de_ingresos_no_puede_validar_solo_el_jefe(reintegro_registrado, usuario, django_user_model):
    encargada = crear_usuario(django_user_model, "encargada", GRUPO_ENCARGADO)
    completar_detalle_reintegro(
        reintegro_registrado, fecha_reintegro=date(2026, 9, 15), banco="BCI", cuenta_bancaria="12345",
        numero_operacion="OP-99", comprobante=_comprobante(), observaciones="", usuario=usuario,
    )
    with pytest.raises(PermisoDenegado):
        validar_reintegro(
            reintegro_registrado, aprobar=True, movimiento_bancario_referencia="Cartola X", comentario="",
            usuario=encargada,
        )


def test_validar_exige_referencia_bancaria_para_aprobar(reintegro_registrado, usuario, jefe):
    completar_detalle_reintegro(
        reintegro_registrado, fecha_reintegro=date(2026, 9, 15), banco="BCI", cuenta_bancaria="12345",
        numero_operacion="OP-99", comprobante=_comprobante(), observaciones="", usuario=usuario,
    )
    with pytest.raises(ReintegroInvalido, match="cartola"):
        validar_reintegro(reintegro_registrado, aprobar=True, movimiento_bancario_referencia="", comentario="", usuario=jefe)


def test_validar_y_aprobar_reduce_el_saldo_y_genera_el_asiento(reintegro_registrado, arqueo_con_perdida, usuario, jefe):
    completar_detalle_reintegro(
        reintegro_registrado, fecha_reintegro=date(2026, 9, 15), banco="BCI", cuenta_bancaria="12345",
        numero_operacion="OP-99", comprobante=_comprobante(), observaciones="", usuario=usuario,
    )
    validar_reintegro(
        reintegro_registrado, aprobar=True, movimiento_bancario_referencia="Cartola BCI 16/09, folio 778899",
        comentario="", usuario=jefe,
    )
    reintegro_registrado.refresh_from_db()
    assert reintegro_registrado.estado == ReintegroCaja.Estado.VALIDADO
    assert reintegro_registrado.exportado_en is not None
    assert saldo_pendiente(arqueo_con_perdida) == 0
    assert estado_agregado(arqueo_con_perdida) == "reintegrado"
    assert perdidas_pendientes_de_reintegro() == []  # ya no sale como pendiente


def _detalle(reintegro, usuario, banco="BCI"):
    completar_detalle_reintegro(
        reintegro, fecha_reintegro=date(2026, 9, 15), banco=banco, cuenta_bancaria="12345",
        numero_operacion="OP-99", comprobante=_comprobante(), observaciones="", usuario=usuario,
    )


def test_la_captura_es_una_linea_recibir_con_el_diario_del_banco_elegido(reintegro_registrado, usuario):
    """Formato pedido por Karla el 24-09: Recibir / cajera / importe / fecha del arqueo / cuenta de pérdida / memo / Diario."""
    _detalle(reintegro_registrado, usuario, banco="Banco Estado 10397-7")
    lineas = generar_lineas_reintegro(reintegro_registrado)
    assert lineas == [{
        "Tipo de pago": "Recibir", "Cliente/proveedor": "Juan Pérez", "Importe en moneda de la empresa firmado": 15000,
        "Fecha": FECHA, "Cuenta Contable": "6501004 Pérdida por diferencia de efectivo",
        "Memo": "Reintegro (reversa de pérdida de caja) – Juan Pérez – Arqueo 01/09/2026", "Diario": "Banco Estado",
    }]


def test_otro_banco_otro_diario_sin_tocar_la_configuracion(reintegro_registrado, arqueo_con_perdida, cajera, usuario):
    segundo = registrar_reintegro(arqueo_con_perdida, cajera=cajera, monto=Decimal(1000), usuario=usuario)
    _detalle(reintegro_registrado, usuario, banco="Banco Estado 10397-7")
    _detalle(segundo, usuario, banco="BCI")
    assert generar_lineas_reintegro(reintegro_registrado)[0]["Diario"] == "Banco Estado"
    assert generar_lineas_reintegro(segundo)[0]["Diario"] == "BCI"


def test_el_nombre_del_archivo_lleva_el_numero_de_caja_y_la_fecha_del_arqueo(reintegro_registrado):
    assert nombre_captura_reintegro(reintegro_registrado) == "Captura Reintegro Caja 1-010926.xlsx"


def test_dos_reintegros_del_mismo_dia_en_cajas_distintas_tienen_nombres_distintos(dia, cajera, usuario):
    a1 = ArqueoEfectivo.objects.create(dia=dia, caja="CAJA 4", cajera=cajera, diferencia=Decimal(-2000))
    a2 = ArqueoEfectivo.objects.create(dia=dia, caja="CAJA 5", cajera=cajera, diferencia=Decimal(-3000))
    r1 = registrar_reintegro(a1, cajera=cajera, monto=Decimal(1000), usuario=usuario)
    r2 = registrar_reintegro(a2, cajera=cajera, monto=Decimal(1000), usuario=usuario)
    assert nombre_captura_reintegro(r1) == "Captura Reintegro Caja 4-010926.xlsx"
    assert nombre_captura_reintegro(r2) == "Captura Reintegro Caja 5-010926.xlsx"


def test_sin_caja_conocida_el_nombre_omite_el_numero(reintegro_registrado):
    ArqueoEfectivo.objects.filter(pk=reintegro_registrado.arqueo_origen_id).update(caja="")
    reintegro_registrado.refresh_from_db()
    assert nombre_captura_reintegro(reintegro_registrado) == "Captura Reintegro Caja 010926.xlsx"


def test_un_banco_sin_diario_impide_validar(reintegro_registrado, usuario, jefe, bancos):
    bancos["BCI"].diario_odoo = ""
    bancos["BCI"].save()
    _detalle(reintegro_registrado, usuario, banco="BCI")
    with pytest.raises(ReintegroInvalido, match="falta el Diario"):
        validar_reintegro(reintegro_registrado, aprobar=True, movimiento_bancario_referencia="Cartola", comentario="", usuario=jefe)


def test_un_banco_que_no_esta_en_configuracion_impide_validar_y_no_deja_nada_a_medias(reintegro_registrado, usuario, jefe):
    _detalle(reintegro_registrado, usuario, banco="Banco Fantasma")
    with pytest.raises(ReintegroInvalido, match="no existe entre las cuentas de Banco"):
        validar_reintegro(reintegro_registrado, aprobar=True, movimiento_bancario_referencia="Cartola", comentario="", usuario=jefe)
    reintegro_registrado.refresh_from_db()
    assert reintegro_registrado.estado == ReintegroCaja.Estado.PENDIENTE_VALIDACION and not reintegro_registrado.archivo_asiento


def test_regenerar_deja_el_archivo_en_el_formato_nuevo(reintegro_registrado, usuario, jefe, tmp_path):
    from openpyxl import load_workbook
    _detalle(reintegro_registrado, usuario, banco="Banco Estado 10397-7")
    validar_reintegro(reintegro_registrado, aprobar=True, movimiento_bancario_referencia="Cartola", comentario="", usuario=jefe)
    reintegro_registrado.refresh_from_db()
    ruta = regenerar_asiento_reintegro(reintegro_registrado, usuario)
    filas = list(load_workbook(ruta).active.iter_rows(values_only=True))
    assert filas[0][0] == "Tipo de pago" and filas[1][:2] == ("Recibir", "Juan Pérez") and filas[1][6] == "Banco Estado"
    assert EventoAuditoria.objects.filter(accion="regenerar_asiento_reintegro", entidad_id=str(reintegro_registrado.pk)).exists()


def test_un_reintegro_sin_detalle_no_tiene_captura(reintegro_registrado, usuario):
    with pytest.raises(ReintegroInvalido, match="Primero completa el detalle"):
        regenerar_asiento_reintegro(reintegro_registrado, usuario)


def test_la_captura_esta_disponible_antes_de_la_validacion_del_jefe(reintegro_registrado, usuario):
    """Karla, 2026-09-24: igual que la captura de depósito, apenas se ingresa el detalle."""
    _detalle(reintegro_registrado, usuario, banco="Banco Estado 10397-7")
    assert reintegro_registrado.estado == ReintegroCaja.Estado.PENDIENTE_VALIDACION
    assert regenerar_asiento_reintegro(reintegro_registrado, usuario).exists()


# ------------------------------------------ pérdidas menores no se listan (2026-09-24)

@pytest.mark.parametrize("perdida,listada", [(500, False), (501, True), (10, False), (15000, True)])
def test_las_perdidas_hasta_500_no_aparecen_en_el_panel(dia, cajera, perdida, listada):
    arqueo = ArqueoEfectivo.objects.create(dia=dia, caja="CAJA 1", cajera=cajera, diferencia=Decimal(-perdida))
    assert (arqueo in perdidas_pendientes_de_reintegro()) is listada
    assert (arqueo in perdidas_menores_ocultas()) is (not listada)
    assert arqueo in perdidas_pendientes_de_reintegro(incluir_menores=True)  # sigue existiendo y contada


def test_una_perdida_menor_con_abonos_ya_registrados_se_sigue_mostrando(dia, cajera, usuario):
    arqueo = ArqueoEfectivo.objects.create(dia=dia, caja="CAJA 1", cajera=cajera, diferencia=Decimal(-10))
    registrar_reintegro(arqueo, cajera=cajera, monto=Decimal(10), usuario=usuario)
    assert arqueo in perdidas_pendientes_de_reintegro()


def test_el_umbral_de_perdidas_menores_es_configurable(dia, cajera):
    arqueo = ArqueoEfectivo.objects.create(dia=dia, caja="CAJA 1", cajera=cajera, diferencia=Decimal(-800))
    from nucleo.models import Parametro
    Parametro.objects.create(clave="umbral_reintegro_minimo", valor=1000)
    assert arqueo in perdidas_menores_ocultas() and arqueo not in perdidas_pendientes_de_reintegro()
    Parametro.objects.filter(clave="umbral_reintegro_minimo").update(valor=0)
    assert arqueo in perdidas_pendientes_de_reintegro()


def test_una_ganancia_nunca_aparece_ni_como_menor(dia, cajera):
    ArqueoEfectivo.objects.create(dia=dia, caja="CAJA 1", cajera=cajera, diferencia=Decimal(300))
    assert perdidas_pendientes_de_reintegro(incluir_menores=True) == [] and perdidas_menores_ocultas() == []


def test_el_panel_muestra_el_contador_de_menores_ocultas_y_permite_verlas(client, dia, cajera, arqueo_con_perdida, usuario):
    ArqueoEfectivo.objects.create(dia=dia, caja="CAJA 2", cajera=cajera, diferencia=Decimal(-10))
    client.force_login(usuario)
    html = client.get(reverse("reintegros_caja")).content.decode()
    assert "1 pérdida(s) de hasta $500 oculta(s)" in html and "pérdida $10 " not in html
    assert "pérdida $10 " in client.get(reverse("reintegros_caja"), {"ver_menores": "1"}).content.decode()


def test_el_boton_vive_en_exportaciones_y_aqui_solo_queda_el_enlace(client, reintegro_registrado, arqueo_con_perdida, usuario):
    _detalle(reintegro_registrado, usuario, banco="Banco Estado 10397-7")  # sin esperar la validación
    client.force_login(usuario)
    html = client.get(reverse("reintegros_caja"), {"tipo": "arqueo", "origen": arqueo_con_perdida.pk}).content.decode()
    assert "Descargar Captura Reintegro Caja" not in html and reverse("exportaciones_odoo") in html
    exportaciones = client.get(reverse("exportaciones_odoo")).content.decode()
    assert reverse("generar_captura_reintegro", args=[reintegro_registrado.pk]) in exportaciones  # primero se genera
    client.post(reverse("generar_captura_reintegro", args=[reintegro_registrado.pk]))
    exportaciones = client.get(reverse("exportaciones_odoo")).content.decode()
    assert "Descargar Captura Reintegro Caja 1-010926.xlsx" in exportaciones
    resp = client.get(reverse("descargar_asiento_reintegro", args=[reintegro_registrado.pk]))
    assert resp.status_code == 200 and resp["Content-Disposition"].startswith("attachment")
    assert "Captura Reintegro Caja 1-010926.xlsx" in resp["Content-Disposition"]


def test_observar_no_reduce_el_saldo(reintegro_registrado, arqueo_con_perdida, usuario, jefe):
    completar_detalle_reintegro(
        reintegro_registrado, fecha_reintegro=date(2026, 9, 15), banco="BCI", cuenta_bancaria="12345",
        numero_operacion="OP-99", comprobante=_comprobante(), observaciones="", usuario=usuario,
    )
    validar_reintegro(reintegro_registrado, aprobar=False, movimiento_bancario_referencia="", comentario="No coincide", usuario=jefe)
    reintegro_registrado.refresh_from_db()
    assert reintegro_registrado.estado == ReintegroCaja.Estado.OBSERVADO
    assert saldo_pendiente(arqueo_con_perdida) == 15000


# ------------------------------------------------------------- por la pantalla

def test_flujo_completo_por_las_vistas(client, arqueo_con_perdida, usuario, jefe):
    client.force_login(usuario)
    pantalla = client.get(reverse("reintegros_caja"))
    assert pantalla.status_code == 200
    assert "Juan Pérez" in pantalla.content.decode()

    resp = client.post(reverse("registrar_reintegro", args=["arqueo", arqueo_con_perdida.pk]), {"monto": "15000"})
    assert resp.status_code == 302
    reintegro = ReintegroCaja.objects.get(arqueo_origen=arqueo_con_perdida)

    client.post(reverse("completar_detalle_reintegro", args=[reintegro.pk]), {
        "fecha_reintegro": "2026-09-15", "banco": "BCI", "cuenta_bancaria": "12345",
        "numero_operacion": "OP-99", "comprobante": _comprobante(), "observaciones": "",
    })
    reintegro.refresh_from_db()
    assert reintegro.estado == ReintegroCaja.Estado.PENDIENTE_VALIDACION

    client.force_login(jefe)
    client.post(reverse("validar_reintegro", args=[reintegro.pk]), {
        "accion": "aprobar", "movimiento_bancario_referencia": "Cartola BCI 16/09", "comentario": "",
    })
    reintegro.refresh_from_db()
    assert reintegro.estado == ReintegroCaja.Estado.VALIDADO


def test_solo_visualizacion_no_puede_registrar_un_reintegro(client, django_user_model, arqueo_con_perdida):
    from django.contrib.auth.models import Group
    from nucleo.documentos_servicio import GRUPO_SOLO_VISUALIZACION

    viewer = django_user_model.objects.create_user(username="viewer", password="x")
    viewer.groups.add(Group.objects.get_or_create(name=GRUPO_SOLO_VISUALIZACION)[0])
    client.force_login(viewer)

    client.post(reverse("registrar_reintegro", args=["arqueo", arqueo_con_perdida.pk]), {"monto": "15000"})
    assert not ReintegroCaja.objects.filter(arqueo_origen=arqueo_con_perdida).exists()
