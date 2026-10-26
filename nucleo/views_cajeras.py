"""Informe Desempeño de cajeras (Karla, 2026-10-26). Ver nucleo.desempeno_cajeras_servicio.

Lo ven todos los perfiles (CONSULTAR). El Excel lo descarga Contabilidad: permiso VALIDAR (Jefe de
Contabilidad y Gerente), porque trae montos atribuidos a personas."""
from datetime import date

from nucleo.descargas import entregar_archivo
from nucleo.desempeno_cajeras_servicio import desempeno, generar_excel
from nucleo.permisos import Permiso, requiere, tiene_permiso
from nucleo.ventas_dashboard_servicio import rango_del_atajo

ATAJOS = [("mes", "Mes"), ("trimestre", "Trimestre"), ("anio_en_curso", "Año en curso"), ("12_meses", "12 meses")]


def _rango(request) -> tuple[date, date, str]:
    try:
        if request.GET.get("desde") and request.GET.get("hasta"):
            desde, hasta = date.fromisoformat(request.GET["desde"]), date.fromisoformat(request.GET["hasta"])
            if desde <= hasta:
                return desde, hasta, "personalizado"
    except ValueError:
        pass
    atajo = request.GET.get("atajo", "mes")
    desde, hasta = rango_del_atajo(atajo)
    return desde, hasta, atajo


@requiere(Permiso.CONSULTAR)
def desempeno_cajeras(request):
    from django.shortcuts import render

    desde, hasta, atajo = _rango(request)
    contexto = desempeno(desde, hasta)
    contexto.update({
        "atajo": atajo, "atajos": ATAJOS,
        "puede_descargar": tiene_permiso(request.user, Permiso.VALIDAR),
        "max_tasa": max([f["tasa"] or 0 for f in contexto["top"]], default=0) or 1,
    })
    return render(request, "nucleo/desempeno_cajeras.html", contexto)


@requiere(Permiso.VALIDAR)
def descargar_desempeno_cajeras(request):
    desde, hasta, _ = _rango(request)
    ruta = generar_excel(desde, hasta)
    return entregar_archivo(request, ruta, ruta.name, entidad="Reporte", entidad_id="desempeno_cajeras",
                            tipo="reporte_desempeno_cajeras", detalle={"desde": desde.isoformat(), "hasta": hasta.isoformat()})
