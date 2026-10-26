"""Cierre de mes (Karla, 2026-10-26).

Reúne lo que no se exige día a día y deja el mes cerrado:

* Días del mes con datos: todos exportados a Odoo (bloquea).
* Notas de crédito del mes: verificadas por el Jefe de Contabilidad (bloquea) y con
  el folio de la nota creada en Odoo (bloquea). Antes la verificación se exigía en
  el cierre diario; la nota en Odoo necesita la boleta original, que llega con la
  Captura de Ventas del día ya cerrado, así que esa exigencia pasa al mes.
* Depósitos bancarios de los días del mes: aprobados (bloquea).
* Reintegros de cajeras originados en el mes sin validar (aviso: un reintegro se
  puede pagar en cuotas durante varios meses).
* Días del mes sin datos cargados (aviso).

Lo cierra el Jefe de Contabilidad (permiso CERRAR_MES); lo reabre la Gerente con
motivo. Con el mes cerrado no se puede reabrir ningún día de ese mes.
"""
import calendar
from datetime import date

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from nucleo.models import (
    CierreMes, DepositoBancario, DiaArqueo, EventoAuditoria, RegularizacionDocumento, ReintegroCaja,
)
from nucleo.permisos import Permiso, exigir


class CierreMesInvalido(Exception):
    pass


def rango(anio: int, mes: int) -> tuple[date, date]:
    return date(anio, mes, 1), date(anio, mes, calendar.monthrange(anio, mes)[1])


def mes_cerrado(anio: int, mes: int) -> bool:
    return CierreMes.objects.filter(anio=anio, mes=mes, cerrado=True).exists()


def dia_en_mes_cerrado(dia: DiaArqueo) -> bool:
    return mes_cerrado(dia.fecha.year, dia.fecha.month)


def revision(anio: int, mes: int, hoy: date | None = None) -> dict:
    hoy = hoy or date.today()
    desde, hasta = rango(anio, mes)
    dias = DiaArqueo.objects.filter(fecha__range=(desde, hasta))
    sin_exportar = list(dias.exclude(estado__in=(DiaArqueo.Estado.SIN_CARGAR, DiaArqueo.Estado.EXPORTADO)).order_by("fecha"))
    con_datos = set(dias.exclude(estado=DiaArqueo.Estado.SIN_CARGAR).values_list("fecha", flat=True))
    ultimo = min(hasta, hoy)
    sin_cargar = [date(anio, mes, d) for d in range(1, ultimo.day + 1) if date(anio, mes, d) not in con_datos] \
        if desde <= ultimo else []

    notas = RegularizacionDocumento.objects.filter(
        tipo=RegularizacionDocumento.Tipo.NOTA_CREDITO, dia__fecha__range=(desde, hasta),
    ).select_related("dia").order_by("dia__fecha", "folio")
    notas_sin_verificar = [n for n in notas if n.verificacion_estado != RegularizacionDocumento.Verificacion.VERIFICADA]
    notas_sin_folio = [n for n in notas if not n.folio_odoo]

    depositos = list(
        DepositoBancario.objects.filter(dia__fecha__range=(desde, hasta))
        .exclude(estado=DepositoBancario.Estado.APROBADO).select_related("dia").order_by("dia__fecha")
    )
    reintegros = list(
        ReintegroCaja.objects.filter(
            Q(arqueo_origen__dia__fecha__range=(desde, hasta)) | Q(excepcion_origen__dia__fecha__range=(desde, hasta))
        ).exclude(estado=ReintegroCaja.Estado.VALIDADO).select_related("cajera").distinct()
    )

    controles = [
        {"clave": "dias", "titulo": "Días exportados a Odoo", "bloqueante": True, "ok": not sin_exportar,
         "detalle": "Todos los días con datos están exportados" if not sin_exportar
         else f"{len(sin_exportar)} día(s) sin exportar: " + ", ".join(f"{d.fecha:%d} ({d.get_estado_display()})" for d in sin_exportar)},
        {"clave": "nc_verificadas", "titulo": "Notas de crédito verificadas", "bloqueante": True, "ok": not notas_sin_verificar,
         "detalle": f"{notas.count()} nota(s) del mes, todas verificadas" if not notas_sin_verificar
         else f"{len(notas_sin_verificar)} sin verificar: " + ", ".join(f"{n.folio[-4:]} ({n.dia.fecha:%d-%m})" for n in notas_sin_verificar)},
        {"clave": "nc_folio", "titulo": "Folio de Odoo de las notas de crédito", "bloqueante": True, "ok": not notas_sin_folio,
         "detalle": "Todas tienen su folio de Odoo" if not notas_sin_folio
         else f"{len(notas_sin_folio)} sin folio de Odoo: " + ", ".join(n.folio[-4:] for n in notas_sin_folio)},
        {"clave": "depositos", "titulo": "Depósitos bancarios aprobados", "bloqueante": True, "ok": not depositos,
         "detalle": "Todos los depósitos del mes están aprobados" if not depositos
         else f"{len(depositos)} depósito(s) sin aprobar: " + ", ".join(f"{d.dia.fecha:%d-%m} ({d.get_estado_display()})" for d in depositos)},
        {"clave": "reintegros", "titulo": "Reintegros de cajeras", "bloqueante": False, "ok": not reintegros,
         "detalle": "Sin reintegros pendientes del mes" if not reintegros
         else f"{len(reintegros)} reintegro(s) sin validar (no bloquea: pueden pagarse en cuotas)"},
        {"clave": "sin_cargar", "titulo": "Días sin datos cargados", "bloqueante": False, "ok": not sin_cargar,
         "detalle": "Todos los días hasta hoy tienen datos" if not sin_cargar
         else f"{len(sin_cargar)} día(s) sin datos: " + ", ".join(f"{d:%d}" for d in sin_cargar) + " (no bloquea si la sala no abrió)"},
    ]
    cierre = CierreMes.objects.filter(anio=anio, mes=mes).select_related("cerrado_por", "reabierto_por").first()
    faltan = [c for c in controles if c["bloqueante"] and not c["ok"]]
    return {
        "anio": anio, "mes": mes, "desde": desde, "hasta": hasta, "controles": controles, "faltan": faltan,
        "cierre": cierre, "cerrado": bool(cierre and cierre.cerrado), "mes_terminado": hasta < hoy,
        "notas": list(notas), "dias_con_datos": len(con_datos),
    }


@transaction.atomic
def cerrar_mes(anio: int, mes: int, usuario, hoy: date | None = None) -> CierreMes:
    exigir(usuario, Permiso.CERRAR_MES, "Solo el Jefe de Contabilidad cierra el mes.")
    r = revision(anio, mes, hoy)
    if r["cerrado"]:
        raise CierreMesInvalido("Este mes ya está cerrado.")
    if not r["mes_terminado"]:
        raise CierreMesInvalido("El mes todavía no termina: se cierra a partir del día siguiente a su último día.")
    if r["faltan"]:
        raise CierreMesInvalido("Quedan pendientes que bloquean el cierre: " + "; ".join(f["titulo"] for f in r["faltan"]) + ".")
    cierre, _ = CierreMes.objects.update_or_create(anio=anio, mes=mes, defaults={
        "cerrado": True, "cerrado_por": usuario, "cerrado_en": timezone.now(),
    })
    EventoAuditoria.objects.create(
        dia=None, entidad="CierreMes", entidad_id=f"{anio}-{mes:02d}", accion="cerrar_mes",
        valor_anterior=None, valor_posterior={"anio": anio, "mes": mes, "dias_con_datos": r["dias_con_datos"],
                                              "notas_credito": len(r["notas"])},
        usuario=usuario,
    )
    return cierre


@transaction.atomic
def reabrir_mes(anio: int, mes: int, usuario, motivo: str) -> CierreMes:
    exigir(usuario, Permiso.REABRIR_DIA, "Solo la Gerente de Administración y Finanzas puede reabrir un mes.")
    cierre = CierreMes.objects.filter(anio=anio, mes=mes, cerrado=True).first()
    if not cierre:
        raise CierreMesInvalido("Este mes no está cerrado.")
    if not (motivo or "").strip():
        raise CierreMesInvalido("Reabrir un mes exige indicar el motivo.")
    cierre.cerrado = False
    cierre.reabierto_por = usuario
    cierre.reabierto_en = timezone.now()
    cierre.motivo_reapertura = motivo.strip()
    cierre.save()
    EventoAuditoria.objects.create(
        dia=None, entidad="CierreMes", entidad_id=f"{anio}-{mes:02d}", accion="reabrir_mes",
        valor_anterior={"cerrado": True}, valor_posterior={"cerrado": False, "motivo": motivo.strip()}, usuario=usuario,
    )
    return cierre
