"""Corre el motor de conciliación sobre un día ya cargado y persiste el
resultado: Coincidencias automáticas, Excepciones para revisión humana, y
la versión de reglas usada (control C11 — un día conciliado debe poder
explicarse con las reglas de entonces, no con las de hoy).

Reconciliar es idempotente (diseño funcional §5): puede repetirse cuantas
veces se quiera. Solo se recalcula lo automático; una excepción que ya
tiene una resolución manual no se toca ni se borra.
"""
from datetime import datetime

from django.db import transaction

from django.utils import timezone

from arqueo_diario.motor.conciliacion import (
    TOLERANCIA_MONTO, UMBRAL_AUTOMATICO, VENTANA_NORMAL,
    MovimientoParaConciliar, PagoParaConciliar, conciliar,
)
from nucleo.permisos import Permiso, exigir
from nucleo.umbrales import umbral
from nucleo.arqueo_servicio import preparar_arqueos_del_dia
from nucleo.reclasificacion_servicio import aplicar_reglas, revertir_automaticas
from nucleo.regularizacion_servicio import calzar_guias_con_cobros
from nucleo.anulaciones_servicio import es_venta_anulada, procesar_anulaciones
from nucleo.models import (
    Coincidencia as CoincidenciaDB, DiaArqueo, EventoAuditoria, cuenta_por_signo,
    Excepcion as ExcepcionDB, MotivoResolucion, MovimientoProveedor, PagoDeclarado, VersionMotor,
)

VERSION_MOTOR = "1.1.0"
MOTIVO_AJUSTE_SENCILLO = "ajuste_sencillo"


def _registrar_ajuste_sencillo(dia, usuario, pago, movimiento, tolerancia):
    """Diferencia de hasta $10 entre el POS y el proveedor (Karla,
    2026-09-21): el par se calza solo y la diferencia va a la cuenta de
    pérdida o de ganancia según el signo (Karla, 2026-09-21: "solo
    ocuparemos dos cuentas... siempre estas cuentas"), con el motivo
    "Ajuste de sencillo". Signo: POS − proveedor (positivo = el proveedor
    cobró menos que el POS → pérdida; negativo = cobró más → ganancia)."""
    diferencia = pago.monto - movimiento.monto_venta_valido_abono
    cuenta = cuenta_por_signo(diferencia)
    motivo = MotivoResolucion.objects.get(codigo=MOTIVO_AJUSTE_SENCILLO)
    ExcepcionDB.objects.create(
        dia=dia, tipo=ExcepcionDB.Tipo.DIFERENCIA_MONTO, monto_en_juego=abs(diferencia),
        pago=pago, movimiento=movimiento,
        decision_propuesta=f"Diferencia de ${diferencia:,.0f} dentro de la tolerancia de ${tolerancia:,.0f}.",
        abierta=False, resolucion=ExcepcionDB.Resolucion.AJUSTE_SENCILLO, motivo=motivo,
        comentario="Calce automático por ajuste de sencillo.",
        resuelta_por=usuario, resuelta_en=timezone.now(),
        cuenta_contable=cuenta, monto_perdida_ganancia=abs(diferencia),
    )


def _registrar_ajuste_guia(dia, usuario, guia, movimiento, tolerancia):
    """Diferencia entre una guía pagada con tarjeta y su cobro del proveedor
    (Karla, 2026-09-24: ej. guía $459.554 vs cobro $459.555). Va a la cuenta de
    pérdida o ganancia según el signo — guía − cobro: positivo = el proveedor
    cobró menos → pérdida — igual que el ajuste de sencillo, y sin cajera
    responsable (es automático)."""
    diferencia = guia.monto - movimiento.monto_venta_valido_abono
    ExcepcionDB.objects.create(
        dia=dia, tipo=ExcepcionDB.Tipo.DIFERENCIA_MONTO, monto_en_juego=abs(diferencia),
        movimiento=movimiento,
        decision_propuesta=f"Guía {guia.folio}: diferencia de ${diferencia:,.0f} con su cobro, dentro de la tolerancia de ${tolerancia:,.0f}.",
        abierta=False, resolucion=ExcepcionDB.Resolucion.AJUSTE_SENCILLO,
        motivo=MotivoResolucion.objects.get(codigo=MOTIVO_AJUSTE_SENCILLO),
        comentario=f"Calce automático de la guía {guia.folio} por horario y monto (tolerancia de guías).",
        resuelta_por=usuario, resuelta_en=timezone.now(),
        cuenta_contable=cuenta_por_signo(diferencia), monto_perdida_ganancia=abs(diferencia),
    )


@transaction.atomic
def ejecutar_conciliacion(dia: DiaArqueo, usuario):
    exigir(usuario, Permiso.OPERAR, "Tu perfil no puede ejecutar la conciliación.")
    # No se tocan las excepciones ya resueltas por una persona — solo se
    # limpian las coincidencias automáticas y las excepciones abiertas de
    # la corrida anterior, antes de escribir las nuevas. Las Coincidencia
    # de origen MANUAL (creadas al resolver una excepción a mano) nunca se
    # borran: si se borraran, el pago/movimiento que ya quedó resuelto
    # volvería a la bolsa de conciliación y podría reconciliarse de nuevo
    # (o generar una excepción nueva) sin que nadie lo pidiera — control
    # hallado en la auditoría QA de 2026-09-18, antes esto podía duplicar
    # un ingreso ya facturado como "otros ingresos".
    CoincidenciaDB.objects.filter(dia=dia, origen=CoincidenciaDB.Origen.AUTOMATICO).delete()
    ExcepcionDB.objects.filter(dia=dia, abierta=True).exclude(tipo="anulacion_descalzada").delete()
    # Los ajustes de sencillo los genera el propio motor (no son una
    # decisión humana): se recalculan en cada corrida, igual que las
    # coincidencias automáticas.
    ExcepcionDB.objects.filter(dia=dia, resolucion=ExcepcionDB.Resolucion.AJUSTE_SENCILLO).delete()

    # Las reclasificaciones automáticas se vuelven a evaluar desde cero con
    # los datos vigentes (archivos y conteo de caja actuales).
    revertir_automaticas(dia)
    arqueos = preparar_arqueos_del_dia(dia)

    # Las anulaciones de Transbank (§8, caso de uso de Karla 2026-09-06)
    # se resuelven antes de armar la conciliación normal: los pares que
    # se compensan solos ni siquiera llegan al motor.
    resultado_anulaciones = procesar_anulaciones(dia)

    # Un pago o movimiento que ya fue objeto de una resolución humana
    # (cualquiera: aceptar, rechazar, vincular otro, pérdida/ganancia) no
    # vuelve a entrar a la bolsa de conciliación — esa decisión ya se tomó
    # y no se relitiga sola en la siguiente corrida.
    excepciones_resueltas = ExcepcionDB.objects.filter(dia=dia, abierta=False)
    pagos_ya_resueltos = set(excepciones_resueltas.exclude(pago=None).values_list("pago_id", flat=True))
    movimientos_ya_resueltos = set(
        excepciones_resueltas.exclude(movimiento=None).values_list("movimiento_id", flat=True)
    )

    pagos_db = [
        p for p in PagoDeclarado.objects.filter(venta__dia=dia, medio__in=["debito", "credito"])
        .exclude(pk__in=pagos_ya_resueltos)
        .select_related("venta")
        if not es_venta_anulada(p.venta)
    ]
    movimientos_db = list(
        MovimientoProveedor.objects.filter(dia=dia, medio__in=["debito", "credito", "prepago"])
        .exclude(pk__in=resultado_anulaciones["ids_excluidos"])
        .exclude(pk__in=movimientos_ya_resueltos)
    )

    pagos = []
    for p in pagos_db:
        hora = p.venta.hora_cierre or p.venta.hora_apertura
        if hora is None:
            continue
        pagos.append(PagoParaConciliar(
            id=p.id, n_interno=p.venta.n_interno, medio=p.medio_aprobado or p.medio, monto=float(p.monto),
            momento=datetime.combine(dia.fecha, hora),
        ))

    movimientos = [
        MovimientoParaConciliar(
            id=m.id, medio=m.medio, monto=float(m.monto_venta_valido_abono),
            momento=m.fecha_hora, terminal=m.terminal,
        )
        for m in movimientos_db
    ]

    tolerancia_sencillo = float(umbral("tolerancia_sencillo"))
    resultado = conciliar(pagos, movimientos, tolerancia_sencillo=tolerancia_sencillo)

    pagos_por_id_db = {p.id: p for p in pagos_db}
    movimientos_por_id_db = {m.id: m for m in movimientos_db}
    calzar_guias_con_cobros(dia, resultado, movimientos_por_id_db, usuario)
    resumen_reglas = aplicar_reglas(
        dia, resultado, pagos_por_id_db=pagos_por_id_db, movimientos_por_id_db=movimientos_por_id_db,
        arqueos=arqueos, usuario=usuario,
    )
    ajustes_sencillo = 0

    corrida = VersionMotor.objects.create(
        dia=dia, version_motor=VERSION_MOTOR,
        parametros={
            "tolerancia_monto": TOLERANCIA_MONTO,
            "ventana_normal_segundos": VENTANA_NORMAL.total_seconds(),
            "tolerancia_sencillo": tolerancia_sencillo,
            "umbral_automatico": UMBRAL_AUTOMATICO,
            "prepago_compatible_con": "debito",
        },
        ejecutado_por=usuario,
    )

    for c in resultado.coincidencias:
        coincidencia = CoincidenciaDB.objects.create(
            dia=dia, corrida=corrida, nivel=c.nivel, score=c.score,
            diferencia_monto=c.diferencia_monto,
            diferencia_hora_segundos=int(c.diferencia_hora_segundos),
            origen=CoincidenciaDB.Origen.AUTOMATICO,
        )
        coincidencia.pagos.set(c.pagos)
        coincidencia.movimientos.set(c.movimientos)

        if (0 < c.diferencia_monto <= tolerancia_sencillo and c.nivel == 3
                and len(c.pagos) == 1 and len(c.movimientos) == 1):
            _registrar_ajuste_sencillo(
                dia, usuario, pagos_por_id_db[c.pagos[0]], movimientos_por_id_db[c.movimientos[0]], tolerancia_sencillo,
            )
            ajustes_sencillo += 1

    for e in resultado.excepciones:
        detalle = e.decision_propuesta
        if len(e.pagos) > 1 or len(e.movimientos) > 1:
            detalle += f" (pagos: {e.pagos}, movimientos: {e.movimientos})"
        ExcepcionDB.objects.create(
            dia=dia, tipo=e.tipo, monto_en_juego=e.monto_en_juego,
            pago_id=e.pagos[0] if e.pagos else None,
            movimiento_id=e.movimientos[0] if e.movimientos else None,
            decision_propuesta=detalle,
        )

    preparar_arqueos_del_dia(dia)  # el efectivo esperado ya incluye las reclasificaciones

    dia.estado = (
        DiaArqueo.Estado.CONCILIADO_CON_EXCEPCIONES if resultado.excepciones
        else DiaArqueo.Estado.EXCEPCIONES_RESUELTAS
    )
    dia.operado_por = dia.operado_por or usuario
    dia.save(update_fields=["estado", "operado_por"])

    EventoAuditoria.objects.create(
        dia=dia, entidad="DiaArqueo", entidad_id=str(dia.pk), accion="ejecutar_conciliacion",
        valor_posterior={
            "coincidencias": len(resultado.coincidencias),
            "excepciones": len(resultado.excepciones),
            "ajustes_de_sencillo": ajustes_sencillo,
            "reclasificaciones_automaticas": resumen_reglas,
            "anulaciones_excluidas": len(resultado_anulaciones["ids_excluidos"]),
            "anulaciones_con_excepcion": resultado_anulaciones["excepciones_creadas"],
            "version_motor": VERSION_MOTOR,
        },
        usuario=usuario,
    )

    return resultado
