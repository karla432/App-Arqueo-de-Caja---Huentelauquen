"""Arqueo de efectivo (diseño funcional §16).

El efectivo es el único medio sin contraparte electrónica: se controla
por diferencia entre lo que la venta implica y lo que hay físicamente,
**por caja**, nunca solo por el consolidado del día (control #9 — un
total en $0 que esconde un sobrante y un faltante en dos cajas distintas
no es un día cuadrado).
"""
from decimal import Decimal

from django.db import transaction

from nucleo.models import (
    ArqueoEfectivo, ConteoDenominacion, DiaArqueo, EventoAuditoria,
    MotivoResolucion, PagoDeclarado, UMBRAL_DIFERENCIA_CAJA,
)

DENOMINACIONES_CLP = [20000, 10000, 5000, 2000, 1000, 500, 100, 50, 10]


def _medio_efectivo_de(pago: PagoDeclarado) -> bool:
    """Un pago cuenta como efectivo para el arqueo si así quedó
    finalmente aprobado — una reclasificación manual (§15) puede mover un
    pago hacia o fuera de efectivo, y el arqueo debe reflejar el medio ya
    resuelto, no el declarado a ciegas."""
    return (pago.medio_aprobado or pago.medio) == PagoDeclarado.Medio.EFECTIVO


def calcular_teorico_por_caja(dia: DiaArqueo) -> dict[str, Decimal]:
    """Efectivo teórico = pagos en efectivo, deduplicados por comanda
    (ya lo están, uno por medio y venta) y agrupados por caja."""
    totales: dict[str, Decimal] = {}
    pagos = PagoDeclarado.objects.filter(venta__dia=dia).select_related("venta")
    for pago in pagos:
        if _medio_efectivo_de(pago):
            caja = pago.venta.caja
            totales[caja] = totales.get(caja, Decimal(0)) + pago.monto
    return totales


def calcular_ajustes_por_caja(dia: DiaArqueo) -> dict[str, Decimal]:
    """Reclasificaciones aprobadas que mueven dinero hacia o fuera de
    efectivo, por caja (§16 — "Ajustes de conciliación")."""
    ajustes: dict[str, Decimal] = {}
    pagos = PagoDeclarado.objects.filter(venta__dia=dia).exclude(medio_aprobado="").select_related("venta")
    for pago in pagos:
        caja = pago.venta.caja
        era_efectivo = pago.medio == PagoDeclarado.Medio.EFECTIVO
        es_efectivo = pago.medio_aprobado == PagoDeclarado.Medio.EFECTIVO
        if es_efectivo and not era_efectivo:
            ajustes[caja] = ajustes.get(caja, Decimal(0)) + pago.monto
        elif era_efectivo and not es_efectivo:
            ajustes[caja] = ajustes.get(caja, Decimal(0)) - pago.monto
    return ajustes


@transaction.atomic
def preparar_arqueos_del_dia(dia: DiaArqueo) -> list[ArqueoEfectivo]:
    """Crea (o actualiza el teórico/ajustes de) un ArqueoEfectivo por cada
    caja con ventas ese día. No toca `fondo_inicial`, `contado` ni nada ya
    registrado a mano."""
    teoricos = calcular_teorico_por_caja(dia)
    ajustes = calcular_ajustes_por_caja(dia)
    cajas = sorted(set(teoricos) | set(ajustes) | set(
        dia.ventas.values_list("caja", flat=True).distinct()
    ))

    arqueos = []
    for caja in cajas:
        teorico = teoricos.get(caja, Decimal(0))
        ajuste = ajustes.get(caja, Decimal(0))
        arqueo, _ = ArqueoEfectivo.objects.get_or_create(dia=dia, caja=caja)
        arqueo.teorico = teorico
        arqueo.ajustes = ajuste
        arqueo.esperado = teorico + ajuste
        if arqueo.contado is not None:
            arqueo.diferencia = arqueo.contado - (arqueo.esperado + arqueo.fondo_inicial - arqueo.gastos_autorizados)
        arqueo.save()
        arqueos.append(arqueo)
    return arqueos


class ConteoInvalido(Exception):
    pass


@transaction.atomic
def registrar_conteo(arqueo: ArqueoEfectivo, *, fondo_inicial: Decimal, gastos_autorizados: Decimal,
                      denominaciones: dict[int, int], usuario) -> ArqueoEfectivo:
    """Guarda el conteo físico por denominación (D8) y valida que la suma
    cuadre con el total contado — el propio conteo se calcula a partir de
    las denominaciones, así que "no cuadrar" solo puede pasar si se pide
    un total aparte; aquí el total ES la suma, por diseño."""
    total_contado = sum(Decimal(denom) * cantidad for denom, cantidad in denominaciones.items() if cantidad)
    if total_contado < 0:
        raise ConteoInvalido("El conteo no puede dar un total negativo.")

    valor_anterior = {"contado": str(arqueo.contado), "fondo_inicial": str(arqueo.fondo_inicial)}

    ConteoDenominacion.objects.filter(arqueo=arqueo).delete()
    ConteoDenominacion.objects.bulk_create([
        ConteoDenominacion(arqueo=arqueo, denominacion=denom, cantidad=cantidad)
        for denom, cantidad in denominaciones.items() if cantidad
    ])

    arqueo.fondo_inicial = fondo_inicial
    arqueo.gastos_autorizados = gastos_autorizados
    arqueo.contado = total_contado
    arqueo.diferencia = total_contado - (arqueo.esperado + fondo_inicial - gastos_autorizados)
    # Una diferencia nueva invalida cualquier autorización previa (no se
    # arrastra una autorización a un número distinto).
    arqueo.autorizado_por = None
    arqueo.motivo_autorizacion = None
    arqueo.save()

    EventoAuditoria.objects.create(
        dia=arqueo.dia, entidad="ArqueoEfectivo", entidad_id=str(arqueo.pk), accion="registrar_conteo",
        valor_anterior=valor_anterior,
        valor_posterior={"contado": str(total_contado), "diferencia": str(arqueo.diferencia)},
        usuario=usuario,
    )
    return arqueo


class AutorizacionInvalida(Exception):
    pass


@transaction.atomic
def autorizar_diferencia(arqueo: ArqueoEfectivo, *, motivo_id: int, comentario: str, usuario) -> ArqueoEfectivo:
    if arqueo.contado is None:
        raise AutorizacionInvalida("Esta caja todavía no tiene conteo registrado.")
    if abs(arqueo.diferencia) <= UMBRAL_DIFERENCIA_CAJA:
        raise AutorizacionInvalida("La diferencia está dentro del umbral; no requiere autorización.")
    if not motivo_id:
        raise AutorizacionInvalida("El motivo es obligatorio.")
    try:
        motivo = MotivoResolucion.objects.get(
            pk=motivo_id, aplica_a=MotivoResolucion.AplicaA.DIFERENCIA_CAJA, activo=True,
        )
    except (MotivoResolucion.DoesNotExist, ValueError):
        raise AutorizacionInvalida("Selecciona un motivo válido de la lista.")

    arqueo.autorizado_por = usuario
    arqueo.motivo_autorizacion = motivo
    arqueo.comentario = comentario
    arqueo.save()

    EventoAuditoria.objects.create(
        dia=arqueo.dia, entidad="ArqueoEfectivo", entidad_id=str(arqueo.pk), accion="autorizar_diferencia",
        valor_anterior={"autorizado_por": None},
        valor_posterior={"autorizado_por": usuario.username, "motivo": motivo.codigo, "comentario": comentario},
        usuario=usuario,
    )
    return arqueo
