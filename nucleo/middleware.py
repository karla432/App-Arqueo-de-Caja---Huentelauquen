"""Control de acceso por perfil, aplicado en un solo lugar para toda petición (Karla, 2026-09-26).

* Sin perfil asignado: no opera ni consulta nada financiero; solo ve la pantalla «Sin perfil asignado».
* Administrador técnico: solo la administración técnica (/admin/: usuarios y grupos). Las pantallas
  financieras le devuelven a /admin/.
* Solo Lectura / Solo Visualización: cualquier petición que no sea de lectura se bloquea, y mientras
  dura la petición ningún servicio puede escribir datos derivados (ver `contexto_solicitud`).
* /admin/: únicamente el Administrador técnico. Ser superusuario o staff no basta.

Además deja disponible el usuario y el modo de solo lectura para los servicios."""
from django.contrib import messages
from django.http import HttpResponseForbidden, HttpResponseRedirect
from django.urls import reverse

from nucleo import contexto_solicitud
from nucleo.permisos import PERFILES_DE_LECTURA, Perfil, perfil_de

MENSAJE_LECTURA = "Tu perfil es de solo lectura: no puedes modificar datos en la app."
METODOS_DE_LECTURA = ("GET", "HEAD", "OPTIONS")


class PerfilesMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        usuario = request.user
        if not usuario.is_authenticated:
            return self.get_response(request)

        perfil = perfil_de(usuario)
        ruta = request.path
        libres = {reverse("login"), reverse("logout"), reverse("sin_perfil")}

        if ruta.startswith("/admin/"):
            if perfil != Perfil.ADMIN_TECNICO:
                return HttpResponseForbidden(
                    "La administración técnica es solo para el perfil Administrador técnico.", content_type="text/plain; charset=utf-8",
                )
        elif ruta not in libres and not ruta.startswith("/static/"):
            if perfil == Perfil.SIN_PERFIL:
                if request.method in METODOS_DE_LECTURA:
                    return HttpResponseRedirect(reverse("sin_perfil"))
                return HttpResponseForbidden("Tu cuenta no tiene un perfil asignado.", content_type="text/plain; charset=utf-8")
            if perfil == Perfil.ADMIN_TECNICO:
                if request.method in METODOS_DE_LECTURA:
                    return HttpResponseRedirect("/admin/")
                return HttpResponseForbidden(
                    "El perfil Administrador técnico no opera la aplicación financiera.", content_type="text/plain; charset=utf-8",
                )
            if perfil in PERFILES_DE_LECTURA and request.method not in METODOS_DE_LECTURA:
                messages.error(request, MENSAJE_LECTURA)
                return HttpResponseRedirect(request.META.get("HTTP_REFERER") or "/")

        fichas = contexto_solicitud.fijar(usuario, solo_lectura=perfil in PERFILES_DE_LECTURA)
        try:
            return self.get_response(request)
        finally:
            contexto_solicitud.restaurar(fichas)
