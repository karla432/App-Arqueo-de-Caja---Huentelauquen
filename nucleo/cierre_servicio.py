"""Aprobación, cierre y reapertura del día (diseño funcional §6, §21).

Transición estricta y sin retroceso silencioso: reabrir un día aprobado
invalida su exportación previa y obliga a regenerarla. Una exportación
nunca se edita — se genera una versión nueva.
"""
from django.db import transaction
from django.utils import timezone

from nucleo.arqueo_servicio import preparar_arqueos_del_dia
from nucleo.documentos_servicio import puede_cerrar_con_pendientes, total_documentos_pendientes
from nucleo.models import DiaArqueo, EventoAuditoria, UMBRAL_DIFERENCIA_CAJA


class CierreInvalido(Exception):
    pass


def estado_checklist(dia: DiaArqueo) -> dict:
    """Lista de verificación previa al cierre (Pantalla 6). Cada entrada
    trae si pasa, y el detalle en lenguaje de negocio."""
    excepciones_bloqueantes = dia.excepciones.filter(abierta=True).exclude(tipo="calidad_registro")
    avisos_calidad = dia.excepciones.filter(abierta=True, tipo="calidad_registro").count()

    arqueos = preparar_arqueos_del_dia(dia)
    sin_contar = [a for a in arqueos if a.contado is None]
    sin_autorizar = [
        a for a in arqueos
        if a.contado is not None and abs(a.diferencia) > UMBRAL_DIFERENCIA_CAJA and not a.autorizado_por_id
    ]

    return {
        "sin_excepciones_bloqueantes": {
            "ok": not excepciones_bloqueantes.exists(),
            "detalle": (
                f"{excepciones_bloqueantes.count()} excepciones bloqueantes abiertas"
                if excepciones_bloqueantes.exists()
                else f"Todas resueltas · {avisos_calidad} avisos de calidad no bloquean"
            ),
        },
        "arqueo_completo": {
            "ok": not sin_contar and bool(arqueos),
            "detalle": (
                f"Falta el conteo de {', '.join(a.caja for a in sin_contar)}"
                if sin_contar else f"Arqueo registrado en las {len(arqueos)} cajas"
            ),
        },
        "diferencias_autorizadas": {
            "ok": not sin_autorizar,
            "detalle": (
                f"Falta autorizar {', '.join(a.caja for a in sin_autorizar)}"
                if sin_autorizar else "Sin diferencias pendientes de autorización"
            ),
        },
        "deposito_registrado": {
            "ok": hasattr(dia, "deposito"),
            "detalle": "Registrado" if hasattr(dia, "deposito") else "No bloquea el cierre; queda pendiente",
            "bloqueante": False,
        },
        "documentos_pendientes": {
            "ok": total_documentos_pendientes(dia) == 0,
            "detalle": (
                "Sin documentos pendientes de emitir" if total_documentos_pendientes(dia) == 0
                else f"{total_documentos_pendientes(dia)} guía(s)/otros ingresos sin factura — "
                     "no bloquea por sí solo, pero exige aprobador del nivel correspondiente"
            ),
            "bloqueante": False,  # el bloqueo real depende de QUIÉN aprueba — ver puede_aprobar_usuario
        },
    }


def puede_aprobar(dia: DiaArqueo) -> bool:
    checklist = estado_checklist(dia)
    return all(v["ok"] for k, v in checklist.items() if v.get("bloqueante", True))


def puede_aprobar_usuario(dia: DiaArqueo, usuario) -> tuple[bool, str]:
    """Combina la lista de verificación fija con el nivel de aprobación
    que exigen los documentos pendientes de emitir (guías de despacho,
    otros ingresos) — este último depende de quién está aprobando, no
    solo del estado del día."""
    if not puede_aprobar(dia):
        return False, "Quedan pendientes bloqueantes en la lista de verificación."
    return puede_cerrar_con_pendientes(dia, usuario)


@transaction.atomic
def aprobar_y_cerrar_dia(dia: DiaArqueo, usuario) -> DiaArqueo:
    if dia.estado in (DiaArqueo.Estado.APROBADO, DiaArqueo.Estado.CERRADO, DiaArqueo.Estado.EXPORTADO):
        raise CierreInvalido("Este día ya fue aprobado.")
    puede, razon = puede_aprobar_usuario(dia, usuario)
    if not puede:
        raise CierreInvalido(razon)

    valor_anterior = {"estado": dia.estado}
    dia.estado = DiaArqueo.Estado.CERRADO
    dia.aprobado_por = usuario
    dia.aprobado_en = timezone.now()
    dia.cerrado_en = timezone.now()
    dia.save(update_fields=["estado", "aprobado_por", "aprobado_en", "cerrado_en"])

    evento = EventoAuditoria.objects.create(
        dia=dia, entidad="DiaArqueo", entidad_id=str(dia.pk), accion="aprobar_y_cerrar",
        valor_anterior=valor_anterior,
        valor_posterior={"estado": dia.estado, "aprobado_por": usuario.username},
        usuario=usuario,
    )
    if dia.operado_por_id == usuario.pk:
        # Control C1: se permite, pero queda registrado como excepción de
        # control — no se bloquea por dotación insuficiente.
        EventoAuditoria.objects.create(
            dia=dia, entidad="DiaArqueo", entidad_id=str(dia.pk),
            accion="excepcion_de_control:aprobador_igual_operador",
            valor_anterior=None, valor_posterior={"usuario": usuario.username},
            usuario=usuario,
        )
    return dia


@transaction.atomic
def reabrir_dia(dia: DiaArqueo, usuario, motivo: str) -> DiaArqueo:
    if dia.estado not in (DiaArqueo.Estado.CERRADO, DiaArqueo.Estado.EXPORTADO):
        raise CierreInvalido("Solo se puede reabrir un día cerrado o exportado.")
    if not motivo.strip():
        raise CierreInvalido("La reapertura exige un motivo.")

    valor_anterior = {"estado": dia.estado}
    invalidadas = dia.exportaciones.filter(invalidada=False).update(invalidada=True)
    dia.estado = DiaArqueo.Estado.EXCEPCIONES_RESUELTAS
    dia.aprobado_por = None
    dia.aprobado_en = None
    dia.cerrado_en = None
    dia.save(update_fields=["estado", "aprobado_por", "aprobado_en", "cerrado_en"])

    EventoAuditoria.objects.create(
        dia=dia, entidad="DiaArqueo", entidad_id=str(dia.pk), accion="reabrir",
        valor_anterior=valor_anterior,
        valor_posterior={"estado": dia.estado, "motivo": motivo, "exportaciones_invalidadas": invalidadas},
        usuario=usuario,
    )
    return dia
