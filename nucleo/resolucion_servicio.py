"""Resolución manual de una excepción (diseño funcional §15).

Toda resolución exige motivo desde una lista cerrada y queda en la
bitácora con usuario, hora y valor anterior/posterior (control C2).
Ninguna resolución modifica el dato original: una reclasificación de
medio se guarda en `medio_aprobado`, nunca sobre el medio declarado
(control C3 — el motor propone, un humano confirma, nunca se sobrescribe
en silencio).
"""
from django.db import transaction
from django.utils import timezone

from nucleo.models import Coincidencia, EventoAuditoria, Excepcion, MotivoResolucion, PagoDeclarado

_NIVEL_POR_TIPO = {
    "medio_contradictorio": 4,
    "diferencia_monto": 5,
    "ambiguedad": 6,
    "agrupacion": 6,
}


class ResolucionInvalida(Exception):
    pass


@transaction.atomic
def resolver_excepcion(excepcion: Excepcion, *, resolucion: str, motivo_id: int,
                        comentario: str, usuario, movimiento_id_alternativo: int | None = None) -> Excepcion:
    if not excepcion.abierta:
        raise ResolucionInvalida("Esta excepción ya fue resuelta.")
    if resolucion not in Excepcion.Resolucion.values:
        raise ResolucionInvalida("Resolución no reconocida.")

    if not motivo_id:
        raise ResolucionInvalida("El motivo es obligatorio.")
    try:
        motivo = MotivoResolucion.objects.get(
            pk=motivo_id, aplica_a=MotivoResolucion.AplicaA.EXCEPCION, activo=True,
        )
    except (MotivoResolucion.DoesNotExist, ValueError):
        raise ResolucionInvalida("Selecciona un motivo válido de la lista.")

    valor_anterior = {"abierta": True, "tipo": excepcion.tipo}

    def _crear_coincidencia_manual(movimiento):
        corrida = excepcion.dia.corridas_motor.latest("ejecutado_en")
        diferencia = 0
        if excepcion.pago_id and movimiento:
            diferencia = abs(excepcion.pago.monto - movimiento.monto_venta_valido_abono)
        coincidencia = Coincidencia.objects.create(
            dia=excepcion.dia, corrida=corrida,
            nivel=_NIVEL_POR_TIPO.get(excepcion.tipo, 6), score=100,
            diferencia_monto=diferencia, diferencia_hora_segundos=0,
            origen=Coincidencia.Origen.MANUAL,
        )
        if excepcion.pago_id:
            coincidencia.pagos.set([excepcion.pago_id])
        if movimiento:
            coincidencia.movimientos.set([movimiento.pk])
        return coincidencia

    if resolucion == Excepcion.Resolucion.ACEPTAR:
        es_otros_ingresos = (
            excepcion.tipo == Excepcion.Tipo.MOVIMIENTO_SIN_PAGO and motivo.codigo == "forma_pago_sin_boleta"
        )
        if es_otros_ingresos:
            # No hay pago que vincular — el movimiento se acepta como
            # ingreso legítimo sin comanda, pendiente de que el Analista
            # de Ingresos le emita su propia boleta al cierre (decisión
            # de Karla, 2026-09-06). No crea Coincidencia: no hay nada
            # que emparejar todavía.
            if not excepcion.movimiento_id:
                raise ResolucionInvalida("Esta excepción no tiene movimiento asociado.")
        elif not excepcion.pago_id or not excepcion.movimiento_id:
            raise ResolucionInvalida("No hay un candidato propuesto para aceptar en esta excepción.")

        if es_otros_ingresos:
            pass  # sin coincidencia: se documenta con su propia boleta (ver documentos_servicio)
        else:
            if excepcion.tipo == "medio_contradictorio":
                pago = excepcion.pago
                medio_proveedor = excepcion.movimiento.medio
                # "Prepago" no es un medio propio del POS (débito/crédito):
                # se reclasifica al más cercano, nunca se inventa categoría
                # nueva (decisión confirmada con Karla, 2026-09-05).
                pago.medio_aprobado = (
                    PagoDeclarado.Medio.DEBITO if medio_proveedor == "prepago" else medio_proveedor
                )
                pago.save(update_fields=["medio_aprobado"])
            _crear_coincidencia_manual(excepcion.movimiento)

    elif resolucion == Excepcion.Resolucion.VINCULAR_OTRO:
        if not movimiento_id_alternativo:
            raise ResolucionInvalida("Indica qué movimiento vincular.")
        from nucleo.models import MovimientoProveedor
        movimiento = MovimientoProveedor.objects.get(pk=movimiento_id_alternativo, dia=excepcion.dia)
        _crear_coincidencia_manual(movimiento)

    # RECHAZAR: no crea coincidencia; el caso queda documentado con su
    # motivo, sin conciliar nada.

    excepcion.abierta = False
    excepcion.resolucion = resolucion
    excepcion.motivo = motivo
    excepcion.comentario = comentario
    excepcion.resuelta_por = usuario
    excepcion.resuelta_en = timezone.now()
    excepcion.save()

    EventoAuditoria.objects.create(
        dia=excepcion.dia, entidad="Excepcion", entidad_id=str(excepcion.pk),
        accion=f"resolver_excepcion:{resolucion}",
        valor_anterior=valor_anterior,
        valor_posterior={"resolucion": resolucion, "motivo": motivo.codigo, "comentario": comentario},
        usuario=usuario,
    )

    return excepcion
