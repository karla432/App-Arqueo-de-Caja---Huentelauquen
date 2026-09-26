"""Visado de pérdidas/ganancias de caja (Karla, 2026-09-16).

Cuando un pago con tarjeta que el POS declaró no tiene contraparte real
del proveedor (o se vincula manualmente a un movimiento de otro monto),
la Analista de Ingresos puede resolver la excepción registrando la
diferencia contra una cuenta de pérdida/ganancia (ver
resolucion_servicio). Sobre UMBRAL_VISADO_PERDIDA por caso, además exige
el visto bueno del Jefe de Contabilidad antes de poder cerrar el día —
dos pasos, el mismo patrón que documentos_servicio usa para guías de
despacho y otros ingresos: cualquiera puede clasificar el caso, pero
alguien con el rol correspondiente debe revisarlo antes del cierre.
"""
from django.db import transaction
from django.utils import timezone

from nucleo.permisos import Perfil, Permiso, exigir, perfil_de
from nucleo.documentos_servicio import GRUPO_JEFE_CONTABILIDAD, nivel_usuario
from nucleo.models import ArqueoEfectivo, DiaArqueo, EventoAuditoria, Excepcion
from nucleo.umbrales import umbral

UMBRAL_VISADO_PERDIDA = 50_000  # valor por defecto; el vigente se ajusta en Configuración


class VisadoInvalido(Exception):
    pass


def perdidas_pendientes_de_visado(dia: DiaArqueo):
    return Excepcion.objects.filter(
        dia=dia, resolucion=Excepcion.Resolucion.PERDIDA_GANANCIA,
        monto_perdida_ganancia__gt=umbral("umbral_visado_perdida"), visado_por__isnull=True,
    )


def diferencias_de_caja_pendientes_de_visado(dia: DiaArqueo):
    """Karla, 2026-09-21: sobre el mismo umbral, una diferencia de caja
    también necesita el visto bueno del Jefe de Contabilidad antes de
    exportarse a la cuenta de resultado con la cajera identificada — el
    mismo control que ya existía para pérdidas de tarjeta. Distinto de
    `autorizado_por` (autorización operativa para cerrar el día pese a la
    diferencia, exigida desde un umbral mucho más bajo)."""
    limite = umbral("umbral_visado_perdida")
    return [
        a for a in ArqueoEfectivo.objects.filter(dia=dia, visado_por__isnull=True).exclude(diferencia=None)
        if abs(a.diferencia) > limite
    ]


def puede_cerrar_con_perdidas_pendientes(dia: DiaArqueo, usuario) -> tuple[bool, str]:
    cantidad = perdidas_pendientes_de_visado(dia).count() + len(diferencias_de_caja_pendientes_de_visado(dia))
    limite = umbral("umbral_visado_perdida")
    if cantidad == 0:
        return True, "Sin pérdidas pendientes de visado."
    if nivel_usuario(usuario) >= 1:
        return True, f"{cantidad} pérdida(s) sobre ${limite:,.0f} — autorizado por rol Jefe de Contabilidad."
    return False, (
        f"{cantidad} pérdida(s) de caja o de tarjeta sobre ${limite:,.0f} sin visar — "
        f"debe aprobar el {GRUPO_JEFE_CONTABILIDAD}."
    )


ACCIONES_DE_PREPARACION = ("registrar_conteo", "registrar_gasto_caja", "asignar_cajera")


def _preparo(usuario, entidad: str, entidad_id, *, resolucion: bool) -> bool:
    """¿La persona trabajó este mismo caso (lo contó, lo resolvió)? Nadie visa lo que preparó."""
    eventos = EventoAuditoria.objects.filter(entidad=entidad, entidad_id=str(entidad_id), usuario=usuario)
    if resolucion:
        return eventos.filter(accion__startswith="resolver_excepcion").exists()
    return eventos.filter(accion__in=ACCIONES_DE_PREPARACION).exists()


def _separacion_del_visado(usuario, dia, entidad, entidad_id, motivo_excepcion: str, *, resolucion: bool) -> bool:
    """Segregación de funciones (Karla, 2026-09-26): el Jefe no visa lo que él mismo preparó; la Gerente solo con
    motivo, que queda como excepción de control. Devuelve True si el visado es una excepción de control."""
    if not _preparo(usuario, entidad, entidad_id, resolucion=resolucion):
        return False
    if perfil_de(usuario) != Perfil.GERENTE:
        raise VisadoInvalido("Participaste en la preparación de este caso: no puedes visarlo. Debe visarlo otra persona.")
    if not (motivo_excepcion or "").strip():
        raise VisadoInvalido("Preparaste este caso: visarlo tú misma es una excepción de control y exige indicar el motivo.")
    EventoAuditoria.objects.create(
        dia=dia, entidad=entidad, entidad_id=str(entidad_id), accion="excepcion_de_control:visado_igual_preparador",
        valor_anterior=None, valor_posterior={"usuario": usuario.username, "motivo": motivo_excepcion.strip()}, usuario=usuario,
    )
    return True


@transaction.atomic
def visar_perdida(excepcion: Excepcion, usuario, motivo_excepcion: str = "") -> Excepcion:
    exigir(usuario, Permiso.VISAR, "Tu perfil no puede visar diferencias.")
    if excepcion.resolucion != Excepcion.Resolucion.PERDIDA_GANANCIA:
        raise VisadoInvalido("Esta excepción no fue resuelta como pérdida o ganancia de caja.")
    if excepcion.visado_por_id:
        raise VisadoInvalido("Esta pérdida ya fue visada.")
    if nivel_usuario(usuario) < 1:
        raise VisadoInvalido(f"Solo el {GRUPO_JEFE_CONTABILIDAD} o un rol superior puede visar esta pérdida.")
    _separacion_del_visado(usuario, excepcion.dia, "Excepcion", excepcion.pk, motivo_excepcion, resolucion=True)

    excepcion.visado_por = usuario
    excepcion.visado_en = timezone.now()
    excepcion.save(update_fields=["visado_por", "visado_en"])

    EventoAuditoria.objects.create(
        dia=excepcion.dia, entidad="Excepcion", entidad_id=str(excepcion.pk), accion="visar_perdida",
        valor_anterior={"visado_por": None},
        valor_posterior={"visado_por": usuario.username, "monto": str(excepcion.monto_perdida_ganancia)},
        usuario=usuario,
    )
    return excepcion


@transaction.atomic
def visar_diferencia_caja(arqueo: ArqueoEfectivo, usuario, motivo_excepcion: str = "") -> ArqueoEfectivo:
    exigir(usuario, Permiso.VISAR, "Tu perfil no puede visar diferencias.")
    if arqueo.contado is None:
        raise VisadoInvalido("Esta caja todavía no tiene conteo registrado.")
    if arqueo.diferencia == 0:
        raise VisadoInvalido("Esta caja no tiene diferencia que visar.")
    if arqueo.visado_por_id:
        raise VisadoInvalido("Esta diferencia ya fue visada.")
    if nivel_usuario(usuario) < 1:
        raise VisadoInvalido(f"Solo el {GRUPO_JEFE_CONTABILIDAD} o un rol superior puede visar esta diferencia.")
    _separacion_del_visado(usuario, arqueo.dia, "ArqueoEfectivo", arqueo.pk, motivo_excepcion, resolucion=False)

    arqueo.visado_por = usuario
    arqueo.visado_en = timezone.now()
    arqueo.save(update_fields=["visado_por", "visado_en"])

    EventoAuditoria.objects.create(
        dia=arqueo.dia, entidad="ArqueoEfectivo", entidad_id=str(arqueo.pk), accion="visar_diferencia_caja",
        valor_anterior={"visado_por": None},
        valor_posterior={"visado_por": usuario.username, "caja": arqueo.caja, "diferencia": str(arqueo.diferencia)},
        usuario=usuario,
    )
    return arqueo
