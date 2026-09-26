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
pendientes exige un aprobador: el Jefe de Contabilidad (o la Gerente) alcanza,
sea cual sea la cantidad de casos (Karla, 2026-09-26; antes, con más de 10 casos
solo podía la Gerente).
"""
from django.db import transaction

from nucleo.models import DiaArqueo, EventoAuditoria, Excepcion, Venta

from nucleo.permisos import (  # noqa: E402  (los perfiles viven en nucleo.permisos)
    GRUPO_ADMIN_TECNICO, GRUPO_ENCARGADO as GRUPO_ENCARGADA_INGRESOS, GRUPO_GERENTE as GRUPO_GERENTE_ADM_FINANZAS,
    GRUPO_JEFE as GRUPO_JEFE_CONTABILIDAD, GRUPO_SOLO_LECTURA, GRUPO_SOLO_VISUALIZACION, Permiso, exigir, nivel_de, tiene_permiso,
)
# Perfiles de entrada a la app (Karla, 2026-09-22): antes solo existían
# estos dos grupos, para aprobar cosas puntuales — cualquier otro usuario
# autenticado podía operar todo. Ahora hay 4 perfiles reales; ver
# nucleo.configuracion_servicio.ROLES y nucleo.middleware.
UMBRAL_ESCALAMIENTO = 10  # valor por defecto; el vigente se ajusta en Configuración
MOTIVO_OTROS_INGRESOS = "forma_pago_sin_boleta"


def puede_operar(usuario) -> bool:
    """¿Puede la persona operar (cargar, contar, registrar)? Solo el Encargado de Ingresos y la Gerente
    (Karla, 2026-09-26: antes bastaba con no ser «Solo Visualización»). Lo usan las plantillas para ocultar
    botones; el control real está en las vistas y en los servicios (`nucleo.permisos`)."""
    return tiene_permiso(usuario, Permiso.OPERAR)


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
    """0: nadie especial necesario (no hay pendientes). 1: Jefe de Contabilidad (o la Gerente), sin importar cuántos
    sean (Karla, 2026-09-26: el Jefe cierra con cualquier cantidad de pendientes; ya no escala a la Gerente)."""
    return 0 if cantidad_pendientes == 0 else 1


def nivel_usuario(usuario) -> int:
    """Nivel de aprobación por PERFIL (2 Gerente, 1 Jefe, 0 el resto). Ser superusuario no cuenta."""
    return nivel_de(usuario)


def puede_cerrar_con_pendientes(dia: DiaArqueo, usuario) -> tuple[bool, str]:
    cantidad = total_documentos_pendientes(dia)
    requerido = nivel_requerido(cantidad)
    if requerido == 0:
        return True, "Sin documentos pendientes."
    if nivel_usuario(usuario) >= requerido:
        return True, f"{cantidad} documento(s) pendiente(s) — autorizado por su perfil."
    return False, f"{cantidad} documento(s) pendiente(s) de emitir — debe aprobar el Jefe de Contabilidad o la Gerente."


@transaction.atomic
def asignar_factura_a_venta(venta: Venta, factura: str, usuario) -> Venta:
    exigir(usuario, Permiso.OPERAR, "Tu perfil no puede asignar documentos.")
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
    exigir(usuario, Permiso.OPERAR, "Tu perfil no puede asignar documentos.")
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
