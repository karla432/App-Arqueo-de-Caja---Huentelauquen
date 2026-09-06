"""Exportación a Odoo (diseño funcional §21).

El formato de salida es el de la hoja real "Captura Odoo1" — no uno
inventado: columnas y mapeos confirmados contra el archivo histórico real
del 02-09-2026 (`Captura Odoo1 (2)`), no solo contra el mockup.

Solo se exportan días Aprobados o Cerrados. Cada exportación es un
archivo nuevo con versión incremental; el archivo se guarda fuera de
cualquier ruta servida al navegador (control #14).
"""
from pathlib import Path

import openpyxl
from django.conf import settings
from django.db import transaction

from nucleo.models import DiaArqueo, EventoAuditoria, Excepcion, Exportacion, PagoDeclarado, Venta

CLIENTE_GENERICO = "CLIENTE OCASIONAL"  # decisión D5, confirmada con Karla — coincide con el histórico real
IMPUESTO_LINEA = "19% IVA Venta Sala de Venta"  # constante en las 686 líneas del histórico real

# Confirmado contra el histórico real (686 líneas, 336 comandas, sin excepciones).
TIPO_DOC_ODOO = {
    "FAC": "Boleta Electrónica",
}

# "Débito" y "crédito" no se distinguen en la exportación real observada:
# ambos llegan como "Tarjeta Transbank". "Transferencia" no aparece en el
# día de referencia (llega en $0) — queda sin mapear a propósito hasta
# que exista un caso real contra el cual confirmarlo, en vez de adivinar.
MEDIO_ODOO = {
    PagoDeclarado.Medio.EFECTIVO: "Efectivo",
    PagoDeclarado.Medio.DEBITO: "Tarjeta Transbank",
    PagoDeclarado.Medio.CREDITO: "Tarjeta Transbank",
}

COLUMNAS = [
    "Cliente", "Fecha orden", "Vencimiento", "Es venta externa", "Tipo de pago externo",
    "ID de pago externo", "Tipo de documento externo", "Número de documento externo",
    "Líneas del pedido/Producto", "Líneas del pedido/Cantidad",
    "Líneas del pedido/Precio un.", "Líneas del pedido/Impuestos",
]

CARPETA_EXPORTACIONES = Path(settings.BASE_DIR) / "exportaciones"


class ExportacionInvalida(Exception):
    pass


def validar_venta(venta: Venta) -> list[str]:
    """§21: "antes de generar, se valida que cada venta exportada tenga
    medio aprobado, documento y líneas cuadradas contra su total"."""
    avisos = []
    pagos = list(venta.pagos.all())
    if len(pagos) != 1:
        avisos.append(
            f"Comanda {venta.n_interno}: tiene {len(pagos)} medios de pago — la exportación a Odoo "
            "asume un solo medio por comanda; requiere decisión manual antes de exportar."
        )
    if not venta.nro_comprobante or len(venta.nro_comprobante) < 7:
        avisos.append(f"Comanda {venta.n_interno}: número de comprobante inválido para extraer el documento.")
    if venta.es_guia_de_despacho:
        # Karla, 2026-09-06: la guía no es el documento final — el
        # Analista de Ingresos emite la Factura al cierre. Sin ese
        # número, no hay nada válido que exportar a Odoo.
        if not venta.factura_asociada:
            avisos.append(
                f"Comanda {venta.n_interno}: es guía de despacho ({venta.nro_comprobante}) sin factura "
                "asignada todavía — asígnala en Cierre y exportación antes de generar el archivo."
            )
    elif venta.tipo_doc not in TIPO_DOC_ODOO:
        avisos.append(f"Comanda {venta.n_interno}: tipo de documento {venta.tipo_doc!r} sin mapeo a Odoo conocido.")
    suma_lineas = sum(l.total_linea for l in venta.lineas.all())
    if abs(suma_lineas - venta.total_comanda) > 1:
        avisos.append(
            f"Comanda {venta.n_interno}: total ${venta.total_comanda:,.0f} no cuadra con "
            f"la suma de líneas ${suma_lineas:,.0f}."
        )
    return avisos


def _filas_de_venta(venta: Venta) -> list[dict]:
    pago = venta.pagos.first()
    medio = pago.medio_aprobado or pago.medio
    lineas = list(venta.lineas.all())

    if venta.es_guia_de_despacho:
        tipo_documento_externo = "Factura Electrónica"
        numero_documento_externo = venta.factura_asociada
    else:
        tipo_documento_externo = TIPO_DOC_ODOO.get(venta.tipo_doc, venta.tipo_doc)
        numero_documento_externo = venta.nro_comprobante[-7:]

    filas = [{
        "Cliente": CLIENTE_GENERICO,
        "Fecha orden": venta.dia.fecha,
        "Vencimiento": venta.dia.fecha,
        "Es venta externa": 1,
        "Tipo de pago externo": MEDIO_ODOO.get(medio, medio),
        "ID de pago externo": venta.n_interno,
        "Tipo de documento externo": tipo_documento_externo,
        "Número de documento externo": numero_documento_externo,
        "Líneas del pedido/Producto": lineas[0].articulo if lineas else "",
        "Líneas del pedido/Cantidad": lineas[0].cantidad if lineas else "",
        "Líneas del pedido/Precio un.": lineas[0].precio_unitario if lineas else "",
        "Líneas del pedido/Impuestos": IMPUESTO_LINEA,
    }]
    for linea in lineas[1:]:
        filas.append({
            "Cliente": None, "Fecha orden": None, "Vencimiento": None, "Es venta externa": None,
            "Tipo de pago externo": None, "ID de pago externo": None,
            "Tipo de documento externo": None, "Número de documento externo": None,
            "Líneas del pedido/Producto": linea.articulo,
            "Líneas del pedido/Cantidad": linea.cantidad,
            "Líneas del pedido/Precio un.": linea.precio_unitario,
            "Líneas del pedido/Impuestos": IMPUESTO_LINEA,
        })
    return filas


def _fila_otros_ingresos(excepcion) -> dict:
    """Un movimiento del proveedor sin comanda, aceptado como ingreso
    legítimo (motivo "Forma de pago sin boleta"): se exporta con su
    propia boleta, identificado como "OTROS INGRESOS" en vez de un
    producto de línea, ya que no hay comanda que lo respalde."""
    movimiento = excepcion.movimiento
    return {
        "Cliente": CLIENTE_GENERICO,
        "Fecha orden": excepcion.dia.fecha,
        "Vencimiento": excepcion.dia.fecha,
        "Es venta externa": 1,
        "Tipo de pago externo": MEDIO_ODOO.get(movimiento.medio, movimiento.medio),
        "ID de pago externo": f"OI-{excepcion.pk}",
        "Tipo de documento externo": "Boleta Electrónica",
        "Número de documento externo": excepcion.factura_asociada,
        "Líneas del pedido/Producto": "OTROS INGRESOS",
        "Líneas del pedido/Cantidad": 1,
        "Líneas del pedido/Precio un.": movimiento.monto_venta_valido_abono,
        "Líneas del pedido/Impuestos": IMPUESTO_LINEA,
    }


@transaction.atomic
def generar_exportacion(dia: DiaArqueo, usuario) -> Exportacion:
    from nucleo.documentos_servicio import otros_ingresos_pendientes

    if dia.estado not in (DiaArqueo.Estado.APROBADO, DiaArqueo.Estado.CERRADO):
        raise ExportacionInvalida("Solo se exportan días Aprobados o Cerrados.")

    # Anuladas quedan en un anexo, nunca se exportan (§21).
    ventas = list(
        Venta.objects.filter(dia=dia).exclude(estado="ANULADA")
        .prefetch_related("pagos", "lineas")
    )
    anuladas = Venta.objects.filter(dia=dia, estado="ANULADA").count()

    if otros_ingresos_pendientes(dia).exists():
        raise ExportacionInvalida(
            f"{otros_ingresos_pendientes(dia).count()} caso(s) de \"otros ingresos\" sin boleta asignada — "
            "asígnala en Cierre y exportación antes de generar el archivo."
        )
    otros_ingresos = Excepcion.objects.filter(
        dia=dia, tipo=Excepcion.Tipo.MOVIMIENTO_SIN_PAGO, resolucion=Excepcion.Resolucion.ACEPTAR,
        motivo__codigo="forma_pago_sin_boleta",
    ).select_related("movimiento")

    avisos = []
    for venta in ventas:
        avisos.extend(validar_venta(venta))
    if avisos:
        raise ExportacionInvalida(
            f"{len(avisos)} comanda(s) no se pueden exportar automáticamente: " + " | ".join(avisos[:5])
        )

    filas = []
    for venta in ventas:
        filas.extend(_filas_de_venta(venta))
    for excepcion in otros_ingresos:
        filas.append(_fila_otros_ingresos(excepcion))

    CARPETA_EXPORTACIONES.mkdir(exist_ok=True)
    ultima_version = dia.exportaciones.order_by("-version").first()
    version = (ultima_version.version + 1) if ultima_version else 1
    nombre_archivo = f"captura_odoo_{dia.fecha.isoformat()}_v{version}.xlsx"
    ruta = CARPETA_EXPORTACIONES / nombre_archivo

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Captura Odoo1"
    ws.append(COLUMNAS)
    for fila in filas:
        ws.append([fila[c] for c in COLUMNAS])
    wb.save(ruta)

    exportacion = Exportacion.objects.create(
        dia=dia, version=version, archivo=str(ruta), generado_por=usuario,
    )

    if dia.estado == DiaArqueo.Estado.CERRADO:
        dia.estado = DiaArqueo.Estado.EXPORTADO
        dia.save(update_fields=["estado"])

    EventoAuditoria.objects.create(
        dia=dia, entidad="Exportacion", entidad_id=str(exportacion.pk), accion="generar_exportacion",
        valor_anterior=None,
        valor_posterior={
            "version": version, "comandas": len(ventas), "anuladas_excluidas": anuladas,
            "archivo": nombre_archivo,
        },
        usuario=usuario,
    )
    return exportacion
