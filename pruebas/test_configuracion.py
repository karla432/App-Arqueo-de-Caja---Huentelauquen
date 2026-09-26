"""Mantenedores de Configuración (Karla, 2026-09-21): cuentas contables,
cajeras, umbrales y aprobadores — solo la Gerente, con bitácora, y los
umbrales realmente gobiernan las reglas."""
from datetime import date, datetime, time
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from nucleo.configuracion_servicio import (
    ConfiguracionInvalida, actualizar_umbral, asignar_rol, crear_cajera, crear_cuenta, editar_cajera,
    editar_cuenta, puede_configurar,
)
from nucleo.conciliacion_servicio import ejecutar_conciliacion
from nucleo.documentos_servicio import GRUPO_GERENTE_ADM_FINANZAS, GRUPO_JEFE_CONTABILIDAD, nivel_requerido
from nucleo.models import (
    ArqueoEfectivo, Cajera, CuentaContable, DiaArqueo, EventoAuditoria, Excepcion, LineaVenta, LoteCarga,
    MovimientoProveedor, PagoDeclarado, Venta,
)
from nucleo.permisos import GRUPO_ENCARGADO, PermisoDenegado
from nucleo.umbrales import umbral

FECHA = date(2026, 9, 1)


def _usuario_con(django_user_model, nombre, grupo=None, **extra):
    u = django_user_model.objects.create_user(username=nombre, password="x", **extra)
    if grupo:
        u.groups.add(Group.objects.get_or_create(name=grupo)[0])
    return u


@pytest.fixture
def gerente(django_user_model):
    return _usuario_con(django_user_model, "gerente", GRUPO_GERENTE_ADM_FINANZAS)


@pytest.fixture
def otra_gerente(django_user_model):
    return _usuario_con(django_user_model, "gerente2", GRUPO_GERENTE_ADM_FINANZAS)


@pytest.fixture
def jefe(django_user_model):
    return _usuario_con(django_user_model, "jefe", GRUPO_JEFE_CONTABILIDAD)


@pytest.fixture
def analista(django_user_model):
    return _usuario_con(django_user_model, "analista")


# ----------------------------------------------------------------- acceso

def test_solo_la_gerente_puede_configurar(gerente, jefe, analista):
    assert puede_configurar(gerente)
    assert not puede_configurar(jefe)  # ni siendo superusuario técnico
    assert not puede_configurar(analista)


def test_la_pantalla_solo_se_abre_para_la_gerente(client, gerente, jefe, db):
    client.force_login(jefe)
    resp = client.get(reverse("configuracion"))
    assert resp.status_code == 302 and resp.url == reverse("ventas_dashboard")

    client.force_login(gerente)
    resp = client.get(reverse("configuracion"))
    assert resp.status_code == 200
    html = resp.content.decode()
    for seccion in ("Umbrales y tolerancias", "Cuentas contables", "Cajeras", "Usuarios y perfiles"):
        assert seccion in html


def test_un_jefe_no_puede_cambiar_umbrales_por_la_vista(client, jefe, db):
    client.force_login(jefe)
    client.post(reverse("guardar_umbrales"), {"umbral_diferencia_caja": "9999"})
    assert umbral("umbral_diferencia_caja") == 5000


def test_el_menu_muestra_configuracion_solo_a_la_gerente(client, gerente, jefe, db):
    client.force_login(gerente)
    assert reverse("configuracion") in client.get(reverse("ventas_dashboard")).content.decode()
    client.force_login(jefe)
    assert reverse("configuracion") not in client.get(reverse("ventas_dashboard")).content.decode()


# ---------------------------------------------------------------- umbrales

def test_los_umbrales_parten_con_los_valores_acordados(db):
    assert umbral("umbral_diferencia_caja") == 5000
    assert umbral("umbral_visado_perdida") == 50_000
    assert umbral("tolerancia_sencillo") == 10


def test_cambiar_un_umbral_queda_en_bitacora_con_antes_y_despues(gerente):
    actualizar_umbral("umbral_diferencia_caja", "8.000", gerente)
    assert umbral("umbral_diferencia_caja") == 8000
    evento = EventoAuditoria.objects.get(accion="cambiar_umbral")
    assert (evento.entidad_id, evento.valor_anterior, evento.valor_posterior, evento.usuario) == (
        "umbral_diferencia_caja", {"valor": "5000"}, {"valor": "8000"}, gerente,
    )


def test_guardar_el_mismo_valor_no_ensucia_la_bitacora(gerente):
    actualizar_umbral("umbral_diferencia_caja", "5000", gerente)
    assert not EventoAuditoria.objects.filter(accion="cambiar_umbral").exists()


@pytest.mark.parametrize("clave,valor", [
    ("tolerancia_sencillo", "51"),          # sobre el máximo: taparía errores reales
    ("umbral_diferencia_caja", "-1"),
    ("umbral_diferencia_caja", "mucho"),
    ("no_existe", "1"),
])
def test_un_valor_fuera_de_rango_o_invalido_se_rechaza(gerente, clave, valor):
    with pytest.raises(ConfiguracionInvalida):
        actualizar_umbral(clave, valor, gerente)


def test_el_umbral_de_diferencia_de_caja_gobierna_el_estado_del_arqueo(gerente, db):
    dia = DiaArqueo.objects.create(fecha=FECHA)
    arqueo = ArqueoEfectivo.objects.create(dia=dia, caja="CAJA 1", contado=Decimal(0), diferencia=Decimal(6000))
    assert arqueo.estado == "requiere_autorizacion"
    actualizar_umbral("umbral_diferencia_caja", "10000", gerente)
    assert arqueo.estado == "dentro_de_umbral"


def test_ya_no_hay_umbral_de_escalamiento_de_documentos_pendientes(db):
    from nucleo.umbrales import DEFINICIONES
    assert "umbral_escalamiento_documentos" not in DEFINICIONES
    assert nivel_requerido(1) == 1 and nivel_requerido(500) == 1  # el Jefe cierra con cualquier cantidad


@pytest.fixture
def dia_con_diferencia_de_cinco_pesos(db, gerente):
    dia = DiaArqueo.objects.create(fecha=FECHA, operado_por=gerente)
    lote_pos = LoteCarga.objects.create(dia=dia, fuente="pos", nombre_archivo="x", huella_sha256="a",
                                         ruta_almacenamiento="/x", fecha_declarada=FECHA, cargado_por=gerente)
    lote_tbk = LoteCarga.objects.create(dia=dia, fuente="transbank", nombre_archivo="t", huella_sha256="b",
                                         ruta_almacenamiento="/t", fecha_declarada=FECHA, cargado_por=gerente)
    venta = Venta.objects.create(dia=dia, lote=lote_pos, n_interno=1, nro_comprobante="B0000001", tipo_doc="FAC",
                                 nro_comanda="1", estado="COBRADA", caja="CAJA 1", turno="T",
                                 hora_cierre=time(12, 0), total_comanda=10000, cliente="X")
    LineaVenta.objects.create(venta=venta, articulo="X", cantidad=1, precio_unitario=10000, total_linea=10000)
    PagoDeclarado.objects.create(venta=venta, medio="debito", monto=10000)
    MovimientoProveedor.objects.create(
        dia=dia, lote=lote_tbk, proveedor="transbank", tipo="Venta", estado="PENDIENTE",
        fecha_hora=datetime(2026, 9, 1, 12, 1), medio="debito", tipo_tarjeta="debito",
        monto_original=9995, monto_venta_valido_abono=9995, autorizacion="A1", terminal="T1",
    )
    return dia


def test_la_tolerancia_de_sencillo_gobierna_el_calce_automatico(dia_con_diferencia_de_cinco_pesos, gerente):
    dia = dia_con_diferencia_de_cinco_pesos
    ejecutar_conciliacion(dia, gerente)
    assert Excepcion.objects.filter(dia=dia, resolucion="ajuste_sencillo").count() == 1  # $5 ≤ $10 por defecto

    actualizar_umbral("tolerancia_sencillo", "3", gerente)
    ejecutar_conciliacion(dia, gerente)
    assert not Excepcion.objects.filter(dia=dia, resolucion="ajuste_sencillo").exists()
    assert Excepcion.objects.filter(dia=dia, abierta=True, tipo="diferencia_monto").count() == 1


# ----------------------------------------------------------------- cuentas

def test_solo_existen_las_dos_cuentas_fijas_de_perdida_y_ganancia(db):
    # La cuenta de Banco (1101001) es la única excepción — puede haber
    # varias de esas (Karla, 2026-09-22); esta viene sembrada por defecto.
    codigos_perdida_ganancia = set(
        CuentaContable.objects.exclude(tipo="banco").values_list("codigo", flat=True)
    )
    assert codigos_perdida_ganancia == {"6501003", "6501004"}
    assert CuentaContable.objects.get(codigo="6501003").tipo == "ganancia"
    assert CuentaContable.objects.get(codigo="6501004").tipo == "perdida"
    assert CuentaContable.objects.get(codigo="1101001").tipo == "banco"


def test_no_se_puede_crear_una_tercera_cuenta_de_perdida_o_ganancia(gerente):
    with pytest.raises(ConfiguracionInvalida):
        crear_cuenta("6501005", "Otra cuenta", "perdida", gerente)
    with pytest.raises(ConfiguracionInvalida):
        crear_cuenta("6501006", "Otra cuenta", "ganancia", gerente)


def test_se_pueden_crear_varias_cuentas_de_banco(gerente):
    banco = crear_cuenta("1101002", "BCI Cuenta Corriente", "banco", gerente)
    assert (banco.codigo, banco.nombre, banco.tipo, banco.activo) == ("1101002", "BCI Cuenta Corriente", "banco", True)
    assert CuentaContable.objects.filter(tipo="banco").count() == 2  # la sembrada + esta
    with pytest.raises(ConfiguracionInvalida, match="Ya existe"):
        crear_cuenta("1101002", "Otro nombre", "banco", gerente)


def test_no_se_puede_convertir_una_cuenta_de_banco_en_perdida_o_ganancia(gerente):
    banco = crear_cuenta("1101002", "BCI Cuenta Corriente", "banco", gerente)
    with pytest.raises(ConfiguracionInvalida, match="fijos"):
        editar_cuenta(banco, nombre=banco.nombre, tipo="perdida", activo=True, usuario=gerente)


def test_renombrar_una_cuenta_fija_si_funciona_pero_no_desactivarla_ni_cambiarle_el_tipo(gerente):
    cuenta = CuentaContable.objects.get(codigo="6501004")
    editar_cuenta(cuenta, nombre="Pérdida de caja (Odoo)", tipo="perdida", activo=True, usuario=gerente)
    cuenta.refresh_from_db()
    assert cuenta.nombre == "Pérdida de caja (Odoo)"
    assert EventoAuditoria.objects.filter(accion="editar_cuenta").exists()

    with pytest.raises(ConfiguracionInvalida):
        editar_cuenta(cuenta, nombre=cuenta.nombre, tipo=cuenta.tipo, activo=False, usuario=gerente)
    with pytest.raises(ConfiguracionInvalida):
        editar_cuenta(cuenta, nombre=cuenta.nombre, tipo="ganancia", activo=True, usuario=gerente)


# ----------------------------------------------------------------- cajeras

def test_crear_renombrar_y_desactivar_una_cajera(gerente):
    cajera = crear_cajera("  Ana   Soto ", gerente)
    assert cajera.nombre == "Ana Soto"
    editar_cajera(cajera, nombre="Ana Soto Pérez", activo=False, usuario=gerente)
    cajera.refresh_from_db()
    assert (cajera.nombre, cajera.activo) == ("Ana Soto Pérez", False)
    assert Cajera.objects.filter(activo=True, nombre="Ana Soto Pérez").count() == 0


def test_no_se_permiten_cajeras_repetidas_ni_sin_nombre(gerente):
    crear_cajera("Ana Soto", gerente)
    with pytest.raises(ConfiguracionInvalida):
        crear_cajera("ana soto", gerente)
    with pytest.raises(ConfiguracionInvalida):
        crear_cajera("   ", gerente)


# ------------------------------------------------- responsables de traslado

def test_crear_renombrar_y_desactivar_un_responsable_de_traslado(gerente):
    from nucleo.configuracion_servicio import crear_responsable_traslado, editar_responsable_traslado
    from nucleo.models import ResponsableTraslado

    responsable = crear_responsable_traslado("  Osvaldo   Bacho ", gerente)
    assert responsable.nombre == "Osvaldo Bacho"
    editar_responsable_traslado(responsable, nombre="Osvaldo Bacho P.", activo=False, usuario=gerente)
    responsable.refresh_from_db()
    assert (responsable.nombre, responsable.activo) == ("Osvaldo Bacho P.", False)
    assert ResponsableTraslado.objects.filter(activo=True, nombre="Osvaldo Bacho P.").count() == 0


def test_no_se_permiten_responsables_de_traslado_repetidos_ni_sin_nombre(gerente):
    from nucleo.configuracion_servicio import crear_responsable_traslado

    crear_responsable_traslado("Osvaldo Bacho", gerente)
    with pytest.raises(ConfiguracionInvalida):
        crear_responsable_traslado("osvaldo bacho", gerente)
    with pytest.raises(ConfiguracionInvalida):
        crear_responsable_traslado("   ", gerente)


# -------------------------------------------------------------- aprobadores

def test_la_gerente_asigna_roles_y_queda_en_bitacora(gerente, analista):
    asignar_rol(analista, "jefe_contabilidad", gerente)
    assert analista.groups.filter(name=GRUPO_JEFE_CONTABILIDAD).exists()
    asignar_rol(analista, "gerente_adm_finanzas", gerente)
    assert analista.groups.filter(name=GRUPO_GERENTE_ADM_FINANZAS).exists()
    assert not analista.groups.filter(name=GRUPO_JEFE_CONTABILIDAD).exists()
    asignar_rol(analista, "encargada_ingresos", gerente)
    assert [g.name for g in analista.groups.all()] == [GRUPO_ENCARGADO]  # un solo perfil a la vez
    assert EventoAuditoria.objects.filter(accion="cambiar_rol_usuario").count() == 3


def test_nadie_puede_cambiar_su_propio_rol(gerente):
    with pytest.raises(ConfiguracionInvalida):
        asignar_rol(gerente, "encargada_ingresos", gerente)


def test_siempre_debe_quedar_una_gerente(gerente, otra_gerente):
    asignar_rol(otra_gerente, "jefe_contabilidad", gerente)  # hay dos: se puede bajar a una
    with pytest.raises(PermisoDenegado):
        asignar_rol(gerente, "jefe_contabilidad", otra_gerente)  # quien queda como Jefe ya no asigna perfiles
    assert gerente.groups.filter(name=GRUPO_GERENTE_ADM_FINANZAS).exists()


def test_flujo_completo_por_las_vistas(client, gerente, analista):
    client.force_login(gerente)
    client.post(reverse("guardar_umbrales"), {"umbral_visado_perdida": "70.000"})
    cuenta = CuentaContable.objects.get(codigo="6501004")
    client.post(reverse("editar_cuenta", args=[cuenta.pk]), {"nombre": "Faltante de caja", "tipo": "perdida", "activo": "on"})
    client.post(reverse("crear_cajera"), {"nombre": "Rosa Díaz"})
    client.post(reverse("cambiar_rol", args=[analista.pk]), {"rol": "jefe_contabilidad"})

    assert umbral("umbral_visado_perdida") == 70_000
    cuenta.refresh_from_db()
    assert cuenta.nombre == "Faltante de caja"
    assert Cajera.objects.filter(nombre="Rosa Díaz").exists()
    assert analista.groups.filter(name=GRUPO_JEFE_CONTABILIDAD).exists()

    html = client.get(reverse("configuracion")).content.decode()
    assert "cambiar_umbral" in html and "Rosa Díaz" in html


# ---------------------------------- banco ↔ Diario y Método de pago de Odoo (2026-09-24)

def test_cada_banco_guarda_su_diario_y_metodo_de_pago_de_odoo(gerente):
    banco = crear_cuenta("1102001", "Banco Estado 10397-7", "banco", gerente,
                          diario_odoo="Banco Estado", metodo_pago_odoo="Pagos Entrantes (Banco Estado)")
    assert (banco.diario_odoo, banco.metodo_pago_odoo) == ("Banco Estado", "Pagos Entrantes (Banco Estado)")
    editar_cuenta(banco, nombre=banco.nombre, tipo="banco", activo=True, usuario=gerente,
                  diario_odoo="Banco Estado CC", metodo_pago_odoo="Pagos Entrantes (BE CC)")
    banco.refresh_from_db()
    assert banco.diario_odoo == "Banco Estado CC" and banco.metodo_pago_odoo == "Pagos Entrantes (BE CC)"
    evento = EventoAuditoria.objects.filter(accion="editar_cuenta", entidad_id=str(banco.pk)).last()
    assert evento.valor_anterior["diario_odoo"] == "Banco Estado" and evento.valor_posterior["diario_odoo"] == "Banco Estado CC"


def test_editar_una_cuenta_sin_mencionar_diario_no_lo_borra(gerente):
    banco = crear_cuenta("1102001", "Banco X", "banco", gerente, diario_odoo="Diario X", metodo_pago_odoo="Método X")
    editar_cuenta(banco, nombre="Banco X", tipo="banco", activo=False, usuario=gerente)
    banco.refresh_from_db()
    assert banco.diario_odoo == "Diario X" and banco.activo is False


def test_no_se_puede_renombrar_un_banco_que_ya_tiene_depositos_o_reintegros(gerente, django_user_model):
    from nucleo.models import DepositoBancario
    banco = crear_cuenta("1102001", "Banco X", "banco", gerente)
    dia = DiaArqueo.objects.create(fecha=FECHA, operado_por=gerente)
    DepositoBancario.objects.create(dia=dia, monto_esperado=1000, registrado_por=gerente, banco="Banco X")
    with pytest.raises(ConfiguracionInvalida, match="no se puede renombrar"):
        editar_cuenta(banco, nombre="Banco Y", tipo="banco", activo=True, usuario=gerente)
    editar_cuenta(banco, nombre="Banco X", tipo="banco", activo=True, usuario=gerente, diario_odoo="D")  # sí puede editar lo demás


def test_la_pantalla_de_configuracion_muestra_diario_y_metodo_y_marca_lo_que_falta(client, gerente):
    crear_cuenta("1102001", "Banco X", "banco", gerente)
    client.force_login(gerente)
    html = client.get(reverse("configuracion")).content.decode()
    assert 'name="diario_odoo"' in html and 'name="metodo_pago_odoo"' not in html and "falta" in html
    assert "Cuenta Banco" not in html  # ya no hay cuenta de banco fija: sale del banco elegido en cada operación
    resp = client.post(reverse("editar_cuenta", args=[CuentaContable.objects.get(codigo="1102001").pk]), {
        "nombre": "Banco X", "tipo": "banco", "activo": "on", "diario_odoo": "Diario X", "metodo_pago_odoo": "Método X",
    })
    assert resp.status_code == 302
    assert CuentaContable.objects.get(codigo="1102001").diario_odoo == "Diario X"


def test_la_empresa_para_la_captura_de_deposito_se_configura_y_no_pide_cuenta_banco(client, gerente):
    from nucleo.models import ConfiguracionArqueoCaja
    config = ConfiguracionArqueoCaja.vigente()
    assert config.razon_social == "AGRICOLA HACIENDA HUENTELAUQUEN LTDA"
    client.force_login(gerente)
    resp = client.post(reverse("guardar_arqueo_caja"), {
        "diario": config.diario, "codigo_analitico": "31", "razon_social": "OTRA EMPRESA LTDA",
        "cuenta_efectivo_codigo": config.cuenta_efectivo_codigo, "cuenta_efectivo_nombre": config.cuenta_efectivo_nombre,
        "cuenta_transbank_codigo": config.cuenta_transbank_codigo, "cuenta_transbank_nombre": config.cuenta_transbank_nombre,
        "cuenta_mercado_pago_codigo": config.cuenta_mercado_pago_codigo,
        "cuenta_mercado_pago_nombre": config.cuenta_mercado_pago_nombre,
    })
    assert resp.status_code == 302
    assert ConfiguracionArqueoCaja.vigente().razon_social == "OTRA EMPRESA LTDA"
