"""Variables disponibles en todas las plantillas."""
from datetime import date

from nucleo.configuracion_servicio import puede_configurar
from nucleo.permisos import NOMBRE_DE_PERFIL, Permiso, perfil_de, tiene_permiso
from nucleo.models import DiaArqueo
from nucleo.umbrales import umbral


def fecha_del_menu(request):
    """Día al que apuntan los enlaces del menú lateral cuando la pantalla
    actual no es de un día puntual: el último día con datos, o hoy si aún
    no hay ninguno (antes estaba fijo en una fecha de prueba)."""
    ultimo = DiaArqueo.objects.order_by("-fecha").values_list("fecha", flat=True).first()
    return {"fecha_menu": (ultimo or date.today()).isoformat()}


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
        "exportaciones_por_generar": DiaArqueo.objects.filter(estado__in=(
            DiaArqueo.Estado.APROBADO, DiaArqueo.Estado.CERRADO, DiaArqueo.Estado.EXPORTADO,
        )).exclude(exportaciones__invalidada=False).count(),
    }
