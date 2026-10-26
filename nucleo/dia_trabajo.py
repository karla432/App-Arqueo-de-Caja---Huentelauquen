"""Día de trabajo (Karla, 2026-10-26).

Antes, al salir de una pantalla de un día (Excepciones, Arqueo, Cierre) hacia
una que no es de un día (Depósitos, Regularización, Calendario), el menú
«Diario» volvía al último día con datos y se perdía el día en que se estaba
trabajando; además no había forma de elegir la fecha sin pasar por el
calendario. Ahora:

* El día que se abre en cualquier pantalla de un día queda como «día de
  trabajo» en una cookie del navegador (no en la base: un GET nunca escribe
  datos, ni siquiera para un perfil de solo lectura).
* El menú usa ese día hasta que la persona lo cambie, y muestra un selector
  con flechas (día anterior/siguiente), calendario y la lista de días
  pendientes (con datos y todavía sin exportar).
* Cambiar de día deja a la persona en la misma pantalla en que estaba.
"""
from datetime import date, timedelta

from django.conf import settings
from django.shortcuts import redirect

from nucleo.models import DiaArqueo
from nucleo.permisos import Permiso, requiere

COOKIE = "dia_trabajo"
# Pantallas «de un día»: el selector lleva a la misma pantalla con la fecha nueva.
RUTAS_DEL_DIA = (
    "dashboard_dia", "carga_de_archivos", "bandeja_excepciones", "arqueo_efectivo", "cierre_y_exportacion",
    "auditoria_reclasificaciones",
)
DESTINO_POR_DEFECTO = "bandeja_excepciones"
MAX_PENDIENTES = 20


def _fecha_valida(texto):
    try:
        return date.fromisoformat(str(texto or "")[:10])
    except ValueError:
        return None


def fecha_de_la_pantalla(request):
    match = getattr(request, "resolver_match", None)
    if match and match.url_name in RUTAS_DEL_DIA:
        return _fecha_valida(match.kwargs.get("fecha"))
    return None


def fecha_de_trabajo(request) -> date:
    """La de la pantalla actual si es de un día; si no, la guardada; si no, el último día con datos."""
    fecha = fecha_de_la_pantalla(request) or _fecha_valida(request.COOKIES.get(COOKIE))
    if fecha:
        return fecha
    ultimo = DiaArqueo.objects.order_by("-fecha").values_list("fecha", flat=True).first()
    return ultimo or date.today()


def dias_pendientes():
    """Días con datos que todavía no están exportados (los que hay que trabajar)."""
    return list(
        DiaArqueo.objects.exclude(estado__in=(DiaArqueo.Estado.SIN_CARGAR, DiaArqueo.Estado.EXPORTADO))
        .order_by("fecha").values("fecha", "estado")[:MAX_PENDIENTES]
    )


def contexto_del_selector(request) -> dict:
    fecha = fecha_de_trabajo(request)
    match = getattr(request, "resolver_match", None)
    destino = match.url_name if match and match.url_name in RUTAS_DEL_DIA else DESTINO_POR_DEFECTO
    dia = DiaArqueo.objects.filter(fecha=fecha).first()
    estado = dia.estado if dia else DiaArqueo.Estado.SIN_CARGAR
    return {
        "fecha_menu": fecha.isoformat(),
        "dia_trabajo": fecha,
        "dia_trabajo_anterior": (fecha - timedelta(days=1)).isoformat(),
        "dia_trabajo_siguiente": (fecha + timedelta(days=1)).isoformat(),
        "dia_trabajo_estado": estado,
        "dia_trabajo_estado_nombre": DiaArqueo.Estado(estado).label,
        "dia_trabajo_destino": destino,
        "dias_pendientes": [
            {"fecha": d["fecha"], "estado": DiaArqueo.Estado(d["estado"]).label} for d in dias_pendientes()
        ],
    }


def recordar_dia(request, response):
    """Guarda en la cookie el día de la pantalla que se acaba de abrir."""
    fecha = fecha_de_la_pantalla(request)
    if fecha and request.COOKIES.get(COOKIE) != fecha.isoformat():
        response.set_cookie(
            COOKIE, fecha.isoformat(), max_age=60 * 60 * 24 * 30, httponly=True, samesite="Lax",
            secure=getattr(settings, "SESSION_COOKIE_SECURE", False),
        )
    return response


class DiaDeTrabajoMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.method == "GET" and getattr(response, "status_code", 500) == 200:
            recordar_dia(request, response)
        return response


@requiere(Permiso.CONSULTAR)
def ir_a_dia(request):
    """Cambia el día de trabajo y lleva a la misma pantalla (o a Excepciones)."""
    fecha = _fecha_valida(request.GET.get("fecha")) or fecha_de_trabajo(request)
    destino = request.GET.get("destino")
    if destino not in RUTAS_DEL_DIA:
        destino = DESTINO_POR_DEFECTO
    response = redirect(destino, fecha=fecha.isoformat())
    response.set_cookie(
        COOKIE, fecha.isoformat(), max_age=60 * 60 * 24 * 30, httponly=True, samesite="Lax",
        secure=getattr(settings, "SESSION_COOKIE_SECURE", False),
    )
    return response
