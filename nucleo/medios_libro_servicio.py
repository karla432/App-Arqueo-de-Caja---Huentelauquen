"""Completar la forma de pago de comandas ya cargadas desde el libro contable (Karla, 2026-11-04).

Caso real del 27-10-2026: la heladería cobra solo con Redelcom (terminal Point de Mercado Pago) y VENTAS POR
ARTICULO no trae columna para ese medio, así que 15 comandas ($90.670) quedaron sin forma de pago y sus cobros
de Mercado Pago, como «movimiento sin pago». El libro contable sí las informa (cuenta REDELCOM).

Desde esta versión la carga toma esos medios del libro sola, y rechaza un archivo con comandas sin forma de pago.
Para los días cargados antes, esta acción lee el libro contable guardado del día, crea los pagos que faltan
(sin tocar los que ya existen) y vuelve a conciliar.
"""
from pathlib import Path

import pandas as pd
from django.db import transaction

from arqueo_diario.importadores.comun import fechas_normalizadas
from arqueo_diario.importadores.pos import MEDIOS_PAGO, _documentos_del_libro, pagos_del_libro
from nucleo.models import DiaArqueo, EventoAuditoria, LoteCarga, PagoDeclarado, RegularizacionDocumento, Venta
from nucleo.regularizacion_servicio import desglose_declarado_por_resto
from nucleo.permisos import Permiso, exigir


class CompletarInvalido(Exception):
    pass


def comandas_sin_forma_de_pago(dia: DiaArqueo) -> list[Venta]:
    ventas = Venta.objects.filter(dia=dia).exclude(estado="ANULADA").exclude(tipo_doc="DES").prefetch_related("pagos")
    return [v for v in ventas if abs(sum((p.monto for p in v.pagos.all()), 0) - v.total_comanda) > 1]


def guias_sin_desglose(dia: DiaArqueo) -> list[RegularizacionDocumento]:
    """Guías con pago mixto cargadas antes del 04-11-2026: sin forma de pago única ni desglose."""
    return list(RegularizacionDocumento.objects.filter(
        dia=dia, tipo=RegularizacionDocumento.Tipo.GUIA, forma_pago="", desglose_pagos={}))


def _lote_pos(dia):
    return LoteCarga.objects.filter(dia=dia, fuente=LoteCarga.Fuente.POS).order_by("-cargado_en").first()


@transaction.atomic
def completar_desde_libro(dia: DiaArqueo, usuario) -> dict:
    exigir(usuario, Permiso.OPERAR, "Tu perfil no puede corregir la carga del día.")
    if dia.estado in (DiaArqueo.Estado.APROBADO, DiaArqueo.Estado.CERRADO, DiaArqueo.Estado.EXPORTADO):
        raise CompletarInvalido("El día ya está aprobado o cerrado: reábrelo antes de corregir su carga.")
    lote = _lote_pos(dia)
    if not lote or not lote.ruta_almacenamiento_secundario or not Path(lote.ruta_almacenamiento_secundario).exists():
        raise CompletarInvalido("No está el libro contable de este día en el servidor: vuelve a cargar los archivos del POS.")
    libro = pd.read_excel(lote.ruta_almacenamiento_secundario, sheet_name=0, engine="openpyxl")
    libro = libro[fechas_normalizadas(libro["FEC_COMPR"]) == pd.Timestamp(dia.fecha)]
    medios = pagos_del_libro(libro)
    conocidos = set(MEDIOS_PAGO.values())

    completadas, sin_resolver = [], []
    for venta in comandas_sin_forma_de_pago(dia):
        del_libro = medios.get(venta.nro_comprobante) or {}
        actuales = {p.medio: float(p.monto) for p in venta.pagos.all()}
        faltan = {m: x for m, x in del_libro.items() if m not in actuales}
        coherente = (
            del_libro and all(m in conocidos for m in del_libro)
            and abs(sum(del_libro.values()) - float(venta.total_comanda)) <= 1
            and all(abs(del_libro.get(m, -1) - x) <= 1 for m, x in actuales.items())
        )
        if not coherente or not faltan:
            sin_resolver.append(venta.n_interno)
            continue
        for medio, monto in faltan.items():
            PagoDeclarado.objects.create(venta=venta, medio=medio, monto=monto)
        completadas.append({"comanda": venta.n_interno, "caja": venta.caja, "medios": faltan})

    guias = []
    pendientes_guia = {g.n_interno: g for g in guias_sin_desglose(dia)}
    if pendientes_guia:
        documentos, _ = _documentos_del_libro(libro)
        for doc in documentos:
            guia = pendientes_guia.get(doc.n_interno)
            desglose = desglose_declarado_por_resto(doc) if guia else {}
            if guia and desglose and abs(sum(desglose.values()) - float(guia.monto)) <= 1:
                guia.desglose_pagos = desglose
                guia.save(update_fields=["desglose_pagos"])
                guias.append({"guia": guia.folio, "desglose": desglose})

    if guias:
        EventoAuditoria.objects.create(
            dia=dia, entidad="RegularizacionDocumento", entidad_id=",".join(g["guia"] for g in guias),
            accion="completar_desglose_guias_desde_libro", valor_anterior=None, valor_posterior={"guias": guias},
            usuario=usuario,
        )
    if completadas:
        listas = {str(c["comanda"]) for c in completadas}
        lote.avisos = [a for a in (lote.avisos or [])
                       if not ("suma de medios" in a and a.split(":")[0].replace("Comanda", "").strip() in listas)]
        lote.save(update_fields=["avisos"])
        EventoAuditoria.objects.create(
            dia=dia, entidad="LoteCarga", entidad_id=str(lote.pk), accion="completar_medios_desde_libro",
            valor_anterior=None,
            valor_posterior={"comandas": completadas, "sin_resolver": sin_resolver}, usuario=usuario,
        )
    return {"completadas": completadas, "sin_resolver": sin_resolver, "guias": guias}
