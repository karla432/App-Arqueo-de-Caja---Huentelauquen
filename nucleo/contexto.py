"""Variables disponibles en todas las plantillas."""
from datetime import date

from nucleo.configuracion_servicio import puede_configurar
from nucleo.permisos import NOMBRE_DE_PERFIL, Permiso, perfil_de, tiene_permiso
from nucleo.models import DiaArqueo
from nucleo.umbrales import umbral


def fecha_del_menu(request):
    """Día de trabajo del menú lateral y su selector (ver nucleo.dia_trabajo):
    el de la pantalla actual, o el último que la persona abrió, o el último
    día con datos."""
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        ultimo = DiaArqueo.objects.order_by("-fecha").values_list("fecha", flat=True).first()
        return {"fecha_menu": (ultimo or date.today()).isoformat()}
    from nucleo.dia_trabajo import contexto_del_selector
    return contexto_del_selector(request)


def configuracion_del_menu(request):
    """Umbrales vigentes (para que los textos de las pantallas no queden
    fijos en un número) y si el menú muestra "Configuración"."""
    if not request.user.is_authenticated:
        return {}
    perfil = perfil_de(request.user)
    return {
        "perfil_nombre": NOMBRE_DE_PERFIL[perfil],
        "permisos": {p.value: tiene_permiso(request.user, p) for p in Permiso},
        "puede_configurar": puede_configurar(request.user),
        "umbral_caja": umbral("umbral_diferencia_caja"),
        "umbral_visado": umbral("umbral_visado_perdida"),
        # días aprobados que todavía no tienen sus archivos para Odoo (contador junto a «Archivos para Odoo»)
        "nc_atrasadas": _nc_atrasadas(),
        "anulaciones_por_aprobar": _anulaciones_por_aprobar(request.user),
        "exportaciones_por_generar": DiaArqueo.objects.filter(estado__in=(
            DiaArqueo.Estado.APROBADO, DiaArqueo.Estado.CERRADO, DiaArqueo.Estado.EXPORTADO,
        )).exclude(exportaciones__invalidada=False).count(),
    }


def _nc_atrasadas() -> int:
    from nucleo.notas_credito_servicio import notas_atrasadas
    return len(notas_atrasadas())


def _anulaciones_por_aprobar(usuario) -> list:
    """Solicitudes de anulación de carga que esta persona puede aprobar (VALIDAR y no las pidió ella)."""
    if not tiene_permiso(usuario, Permiso.VALIDAR):
        return []
    from nucleo.models import SolicitudAnulacionCarga
    return list(SolicitudAnulacionCarga.objects.filter(estado="pendiente").exclude(solicitada_por=usuario)
                .select_related("dia").order_by("dia__fecha"))
