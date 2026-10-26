"""Pantalla Cierre de mes (Karla, 2026-10-26). Ver nucleo.cierre_mes_servicio."""
from datetime import date

from django.contrib import messages
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from nucleo.cierre_mes_servicio import CierreMesInvalido, cerrar_mes, reabrir_mes, revision
from nucleo.notas_credito_servicio import notas_atrasadas
from nucleo.permisos import Permiso, requiere, tiene_permiso


def _mes(texto):
    try:
        anio, mes = (int(x) for x in str(texto).split("-")[:2])
        date(anio, mes, 1)
        return anio, mes
    except (ValueError, TypeError):
        hoy = date.today()
        # Por defecto, el mes anterior (el que toca cerrar).
        return (hoy.year, hoy.month - 1) if hoy.month > 1 else (hoy.year - 1, 12)


@requiere(Permiso.CONSULTAR)
def cierre_mes(request):
    anio, mes = _mes(request.GET.get("mes"))
    contexto = revision(anio, mes)
    anterior = (anio, mes - 1) if mes > 1 else (anio - 1, 12)
    siguiente = (anio, mes + 1) if mes < 12 else (anio + 1, 1)
    contexto.update({
        "clave_mes": f"{anio}-{mes:02d}",
        "mes_anterior": f"{anterior[0]}-{anterior[1]:02d}",
        "mes_siguiente": f"{siguiente[0]}-{siguiente[1]:02d}",
        "nombre_mes": date(anio, mes, 1),
        "puede_cerrar": tiene_permiso(request.user, Permiso.CERRAR_MES),
        "puede_reabrir": tiene_permiso(request.user, Permiso.REABRIR_DIA),
        "atrasadas": notas_atrasadas(),
    })
    return render(request, "nucleo/cierre_mes.html", contexto)


@require_POST  # primero el método: un GET responde 405 a cualquier perfil (la Gerente no tiene CERRAR_MES)
@requiere(Permiso.CERRAR_MES)
def cerrar_mes_view(request, mes):
    anio, m = _mes(mes)
    try:
        cerrar_mes(anio, m, request.user)
        messages.success(request, f"Mes {m:02d}/{anio} cerrado.")
    except CierreMesInvalido as exc:
        messages.error(request, str(exc))
    return redirect(f"/cierre-mes/?mes={anio}-{m:02d}")


@requiere(Permiso.REABRIR_DIA)
@require_POST
def reabrir_mes_view(request, mes):
    anio, m = _mes(mes)
    try:
        reabrir_mes(anio, m, request.user, request.POST.get("motivo", ""))
        messages.success(request, f"Mes {m:02d}/{anio} reabierto.")
    except CierreMesInvalido as exc:
        messages.error(request, str(exc))
    return redirect(f"/cierre-mes/?mes={anio}-{m:02d}")
