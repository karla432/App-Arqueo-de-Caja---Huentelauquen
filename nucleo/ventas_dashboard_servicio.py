"""Tablero de ventas · mensual y anual (diseño funcional, Pantalla 1).

Regla de la fuente única: el tablero solo suma días en estado Cerrado o
Exportado. Un día con excepciones abiertas no entra en la venta del
período — aparece como día excluido con su monto provisorio a la vista,
para que nadie explique una caída de venta que en realidad es un arqueo
pendiente.
"""
from calendar import monthrange
from datetime import date, timedelta

from nucleo.models import DiaArqueo, LineaVenta, PagoDeclarado, Venta
from nucleo.umbrales import umbral

ESTADOS_INCLUIDOS = (DiaArqueo.Estado.CERRADO, DiaArqueo.Estado.EXPORTADO)


def rango_del_atajo(atajo: str, referencia: date | None = None) -> tuple[date, date]:
    hoy = referencia or date.today()
    if atajo == "trimestre":
        primer_mes_trimestre = ((hoy.month - 1) // 3) * 3 + 1
        desde = date(hoy.year, primer_mes_trimestre, 1)
        ultimo_mes = primer_mes_trimestre + 2
        hasta = date(hoy.year, ultimo_mes, monthrange(hoy.year, ultimo_mes)[1])
    elif atajo == "anio_en_curso":
        desde, hasta = date(hoy.year, 1, 1), date(hoy.year, 12, 31)
    elif atajo == "12_meses":
        desde, hasta = hoy.replace(day=1) - timedelta(days=365), hoy
    else:  # "mes" por defecto
        desde = hoy.replace(day=1)
        hasta = date(hoy.year, hoy.month, monthrange(hoy.year, hoy.month)[1])
    return desde, hasta


def _mismo_rango_anio_anterior(desde: date, hasta: date) -> tuple[date, date]:
    def restar_anio(f: date) -> date:
        try:
            return f.replace(year=f.year - 1)
        except ValueError:  # 29 de febrero
            return f.replace(year=f.year - 1, day=28)
    return restar_anio(desde), restar_anio(hasta)


def _dias_del_rango(desde: date, hasta: date):
    return DiaArqueo.objects.filter(fecha__range=(desde, hasta))


def _kpis(desde: date, hasta: date) -> dict:
    dias_incluidos = _dias_del_rango(desde, hasta).filter(estado__in=ESTADOS_INCLUIDOS)
    ventas = Venta.objects.filter(dia__in=dias_incluidos)
    lineas = LineaVenta.objects.filter(venta__in=ventas)

    venta_neta = sum(v.total_comanda for v in ventas)
    comandas = ventas.count()
    dias_operados = dias_incluidos.count()
    return {
        "venta_neta": venta_neta,
        "comandas": comandas,
        "ticket_promedio": (venta_neta / comandas) if comandas else 0,
        "venta_diaria_promedio": (venta_neta / dias_operados) if dias_operados else 0,
        "unidades": sum(l.cantidad for l in lineas),
        "dias_operados": dias_operados,
    }


DIAS_SEMANA = ("L", "M", "M", "J", "V", "S", "D")
MAX_DIAS_GRAFICO = 62  # sobre dos meses el detalle diario no se lee; se usa el gráfico por mes


def grafico_por_dia(desde: date, hasta: date, por_dia: dict, provisorios: dict, hoy: date | None = None) -> dict | None:
    """Barras por día con etiqueta, fin de semana marcado, promedio, mejor y peor día (Karla, 2026-10-26:
    «no se entiende el gráfico de venta por día»). Los días sin cerrar aparecen como pendientes, sin altura,
    para que un hueco no se lea como un día sin venta."""
    ultimo = min(hasta, hoy or date.today())
    if ultimo < desde or (ultimo - desde).days + 1 > MAX_DIAS_GRAFICO:
        return None
    maximo = max(por_dia.values(), default=0) or 1
    barras = []
    fecha = desde
    while fecha <= ultimo:
        monto = por_dia.get(fecha)
        barras.append({
            "fecha": fecha, "dia": fecha.day, "letra": DIAS_SEMANA[fecha.weekday()],
            "fin_de_semana": fecha.weekday() >= 5, "monto": monto,
            "alto": round(monto / maximo * 100) if monto else 0,
            "pendiente": monto is None and fecha in provisorios, "provisorio": provisorios.get(fecha),
        })
        fecha += timedelta(days=1)
    if not por_dia:
        return {"barras": barras, "promedio": 0, "alto_promedio": 0, "mejor": None, "peor": None}
    promedio = sum(por_dia.values()) / len(por_dia)
    mejor = max(por_dia.items(), key=lambda kv: kv[1])
    peor = min(por_dia.items(), key=lambda kv: kv[1])
    return {
        "barras": barras, "promedio": promedio, "alto_promedio": round(promedio / maximo * 100),
        "mejor": {"fecha": mejor[0], "monto": mejor[1]}, "peor": {"fecha": peor[0], "monto": peor[1]},
    }


def grafico_por_hora(por_hora: dict, comandas_por_hora: dict) -> dict | None:
    """Venta por hora de cierre de la comanda, con todas las horas entre la primera y la última, % del total y hora peak."""
    if not por_hora:
        return None
    total = sum(por_hora.values()) or 1
    maximo = max(por_hora.values()) or 1
    horas = range(min(por_hora), max(por_hora) + 1)
    barras = [{
        "hora": h, "monto": por_hora.get(h, 0), "comandas": comandas_por_hora.get(h, 0),
        "pct": por_hora.get(h, 0) / total * 100, "alto": round(por_hora.get(h, 0) / maximo * 100),
    } for h in horas]
    peak = max(barras, key=lambda b: b["monto"])
    for b in barras:
        b["peak"] = b is peak
    return {"barras": barras, "peak": peak}


def _variacion_pct(actual: float, anterior: float) -> float | None:
    if not anterior:
        return None
    return (actual - anterior) / anterior * 100


def resumen_periodo(desde: date, hasta: date) -> dict:
    kpis_actual = _kpis(desde, hasta)
    desde_anterior, hasta_anterior = _mismo_rango_anio_anterior(desde, hasta)
    kpis_anterior = _kpis(desde_anterior, hasta_anterior)

    kpis = {}
    for clave in ("venta_neta", "comandas", "ticket_promedio", "venta_diaria_promedio", "unidades"):
        kpis[clave] = {
            "valor": kpis_actual[clave],
            "variacion_pct": _variacion_pct(kpis_actual[clave], kpis_anterior[clave]),
        }

    dias_todos = _dias_del_rango(desde, hasta)
    dias_incluidos = dias_todos.filter(estado__in=ESTADOS_INCLUIDOS)
    dias_excluidos = dias_todos.exclude(estado__in=ESTADOS_INCLUIDOS)
    ventas = Venta.objects.filter(dia__in=dias_incluidos)

    dias_excluidos_detalle = [
        {"dia": d, "monto_provisorio": sum(v.total_comanda for v in Venta.objects.filter(dia=d))}
        for d in dias_excluidos
    ]

    # Agregación simple en Python — el volumen esperado (días por año) no
    # justifica una consulta SQL más compleja todavía.
    por_dia: dict[date, float] = {}
    for v in ventas.select_related("dia"):
        por_dia[v.dia.fecha] = por_dia.get(v.dia.fecha, 0) + float(v.total_comanda)

    por_mes: dict[str, float] = {}
    por_mes_anterior: dict[str, float] = {}
    for fecha, monto in por_dia.items():
        por_mes[fecha.strftime("%Y-%m")] = por_mes.get(fecha.strftime("%Y-%m"), 0) + monto
    for v in Venta.objects.filter(dia__in=_dias_del_rango(desde_anterior, hasta_anterior).filter(estado__in=ESTADOS_INCLUIDOS)).select_related("dia"):
        clave = v.dia.fecha.strftime("%Y-%m")
        por_mes_anterior[clave] = por_mes_anterior.get(clave, 0) + float(v.total_comanda)

    por_hora: dict[int, float] = {}
    comandas_por_hora: dict[int, int] = {}
    for v in ventas:
        hora = v.hora_cierre or v.hora_apertura
        if hora:
            por_hora[hora.hour] = por_hora.get(hora.hour, 0) + float(v.total_comanda)
            comandas_por_hora[hora.hour] = comandas_por_hora.get(hora.hour, 0) + 1

    mix_medios: dict[str, float] = {}
    for pago in PagoDeclarado.objects.filter(venta__dia__in=dias_incluidos):
        mix_medios[pago.medio] = mix_medios.get(pago.medio, 0) + float(pago.monto)

    por_caja: dict[str, dict] = {}
    for v in ventas:
        entrada = por_caja.setdefault(v.caja, {"venta": 0, "comandas": 0})
        entrada["venta"] += float(v.total_comanda)
        entrada["comandas"] += 1

    from nucleo.descuentos_servicio import descuento_por_linea
    descuentos = descuento_por_linea(ventas)  # descuento a trabajadores: la venta es la cobrada
    productos: dict[str, dict] = {}
    for linea in LineaVenta.objects.filter(venta__in=ventas):
        entrada = productos.setdefault(linea.articulo, {"unidades": 0, "venta": 0})
        entrada["unidades"] += float(linea.cantidad)
        entrada["venta"] += float(linea.total_linea - descuentos.get(linea.pk, 0))
    top_productos = sorted(productos.items(), key=lambda kv: kv[1]["venta"], reverse=True)[:8]
    venta_total_productos = sum(p["venta"] for p in productos.values()) or 1
    acumulado = 0
    top_productos_pct = []
    for nombre, datos in top_productos:
        acumulado += datos["venta"]
        top_productos_pct.append({
            "nombre": nombre, "unidades": datos["unidades"], "venta": datos["venta"],
            "pct_acumulado": acumulado / venta_total_productos * 100,
        })

    tope = umbral("umbral_diferencia_caja")
    dias_dentro_de_umbral = 0
    dias_cerrados_sin_diferencia = 0
    dias_con_diferencia_autorizada = 0
    diferencia_acumulada = 0.0
    tiempos_cierre_aprobacion = []
    for dia in dias_incluidos:
        arqueos = dia.arqueos_caja.all()
        diferencias = [a.diferencia for a in arqueos if a.diferencia is not None]
        diferencia_acumulada += float(sum(diferencias))
        if diferencias and all(d == 0 for d in diferencias):
            dias_cerrados_sin_diferencia += 1
        if diferencias and all(abs(d) <= tope for d in diferencias):
            dias_dentro_de_umbral += 1
        elif any(a.autorizado_por_id for a in arqueos):
            dias_con_diferencia_autorizada += 1
        if dia.aprobado_en and dia.creado_en:
            tiempos_cierre_aprobacion.append((dia.aprobado_en - dia.creado_en).total_seconds() / 86400)

    return {
        "desde": desde, "hasta": hasta,
        "desde_anterior": desde_anterior, "hasta_anterior": hasta_anterior,
        "kpis": kpis,
        "dias_excluidos": dias_excluidos_detalle,
        "por_mes": sorted(por_mes.items()),
        "por_mes_anterior": por_mes_anterior,
        "por_dia": sorted(por_dia.items()),
        "por_hora": sorted(por_hora.items()),
        "grafico_dia": grafico_por_dia(desde, hasta, por_dia, {
            d["dia"].fecha: d["monto_provisorio"] for d in dias_excluidos_detalle}),
        "grafico_hora": grafico_por_hora(por_hora, comandas_por_hora),
        "unidades_por_comanda": (float(kpis_actual["unidades"]) / kpis_actual["comandas"]) if kpis_actual["comandas"] else 0,
        "mix_medios": mix_medios,
        "por_caja": sorted(por_caja.items(), key=lambda kv: kv[1]["venta"], reverse=True),
        "top_productos": top_productos_pct,
        "control": {
            "dias_operados": dias_incluidos.count(),
            "dias_cerrados_sin_diferencia": dias_cerrados_sin_diferencia,
            "dias_dentro_de_umbral": dias_dentro_de_umbral,
            "descuento_trabajadores": sum(descuentos.values()),
            "comandas_con_descuento": len({ventas_de_linea for ventas_de_linea in
                                           LineaVenta.objects.filter(pk__in=list(descuentos)).values_list("venta_id", flat=True)}),
            "umbral_diferencia_caja": tope,
            "dias_con_diferencia_autorizada": dias_con_diferencia_autorizada,
            "diferencia_acumulada": diferencia_acumulada,
            "pct_sobre_venta": (
                abs(diferencia_acumulada) / float(kpis_actual["venta_neta"]) * 100
                if kpis_actual["venta_neta"] else 0
            ),
            "dias_cierre_a_aprobacion_promedio": (
                sum(tiempos_cierre_aprobacion) / len(tiempos_cierre_aprobacion) if tiempos_cierre_aprobacion else None
            ),
        },
    }
