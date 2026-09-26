"""Menú «Archivos para Odoo» (Karla, 2026-09-24): las cuatro capturas que se cargan
en Odoo, juntas y explicadas, para no tener que buscarlas en cada pantalla.

* Captura Ventas — las boletas del día con su tipo de pago (archivo 1).
* Captura Arqueo Caja — el asiento de diferencias de efectivo y tarjeta (archivo 2).
* Captura Depósito — el depósito en efectivo en el banco.
* Captura Reintegro Caja — el depósito en efectivo en el banco de lo que devuelve la cajera.

Solo lee y arma la lista de lo que está disponible, con el motivo cuando algo está bloqueado;
no genera nada por su cuenta (aprobar y generar la versión del día sigue siendo un paso del cierre).
"""
from __future__ import annotations

from pathlib import Path

from nucleo.depositos_servicio import ControlDepositoInvalido, cuenta_del_banco as cuenta_banco_deposito
from nucleo.depositos_servicio import ruta_captura_generada as ruta_captura_generada_deposito
from nucleo.models import DepositoBancario, DiaArqueo, Exportacion, ReintegroCaja
from nucleo.reintegros_servicio import ReintegroInvalido, cuenta_del_banco as cuenta_banco_reintegro, nombre_captura_reintegro
from nucleo.reintegros_servicio import ruta_captura_generada as ruta_captura_generada_reintegro

# Descripción de cada captura: lo que se muestra en su tarjeta.
CAPTURAS = [
    {
        "clave": "ventas", "numero": 1, "titulo": "Captura Ventas",
        "sirve": "Las boletas del día con su tipo de pago, para cargarlas en Odoo. Es el archivo 1 del cierre.",
        "cuando": "Con el día aprobado: se genera sola al aprobar en «Cierre» (junto con la Captura Arqueo Caja) y aquí se descarga.",
        "contiene": "Una fila por venta: comanda, boleta, cliente, tipo de pago externo y, si el pago fue dividido, el medio principal.",
    },
    {
        "clave": "arqueo", "numero": 2, "titulo": "Captura Arqueo Caja",
        "sirve": "El asiento del día con las diferencias de efectivo y de tarjeta, el ajuste de sencillo y los traspasos por pago dividido.",
        "cuando": "Con el día aprobado: sale sola, junto con la Captura Ventas, al aprobar en «Cierre».",
        "contiene": "Diario, fecha, referencia «Arqueo Caja Día dd/mm/aaaa», cuentas de efectivo, Transbank y pérdida/ganancia, con la cajera como empresa.",
    },
    {
        "clave": "deposito", "numero": 3, "titulo": "Captura Depósito",
        "sirve": "Depósito en Efectivo en Banco: el efectivo del día que se lleva al banco.",
        "cuando": "Desde que ingresas los datos del depósito en Control de Depósitos, sin esperar la validación del Jefe de Contabilidad.",
        "contiene": "Recibir, la empresa, el monto depositado, la fecha del depósito, la cuenta de efectivo, el memo del día de caja y el Diario del banco.",
    },
    {
        "clave": "reintegro", "numero": 4, "titulo": "Captura Reintegro Caja",
        "sirve": "Depósito en Efectivo en Banco: lo que devuelve una cajera por una pérdida de caja.",
        "cuando": "Desde que ingresas los datos del reintegro en Reintegros de Caja, sin esperar la validación del Jefe de Contabilidad.",
        "contiene": "Recibir, la cajera, el monto devuelto, la fecha del arqueo, la cuenta de pérdida por diferencia de efectivo, el memo y el Diario del banco.",
    },
]


def _existe(ruta: str) -> bool:
    return bool(ruta) and Path(ruta).exists()


def capturas_de_ventas_y_arqueo(desde=None, hasta=None) -> dict:
    """Por día: la última versión vigente (no invalidada) con sus dos archivos, y los días
    aprobados o cerrados que todavía no tienen una versión vigente (esos siempre, sin filtro).
    `desde`/`hasta` acotan solo las filas descargables (el archivo en disco se revisa solo en esas)."""
    dias = DiaArqueo.objects.filter(estado__in=(
        DiaArqueo.Estado.APROBADO, DiaArqueo.Estado.CERRADO, DiaArqueo.Estado.EXPORTADO,
    )).order_by("-fecha")
    filas, por_generar, total = [], [], 0
    for dia in dias:
        vigente = dia.exportaciones.filter(invalidada=False).order_by("-version").first()
        if vigente is None:
            por_generar.append(dia)
            continue
        total += 1
        if (desde and dia.fecha < desde) or (hasta and dia.fecha > hasta):
            continue
        filas.append({
            "dia": dia, "fecha": dia.fecha, "exportacion": vigente, "version": vigente.version, "generado_en": vigente.generado_en,
            "acuse": vigente.get_acuse_estado_display(),
            "ventas_disponible": _existe(vigente.archivo), "arqueo_disponible": _existe(vigente.archivo_arqueo),
        })
    return {"filas": filas, "por_generar": por_generar, "total": total}


def capturas_de_deposito() -> list[dict]:
    filas = []
    for d in DepositoBancario.objects.select_related("dia").order_by("-dia__fecha", "-registrado_en"):
        motivo = ""
        if d.estado == DepositoBancario.Estado.PENDIENTE_DEPOSITO or d.monto_depositado is None:
            motivo = "Falta ingresar los datos del depósito (banco, monto, fecha y comprobante)."
        else:
            try:
                cuenta_banco_deposito(d)
            except ControlDepositoInvalido as exc:
                motivo = str(exc)
        filas.append({
            "deposito": d, "fecha": d.dia.fecha, "estado": d.get_estado_display(), "banco": d.banco,
            "monto": d.monto_depositado if d.monto_depositado is not None else d.monto_esperado,
            "disponible": not motivo, "motivo": motivo, "ultima": d.captura_exportada_en,
            "generada": ruta_captura_generada_deposito(d) is not None,
        })
    return filas


def capturas_de_reintegro() -> list[dict]:
    filas = []
    for r in ReintegroCaja.objects.select_related("arqueo_origen", "excepcion_origen", "cajera").order_by("-registrado_en"):
        if r.estado == ReintegroCaja.Estado.REGISTRADO:
            motivo = "Falta completar el detalle del reintegro (banco, fecha, N° de operación y comprobante)."
        else:
            try:
                cuenta_banco_reintegro(r)
                motivo = ""
            except ReintegroInvalido as exc:
                motivo = str(exc)
        filas.append({
            "reintegro": r, "fecha": r.dia.fecha, "cajera": r.cajera.nombre, "estado": r.get_estado_display(),
            "banco": r.banco, "monto": r.monto, "disponible": not motivo, "motivo": motivo,
            "nombre_archivo": nombre_captura_reintegro(r), "ultima": r.exportado_en,
            "generada": ruta_captura_generada_reintegro(r) is not None,
        })
    return sorted(filas, key=lambda f: f["fecha"], reverse=True)


def _en_rango(fecha, desde, hasta) -> bool:
    return (not desde or fecha >= desde) and (not hasta or fecha <= hasta)


def requiere_atencion(ventas: dict, depositos: list[dict], reintegros: list[dict]) -> list[dict]:
    """Lo pendiente o bloqueado de las cuatro capturas, SIN importar el mes que se esté mirando:
    un depósito bloqueado de hace tres meses no puede quedar escondido por el filtro."""
    from django.urls import reverse
    items = []
    from nucleo.exportacion_servicio import motivo_de_bloqueo
    for d in ventas["por_generar"]:
        motivo = motivo_de_bloqueo(d)
        items.append({
            "captura": "Ventas y Arqueo", "fecha": d.fecha,
            "motivo": motivo or "Sin archivos generados: puedes generarlos ahora.",
            "url": reverse("cierre_y_exportacion", args=[d.fecha.isoformat()]), "etiqueta": "Ir a Cierre →" if motivo else "",
            "generar_fecha": None if motivo else d.fecha.isoformat(),
        })
    for f in depositos:
        if not f["disponible"]:
            d = f["deposito"]
            url = (reverse("control_depositos") + f"?deposito={d.pk}") if d.estado == DepositoBancario.Estado.PENDIENTE_DEPOSITO                 else reverse("configuracion") + "#cuentas"
            items.append({"captura": "Depósito", "fecha": f["fecha"], "motivo": f["motivo"], "url": url, "etiqueta": "Resolver →"})
    for f in reintegros:
        if not f["disponible"]:
            r = f["reintegro"]
            origen = f"tipo=arqueo&origen={r.arqueo_origen_id}" if r.arqueo_origen_id else f"tipo=excepcion&origen={r.excepcion_origen_id}"
            items.append({"captura": f"Reintegro · {f['cajera']}", "fecha": f["fecha"], "motivo": f["motivo"],
                          "url": reverse("reintegros_caja") + "?" + origen, "etiqueta": "Resolver →"})
    return sorted(items, key=lambda i: i["fecha"])


def resumen(desde=None, hasta=None) -> dict:
    ventas = capturas_de_ventas_y_arqueo(desde, hasta)
    todos_depositos = capturas_de_deposito()
    todos_reintegros = capturas_de_reintegro()
    depositos = [f for f in todos_depositos if _en_rango(f["fecha"], desde, hasta)]
    reintegros = [f for f in todos_reintegros if _en_rango(f["fecha"], desde, hasta)]
    atencion = requiere_atencion(ventas, todos_depositos, todos_reintegros)
    return {
        "ventas": ventas, "depositos": depositos, "reintegros": reintegros, "atencion": atencion,
        "contadores": {
            "ventas": {"mes": len(ventas["filas"]), "pendientes": len(ventas["por_generar"]), "total": ventas["total"]},
            "deposito": {"mes": len(depositos), "pendientes": sum(1 for f in todos_depositos if not f["disponible"]), "total": len(todos_depositos)},
            "reintegro": {"mes": len(reintegros), "pendientes": sum(1 for f in todos_reintegros if not f["disponible"]), "total": len(todos_reintegros)},
        },
    }
