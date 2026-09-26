"""Reporte de ventas por producto (Karla, 2026-09-22): "aprovechando que
tienes toda la data de la sala de venta" — cuánto se vendió de cada
producto, en cantidad y en valor, para un período. Misma regla de la
fuente única que el tablero de ventas: solo cuentan días Cerrado o
Exportado, para no mezclar un arqueo pendiente con venta real.
"""
from datetime import date
from pathlib import Path

import openpyxl
from django.conf import settings

from nucleo.archivos_seguros import guardar_libro
from nucleo.models import LineaVenta
from nucleo.ventas_dashboard_servicio import ESTADOS_INCLUIDOS, _dias_del_rango

COLUMNAS = ["Producto", "Cantidad vendida", "Valor vendido", "Precio promedio", "% del total"]
CARPETA_REPORTES = Path(settings.BASE_DIR) / "exportaciones"


def resumen_por_producto(desde: date, hasta: date) -> list[dict]:
    dias_incluidos = _dias_del_rango(desde, hasta).filter(estado__in=ESTADOS_INCLUIDOS)
    productos: dict[str, dict] = {}
    for linea in LineaVenta.objects.filter(venta__dia__in=dias_incluidos).select_related("venta"):
        entrada = productos.setdefault(linea.articulo, {"cantidad": 0, "venta": 0})
        entrada["cantidad"] += linea.cantidad
        entrada["venta"] += linea.total_linea

    total_venta = sum(p["venta"] for p in productos.values()) or 1
    filas = [
        {
            "producto": nombre, "cantidad": datos["cantidad"], "venta": datos["venta"],
            "precio_promedio": (datos["venta"] / datos["cantidad"]) if datos["cantidad"] else 0,
            "pct_del_total": float(datos["venta"]) / float(total_venta) * 100,
        }
        for nombre, datos in productos.items()
    ]
    filas.sort(key=lambda f: f["venta"], reverse=True)
    return filas


def totales(filas: list[dict]) -> dict:
    return {
        "cantidad": sum(f["cantidad"] for f in filas),
        "venta": sum(f["venta"] for f in filas),
        "productos_distintos": len(filas),
    }


def generar_excel(desde: date, hasta: date, carpeta: Path | None = None) -> Path:
    filas = resumen_por_producto(desde, hasta)
    carpeta = carpeta or CARPETA_REPORTES
    carpeta.mkdir(exist_ok=True)
    ruta = carpeta / f"reporte_ventas_por_producto_{desde.isoformat()}_a_{hasta.isoformat()}.xlsx"

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Ventas por producto"
    ws.append(COLUMNAS)
    for f in filas:
        ws.append([
            f["producto"], float(f["cantidad"]), float(f["venta"]),
            round(float(f["precio_promedio"])), round(f["pct_del_total"], 1),
        ])
    guardar_libro(wb, ruta)
    return ruta
