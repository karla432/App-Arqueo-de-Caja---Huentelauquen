"""Arqueo de efectivo (diseño funcional §16).

El efectivo es el único medio sin contraparte electrónica: se controla
por diferencia entre lo que la venta implica y lo que hay físicamente,
**por caja**, nunca solo por el consolidado del día (control #9 — un
total en $0 que esconde un sobrante y un faltante en dos cajas distintas
no es un día cuadrado).
"""
from decimal import Decimal

from django.db import models, transaction

from nucleo.contexto_solicitud import solo_lectura_activo
from nucleo.permisos import Permiso, exigir
from nucleo.models import (
    ArqueoEfectivo, Cajera, DiaArqueo, EventoAuditoria,
    GastoCaja, MotivoResolucion, PagoDeclarado,
)
from nucleo.umbrales import umbral


def calcular_teorico_por_caja(dia: DiaArqueo) -> dict[str, Decimal]:
    """Efectivo teórico = pagos declarados en efectivo por la cajera (lo que
    dice el POS, sin correcciones), agrupados por caja. Las correcciones de
    forma de pago entran aparte, como ajustes — así el teórico no cambia y
    cada corrección se ve y se audita por separado.

    Incluye también las guías de despacho pagadas en efectivo (suman) y las
    notas de crédito devueltas en efectivo (restan) — Karla, 2026-09-23: no
    crean Venta/PagoDeclarado, pero el efectivo sí se movió en esa caja;
    omitirlo dejaría un sobrante o faltante falso en el arqueo."""
    from nucleo.regularizacion_servicio import efectivo_documentos_por_caja

    totales: dict[str, Decimal] = {}
    pagos = PagoDeclarado.objects.filter(venta__dia=dia, medio=PagoDeclarado.Medio.EFECTIVO).select_related("venta")
    for pago in pagos:
        caja = pago.venta.caja
        totales[caja] = totales.get(caja, Decimal(0)) + pago.monto
    for caja, monto in efectivo_documentos_por_caja(dia).items():
        totales[caja] = totales.get(caja, Decimal(0)) + monto
    return totales


def calcular_ajustes_por_caja(dia: DiaArqueo) -> dict[str, Decimal]:
    """Reclasificaciones de forma de pago que mueven dinero hacia o fuera de
    efectivo, por caja (§16 — "Ajustes de conciliación"). Salen de la tabla
    de trazabilidad: manuales, mezclas de medios y reglas automáticas."""
    ajustes: dict[str, Decimal] = {}
    for r in dia.reclasificaciones.select_related("pago__venta"):
        caja = r.pago.venta.caja
        if r.medio_corregido == PagoDeclarado.Medio.EFECTIVO and r.medio_original != PagoDeclarado.Medio.EFECTIVO:
            ajustes[caja] = ajustes.get(caja, Decimal(0)) + r.monto
        elif r.medio_original == PagoDeclarado.Medio.EFECTIVO and r.medio_corregido != PagoDeclarado.Medio.EFECTIVO:
            ajustes[caja] = ajustes.get(caja, Decimal(0)) - r.monto
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
    en_lectura = solo_lectura_activo()  # perfil de solo lectura: se calcula pero NO se guarda nada (ni filas ni datos derivados)
    existentes = {a.caja: a for a in ArqueoEfectivo.objects.filter(dia=dia)} if en_lectura else {}
    for caja in cajas:
        teorico = teoricos.get(caja, Decimal(0))
        ajuste = ajustes.get(caja, Decimal(0))
        if en_lectura:
            arqueo = existentes.get(caja) or ArqueoEfectivo(dia=dia, caja=caja)
        else:
            arqueo, _ = ArqueoEfectivo.objects.get_or_create(dia=dia, caja=caja)
        arqueo.teorico = teorico
        arqueo.ajustes = ajuste
        arqueo.esperado = teorico + ajuste
        if arqueo.contado is not None:
            arqueo.diferencia = arqueo.contado - (arqueo.esperado + arqueo.fondo_inicial - arqueo.gastos_autorizados)
        if not en_lectura:
            arqueo.save()
        arqueos.append(arqueo)
    return arqueos


class ConteoInvalido(Exception):
    pass


@transaction.atomic
def registrar_conteo(arqueo: ArqueoEfectivo, *, fondo_inicial: Decimal, gastos_autorizados: Decimal,
                      total_contado: Decimal, usuario) -> ArqueoEfectivo:
    """Guarda el total de efectivo contado de la caja. Karla (2026-09-21):
    la persona de ingresos digita solo el total por caja, no el desglose
    por denominación (D8 queda sin efecto)."""
    exigir(usuario, Permiso.OPERAR, "Tu perfil no puede registrar el conteo.")
    total_contado = Decimal(total_contado)
    if total_contado < 0:
        raise ConteoInvalido("El conteo no puede dar un total negativo.")

    valor_anterior = {"contado": str(arqueo.contado), "fondo_inicial": str(arqueo.fondo_inicial)}

    arqueo.fondo_inicial = fondo_inicial
    arqueo.gastos_autorizados = gastos_autorizados
    arqueo.contado = total_contado
    arqueo.diferencia = total_contado - (arqueo.esperado + fondo_inicial - gastos_autorizados)
    # Una diferencia nueva invalida cualquier autorización u visado previo
    # (no se arrastra una autorización a un número distinto).
    arqueo.autorizado_por = None
    arqueo.motivo_autorizacion = None
    arqueo.visado_por = None
    arqueo.visado_en = None
    arqueo.save()

    EventoAuditoria.objects.create(
        dia=arqueo.dia, entidad="ArqueoEfectivo", entidad_id=str(arqueo.pk), accion="registrar_conteo",
        valor_anterior=valor_anterior,
        valor_posterior={"contado": str(total_contado), "diferencia": str(arqueo.diferencia)},
        usuario=usuario,
    )

    # Las reglas de reclasificación automática dependen de lo contado: si el
    # día ya estaba conciliado (y no está aprobado), se vuelve a evaluar para
    # que un sobrante o faltante explicado se corrija de inmediato.
    dia = arqueo.dia
    if dia.corridas_motor.exists() and dia.estado in (
        DiaArqueo.Estado.CONCILIADO_CON_EXCEPCIONES, DiaArqueo.Estado.EXCEPCIONES_RESUELTAS,
    ):
        from nucleo.conciliacion_servicio import ejecutar_conciliacion
        ejecutar_conciliacion(dia, usuario)
        arqueo.refresh_from_db()
    return arqueo


class AutorizacionInvalida(Exception):
    pass


@transaction.atomic
def autorizar_diferencia(arqueo: ArqueoEfectivo, *, motivo_id: int, comentario: str, usuario) -> ArqueoEfectivo:
    exigir(usuario, Permiso.AUTORIZAR_EXCEPCION, "Solo la Gerente puede autorizar una diferencia sobre el umbral.")
    if arqueo.contado is None:
        raise AutorizacionInvalida("Esta caja todavía no tiene conteo registrado.")
    if abs(arqueo.diferencia) <= umbral("umbral_diferencia_caja"):
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


class AsignacionCajeraInvalida(Exception):
    pass


@transaction.atomic
def asignar_cajera(arqueo: ArqueoEfectivo, *, cajera_id: int, usuario) -> ArqueoEfectivo:
    """Quién operó esta caja este día (Karla, 2026-09-16) — permite luego
    analizar diferencias de arqueo por cajera, y atribuirle una eventual
    pérdida de caja para poder cobrársela."""
    exigir(usuario, Permiso.OPERAR, "Tu perfil no puede asignar cajeras.")
    if not cajera_id:
        raise AsignacionCajeraInvalida("Selecciona una cajera de la lista.")
    try:
        cajera = Cajera.objects.get(pk=cajera_id, activo=True)
    except (Cajera.DoesNotExist, ValueError):
        raise AsignacionCajeraInvalida("Selecciona una cajera válida de la lista.")

    anterior = arqueo.cajera.nombre if arqueo.cajera_id else None
    arqueo.cajera = cajera
    arqueo.save(update_fields=["cajera"])

    EventoAuditoria.objects.create(
        dia=arqueo.dia, entidad="ArqueoEfectivo", entidad_id=str(arqueo.pk), accion="asignar_cajera",
        valor_anterior={"cajera": anterior},
        valor_posterior={"cajera": cajera.nombre},
        usuario=usuario,
    )
    return arqueo


class GastoCajaInvalido(Exception):
    pass


@transaction.atomic
def registrar_gasto_caja(arqueo: ArqueoEfectivo, *, monto: Decimal, motivo_id: int,
                          boleta_asociada: str, comentario: str, usuario) -> GastoCaja:
    """Una línea de "Gastos autorizados" con trazabilidad (Karla,
    2026-09-18) — el caso que lo originó: una devolución en efectivo por
    una Nota de Crédito, pagada originalmente con Transbank. Sirve para
    que el arqueo de esa caja cuadre; el efecto contable completo de la
    NCR lo maneja Odoo cuando se emite el documento real, esta app no lo
    reemplaza."""
    exigir(usuario, Permiso.OPERAR, "Tu perfil no puede registrar gastos de caja.")
    if not monto or monto <= 0:
        raise GastoCajaInvalido("El monto debe ser mayor a cero.")
    if not motivo_id:
        raise GastoCajaInvalido("El motivo es obligatorio.")
    try:
        motivo = MotivoResolucion.objects.get(
            pk=motivo_id, aplica_a=MotivoResolucion.AplicaA.GASTO_CAJA, activo=True,
        )
    except (MotivoResolucion.DoesNotExist, ValueError):
        raise GastoCajaInvalido("Selecciona un motivo válido de la lista.")

    gasto = GastoCaja.objects.create(
        arqueo=arqueo, monto=monto, motivo=motivo, boleta_asociada=boleta_asociada.strip(),
        comentario=comentario, usuario=usuario,
    )

    valor_anterior_gastos = arqueo.gastos_autorizados
    arqueo.gastos_autorizados = arqueo.gastos.aggregate(total=models.Sum("monto"))["total"] or Decimal(0)
    if arqueo.contado is not None:
        arqueo.diferencia = arqueo.contado - (arqueo.esperado + arqueo.fondo_inicial - arqueo.gastos_autorizados)
        # Una diferencia nueva invalida cualquier autorización previa,
        # igual que al registrar un conteo nuevo.
        arqueo.autorizado_por = None
        arqueo.motivo_autorizacion = None
    arqueo.save()

    EventoAuditoria.objects.create(
        dia=arqueo.dia, entidad="ArqueoEfectivo", entidad_id=str(arqueo.pk), accion="registrar_gasto_caja",
        valor_anterior={"gastos_autorizados": str(valor_anterior_gastos)},
        valor_posterior={
            "gastos_autorizados": str(arqueo.gastos_autorizados), "monto": str(monto),
            "motivo": motivo.codigo, "boleta_asociada": boleta_asociada, "comentario": comentario,
        },
        usuario=usuario,
    )
    return gasto
