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
    REGLA_CORRECCION_MANUAL_DE_MEDIO, REGLA_MEDIO_CONTRADICTORIO, REGLA_MEZCLA_DE_MEDIOS, registrar_manual,
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


def sincronizar_estado_del_dia(dia) -> None:
    """El estado del día sigue a sus excepciones (Karla, 2026-10-21): al
    resolver la última bloqueante pasa a "Excepciones resueltas", y si se
    vuelve a abrir una, regresa a "Conciliado con excepciones". Antes solo lo
    fijaba la conciliación, y el calendario mostraba días con cero abiertas
    como "Conciliado con excepciones". Un día aprobado, cerrado o exportado
    nunca se toca desde aquí."""
    from nucleo.models import DiaArqueo
    if dia.estado not in (DiaArqueo.Estado.CONCILIADO_CON_EXCEPCIONES, DiaArqueo.Estado.EXCEPCIONES_RESUELTAS):
        return
    quedan = dia.excepciones.filter(abierta=True).exclude(tipo=Excepcion.Tipo.CALIDAD_REGISTRO).exists()
    nuevo = DiaArqueo.Estado.CONCILIADO_CON_EXCEPCIONES if quedan else DiaArqueo.Estado.EXCEPCIONES_RESUELTAS
    if dia.estado != nuevo:
        dia.estado = nuevo
        dia.save(update_fields=["estado"])


@transaction.atomic
def resolver_excepcion(excepcion: Excepcion, *, resolucion: str, motivo_id: int,
                        comentario: str, usuario, movimiento_id_alternativo: int | None = None,
                        cuenta_contable_id: int | None = None, cajera_id: int | None = None,
                        lineas: list[dict] | None = None, pago_objetivo_id: int | None = None,
                        medio_destino: str | None = None, pagos_agrupados: list | None = None,
                        movimientos_adicionales: list | None = None) -> Excepcion:
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
    # Cobros y pagos que esta resolución deja conciliados: sus propias
    # excepciones ("movimiento sin pago", "pago duplicado", "pago sin
    # movimiento") se cierran arrastradas por esta (Karla, 2026-10-21).
    movimientos_usados: set[int] = set()
    pagos_usados: set[int] = set()

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
        # Karla, 2026-10-23 (día 13, cobro de $4.500 a las 15:29): un cobro
        # sin boleta que no es venta (no se emite boleta) se registra como
        # GANANCIA de caja, a nombre de la cajera, en vez de «otros ingresos».
        cobro_sin_venta = excepcion.tipo in (Excepcion.Tipo.MOVIMIENTO_SIN_PAGO, Excepcion.Tipo.PAGO_DUPLICADO)
        if excepcion.tipo not in (Excepcion.Tipo.PAGO_SIN_MOVIMIENTO, Excepcion.Tipo.DIFERENCIA_MONTO) \
                and not cobro_sin_venta:
            raise ResolucionInvalida("Esta excepción no admite registrarse como pérdida o ganancia de caja.")
        if cobro_sin_venta and not excepcion.movimiento_id:
            raise ResolucionInvalida("Esta excepción no tiene cobro del proveedor asociado.")
        if not cobro_sin_venta and not excepcion.pago_id:
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

        # Karla, 2026-10-22 (día 12, comanda 677493): la boleta de $29.700
        # se cobró en dos pasadas de débito ($24.900 + $5.000) y la cajera
        # cobró $200 de más. Se puede sumar más de un cobro; la diferencia
        # es contra la suma de todos.
        adicionales = []
        ids_adicionales = sorted({int(i) for i in (movimientos_adicionales or []) if str(i).strip().isdigit()})
        if ids_adicionales:
            if not movimiento:
                raise ResolucionInvalida("Elige primero el cobro principal en «Vincular con otro movimiento».")
            from nucleo.models import MovimientoProveedor
            adicionales = list(MovimientoProveedor.objects.filter(pk__in=ids_adicionales, dia=excepcion.dia))
            if len(adicionales) != len(ids_adicionales) or movimiento.pk in ids_adicionales:
                raise ResolucionInvalida("Los cobros adicionales deben ser de este día y distintos del principal.")
            if any(m.coincidencias.exists() for m in adicionales):
                raise ResolucionInvalida("Uno de los cobros adicionales ya está conciliado con otra boleta.")

        if cobro_sin_venta:
            diferencia = -movimiento.monto_venta_valido_abono  # todo el cobro es ganancia
        elif movimiento:
            diferencia = excepcion.pago.monto - movimiento.monto_venta_valido_abono - sum(
                m.monto_venta_valido_abono for m in adicionales
            )
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
            coincidencia = _crear_coincidencia_manual(movimiento)
            if adicionales:
                coincidencia.movimientos.add(*[m.pk for m in adicionales])
                coincidencia.diferencia_monto = abs(diferencia)
                coincidencia.save(update_fields=["diferencia_monto"])
                movimientos_usados.update(m.pk for m in adicionales)
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
        # Vuelto en efectivo (Karla, 2026-10-22, día 19, comanda 682089): la
        # cajera cobró $20.000 con débito por una venta de $2.000 y le
        # devolvió $18.000 al cliente desde la caja. Se registra como una
        # línea de EFECTIVO NEGATIVA: es un traspaso entre la caja y
        # Transbank, sin pérdida ni ganancia. Solo el efectivo puede ser
        # negativo, y solo si hay un cobro que lo explique.
        for linea, m in zip(lineas_validas, montos):
            if m == 0 or (m < 0 and linea["medio"] != PagoDeclarado.Medio.EFECTIVO):
                raise ResolucionInvalida(
                    "Cada línea debe tener un monto mayor a cero (solo el efectivo puede ir en negativo, "
                    "como vuelto entregado desde la caja)."
                )
        if any(m < 0 for m in montos) and not any(l.get("movimiento_id") for l in lineas_validas):
            raise ResolucionInvalida("Un vuelto en efectivo tiene que ir junto al cobro de tarjeta que lo originó.")
        suma = sum(montos)
        if suma != excepcion.pago.monto:
            raise ResolucionInvalida(
                f"Las líneas deben sumar exactamente ${excepcion.pago.monto:,.0f} (suman ${suma:,.0f})."
            )

        from nucleo.models import MovimientoProveedor
        ids_lineas = [str(l.get("movimiento_id")) for l in lineas_validas if l.get("movimiento_id")]
        if len(ids_lineas) != len(set(ids_lineas)):
            raise ResolucionInvalida("El mismo cobro está en dos líneas: cada cobro va en una sola línea.")
        movimientos_referenciados = []
        # Proveedor de referencia para las líneas sin cobro propio (efectivo):
        # el del primer cobro elegido, para que el traspaso vaya a su cuenta.
        proveedor_de_referencia = next((
            MovimientoProveedor.objects.get(pk=l["movimiento_id"], dia=excepcion.dia).proveedor
            for l in lineas_validas if l.get("movimiento_id")
        ), "")
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
                    "proveedor": movimiento_linea.proveedor if movimiento_linea else proveedor_de_referencia,
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
            movimientos_usados.update(movimientos_referenciados)

    elif resolucion == Excepcion.Resolucion.CORREGIR_MEDIO:
        _corregir_medio(excepcion, motivo=motivo, comentario=comentario, usuario=usuario,
                        pago_objetivo_id=pago_objetivo_id, medio_destino=medio_destino)
        if excepcion.movimiento_id:
            movimientos_usados.add(excepcion.movimiento_id)

    elif resolucion == Excepcion.Resolucion.AGRUPAR:
        pagos_usados.update(_agrupar(excepcion, pagos_agrupados or []))

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
        movimientos_usados.add(movimiento_vinculado.pk)

    arrastradas = Excepcion.objects.none()
    if movimientos_usados:
        arrastradas = arrastradas | Excepcion.objects.filter(
            dia=excepcion.dia, abierta=True,
            tipo__in=[Excepcion.Tipo.MOVIMIENTO_SIN_PAGO, Excepcion.Tipo.PAGO_DUPLICADO],
            movimiento_id__in=movimientos_usados,
        )
    if pagos_usados:
        arrastradas = arrastradas | Excepcion.objects.filter(
            dia=excepcion.dia, abierta=True, tipo=Excepcion.Tipo.PAGO_SIN_MOVIMIENTO, pago_id__in=pagos_usados,
        )
    for hermana in arrastradas.exclude(pk=excepcion.pk).distinct():
        hermana.abierta = False
        hermana.resolucion = Excepcion.Resolucion.VINCULAR_OTRO
        hermana.motivo = motivo
        hermana.comentario = f"Se juntó con la excepción #{excepcion.pk} ({Excepcion.Resolucion(resolucion).label})."
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

    sincronizar_estado_del_dia(excepcion.dia)
    return excepcion


# --------------------------------------------------------------------------
# Casos levantados por la Encargada de Ingresos (correo del 15-10-2026)
# --------------------------------------------------------------------------

def _tolerancia_monto() -> float:
    return 1.0  # pesos: los montos de boleta y cobro deben coincidir (redondeo)


def _medio_tarjeta_del(movimiento) -> str:
    return PagoDeclarado.Medio.DEBITO if movimiento.medio == "prepago" else movimiento.medio


def _corregir_medio(excepcion: Excepcion, *, motivo, comentario, usuario, pago_objetivo_id, medio_destino) -> None:
    """Caso 3: la cajera registró un medio y cobró con otro.

    * Desde un «movimiento sin pago»: el cobro de tarjeta corresponde a una
      boleta que Resto registró en EFECTIVO. Se elige esa boleta; queda
      reclasificada a tarjeta, conciliada con el cobro, y el efectivo esperado
      de su caja baja en ese monto (la cajera deja de aparecer con faltante).
    * Desde un «pago sin movimiento»: la boleta se registró con tarjeta pero
      se pagó en efectivo (o transferencia). Se corrige el medio; el efectivo
      esperado de su caja sube en ese monto.
    El pago declarado nunca se modifica: la corrección vive en
    `medio_aprobado` y en la tabla de reclasificaciones (auditable)."""
    if excepcion.tipo in (Excepcion.Tipo.MOVIMIENTO_SIN_PAGO, Excepcion.Tipo.PAGO_DUPLICADO):
        if not excepcion.movimiento_id:
            raise ResolucionInvalida("Esta excepción no tiene cobro del proveedor asociado.")
        if not pago_objetivo_id:
            raise ResolucionInvalida("Elige la boleta registrada en efectivo que corresponde a este cobro.")
        try:
            pago = PagoDeclarado.objects.select_related("venta").get(pk=pago_objetivo_id, venta__dia=excepcion.dia)
        except (PagoDeclarado.DoesNotExist, ValueError):
            raise ResolucionInvalida("La boleta elegida no es de este día.")
        if pago.medio != PagoDeclarado.Medio.EFECTIVO:
            raise ResolucionInvalida("La boleta elegida no está registrada en efectivo.")
        if pago.medio_aprobado or pago.reclasificaciones.exists() or pago.coincidencias.exists():
            raise ResolucionInvalida("Esa boleta ya tiene una corrección o un cobro asociado.")
        movimiento = excepcion.movimiento
        if abs(float(pago.monto) - float(movimiento.monto_venta_valido_abono)) > _tolerancia_monto():
            raise ResolucionInvalida(
                f"La boleta es por ${pago.monto:,.0f} y el cobro por ${movimiento.monto_venta_valido_abono:,.0f}: "
                "deben ser del mismo monto."
            )
        medio_nuevo = _medio_tarjeta_del(movimiento)
        pago.medio_aprobado = medio_nuevo
        pago.save(update_fields=["medio_aprobado"])
        registrar_manual(
            pago=pago, medio_corregido=medio_nuevo, monto=pago.monto, regla=REGLA_CORRECCION_MANUAL_DE_MEDIO,
            motivo=motivo.texto, usuario=usuario, excepcion=excepcion,
            evidencia={
                "proveedor": movimiento.proveedor, "movimiento_id": movimiento.pk,
                "autorizacion": movimiento.autorizacion, "terminal": movimiento.terminal,
                "monto_movimiento": float(movimiento.monto_venta_valido_abono),
                "hora_movimiento": movimiento.fecha_hora.strftime("%H:%M"),
                "nota_de_venta": pago.venta.n_interno, "caja": pago.venta.caja, "comentario": comentario,
            },
        )
        corrida = excepcion.dia.corridas_motor.latest("ejecutado_en")
        coincidencia = Coincidencia.objects.create(
            dia=excepcion.dia, corrida=corrida, nivel=4, score=100, diferencia_monto=0,
            diferencia_hora_segundos=0, origen=Coincidencia.Origen.MANUAL,
        )
        coincidencia.pagos.set([pago.pk])
        coincidencia.movimientos.set([movimiento.pk])
        excepcion.pago = pago  # trazabilidad: qué boleta explicó el cobro
        return

    if excepcion.tipo == Excepcion.Tipo.PAGO_SIN_MOVIMIENTO:
        if not excepcion.pago_id:
            raise ResolucionInvalida("Esta excepción no tiene un pago del POS asociado.")
        if medio_destino not in (PagoDeclarado.Medio.EFECTIVO, PagoDeclarado.Medio.TRANSFERENCIA):
            raise ResolucionInvalida("Indica con qué medio se pagó en realidad (efectivo o transferencia).")
        pago = excepcion.pago
        if medio_destino == pago.medio:
            raise ResolucionInvalida("Ese es el mismo medio que ya tiene la boleta.")
        pago.medio_aprobado = medio_destino
        pago.save(update_fields=["medio_aprobado"])
        registrar_manual(
            pago=pago, medio_corregido=medio_destino, monto=pago.monto, regla=REGLA_CORRECCION_MANUAL_DE_MEDIO,
            motivo=motivo.texto, usuario=usuario, excepcion=excepcion,
            evidencia={"nota_de_venta": pago.venta.n_interno, "caja": pago.venta.caja, "comentario": comentario,
                       "busqueda_proveedor": "Sin cobro del proveedor por esta boleta"},
        )
        return

    raise ResolucionInvalida("Esta excepción no admite corregir el medio de la boleta.")


def _agrupar(excepcion: Excepcion, pagos_ids: list) -> set[int]:
    """Caso 1: un solo cobro de tarjeta pagó varias boletas (ej. $17.664 +
    $1.500 = $19.164). Desde el «movimiento sin pago» se eligen las boletas
    con «pago sin movimiento» que suman exactamente el cobro; quedan
    conciliadas juntas y sus excepciones se cierran con esta."""
    if excepcion.tipo != Excepcion.Tipo.MOVIMIENTO_SIN_PAGO or not excepcion.movimiento_id:
        raise ResolucionInvalida("Solo un cobro sin boleta (movimiento sin pago) puede pagar varias boletas.")
    ids = sorted({int(i) for i in pagos_ids if str(i).strip().isdigit()})
    if len(ids) < 2:
        raise ResolucionInvalida("Marca al menos dos boletas que se pagaron con este cobro.")
    abiertas = {
        e.pago_id: e for e in Excepcion.objects.filter(
            dia=excepcion.dia, abierta=True, tipo=Excepcion.Tipo.PAGO_SIN_MOVIMIENTO, pago_id__in=ids,
        ).select_related("pago")
    }
    if set(abiertas) != set(ids):
        raise ResolucionInvalida("Solo se pueden juntar boletas de este día que estén como «pago sin movimiento».")
    suma = sum(float(e.pago.monto) for e in abiertas.values())
    cobro = float(excepcion.movimiento.monto_venta_valido_abono)
    if abs(suma - cobro) > _tolerancia_monto():
        raise ResolucionInvalida(
            f"Las boletas marcadas suman ${suma:,.0f} y el cobro es de ${cobro:,.0f}: deben sumar exactamente lo mismo."
        )
    corrida = excepcion.dia.corridas_motor.latest("ejecutado_en")
    coincidencia = Coincidencia.objects.create(
        dia=excepcion.dia, corrida=corrida, nivel=6, score=100, diferencia_monto=0,
        diferencia_hora_segundos=0, origen=Coincidencia.Origen.MANUAL,
    )
    coincidencia.pagos.set(ids)
    coincidencia.movimientos.set([excepcion.movimiento_id])
    return set(ids)
