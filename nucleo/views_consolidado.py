"""Consolidado por día y seguimiento de regularizaciones y reintegros (Karla,
2026-09-24). Solo lectura + descarga a Excel; cada fila enlaza a la pantalla
donde se resuelve."""
from datetime import date, timedelta

from django.contrib.auth.decorators import login_required
from django.http import FileResponse
from django.shortcuts import render

from nucleo.descargas import entregar_archivo
from nucleo.permisos import Permiso, es_de_lectura, requiere, tiene_permiso
from nucleo.consolidado_servicio import (
    consolidado_por_dia, generar_excel_consolidado, generar_excel_seguimiento, rango_del_mes, resumen_por_cajera,
    seguimiento_documentos, seguimiento_reintegros, totales_consolidado,
)
from nucleo.models import DiaArqueo, RegularizacionDocumento


def _fecha(texto: str | None) -> date | None:
    try:
        return date.fromisoformat(texto) if texto else None
    except ValueError:
        return None


def _rango_por_defecto_mensual(hoy: date) -> tuple[date, date]:
    """El mes actual; si todavía no tiene días cargados, el mes del último día cargado."""
    desde, hasta = rango_del_mes(hoy.year, hoy.month)
    if not DiaArqueo.objects.filter(fecha__gte=desde, fecha__lte=hasta).exists():
        ultimo = DiaArqueo.objects.order_by("-fecha").first()
        if ultimo:
            desde, hasta = rango_del_mes(ultimo.fecha.year, ultimo.fecha.month)
    return desde, hasta


def _rango_de_la_peticion(request, por_defecto: tuple[date, date]) -> tuple[date, date]:
    desde, hasta = _fecha(request.GET.get("desde")), _fecha(request.GET.get("hasta"))
    if not desde or not hasta or desde > hasta:
        return por_defecto
    return desde, hasta


@requiere(Permiso.CONSULTAR)
def consolidado_dias(request):
    desde, hasta = _rango_de_la_peticion(request, _rango_por_defecto_mensual(date.today()))
    filas = consolidado_por_dia(desde, hasta)
    return render(request, "nucleo/consolidado_dias.html", {
        "filas": filas, "totales": totales_consolidado(filas) if filas else None,
        "desde": desde, "hasta": hasta,
    })


@requiere(Permiso.DESCARGAR_GENERADOS)
def descargar_consolidado(request):
    desde, hasta = _rango_de_la_peticion(request, _rango_por_defecto_mensual(date.today()))
    ruta = generar_excel_consolidado(desde, hasta)
    return entregar_archivo(request, ruta, ruta.name, entidad="Reporte", entidad_id="auditoria_de_pagos", tipo="reporte_consolidado_por_dia",
                            detalle={"desde": desde.isoformat(), "hasta": hasta.isoformat()})


def _rango_seguimiento(request) -> tuple[date, date]:
    hoy = date.today()
    return _rango_de_la_peticion(request, (hoy - timedelta(days=90), hoy))


@requiere(Permiso.CONSULTAR)
def seguimiento(request):
    desde, hasta = _rango_seguimiento(request)
    vista = request.GET.get("vista", "documentos")
    vista = vista if vista in ("documentos", "reintegros") else "documentos"
    estado = request.GET.get("estado", "")
    tipo = request.GET.get("tipo", "")

    documentos = seguimiento_documentos(desde, hasta, estado=estado if vista == "documentos" else "", tipo=tipo)
    reintegros = seguimiento_reintegros(desde, hasta, estado=estado if vista == "reintegros" else "")
    todos_docs = seguimiento_documentos(desde, hasta)
    todos_reintegros = seguimiento_reintegros(desde, hasta)
    return render(request, "nucleo/seguimiento.html", {
        "vista": vista, "desde": desde, "hasta": hasta, "estado": estado, "tipo": tipo,
        "documentos": documentos, "reintegros": reintegros,
        "por_cajera": resumen_por_cajera(todos_reintegros),
        "kpi_docs_pendientes": sum(1 for f in todos_docs if f["pendiente"]),
        "kpi_docs_monto": sum((f["monto"] for f in todos_docs if f["pendiente"]), 0),
        "kpi_reint_saldo": sum((f["saldo"] for f in todos_reintegros), 0),
        "kpi_reint_pendientes": sum(1 for f in todos_reintegros if f["saldo"] > 0),
        "tipos": RegularizacionDocumento.Tipo.choices,
    })


@requiere(Permiso.DESCARGAR_GENERADOS)
def descargar_seguimiento(request):
    desde, hasta = _rango_seguimiento(request)
    ruta = generar_excel_seguimiento(desde, hasta)
    return entregar_archivo(request, ruta, ruta.name, entidad="Reporte", entidad_id="seguimiento", tipo="reporte_regularizaciones_y_reintegros",
                            detalle={"desde": desde.isoformat(), "hasta": hasta.isoformat()})
