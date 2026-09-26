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

from nucleo.archivos_seguros import guardar_libro
from nucleo.models import DiaArqueo, EventoAuditoria, Excepcion, Exportacion, PagoDeclarado, Venta
from nucleo.pagos_divididos_servicio import ETIQUETA_TIPO_PAGO_ODOO, plan_de_pago_dividido, planes_del_dia

CLIENTE_GENERICO = "CLIENTE OCASIONAL"  # decisión D5, confirmada con Karla — coincide con el histórico real
IMPUESTO_LINEA = "19% IVA Venta Sala de Venta"  # constante en las 686 líneas del histórico real

# Confirmado contra el histórico real (686 líneas, 336 comandas, sin excepciones).
TIPO_DOC_ODOO = {
    "FAC": "Boleta Electrónica",
}

# "Débito" y "crédito" no se distinguen entre sí en la exportación real
# observada — lo que cambia el rótulo es el adquirente que procesó la
# tarjeta, no si fue débito o crédito (confirmado con Karla, 2026-09-16:
# Transbank -> "Tarjeta Transbank", Mercado Pago -> "Tarjeta Mercado
# Pago"). "Transferencia" no aparece en el día de referencia (llega en
# $0) — queda sin mapear a propósito hasta que exista un caso real.
MEDIO_ODOO = {
    PagoDeclarado.Medio.EFECTIVO: "Efectivo",
}

TARJETA_ODOO_POR_PROVEEDOR = {
    "transbank": "Tarjeta Transbank",
    "mercado_pago": "Tarjeta Mercado Pago",
}
# Transbank fue el único adquirente hasta 2026-09: un pago con tarjeta
# que no se pudo trazar a ningún movimiento conciliado (excepción
# rechazada, o un escenario armado a mano sin pasar por el motor) se
# asume Transbank por continuidad con el histórico, en vez de dejar el
# dato en blanco.
PROVEEDOR_TARJETA_POR_DEFECTO = "transbank"


def _proveedor_del_pago(pago: PagoDeclarado) -> str | None:
    coincidencia = pago.coincidencias.first()
    if not coincidencia:
        return None
    movimiento = coincidencia.movimientos.first()
    return movimiento.proveedor if movimiento else None


def _tipo_pago_odoo(pago: PagoDeclarado) -> str:
    medio = pago.medio_aprobado or pago.medio
    if medio in (PagoDeclarado.Medio.DEBITO, PagoDeclarado.Medio.CREDITO):
        proveedor = _proveedor_del_pago(pago) or PROVEEDOR_TARJETA_POR_DEFECTO
        return TARJETA_ODOO_POR_PROVEEDOR.get(proveedor, "Tarjeta Transbank")
    return MEDIO_ODOO.get(medio, medio)


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
    plan = plan_de_pago_dividido(venta)
    if plan is not None:
        # Pago dividido (Karla, 2026-09-23): Odoo admite un solo medio por
        # venta; la venta va por el principal y el resto viaja como
        # traspaso en el asiento Arqueo Caja. Ver pagos_divididos_servicio.
        for problema in plan.problemas:
            avisos.append(f"Comanda {venta.n_interno}: pago dividido — {problema}.")
    elif not pagos:
        avisos.append(f"Comanda {venta.n_interno}: no tiene ningún medio de pago declarado.")
    if not venta.nro_comprobante or len(venta.nro_comprobante) < 7:
        avisos.append(f"Comanda {venta.n_interno}: número de comprobante inválido para extraer el documento.")
    if venta.es_guia_de_despacho:
        # Karla, 2026-09-06: la guía no es el documento final — el
        # Analista de Ingresos emite la Factura al cierre. Sin ese
        # número, no hay nada válido que exportar a Odoo.
        if not venta.factura_asociada:
            avisos.append(
                f"Comanda {venta.n_interno}: es guía de despacho ({venta.nro_comprobante}) sin factura "
                "asignada todavía — asígnala en Cierre antes de generar el archivo."
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


def _tipo_pago_de_la_venta(venta: Venta) -> str:
    plan = plan_de_pago_dividido(venta)
    if plan is not None:
        return ETIQUETA_TIPO_PAGO_ODOO[plan.principal]
    return _tipo_pago_odoo(venta.pagos.first())


def _filas_de_venta(venta: Venta) -> list[dict]:
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
        "Tipo de pago externo": _tipo_pago_de_la_venta(venta),
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
        "Tipo de pago externo": TARJETA_ODOO_POR_PROVEEDOR.get(movimiento.proveedor, "Tarjeta Transbank"),
        "ID de pago externo": f"OI-{excepcion.pk}",
        "Tipo de documento externo": "Boleta Electrónica",
        "Número de documento externo": excepcion.factura_asociada,
        "Líneas del pedido/Producto": "OTROS INGRESOS",
        "Líneas del pedido/Cantidad": 1,
        "Líneas del pedido/Precio un.": movimiento.monto_venta_valido_abono,
        "Líneas del pedido/Impuestos": IMPUESTO_LINEA,
    }


@transaction.atomic
def _revisar_exportable(dia: DiaArqueo):
    """Todo lo que debe cumplirse para poder generar los archivos, SIN escribir nada. Lanza
    ExportacionInvalida con el motivo. Devuelve (ventas, anuladas, otros_ingresos, filas, filas_arqueo)."""
    from nucleo.arqueo_caja_servicio import ArqueoCajaInvalido, generar_lineas_arqueo_caja
    from nucleo.documentos_servicio import otros_ingresos_pendientes
    from nucleo.perdidas_servicio import diferencias_de_caja_pendientes_de_visado, perdidas_pendientes_de_visado

    if dia.estado not in (DiaArqueo.Estado.APROBADO, DiaArqueo.Estado.CERRADO):
        raise ExportacionInvalida("Solo se exportan días Aprobados o Cerrados.")

    # Anuladas quedan en un anexo, nunca se exportan (§21).
    ventas = list(
        Venta.objects.filter(dia=dia).exclude(estado="ANULADA")
        .prefetch_related("pagos__coincidencias__movimientos", "lineas")
    )
    anuladas = Venta.objects.filter(dia=dia, estado="ANULADA").count()

    if otros_ingresos_pendientes(dia).exists():
        raise ExportacionInvalida(
            f"{otros_ingresos_pendientes(dia).count()} caso(s) de \"otros ingresos\" sin boleta asignada — "
            "asígnala en Cierre antes de generar el archivo."
        )
    pendientes_de_visado = perdidas_pendientes_de_visado(dia).count() + len(diferencias_de_caja_pendientes_de_visado(dia))
    if pendientes_de_visado:
        raise ExportacionInvalida(
            f"{pendientes_de_visado} diferencia(s) de caja o de tarjeta sobre el umbral sin visar — "
            "el Jefe de Contabilidad debe visarlas antes de generar el archivo."
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

    # Fase 2 (Karla, 2026-09-21): las filas del segundo archivo, el asiento
    # "Arqueo Caja", se calculan antes de escribir nada a disco — así una
    # falta de cajera asignada no deja un archivo de ventas huérfano.
    try:
        filas_arqueo = generar_lineas_arqueo_caja(dia)
    except ArqueoCajaInvalido as exc:
        raise ExportacionInvalida(str(exc))
    return ventas, anuladas, otros_ingresos, filas, filas_arqueo


def motivo_de_bloqueo(dia: DiaArqueo) -> str:
    """Por qué NO se pueden generar hoy los archivos del día (vacío si se puede). No escribe nada."""
    try:
        _revisar_exportable(dia)
    except ExportacionInvalida as exc:
        return str(exc)
    return ""


def exportacion_vigente(dia: DiaArqueo):
    return dia.exportaciones.filter(invalidada=False).order_by("-version").first()


def intentar_generar_exportacion(dia: DiaArqueo, usuario) -> tuple["Exportacion | None", str]:
    """Genera los archivos del día al aprobar (Karla, 2026-09-25: «no tiene sentido» un botón aparte). Nunca
    lanza: devuelve (exportación, "") si quedó lista, o (None, motivo) si algo la bloquea — el cierre NO se
    revierte por eso. Si ya hay una versión vigente no crea otra (evita doble versión por doble clic)."""
    vigente = exportacion_vigente(dia)
    if vigente is not None:
        return vigente, ""
    if dia.estado not in (DiaArqueo.Estado.APROBADO, DiaArqueo.Estado.CERRADO):
        return None, ""
    try:
        return generar_exportacion(dia, usuario, automatica=True), ""
    except ExportacionInvalida as exc:
        return None, str(exc)


def generar_exportacion(dia: DiaArqueo, usuario, *, automatica: bool = False) -> Exportacion:
    """Genera los archivos finales del día. A mano exige el permiso GENERAR_EXPORTACION (Jefe o Gerente); cuando el
    sistema la genera solo (al aprobar el día o al resolverse un bloqueo) el día ya fue aprobado por quien sí
    tenía permiso, y el evento lo deja indicado."""
    from nucleo.arqueo_caja_servicio import generar_archivo_arqueo_caja

    if not automatica:
        from nucleo.permisos import Permiso, exigir
        exigir(usuario, Permiso.GENERAR_EXPORTACION, "Tu perfil no puede generar los archivos finales.")
    ventas, anuladas, otros_ingresos, filas, filas_arqueo = _revisar_exportable(dia)

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
    guardar_libro(wb, ruta)

    ruta_arqueo, filas_arqueo = generar_archivo_arqueo_caja(dia, version, CARPETA_EXPORTACIONES, filas_arqueo)

    exportacion = Exportacion.objects.create(
        dia=dia, version=version, archivo=str(ruta), archivo_arqueo=str(ruta_arqueo), generado_por=usuario,
    )

    if dia.estado == DiaArqueo.Estado.CERRADO:
        dia.estado = DiaArqueo.Estado.EXPORTADO
        dia.save(update_fields=["estado"])

    EventoAuditoria.objects.create(
        dia=dia, entidad="Exportacion", entidad_id=str(exportacion.pk), accion="generar_exportacion",
        valor_anterior=None,
        valor_posterior={
            "version": version, "automatica": automatica, "comandas": len(ventas), "anuladas_excluidas": anuladas,
            "archivo": nombre_archivo, "archivo_arqueo": ruta_arqueo.name,
            "lineas_arqueo_caja": len(filas_arqueo),
            "pagos_divididos": [
                {"comanda": p.venta.n_interno, "exportada_como": p.principal, "partes": {m: str(x) for m, x in p.partes.items()}}
                for p in planes_del_dia(dia)
            ],
        },
        usuario=usuario,
    )
    return exportacion
