"""Calidad de registro en el POS (M10, diseño funcional §19/§25). Módulo
de gestión, no de arqueo — nada de esto bloquea un cierre. Existe para
que el volumen de excepciones baje con el tiempo, no solo se absorba.
"""
from datetime import date

from nucleo.dashboard_servicio import calidad_conciliacion
from nucleo.models import Coincidencia, DiaArqueo, Excepcion, PagoDeclarado, Venta


def resumen_calidad(fecha_desde: date, fecha_hasta: date) -> dict:
    dias = DiaArqueo.objects.filter(fecha__range=(fecha_desde, fecha_hasta))
    ventas = Venta.objects.filter(dia__in=dias)
    total_comandas = ventas.count()

    # El POS (VENTAS POR ARTICULO) no trae autorización — hasta que D1 se
    # resuelva con una fuente de enriquecimiento real, el 100% de las
    # comandas cuenta como "sin autorización". Es un hallazgo real, no un
    # error del cálculo: hoy el POS no puede dar ninguna otra cifra.
    pct_sin_autorizacion = 100.0 if total_comandas else 0.0

    medios_mal_declarados = Excepcion.objects.filter(
        dia__in=dias, tipo="medio_contradictorio", resolucion="aceptar",
    ).count()

    coincidencias = Coincidencia.objects.filter(dia__in=dias)
    score_promedio = (
        sum(c.score for c in coincidencias) / coincidencias.count() if coincidencias.exists() else 0
    )
    pct_automatico_dias = [calidad_conciliacion(d)["pct_automatico"] for d in dias]
    pct_automatico = sum(pct_automatico_dias) / len(pct_automatico_dias) if pct_automatico_dias else 0

    por_caja = []
    cajas = ventas.values_list("caja", flat=True).distinct()
    for caja in sorted(cajas):
        ventas_caja = ventas.filter(caja=caja)
        por_caja.append({
            "caja": caja,
            "turno": ventas_caja.first().turno if ventas_caja.exists() else "",
            "comandas": ventas_caja.count(),
            "medios_mal_declarados": Excepcion.objects.filter(
                dia__in=dias, tipo="medio_contradictorio", resolucion="aceptar",
                pago__venta__caja=caja,
            ).count(),
        })

    excepciones_por_tipo = [
        (t, e, Excepcion.objects.filter(dia__in=dias, tipo=t).count())
        for t, e in Excepcion.Tipo.choices
        if Excepcion.objects.filter(dia__in=dias, tipo=t).exists()
    ]

    dias_con_observaciones = []
    for dia in dias:
        excepciones_dia = Excepcion.objects.filter(dia=dia)
        if excepciones_dia.filter(abierta=True).count() >= 5:
            dias_con_observaciones.append((dia, f"{excepciones_dia.filter(abierta=True).count()} excepciones abiertas"))

    return {
        "total_comandas": total_comandas,
        "pct_automatico": pct_automatico,
        "score_promedio": score_promedio,
        "pct_sin_autorizacion": pct_sin_autorizacion,
        "medios_mal_declarados": medios_mal_declarados,
        "por_caja": por_caja,
        "excepciones_por_tipo": excepciones_por_tipo,
        "dias_con_observaciones": dias_con_observaciones,
    }
