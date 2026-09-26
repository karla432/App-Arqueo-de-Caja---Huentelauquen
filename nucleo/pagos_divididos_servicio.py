"""Pago dividido: una comanda pagada con más de un medio (Karla, 2026-09-23).

Odoo, en una venta externa, admite UN solo "Tipo de pago externo" por venta.
Resto sí declara los medios por separado (ej. efectivo $10.000 + débito
$1.353), y la app los conserva así: el arqueo de efectivo y la conciliación
con Transbank/Mercado Pago no cambian. Al exportar:

* La venta va completa, por el total del documento, bajo UN medio "principal".
* El asiento Arqueo Caja lleva, por cada otra parte, un traspaso entre
  cuentas de balance: Debe la cuenta del medio real / Haber la cuenta del
  medio bajo el que se exportó. Sin IVA, sin pérdida ni ganancia.

Regla del medio principal (validada con Tesorero, Contador Senior y Riesgo,
2026-09-23): EFECTIVO si hay efectivo en el pago; si no, el de mayor monto y,
en empate, Transbank. Razón: la cuenta de Transbank queda con una línea por
el monto exacto que el proveedor va a liquidar (calza 1:1 en Odoo) y el
efectivo se cuadra por el total del día.

Nunca en silencio: si falta la cuenta de un medio (ej. transferencia), la
parte de tarjeta no está calzada con su proveedor, los pagos no suman el
total de la comanda o la comanda ya tiene una corrección de "mezcla de
medios", la exportación se bloquea con el motivo.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from nucleo.models import ConfiguracionArqueoCaja, PagoDeclarado, Reclasificacion, Venta

EFECTIVO = "efectivo"
TRANSBANK = "transbank"
MERCADO_PAGO = "mercado_pago"
TRANSFERENCIA = "transferencia"

ETIQUETA_TIPO_PAGO_ODOO = {
    EFECTIVO: "Efectivo", TRANSBANK: "Tarjeta Transbank", MERCADO_PAGO: "Tarjeta Mercado Pago",
}
_SIGLA = {EFECTIVO: "EF", TRANSBANK: "TBK", MERCADO_PAGO: "MP", TRANSFERENCIA: "TR"}
_ORDEN_DESEMPATE = {TRANSBANK: 0, MERCADO_PAGO: 1, EFECTIVO: 2, TRANSFERENCIA: 3}


@dataclass
class PlanPagoDividido:
    venta: Venta
    principal: str
    partes: dict[str, Decimal]  # medio Odoo -> monto real, incluido el principal
    problemas: list[str] = field(default_factory=list)

    @property
    def traspasos(self) -> list[tuple[str, Decimal]]:
        return [(medio, monto) for medio, monto in self.partes.items() if medio != self.principal]

    @property
    def resumen(self) -> str:
        return " / ".join(f"{_SIGLA[m]} {monto:,.0f}".replace(",", ".") for m, monto in self.partes.items())


def _pesos(monto) -> str:
    return f"${monto:,.0f}".replace(",", ".")


def _clave_odoo(pago: PagoDeclarado) -> str | None:
    """Medio Odoo de un pago: efectivo, el proveedor con que calzó la
    tarjeta, o transferencia. None si es tarjeta sin proveedor conocido."""
    medio = pago.medio_aprobado or pago.medio
    if medio == PagoDeclarado.Medio.EFECTIVO:
        return EFECTIVO
    if medio == PagoDeclarado.Medio.TRANSFERENCIA:
        return TRANSFERENCIA
    coincidencia = pago.coincidencias.first()
    movimiento = coincidencia.movimientos.first() if coincidencia else None
    return movimiento.proveedor if movimiento else None


def cuenta_de_medio_odoo(clave: str, config: ConfiguracionArqueoCaja) -> str | None:
    return {
        EFECTIVO: config.cuenta_efectivo, TRANSBANK: config.cuenta_transbank,
        MERCADO_PAGO: config.cuenta_mercado_pago,
    }.get(clave)


def plan_de_pago_dividido(venta: Venta) -> PlanPagoDividido | None:
    """None si la comanda se exporta con normalidad (todos sus pagos caen
    en el mismo medio Odoo — ej. débito y crédito de Transbank cuentan
    como uno solo)."""
    pagos = list(venta.pagos.all())
    if len(pagos) <= 1:
        return None
    claves = [_clave_odoo(p) for p in pagos]
    conocidas = {c for c in claves if c is not None}
    solo_tarjeta = all((p.medio_aprobado or p.medio) in (PagoDeclarado.Medio.DEBITO, PagoDeclarado.Medio.CREDITO) for p in pagos)
    if None in claves and not conocidas and solo_tarjeta:
        return None  # varios pagos de tarjeta sin calzar: se exporta como siempre (Transbank por defecto)
    if None not in claves and len(conocidas) <= 1:
        return None

    problemas: list[str] = []
    partes: dict[str, Decimal] = {}
    for pago, clave in zip(pagos, claves):
        if clave is None:
            problemas.append(
                f"la parte con tarjeta de {_pesos(pago.monto)} no está calzada con un movimiento de Transbank "
                "o Mercado Pago — corre la conciliación del día"
            )
            continue
        partes[clave] = partes.get(clave, Decimal(0)) + pago.monto

    if len(partes) <= 1 and not problemas:
        return None

    if EFECTIVO in partes:
        principal = EFECTIVO
    elif partes:
        principal = max(partes, key=lambda m: (partes[m], -_ORDEN_DESEMPATE[m]))
    else:
        principal = EFECTIVO

    if TRANSFERENCIA in partes:
        problemas.append(
            "una parte se pagó por transferencia y no hay una cuenta configurada para ella — "
            "decide a mano cómo exportar esta comanda"
        )
    if abs(sum(partes.values(), Decimal(0)) - venta.total_comanda) > 0 and not problemas:
        problemas.append(
            f"los pagos suman {_pesos(sum(partes.values(), Decimal(0)))} y la comanda {_pesos(venta.total_comanda)}"
        )
    if Reclasificacion.objects.filter(pago__venta=venta, regla="mezcla_de_medios").exists():
        problemas.append(
            "ya tiene una corrección de \"mezcla de medios\": no se puede dividir además por pago dividido"
        )
    return PlanPagoDividido(venta=venta, principal=principal, partes=partes, problemas=problemas)


def planes_del_dia(dia) -> list[PlanPagoDividido]:
    ventas = (
        Venta.objects.filter(dia=dia).exclude(estado="ANULADA")
        .prefetch_related("pagos__coincidencias__movimientos")
    )
    return [p for p in (plan_de_pago_dividido(v) for v in ventas) if p is not None]
