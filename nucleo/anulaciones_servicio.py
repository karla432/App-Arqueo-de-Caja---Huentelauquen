"""Anulaciones de Transbank (caso de uso pedido por Karla, 2026-09-06:
"pago con tarjeta que después se anuló esa transacción").

Sin ejemplo real todavía — el día de referencia (02-09-2026) tuvo cero
anulaciones. Se implementa según lo que el diseño funcional ya exige
(§8: "las anulaciones se tratan como movimientos con signo, no se
eliminan") y la estructura de columnas real de Transbank (que sí trae
"Código de autorización de la anulación" y "Monto anulado" como campos
propios). Karla lo va a probar contra un caso real esta semana — es
esperable que algo se ajuste cuando eso pase.

Se corre ANTES del motor de conciliación (nucleo.conciliacion_servicio):
empareja cada anulación con su venta original por autorización y saca a
ambas de la bolsa de movimientos a conciliar — se compensan solas, no
generan diferencia de caja. Si no hay una original clara, o si la
comanda del POS asociada sigue activa pese a la anulación, queda una
excepción de alto riesgo para que alguien la mire (posible reembolso no
registrado, o cliente cobrado sin reversa real).
"""
from nucleo.models import DiaArqueo, Excepcion, MovimientoProveedor, PagoDeclarado


def es_movimiento_anulado(movimiento: MovimientoProveedor) -> bool:
    return "anul" in movimiento.tipo.lower() or movimiento.monto_anulado > 0


def es_venta_anulada(venta) -> bool:
    return "anul" in venta.estado.lower()


def procesar_anulaciones(dia: DiaArqueo) -> dict:
    """Devuelve los ids de MovimientoProveedor que deben excluirse del
    motor de conciliación normal porque ya quedaron resueltos aquí
    (compensados, o convertidos en una excepción de alto riesgo)."""
    movimientos = list(MovimientoProveedor.objects.filter(dia=dia))
    anulaciones = [m for m in movimientos if es_movimiento_anulado(m)]
    if not anulaciones:
        return {"ids_excluidos": set(), "excepciones_creadas": 0}

    activos_por_auth = {
        m.autorizacion: m for m in movimientos if not es_movimiento_anulado(m) and m.autorizacion
    }
    ids_excluidos: set[int] = set()
    creadas = 0

    def _ya_tiene_excepcion(movimiento_id: int) -> bool:
        # Cualquiera, abierta o ya resuelta: reconciliar es idempotente
        # (§5) — si alguien ya la resolvió, no se vuelve a crear.
        return Excepcion.objects.filter(
            dia=dia, tipo=Excepcion.Tipo.ANULACION_DESCALZADA, movimiento_id=movimiento_id,
        ).exists()

    for anulacion in anulaciones:
        ids_excluidos.add(anulacion.pk)
        original = activos_por_auth.get(anulacion.autorizacion) if anulacion.autorizacion else None

        if not original:
            if not _ya_tiene_excepcion(anulacion.pk):
                Excepcion.objects.create(
                    dia=dia, tipo=Excepcion.Tipo.ANULACION_DESCALZADA,
                    monto_en_juego=anulacion.monto_anulado or anulacion.monto_original,
                    movimiento=anulacion,
                    decision_propuesta=(
                        "Transbank registra una anulación sin un cobro original identificable "
                        "ese día — verificar manualmente."
                    ),
                )
                creadas += 1
            continue

        ids_excluidos.add(original.pk)
        pago_relacionado = (
            PagoDeclarado.objects.filter(
                venta__dia=dia, monto=original.monto_venta_valido_abono, medio__in=["debito", "credito"],
            )
            .select_related("venta")
            .first()
        )

        if pago_relacionado and not es_venta_anulada(pago_relacionado.venta):
            if not _ya_tiene_excepcion(original.pk):
                Excepcion.objects.create(
                    dia=dia, tipo=Excepcion.Tipo.ANULACION_DESCALZADA,
                    monto_en_juego=original.monto_venta_valido_abono, movimiento=original,
                    decision_propuesta=(
                        "Transbank anuló este cobro, pero la comanda del POS sigue activa (no anulada). "
                        "Verificar si la venta realmente se revirtió — riesgo de faltante no explicado."
                    ),
                )
                creadas += 1
        # Si la venta del POS también está anulada, o si no hay comanda
        # que la respalde, ambos movimientos se compensan solos: no se
        # crea ninguna excepción, quedan simplemente excluidos.

    return {"ids_excluidos": ids_excluidos, "excepciones_creadas": creadas}
