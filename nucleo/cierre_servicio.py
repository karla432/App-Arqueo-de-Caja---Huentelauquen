"""Aprobación, cierre y reapertura del día (diseño funcional §6, §21).

Transición estricta y sin retroceso silencioso: reabrir un día aprobado
invalida su exportación previa y obliga a regenerarla. Una exportación
nunca se edita — se genera una versión nueva.
"""
from django.db import transaction
from django.utils import timezone

from nucleo.arqueo_servicio import preparar_arqueos_del_dia
from nucleo.documentos_servicio import puede_cerrar_con_pendientes, total_documentos_pendientes
from nucleo.templatetags.formato import folio_corto
from nucleo.models import DiaArqueo, EventoAuditoria, PagoDeclarado
from nucleo.umbrales import umbral
from nucleo.perdidas_servicio import (
    diferencias_de_caja_pendientes_de_visado, perdidas_pendientes_de_visado, puede_cerrar_con_perdidas_pendientes,
)
from nucleo.permisos import Perfil, Permiso, exigir, perfil_de, tiene_permiso


class CierreInvalido(Exception):
    pass


def _deposito_registrado(dia: DiaArqueo) -> bool:
    """Karla, 2026-09-24: el depósito se lleva solo en Control de Depósitos."""
    from nucleo.models import DepositoBancario
    return DepositoBancario.objects.filter(dia=dia).exclude(estado=DepositoBancario.Estado.PENDIENTE_DEPOSITO).exists()


def estado_checklist(dia: DiaArqueo) -> dict:
    """Lista de verificación previa al cierre (Pantalla 6). Cada entrada
    trae si pasa, y el detalle en lenguaje de negocio."""
    excepciones_bloqueantes = dia.excepciones.filter(abierta=True).exclude(tipo="calidad_registro")
    avisos_calidad = dia.excepciones.filter(abierta=True, tipo="calidad_registro").count()

    arqueos = preparar_arqueos_del_dia(dia)
    sin_contar = [a for a in arqueos if a.contado is None]
    sin_autorizar = [
        a for a in arqueos
        if a.contado is not None and abs(a.diferencia) > umbral("umbral_diferencia_caja") and not a.autorizado_por_id
    ]

    # Sin correr la conciliación no puede haber excepciones: "cero abiertas"
    # sería un falso verde (caso real de Karla, 2026-09-23: día cerrado sin
    # conciliar, con un pago de tarjeta que nunca se cruzó con Transbank).
    hay_que_conciliar = (
        dia.movimientos.exists()
        or PagoDeclarado.objects.filter(venta__dia=dia, medio__in=["debito", "credito"]).exists()
    )
    conciliacion_ok = dia.corridas_motor.exists() or not hay_que_conciliar

    from nucleo.regularizacion_servicio import documentos_sin_clasificar
    sin_clasificar = list(documentos_sin_clasificar(dia))

    from nucleo.notas_credito_servicio import notas_sin_evidencia
    notas_pendientes = list(notas_sin_evidencia(dia))

    return {
        "notas_credito_verificadas": {
            "ok": not notas_pendientes,
            "detalle": (
                "Sin notas de crédito pendientes de evidencia" if not notas_pendientes
                else f"{len(notas_pendientes)} nota(s) de crédito sin evidencia "
                     f"({', '.join(folio_corto(n.folio) for n in notas_pendientes)}) — carga el vale firmado y la boleta original "
                     "(la verificación se exige en el cierre de mes)"
            ),
        },
        "documentos_sin_clasificar": {
            "ok": not sin_clasificar,
            "detalle": (
                "Todos los documentos del libro están clasificados" if not sin_clasificar
                else f"{len(sin_clasificar)} documento(s) con código no configurado "
                     f"({', '.join(sorted({d.codigo_original for d in sin_clasificar}))}) — "
                     "clasifícalos en Regularización de Documentos"
            ),
        },
        "conciliacion_ejecutada": {
            "ok": conciliacion_ok,
            "detalle": (
                "Conciliación ejecutada" if dia.corridas_motor.exists()
                else "Nada que conciliar" if conciliacion_ok
                else "Falta ejecutar la conciliación del día (pantalla Excepciones → Conciliar)"
            ),
        },
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
            "ok": _deposito_registrado(dia),
            "detalle": "Registrado en Control de Depósitos" if _deposito_registrado(dia)
                       else "No bloquea el cierre: se registra después, en Control de Depósitos",
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
        "perdidas_pendientes_de_visado": {
            "ok": not perdidas_pendientes_de_visado(dia).exists() and not diferencias_de_caja_pendientes_de_visado(dia),
            "detalle": (
                "Sin diferencias pendientes de visar"
                if not perdidas_pendientes_de_visado(dia).exists() and not diferencias_de_caja_pendientes_de_visado(dia)
                else f"{perdidas_pendientes_de_visado(dia).count() + len(diferencias_de_caja_pendientes_de_visado(dia))} "
                     "diferencia(s) de caja o de tarjeta sobre el umbral sin visar — no bloquea por sí solo, "
                     "pero exige aprobador del nivel correspondiente"
            ),
            "bloqueante": False,  # el bloqueo real depende de QUIÉN aprueba — ver puede_aprobar_usuario
        },
    }


def puede_aprobar(dia: DiaArqueo) -> bool:
    checklist = estado_checklist(dia)
    return all(v["ok"] for k, v in checklist.items() if v.get("bloqueante", True))


ACCIONES_DE_PREPARACION = (
    "registrar_conteo", "registrar_gasto_caja", "asignar_cajera", "asignar_factura_guia", "asignar_boleta_otros_ingresos",
    "ejecutar_conciliacion", "carga_archivo",
)


def preparadores_del_dia(dia: DiaArqueo) -> set[int]:
    """Quiénes prepararon la operación de un día: quien la operó, quien cargó los archivos y quien contó, asignó
    cajeras, resolvió excepciones o asignó documentos. Nadie aprueba su propio trabajo sin dejar una excepción."""
    ids = {dia.operado_por_id} | set(dia.lotes.values_list("cargado_por_id", flat=True))
    eventos = EventoAuditoria.objects.filter(dia=dia)
    ids |= set(eventos.filter(accion__in=ACCIONES_DE_PREPARACION).values_list("usuario_id", flat=True))
    ids |= set(eventos.filter(accion__startswith="resolver_excepcion").values_list("usuario_id", flat=True))
    return {i for i in ids if i}


def preparo_el_dia(dia: DiaArqueo, usuario) -> bool:
    return usuario.pk in preparadores_del_dia(dia)


def requiere_motivo_de_excepcion(dia: DiaArqueo, usuario) -> bool:
    """La Gerente puede aprobar un día que ella misma preparó, pero solo como excepción con motivo."""
    return perfil_de(usuario) == Perfil.GERENTE and preparo_el_dia(dia, usuario)


def puede_aprobar_usuario(dia: DiaArqueo, usuario) -> tuple[bool, str]:
    """Combina el permiso del perfil, la lista de verificación fija, la segregación de funciones (nadie aprueba lo
    que preparó; la Gerente solo con motivo) y el nivel que exigen los documentos y las pérdidas pendientes."""
    if not tiene_permiso(usuario, Permiso.APROBAR_CIERRE):
        return False, "Tu perfil no puede aprobar cierres."
    if not puede_aprobar(dia):
        return False, "Quedan pendientes bloqueantes en la lista de verificación."
    if perfil_de(usuario) == Perfil.JEFE and preparo_el_dia(dia, usuario):
        return False, "Participaste en la preparación de este día: no puedes aprobarlo. Debe aprobarlo la Gerente."
    puede, razon = puede_cerrar_con_pendientes(dia, usuario)
    if not puede:
        return False, razon
    return puede_cerrar_con_perdidas_pendientes(dia, usuario)


@transaction.atomic
def aprobar_y_cerrar_dia(dia: DiaArqueo, usuario, motivo_excepcion: str = "") -> DiaArqueo:
    exigir(usuario, Permiso.APROBAR_CIERRE, "Tu perfil no puede aprobar cierres.")
    if dia.estado in (DiaArqueo.Estado.APROBADO, DiaArqueo.Estado.CERRADO, DiaArqueo.Estado.EXPORTADO):
        raise CierreInvalido("Este día ya fue aprobado.")
    puede, razon = puede_aprobar_usuario(dia, usuario)
    if not puede:
        raise CierreInvalido(razon)
    autoaprobacion = preparo_el_dia(dia, usuario)
    if autoaprobacion and not motivo_excepcion.strip():
        raise CierreInvalido(
            "Preparaste este día: aprobarlo tú misma es una excepción de control y exige indicar el motivo."
        )

    valor_anterior = {"estado": dia.estado}
    dia.estado = DiaArqueo.Estado.CERRADO
    dia.aprobado_por = usuario
    dia.aprobado_en = timezone.now()
    dia.cerrado_en = timezone.now()
    dia.save(update_fields=["estado", "aprobado_por", "aprobado_en", "cerrado_en"])

    EventoAuditoria.objects.create(
        dia=dia, entidad="DiaArqueo", entidad_id=str(dia.pk), accion="aprobar_y_cerrar",
        valor_anterior=valor_anterior,
        valor_posterior={"estado": dia.estado, "aprobado_por": usuario.username, "perfil": perfil_de(usuario).value},
        usuario=usuario,
    )
    if autoaprobacion:
        # Control C1: solo la Gerente y solo con motivo; queda como excepción de control en la bitácora.
        EventoAuditoria.objects.create(
            dia=dia, entidad="DiaArqueo", entidad_id=str(dia.pk),
            accion="excepcion_de_control:aprobador_igual_operador",
            valor_anterior=None, valor_posterior={"usuario": usuario.username, "motivo": motivo_excepcion.strip()},
            usuario=usuario,
        )

    # Control de Depósitos (Karla, 2026-09-21): nace del cierre, para que
    # la Analista lo complete y el Jefe de Contabilidad lo valide después.
    from nucleo.depositos_servicio import crear_al_cerrar
    crear_al_cerrar(dia, usuario)

    return dia


@transaction.atomic
def reabrir_dia(dia: DiaArqueo, usuario, motivo: str) -> DiaArqueo:
    exigir(usuario, Permiso.REABRIR_DIA, "Solo la Gerente de Administración y Finanzas puede reabrir un día.")
    if dia.estado not in (DiaArqueo.Estado.CERRADO, DiaArqueo.Estado.EXPORTADO):
        raise CierreInvalido("Solo se puede reabrir un día cerrado o exportado.")
    if not motivo.strip():
        raise CierreInvalido("La reapertura exige un motivo.")
    from nucleo.cierre_mes_servicio import dia_en_mes_cerrado
    if dia_en_mes_cerrado(dia):
        raise CierreInvalido("El mes de este día está cerrado: primero hay que reabrir el mes (Cierre de Mes).")

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
