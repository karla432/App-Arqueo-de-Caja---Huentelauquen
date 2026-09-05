"""Histórico e indicadores (diseño funcional §19). Con un solo día
cargado hoy, estas consultas ya funcionan igual sobre cien — se prueban
contra lo que existe, no contra datos inventados."""
import calendar
from datetime import date

from django.db.models import Q

from nucleo.dashboard_servicio import calidad_conciliacion, cuadre_por_medio, resumen_efectivo
from nucleo.models import DiaArqueo, MovimientoProveedor, Venta


def resumen_dias(fecha_desde: date, fecha_hasta: date) -> list[dict]:
    dias = DiaArqueo.objects.filter(fecha__range=(fecha_desde, fecha_hasta)).order_by("-fecha")
    filas = []
    for dia in dias:
        ventas = Venta.objects.filter(dia=dia)
        cuadre = cuadre_por_medio(dia)
        transbank = sum(f["declarado_pos"] for f in cuadre if f["medio"] in ("debito", "credito"))
        efectivo = resumen_efectivo(dia)
        calidad = calidad_conciliacion(dia)
        exportacion = dia.exportaciones.filter(invalidada=False).order_by("-version").first()
        filas.append({
            "dia": dia,
            "venta_total": sum(v.total_comanda for v in ventas),
            "transbank": transbank,
            "efectivo": efectivo["teorico"],
            "mercado_pago": 0,  # Fase 2 (D12) — siempre en cero hasta entonces
            "pct_automatico": calidad["pct_automatico"],
            "excepciones_abiertas": calidad["total_excepciones_abiertas"],
            "diferencia_caja": efectivo["diferencia"],
            "exportacion": exportacion,
            "tiene_deposito": hasattr(dia, "deposito"),
        })
    return filas


def calendario_mes(anio: int, mes: int) -> list[list[dict | None]]:
    dias_del_mes = {d.fecha: d for d in DiaArqueo.objects.filter(fecha__year=anio, fecha__month=mes)}
    cal = calendar.Calendar(firstweekday=0)  # lunes primero, como el mockup
    semanas = []
    for semana in cal.monthdatescalendar(anio, mes):
        fila = []
        for fecha in semana:
            if fecha.month != mes:
                fila.append(None)
                continue
            dia = dias_del_mes.get(fecha)
            fila.append({"fecha": fecha, "dia": dia})
        semanas.append(fila)
    return semanas


def buscar_transversal(query: str) -> dict:
    query = query.strip()
    if not query:
        return {"ventas": [], "movimientos": []}

    filtro_venta = Q(n_interno__icontains=query) | Q(nro_comprobante__icontains=query)
    filtro_mov = Q(autorizacion__icontains=query)
    try:
        monto = float(query.replace(".", "").replace(",", ""))
        filtro_venta |= Q(total_comanda=monto)
        filtro_mov |= Q(monto_venta_valido_abono=monto)
    except ValueError:
        pass

    return {
        "ventas": Venta.objects.filter(filtro_venta).select_related("dia")[:20],
        "movimientos": MovimientoProveedor.objects.filter(filtro_mov).select_related("dia")[:20],
    }
