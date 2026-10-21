"""Volver a abrir una excepción ya resuelta (Karla, 2026-10-21).

Caso que lo originó: la Encargada de Ingresos aceptó cobros de Transbank como
"otros ingresos" (forma de pago sin boleta) y después hubo dudas de si en
realidad eran ventas con comanda, pagadas con dos tarjetas o declaradas en otro
medio. Hasta ahora una resolución humana no se podía deshacer: la conciliación
la respeta a propósito (auditoría QA 2026-09-18) y la administración técnica es
de solo consulta. Esta es la única puerta para corregirla, y es controlada:

* Solo el Jefe de Contabilidad o la Gerente (permiso REABRIR_EXCEPCION).
* Solo en días que no están aprobados, cerrados ni exportados (para esos,
  primero se reabre el día — facultad de la Gerente).
* Motivo obligatorio. La bitácora guarda la resolución completa que se deshizo
  (motivo, comentario, quién, cuándo, líneas, reclasificaciones, vínculos y
  pérdidas/ganancias), así que nada se pierde aunque los registros derivados se
  eliminen para que dejen de afectar el arqueo y la exportación.
* No se reabre un ajuste de sencillo (lo genera el motor en cada corrida), ni un
  "otros ingresos" que ya tiene boleta asignada (la boleta ya se emitió), ni una
  excepción que se cerró arrastrada por otra (se reabre la que la arrastró).

Al volver a abrirla, la excepción queda igual que recién salida de la
conciliación, lista para resolverse de la forma correcta.
"""
from django.db import transaction

from nucleo.models import (
    Coincidencia, DiaArqueo, EventoAuditoria, Excepcion,
)
from nucleo.permisos import Permiso, exigir

ESTADOS_CERRADOS = (
    DiaArqueo.Estado.APROBADO, DiaArqueo.Estado.CERRADO, DiaArqueo.Estado.EXPORTADO,
)
ACCION_ARRASTRADA = "resolver_excepcion:vinculada_con_pago"


class ReaperturaInvalida(Exception):
    pass


def _texto(valor) -> str:
    return "" if valor is None else str(valor)


def _arrastrada_por(excepcion: Excepcion) -> int | None:
    """Si esta excepción se cerró sola al resolver otra (movimiento sin pago
    juntado con un pago), devuelve el id de la que la arrastró."""
    evento = (
        EventoAuditoria.objects.filter(
            dia=excepcion.dia, entidad="Excepcion", entidad_id=str(excepcion.pk), accion=ACCION_ARRASTRADA,
        ).order_by("-id").first()
    )
    if evento is None:
        return None
    return (evento.valor_posterior or {}).get("junto_con")


def _arrastradas_por(excepcion: Excepcion) -> list[Excepcion]:
    ids = [
        int(e.entidad_id)
        for e in EventoAuditoria.objects.filter(dia=excepcion.dia, entidad="Excepcion", accion=ACCION_ARRASTRADA)
        if (e.valor_posterior or {}).get("junto_con") == excepcion.pk
    ]
    return list(Excepcion.objects.filter(pk__in=ids, abierta=False))


def motivo_para_no_reabrir(excepcion: Excepcion) -> str:
    """Vacío si se puede volver a abrir; si no, la razón en lenguaje de negocio."""
    if excepcion.abierta:
        return "Esta excepción ya está abierta."
    if excepcion.dia.estado in ESTADOS_CERRADOS:
        return "El día está cerrado: para corregir una excepción, primero hay que reabrir el día."
    if excepcion.resolucion == Excepcion.Resolucion.AJUSTE_SENCILLO:
        return "Los ajustes de sencillo los calcula la conciliación; se recalculan solos al reconciliar."
    if excepcion.factura_asociada:
        return (
            f"Ya tiene asignada la boleta {excepcion.factura_asociada}: la boleta se emitió, "
            "así que no se puede deshacer desde aquí."
        )
    origen = _arrastrada_por(excepcion)
    if origen:
        return f"Se cerró junto con la excepción #{origen}: vuelve a abrir esa y esta se abre con ella."
    return ""


def _limpiar_resolucion(excepcion: Excepcion) -> None:
    if excepcion.tipo == Excepcion.Tipo.PAGO_SIN_MOVIMIENTO:
        # El motor crea el "pago sin movimiento" sin movimiento; si tiene uno
        # es porque lo puso la resolución (vincular / pérdida-ganancia).
        excepcion.movimiento = None
    if excepcion.tipo in (Excepcion.Tipo.MOVIMIENTO_SIN_PAGO, Excepcion.Tipo.PAGO_DUPLICADO):
        # Y al revés: el cobro sin boleta nace sin pago; si tiene uno es
        # porque lo puso «corregir el medio de la boleta».
        excepcion.pago = None
    excepcion.abierta = True
    excepcion.resolucion = ""
    excepcion.motivo = None
    excepcion.comentario = ""
    excepcion.resuelta_por = None
    excepcion.resuelta_en = None
    excepcion.cuenta_contable = None
    excepcion.cajera_responsable = None
    excepcion.monto_perdida_ganancia = 0
    excepcion.visado_por = None
    excepcion.visado_en = None
    excepcion.asiento_referencia = ""
    excepcion.save()


def _foto(excepcion: Excepcion) -> dict:
    return {
        "tipo": excepcion.tipo,
        "resolucion": excepcion.resolucion,
        "motivo": excepcion.motivo.codigo if excepcion.motivo_id else "",
        "comentario": excepcion.comentario,
        "resuelta_por": excepcion.resuelta_por.username if excepcion.resuelta_por_id else "",
        "resuelta_en": _texto(excepcion.resuelta_en),
        "pago_id": excepcion.pago_id,
        "movimiento_id": excepcion.movimiento_id,
        "cuenta_contable": excepcion.cuenta_contable.codigo if excepcion.cuenta_contable_id else "",
        "cajera_responsable": excepcion.cajera_responsable.nombre if excepcion.cajera_responsable_id else "",
        "monto_perdida_ganancia": _texto(excepcion.monto_perdida_ganancia),
        "visado_por": excepcion.visado_por.username if excepcion.visado_por_id else "",
        "asiento_referencia": excepcion.asiento_referencia,
    }


@transaction.atomic
def reabrir_excepcion(excepcion: Excepcion, usuario, motivo: str) -> Excepcion:
    exigir(usuario, Permiso.REABRIR_EXCEPCION, "Solo el Jefe de Contabilidad o la Gerente pueden volver a abrir una excepción.")
    excepcion = Excepcion.objects.select_for_update().select_related("dia").get(pk=excepcion.pk)
    razon = motivo_para_no_reabrir(excepcion)
    if razon:
        raise ReaperturaInvalida(razon)
    if not (motivo or "").strip():
        raise ReaperturaInvalida("Volver a abrir una excepción exige indicar el motivo.")

    dia = excepcion.dia
    valor_anterior = _foto(excepcion)

    # Vínculos manuales que creó la resolución (solo existen cuando hay un pago del POS).
    coincidencias = set()
    if excepcion.pago_id:
        coincidencias |= set(
            Coincidencia.objects.filter(dia=dia, origen=Coincidencia.Origen.MANUAL, pagos=excepcion.pago_id)
        )
    if excepcion.movimiento_id and excepcion.resolucion in (
        Excepcion.Resolucion.AGRUPAR, Excepcion.Resolucion.CORREGIR_MEDIO,
    ):
        # Resuelta desde el cobro: el vínculo cuelga del movimiento.
        coincidencias |= set(
            Coincidencia.objects.filter(dia=dia, origen=Coincidencia.Origen.MANUAL, movimientos=excepcion.movimiento_id)
        )
    coincidencias = sorted(coincidencias, key=lambda c: c.pk)
    valor_anterior["vinculos"] = [
        {"pagos": list(c.pagos.values_list("id", flat=True)), "movimientos": list(c.movimientos.values_list("id", flat=True))}
        for c in coincidencias
    ]

    # Líneas de "mezcla de medios" / pago con varios cobros.
    lineas = list(excepcion.lineas_conciliacion.all())
    valor_anterior["lineas"] = [
        {"medio": l.medio_conciliado, "monto": _texto(l.monto_conciliado), "movimiento_id": l.movimiento_id}
        for l in lineas
    ]

    # Reclasificaciones de forma de pago hechas por esta resolución: dejan de
    # afectar el efectivo esperado del arqueo.
    reclasificaciones = list(excepcion.reclasificaciones.select_related("pago"))
    valor_anterior["reclasificaciones"] = [
        {"pago_id": r.pago_id, "medio_original": r.medio_original, "medio_corregido": r.medio_corregido,
         "monto": _texto(r.monto), "regla": r.regla}
        for r in reclasificaciones
    ]
    for r in reclasificaciones:
        if r.pago.medio_aprobado and r.pago.medio_aprobado == r.medio_corregido:
            r.pago.medio_aprobado = ""
            r.pago.save(update_fields=["medio_aprobado"])

    arrastradas = _arrastradas_por(excepcion)
    valor_anterior["arrastradas"] = [e.pk for e in arrastradas]

    for c in coincidencias:
        c.delete()
    for l in lineas:
        l.delete()
    for r in reclasificaciones:
        r.delete()

    _limpiar_resolucion(excepcion)
    for hermana in arrastradas:
        _limpiar_resolucion(hermana)
        EventoAuditoria.objects.create(
            dia=dia, entidad="Excepcion", entidad_id=str(hermana.pk), accion="reabrir_excepcion:arrastrada",
            valor_anterior={"abierta": False, "junto_con": excepcion.pk},
            valor_posterior={"abierta": True, "motivo": motivo.strip()},
            usuario=usuario,
        )

    EventoAuditoria.objects.create(
        dia=dia, entidad="Excepcion", entidad_id=str(excepcion.pk), accion="reabrir_excepcion",
        valor_anterior=valor_anterior,
        valor_posterior={"abierta": True, "motivo": motivo.strip()},
        usuario=usuario,
    )

    from nucleo.arqueo_servicio import preparar_arqueos_del_dia
    from nucleo.resolucion_servicio import sincronizar_estado_del_dia
    preparar_arqueos_del_dia(dia)  # el efectivo esperado deja de contar las reclasificaciones deshechas
    sincronizar_estado_del_dia(dia)
    return excepcion
