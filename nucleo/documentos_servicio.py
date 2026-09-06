"""Documentos tributarios pendientes de emisión al cierre (decisión de
Karla, 2026-09-06) — dos casos que el diseño funcional original no
contemplaba:

1. Ventas con guía de despacho (comprobante que empieza con "GO"): el
   Analista de Ingresos emite la Factura correspondiente recién al
   cierre del arqueo, nunca el día de la guía.
2. Movimientos de Transbank sin ninguna comanda en el POS que en
   realidad son otros ingresos legítimos: se resuelven con el motivo
   "Forma de pago sin boleta — otros ingresos" y también necesitan una
   boleta emitida al cierre.

Ambos casos comparten el mismo control: cerrar el día con documentos
pendientes exige un aprobador, y el nivel exigido escala según cuántos
casos haya ese día:
  - 1 a 10 casos: Jefe de Contabilidad (o superior) alcanza.
  - Más de 10 casos: solo la Gerente de Administración y Finanzas.
"""
from django.db import transaction

from nucleo.models import DiaArqueo, EventoAuditoria, Excepcion, Venta

GRUPO_JEFE_CONTABILIDAD = "Jefe de Contabilidad"
GRUPO_GERENTE_ADM_FINANZAS = "Gerente de Administración y Finanzas"
UMBRAL_ESCALAMIENTO = 10
MOTIVO_OTROS_INGRESOS = "forma_pago_sin_boleta"


class AsignacionInvalida(Exception):
    pass


def guias_pendientes(dia: DiaArqueo):
    return [v for v in Venta.objects.filter(dia=dia) if v.es_guia_de_despacho and not v.factura_asociada]


def otros_ingresos_pendientes(dia: DiaArqueo):
    return Excepcion.objects.filter(
        dia=dia, tipo=Excepcion.Tipo.MOVIMIENTO_SIN_PAGO, resolucion=Excepcion.Resolucion.ACEPTAR,
        motivo__codigo=MOTIVO_OTROS_INGRESOS, factura_asociada="",
    )


def total_documentos_pendientes(dia: DiaArqueo) -> int:
    return len(guias_pendientes(dia)) + otros_ingresos_pendientes(dia).count()


def nivel_requerido(cantidad_pendientes: int) -> int:
    """0: nadie especial necesario (no hay pendientes). 1: Jefe de
    Contabilidad. 2: Gerente de Administración y Finanzas."""
    if cantidad_pendientes == 0:
        return 0
    return 2 if cantidad_pendientes > UMBRAL_ESCALAMIENTO else 1


def nivel_usuario(usuario) -> int:
    if usuario.groups.filter(name=GRUPO_GERENTE_ADM_FINANZAS).exists():
        return 2
    if usuario.groups.filter(name=GRUPO_JEFE_CONTABILIDAD).exists():
        return 1
    return 0


def puede_cerrar_con_pendientes(dia: DiaArqueo, usuario) -> tuple[bool, str]:
    cantidad = total_documentos_pendientes(dia)
    requerido = nivel_requerido(cantidad)
    if requerido == 0:
        return True, "Sin documentos pendientes."
    if nivel_usuario(usuario) >= requerido:
        nombre_nivel = "Gerente de Administración y Finanzas" if requerido == 2 else "Jefe de Contabilidad"
        return True, f"{cantidad} documento(s) pendiente(s) — autorizado por rol {nombre_nivel}."
    nombre_nivel = "la Gerente de Administración y Finanzas" if requerido == 2 else "el Jefe de Contabilidad"
    return False, (
        f"{cantidad} documento(s) pendiente(s) de emitir "
        f"{'(más de ' + str(UMBRAL_ESCALAMIENTO) + ', escala de nivel)' if requerido == 2 else ''} "
        f"— debe aprobar {nombre_nivel}."
    )


@transaction.atomic
def asignar_factura_a_venta(venta: Venta, factura: str, usuario) -> Venta:
    if not venta.es_guia_de_despacho:
        raise AsignacionInvalida("Esta venta no tiene guía de despacho asociada.")
    if not factura.strip():
        raise AsignacionInvalida("El número de factura no puede quedar vacío.")

    anterior = venta.factura_asociada
    venta.factura_asociada = factura.strip()
    venta.save(update_fields=["factura_asociada"])

    EventoAuditoria.objects.create(
        dia=venta.dia, entidad="Venta", entidad_id=str(venta.pk), accion="asignar_factura_guia",
        valor_anterior={"factura_asociada": anterior},
        valor_posterior={"factura_asociada": venta.factura_asociada},
        usuario=usuario,
    )
    return venta


@transaction.atomic
def asignar_factura_a_excepcion(excepcion: Excepcion, factura: str, usuario) -> Excepcion:
    if excepcion.motivo_id is None or excepcion.motivo.codigo != MOTIVO_OTROS_INGRESOS:
        raise AsignacionInvalida("Esta excepción no fue resuelta como otros ingresos.")
    if not factura.strip():
        raise AsignacionInvalida("El número de boleta no puede quedar vacío.")

    anterior = excepcion.factura_asociada
    excepcion.factura_asociada = factura.strip()
    excepcion.save(update_fields=["factura_asociada"])

    EventoAuditoria.objects.create(
        dia=excepcion.dia, entidad="Excepcion", entidad_id=str(excepcion.pk), accion="asignar_boleta_otros_ingresos",
        valor_anterior={"factura_asociada": anterior},
        valor_posterior={"factura_asociada": excepcion.factura_asociada},
        usuario=usuario,
    )
    return excepcion
