"""Menú «Archivos para Odoo» (Karla, 2026-09-24): las cuatro capturas juntas y explicadas.
Solo lectura: las descargas siguen usando las mismas rutas de cada captura.

Diseñado para crecer (consejo de UI): filtro por mes (el actual por defecto), franja
«Requiere tu atención» que ignora el filtro, Ventas y Arqueo en una sola tabla, explicaciones
plegadas y paginación de 25 filas."""
from datetime import date
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import render

from nucleo.permisos import Permiso, es_de_lectura, requiere, tiene_permiso
from nucleo.consolidado_servicio import rango_del_mes
from nucleo.exportaciones_servicio import CAPTURAS, resumen
from nucleo.models import DiaArqueo

POR_PAGINA = 25
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


def _mes_por_defecto(hoy: date) -> tuple[int, int]:
    """El mes actual; si todavía no tiene días, el mes del último día cargado."""
    if DiaArqueo.objects.filter(fecha__year=hoy.year, fecha__month=hoy.month).exists():
        return hoy.year, hoy.month
    ultimo = DiaArqueo.objects.order_by("-fecha").first()
    return (ultimo.fecha.year, ultimo.fecha.month) if ultimo else (hoy.year, hoy.month)


def _anio_mes(request, hoy: date) -> tuple[int, int | None]:
    """(año, mes) o (año, None) para «todo el año». Un valor mal escrito cae al mes por defecto."""
    defecto = _mes_por_defecto(hoy)
    try:
        anio = int(request.GET.get("anio") or defecto[0])
        mes_txt = request.GET.get("mes", "")
        mes = None if mes_txt == "todo" else int(mes_txt or defecto[1])
    except ValueError:
        return defecto
    if not 2000 <= anio <= 2100 or (mes is not None and not 1 <= mes <= 12):
        return defecto
    return anio, mes


def _mes_vecino(anio: int, mes: int, delta: int) -> tuple[int, int]:
    n = anio * 12 + (mes - 1) + delta
    return n // 12, n % 12 + 1


@requiere(Permiso.CONSULTAR)
def exportaciones_odoo(request):
    anio, mes = _anio_mes(request, date.today())
    desde, hasta = rango_del_mes(anio, mes) if mes else (date(anio, 1, 1), date(anio, 12, 31))
    datos = resumen(desde, hasta)

    def consulta(**cambios):
        q = {"anio": anio, "mes": mes if mes else "todo"}
        q.update(cambios)
        return "?" + urlencode(q)

    def pagina(lista, clave):
        try:
            n = int(request.GET.get(clave, 1))
        except ValueError:
            n = 1
        return Paginator(lista, POR_PAGINA).get_page(n)

    capturas = [{**c, "contador": datos["contadores"].get({"arqueo": "ventas"}.get(c["clave"], c["clave"]))} for c in CAPTURAS]
    ant = _mes_vecino(anio, mes, -1) if mes else (anio - 1, 12)
    sig = _mes_vecino(anio, mes, 1) if mes else (anio + 1, 1)
    return render(request, "nucleo/exportaciones_odoo.html", {
        "capturas": capturas, "cap_ventas": capturas[:2], "cap_deposito": capturas[2], "cap_reintegro": capturas[3],
        "atencion": datos["atencion"], "contadores": datos["contadores"], "puede_generar": tiene_permiso(request.user, Permiso.GENERAR_EXPORTACION), "puede_operar": tiene_permiso(request.user, Permiso.OPERAR),
        "pag_ventas": pagina(datos["ventas"]["filas"], "p_v"),
        "pag_depositos": pagina(datos["depositos"], "p_d"),
        "pag_reintegros": pagina(datos["reintegros"], "p_r"),
        "anio": anio, "mes": mes, "mes_nombre": MESES[mes - 1] if mes else "",
        "periodo": f"{MESES[mes - 1]} de {anio}" if mes else f"todo {anio}",
        "url_anterior": consulta(anio=ant[0], mes=ant[1] if mes else "todo"),
        "url_siguiente": consulta(anio=sig[0], mes=sig[1] if mes else "todo"),
        "url_anio": consulta(mes="todo"), "url_mes_actual": consulta(anio=date.today().year, mes=date.today().month),
        "meses": list(enumerate(MESES, start=1)),
        "base_paginas": {"anio": anio, "mes": mes if mes else "todo"},
    })
