"""Resumen del día (diseño funcional §17): lo que responde en cinco
segundos "¿cuadró el día, cuánto falta y puedo aprobarlo?" — agrega datos
que ya existen en otras partes del sistema, no calcula nada nuevo.
"""
from nucleo.arqueo_servicio import UMBRAL_DIFERENCIA_CAJA, preparar_arqueos_del_dia
from nucleo.cierre_servicio import estado_checklist, puede_aprobar
from nucleo.models import Coincidencia, DiaArqueo, Excepcion, MovimientoProveedor, PagoDeclarado, Venta

# Un movimiento "prepago" se reporta junto al débito, consistente con el
# supuesto de conciliación que Karla confirmó (2026-09-05): el POS lo
# declara como débito, así que separarlo en el cuadre inflaría
# artificialmente la diferencia de débito.
MEDIO_REPORTE = {"debito": "debito", "credito": "credito", "prepago": "debito"}


def cuadre_por_medio(dia: DiaArqueo) -> list[dict]:
    declarado: dict[str, float] = {}
    for pago in PagoDeclarado.objects.filter(venta__dia=dia).exclude(medio__in=["efectivo", "transferencia"]):
        declarado[pago.medio] = declarado.get(pago.medio, 0) + float(pago.monto)

    proveedor: dict[str, float] = {}
    for mov in MovimientoProveedor.objects.filter(dia=dia):
        medio = MEDIO_REPORTE.get(mov.medio, mov.medio)
        proveedor[medio] = proveedor.get(medio, 0) + float(mov.monto_venta_valido_abono)

    conciliado: dict[str, float] = {}
    for c in Coincidencia.objects.filter(dia=dia).prefetch_related("pagos"):
        for pago in c.pagos.all():
            conciliado[pago.medio] = conciliado.get(pago.medio, 0) + float(pago.monto)

    filas = []
    for medio in ("debito", "credito"):
        filas.append({
            "medio": medio,
            "declarado_pos": declarado.get(medio, 0),
            "proveedor": proveedor.get(medio, 0),
            "conciliado": conciliado.get(medio, 0),
            "diferencia": declarado.get(medio, 0) - proveedor.get(medio, 0),
        })

    transferencia = sum(
        float(m) for m in
        PagoDeclarado.objects.filter(venta__dia=dia, medio="transferencia").values_list("monto", flat=True)
    )
    filas.append({
        "medio": "transferencia", "declarado_pos": transferencia, "proveedor": None,
        "conciliado": None, "diferencia": None,
    })
    return filas


def resumen_efectivo(dia: DiaArqueo) -> dict:
    arqueos = preparar_arqueos_del_dia(dia)
    return {
        "teorico": sum(a.teorico for a in arqueos),
        "contado": sum(a.contado for a in arqueos if a.contado is not None),
        "diferencia": sum(a.diferencia for a in arqueos if a.diferencia is not None),
        "todas_contadas": all(a.contado is not None for a in arqueos) and bool(arqueos),
    }


def calidad_conciliacion(dia: DiaArqueo) -> dict:
    coincidencias = Coincidencia.objects.filter(dia=dia)
    total_movimientos = MovimientoProveedor.objects.filter(dia=dia).count()
    total_conciliado = sum(c.movimientos.count() for c in coincidencias)
    excepciones_abiertas = Excepcion.objects.filter(dia=dia, abierta=True)

    return {
        "pct_automatico": (total_conciliado / total_movimientos * 100) if total_movimientos else 0,
        "score_promedio": (
            sum(c.score for c in coincidencias) / coincidencias.count() if coincidencias.exists() else 0
        ),
        "excepciones_por_tipo": [
            (t, e, excepciones_abiertas.filter(tipo=t).count())
            for t, e in Excepcion.Tipo.choices
            if excepciones_abiertas.filter(tipo=t).exists()
        ],
        "total_excepciones_abiertas": excepciones_abiertas.count(),
        "monto_en_juego": sum(e.monto_en_juego for e in excepciones_abiertas),
    }


def semaforo(dia: DiaArqueo) -> str:
    """verde: sin bloqueantes y caja dentro de umbral. ámbar: hay
    excepciones abiertas. rojo: diferencia de caja fuera de umbral sin
    autorizar."""
    arqueos = preparar_arqueos_del_dia(dia)
    tiene_diferencia_sin_autorizar = any(
        a.contado is not None and abs(a.diferencia) > UMBRAL_DIFERENCIA_CAJA and not a.autorizado_por_id
        for a in arqueos
    )
    if tiene_diferencia_sin_autorizar:
        return "rojo"
    if Excepcion.objects.filter(dia=dia, abierta=True).exclude(tipo="calidad_registro").exists():
        return "ambar"
    return "verde"


def resumen_dia(dia: DiaArqueo) -> dict:
    ventas = Venta.objects.filter(dia=dia)
    checklist = estado_checklist(dia)
    return {
        "dia": dia,
        "semaforo": semaforo(dia),
        "venta_total": sum(v.total_comanda for v in ventas),
        "cantidad_comandas": ventas.count(),
        "cuadre_por_medio": cuadre_por_medio(dia),
        "efectivo": resumen_efectivo(dia),
        "calidad": calidad_conciliacion(dia),
        "cajas": preparar_arqueos_del_dia(dia),
        "checklist": checklist,
        "puede_aprobar": puede_aprobar(dia),
    }
