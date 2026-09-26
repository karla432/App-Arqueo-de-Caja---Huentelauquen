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

from nucleo.permisos import Permiso, exigir
from nucleo.reclasificacion_servicio import (
    REGLA_MEDIO_CONTRADICTORIO, REGLA_MEZCLA_DE_MEDIOS, registrar_manual,
)

from nucleo.models import (
    Cajera, Coincidencia, CuentaContable, EventoAuditoria, Excepcion, LineaConciliacion,
    MotivoResolucion, PagoDeclarado,
)

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
                        comentario: str, usuario, movimiento_id_alternativo: int | None = None,
                        cuenta_contable_id: int | None = None, cajera_id: int | None = None,
                        lineas: list[dict] | None = None) -> Excepcion:
    exigir(usuario, Permiso.OPERAR, "Tu perfil no puede resolver excepciones.")
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
    movimiento_vinculado = None  # el cobro del proveedor que se junta con el pago (si lo hay)

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
                mov = excepcion.movimiento
                registrar_manual(
                    pago=pago, medio_corregido=pago.medio_aprobado, monto=pago.monto,
                    regla=REGLA_MEDIO_CONTRADICTORIO, motivo=motivo.texto, usuario=usuario, excepcion=excepcion,
                    evidencia={
                        "proveedor": mov.proveedor, "movimiento_id": mov.pk, "autorizacion": mov.autorizacion,
                        "terminal": mov.terminal, "monto_movimiento": float(mov.monto_venta_valido_abono),
                        "hora_movimiento": mov.fecha_hora.strftime("%H:%M"), "comentario": comentario,
                    },
                )
            _crear_coincidencia_manual(excepcion.movimiento)

    elif resolucion == Excepcion.Resolucion.VINCULAR_OTRO:
        if not movimiento_id_alternativo:
            raise ResolucionInvalida("Indica qué movimiento vincular.")
        from nucleo.models import MovimientoProveedor
        movimiento = MovimientoProveedor.objects.get(pk=movimiento_id_alternativo, dia=excepcion.dia)
        _crear_coincidencia_manual(movimiento)
        movimiento_vinculado = movimiento

    elif resolucion == Excepcion.Resolucion.PERDIDA_GANANCIA:
        # Un pago con tarjeta que el POS declaró y que no tiene (o no
        # calza con) un cobro real del proveedor: en vez de dejarlo
        # flotando, se registra la diferencia contra una cuenta de
        # pérdida/ganancia, identificando a la cajera responsable de esa
        # caja para poder cobrarle (Karla, 2026-09-16). Sobre
        # UMBRAL_VISADO_PERDIDA exige el visto bueno del Jefe de
        # Contabilidad antes de poder cerrar el día — ver
        # nucleo.perdidas_servicio.
        if excepcion.tipo not in (Excepcion.Tipo.PAGO_SIN_MOVIMIENTO, Excepcion.Tipo.DIFERENCIA_MONTO):
            raise ResolucionInvalida("Esta excepción no admite registrarse como pérdida o ganancia de caja.")
        if not excepcion.pago_id:
            raise ResolucionInvalida("Esta excepción no tiene un pago del POS asociado.")
        if not cuenta_contable_id:
            raise ResolucionInvalida("Selecciona la cuenta contable de pérdida o ganancia.")
        if not cajera_id:
            raise ResolucionInvalida("Selecciona la cajera responsable de esta caja.")
        try:
            cuenta = CuentaContable.objects.get(pk=cuenta_contable_id, activo=True)
        except (CuentaContable.DoesNotExist, ValueError):
            raise ResolucionInvalida("Selecciona una cuenta contable válida de la lista.")
        try:
            cajera = Cajera.objects.get(pk=cajera_id, activo=True)
        except (Cajera.DoesNotExist, ValueError):
            raise ResolucionInvalida("Selecciona una cajera válida de la lista.")

        movimiento = None
        if excepcion.movimiento_id:
            # diferencia_monto: el motor ya propuso este par: pago y
            # movimiento no calzan en monto, por eso es una excepción.
            movimiento = excepcion.movimiento
        elif movimiento_id_alternativo:
            from nucleo.models import MovimientoProveedor
            movimiento = MovimientoProveedor.objects.get(pk=movimiento_id_alternativo, dia=excepcion.dia)

        if movimiento:
            diferencia = excepcion.pago.monto - movimiento.monto_venta_valido_abono
        else:
            # Sin ningún movimiento del proveedor que lo respalde: todo
            # el monto declarado en el POS es pérdida.
            diferencia = excepcion.pago.monto

        if diferencia == 0:
            raise ResolucionInvalida(
                'No hay diferencia que registrar — usa "Aceptar" o "Vincular otro movimiento".'
            )
        tipo_esperado = CuentaContable.Tipo.PERDIDA if diferencia > 0 else CuentaContable.Tipo.GANANCIA
        if cuenta.tipo != tipo_esperado:
            raise ResolucionInvalida(
                f"El monto es una {tipo_esperado.label.lower()} (${abs(diferencia):,.0f}) — "
                f"selecciona una cuenta de tipo {tipo_esperado.label.lower()}."
            )

        if movimiento:
            _crear_coincidencia_manual(movimiento)
            movimiento_vinculado = movimiento
        excepcion.cuenta_contable = cuenta
        excepcion.cajera_responsable = cajera
        excepcion.monto_perdida_ganancia = abs(diferencia)

    elif resolucion == Excepcion.Resolucion.MEZCLA_MEDIOS:
        # Resto declaró un solo medio, pero la conciliación comprobó que
        # en realidad se recibió repartido en más de uno (Karla,
        # 2026-09-18: ej. Transbank $45.000 + Efectivo $5.000 de una
        # venta de $50.000). Es un traspaso entre cuentas — la plata está
        # toda, no afecta el resultado — así que no lleva cuenta de
        # pérdida/ganancia ni cajera. El pago declarado no se toca.
        if excepcion.tipo not in (Excepcion.Tipo.PAGO_SIN_MOVIMIENTO, Excepcion.Tipo.DIFERENCIA_MONTO):
            raise ResolucionInvalida("Esta excepción no admite registrarse como mezcla de medios.")
        if not excepcion.pago_id:
            raise ResolucionInvalida("Esta excepción no tiene un pago del POS asociado.")
        lineas_validas = [
            l for l in (lineas or [])
            if l.get("medio") and l.get("monto") not in (None, "", 0)
        ]
        if len(lineas_validas) < 2:
            raise ResolucionInvalida("Agrega al menos dos líneas (medio y monto) para una mezcla de medios.")

        from decimal import Decimal, InvalidOperation
        try:
            montos = [Decimal(str(l["monto"])) for l in lineas_validas]
        except InvalidOperation:
            raise ResolucionInvalida("Uno de los montos ingresados no es válido.")
        if any(m <= 0 for m in montos):
            raise ResolucionInvalida("Cada línea debe tener un monto mayor a cero.")
        suma = sum(montos)
        if suma != excepcion.pago.monto:
            raise ResolucionInvalida(
                f"Las líneas deben sumar exactamente ${excepcion.pago.monto:,.0f} (suman ${suma:,.0f})."
            )

        from nucleo.models import MovimientoProveedor
        movimientos_referenciados = []
        for linea, monto in zip(lineas_validas, montos):
            movimiento_linea = None
            movimiento_id = linea.get("movimiento_id")
            if movimiento_id:
                movimiento_linea = MovimientoProveedor.objects.get(pk=movimiento_id, dia=excepcion.dia)
                movimientos_referenciados.append(movimiento_linea.pk)
            LineaConciliacion.objects.create(
                excepcion=excepcion, medio_conciliado=linea["medio"], monto_conciliado=monto,
                movimiento=movimiento_linea, fuente_evidencia=linea.get("fuente_evidencia", ""),
            )
            registrar_manual(
                pago=excepcion.pago, medio_corregido=linea["medio"], monto=monto,
                regla=REGLA_MEZCLA_DE_MEDIOS, motivo=motivo.texto, usuario=usuario, excepcion=excepcion,
                evidencia={
                    "movimiento_id": movimiento_linea.pk if movimiento_linea else None,
                    "autorizacion": movimiento_linea.autorizacion if movimiento_linea else "",
                    "proveedor": movimiento_linea.proveedor if movimiento_linea else "",
                    "fuente": linea.get("fuente_evidencia", "") or ("Cartola del proveedor" if movimiento_linea else ""),
                    "comentario": comentario,
                },
            )
        if movimientos_referenciados:
            corrida = excepcion.dia.corridas_motor.latest("ejecutado_en")
            coincidencia = Coincidencia.objects.create(
                dia=excepcion.dia, corrida=corrida, nivel=6, score=100,
                diferencia_monto=0, diferencia_hora_segundos=0, origen=Coincidencia.Origen.MANUAL,
            )
            coincidencia.pagos.set([excepcion.pago_id])
            coincidencia.movimientos.set(movimientos_referenciados)

    # RECHAZAR: no crea coincidencia; el caso queda documentado con su
    # motivo, sin conciliar nada.

    if movimiento_vinculado is not None:
        # Karla, 2026-09-23: la cajera cobró en el POS de Transbank menos de
        # lo vendido ($26.307 vs $29.307): el pago sin movimiento y el
        # movimiento sin pago son las dos caras del mismo caso. Al juntarlos
        # el movimiento queda asociado a esta excepción (para que una nueva
        # conciliación no lo vuelva a soltar) y se cierra su excepción
        # "movimiento sin pago", que ya no tiene nada pendiente.
        if excepcion.movimiento_id is None:
            excepcion.movimiento = movimiento_vinculado
        hermanas = Excepcion.objects.filter(
            dia=excepcion.dia, abierta=True, tipo=Excepcion.Tipo.MOVIMIENTO_SIN_PAGO,
            movimiento=movimiento_vinculado,
        ).exclude(pk=excepcion.pk)
        for hermana in hermanas:
            hermana.abierta = False
            hermana.resolucion = Excepcion.Resolucion.VINCULAR_OTRO
            hermana.motivo = motivo
            hermana.comentario = f"Se juntó con el pago de la excepción #{excepcion.pk} ({Excepcion.Resolucion(resolucion).label})."
            hermana.resuelta_por = usuario
            hermana.resuelta_en = timezone.now()
            hermana.save()
            EventoAuditoria.objects.create(
                dia=hermana.dia, entidad="Excepcion", entidad_id=str(hermana.pk),
                accion="resolver_excepcion:vinculada_con_pago",
                valor_anterior={"abierta": True, "tipo": hermana.tipo},
                valor_posterior={"resolucion": hermana.resolucion, "junto_con": excepcion.pk},
                usuario=usuario,
            )

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
