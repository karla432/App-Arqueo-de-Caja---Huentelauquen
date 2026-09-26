"""Pruebas del motor de conciliación con escenarios sintéticos pequeños —
uno por cada regla de la sección 9-15 del diseño funcional. Los criterios
de aceptación #3, #4 y #7 son verificables aquí sin tocar ningún archivo
real.
"""
from datetime import datetime, timedelta

from arqueo_diario.motor.conciliacion import (
    MovimientoParaConciliar, PagoParaConciliar, conciliar,
)

D = datetime(2026, 9, 2)


def _pago(id, medio, monto, hora, minuto):
    return PagoParaConciliar(id=id, n_interno=100 + id, medio=medio, monto=monto,
                              momento=D.replace(hour=hora, minute=minuto))


def _mov(id, medio, monto, hora, minuto):
    return MovimientoParaConciliar(id=id, medio=medio, monto=monto,
                                    momento=D.replace(hour=hora, minute=minuto))


def test_nivel_3_monto_medio_y_hora_exactos_es_automatico():
    pagos = [_pago(1, "debito", 10000, 10, 0)]
    movs = [_mov(1, "debito", 10000, 10, 1)]
    r = conciliar(pagos, movs)
    assert len(r.coincidencias) == 1
    c = r.coincidencias[0]
    assert c.nivel == 3
    assert c.score >= 85  # criterio de aceptación #4
    assert not r.excepciones


def test_nivel_4_medio_distinto_nunca_es_automatico():
    # Monto y hora calzan, pero el medio no: pasa a excepción, no se
    # asigna solo (§9: "del 4 al 6 siempre pasan por decisión humana").
    pagos = [_pago(1, "debito", 17600, 14, 2)]
    movs = [_mov(1, "credito", 17600, 14, 3)]
    r = conciliar(pagos, movs)
    assert not r.coincidencias
    assert len(r.excepciones) == 1
    assert r.excepciones[0].tipo == "medio_contradictorio"


def test_prepago_se_trata_como_compatible_con_debito():
    # Supuesto documentado en el motor: el POS no distingue "prepago".
    pagos = [_pago(1, "debito", 5000, 12, 0)]
    movs = [_mov(1, "prepago", 5000, 12, 1)]
    r = conciliar(pagos, movs)
    assert len(r.coincidencias) == 1
    assert r.coincidencias[0].nivel == 3


def test_diferencia_de_monto_dentro_de_tolerancia_es_excepcion():
    pagos = [_pago(1, "debito", 20000, 9, 0)]
    movs = [_mov(1, "debito", 19980, 9, 1)]  # $20 de diferencia, <= $50
    r = conciliar(pagos, movs)
    assert not r.coincidencias
    assert r.excepciones[0].tipo == "diferencia_monto"


def test_ajuste_de_sencillo_hasta_10_pesos_se_calza_solo_en_ambos_sentidos():
    # Karla, 2026-09-21: hasta $10 de diferencia, positiva o negativa, se
    # calza automáticamente (con la diferencia registrada aparte).
    for monto_proveedor in (10001, 9990, 10010, 9999):
        r = conciliar([_pago(1, "debito", 10000, 9, 0)], [_mov(1, "debito", monto_proveedor, 9, 1)])
        assert len(r.coincidencias) == 1, monto_proveedor
        assert r.coincidencias[0].score >= 85
        assert r.coincidencias[0].diferencia_monto == abs(10000 - monto_proveedor)
        assert not r.excepciones


def test_diferencia_de_11_pesos_sigue_siendo_excepcion_humana():
    r = conciliar([_pago(1, "debito", 10000, 9, 0)], [_mov(1, "debito", 10011, 9, 1)])
    assert not r.coincidencias
    assert r.excepciones[0].tipo == "diferencia_monto"


def test_ajuste_de_sencillo_exige_el_mismo_medio():
    r = conciliar([_pago(1, "debito", 10000, 9, 0)], [_mov(1, "credito", 10001, 9, 1)])
    assert not r.coincidencias  # con otro medio no se calza solo


def test_exacto_tiene_prioridad_sobre_el_ajuste_de_sencillo():
    pagos = [_pago(1, "debito", 10000, 9, 0), _pago(2, "debito", 10001, 9, 0)]
    movs = [_mov(1, "debito", 10000, 9, 1), _mov(2, "debito", 10001, 9, 1)]
    r = conciliar(pagos, movs)
    assert len(r.coincidencias) == 2
    assert all(c.diferencia_monto == 0 for c in r.coincidencias)


def test_fuera_de_tolerancia_no_es_candidato_en_ningun_nivel():
    pagos = [_pago(1, "debito", 20000, 9, 0)]
    movs = [_mov(1, "debito", 19900, 9, 1)]  # $100 de diferencia, > $50
    r = conciliar(pagos, movs)
    assert not r.coincidencias
    tipos = {e.tipo for e in r.excepciones}
    assert tipos == {"pago_sin_movimiento", "movimiento_sin_pago"}


def test_fuera_de_ventana_horaria_no_concilia():
    pagos = [_pago(1, "debito", 10000, 10, 0)]
    movs = [_mov(1, "debito", 10000, 10, 25)]  # 25 min, fuera de ±10
    r = conciliar(pagos, movs)
    assert not r.coincidencias


def test_montos_repetidos_elige_el_horario_mas_cercano():
    # Dos movimientos del mismo monto; el pago debe preferir el más
    # cercano en el tiempo, no el primero en aparecer en la lista.
    pagos = [_pago(1, "debito", 43550, 10, 50)]
    movs = [_mov(1, "debito", 43550, 10, 49), _mov(2, "debito", 43550, 10, 50)]
    r = conciliar(pagos, movs)
    assert len(r.coincidencias) == 1
    assert r.coincidencias[0].movimientos == [2]  # el de las 10:50, diferencia 0
    # El otro movimiento queda como excepción real, no se descarta — y
    # como repite monto y medio del que sí se concilió, se marca como
    # posible pago duplicado (Karla, 2026-09-18: caso 11), no como un
    # "movimiento sin pago" genérico.
    assert any(e.tipo == "pago_duplicado" and e.movimientos == [1] for e in r.excepciones)


def test_ambiguedad_real_no_asigna_ninguno():
    # Dos pagos, un solo movimiento candidato, mismo monto y misma
    # distancia horaria a ambos: empate real (§10, quinta regla).
    pagos = [_pago(1, "debito", 9200, 14, 25), _pago(2, "debito", 9200, 14, 29)]
    movs = [_mov(1, "debito", 9200, 14, 27)]
    r = conciliar(pagos, movs)
    assert not r.coincidencias
    assert len(r.excepciones) == 1
    assert r.excepciones[0].tipo == "ambiguedad"
    assert set(r.excepciones[0].pagos) == {1, 2}


def test_equivalencia_indiferente_no_genera_excepcion():
    # Dos pagos y dos movimientos idénticos en monto, medio y minuto
    # (§12): el total cuadra igual sin importar cuál se empareja con
    # cuál, así que se resuelve solo, sin molestar al operador. Cuando
    # las DOS parejas alternativas son igual de válidas, cada candidato
    # "libre" que se revisa para detectar empate ya fue tomado por la
    # otra pareja equivalente — por eso no siempre queda etiquetado como
    # "equivalencia_indiferente", pero el resultado (todos emparejados,
    # cero excepciones) es el que importa.
    pagos = [_pago(1, "debito", 5000, 14, 5), _pago(2, "debito", 5000, 14, 5)]
    movs = [_mov(1, "debito", 5000, 14, 5), _mov(2, "debito", 5000, 14, 5)]
    r = conciliar(pagos, movs)
    assert len(r.coincidencias) == 2
    assert not r.excepciones
    assert {p for c in r.coincidencias for p in c.pagos} == {1, 2}
    assert {m for c in r.coincidencias for m in c.movimientos} == {1, 2}


def test_resultado_no_depende_del_orden_de_las_filas():
    # Criterio de aceptación #3.
    pagos = [_pago(1, "debito", 10000, 10, 0), _pago(2, "credito", 20000, 11, 0),
              _pago(3, "debito", 5000, 12, 0)]
    movs = [_mov(1, "debito", 10000, 10, 1), _mov(2, "credito", 20000, 11, 1),
             _mov(3, "debito", 5000, 12, 1)]

    r1 = conciliar(pagos, movs)
    r2 = conciliar(list(reversed(pagos)), list(reversed(movs)))

    resumen = lambda r: sorted((tuple(sorted(c.pagos)), tuple(sorted(c.movimientos)), c.nivel, c.score)
                                for c in r.coincidencias)
    assert resumen(r1) == resumen(r2)
    assert len(r1.excepciones) == len(r2.excepciones) == 0


def test_pago_sin_movimiento_y_movimiento_sin_pago():
    pagos = [_pago(1, "debito", 10000, 10, 0), _pago(2, "credito", 7000, 11, 0)]
    movs = [_mov(1, "debito", 10000, 10, 1)]
    r = conciliar(pagos, movs)
    assert len(r.coincidencias) == 1
    tipos = {e.tipo for e in r.excepciones}
    assert tipos == {"pago_sin_movimiento"}
    assert r.excepciones[0].monto_en_juego == 7000


def test_dos_movimientos_del_mismo_monto_y_medio_es_pago_duplicado():
    """Karla, 2026-09-18 (caso 11): una boleta de $50.000 con dos
    transacciones Transbank de $50.000 no debe verse como un sobrante —
    el motor concilia una normal y marca la otra como posible duplicado,
    nunca como "movimiento sin pago" a secas."""
    pagos = [_pago(1, "debito", 50000, 12, 0)]
    movs = [_mov(1, "debito", 50000, 12, 0), _mov(2, "debito", 50000, 12, 1)]
    r = conciliar(pagos, movs)
    assert len(r.coincidencias) == 1
    assert len(r.excepciones) == 1
    excepcion = r.excepciones[0]
    assert excepcion.tipo == "pago_duplicado"
    assert excepcion.monto_en_juego == 50000
    assert "duplicado" in excepcion.decision_propuesta.lower()
    assert "101" in excepcion.decision_propuesta  # n_interno del pago 1 (100 + id)


def test_movimiento_sin_pago_que_no_repite_ningun_monto_conciliado_no_es_duplicado():
    pagos = [_pago(1, "debito", 10000, 10, 0)]
    movs = [_mov(1, "debito", 10000, 10, 0), _mov(2, "credito", 7000, 15, 0)]
    r = conciliar(pagos, movs)
    tipos = {e.tipo for e in r.excepciones}
    assert tipos == {"movimiento_sin_pago"}
