"""Pantallas de la verificación en dos pasos (ver `nucleo.dos_pasos`). No usan los permisos del negocio: solo exigen
sesión iniciada; el middleware es quien manda aquí a quien todavía no pasó el segundo paso."""
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme

from nucleo import dos_pasos
from nucleo.dos_pasos import CLAVE_DE_SESION, DosPasosInvalido


def _destino(request) -> str:
    destino = request.session.pop("despues_de_verificar", "") or "/"
    return destino if url_has_allowed_host_and_scheme(destino, allowed_hosts={request.get_host()}) else "/"


def _origen(request) -> str:
    return (request.META.get("REMOTE_ADDR") or "")[:100]


@login_required
def verificacion(request):
    usuario = request.user
    if not dos_pasos.requiere_verificacion(usuario) or request.session.get(CLAVE_DE_SESION):
        return redirect(_destino(request))
    if not dos_pasos.tiene_activo(usuario):
        return redirect("activar_dos_pasos")
    error = ""
    if request.method == "POST":
        try:
            dos_pasos.verificar(usuario, request.POST.get("codigo", ""), _origen(request))
        except DosPasosInvalido as exc:
            error = str(exc)
        else:
            request.session[CLAVE_DE_SESION] = True
            request.session.cycle_key()
            return redirect(_destino(request))
    return render(request, "nucleo/dos_pasos.html", {"modo": "verificar", "error": error})


@login_required
def activar_dos_pasos(request):
    usuario = request.user
    if not dos_pasos.requiere_verificacion(usuario):
        return redirect("/")
    if dos_pasos.tiene_activo(usuario):
        return redirect("verificacion_dos_pasos")
    dispositivo = dos_pasos.iniciar_activacion(usuario)
    error = ""
    if request.method == "POST":
        try:
            codigos = dos_pasos.confirmar_activacion(usuario, request.POST.get("codigo", ""))
        except DosPasosInvalido as exc:
            error = str(exc)
        else:
            request.session[CLAVE_DE_SESION] = True
            request.session.cycle_key()
            return render(request, "nucleo/dos_pasos.html", {"modo": "codigos", "codigos": codigos, "destino": _destino(request)})
    return render(request, "nucleo/dos_pasos.html", {
        "modo": "activar", "error": error, "secreto": dispositivo.secreto,
        "qr": dos_pasos.qr_svg(dos_pasos.uri_de_aprovisionamiento(dispositivo)),
    })
