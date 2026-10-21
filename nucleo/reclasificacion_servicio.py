"""Reclasificación de la forma de pago, con trazabilidad completa (Karla,
2026-09-21, Fase A).

La nota de venta conserva lo que declaró la cajera; cada corrección queda en
`Reclasificacion` con medio original, medio corregido, monto, motivo, cuándo,
quién (persona o regla automática) y la evidencia usada. No requiere
aprobación previa de la Gerente: se audita a posteriori con el reporte.

Reglas automáticas (solo actúan cuando la evidencia es única, nunca a la
fuerza — si hay más de una explicación posible el caso queda como excepción
con una pista):

* Tarjeta → efectivo: una venta declarada como Transbank/Mercado Pago que no
  aparece en el archivo del proveedor, en una caja que tiene un **sobrante**
  de efectivo por el mismo monto.
* Efectivo → tarjeta: un cobro del proveedor sin venta, en una caja que tiene
  un **faltante** de efectivo por el mismo monto y una venta en efectivo de
  ese monto.

Las reclasificaciones automáticas se recalculan en cada conciliación (se
revierten y se vuelven a evaluar), así que un conteo nuevo o un archivo nuevo
nunca deja una corrección vieja pegada. Cada creación queda además en la
bitácora inmutable.
"""
from datetime import datetime
from itertools import combinations

from nucleo.models import EventoAuditoria, PagoDeclarado, Reclasificacion
from nucleo.umbrales import umbral

REGLA_TARJETA_A_EFECTIVO = "tarjeta_a_efectivo_por_sobrante"
REGLA_EFECTIVO_A_TARJETA = "efectivo_a_tarjeta_por_faltante"
REGLA_MEDIO_CONTRADICTORIO = "medio_contradictorio_aceptado"
REGLA_MEZCLA_DE_MEDIOS = "mezcla_de_medios"
REGLA_CORRECCION_MANUAL_DE_MEDIO = "correccion_manual_de_medio"

NOMBRE_DE_REGLA = {
    REGLA_TARJETA_A_EFECTIVO: "Declarada como tarjeta, no aparece en el proveedor y sobra efectivo",
    REGLA_EFECTIVO_A_TARJETA: "Declarada como efectivo, hay un cobro del proveedor sin venta y falta efectivo",
    REGLA_MEDIO_CONTRADICTORIO: "Medio contradictorio aceptado por una persona",
    REGLA_MEZCLA_DE_MEDIOS: "Mezcla de medios resuelta por una persona",
    REGLA_CORRECCION_MANUAL_DE_MEDIO: "Boleta registrada con un medio y cobrada con otro (corregida por una persona)",
}

MAX_PAGOS_POR_COMBINACION = 3
MAX_CANDIDATOS = 40  # tope de seguridad para no explotar combinando


def _medio_del_proveedor(medio: str) -> str:
    """"Prepago" no es un medio del POS: se corrige al más cercano, débito."""
    return PagoDeclarado.Medio.DEBITO if medio == "prepago" else medio


def registrar_manual(*, pago, medio_corregido, monto, regla, motivo, usuario, excepcion=None, evidencia=None):
    medio_original = pago.medio
    if medio_corregido == medio_original:
        return None
    return Reclasificacion.objects.create(
        dia=pago.venta.dia, pago=pago, medio_original=medio_original, medio_corregido=medio_corregido,
        monto=monto, origen=Reclasificacion.Origen.MANUAL, regla=regla, motivo=motivo,
        evidencia=evidencia or {}, usuario=usuario, excepcion=excepcion,
    )


def revertir_automaticas(dia) -> int:
    """Deshace las reclasificaciones de reglas automáticas del día para
    volver a evaluarlas con los datos vigentes. Las manuales no se tocan."""
    automaticas = list(Reclasificacion.objects.filter(dia=dia, origen=Reclasificacion.Origen.AUTOMATICA).select_related("pago"))
    for r in automaticas:
        if r.pago.medio_aprobado == r.medio_corregido:
            r.pago.medio_aprobado = ""
            r.pago.save(update_fields=["medio_aprobado"])
    Reclasificacion.objects.filter(pk__in=[r.pk for r in automaticas]).delete()
    return len(automaticas)


def _combinaciones(items, objetivo, tolerancia):
    """Todas las combinaciones (hasta 3) de (id, monto) que suman el objetivo."""
    items = items[:MAX_CANDIDATOS]
    encontradas = []
    for n in range(1, MAX_PAGOS_POR_COMBINACION + 1):
        for combo in combinations(items, n):
            if abs(sum(m for _, m in combo) - objetivo) <= tolerancia:
                encontradas.append(combo)
    return encontradas


def _minutos_entre(pago, movimiento):
    hora = pago.venta.hora_cierre or pago.venta.hora_apertura
    if hora is None:
        return None
    momento = datetime.combine(movimiento.fecha_hora.date(), hora)
    return abs((movimiento.fecha_hora.replace(tzinfo=None) - momento).total_seconds()) / 60


def _crear_automatica(*, pago, medio_corregido, regla, motivo, evidencia, usuario):
    pago.medio_aprobado = medio_corregido
    pago.save(update_fields=["medio_aprobado"])
    r = Reclasificacion.objects.create(
        dia=pago.venta.dia, pago=pago, medio_original=pago.medio, medio_corregido=medio_corregido,
        monto=pago.monto, origen=Reclasificacion.Origen.AUTOMATICA, regla=regla, motivo=motivo,
        evidencia=evidencia,
    )
    EventoAuditoria.objects.create(
        dia=pago.venta.dia, entidad="Reclasificacion", entidad_id=str(r.pk), accion="reclasificacion_automatica",
        valor_anterior={"medio": pago.medio, "pago_id": pago.pk},
        valor_posterior={
            "medio_corregido": medio_corregido, "monto": str(pago.monto), "regla": regla, "evidencia": evidencia,
            "nota": "Regla automática ejecutada durante la conciliación",
        },
        usuario=usuario,
    )
    return r


def aplicar_reglas(dia, resultado, *, pagos_por_id_db, movimientos_por_id_db, arqueos, usuario) -> dict:
    """Aplica las reglas automáticas sobre el resultado del motor (antes de
    persistirlo). Quita del resultado las excepciones que la regla explica y
    agrega, cuando corresponde, la coincidencia; a los casos ambiguos les deja
    una pista en la decisión propuesta. Devuelve un resumen."""
    from arqueo_diario.motor.conciliacion import Coincidencia as CoincidenciaMotor
    from nucleo.anulaciones_servicio import es_venta_anulada

    tolerancia = float(umbral("tolerancia_sencillo"))
    resumen = {"tarjeta_a_efectivo": 0, "efectivo_a_tarjeta": 0, "ambiguos": 0}

    diferencia_por_caja = {a.caja: a for a in arqueos if a.contado is not None}
    if not diferencia_por_caja:
        return resumen

    # ---------------------------------------------- tarjeta -> efectivo
    for caja, arqueo in diferencia_por_caja.items():
        sobrante = float(arqueo.diferencia)
        if sobrante <= tolerancia:
            continue
        candidatos = {}
        for e in resultado.excepciones:
            if e.tipo != "pago_sin_movimiento" or len(e.pagos) != 1:
                continue
            pago = pagos_por_id_db.get(e.pagos[0])
            if pago and pago.venta.caja == caja and not pago.medio_aprobado:
                candidatos[pago.pk] = (e, pago)
        soluciones = _combinaciones([(pid, float(p.monto)) for pid, (_, p) in candidatos.items()], sobrante, tolerancia)
        if len(soluciones) == 1:
            for pid, _monto in soluciones[0]:
                e, pago = candidatos[pid]
                resultado.excepciones.remove(e)
                _crear_automatica(
                    pago=pago, medio_corregido=PagoDeclarado.Medio.EFECTIVO, regla=REGLA_TARJETA_A_EFECTIVO,
                    motivo=(
                        f"Declarada como {pago.get_medio_display()}, sin cobro del proveedor por ${pago.monto:,.0f}, "
                        f"y {caja} tiene un sobrante de efectivo de ${sobrante:,.0f}."
                    ),
                    evidencia={
                        "caja": caja, "sobrante_efectivo": sobrante, "efectivo_contado": float(arqueo.contado),
                        "busqueda_proveedor": "Sin movimiento del proveedor por el mismo monto y medio",
                        "nota_de_venta": pago.venta.n_interno,
                    },
                    usuario=usuario,
                )
                resumen["tarjeta_a_efectivo"] += 1
        elif len(soluciones) > 1:
            resumen["ambiguos"] += 1
            for pid, (e, _p) in candidatos.items():
                if any(pid == x for combo in soluciones for x, _ in combo):
                    e.decision_propuesta += (
                        f" Posible pago en efectivo declarado como tarjeta: {caja} tiene un sobrante de "
                        f"${sobrante:,.0f}, pero hay más de una venta que podría explicarlo."
                    )

    # ---------------------------------------------- efectivo -> tarjeta
    efectivo_por_caja: dict[str, list] = {}
    for pago in PagoDeclarado.objects.filter(
        venta__dia=dia, medio=PagoDeclarado.Medio.EFECTIVO, medio_aprobado="",
    ).select_related("venta"):
        if not es_venta_anulada(pago.venta):
            efectivo_por_caja.setdefault(pago.venta.caja, []).append(pago)

    movimientos_sin_venta = {
        e.movimientos[0]: e for e in resultado.excepciones
        if e.tipo == "movimiento_sin_pago" and len(e.movimientos) == 1
    }
    for caja, arqueo in diferencia_por_caja.items():
        faltante = -float(arqueo.diferencia)
        if faltante <= tolerancia or not movimientos_sin_venta:
            continue
        pagos_caja = list(efectivo_por_caja.get(caja, []))
        candidatos_mov = []
        for mid in movimientos_sin_venta:
            m = movimientos_por_id_db[mid]
            if any(float(p.monto) == float(m.monto_venta_valido_abono) for p in pagos_caja):
                candidatos_mov.append((mid, float(m.monto_venta_valido_abono)))
        soluciones = _combinaciones(candidatos_mov, faltante, tolerancia)
        if len(soluciones) != 1:
            if len(soluciones) > 1:
                resumen["ambiguos"] += 1
                for mid, _ in candidatos_mov:
                    movimientos_sin_venta[mid].decision_propuesta += (
                        f" Posible venta en efectivo declarada mal: {caja} tiene un faltante de "
                        f"${faltante:,.0f}, pero hay más de un cobro que podría explicarlo."
                    )
            continue

        asignacion, usados = [], set()
        for mid, monto in soluciones[0]:
            m = movimientos_por_id_db[mid]
            posibles = [p for p in pagos_caja if float(p.monto) == monto and p.pk not in usados]
            if len(posibles) > 1:
                # Desempate por hora: solo cuentan las ventas cerradas a
                # menos de 10 minutos del cobro del proveedor.
                posibles = [
                    p for p in posibles
                    if _minutos_entre(p, m) is not None and _minutos_entre(p, m) <= 10
                ]
            if len(posibles) != 1:
                asignacion = None
                break
            usados.add(posibles[0].pk)
            asignacion.append((mid, posibles[0]))
        if not asignacion:
            resumen["ambiguos"] += 1
            continue

        for mid, pago in asignacion:
            m = movimientos_por_id_db[mid]
            medio_nuevo = _medio_del_proveedor(m.medio)
            resultado.excepciones.remove(movimientos_sin_venta.pop(mid))
            pagos_caja.remove(pago)
            _crear_automatica(
                pago=pago, medio_corregido=medio_nuevo, regla=REGLA_EFECTIVO_A_TARJETA,
                motivo=(
                    f"Declarada como efectivo, pero el proveedor informa un cobro de ${m.monto_venta_valido_abono:,.0f} "
                    f"sin venta, y {caja} tiene un faltante de efectivo de ${faltante:,.0f}."
                ),
                evidencia={
                    "caja": caja, "faltante_efectivo": faltante, "efectivo_contado": float(arqueo.contado),
                    "proveedor": m.proveedor, "movimiento_id": m.pk, "autorizacion": m.autorizacion,
                    "terminal": m.terminal, "monto_movimiento": float(m.monto_venta_valido_abono),
                    "hora_movimiento": m.fecha_hora.strftime("%H:%M"), "nota_de_venta": pago.venta.n_interno,
                },
                usuario=usuario,
            )
            resultado.coincidencias.append(CoincidenciaMotor(
                pagos=[pago.pk], movimientos=[mid], nivel=4, score=80, diferencia_monto=0.0,
                diferencia_hora_segundos=0.0, origen="reclasificacion_automatica",
                detalle="Efectivo declarado que en realidad fue tarjeta (regla automática).",
            ))
            resumen["efectivo_a_tarjeta"] += 1

    return resumen
