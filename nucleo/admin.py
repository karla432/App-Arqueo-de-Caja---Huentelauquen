"""Administración técnica de Django (Karla, 2026-09-26; a partir del informe de seguridad).

* Solo entra el perfil Administrador técnico (además de ser staff). Ser superusuario o Gerente no basta.
* Todos los modelos del negocio son de SOLO CONSULTA: no se puede agregar, editar ni borrar ventas, pagos, movimientos,
  arqueos, cierres, depósitos, reintegros, documentos, configuración ni la bitácora por esta puerta. Esos cambios
  solo ocurren por las pantallas de la aplicación, con sus reglas de negocio y su bitácora.
* Lo que sí administra: usuarios y grupos (los perfiles), desde las pantallas estándar de Django. Cada cambio de grupos
  o de privilegios de una cuenta queda en la bitácora (ver `nucleo.auditoria_acceso`).
* Vía técnica de recuperación: no depende de esta pantalla sino de comandos de consola con acceso al servidor:
  `manage.py crear_admin_tecnico`, `manage.py recuperar_acceso` y `manage.py migrar_perfiles`."""
from django.contrib import admin
from django.apps import apps

from nucleo.permisos import Perfil, perfil_de


def _solo_administrador_tecnico(request) -> bool:
    usuario = request.user
    return bool(usuario.is_active and usuario.is_staff and perfil_de(usuario) == Perfil.ADMIN_TECNICO)


admin.site.has_permission = _solo_administrador_tecnico


class SoloConsultaAdmin(admin.ModelAdmin):
    """Ver sí; agregar, editar o borrar no."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# Listados útiles para inspeccionar; el resto de los modelos usa el listado estándar.
LISTADOS = {
    "DiaArqueo": {"list_display": ("fecha", "estado", "operado_por", "aprobado_por"), "list_filter": ("estado",)},
    "LoteCarga": {"list_display": ("dia", "fuente", "nombre_archivo", "version", "cargado_por", "cargado_en"), "list_filter": ("fuente",)},
    "Venta": {"list_display": ("n_interno", "dia", "caja", "turno", "total_comanda", "estado"), "list_filter": ("dia", "caja"), "search_fields": ("n_interno", "nro_comprobante")},
    "Exportacion": {"list_display": ("dia", "version", "generado_por", "generado_en", "acuse_estado", "invalidada")},
    "EventoAuditoria": {"list_display": ("hora", "usuario", "entidad", "accion"), "list_filter": ("entidad", "accion")},
}

for modelo in apps.get_app_config("nucleo").get_models():
    if admin.site.is_registered(modelo):
        admin.site.unregister(modelo)
    opciones = LISTADOS.get(modelo.__name__, {})
    admin.site.register(modelo, type(f"{modelo.__name__}SoloConsultaAdmin", (SoloConsultaAdmin,), dict(opciones)))
