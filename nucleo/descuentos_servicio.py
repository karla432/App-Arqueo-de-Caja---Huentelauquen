"""Descuento a trabajadores (Karla, 2026-10-27).

En los días de pago (no siempre el mismo día) los clientes «TRABAJADOR» tienen un % de descuento por producto
(tabla DescuentoTrabajador, en Configuración). Resto informa cada producto a precio de lista y el total de la
comanda ya descontado, así que la comanda «no cuadra» con sus líneas y el día no se podía exportar.

Ahora, para cada comanda de cliente TRABAJADOR cuyo total es menor que la suma de sus líneas:
* Se calcula el descuento esperado con la tabla. Si coincide con el que cobró Resto (dentro de la tolerancia
  «tolerancia_descuento_trabajador»), la comanda queda «cuadra» y se exporta sola, sin bloquear.
* Si no coincide (producto fuera de la tabla, % distinto, cliente mal marcado) queda «no cuadra» y bloquea los
  archivos hasta que Contabilidad (permiso VALIDAR) acepte el descuento con un motivo.
* A Odoo va con una línea «DESCUENTO TRABAJADOR» negativa por el descuento cobrado (precio de lista − total),
  así el pedido suma exactamente lo que cobró Resto.

El descuento nunca cambia el efectivo esperado ni la conciliación: los pagos ya vienen por el total cobrado.
"""
from dataclasses import dataclass, field
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from nucleo.models import DescuentoTrabajador, DiaArqueo, EventoAuditoria, Venta
from nucleo.permisos import Permiso, exigir
from nucleo.umbrales import umbral

CLIENTE_TRABAJADOR = "TRABAJADOR"
PRODUCTO_DESCUENTO = "DESCUENTO TRABAJADOR"


class DescuentoInvalido(Exception):
    pass


def normalizar(producto: str) -> str:
    """Resto escribe a veces dobles espacios («EMPANADA  QUESO  CRUDA»): se comparan sin ellos."""
    return " ".join(str(producto or "").upper().split())


def tabla_vigente() -> dict[str, Decimal]:
    return {normalizar(d.producto): d.porcentaje for d in DescuentoTrabajador.objects.filter(activo=True)}


def es_trabajador(venta: Venta) -> bool:
    return CLIENTE_TRABAJADOR in (venta.cliente or "").upper()


@dataclass
class Analisis:
    venta: Venta
    precio_lista: Decimal
    cobrado: Decimal
    esperado: Decimal
    lineas: list = field(default_factory=list)  # (articulo, total_linea, porcentaje)

    @property
    def descuento(self) -> Decimal:
        """Lo que efectivamente descontó Resto: es lo que va a Odoo."""
        return self.precio_lista - self.cobrado

    @property
    def descuento_tabla(self) -> Decimal:
        return self.precio_lista - self.esperado

    @property
    def diferencia(self) -> Decimal:
        """Cobrado − esperado según la tabla (positivo: Resto descontó menos que la tabla)."""
        return self.cobrado - self.esperado

    @property
    def cuadra(self) -> bool:
        return abs(self.diferencia) <= umbral("tolerancia_descuento_trabajador")

    @property
    def aceptado(self) -> bool:
        return self.venta.descuento_aceptado_por_id is not None

    @property
    def valido(self) -> bool:
        return self.cuadra or self.aceptado

    @property
    def estado(self) -> str:
        return "cuadra" if self.cuadra else ("aceptado" if self.aceptado else "no_cuadra")

    @property
    def sin_tabla(self) -> list[str]:
        return [a for a, _, p in self.lineas if p is None]


def analizar(venta: Venta, tabla: dict | None = None) -> Analisis | None:
    """None si la comanda no es de un trabajador o no tiene descuento (sus líneas ya suman el total)."""
    if not es_trabajador(venta):
        return None
    lineas = list(venta.lineas.all())
    precio_lista = sum((l.total_linea for l in lineas), Decimal(0))
    if precio_lista - venta.total_comanda <= 1:
        return None
    tabla = tabla_vigente() if tabla is None else tabla
    detalle, esperado = [], Decimal(0)
    for l in lineas:
        pct = tabla.get(normalizar(l.articulo))
        detalle.append((l.articulo, l.total_linea, pct))
        esperado += l.total_linea * (1 - (pct or 0) / Decimal(100))
    return Analisis(venta=venta, precio_lista=precio_lista, cobrado=venta.total_comanda,
                    esperado=esperado.quantize(Decimal(1)), lineas=detalle)


def del_dia(dia: DiaArqueo) -> list[Analisis]:
    tabla = tabla_vigente()
    ventas = (Venta.objects.filter(dia=dia, cliente__icontains=CLIENTE_TRABAJADOR).exclude(estado="ANULADA")
              .prefetch_related("lineas").select_related("descuento_aceptado_por").order_by("n_interno"))
    return [a for a in (analizar(v, tabla) for v in ventas) if a is not None]


def descuento_valido(venta: Venta) -> Decimal | None:
    """El monto a exportar como línea de descuento, o None si la comanda no tiene un descuento válido."""
    a = analizar(venta)
    return a.descuento if a is not None and a.valido else None


def descuento_por_linea(ventas) -> dict[int, Decimal]:
    """Para los reportes por producto: cuánto del descuento válido de cada comanda corresponde a cada línea
    (según su % de la tabla, ajustado para sumar exactamente el descuento cobrado). {linea_id: monto}."""
    tabla = tabla_vigente()
    resultado: dict[int, Decimal] = {}
    for venta in ventas.filter(cliente__icontains=CLIENTE_TRABAJADOR).prefetch_related("lineas"):
        a = analizar(venta, tabla)
        if a is None or not a.valido:
            continue
        lineas = list(venta.lineas.all())
        teorico = {l.pk: l.total_linea * (tabla.get(normalizar(l.articulo)) or 0) / Decimal(100) for l in lineas}
        base = sum(teorico.values())
        if not base:  # sin productos en la tabla: se reparte según el valor de cada línea
            teorico = {l.pk: l.total_linea for l in lineas}
            base = sum(teorico.values()) or Decimal(1)
        for pk, t in teorico.items():
            resultado[pk] = a.descuento * t / base
    return resultado


@transaction.atomic
def aceptar(venta: Venta, usuario, motivo: str) -> Venta:
    exigir(usuario, Permiso.VALIDAR, "Solo el Jefe de Contabilidad o la Gerente aceptan un descuento que no calza con la tabla.")
    motivo = " ".join((motivo or "").split())
    if not motivo:
        raise DescuentoInvalido("Aceptar el descuento exige indicar el motivo.")
    if venta.dia.estado == DiaArqueo.Estado.EXPORTADO:
        raise DescuentoInvalido("El día ya está exportado: reábrelo antes de cambiar sus comandas.")
    a = analizar(venta)
    if a is None:
        raise DescuentoInvalido("Esta comanda no tiene un descuento de trabajador.")
    if a.cuadra:
        raise DescuentoInvalido("El descuento ya calza con la tabla: no hay nada que aceptar.")
    venta.descuento_aceptado_por = usuario
    venta.descuento_aceptado_en = timezone.now()
    venta.descuento_aceptado_motivo = motivo[:300]
    venta.save(update_fields=["descuento_aceptado_por", "descuento_aceptado_en", "descuento_aceptado_motivo"])
    EventoAuditoria.objects.create(
        dia=venta.dia, entidad="Venta", entidad_id=str(venta.pk), accion="aceptar_descuento_trabajador",
        valor_anterior=None,
        valor_posterior={"comanda": venta.n_interno, "precio_lista": str(a.precio_lista), "cobrado": str(a.cobrado),
                         "esperado_tabla": str(a.esperado), "motivo": motivo},
        usuario=usuario,
    )
    return venta
