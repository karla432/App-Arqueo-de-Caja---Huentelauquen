"""Seguridad y perfiles de acceso (Karla, 2026-09-26; a partir del informe «Revisión de seguridad»).

Cada perfil frente a las rutas críticas, acceso directo por URL, autoaprobación, cuentas sin perfil,
/admin/, descargas de los perfiles de solo lectura, GET sin escrituras, bitácora protegida, modo producción
y migración de cuentas. La matriz esperada está escrita AQUÍ a mano a propósito: si alguien cambia la matriz
del código, esta prueba avisa."""
import os
import subprocess
import sys
from datetime import date
from decimal import Decimal
from io import StringIO
from pathlib import Path

import pytest
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, connection, transaction
from django.urls import reverse

from nucleo.cierre_servicio import CierreInvalido, aprobar_y_cerrar_dia, reabrir_dia
from nucleo.models import DiaArqueo, EventoAuditoria, Exportacion, LoteCarga
from nucleo.permisos import (
    PERMISOS_POR_PERFIL, Perfil, Permiso, PermisoDenegado, perfil_de, tiene_permiso,
)
from pruebas.perfiles_de_prueba import (
    GRUPO_ADMIN_TECNICO, GRUPO_ENCARGADO, GRUPO_GERENTE, GRUPO_JEFE, GRUPO_SOLO_LECTURA, GRUPO_SOLO_VISUALIZACION,
    crear_usuario,
)
from pruebas.test_cierre_y_exportacion import caja_cuadrada, carpeta_tmp, dia_listo  # noqa: F401  (escenario listo)

FECHA = date(2026, 9, 2)
RAIZ = Path(__file__).resolve().parent.parent

PERFILES = {
    "gerente": GRUPO_GERENTE, "jefe": GRUPO_JEFE, "encargada": GRUPO_ENCARGADO, "admin_tecnico": GRUPO_ADMIN_TECNICO,
    "solo_lectura": GRUPO_SOLO_LECTURA, "sin_perfil": None,
}

C, D = Permiso.CONSULTAR, Permiso.DESCARGAR_GENERADOS
MATRIZ_ESPERADA = {
    "encargada": {C, D, Permiso.OPERAR},
    "jefe": {
        C, D, Permiso.VALIDAR, Permiso.VISAR, Permiso.APROBAR_CIERRE, Permiso.GENERAR_EXPORTACION,
        Permiso.REABRIR_EXCEPCION, Permiso.CERRAR_MES,
    },
    "gerente": {
        C, D, Permiso.OPERAR, Permiso.VALIDAR, Permiso.VISAR, Permiso.APROBAR_CIERRE, Permiso.GENERAR_EXPORTACION,
        Permiso.AUTORIZAR_EXCEPCION, Permiso.REABRIR_DIA, Permiso.REABRIR_EXCEPCION, Permiso.CONFIGURAR,
        Permiso.ASIGNAR_PERFILES,
    },
    "admin_tecnico": {Permiso.ADMIN_TECNICA},
    "solo_lectura": {C, D},
    "sin_perfil": set(),
}


@pytest.fixture
def usuario(django_user_model):
    return crear_usuario(django_user_model, "operador", GRUPO_GERENTE)


@pytest.fixture
def cuentas(django_user_model):
    return {k: crear_usuario(django_user_model, f"u_{k}", g) for k, g in PERFILES.items()}


def _clave(perfil: Perfil) -> str:
    return perfil.value


# ---------------------------------------------------------------- la matriz

def test_la_matriz_del_codigo_es_la_acordada(cuentas):
    for clave, usuario in cuentas.items():
        for permiso in Permiso:
            assert tiene_permiso(usuario, permiso) == (permiso in MATRIZ_ESPERADA[clave]), (clave, permiso)


def test_ser_superusuario_o_staff_no_da_ningun_permiso_de_la_app(django_user_model):
    u = django_user_model.objects.create_user(username="super", password="x", is_superuser=True, is_staff=True)
    assert perfil_de(u) == Perfil.SIN_PERFIL
    assert not any(tiene_permiso(u, p) for p in Permiso)
    g = crear_usuario(django_user_model, "gerente_super", GRUPO_GERENTE, is_superuser=True, is_staff=True)
    assert tiene_permiso(g, Permiso.APROBAR_CIERRE)  # lo tiene por su perfil, no por el superusuario


def test_los_perfiles_son_excluyentes_y_gana_el_mas_restrictivo(django_user_model):
    u = crear_usuario(django_user_model, "doble", GRUPO_GERENTE)
    u.groups.add(Group.objects.get(name=GRUPO_SOLO_LECTURA))
    assert perfil_de(u) == Perfil.SOLO_LECTURA
    assert not tiene_permiso(u, Permiso.APROBAR_CIERRE)


# ---------------------------------------------------------------- rutas críticas por perfil

# (nombre de la ruta, argumentos, permiso que exige). Todas son POST salvo indicación: con un id inexistente, si el
# perfil PASA el control la vista responde 404/302, nunca 403.
POSTS = [
    ("reconciliar_dia", [FECHA.isoformat()], Permiso.OPERAR),
    ("registrar_conteo", [999], Permiso.OPERAR),
    ("asignar_cajera", [999], Permiso.OPERAR),
    ("registrar_gasto_caja", [999], Permiso.OPERAR),
    ("resolver_excepcion", [999], Permiso.OPERAR),
    ("asignar_factura_venta", [999], Permiso.OPERAR),
    ("registrar_detalle_deposito", [999], Permiso.OPERAR),
    ("generar_captura_deposito", [999], Permiso.OPERAR),
    ("generar_captura_reintegro", [999], Permiso.OPERAR),
    ("registrar_evidencia_nc", [999], Permiso.OPERAR),
    ("validar_deposito", [999], Permiso.VALIDAR),
    ("validar_reintegro", [999], Permiso.VALIDAR),
    ("clasificar_documento", [999], Permiso.VALIDAR),
    ("verificar_nc", [999], Permiso.VALIDAR),
    ("visar_perdida", [999], Permiso.VISAR),
    ("visar_diferencia_caja", [999], Permiso.VISAR),
    ("autorizar_diferencia", [999], Permiso.AUTORIZAR_EXCEPCION),
    ("segunda_firma_nc", [999], Permiso.AUTORIZAR_EXCEPCION),
    ("aprobar_dia", [FECHA.isoformat()], Permiso.APROBAR_CIERRE),
    ("reabrir_dia", [FECHA.isoformat()], Permiso.REABRIR_DIA),
    ("reabrir_excepcion", [999], Permiso.REABRIR_EXCEPCION),
    ("cerrar_mes", ["2026-09"], Permiso.CERRAR_MES),
    ("reabrir_mes", ["2026-09"], Permiso.REABRIR_DIA),
    ("folio_odoo_nc", [999], Permiso.VALIDAR),
    ("aceptar_descuento", [999], Permiso.VALIDAR),
    ("crear_descuento", [], Permiso.CONFIGURAR),
    ("editar_descuento", [999], Permiso.CONFIGURAR),
    ("generar_exportacion", [FECHA.isoformat()], Permiso.GENERAR_EXPORTACION),
    ("guardar_umbrales", [], Permiso.CONFIGURAR),
    ("crear_cajera", [], Permiso.CONFIGURAR),
    ("cambiar_rol", [999], Permiso.ASIGNAR_PERFILES),
]


@pytest.mark.parametrize("nombre,args,permiso", POSTS, ids=[p[0] for p in POSTS])
def test_cada_ruta_de_escritura_respeta_el_perfil(client, cuentas, nombre, args, permiso):
    url = reverse(nombre, args=args)
    for clave, usuario in cuentas.items():
        client.force_login(usuario)
        resp = client.post(url, {})
        if permiso in MATRIZ_ESPERADA[clave]:
            assert resp.status_code != 403, f"{clave} debería pasar el control en {nombre}"
            assert "/sin-perfil/" not in resp.get("Location", "")
        else:
            # 403 para perfiles funcionales, sin perfil y técnico; los de solo lectura se frenan en el middleware (302 sin efecto)
            assert resp.status_code in (403, 302), (clave, nombre, resp.status_code)
            if resp.status_code == 302:
                assert clave == "solo_lectura", (clave, nombre, resp["Location"])


def test_las_pantallas_de_consulta_por_perfil(client, cuentas, dia_listo):
    rutas = [reverse("ventas_dashboard"), reverse("historico"), reverse("control_depositos"),
             reverse("exportaciones_odoo"), reverse("seguimiento"), reverse("consolidado_dias")]
    for clave, usuario in cuentas.items():
        client.force_login(usuario)
        for ruta in rutas:
            resp = client.get(ruta)
            if clave == "sin_perfil":
                assert resp.status_code == 302 and resp["Location"] == reverse("sin_perfil"), (clave, ruta)
            elif clave == "admin_tecnico":
                assert resp.status_code == 302 and resp["Location"] == "/admin/", (clave, ruta)
            else:
                assert resp.status_code == 200, (clave, ruta, resp.status_code)


def test_sin_perfil_no_ve_ningun_dato_financiero(client, cuentas, dia_listo):
    client.force_login(cuentas["sin_perfil"])
    for ruta in (reverse("ventas_dashboard"), reverse("dashboard_dia", args=[FECHA.isoformat()]),
                 reverse("arqueo_efectivo", args=[FECHA.isoformat()]), reverse("historico")):
        resp = client.get(ruta, follow=True)
        html = resp.content.decode()
        assert resp.redirect_chain[-1][0] == reverse("sin_perfil")
        assert "17.600" not in html and "17600" not in html
    assert client.post(reverse("aprobar_dia", args=[FECHA.isoformat()])).status_code == 403


def test_la_pantalla_de_carga_es_solo_para_quien_opera(client, cuentas):
    ruta = reverse("carga_de_archivos", args=[FECHA.isoformat()])
    for clave, usuario in cuentas.items():
        client.force_login(usuario)
        resp = client.get(ruta)
        if Permiso.OPERAR in MATRIZ_ESPERADA[clave]:
            assert resp.status_code == 200, clave
        else:
            assert resp.status_code in (302, 403), clave


# ---------------------------------------------------------------- /admin/

def test_admin_solo_para_el_administrador_tecnico(client, cuentas, django_user_model):
    super_gerente = crear_usuario(django_user_model, "gerente_super", GRUPO_GERENTE, is_superuser=True, is_staff=True)
    super_sin_perfil = django_user_model.objects.create_user(username="super_x", password="x", is_superuser=True, is_staff=True)
    for usuario in (*cuentas.values(), super_gerente, super_sin_perfil):
        if usuario.username == "u_admin_tecnico":
            continue
        client.force_login(usuario)
        assert client.get("/admin/").status_code in (302, 403), usuario.username
        assert client.get("/admin/nucleo/venta/").status_code in (302, 403), usuario.username
    tecnico = cuentas["admin_tecnico"]
    tecnico.is_staff = True
    tecnico.save()
    client.force_login(tecnico)
    assert client.get("/admin/").status_code == 200


def test_el_admin_no_puede_crear_editar_ni_borrar_datos_del_negocio(client, cuentas, dia_listo):
    tecnico = cuentas["admin_tecnico"]
    tecnico.is_staff = True
    tecnico.is_superuser = True
    tecnico.save()
    client.force_login(tecnico)
    assert client.get("/admin/nucleo/venta/").status_code == 200  # puede consultar
    venta = dia_listo.ventas.first()
    for modelo, obj_id in (("venta", venta.pk), ("diaarqueo", dia_listo.pk)):
        assert client.get(f"/admin/nucleo/{modelo}/add/").status_code == 403
        cambio = client.post(f"/admin/nucleo/{modelo}/{obj_id}/change/", {"total_comanda": "1"})
        assert cambio.status_code == 403
        assert client.post(f"/admin/nucleo/{modelo}/{obj_id}/delete/", {"post": "yes"}).status_code == 403
    assert client.get("/admin/nucleo/eventoauditoria/add/").status_code == 403


def test_el_admin_administra_usuarios_y_grupos(client, cuentas):
    tecnico = cuentas["admin_tecnico"]
    tecnico.is_staff = True
    tecnico.is_superuser = True
    tecnico.save()
    client.force_login(tecnico)
    assert client.get("/admin/auth/user/").status_code == 200
    assert client.get("/admin/auth/group/").status_code == 200


def test_el_administrador_tecnico_no_opera_ni_aprueba(client, cuentas, dia_listo):
    tecnico = cuentas["admin_tecnico"]
    tecnico.is_superuser = True
    tecnico.save()
    with pytest.raises(PermisoDenegado):
        aprobar_y_cerrar_dia(dia_listo, tecnico, motivo_excepcion="x")
    client.force_login(tecnico)
    assert client.post(reverse("aprobar_dia", args=[FECHA.isoformat()])).status_code == 403


# ---------------------------------------------------------------- autoaprobación

def test_el_jefe_que_preparo_el_dia_no_lo_aprueba(dia_listo, cuentas, caja_cuadrada):
    jefe = cuentas["jefe"]
    dia_listo.operado_por = jefe
    dia_listo.save()
    with pytest.raises(CierreInvalido, match="no puedes aprobarlo"):
        aprobar_y_cerrar_dia(dia_listo, jefe)
    dia_listo.refresh_from_db()
    assert dia_listo.estado != DiaArqueo.Estado.CERRADO


def test_el_jefe_aprueba_lo_que_preparo_otra_persona(dia_listo, cuentas, caja_cuadrada):
    dia_listo.operado_por = cuentas["encargada"]
    dia_listo.save()
    LoteCarga.objects.filter(dia=dia_listo).update(cargado_por=cuentas["encargada"])
    aprobar_y_cerrar_dia(dia_listo, cuentas["jefe"])
    dia_listo.refresh_from_db()
    assert dia_listo.estado == DiaArqueo.Estado.CERRADO and dia_listo.aprobado_por == cuentas["jefe"]


def test_la_gerente_que_preparo_el_dia_solo_aprueba_con_motivo_y_queda_registrado(dia_listo, usuario, caja_cuadrada):
    with pytest.raises(CierreInvalido, match="motivo"):
        aprobar_y_cerrar_dia(dia_listo, usuario)
    with pytest.raises(CierreInvalido, match="motivo"):
        aprobar_y_cerrar_dia(dia_listo, usuario, motivo_excepcion="   ")
    aprobar_y_cerrar_dia(dia_listo, usuario, motivo_excepcion="La Encargada estaba de licencia")
    e = EventoAuditoria.objects.get(accion="excepcion_de_control:aprobador_igual_operador")
    assert e.valor_posterior["motivo"] == "La Encargada estaba de licencia" and e.usuario == usuario


def test_la_encargada_no_aprueba_ni_reabre_ni_exporta(dia_listo, cuentas, caja_cuadrada):
    enc = cuentas["encargada"]
    with pytest.raises(PermisoDenegado):
        aprobar_y_cerrar_dia(dia_listo, enc, motivo_excepcion="x")
    with pytest.raises(PermisoDenegado):
        reabrir_dia(dia_listo, enc, motivo="x")


def test_solo_la_gerente_reabre_y_queda_con_motivo(dia_listo, cuentas, usuario, caja_cuadrada):
    aprobar_y_cerrar_dia(dia_listo, usuario, motivo_excepcion="prueba")
    with pytest.raises(PermisoDenegado):
        reabrir_dia(dia_listo, cuentas["jefe"], motivo="quiero")
    with pytest.raises(CierreInvalido):
        reabrir_dia(dia_listo, usuario, motivo=" ")
    reabrir_dia(dia_listo, usuario, motivo="Corregir una línea")
    e = EventoAuditoria.objects.filter(accion="reabrir").get()
    assert e.valor_posterior["motivo"] == "Corregir una línea" and e.usuario == usuario


def test_los_servicios_tambien_exigen_el_perfil_no_solo_las_vistas(dia_listo, cuentas, caja_cuadrada):
    """Un superusuario sin perfil que llama al servicio directamente tampoco puede."""
    from django.contrib.auth import get_user_model
    s = get_user_model().objects.create_user(username="s", password="x", is_superuser=True, is_staff=True)
    with pytest.raises(PermisoDenegado):
        aprobar_y_cerrar_dia(dia_listo, s, motivo_excepcion="x")
    with pytest.raises(PermisoDenegado):
        reabrir_dia(dia_listo, s, motivo="x")


# ---------------------------------------------------------------- solo lectura: descargas y GET sin escrituras

class _Escrituras:
    def __init__(self):
        self.sql = []

    def __call__(self, execute, sql, params, many, context):
        s = sql.lstrip().upper()
        if s.startswith(("INSERT", "UPDATE", "DELETE", "REPLACE")):
            self.sql.append(sql)
        return execute(sql, params, many, context)

    def tablas(self):
        import re
        out = set()
        for s in self.sql:
            m = re.search(r'(?:INTO|UPDATE|FROM)\s+"?(\w+)"?', s, re.I)
            out.add(m.group(1) if m else s[:40])
        return out


@pytest.fixture
def dia_exportado(dia_listo, caja_cuadrada, usuario, carpeta_tmp):
    from nucleo.exportacion_servicio import generar_exportacion
    aprobar_y_cerrar_dia(dia_listo, usuario, motivo_excepcion="prueba")
    generar_exportacion(dia_listo, usuario)
    return dia_listo


GETS_DE_SOLO_LECTURA = [
    ("ventas_dashboard", []), ("historico", []), ("consolidado_dias", []), ("seguimiento", []), ("calidad_registro", []),
    ("control_depositos", []), ("regularizacion_documentos", []), ("reintegros_caja", []), ("exportaciones_odoo", []),
    ("reporte_ventas_producto", []), ("desempeno_cajeras", []), ("dashboard_dia", [FECHA.isoformat()]), ("arqueo_efectivo", [FECHA.isoformat()]),
    ("bandeja_excepciones", [FECHA.isoformat()]), ("cierre_y_exportacion", [FECHA.isoformat()]),
    ("auditoria_reclasificaciones", [FECHA.isoformat()]),
]


@pytest.mark.parametrize("clave", ["solo_lectura"])
def test_ningun_get_de_un_perfil_de_lectura_escribe_datos(client, cuentas, dia_listo, clave):
    """Ni siquiera el arqueo de un día sin arqueos guardados: se calcula en memoria, no se persiste."""
    client.force_login(cuentas[clave])
    espia = _Escrituras()
    with connection.execute_wrapper(espia):
        for nombre, args in GETS_DE_SOLO_LECTURA:
            resp = client.get(reverse(nombre, args=args))
            assert resp.status_code in (200, 302), (nombre, resp.status_code)
    assert espia.sql == [], espia.sql


@pytest.mark.parametrize("clave", ["solo_lectura"])
def test_los_perfiles_de_lectura_no_pueden_escribir_por_ninguna_ruta(client, cuentas, dia_listo, clave):
    client.force_login(cuentas[clave])
    espia = _Escrituras()
    with connection.execute_wrapper(espia):
        for nombre, args, _permiso in POSTS:
            resp = client.post(reverse(nombre, args=args), {})
            assert resp.status_code in (302, 403), nombre
    assert espia.sql == []


@pytest.mark.parametrize("clave", ["solo_lectura"])
def test_lectura_descarga_solo_archivos_generados_y_autorizados(client, cuentas, dia_exportado, clave):
    exp = Exportacion.objects.get(dia=dia_exportado)
    client.force_login(cuentas[clave])
    assert client.get(reverse("descargar_exportacion", args=[exp.pk])).status_code == 200
    # nada de archivos originales cargados (pueden traer datos que no están autorizados para consulta)
    lote = LoteCarga.objects.filter(dia=dia_exportado).first()
    assert client.get(reverse("descargar_archivo_original", args=[lote.pk])).status_code in (302, 403)
    # una versión invalidada ya no se ofrece
    Exportacion.objects.filter(pk=exp.pk).update(invalidada=True)
    resp = client.get(reverse("descargar_exportacion", args=[exp.pk]))
    assert resp.status_code != 200


def test_descargar_no_genera_archivos_ni_cambia_fechas(client, cuentas, dia_exportado, usuario):
    """GET de una captura no generada: no se genera; redirige avisando."""
    from nucleo.models import DepositoBancario
    dep = DepositoBancario.objects.filter(dia=dia_exportado).first()
    assert dep is not None
    client.force_login(cuentas["solo_lectura"])
    espia = _Escrituras()
    with connection.execute_wrapper(espia):
        resp = client.get(reverse("captura_deposito", args=[dep.pk]))
    assert resp.status_code == 302
    assert espia.sql == []  # ni siquiera evento: no hubo descarga


def test_cada_descarga_queda_en_la_bitacora_con_quien_y_su_perfil(client, cuentas, dia_exportado):
    exp = Exportacion.objects.get(dia=dia_exportado)
    client.force_login(cuentas["solo_lectura"])
    client.get(reverse("descargar_exportacion", args=[exp.pk]))
    e = EventoAuditoria.objects.filter(accion="descargar_archivo").latest("id")
    assert e.usuario == cuentas["solo_lectura"]
    assert e.valor_posterior.get("perfil") == Perfil.SOLO_LECTURA.value


# ---------------------------------------------------------------- bitácora de accesos y decisiones

def test_inicio_cierre_y_fallo_de_sesion_quedan_registrados(client, cuentas):
    usuario = cuentas["gerente"]
    client.post(reverse("login"), {"username": usuario.username, "password": "mala"})
    assert EventoAuditoria.objects.filter(accion="inicio_sesion_fallido", usuario=usuario).exists()
    assert client.login(username=usuario.username, password="x")
    assert EventoAuditoria.objects.filter(accion="iniciar_sesion", usuario=usuario).exists()
    client.post(reverse("logout"))
    assert EventoAuditoria.objects.filter(accion="cerrar_sesion", usuario=usuario).exists()


def test_cambios_de_perfil_y_de_privilegios_quedan_en_la_bitacora(django_user_model):
    u = django_user_model.objects.create_user(username="nuevo", password="x")
    u.groups.add(Group.objects.get(name=GRUPO_JEFE))
    assert EventoAuditoria.objects.filter(accion="cambiar_grupos_usuario").exists()
    u.is_superuser = True
    u.save()
    assert EventoAuditoria.objects.filter(accion="cambiar_privilegios_usuario").exists()


def test_aprobar_deja_perfil_y_quien(dia_listo, cuentas, caja_cuadrada):
    dia_listo.operado_por = cuentas["encargada"]
    dia_listo.save()
    LoteCarga.objects.filter(dia=dia_listo).update(cargado_por=cuentas["encargada"])
    aprobar_y_cerrar_dia(dia_listo, cuentas["jefe"])
    e = EventoAuditoria.objects.get(accion="aprobar_y_cerrar")
    assert e.usuario == cuentas["jefe"] and e.valor_posterior["perfil"] == Perfil.JEFE.value


# ---------------------------------------------------------------- asignación de perfiles

def test_solo_la_gerente_asigna_perfiles_y_nunca_el_administrador_tecnico(cuentas):
    from nucleo.configuracion_servicio import ConfiguracionInvalida, asignar_rol
    objetivo = cuentas["sin_perfil"]
    for clave in ("jefe", "encargada", "admin_tecnico", "solo_lectura", "sin_perfil"):
        with pytest.raises(PermisoDenegado):
            asignar_rol(objetivo, "jefe_contabilidad", cuentas[clave])
    with pytest.raises(ConfiguracionInvalida):
        asignar_rol(objetivo, "administrador_tecnico", cuentas["gerente"])
    asignar_rol(objetivo, "solo_lectura", cuentas["gerente"])
    assert perfil_de(objetivo) == Perfil.SOLO_LECTURA


def test_los_roles_asignables_incluyen_solo_lectura_y_no_al_tecnico():
    from nucleo.configuracion_servicio import ROLES_ASIGNABLES
    claves = {r[0] if isinstance(r, (tuple, list)) else r for r in (ROLES_ASIGNABLES.items() if hasattr(ROLES_ASIGNABLES, "items") else ROLES_ASIGNABLES)}
    assert Perfil.SOLO_LECTURA.value in claves and Perfil.ADMIN_TECNICO.value not in claves


# ---------------------------------------------------------------- bitácora protegida

def test_la_bitacora_no_se_borra_ni_se_edita_ni_por_sql_directo(usuario):
    e = EventoAuditoria.objects.create(dia=None, entidad="X", entidad_id="1", accion="prueba", usuario=usuario)
    with pytest.raises(Exception):
        e.delete()
    with pytest.raises(DatabaseError), transaction.atomic():
        EventoAuditoria.objects.filter(pk=e.pk).delete()
    with pytest.raises(DatabaseError), transaction.atomic():
        EventoAuditoria.objects.filter(pk=e.pk).update(accion="otra")
    if connection.vendor == "sqlite":
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as c:
            c.execute("DELETE FROM nucleo_eventoauditoria")
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as c:
            c.execute("UPDATE nucleo_eventoauditoria SET accion='x'")
    assert EventoAuditoria.objects.filter(pk=e.pk, accion="prueba").exists()


def test_verificar_bitacora_detecta_una_alteracion_por_debajo(usuario):
    if connection.vendor != "sqlite":
        pytest.skip("solo SQLite")
    EventoAuditoria.objects.create(dia=None, entidad="X", entidad_id="1", accion="uno", usuario=usuario)
    e2 = EventoAuditoria.objects.create(dia=None, entidad="X", entidad_id="2", accion="dos", usuario=usuario)
    salida = StringIO()
    call_command("verificar_bitacora", stdout=salida)
    assert "íntegra" in salida.getvalue()
    with connection.cursor() as c:  # quien tiene la base en la mano quita el trigger y edita
        c.execute("DROP TRIGGER bitacora_sin_edicion")
        c.execute("UPDATE nucleo_eventoauditoria SET accion='falsificada' WHERE id=%s", [e2.pk])
    with pytest.raises(SystemExit):
        call_command("verificar_bitacora", stdout=StringIO(), stderr=StringIO())


# ---------------------------------------------------------------- producción

def _manage(entorno, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DJANGO_", "ARQUEO_"))}
    env.update(entorno)
    return subprocess.run(
        [sys.executable, "manage.py", *args], cwd=RAIZ, env=env, capture_output=True, text=True, timeout=120,
    )


def test_produccion_exige_clave_y_hosts_por_entorno():
    sin_nada = _manage({"ARQUEO_ENTORNO": "produccion"}, "check")
    assert sin_nada.returncode != 0 and "DJANGO_SECRET_KEY" in sin_nada.stderr
    sin_hosts = _manage({"ARQUEO_ENTORNO": "produccion", "DJANGO_SECRET_KEY": "x" * 60}, "check")
    assert sin_hosts.returncode != 0 and "DJANGO_ALLOWED_HOSTS" in sin_hosts.stderr


def test_produccion_pasa_check_deploy_con_valores_de_ejemplo():
    r = _manage(
        {"ARQUEO_ENTORNO": "produccion", "DJANGO_SECRET_KEY": "k" * 12 + "!Zx9" * 12, "DJANGO_ALLOWED_HOSTS": "arqueo.ejemplo.cl",
         "DJANGO_PROXY_SSL": "1"},
        "check", "--deploy", "--fail-level", "ERROR",
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_produccion_apaga_debug_y_activa_cookies_seguras():
    codigo = (
        "import django,os;os.environ['DJANGO_SETTINGS_MODULE']='config.settings';django.setup();"
        "from django.conf import settings as s;"
        "print(s.DEBUG,s.SESSION_COOKIE_SECURE,s.CSRF_COOKIE_SECURE,s.SECURE_SSL_REDIRECT,s.SECURE_HSTS_SECONDS>0,s.ALLOWED_HOSTS)"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DJANGO_", "ARQUEO_"))}
    env.update({"ARQUEO_ENTORNO": "produccion", "DJANGO_SECRET_KEY": "k" * 60, "DJANGO_ALLOWED_HOSTS": "arqueo.ejemplo.cl"})
    r = subprocess.run([sys.executable, "-c", codigo], cwd=RAIZ, env=env, capture_output=True, text=True, timeout=120)
    assert r.stdout.strip() == "False True True True True ['arqueo.ejemplo.cl']", r.stdout + r.stderr


def test_la_clave_secreta_no_esta_en_el_codigo_versionado():
    texto = (RAIZ / "config" / "settings.py").read_text(encoding="utf-8")
    assert "django-insecure" not in texto
    assert ".clave_secreta_local" in (RAIZ / ".gitignore").read_text(encoding="utf-8")


# ---------------------------------------------------------------- migración de cuentas y recuperación

def test_migrar_perfiles_informa_sin_cambiar_nada(django_user_model):
    k = crear_usuario(django_user_model, "karla_t", GRUPO_GERENTE, is_superuser=True, is_staff=True)
    j = crear_usuario(django_user_model, "jefe_t", GRUPO_JEFE, is_superuser=True, is_staff=True)
    salida = StringIO()
    call_command("migrar_perfiles", stdout=salida)
    texto = salida.getvalue()
    assert "karla_t" in texto and "jefe_t" in texto and "NINGUNO" in texto
    k.refresh_from_db(); j.refresh_from_db()
    assert k.is_superuser and j.is_superuser and k.is_staff  # informe solamente


def test_migrar_perfiles_se_niega_sin_administrador_tecnico_listo(django_user_model):
    k = crear_usuario(django_user_model, "karla_t", GRUPO_GERENTE, is_superuser=True, is_staff=True)
    with pytest.raises(CommandError, match="Administrador técnico"):
        call_command("migrar_perfiles", "--aplicar", stdout=StringIO())
    k.refresh_from_db()
    assert k.is_superuser


def test_migrar_perfiles_retira_privilegios_sin_borrar_nada(django_user_model, dia_listo):
    k = crear_usuario(django_user_model, "karla_t", GRUPO_GERENTE, is_superuser=True, is_staff=True)
    j = crear_usuario(django_user_model, "jefe_t", GRUPO_JEFE, is_superuser=True, is_staff=True)
    sin_grupo = django_user_model.objects.create_user(username="sin_grupo", password="x", is_superuser=True)
    crear_usuario(django_user_model, "tecnico_t", GRUPO_ADMIN_TECNICO, is_staff=True, is_superuser=True)  # con contraseña
    usuarios, dias, eventos = django_user_model.objects.count(), DiaArqueo.objects.count(), EventoAuditoria.objects.count()
    call_command("migrar_perfiles", "--aplicar", stdout=StringIO())
    for u in (k, j):
        u.refresh_from_db()
        assert not u.is_superuser and not u.is_staff
    assert perfil_de(k) == Perfil.GERENTE and perfil_de(j) == Perfil.JEFE
    sin_grupo.refresh_from_db()
    assert sin_grupo.is_superuser and perfil_de(sin_grupo) == Perfil.SIN_PERFIL  # no se le inventa un perfil
    assert django_user_model.objects.count() == usuarios and DiaArqueo.objects.count() == dias
    assert EventoAuditoria.objects.count() >= eventos  # la bitácora solo crece
    assert EventoAuditoria.objects.filter(accion="cambiar_privilegios_usuario").exists()


def test_crear_admin_tecnico_no_pone_contrasena_y_no_tiene_facultades_financieras(django_user_model, monkeypatch):
    call_command("crear_admin_tecnico", "tec", stdout=StringIO())
    t = django_user_model.objects.get(username="tec")
    assert not t.has_usable_password() and perfil_de(t) == Perfil.ADMIN_TECNICO
    assert not tiene_permiso(t, Permiso.APROBAR_CIERRE) and not tiene_permiso(t, Permiso.CONSULTAR)
    monkeypatch.setenv("CLAVE_DE_PRUEBA_TEC", "una-clave-de-prueba-larga-99")
    call_command("crear_admin_tecnico", "tec", "--password-desde-entorno", "CLAVE_DE_PRUEBA_TEC", stdout=StringIO())
    t.refresh_from_db()
    assert t.has_usable_password()


def test_recuperar_acceso_reactiva_restablece_y_deja_evento(django_user_model, monkeypatch):
    u = crear_usuario(django_user_model, "olvidadiza", GRUPO_GERENTE)
    u.is_active = False
    u.save()
    monkeypatch.setenv("CLAVE_NUEVA_PRUEBA", "otra-clave-de-prueba-88")
    call_command("recuperar_acceso", "olvidadiza", "--reactivar", "--password-desde-entorno", "CLAVE_NUEVA_PRUEBA", stdout=StringIO())
    u.refresh_from_db()
    assert u.is_active and u.check_password("otra-clave-de-prueba-88") and perfil_de(u) == Perfil.GERENTE
    assert EventoAuditoria.objects.filter(accion="recuperar_acceso_por_consola").exists()


def test_reiniciar_datos_de_prueba_se_niega_sin_confirmacion():
    with pytest.raises(CommandError):
        call_command("reiniciar_datos_de_prueba", stdout=StringIO())


# ---------------------------------------------------------------- los servicios no dependen de la vista

def test_servicios_de_configuracion_y_operacion_rechazan_perfiles_sin_permiso(dia_listo, cuentas):
    from nucleo.configuracion_servicio import actualizar_umbral, crear_cajera
    from nucleo.depositos_servicio import agregar_deposito_adicional
    from nucleo.models import Cajera
    for clave in ("jefe", "encargada", "solo_lectura", "sin_perfil", "admin_tecnico"):
        with pytest.raises(PermisoDenegado):
            crear_cajera("Intrusa", cuentas[clave])
        with pytest.raises(PermisoDenegado):
            actualizar_umbral("umbral_diferencia_caja", "1", cuentas[clave])
    assert not Cajera.objects.filter(nombre="Intrusa").exists()
    for clave in ("jefe", "solo_lectura", "sin_perfil", "admin_tecnico"):
        with pytest.raises(PermisoDenegado):
            agregar_deposito_adicional(dia_listo, monto_esperado=Decimal(1000), usuario=cuentas[clave])


# ---------------------------------------------------------------- decisiones de Karla (2026-09-26)

def test_solo_visualizacion_fue_retirado_y_quien_lo_tenia_funciona_como_solo_lectura(django_user_model, client, dia_listo):
    u = crear_usuario(django_user_model, "antiguo", GRUPO_SOLO_VISUALIZACION)
    assert perfil_de(u) == Perfil.SOLO_LECTURA
    assert not tiene_permiso(u, Permiso.OPERAR) and tiene_permiso(u, Permiso.CONSULTAR)
    client.force_login(u)
    assert client.post(reverse("aprobar_dia", args=[FECHA.isoformat()])).status_code == 302  # frenado, sin efecto
    from nucleo.configuracion_servicio import ROLES_ASIGNABLES
    assert "solo_visualizacion" not in ROLES_ASIGNABLES


def test_reasignar_perfil_quita_tambien_el_grupo_antiguo(django_user_model, cuentas):
    from nucleo.configuracion_servicio import asignar_rol
    u = crear_usuario(django_user_model, "antiguo2", GRUPO_SOLO_VISUALIZACION)
    asignar_rol(u, "jefe_contabilidad", cuentas["gerente"])
    assert [g.name for g in u.groups.all()] == [GRUPO_JEFE]


def _arqueo_con_diferencia(dia, contador):
    from nucleo.arqueo_servicio import registrar_conteo
    from nucleo.models import ArqueoEfectivo
    a, _ = ArqueoEfectivo.objects.get_or_create(dia=dia, caja="CAJA 4")
    registrar_conteo(a, fondo_inicial=Decimal(0), gastos_autorizados=Decimal(0), total_contado=Decimal(200000), usuario=contador)
    a.refresh_from_db()
    return a


def test_el_visado_no_lo_hace_quien_preparo_el_caso(dia_listo, cuentas, usuario):
    from nucleo.perdidas_servicio import VisadoInvalido, visar_diferencia_caja
    a = _arqueo_con_diferencia(dia_listo, usuario)  # la Gerente contó
    assert abs(a.diferencia) > 0
    jefe = cuentas["jefe"]
    visar_diferencia_caja(a, jefe)  # otra persona: puede
    a.visado_por = None
    a.save()
    with pytest.raises(VisadoInvalido, match="motivo"):
        visar_diferencia_caja(a, usuario)  # la Gerente que contó: solo con motivo
    visar_diferencia_caja(a, usuario, motivo_excepcion="La Encargada no estaba")
    e = EventoAuditoria.objects.get(accion="excepcion_de_control:visado_igual_preparador")
    assert e.valor_posterior["motivo"] == "La Encargada no estaba"


def test_un_jefe_que_preparo_no_puede_visar_ni_con_motivo(dia_listo, cuentas):
    from nucleo.perdidas_servicio import VisadoInvalido, visar_diferencia_caja
    jefe = cuentas["jefe"]
    a = _arqueo_con_diferencia(dia_listo, cuentas["gerente"])
    EventoAuditoria.objects.create(  # el Jefe figura como quien contó (p. ej. perfil anterior)
        dia=dia_listo, entidad="ArqueoEfectivo", entidad_id=str(a.pk), accion="registrar_conteo", usuario=jefe,
    )
    with pytest.raises(VisadoInvalido, match="no puedes visarlo"):
        visar_diferencia_caja(a, jefe, motivo_excepcion="igual")


def test_por_defecto_el_entorno_es_produccion(tmp_path):
    from config.entorno import MARCA_LOCAL, resolver_entorno
    assert resolver_entorno(None, tmp_path) == "produccion"
    assert resolver_entorno("  ", tmp_path) == "produccion"
    assert resolver_entorno("desarrollo", tmp_path) == "desarrollo"
    assert resolver_entorno("PRODUCCION", tmp_path) == "produccion"
    (tmp_path / MARCA_LOCAL).write_text("")
    assert resolver_entorno(None, tmp_path) == "desarrollo"
    assert resolver_entorno("produccion", tmp_path) == "produccion"  # la variable manda sobre la marca


def test_la_marca_local_de_desarrollo_no_se_versiona():
    assert ".entorno_desarrollo" in (RAIZ / ".gitignore").read_text(encoding="utf-8")


def test_sin_variables_ni_marca_la_app_no_arranca_en_produccion_sin_clave(tmp_path):
    """Copia mínima del proyecto sin la marca local: debe exigir la clave (producción por defecto)."""
    import shutil
    destino = tmp_path / "copia"
    shutil.copytree(RAIZ / "config", destino / "config")
    shutil.copytree(RAIZ / "nucleo", destino / "nucleo", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(RAIZ / "manage.py", destino / "manage.py")
    r = subprocess.run([sys.executable, "manage.py", "check"], cwd=destino,
                       env={k: v for k, v in os.environ.items() if not k.startswith(("DJANGO_", "ARQUEO_"))},
                       capture_output=True, text=True, timeout=120)
    assert r.returncode != 0 and "DJANGO_SECRET_KEY" in r.stderr


# ---------------------------------------------------------------- bloqueo por intentos fallidos (Karla, 2026-09-26)

def _intentar(client, usuario, clave):
    return client.post(reverse("login"), {"username": usuario, "password": clave})


def _fallar(client, usuario, veces):
    for _ in range(veces):
        _intentar(client, usuario, "clave-equivocada")


def test_cinco_fallos_bloquean_la_cuenta_ni_con_la_clave_correcta(client, cuentas):
    from nucleo import bloqueo_acceso
    nombre = cuentas["gerente"].username
    _fallar(client, nombre, bloqueo_acceso.MAX_INTENTOS - 1)
    assert _intentar(client, nombre, "x").status_code == 302  # el cuarto fallo aún no bloquea: entra con la clave buena
    client.logout()
    _fallar(client, nombre, bloqueo_acceso.MAX_INTENTOS)
    resp = _intentar(client, nombre, "x")  # la clave correcta ("x" en las pruebas)
    assert resp.status_code == 200 and "bloqueada" in resp.content.decode()
    assert "_auth_user_id" not in client.session
    assert bloqueo_acceso.minutos_restantes(nombre) > 0
    assert EventoAuditoria.objects.filter(accion="cuenta_bloqueada_por_intentos", usuario=cuentas["gerente"]).count() == 1


def test_el_bloqueo_se_levanta_solo_pasados_los_minutos(client, cuentas):
    from datetime import timedelta
    from django.utils import timezone
    from nucleo import bloqueo_acceso
    from nucleo.models import IntentoFallido
    nombre = cuentas["jefe"].username
    _fallar(client, nombre, bloqueo_acceso.MAX_INTENTOS)
    assert bloqueo_acceso.esta_bloqueado(nombre)
    IntentoFallido.objects.filter(nombre=nombre.lower()).update(
        momento=timezone.now() - timedelta(minutes=bloqueo_acceso.MINUTOS_DE_BLOQUEO + 1),
    )
    assert not bloqueo_acceso.esta_bloqueado(nombre)
    assert _intentar(client, nombre, "x").status_code == 302


def test_intentar_durante_el_bloqueo_no_lo_alarga_y_un_ingreso_bueno_limpia_los_fallos(client, cuentas):
    from nucleo import bloqueo_acceso
    from nucleo.models import IntentoFallido
    nombre = cuentas["jefe"].username
    _fallar(client, nombre, bloqueo_acceso.MAX_INTENTOS + 3)
    assert IntentoFallido.objects.filter(nombre=nombre.lower()).count() == bloqueo_acceso.MAX_INTENTOS
    otra = cuentas["encargada"].username
    _fallar(client, otra, 2)
    assert _intentar(client, otra, "x").status_code == 302
    assert not IntentoFallido.objects.filter(nombre=otra.lower()).exists()


def test_el_bloqueo_no_distingue_mayusculas_ni_revela_si_el_usuario_existe(client, cuentas):
    from nucleo import bloqueo_acceso
    _fallar(client, "Fantasma", bloqueo_acceso.MAX_INTENTOS)
    assert bloqueo_acceso.esta_bloqueado("fantasma") and bloqueo_acceso.esta_bloqueado("FANTASMA")
    assert "bloqueada" in _intentar(client, "fantasma", "cualquiera").content.decode()  # mismo trato que una cuenta real
    assert not EventoAuditoria.objects.filter(accion="cuenta_bloqueada_por_intentos", valor_posterior__usuario="fantasma").exists()
    real = cuentas["gerente"].username
    _fallar(client, real.upper(), bloqueo_acceso.MAX_INTENTOS)
    assert bloqueo_acceso.esta_bloqueado(real)


def test_el_bloqueo_tambien_vale_para_el_login_de_admin(client, cuentas):
    from nucleo import bloqueo_acceso
    tecnico = cuentas["admin_tecnico"]
    tecnico.is_staff = True
    tecnico.save()
    for _ in range(bloqueo_acceso.MAX_INTENTOS):
        client.post("/admin/login/", {"username": tecnico.username, "password": "mala", "next": "/admin/"})
    client.post("/admin/login/", {"username": tecnico.username, "password": "x", "next": "/admin/"})
    assert client.get("/admin/").status_code in (302, 403)  # no quedó dentro


def test_recuperar_acceso_levanta_el_bloqueo(client, cuentas, monkeypatch):
    from nucleo import bloqueo_acceso
    nombre = cuentas["gerente"].username
    _fallar(client, nombre, bloqueo_acceso.MAX_INTENTOS)
    assert bloqueo_acceso.esta_bloqueado(nombre)
    monkeypatch.setenv("CLAVE_TMP_PRUEBA", "clave-nueva-de-prueba-77")
    call_command("recuperar_acceso", nombre, "--password-desde-entorno", "CLAVE_TMP_PRUEBA", stdout=StringIO())
    assert not bloqueo_acceso.esta_bloqueado(nombre)
    assert _intentar(client, nombre, "clave-nueva-de-prueba-77").status_code == 302


# ---------------------------------------------------------------- auditoría de TODAS las rutas (Bloque D)

# Rutas que a propósito no piden un permiso del negocio: solo sesión (o ni eso, como el ingreso).
RUTAS_SIN_PERMISO_DEL_NEGOCIO = {
    "login", "logout", "sin_perfil", "verificacion_dos_pasos", "activar_dos_pasos",
}


def _rutas_de_la_app():
    from django.urls import URLPattern, URLResolver, get_resolver

    def recorrer(patrones, prefijo=""):
        for p in patrones:
            if isinstance(p, URLResolver):
                yield from recorrer(p.url_patterns, prefijo + str(p.pattern))
            elif isinstance(p, URLPattern):
                yield prefijo + str(p.pattern), p.name, p.callback
    return list(recorrer(get_resolver().url_patterns))


def test_toda_ruta_de_la_app_exige_un_permiso_o_esta_en_la_lista_de_excepciones():
    """Si alguien agrega una vista nueva sin `@requiere(...)`, esta prueba falla: ninguna ruta queda abierta por olvido."""
    sin_control = []
    for ruta, nombre, vista in _rutas_de_la_app():
        if ruta.startswith("admin/"):
            continue  # el sitio de administración lo cubre el perfil Administrador técnico (has_permission)
        if getattr(vista, "permiso_requerido", None) is None and nombre not in RUTAS_SIN_PERMISO_DEL_NEGOCIO:
            sin_control.append((ruta, nombre))
    assert sin_control == [], f"Rutas sin control de permiso: {sin_control}"


def test_las_rutas_marcadas_como_lectura_no_exigen_mas_que_consultar_o_descargar():
    """Las rutas que un perfil de solo lectura puede abrir (GET) son solo de consulta o de descarga de lo ya generado."""
    de_lectura = {Permiso.CONSULTAR, Permiso.DESCARGAR_GENERADOS}
    for ruta, nombre, vista in _rutas_de_la_app():
        permiso = getattr(vista, "permiso_requerido", None)
        if permiso in de_lectura:
            for prohibido in ("guardar", "registrar", "aprobar", "reabrir", "validar", "visar", "generar", "cambiar", "crear", "editar", "asignar"):
                assert prohibido not in (nombre or "") or nombre in ("historico",), (nombre, permiso)


@pytest.mark.parametrize("nombre,args,permiso", POSTS, ids=[p[0] for p in POSTS])
def test_las_rutas_que_modifican_datos_rechazan_el_get(client, cuentas, nombre, args, permiso):
    """Ninguna ruta de escritura acepta GET, ni con el perfil que sí puede escribir: un enlace o una imagen en un
    correo no puede provocar un cambio."""
    client.force_login(cuentas["gerente"])
    assert client.get(reverse(nombre, args=args)).status_code == 405
