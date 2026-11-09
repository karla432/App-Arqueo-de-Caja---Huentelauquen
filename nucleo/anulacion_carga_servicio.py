"""Anular la carga de un día (Karla, 2026-11-09).

Caso real del 05-11-2026: la Encargada cargó mal los archivos de ventas, Transbank y Mercado Pago y no había
forma de reemplazarlos (la carga rechaza un archivo distinto para un día que ya tiene uno: CargaEnConflicto).

Flujo con doble firma:
1. La Encargada (permiso OPERAR) solicita la anulación: elige las fuentes y escribe el motivo.
2. El Jefe de Contabilidad o la Gerente (permiso VALIDAR), que no puede ser quien la pidió, la aprueba o la
   rechaza. Al aprobar se ejecuta en una sola transacción.

Qué borra al ejecutar (Karla: «prefiero que se borre todo»):
- la conciliación del día (corridas, coincidencias, reclasificaciones y excepciones con sus líneas), porque
  apunta a los pagos y cobros que se borran;
- los movimientos y las comandas de los lotes de las fuentes elegidas, y esos lotes;
- si se anula el POS: las guías y notas de crédito del día y el arqueo de efectivo de cada caja (gastos y
  conteo por denominación incluidos), porque todo eso se calcula sobre las ventas.
No borra la bitácora (queda un evento «anular_carga» con el resumen de lo borrado), ni los archivos originales
del servidor, ni depósitos ni exportaciones. Solo en días que no estén aprobados, cerrados ni exportados.
"""
from django.db import transaction
from django.db.models import ProtectedError, Sum
from django.utils import timezone

from nucleo.models import (
    ArqueoEfectivo, Coincidencia, DiaArqueo, EventoAuditoria, Excepcion, LoteCarga, MovimientoProveedor,
    Reclasificacion, RegularizacionDocumento, ReintegroCaja, SolicitudAnulacionCarga, Venta, VersionMotor,
)
from nucleo.permisos import Permiso, exigir

FUENTES_ANULABLES = (LoteCarga.Fuente.POS, LoteCarga.Fuente.TRANSBANK, LoteCarga.Fuente.MERCADO_PAGO)
ESTADOS_BLOQUEADOS = (DiaArqueo.Estado.APROBADO, DiaArqueo.Estado.CERRADO, DiaArqueo.Estado.EXPORTADO)


class AnulacionInvalida(Exception):
    pass


def _validar_dia(dia: DiaArqueo) -> None:
    if dia.estado in ESTADOS_BLOQUEADOS:
        raise AnulacionInvalida("El día ya está aprobado o cerrado: la Gerente debe reabrirlo antes de anular su carga.")


def pendiente_del_dia(dia: DiaArqueo) -> SolicitudAnulacionCarga | None:
    return dia.anulaciones_carga.filter(estado=SolicitudAnulacionCarga.Estado.PENDIENTE).first()


def fuentes_cargadas(dia: DiaArqueo) -> list[str]:
    cargadas = set(dia.lotes.values_list("fuente", flat=True))
    return [f.value for f in FUENTES_ANULABLES if f.value in cargadas]


def solicitar(dia: DiaArqueo, fuentes, motivo: str, usuario) -> SolicitudAnulacionCarga:
    exigir(usuario, Permiso.OPERAR, "Tu perfil no puede solicitar la anulación de una carga.")
    _validar_dia(dia)
    motivo = (motivo or "").strip()
    if not motivo:
        raise AnulacionInvalida("Escribe el motivo de la anulación (qué se cargó mal).")
    fuentes = [f for f in dict.fromkeys(fuentes or []) if f]
    if not fuentes:
        raise AnulacionInvalida("Marca al menos un archivo para anular.")
    if any(f not in FUENTES_ANULABLES for f in fuentes):
        raise AnulacionInvalida("Fuente no válida.")
    sin_carga = [f for f in fuentes if f not in fuentes_cargadas(dia)]
    if sin_carga:
        nombres = dict(LoteCarga.Fuente.choices)
        raise AnulacionInvalida("No hay carga de " + ", ".join(nombres[f] for f in sin_carga) + " en este día.")
    if pendiente_del_dia(dia):
        raise AnulacionInvalida("Ya hay una solicitud de anulación pendiente para este día.")
    solicitud = SolicitudAnulacionCarga.objects.create(dia=dia, fuentes=fuentes, motivo=motivo, solicitada_por=usuario)
    EventoAuditoria.objects.create(
        dia=dia, entidad="SolicitudAnulacionCarga", entidad_id=str(solicitud.pk), accion="solicitar_anulacion_carga",
        valor_anterior=None, valor_posterior={"fuentes": fuentes, "motivo": motivo}, usuario=usuario,
    )
    return solicitud


def _validar_resolucion(solicitud: SolicitudAnulacionCarga, usuario, accion: str) -> None:
    exigir(usuario, Permiso.VALIDAR, f"Solo el Jefe de Contabilidad o la Gerente pueden {accion} una anulación de carga.")
    if solicitud.estado != SolicitudAnulacionCarga.Estado.PENDIENTE:
        raise AnulacionInvalida("Esta solicitud ya fue resuelta.")
    if solicitud.solicitada_por_id == usuario.pk:
        raise AnulacionInvalida(f"Quien solicita la anulación no puede {accion}la: debe hacerlo otra persona.")


def rechazar(solicitud: SolicitudAnulacionCarga, usuario, comentario: str) -> SolicitudAnulacionCarga:
    _validar_resolucion(solicitud, usuario, "rechazar")
    comentario = (comentario or "").strip()
    if not comentario:
        raise AnulacionInvalida("Escribe por qué se rechaza la anulación.")
    solicitud.estado = SolicitudAnulacionCarga.Estado.RECHAZADA
    solicitud.resuelta_por, solicitud.resuelta_en, solicitud.comentario_resolucion = usuario, timezone.now(), comentario
    solicitud.save()
    EventoAuditoria.objects.create(
        dia=solicitud.dia, entidad="SolicitudAnulacionCarga", entidad_id=str(solicitud.pk), accion="rechazar_anulacion_carga",
        valor_anterior={"estado": "pendiente"}, valor_posterior={"estado": "rechazada", "comentario": comentario}, usuario=usuario,
    )
    return solicitud


def _resumen_lote(lote: LoteCarga) -> dict:
    r = {
        "lote": lote.pk, "fuente": lote.fuente, "archivo": lote.nombre_archivo, "huella": lote.huella_sha256,
        "archivo_secundario": lote.nombre_archivo_secundario or "", "ruta": lote.ruta_almacenamiento,
        "cargado_por": str(lote.cargado_por), "cargado_en": lote.cargado_en.isoformat(timespec="minutes"),
    }
    if lote.fuente == LoteCarga.Fuente.POS:
        ventas = lote.ventas.all()
        r["comandas"] = ventas.count()
        r["total"] = float(ventas.aggregate(t=Sum("total_comanda"))["t"] or 0)
    else:
        movimientos = lote.movimientos.all()
        r["movimientos"] = movimientos.count()
        r["total"] = float(movimientos.aggregate(t=Sum("monto_original"))["t"] or 0)
    return r


def aprobar(solicitud: SolicitudAnulacionCarga, usuario, comentario: str = "") -> SolicitudAnulacionCarga:
    _validar_resolucion(solicitud, usuario, "aprobar")
    try:
        return _ejecutar(solicitud, usuario, (comentario or "").strip())
    except ProtectedError:
        raise AnulacionInvalida("La carga tiene registros usados fuera de este día (por ejemplo, en otra conciliación); "
                                "no se puede anular desde la pantalla. Avisa a soporte.")


@transaction.atomic
def _ejecutar(solicitud: SolicitudAnulacionCarga, usuario, comentario: str) -> SolicitudAnulacionCarga:
    dia = DiaArqueo.objects.select_for_update().get(pk=solicitud.dia_id)
    _validar_dia(dia)
    fuentes = list(solicitud.fuentes)
    con_pos = LoteCarga.Fuente.POS in fuentes
    lotes = list(dia.lotes.filter(fuente__in=fuentes))
    if not lotes:
        raise AnulacionInvalida("Esos archivos ya no están cargados en el día.")

    if ReintegroCaja.objects.filter(excepcion_origen__dia=dia).exists() or (
            con_pos and ReintegroCaja.objects.filter(arqueo_origen__dia=dia).exists()):
        raise AnulacionInvalida("Hay reintegros de cajera registrados sobre este día: deben anularse antes de borrar la carga.")

    resumen = {"lotes": [_resumen_lote(l) for l in lotes], "estado_anterior": dia.estado}

    # 1. Conciliación del día (apunta a pagos y cobros).
    resumen["excepciones"] = Excepcion.objects.filter(dia=dia).count()
    resumen["coincidencias"] = Coincidencia.objects.filter(dia=dia).count()
    RegularizacionDocumento.objects.filter(dia=dia, movimiento__isnull=False).update(movimiento=None)
    Coincidencia.objects.filter(dia=dia).delete()
    VersionMotor.objects.filter(dia=dia).delete()
    Reclasificacion.objects.filter(dia=dia).delete()
    Excepcion.objects.filter(dia=dia).delete()

    # 2. Datos de los archivos.
    MovimientoProveedor.objects.filter(lote__in=lotes).delete()
    if con_pos:
        documentos = RegularizacionDocumento.objects.filter(dia=dia)
        resumen["documentos"] = [f"{d.tipo} {d.folio} ({d.estado})" for d in documentos]
        documentos.delete()
        arqueos = ArqueoEfectivo.objects.filter(dia=dia)
        resumen["arqueos"] = [a.caja for a in arqueos] if hasattr(ArqueoEfectivo, "caja") else arqueos.count()
        arqueos.delete()
        Venta.objects.filter(dia=dia).delete()
    LoteCarga.objects.filter(pk__in=[l.pk for l in lotes]).delete()

    dia.estado = DiaArqueo.Estado.CARGADO if dia.lotes.exists() else DiaArqueo.Estado.SIN_CARGAR
    dia.save(update_fields=["estado"])
    resumen["estado_nuevo"] = dia.estado

    solicitud.estado = SolicitudAnulacionCarga.Estado.EJECUTADA
    solicitud.resuelta_por, solicitud.resuelta_en = usuario, timezone.now()
    solicitud.comentario_resolucion, solicitud.resumen = comentario, resumen
    solicitud.save()
    EventoAuditoria.objects.create(
        dia=dia, entidad="SolicitudAnulacionCarga", entidad_id=str(solicitud.pk), accion="anular_carga",
        valor_anterior=resumen, valor_posterior={"estado": dia.estado, "solicitada_por": str(solicitud.solicitada_por),
                                                 "motivo": solicitud.motivo, "comentario": comentario},
        usuario=usuario,
    )
    return solicitud
