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

from arqueo_diario.motor.conciliacion import (
    TOLERANCIA_MONTO, UMBRAL_AUTOMATICO, VENTANA_NORMAL,
    MovimientoParaConciliar, PagoParaConciliar, conciliar,
)
from nucleo.anulaciones_servicio import es_venta_anulada, procesar_anulaciones
from nucleo.models import (
    Coincidencia as CoincidenciaDB, DiaArqueo, EventoAuditoria,
    Excepcion as ExcepcionDB, MovimientoProveedor, PagoDeclarado, VersionMotor,
)

VERSION_MOTOR = "1.0.0"


@transaction.atomic
def ejecutar_conciliacion(dia: DiaArqueo, usuario):
    # No se tocan las excepciones ya resueltas por una persona — solo se
    # limpian las coincidencias automáticas y las excepciones abiertas de
    # la corrida anterior, antes de escribir las nuevas.
    CoincidenciaDB.objects.filter(dia=dia).delete()
    ExcepcionDB.objects.filter(dia=dia, abierta=True).exclude(tipo="anulacion_descalzada").delete()

    # Las anulaciones de Transbank (§8, caso de uso de Karla 2026-09-06)
    # se resuelven antes de armar la conciliación normal: los pares que
    # se compensan solos ni siquiera llegan al motor.
    resultado_anulaciones = procesar_anulaciones(dia)

    pagos_db = [
        p for p in PagoDeclarado.objects.filter(venta__dia=dia, medio__in=["debito", "credito"])
        .select_related("venta")
        if not es_venta_anulada(p.venta)
    ]
    movimientos_db = list(
        MovimientoProveedor.objects.filter(dia=dia, medio__in=["debito", "credito", "prepago"])
        .exclude(pk__in=resultado_anulaciones["ids_excluidos"])
    )

    pagos = []
    for p in pagos_db:
        hora = p.venta.hora_cierre or p.venta.hora_apertura
        if hora is None:
            continue
        pagos.append(PagoParaConciliar(
            id=p.id, n_interno=p.venta.n_interno, medio=p.medio, monto=float(p.monto),
            momento=datetime.combine(dia.fecha, hora),
        ))

    movimientos = [
        MovimientoParaConciliar(
            id=m.id, medio=m.medio, monto=float(m.monto_venta_valido_abono),
            momento=m.fecha_hora, terminal=m.terminal,
        )
        for m in movimientos_db
    ]

    resultado = conciliar(pagos, movimientos)

    corrida = VersionMotor.objects.create(
        dia=dia, version_motor=VERSION_MOTOR,
        parametros={
            "tolerancia_monto": TOLERANCIA_MONTO,
            "ventana_normal_segundos": VENTANA_NORMAL.total_seconds(),
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
            "anulaciones_excluidas": len(resultado_anulaciones["ids_excluidos"]),
            "anulaciones_con_excepcion": resultado_anulaciones["excepciones_creadas"],
            "version_motor": VERSION_MOTOR,
        },
        usuario=usuario,
    )

    return resultado
