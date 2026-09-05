"""Motor de conciliación multi-señal por niveles (diseño funcional §9-§15).

Reglas de diseño que este archivo protege y no puede violar:

- Agnóstico del proveedor (control obligatorio #11 / criterio de
  aceptación #16): solo conoce `PagoParaConciliar` y
  `MovimientoParaConciliar`, normalizados por los importadores. Ninguna
  línea de este módulo puede mencionar Transbank ni ningún otro proveedor.
- Matching global, no "primer candidato que calza" (§10): dentro de cada
  nivel se arman todos los candidatos posibles antes de decidir nada.
- El resultado no depende del orden de las filas (criterio de aceptación
  #3): todo ordenamiento interno usa claves estables (score, diferencias,
  id), nunca la posición de llegada.
- Ningún emparejamiento automático por debajo de score 85 (criterio de
  aceptación #4): los niveles 1 a 3 siempre puntúan 85 o más; los niveles
  4 a 6 siempre puntúan menos de 85. Es una garantía por construcción, no
  una casualidad de los números (ver `_puntaje`).
- Ante un empate real, ninguno se asigna (§10, quinta regla de
  desempate): se abre una excepción de ambigüedad con los candidatos a la
  vista, salvo el caso de "equivalencia indiferente" (§12).

Nota de alcance (2026-09-05): el POS (VENTAS POR ARTICULO) no trae número
de autorización — esa señal solo existiría si se resuelve la decisión
D1 (usar VENTA CONSOLIDADA para enriquecer). Los niveles 1 y 2 están
implementados y listos para activarse solos el día en que `autorizacion`
venga poblada en `PagoParaConciliar`; hasta entonces nunca encuentran
candidatos y todo se resuelve desde el nivel 3 en adelante — que es
exactamente el comportamiento que el diseño funcional (§11) describe para
"autorización ausente en el POS".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

TOLERANCIA_MONTO = 50  # $ — §9: "$0 exacto / hasta $50 con tolerancia"
VENTANA_NORMAL = timedelta(minutes=10)  # §9/§13
VENTANA_ESTRECHA = timedelta(minutes=3)  # §13: desempate de montos repetidos

NIVEL_BASE = {1: 100, 2: 92, 3: 85, 4: 70, 5: 55, 6: 40}
UMBRAL_AUTOMATICO = 85  # §14: score mínimo para aceptar solo (niveles 1-3)

# Delta de score dentro del cual dos candidatos se consideran "similares"
# para efectos de la penalización -15 de §14 ("existe otro candidato con
# score similar").
DELTA_COMPETENCIA = 10


def _normalizar_autorizacion(valor: str | None) -> str | None:
    """§11: comparar autorizaciones alfanuméricas normalizando — solo
    dígitos, sin ceros a la izquierda. Una autorización alfanumérica
    (letras) nunca puede normalizarse a algo comparable con un campo
    numérico del POS, así que devuelve None (nunca coincide, tal como
    describe el diseño)."""
    if not valor:
        return None
    solo_digitos = re.sub(r"\D", "", valor)
    if not solo_digitos or solo_digitos != valor.strip():
        # Tenía letras u otros caracteres: no es comparable como número.
        if not valor.strip().isdigit():
            return None
    return solo_digitos.lstrip("0") or "0"


@dataclass
class PagoParaConciliar:
    id: int
    n_interno: int
    medio: str  # 'debito' | 'credito'
    monto: float
    momento: datetime
    autorizacion: str | None = None


@dataclass
class MovimientoParaConciliar:
    id: int
    medio: str  # 'debito' | 'credito' | 'prepago'
    monto: float
    momento: datetime
    autorizacion: str | None = None
    terminal: str | None = None


@dataclass
class Candidato:
    pago_id: int
    movimiento_id: int
    nivel: int
    diferencia_monto: float
    diferencia_hora: timedelta
    medio_coincide: bool
    autorizacion_coincide: bool

    def clave_calidad(self):
        """Menor es mejor. Usada para ordenar y para detectar empates
        reales entre candidatos de un mismo pago o movimiento."""
        return (self.diferencia_monto, self.diferencia_hora.total_seconds())


@dataclass
class Coincidencia:
    pagos: list[int]
    movimientos: list[int]
    nivel: int
    score: int
    diferencia_monto: float
    diferencia_hora_segundos: float
    origen: str  # 'automatico' | 'equivalencia_indiferente'
    detalle: str = ""


@dataclass
class Excepcion:
    tipo: str
    pagos: list[int]
    movimientos: list[int]
    monto_en_juego: float
    decision_propuesta: str


@dataclass
class ResultadoConciliacion:
    coincidencias: list[Coincidencia] = field(default_factory=list)
    excepciones: list[Excepcion] = field(default_factory=list)

    @property
    def monto_en_juego_total(self) -> float:
        return sum(e.monto_en_juego for e in self.excepciones)

    @property
    def score_promedio(self) -> float:
        if not self.coincidencias:
            return 0.0
        return sum(c.score for c in self.coincidencias) / len(self.coincidencias)

    @property
    def por_nivel(self) -> dict[int, int]:
        conteo = {n: 0 for n in range(1, 7)}
        for c in self.coincidencias:
            conteo[c.nivel] += len(c.movimientos)
        return conteo


def _puntaje(nivel: int, *, unico: bool, tiene_competencia: bool, terminal_coincide: bool,
             agrupado: bool) -> int:
    """Score = base del nivel + ajustes de §14, con un límite que garantiza
    el criterio de aceptación #4 por construcción: niveles 1-3 nunca bajan
    de 85, niveles 4-6 nunca lo alcanzan."""
    score = NIVEL_BASE[nivel]
    if unico:
        score += 5
    if terminal_coincide:
        score += 10
    if tiene_competencia:
        score -= 15
    if agrupado:
        score -= 10

    if nivel <= 3:
        return max(85, min(100, score))
    return max(0, min(84, score))


def _clasificar(pago: PagoParaConciliar, mov: MovimientoParaConciliar,
                 desfase: timedelta) -> Candidato | None:
    """Evalúa un par (pago, movimiento) y devuelve el nivel que le
    corresponde, o None si no es candidato en ningún nivel 1-5 (la
    agrupación de nivel 6 se resuelve aparte, no par a par)."""
    diferencia_monto = abs(pago.monto - mov.monto)
    monto_exacto = diferencia_monto == 0
    monto_en_tolerancia = 0 < diferencia_monto <= TOLERANCIA_MONTO
    if not (monto_exacto or monto_en_tolerancia):
        return None

    diferencia_hora = abs((mov.momento + desfase) - pago.momento)
    if diferencia_hora > VENTANA_NORMAL:
        return None

    # Supuesto a confirmar con Karla: el POS solo distingue "débito" y
    # "crédito" (no tiene botón de "prepago"). En el día de referencia,
    # los 11 movimientos "prepago" de Transbank se declararon en el POS
    # como "débito" — no como "crédito" — en el 100% de los casos
    # comparables (10 de 11 quedaban como "medio contradictorio" sin este
    # ajuste; era ruido, no un error real del cajero). Se trata "prepago"
    # como compatible con "débito" solo para esta señal de coincidencia;
    # el dato original de Transbank (tipo_tarjeta="prepago") no se altera
    # en ningún lado ni se pierde.
    medio_coincide = pago.medio == mov.medio or (pago.medio == "debito" and mov.medio == "prepago")

    auth_pago = _normalizar_autorizacion(pago.autorizacion)
    auth_mov = _normalizar_autorizacion(mov.autorizacion)
    autorizacion_coincide = auth_pago is not None and auth_pago == auth_mov

    if autorizacion_coincide and monto_exacto:
        nivel = 1
    elif autorizacion_coincide and monto_en_tolerancia:
        nivel = 2
    elif monto_exacto and medio_coincide:
        nivel = 3
    elif monto_exacto and not medio_coincide:
        nivel = 4
    elif monto_en_tolerancia:
        nivel = 5
    else:
        return None

    return Candidato(
        pago_id=pago.id, movimiento_id=mov.id, nivel=nivel,
        diferencia_monto=diferencia_monto, diferencia_hora=diferencia_hora,
        medio_coincide=medio_coincide, autorizacion_coincide=autorizacion_coincide,
    )


def _badness(c: Candidato) -> float:
    """Convierte (diferencia_monto, diferencia_hora) en un solo número
    para el solver: el monto pesa mucho más que la hora, así que nunca se
    prefiere una diferencia de monto menor a costa de una de hora mayor
    fuera de lo que el propio nivel ya permite."""
    return c.diferencia_monto * 100_000 + c.diferencia_hora.total_seconds()


_COSTO_SIN_MATCH = 10_000_000  # preferible a cualquier candidato real, pero...
_COSTO_INVALIDO = 100_000_000  # ...nunca se usa un par que no es candidato.


def _asignacion_optima(del_nivel: list[Candidato]) -> dict[int, int]:
    """Matching global de score (aquí, menor "badness") máximo entre los
    candidatos de un nivel, permitiendo que un pago o un movimiento quede
    sin pareja si ningún candidato real le conviene (§10: "matching
    global por score total máximo, no primer candidato que calza").
    Determinista: mismo conjunto de candidatos → misma asignación, sin
    importar en qué orden llegaron."""
    from scipy.optimize import linear_sum_assignment
    import numpy as np

    pagos_ids = sorted({c.pago_id for c in del_nivel})
    mov_ids = sorted({c.movimiento_id for c in del_nivel})
    n, m = len(pagos_ids), len(mov_ids)
    idx_pago = {pid: i for i, pid in enumerate(pagos_ids)}
    idx_mov = {mid: i for i, mid in enumerate(mov_ids)}

    tam = n + m
    costo = np.full((tam, tam), _COSTO_INVALIDO, dtype=float)
    for c in del_nivel:
        costo[idx_pago[c.pago_id], idx_mov[c.movimiento_id]] = _badness(c)
    for i in range(n):
        costo[i, m + i] = _COSTO_SIN_MATCH  # pago i, "no matchear"
    for j in range(m):
        costo[n + j, j] = _COSTO_SIN_MATCH  # movimiento j, "no matchear"
    costo[n:, m:] = 0  # dummy con dummy: irrelevante

    filas, columnas = linear_sum_assignment(costo)
    return {
        pagos_ids[i]: mov_ids[j]
        for i, j in zip(filas, columnas)
        if i < n and j < m
    }


def _resolver_nivel(candidatos: list[Candidato], nivel: int,
                     pagos_por_id: dict[int, PagoParaConciliar],
                     movimientos_por_id: dict[int, MovimientoParaConciliar],
                     ) -> tuple[list[Coincidencia], list[Excepcion]]:
    """Dos pasadas: (1) encontrar la asignación de menor "badness" total
    posible entre TODOS los candidatos del nivel (matching global,
    determinista); (2) revisar, solo para lo que quedó asignado, si había
    una alternativa libre igual de buena — eso sí es una ambigüedad real
    (§10 quinta regla) o una equivalencia indiferente (§12), no cualquier
    par que alguna vez compitió por algo."""
    del_nivel = [c for c in candidatos if c.nivel == nivel]
    if not del_nivel:
        return [], []

    por_pago: dict[int, list[Candidato]] = {}
    por_mov: dict[int, list[Candidato]] = {}
    por_par: dict[tuple[int, int], Candidato] = {}
    for c in del_nivel:
        por_pago.setdefault(c.pago_id, []).append(c)
        por_mov.setdefault(c.movimiento_id, []).append(c)
        por_par[(c.pago_id, c.movimiento_id)] = c

    asignados = _asignacion_optima(del_nivel)

    # --- Detección de empates reales, contra la asignación óptima ya
    # encontrada: ¿el pago o el movimiento asignado tenían otra opción
    # LIBRE con exactamente la misma calidad?
    grafo: dict[tuple[str, int], set[tuple[str, int]]] = {}

    def nodo_p(i):
        return ("p", i)

    def nodo_m(i):
        return ("m", i)

    for pago_id, mov_id in asignados.items():
        badness_asignado = _badness(por_par[(pago_id, mov_id)])

        competidores_mov = [
            c for c in por_pago[pago_id]
            if c.movimiento_id != mov_id
            and c.movimiento_id not in asignados.values()
            and _badness(c) == badness_asignado
        ]
        competidores_pago = [
            c for c in por_mov[mov_id]
            if c.pago_id != pago_id
            and c.pago_id not in asignados
            and _badness(c) == badness_asignado
        ]
        if competidores_mov or competidores_pago:
            grafo.setdefault(nodo_p(pago_id), set()).add(nodo_m(mov_id))
            grafo.setdefault(nodo_m(mov_id), set()).add(nodo_p(pago_id))
            for c in competidores_mov:
                grafo.setdefault(nodo_p(pago_id), set()).add(nodo_m(c.movimiento_id))
                grafo.setdefault(nodo_m(c.movimiento_id), set()).add(nodo_p(pago_id))
            for c in competidores_pago:
                grafo.setdefault(nodo_m(mov_id), set()).add(nodo_p(c.pago_id))
                grafo.setdefault(nodo_p(c.pago_id), set()).add(nodo_m(mov_id))

    visitados: set[tuple[str, int]] = set()
    excepciones: list[Excepcion] = []
    coincidencias: list[Coincidencia] = []
    ids_resueltos_pago: set[int] = set()
    ids_resueltos_mov: set[int] = set()

    for nodo in sorted(grafo):
        if nodo in visitados:
            continue
        cluster = {nodo}
        pendientes = [nodo]
        while pendientes:
            actual = pendientes.pop()
            for vecino in grafo.get(actual, ()):
                if vecino not in cluster:
                    cluster.add(vecino)
                    pendientes.append(vecino)
        visitados |= cluster

        cluster_pagos = sorted(i for tipo, i in cluster if tipo == "p")
        cluster_movs = sorted(i for tipo, i in cluster if tipo == "m")
        candidatos_cluster = [
            por_par[(p, m)] for p in cluster_pagos for m in cluster_movs if (p, m) in por_par
        ]
        valores = {c.clave_calidad() for c in candidatos_cluster}
        es_indiferente = (
            len(cluster_pagos) == len(cluster_movs)
            and len(valores) == 1
            and len(candidatos_cluster) == len(cluster_pagos) * len(cluster_movs)
        )

        if es_indiferente:
            # Todos contra todos, mismo monto/hora: no importa cuál se
            # empareje con cuál. Se asignan en bloque, en orden estable.
            for pago_id, mov_id in zip(cluster_pagos, cluster_movs):
                c = por_par[(pago_id, mov_id)]
                score = _puntaje(nivel, unico=False, tiene_competencia=False,
                                  terminal_coincide=False, agrupado=False)
                coincidencias.append(Coincidencia(
                    pagos=[pago_id], movimientos=[mov_id], nivel=nivel, score=score,
                    diferencia_monto=c.diferencia_monto,
                    diferencia_hora_segundos=c.diferencia_hora.total_seconds(),
                    origen="equivalencia_indiferente",
                    detalle="Candidatos idénticos en monto, medio y hora — el total no cambia.",
                ))
                ids_resueltos_pago.add(pago_id)
                ids_resueltos_mov.add(mov_id)
        else:
            monto_en_juego = sum(pagos_por_id[p].monto for p in cluster_pagos if p in pagos_por_id)
            excepciones.append(Excepcion(
                tipo="ambiguedad",
                pagos=cluster_pagos,
                movimientos=cluster_movs,
                monto_en_juego=monto_en_juego,
                decision_propuesta=(
                    f"{len(cluster_pagos)} pago(s) y {len(cluster_movs)} movimiento(s) con "
                    "evidencia equivalente — requiere elegir manualmente."
                ),
            ))
            ids_resueltos_pago.update(cluster_pagos)
            ids_resueltos_mov.update(cluster_movs)

    # El resto de la asignación óptima, sin empates, se confirma tal cual.
    for pago_id, mov_id in sorted(asignados.items()):
        if pago_id in ids_resueltos_pago or mov_id in ids_resueltos_mov:
            continue
        c = por_par[(pago_id, mov_id)]
        unico = len(por_pago[pago_id]) == 1 and len(por_mov[mov_id]) == 1
        tiene_competencia = len(por_pago[pago_id]) > 1 or len(por_mov[mov_id]) > 1
        score = _puntaje(nivel, unico=unico, tiene_competencia=tiene_competencia,
                          terminal_coincide=False, agrupado=(nivel == 6))

        if nivel <= 3:
            # Score >= 85 por construcción: se acepta solo, sin excepción
            # (§9: "niveles 1 a 3 se aceptan solos").
            coincidencias.append(Coincidencia(
                pagos=[pago_id], movimientos=[mov_id], nivel=nivel, score=score,
                diferencia_monto=c.diferencia_monto,
                diferencia_hora_segundos=c.diferencia_hora.total_seconds(),
                origen="automatico",
            ))
        else:
            # §9: "del 4 al 6 siempre pasan por decisión humana con motivo
            # obligatorio" — el candidato queda propuesto, no confirmado.
            # Se modela como Excepción (§15), no como Coincidencia: la
            # coincidencia real solo existe una vez que alguien la acepta
            # en la bandeja de excepciones (etapa posterior).
            pago = pagos_por_id[pago_id]
            mov = movimientos_por_id[mov_id]
            if nivel == 4:
                tipo = "medio_contradictorio"
                decision = (
                    f"Monto y hora calzan; el proveedor registra {mov.medio} y el POS "
                    f"declaró {pago.medio}. Reclasificar a {mov.medio}, con confirmación."
                )
            else:  # nivel 5
                tipo = "diferencia_monto"
                decision = (
                    f"Candidato dentro de tolerancia (diferencia ${c.diferencia_monto:,.0f}). "
                    "Aceptar con motivo si es explicable; investigar si no."
                )
            excepciones.append(Excepcion(
                tipo=tipo, pagos=[pago_id], movimientos=[mov_id],
                monto_en_juego=pago.monto, decision_propuesta=decision,
            ))

    return coincidencias, excepciones


def conciliar(pagos: list[PagoParaConciliar], movimientos: list[MovimientoParaConciliar],
              *, desfase_reloj: timedelta = timedelta(0)) -> ResultadoConciliacion:
    """Corre los niveles 1 a 5 en orden sobre lo que cada nivel anterior
    deja libre. El nivel 6 (agrupación) queda fuera del alcance de esta
    primera versión del motor — el día de referencia no lo necesita (los
    casos "movimiento sin pago" de ese día son comandas realmente no
    registradas, no sumas de varios movimientos); se agrega cuando
    aparezca un caso real que lo justifique, en vez de adivinar el
    algoritmo de agrupación sin un ejemplo contra el cual probarlo."""
    pagos_por_id = {p.id: p for p in pagos}
    movimientos_por_id = {m.id: m for m in movimientos}

    pagos_libres = set(pagos_por_id)
    movimientos_libres = set(movimientos_por_id)

    resultado = ResultadoConciliacion()

    for nivel in (1, 2, 3, 4, 5):
        candidatos = []
        for pid in pagos_libres:
            for mid in movimientos_libres:
                c = _clasificar(pagos_por_id[pid], movimientos_por_id[mid], desfase_reloj)
                if c and c.nivel == nivel:
                    candidatos.append(c)

        coincidencias, excepciones = _resolver_nivel(
            candidatos, nivel, pagos_por_id, movimientos_por_id,
        )
        resultado.coincidencias.extend(coincidencias)
        resultado.excepciones.extend(excepciones)

        for c in coincidencias:
            pagos_libres -= set(c.pagos)
            movimientos_libres -= set(c.movimientos)
        for e in excepciones:
            pagos_libres -= set(e.pagos)
            movimientos_libres -= set(e.movimientos)

    for pid in sorted(pagos_libres):
        pago = pagos_por_id[pid]
        resultado.excepciones.append(Excepcion(
            tipo="pago_sin_movimiento", pagos=[pid], movimientos=[],
            monto_en_juego=pago.monto,
            decision_propuesta="Verificar cobro; posible venta no cobrada o medio mal declarado.",
        ))
    for mid in sorted(movimientos_libres):
        mov = movimientos_por_id[mid]
        resultado.excepciones.append(Excepcion(
            tipo="movimiento_sin_pago", pagos=[], movimientos=[mid],
            monto_en_juego=mov.monto,
            decision_propuesta="Buscar comanda no cerrada; posible venta sin registrar.",
        ))

    return resultado
