"""Reporte de auditoría de correcciones de forma de pago (Karla, 2026-09-21)."""
from datetime import date

from django.contrib.auth.decorators import login_required
from django.http import FileResponse
from django.shortcuts import get_object_or_404, redirect, render

from nucleo.descargas import entregar_archivo
from nucleo.permisos import Permiso, es_de_lectura, requiere, tiene_permiso
from nucleo.auditoria_servicio import filas_reclasificacion, generar_archivo_auditoria, resumen_reclasificacion
from nucleo.models import DiaArqueo


def _dia_o_redireccion(request, fecha):
    from django.contrib import messages
    dia = DiaArqueo.objects.filter(fecha=date.fromisoformat(fecha)).first()
    if dia is None:
        messages.info(request, f"El {fecha} todavía no tiene archivos cargados — empieza cargándolos aquí.")
    return dia


@requiere(Permiso.CONSULTAR)
def auditoria_reclasificaciones(request, fecha):
    dia = _dia_o_redireccion(request, fecha)
    if dia is None:
        return redirect("carga_de_archivos", fecha=fecha)
    filas = filas_reclasificacion(dia)
    return render(request, "nucleo/auditoria_reclasificaciones.html", {
        "dia": dia, "filas": filas, "resumen": resumen_reclasificacion(filas),
    })


@requiere(Permiso.DESCARGAR_GENERADOS)
def descargar_auditoria(request, fecha):
    dia = get_object_or_404(DiaArqueo, fecha=date.fromisoformat(fecha))
    ruta = generar_archivo_auditoria(dia)
    return entregar_archivo(request, ruta, ruta.name, entidad="Reporte", entidad_id="auditoria_pagos", dia=dia, tipo="reporte_auditoria_de_pagos")
