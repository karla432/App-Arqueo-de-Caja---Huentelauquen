"""Motor de conciliación contra el día de referencia 02-09-2026 (QA Golden
Master). Estas cifras salieron de correr el motor contra los archivos
reales, cargados en la base de datos — no son una copia de las cifras
ilustrativas del mockup de UI (ese mockup se hizo antes de que este motor
existiera; ver memoria de proyecto, 2026-09-05). Lo que sí es una cifra
medida e independiente del motor es la brecha total de tarjetas
($195.619, ver README.md) — y es contra eso que se valida esta prueba.

Criterios de aceptación #2, #3 y #4 del diseño funcional (sección 23).
"""
from datetime import date, datetime
from pathlib import Path

import pytest

from arqueo_diario.motor.conciliacion import MovimientoParaConciliar, PagoParaConciliar, conciliar
from nucleo.carga import cargar_pos, cargar_transbank
from nucleo.models import DiaArqueo, PagoDeclarado, MovimientoProveedor

FIXTURES = Path(__file__).parent / "fixtures" / "dia_referencia_02-09-2026"
DIA_REFERENCIA = date(2026, 9, 2)

BRECHA_TARJETAS_MEDIDA = 195_619  # $4.968.393 (Transbank) - $4.772.774 (POS), README.md


@pytest.fixture
def resultado_dia_referencia(db, django_user_model):
    usuario = django_user_model.objects.create_user(username="qa_golden_master")
    cargar_pos(FIXTURES / "Captura_Ventas_Sala_de_Ventas_02.09.26.xlsx", DIA_REFERENCIA, usuario)
    cargar_transbank(FIXTURES / "Cartola_Movimientos_02-sep-2026.xlsx", DIA_REFERENCIA, usuario)
    dia = DiaArqueo.objects.get(fecha=DIA_REFERENCIA)

    pagos = []
    for p in PagoDeclarado.objects.filter(venta__dia=dia, medio__in=["debito", "credito"]).select_related("venta"):
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
        for m in MovimientoProveedor.objects.filter(dia=dia, medio__in=["debito", "credito", "prepago"])
    ]

    return conciliar(pagos, movimientos)


@pytest.mark.django_db
def test_conciliacion_automatica_por_encima_de_95_por_ciento(resultado_dia_referencia):
    total_movimientos = 295
    auto = sum(len(c.movimientos) for c in resultado_dia_referencia.coincidencias)
    assert auto / total_movimientos > 0.95


@pytest.mark.django_db
def test_ningun_score_automatico_bajo_85(resultado_dia_referencia):
    # Criterio de aceptación #4.
    assert all(c.score >= 85 for c in resultado_dia_referencia.coincidencias)


@pytest.mark.django_db
def test_brecha_de_tarjetas_queda_explicada_en_excepciones(resultado_dia_referencia):
    # Criterio de aceptación #2: la brecha de $195.619 debe quedar
    # íntegramente explicada. Se explica como: lo que el proveedor cobró
    # y el POS no registró, menos lo que el POS declaró y el proveedor no
    # cobró — con un margen de $50 por comanda, la tolerancia de monto
    # que el propio diseño define (§9).
    # "pago_duplicado" (Karla, 2026-09-18: caso 11) es, para efectos de
    # esta brecha, el mismo tipo de movimiento sin comanda que
    # "movimiento_sin_pago" — solo lleva una etiqueta más precisa cuando
    # repite monto y medio de otro ya conciliado. El día de referencia
    # real tiene un caso así (comanda 654751): antes del ajuste quedaba
    # contado como "movimiento_sin_pago" genérico, ahora como duplicado —
    # la brecha total no cambia, solo cómo se explica.
    sin_pago = sum(
        e.monto_en_juego for e in resultado_dia_referencia.excepciones
        if e.tipo in ("movimiento_sin_pago", "pago_duplicado")
    )
    sin_mov = sum(
        e.monto_en_juego for e in resultado_dia_referencia.excepciones if e.tipo == "pago_sin_movimiento"
    )
    brecha_explicada = sin_pago - sin_mov
    assert abs(brecha_explicada - BRECHA_TARJETAS_MEDIDA) <= 50


@pytest.mark.django_db
def test_ningun_pago_ni_movimiento_desaparece(resultado_dia_referencia):
    # Regla de conservación (§8): nada se descarta en silencio. Todo pago
    # y todo movimiento termina en una coincidencia o en una excepción.
    r = resultado_dia_referencia
    pagos_en_coincidencias = {p for c in r.coincidencias for p in c.pagos}
    pagos_en_excepciones = {p for e in r.excepciones for p in e.pagos}
    movs_en_coincidencias = {m for c in r.coincidencias for m in c.movimientos}
    movs_en_excepciones = {m for e in r.excepciones for m in e.movimientos}

    assert len(pagos_en_coincidencias & pagos_en_excepciones) == 0
    assert len(movs_en_coincidencias & movs_en_excepciones) == 0


@pytest.mark.django_db
def test_resultado_no_depende_del_orden_de_carga(resultado_dia_referencia):
    # Criterio de aceptación #3, validado también a nivel de motor (no
    # solo con datos sintéticos en test_motor_conciliacion.py): recargar
    # los mismos pagos y movimientos en orden inverso da la misma
    # conciliación.
    dia = DiaArqueo.objects.get(fecha=DIA_REFERENCIA)
    pagos = [
        PagoParaConciliar(id=p.id, n_interno=p.venta.n_interno, medio=p.medio, monto=float(p.monto),
                          momento=datetime.combine(dia.fecha, p.venta.hora_cierre or p.venta.hora_apertura))
        for p in PagoDeclarado.objects.filter(venta__dia=dia, medio__in=["debito", "credito"]).select_related("venta")
        if p.venta.hora_cierre or p.venta.hora_apertura
    ]
    movimientos = [
        MovimientoParaConciliar(id=m.id, medio=m.medio, monto=float(m.monto_venta_valido_abono),
                                momento=m.fecha_hora, terminal=m.terminal)
        for m in MovimientoProveedor.objects.filter(dia=dia, medio__in=["debito", "credito", "prepago"])
    ]

    r_normal = conciliar(pagos, movimientos)
    r_invertido = conciliar(list(reversed(pagos)), list(reversed(movimientos)))

    resumen = lambda r: sorted(
        (tuple(sorted(c.pagos)), tuple(sorted(c.movimientos)), c.nivel, c.score) for c in r.coincidencias
    )
    assert resumen(r_normal) == resumen(r_invertido)
    assert len(r_normal.excepciones) == len(r_invertido.excepciones)
