"""Pantalla de Configuración (Karla, 2026-09-21): cuentas contables, cajeras,
umbrales y aprobadores. Solo la Gerente de Administración y Finanzas."""
from functools import wraps

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from nucleo.configuracion_servicio import (
    ROLES, ConfiguracionInvalida, actualizar_config_arqueo_caja, actualizar_umbral, asignar_rol,
    config_arqueo_caja, crear_cajera, crear_cuenta, crear_responsable_traslado, cuentas_banco, editar_cajera,
    editar_cuenta, editar_responsable_traslado, puede_configurar, usuarios_con_rol, ROLES_ASIGNABLES,
)
from nucleo.models import Cajera, CuentaContable, EventoAuditoria, ResponsableTraslado
from nucleo.umbrales import DEFINICIONES, umbrales_vigentes

ENTIDADES_DE_CONFIGURACION = ["Parametro", "CuentaContable", "Cajera", "ResponsableTraslado", "Usuario"]
from nucleo.permisos import Permiso, PermisoDenegado


def solo_gerente(view):
    """Configuración: solo con el permiso CONFIGURAR (la Gerente). Se comprueba acá y, otra vez, en cada servicio."""
    @wraps(view)
    def envoltura(request, *args, **kwargs):
        if not puede_configurar(request.user):
            if request.method != "GET":
                raise PermisoDenegado("Solo la Gerente de Administración y Finanzas puede cambiar la configuración.")
            messages.error(request, "Solo la Gerente de Administración y Finanzas puede cambiar la configuración.")
            return redirect("ventas_dashboard")
        return view(request, *args, **kwargs)
    envoltura.permiso_requerido = Permiso.CONFIGURAR  # lo lee la prueba que audita todas las rutas
    return envoltura


def _volver(seccion):
    return redirect(f"/configuracion/#{seccion}")


@login_required
@solo_gerente
def configuracion(request):
    return render(request, "nucleo/configuracion.html", {
        "umbrales": umbrales_vigentes(),
        "cuentas": CuentaContable.objects.order_by("-activo", "codigo"),
        "tipos_cuenta": CuentaContable.Tipo.choices,
        "cajeras": Cajera.objects.order_by("-activo", "nombre"),
        "responsables_traslado": ResponsableTraslado.objects.order_by("-activo", "nombre"),
        "usuarios": usuarios_con_rol(get_user_model()),
        "roles": ROLES_ASIGNABLES.items(),
        "arqueo_caja": config_arqueo_caja(),
        "cuentas_banco": cuentas_banco(),
        "cambios": EventoAuditoria.objects.filter(entidad__in=ENTIDADES_DE_CONFIGURACION).select_related("usuario")[:15],
    })


@login_required
@solo_gerente
@require_POST
def guardar_umbrales(request):
    try:
        for clave in DEFINICIONES:
            if clave in request.POST:
                actualizar_umbral(clave, request.POST[clave], request.user)
        messages.success(request, "Umbrales guardados.")
    except ConfiguracionInvalida as exc:
        messages.error(request, str(exc))
    return _volver("umbrales")


@login_required
@solo_gerente
@require_POST
def editar_cuenta_view(request, cuenta_id):
    cuenta = get_object_or_404(CuentaContable, pk=cuenta_id)
    try:
        editar_cuenta(
            cuenta, nombre=request.POST.get("nombre", ""), tipo=request.POST.get("tipo", ""),
            activo=request.POST.get("activo") == "on", usuario=request.user,
            diario_odoo=request.POST.get("diario_odoo"), metodo_pago_odoo=request.POST.get("metodo_pago_odoo"),
        )
        messages.success(request, f"Cuenta {cuenta.codigo} actualizada.")
    except ConfiguracionInvalida as exc:
        messages.error(request, str(exc))
    return _volver("cuentas")


@login_required
@solo_gerente
@require_POST
def crear_cuenta_banco_view(request):
    try:
        cuenta = crear_cuenta(
            request.POST.get("codigo", ""), request.POST.get("nombre", ""), CuentaContable.Tipo.BANCO,
            request.user, diario_odoo=request.POST.get("diario_odoo", ""),
            metodo_pago_odoo=request.POST.get("metodo_pago_odoo", ""),
        )
        messages.success(request, f"Cuenta de banco {cuenta.nombre} agregada.")
    except ConfiguracionInvalida as exc:
        messages.error(request, str(exc))
    return _volver("cuentas")


@login_required
@solo_gerente
@require_POST
def crear_cajera_view(request):
    try:
        cajera = crear_cajera(request.POST.get("nombre", ""), request.user)
        messages.success(request, f"Cajera {cajera.nombre} agregada.")
    except ConfiguracionInvalida as exc:
        messages.error(request, str(exc))
    return _volver("cajeras")


@login_required
@solo_gerente
@require_POST
def editar_cajera_view(request, cajera_id):
    cajera = get_object_or_404(Cajera, pk=cajera_id)
    try:
        editar_cajera(
            cajera, nombre=request.POST.get("nombre", ""), activo=request.POST.get("activo") == "on",
            usuario=request.user,
        )
        messages.success(request, "Cajera actualizada.")
    except ConfiguracionInvalida as exc:
        messages.error(request, str(exc))
    return _volver("cajeras")


@login_required
@solo_gerente
@require_POST
def crear_responsable_traslado_view(request):
    try:
        responsable = crear_responsable_traslado(request.POST.get("nombre", ""), request.user)
        messages.success(request, f"{responsable.nombre} agregada(o) a la lista.")
    except ConfiguracionInvalida as exc:
        messages.error(request, str(exc))
    return _volver("responsables-traslado")


@login_required
@solo_gerente
@require_POST
def editar_responsable_traslado_view(request, responsable_id):
    responsable = get_object_or_404(ResponsableTraslado, pk=responsable_id)
    try:
        editar_responsable_traslado(
            responsable, nombre=request.POST.get("nombre", ""), activo=request.POST.get("activo") == "on",
            usuario=request.user,
        )
        messages.success(request, "Actualizado.")
    except ConfiguracionInvalida as exc:
        messages.error(request, str(exc))
    return _volver("responsables-traslado")


@login_required
@solo_gerente
@require_POST
def cambiar_rol_view(request, usuario_id):
    objetivo = get_object_or_404(get_user_model(), pk=usuario_id, is_active=True)
    try:
        asignar_rol(objetivo, request.POST.get("rol", ""), request.user)
        messages.success(request, f"Perfil de {objetivo.username} actualizado.")
    except ConfiguracionInvalida as exc:
        messages.error(request, str(exc))
    return _volver("aprobadores")


@login_required
@solo_gerente
@require_POST
def guardar_arqueo_caja_view(request):
    try:
        actualizar_config_arqueo_caja(
            diario=request.POST.get("diario", ""),
            cuenta_efectivo_codigo=request.POST.get("cuenta_efectivo_codigo", ""),
            cuenta_efectivo_nombre=request.POST.get("cuenta_efectivo_nombre", ""),
            cuenta_transbank_codigo=request.POST.get("cuenta_transbank_codigo", ""),
            cuenta_transbank_nombre=request.POST.get("cuenta_transbank_nombre", ""),
            cuenta_mercado_pago_codigo=request.POST.get("cuenta_mercado_pago_codigo", ""),
            cuenta_mercado_pago_nombre=request.POST.get("cuenta_mercado_pago_nombre", ""),
            razon_social=request.POST.get("razon_social"),
            codigo_analitico=request.POST.get("codigo_analitico", ""),
            usuario=request.user,
        )
        messages.success(request, "Datos del asiento de arqueo guardados.")
    except ConfiguracionInvalida as exc:
        messages.error(request, str(exc))
    return _volver("arqueo-caja")
