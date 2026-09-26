"""Reporte de auditoría de reclasificaciones de forma de pago (Karla,
2026-09-21): todo cambio de forma de pago de una nota de venta, con su
historia y su evidencia, disponible sin necesitar la aprobación previa de la
Gerente."""
from pathlib import Path

import openpyxl
from django.conf import settings

from nucleo.archivos_seguros import guardar_libro
from nucleo.models import ArqueoEfectivo, DiaArqueo, PagoDeclarado, Reclasificacion
from nucleo.reclasificacion_servicio import NOMBRE_DE_REGLA

MEDIO = dict(PagoDeclarado.Medio.choices)

_ETIQUETAS_EVIDENCIA = [
    ("proveedor", "Proveedor", None),
    ("autorizacion", "Autorización", None),
    ("terminal", "Terminal", None),
    ("monto_movimiento", "Monto del cobro", "$"),
    ("hora_movimiento", "Hora del cobro", None),
    ("sobrante_efectivo", "Sobrante de efectivo", "$"),
    ("faltante_efectivo", "Faltante de efectivo", "$"),
    ("efectivo_contado", "Efectivo contado", "$"),
    ("busqueda_proveedor", "Búsqueda en el proveedor", None),
    ("fuente", "Fuente", None),
    ("comentario", "Comentario", None),
]

COLUMNAS_REPORTE = [
    "Nota de venta", "Comprobante", "Caja", "Cajera", "Forma de pago informada", "Forma de pago corregida",
    "Monto reclasificado", "Motivo", "Quién o qué regla", "Fecha y hora", "Evidencia utilizada",
]


def _texto_evidencia(evidencia: dict) -> str:
    partes = []
    for clave, etiqueta, unidad in _ETIQUETAS_EVIDENCIA:
        valor = evidencia.get(clave)
        if valor in (None, ""):
            continue
        if unidad == "$":
            valor = f"${float(valor):,.0f}".replace(",", ".")
        partes.append(f"{etiqueta}: {valor}")
    return " · ".join(partes)


def filas_reclasificacion(dia: DiaArqueo) -> list[dict]:
    cajeras = {a.caja: a.cajera for a in ArqueoEfectivo.objects.filter(dia=dia).select_related("cajera")}
    filas = []
    for r in Reclasificacion.objects.filter(dia=dia).select_related("pago__venta", "usuario"):
        venta = r.pago.venta
        cajera = cajeras.get(venta.caja)
        if r.origen == Reclasificacion.Origen.AUTOMATICA:
            quien = f"Regla automática: {NOMBRE_DE_REGLA.get(r.regla, r.regla)}"
        else:
            quien = r.usuario.get_full_name() or r.usuario.username if r.usuario else "Manual"
        filas.append({
            "reclasificacion": r,
            "nota_de_venta": venta.n_interno,
            "comprobante": venta.nro_comprobante,
            "caja": venta.caja,
            "cajera": cajera.nombre if cajera else "",
            "medio_original": MEDIO.get(r.medio_original, r.medio_original),
            "medio_corregido": MEDIO.get(r.medio_corregido, r.medio_corregido),
            "monto": r.monto,
            "motivo": r.motivo,
            "quien": quien,
            "automatica": r.origen == Reclasificacion.Origen.AUTOMATICA,
            "cuando": r.creada_en,
            "evidencia": _texto_evidencia(r.evidencia),
        })
    return filas


def resumen_reclasificacion(filas: list[dict]) -> dict:
    return {
        "cantidad": len(filas),
        "automaticas": sum(1 for f in filas if f["automatica"]),
        "manuales": sum(1 for f in filas if not f["automatica"]),
        "monto": sum(f["monto"] for f in filas),
    }


def generar_archivo_auditoria(dia: DiaArqueo, ruta_carpeta: Path | None = None) -> Path:
    carpeta = ruta_carpeta or Path(settings.BASE_DIR) / "exportaciones"
    carpeta.mkdir(exist_ok=True)
    ruta = carpeta / f"auditoria_reclasificaciones_{dia.fecha.isoformat()}.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Reclasificaciones"
    ws.append(COLUMNAS_REPORTE)
    for f in filas_reclasificacion(dia):
        ws.append([
            f["nota_de_venta"], f["comprobante"], f["caja"], f["cajera"], f["medio_original"], f["medio_corregido"],
            f["monto"], f["motivo"], f["quien"], f["cuando"].strftime("%d-%m-%Y %H:%M"), f["evidencia"],
        ])
    guardar_libro(wb, ruta)
    return ruta
