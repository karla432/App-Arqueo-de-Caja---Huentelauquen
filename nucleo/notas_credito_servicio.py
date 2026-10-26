"""Verificación del medio real de devolución de una nota de crédito (Karla,
2026-09-24; diseñado con los agentes de Control, Contador Senior y Tesorero).

Resto anota la devolución como tarjeta Transbank aunque el dinero se haya
devuelto en efectivo (por norma no se anula el cobro en Transbank), y ni el libro
ni el detalle vinculan la boleta original. Su cierre de caja ya trae la nota
descontada del efectivo, y el libro de ventas sale neto; contabilidad carga la
boleta y la nota por separado en Odoo. Por eso:

* La nota NO resta del efectivo esperado (sería descontarla dos veces) y no lleva
  línea en el asiento Arqueo Caja (decisión de Karla, 2026-09-24).
* La verificación es el respaldo documental (vale firmado por el cliente y
  boleta original), no la que decide si salió efectivo.
* Evidencia cargada (medio real, boleta original, comprobante) por quien
  registra el caso → la confirma el Jefe de Contabilidad (o superior), nunca
  la misma persona que cargó la evidencia (salvo la Gerente, que queda en
  bitácora).
* Sobre el umbral de visado de pérdida ($50.000, configurable) además exige la
  segunda firma de la Gerente de Administración y Finanzas.
* Verificada con medio «efectivo» → recién ahí resta de su caja (el dinero salió
  de un cajón que Resto no descontó). Con otro medio no cambia nada; en Odoo la
  rebaja de ventas e IVA la hace la propia nota.
* Toda nota sin verificar bloquea el cierre del día, sea cual sea el medio que
  Resto anotó, hasta resolverla con evidencia.
"""
from __future__ import annotations

from datetime import timedelta

from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.utils import timezone

from nucleo.permisos import Permiso, exigir
from nucleo.comprobantes import ComprobanteInvalido, validar_comprobante
from nucleo.documentos_servicio import nivel_usuario
from nucleo.models import EventoAuditoria, PagoDeclarado, RegularizacionDocumento, Venta
from nucleo.regularizacion_servicio import RegularizacionInvalida, _guardar_comprobante
from nucleo.umbrales import umbral

V = RegularizacionDocumento.Verificacion
DIAS_HACIA_ATRAS_BOLETAS = 30


def _es_nota(doc: RegularizacionDocumento) -> None:
    if doc.tipo != RegularizacionDocumento.Tipo.NOTA_CREDITO:
        raise RegularizacionInvalida("La verificación de devolución aplica solo a notas de crédito.")


def requiere_segunda_firma(doc: RegularizacionDocumento) -> bool:
    return doc.monto > umbral("umbral_visado_perdida")


def notas_efectivo_sin_verificar(dia=None):
    """Notas de crédito sin evidencia todavía: bloquean el cierre del día, sea cual
    sea el medio que Resto anotó en el detalle (Karla, 2026-09-24: Resto la anota
    como tarjeta Transbank aunque el dinero se devolvió en efectivo, así que ese
    dato no sirve para decidir)."""
    qs = RegularizacionDocumento.objects.filter(
        tipo=RegularizacionDocumento.Tipo.NOTA_CREDITO,
    ).exclude(verificacion_estado=V.VERIFICADA)
    return qs.filter(dia=dia) if dia is not None else qs


ESTADOS_SIN_EVIDENCIA = (V.SIN_VERIFICAR, V.RECHAZADA)


def notas_sin_evidencia(dia=None):
    """Lo que bloquea el CIERRE DEL DÍA (Karla, 2026-10-26): notas sin el respaldo
    cargado (vale firmado y boleta original) o con la evidencia rechazada. La
    verificación del Jefe de Contabilidad y el folio de la nota en Odoo se exigen
    en el cierre de mes: la nota en Odoo necesita la boleta original, que recién
    llega con la Captura de Ventas del día ya cerrado."""
    qs = RegularizacionDocumento.objects.filter(
        tipo=RegularizacionDocumento.Tipo.NOTA_CREDITO, verificacion_estado__in=ESTADOS_SIN_EVIDENCIA,
    )
    return qs.filter(dia=dia) if dia is not None else qs


def dias_habiles_entre(desde, hasta) -> int:
    """Días hábiles (lunes a viernes) transcurridos después de `desde` hasta `hasta`."""
    if hasta <= desde:
        return 0
    dias, actual = 0, desde
    while actual < hasta:
        actual += timedelta(days=1)
        if actual.weekday() < 5:
            dias += 1
    return dias


def notas_atrasadas(hoy=None):
    """Notas de crédito todavía sin verificar después del plazo configurado
    (`dias_alerta_nc`, por defecto 5 días hábiles desde el día de la nota)."""
    from datetime import date
    hoy = hoy or date.today()
    plazo = int(umbral("dias_alerta_nc"))
    return [n for n in notas_efectivo_sin_verificar().select_related("dia") if dias_habiles_entre(n.dia.fecha, hoy) > plazo]


@transaction.atomic
def registrar_folio_odoo(doc: RegularizacionDocumento, folio: str, usuario) -> RegularizacionDocumento:
    """Folio de la nota de crédito creada en Odoo (Karla, 2026-10-26): lo exige el
    cierre de mes. Lo registra contabilidad (Jefe de Contabilidad o Gerente)."""
    exigir(usuario, Permiso.VALIDAR, "Solo el Jefe de Contabilidad o la Gerente registran el folio de Odoo.")
    _es_nota(doc)
    folio = (folio or "").strip()
    if not folio:
        raise RegularizacionInvalida("Escribe el folio de la nota de crédito en Odoo.")
    anterior = doc.folio_odoo
    doc.folio_odoo = folio[:50]
    doc.folio_odoo_por = usuario
    doc.folio_odoo_en = timezone.now()
    doc.save(update_fields=["folio_odoo", "folio_odoo_por", "folio_odoo_en"])
    EventoAuditoria.objects.create(
        dia=doc.dia, entidad="RegularizacionDocumento", entidad_id=str(doc.pk), accion="registrar_folio_odoo_nc",
        valor_anterior={"folio_odoo": anterior}, valor_posterior={"folio_odoo": doc.folio_odoo}, usuario=usuario,
    )
    return doc


def boletas_candidatas(doc: RegularizacionDocumento) -> list[dict]:
    """Boletas que podrían ser la original de la nota: mismo monto y misma
    caja, del mismo día o hasta 30 días antes. Solo SUGERENCIAS — Resto no
    vincula la boleta, así que la persona que resuelve elige (con el PDF de la
    nota, donde el SII exige la referencia)."""
    desde = doc.dia.fecha - timedelta(days=DIAS_HACIA_ATRAS_BOLETAS)
    ventas = (
        Venta.objects.filter(
            total_comanda=doc.monto, caja=doc.caja, dia__fecha__gte=desde, dia__fecha__lte=doc.dia.fecha,
        ).exclude(estado="ANULADA").select_related("dia").prefetch_related("pagos").order_by("-dia__fecha", "-hora_cierre")
    )
    return [
        {
            "boleta": v.nro_comprobante[-7:].lstrip("0") or v.nro_comprobante, "fecha": v.dia.fecha,
            "hora": v.hora_cierre, "medios": ", ".join(p.get_medio_display() for p in v.pagos.all()),
            "mismo_dia": v.dia_id == doc.dia_id,
        }
        for v in ventas[:8]
    ]


@transaction.atomic
def registrar_evidencia(
    doc: RegularizacionDocumento, *, medio: str, boleta_original: str, comentario: str,
    comprobante: UploadedFile | None, usuario,
) -> RegularizacionDocumento:
    exigir(usuario, Permiso.OPERAR, "Tu perfil no puede cargar evidencia.")
    _es_nota(doc)
    if doc.verificacion_estado not in (V.SIN_VERIFICAR, V.RECHAZADA):
        raise RegularizacionInvalida("Esta nota ya tiene evidencia cargada o está verificada.")
    if medio not in RegularizacionDocumento.MedioVerificado.values:
        raise RegularizacionInvalida("Elige el medio real con que se devolvió el dinero.")
    if not comprobante:
        raise RegularizacionInvalida(
            "Adjunta la evidencia (vale firmado por el cliente, reverso de Transbank, comprobante de "
            "transferencia o boleta de reemplazo) — es obligatoria."
        )
    if medio == RegularizacionDocumento.MedioVerificado.SIN_DEVOLUCION and not boleta_original.strip():
        raise RegularizacionInvalida("Indica la boleta de reemplazo o corrección cuando no hubo devolución de dinero.")
    try:
        validar_comprobante(comprobante)
    except ComprobanteInvalido as exc:
        raise RegularizacionInvalida(str(exc))

    anterior = {"verificacion_estado": doc.verificacion_estado, "medio_verificado": doc.medio_verificado}
    doc.medio_verificado = medio
    doc.boleta_original = boleta_original.strip()
    doc.evidencia_comentario = comentario.strip()
    doc.evidencia_archivo = str(_guardar_comprobante(comprobante, doc))
    doc.evidencia_nombre_original = comprobante.name
    doc.evidencia_por = usuario
    doc.evidencia_en = timezone.now()
    doc.verificacion_estado = V.EVIDENCIA_CARGADA
    doc.save(update_fields=[
        "medio_verificado", "boleta_original", "evidencia_comentario", "evidencia_archivo",
        "evidencia_nombre_original", "evidencia_por", "evidencia_en", "verificacion_estado",
    ])
    _bitacora(doc, "registrar_evidencia_nota_credito", anterior, usuario, extra={"boleta_original": doc.boleta_original})
    return doc


def _bitacora(doc, accion, anterior, usuario, extra=None):
    EventoAuditoria.objects.create(
        dia=doc.dia, entidad="RegularizacionDocumento", entidad_id=str(doc.pk), accion=accion,
        valor_anterior=anterior,
        valor_posterior={
            "verificacion_estado": doc.verificacion_estado, "medio_verificado": doc.medio_verificado,
            "folio": doc.folio, "monto": str(doc.monto), **(extra or {}),
        },
        usuario=usuario,
    )


def _cerrar_como_verificada(doc: RegularizacionDocumento, usuario) -> None:
    doc.verificacion_estado = V.VERIFICADA
    doc.estado = RegularizacionDocumento.Estado.REGULARIZADO
    doc.regularizado_por = usuario
    doc.regularizado_en = timezone.now()
    doc.documento_relacionado = doc.boleta_original
    doc.comprobante_archivo = doc.evidencia_archivo
    doc.comprobante_nombre_original = doc.evidencia_nombre_original


@transaction.atomic
def verificar(doc: RegularizacionDocumento, *, aprobar: bool, comentario: str, usuario) -> RegularizacionDocumento:
    """Primera firma (Jefe de Contabilidad o superior)."""
    exigir(usuario, Permiso.VALIDAR, "Tu perfil no puede verificar notas de crédito.")
    _es_nota(doc)
    if doc.verificacion_estado != V.EVIDENCIA_CARGADA:
        raise RegularizacionInvalida("Esta nota no está esperando verificación.")
    nivel = nivel_usuario(usuario)
    if nivel < 1:
        raise RegularizacionInvalida("Solo el Jefe de Contabilidad o un rol superior puede verificar una devolución.")
    if doc.evidencia_por_id == usuario.pk and nivel < 2:
        raise RegularizacionInvalida(
            "Quien cargó la evidencia no puede verificarla: debe hacerlo otra persona (o la Gerente, que queda en bitácora)."
        )
    if not aprobar and not comentario.strip():
        raise RegularizacionInvalida("Al rechazar, indica por qué la evidencia no alcanza.")

    anterior = {"verificacion_estado": doc.verificacion_estado, "medio_verificado": doc.medio_verificado}
    doc.verificado_por = usuario
    doc.verificado_en = timezone.now()
    doc.verificacion_comentario = comentario.strip()
    if not aprobar:
        doc.verificacion_estado = V.RECHAZADA
    elif requiere_segunda_firma(doc) and nivel < 2:
        doc.verificacion_estado = V.PENDIENTE_SEGUNDA_FIRMA
    else:
        # Bajo el umbral, o una Gerente: su firma vale como primera y segunda.
        if requiere_segunda_firma(doc):
            doc.segunda_firma_por, doc.segunda_firma_en = usuario, timezone.now()
        _cerrar_como_verificada(doc, usuario)
    doc.save()
    _bitacora(doc, "verificar_nota_credito" if aprobar else "rechazar_evidencia_nota_credito", anterior, usuario,
              extra={"comentario": comentario.strip()})
    return doc


@transaction.atomic
def segunda_firma(doc: RegularizacionDocumento, *, aprobar: bool, comentario: str, usuario) -> RegularizacionDocumento:
    """Segunda firma de la Gerente (solo sobre el umbral de visado)."""
    exigir(usuario, Permiso.AUTORIZAR_EXCEPCION, "La segunda firma corresponde a la Gerente de Administración y Finanzas.")
    _es_nota(doc)
    if doc.verificacion_estado != V.PENDIENTE_SEGUNDA_FIRMA:
        raise RegularizacionInvalida("Esta nota no está esperando la segunda firma.")
    if nivel_usuario(usuario) < 2:
        raise RegularizacionInvalida("La segunda firma corresponde a la Gerente de Administración y Finanzas.")
    if not aprobar and not comentario.strip():
        raise RegularizacionInvalida("Al rechazar, indica por qué.")

    anterior = {"verificacion_estado": doc.verificacion_estado, "medio_verificado": doc.medio_verificado}
    doc.segunda_firma_por = usuario
    doc.segunda_firma_en = timezone.now()
    if aprobar:
        _cerrar_como_verificada(doc, usuario)
    else:
        doc.verificacion_estado = V.RECHAZADA
        doc.verificacion_comentario = comentario.strip()
    doc.save()
    _bitacora(doc, "segunda_firma_nota_credito" if aprobar else "rechazar_segunda_firma_nota_credito", anterior, usuario,
              extra={"comentario": comentario.strip()})
    return doc
