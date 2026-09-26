"""Reintegro de Caja (Karla, 2026-09-22): cuando una cajera con una
diferencia negativa (pérdida — de efectivo en el arqueo, o de tarjeta ya
resuelta como pérdida/ganancia) devuelve la plata antes de que se le
descuente de la remuneración, se registra aquí. Total o parcial: el saldo
pendiente se recalcula siempre en caliente, sumando solo los abonos ya
Validados (Tesorero, 2026-09-22: un reintegro Registrado o Pendiente de
validación bancaria NO reduce el saldo — solo uno con evidencia real de
haber llegado al banco).

El asiento contable (Debe Banco / Haber la cuenta de pérdida que ya se usó
para esa diferencia) se genera solo al validar cada reintegro — nunca se
reabre el asiento "Arqueo Caja" del día original (Karla, "no quiero
modificar retroactivamente el resultado histórico del arqueo"): es una
entrada contable nueva e independiente, con su propia fecha."""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import openpyxl
from django.conf import settings
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.utils import timezone

from nucleo.permisos import Permiso, exigir
from nucleo.comprobantes import ComprobanteInvalido, validar_comprobante
from nucleo.documentos_servicio import GRUPO_JEFE_CONTABILIDAD, nivel_usuario
from nucleo.umbrales import umbral
from nucleo.models import (
    ArqueoEfectivo, Cajera, ConfiguracionArqueoCaja, CUENTA_PERDIDA_CODIGO, CuentaContable, DiaArqueo,
    EventoAuditoria, Excepcion, ReintegroCaja,
)

CARPETA_COMPROBANTES = Path(settings.BASE_DIR) / "archivos_cargados" / "comprobantes_reintegro"
CARPETA_EXPORTACIONES = Path(settings.BASE_DIR) / "exportaciones"

# Karla, 2026-09-24: la captura del reintegro usa el mismo formato que la captura de depósito
# (un «Recibir» de la cajera al Diario del banco), no un asiento de dos líneas.
COLUMNAS_ASIENTO = [
    "Tipo de pago", "Cliente/proveedor", "Importe en moneda de la empresa firmado", "Fecha",
    "Cuenta Contable", "Memo", "Diario",
]


class ReintegroInvalido(Exception):
    pass


# --------------------------------------------------------------- orígenes

def _es_perdida_de_arqueo(arqueo: ArqueoEfectivo) -> bool:
    return arqueo.cajera_id is not None and arqueo.diferencia is not None and arqueo.diferencia < 0


def _es_perdida_de_excepcion(excepcion: Excepcion) -> bool:
    return (
        excepcion.resolucion == Excepcion.Resolucion.PERDIDA_GANANCIA
        and excepcion.cajera_responsable_id is not None
        and excepcion.cuenta_contable_id is not None
        and excepcion.cuenta_contable.tipo == CuentaContable.Tipo.PERDIDA
    )


def monto_perdida_original(origen) -> Decimal:
    if isinstance(origen, ArqueoEfectivo):
        return abs(origen.diferencia)
    return origen.monto_perdida_ganancia


def cajera_de(origen) -> Cajera:
    if isinstance(origen, ArqueoEfectivo):
        return origen.cajera
    return origen.cajera_responsable


def dia_de(origen) -> DiaArqueo:
    return origen.dia


def _reintegros_de(origen):
    if isinstance(origen, ArqueoEfectivo):
        return ReintegroCaja.objects.filter(arqueo_origen=origen)
    return ReintegroCaja.objects.filter(excepcion_origen=origen)


def saldo_pendiente(origen) -> Decimal:
    validados = _reintegros_de(origen).filter(estado=ReintegroCaja.Estado.VALIDADO)
    reintegrado = sum((r.monto for r in validados), Decimal(0))
    return monto_perdida_original(origen) - reintegrado


def estado_agregado(origen) -> str:
    saldo = saldo_pendiente(origen)
    if saldo <= 0:
        return "reintegrado"
    if saldo < monto_perdida_original(origen):
        return "parcialmente_reintegrado"
    return "pendiente"


def _es_menor_al_umbral(origen) -> bool:
    """Pérdida chica (Karla, 2026-09-24: hasta $500 no se cobra y ensucia el panel).
    Una que ya tiene abonos registrados deja de ser «menor»: hay que terminarla."""
    if monto_perdida_original(origen) > umbral("umbral_reintegro_minimo"):
        return False
    return not _reintegros_de(origen).exists()


def perdidas_pendientes_de_reintegro(dia: DiaArqueo | None = None, *, incluir_menores: bool = False) -> list:
    """Toda pérdida atribuida a una cajera (de efectivo o de tarjeta) con
    saldo pendiente — la lista que alimenta el selector de "sobre qué
    pérdida se está reintegrando". Las menores al umbral configurado (por
    defecto $500) quedan fuera salvo `incluir_menores`; ver `perdidas_menores_ocultas`."""
    arqueos = ArqueoEfectivo.objects.exclude(diferencia=None).select_related("cajera", "dia")
    excepciones = Excepcion.objects.filter(
        resolucion=Excepcion.Resolucion.PERDIDA_GANANCIA,
    ).select_related("cajera_responsable", "cuenta_contable", "dia")
    if dia is not None:
        arqueos = arqueos.filter(dia=dia)
        excepciones = excepciones.filter(dia=dia)

    origenes = [a for a in arqueos if _es_perdida_de_arqueo(a)] + [e for e in excepciones if _es_perdida_de_excepcion(e)]
    pendientes = [o for o in origenes if saldo_pendiente(o) > 0]
    return pendientes if incluir_menores else [o for o in pendientes if not _es_menor_al_umbral(o)]


def perdidas_menores_ocultas(dia: DiaArqueo | None = None) -> list:
    """Las pérdidas de cajera que el panel oculta por ser menores al umbral."""
    return [o for o in perdidas_pendientes_de_reintegro(dia, incluir_menores=True) if _es_menor_al_umbral(o)]


def origen_desde_tipo_y_id(tipo: str, origen_id: int):
    if tipo == "arqueo":
        try:
            return ArqueoEfectivo.objects.select_related("cajera", "dia").get(pk=origen_id)
        except ArqueoEfectivo.DoesNotExist:
            raise ReintegroInvalido("Ese arqueo no existe.")
    if tipo == "excepcion":
        try:
            return Excepcion.objects.select_related("cajera_responsable", "cuenta_contable", "dia").get(pk=origen_id)
        except Excepcion.DoesNotExist:
            raise ReintegroInvalido("Esa excepción no existe.")
    raise ReintegroInvalido("Origen de la pérdida desconocido.")


# ------------------------------------------------------------- reintegros

@transaction.atomic
def registrar_reintegro(origen, *, cajera: Cajera, monto, usuario) -> ReintegroCaja:
    exigir(usuario, Permiso.OPERAR, "Tu perfil no puede registrar reintegros.")
    origen_cajera = cajera_de(origen)
    if origen_cajera is None or origen_cajera.pk != cajera.pk:
        raise ReintegroInvalido("La cajera no corresponde al origen de esta pérdida.")
    if monto is None or monto <= 0:
        raise ReintegroInvalido("Indica el monto que la cajera está devolviendo.")
    saldo = saldo_pendiente(origen)
    if saldo <= 0:
        raise ReintegroInvalido("Esta pérdida ya está completamente reintegrada.")
    if monto > saldo:
        raise ReintegroInvalido(f"El monto (${monto:,.0f}) supera el saldo pendiente (${saldo:,.0f}).")

    campos_origen = {"arqueo_origen": origen} if isinstance(origen, ArqueoEfectivo) else {"excepcion_origen": origen}
    reintegro = ReintegroCaja.objects.create(
        cajera=cajera, monto=monto, registrado_por=usuario, **campos_origen,
    )
    EventoAuditoria.objects.create(
        dia=dia_de(origen), entidad="ReintegroCaja", entidad_id=str(reintegro.pk), accion="registrar_reintegro",
        valor_anterior=None,
        valor_posterior={"cajera": cajera.nombre, "monto": str(monto), "saldo_pendiente_antes": str(saldo)},
        usuario=usuario,
    )
    return reintegro


def _guardar_comprobante(archivo: UploadedFile, reintegro: ReintegroCaja) -> Path:
    carpeta = CARPETA_COMPROBANTES / reintegro.dia.fecha.isoformat()
    carpeta.mkdir(parents=True, exist_ok=True)
    marca = datetime.now().strftime("%Y%m%d%H%M%S")
    ruta = carpeta / f"{marca}_{Path(archivo.name).name}"
    with open(ruta, "wb") as destino:
        for parte in archivo.chunks():
            destino.write(parte)
    return ruta


@transaction.atomic
def completar_detalle_reintegro(
    reintegro: ReintegroCaja, *, fecha_reintegro: date, banco: str, cuenta_bancaria: str,
    numero_operacion: str, comprobante: UploadedFile | None, observaciones: str, usuario,
) -> ReintegroCaja:
    """Mismo rigor que Control de Depósitos: el comprobante es obligatorio
    para poder pasar a validación (Karla, 2026-09-22: "no quiero que se dé
    por cerrado solo porque alguien dice que la cajera devolvió la plata")."""
    exigir(usuario, Permiso.OPERAR, "Tu perfil no puede completar el detalle del reintegro.")
    if reintegro.estado != ReintegroCaja.Estado.REGISTRADO:
        raise ReintegroInvalido("Este reintegro ya tiene el detalle completado.")
    if not banco.strip():
        raise ReintegroInvalido("Indica el banco al que se depositó.")
    if fecha_reintegro is None:
        raise ReintegroInvalido("Indica la fecha del reintegro.")
    if not numero_operacion.strip():
        raise ReintegroInvalido("Indica el número de operación o comprobante.")
    if not comprobante:
        raise ReintegroInvalido("Adjunta el comprobante del depósito — es obligatorio.")
    try:
        validar_comprobante(comprobante)
    except ComprobanteInvalido as exc:
        raise ReintegroInvalido(str(exc))

    valor_anterior = {"estado": reintegro.estado}
    reintegro.fecha_reintegro = fecha_reintegro
    reintegro.banco = banco.strip()
    reintegro.cuenta_bancaria = cuenta_bancaria.strip()
    reintegro.numero_operacion = numero_operacion.strip()
    reintegro.observaciones = observaciones.strip()
    reintegro.comprobante_archivo = str(_guardar_comprobante(comprobante, reintegro))
    reintegro.comprobante_nombre_original = comprobante.name
    reintegro.estado = ReintegroCaja.Estado.PENDIENTE_VALIDACION
    reintegro.save()

    EventoAuditoria.objects.create(
        dia=reintegro.dia, entidad="ReintegroCaja", entidad_id=str(reintegro.pk),
        accion="completar_detalle_reintegro", valor_anterior=valor_anterior,
        valor_posterior={"estado": reintegro.estado, "numero_operacion": reintegro.numero_operacion},
        usuario=usuario,
    )
    return reintegro


@transaction.atomic
def validar_reintegro(
    reintegro: ReintegroCaja, *, aprobar: bool, movimiento_bancario_referencia: str, comentario: str, usuario,
) -> ReintegroCaja:
    exigir(usuario, Permiso.VALIDAR, "Tu perfil no puede validar reintegros.")
    if reintegro.estado != ReintegroCaja.Estado.PENDIENTE_VALIDACION:
        raise ReintegroInvalido("Este reintegro no está pendiente de validación.")
    if nivel_usuario(usuario) < 1:
        raise ReintegroInvalido(f"Solo el {GRUPO_JEFE_CONTABILIDAD} o un rol superior puede validar un reintegro.")
    if aprobar and not movimiento_bancario_referencia.strip():
        raise ReintegroInvalido(
            "Para aprobar, indica con qué movimiento de la cartola bancaria se cotejó — "
            "la validación exige evidencia real de que el dinero entró al banco."
        )

    if aprobar:
        cuenta_del_banco(reintegro)  # sin banco con Diario no se puede validar (falla antes de guardar nada)

    valor_anterior = {"estado": reintegro.estado}
    reintegro.estado = ReintegroCaja.Estado.VALIDADO if aprobar else ReintegroCaja.Estado.OBSERVADO
    reintegro.validado_por = usuario
    reintegro.validado_en = timezone.now()
    reintegro.movimiento_bancario_referencia = movimiento_bancario_referencia.strip()
    reintegro.comentario_validacion = comentario.strip()
    reintegro.save()

    EventoAuditoria.objects.create(
        dia=reintegro.dia, entidad="ReintegroCaja", entidad_id=str(reintegro.pk), accion="validar_reintegro",
        valor_anterior=valor_anterior,
        valor_posterior={"estado": reintegro.estado, "movimiento_bancario_referencia": reintegro.movimiento_bancario_referencia},
        usuario=usuario,
    )

    if aprobar:
        _generar_asiento_reintegro(reintegro)
    return reintegro


# ---------------------------------------------------- asiento contable (3er asiento)

def ruta_captura_generada(reintegro: ReintegroCaja):
    """La captura ya generada, si el archivo sigue en el servidor; nunca genera nada."""
    if reintegro.archivo_asiento and Path(reintegro.archivo_asiento).exists():
        return Path(reintegro.archivo_asiento)
    return None


def cuenta_del_banco(reintegro: ReintegroCaja) -> CuentaContable:
    """Cuenta de Banco (con su Diario de Odoo) elegida por el usuario en este reintegro.
    Sin banco, o sin Diario, no hay captura — se dice qué falta, no se usa un valor por defecto."""
    nombre = reintegro.banco.strip()
    if not nombre:
        raise ReintegroInvalido("Este reintegro no tiene banco: complétalo antes de generar la captura.")
    cuenta = CuentaContable.objects.filter(tipo=CuentaContable.Tipo.BANCO, nombre=nombre).first()
    if cuenta is None:
        raise ReintegroInvalido(
            f"El banco «{nombre}» no existe entre las cuentas de Banco de Configuración: agrégalo ahí (con su Diario de Odoo) "
            "para poder generar la captura."
        )
    if not cuenta.diario_odoo.strip():
        raise ReintegroInvalido(
            f"Al banco «{cuenta.nombre}» le falta el Diario de Odoo: complétalo en Configuración → Cuentas contables."
        )
    return cuenta


def _cuenta_texto(codigo: str) -> str:
    cuenta = CuentaContable.objects.get(codigo=codigo)
    return f"{cuenta.codigo} {cuenta.nombre}"


def generar_lineas_reintegro(reintegro: ReintegroCaja) -> list[dict]:
    """Una sola línea «Recibir» (Karla, 2026-09-24): cliente = la cajera, importe = lo reintegrado,
    fecha = el día del arqueo de la pérdida, cuenta = pérdida por diferencia de efectivo, memo =
    reversa de la pérdida y Diario = el del banco elegido. Nunca reabre el asiento del día original."""
    cuenta = cuenta_del_banco(reintegro)
    dia_origen = reintegro.dia
    memo = f"Reintegro (reversa de pérdida de caja) – {reintegro.cajera.nombre} – Arqueo {dia_origen.fecha.strftime('%d/%m/%Y')}"
    return [{
        "Tipo de pago": "Recibir", "Cliente/proveedor": reintegro.cajera.nombre,
        "Importe en moneda de la empresa firmado": reintegro.monto, "Fecha": dia_origen.fecha,
        "Cuenta Contable": _cuenta_texto(CUENTA_PERDIDA_CODIGO), "Memo": memo, "Diario": cuenta.diario_odoo,
    }]


def numero_de_caja(reintegro: ReintegroCaja) -> str:
    """N° de la caja donde se produjo la pérdida: la del arqueo, o la de la venta de la
    excepción de tarjeta. Vacío si no se puede saber."""
    caja = ""
    if reintegro.arqueo_origen_id:
        caja = reintegro.arqueo_origen.caja
    elif reintegro.excepcion_origen_id and reintegro.excepcion_origen.pago_id:
        caja = reintegro.excepcion_origen.pago.venta.caja
    encontrado = re.search(r"\d+", caja or "")
    return str(int(encontrado.group())) if encontrado else ""


def nombre_captura_reintegro(reintegro: ReintegroCaja) -> str:
    """«Captura Reintegro Caja N-ddmmaa» (Karla, 2026-09-24): N° de la caja y fecha del
    arqueo, ej. «Captura Reintegro Caja 4-070926.xlsx». Sin caja conocida se omite el N°."""
    n = numero_de_caja(reintegro)
    return f"Captura Reintegro Caja {n + '-' if n else ''}{reintegro.dia.fecha:%d%m%y}.xlsx"


def _generar_asiento_reintegro(reintegro: ReintegroCaja) -> Path:
    CARPETA_EXPORTACIONES.mkdir(exist_ok=True)
    marca = timezone.now().strftime("%Y%m%d%H%M%S")
    ruta = CARPETA_EXPORTACIONES / f"captura_reintegro_caja_{reintegro.pk}_{marca}.xlsx"
    filas = generar_lineas_reintegro(reintegro)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Captura Reintegro Caja"
    ws.append(COLUMNAS_ASIENTO)
    for fila in filas:
        ws.append([fila[c] for c in COLUMNAS_ASIENTO])
    wb.save(ruta)

    reintegro.exportado_en = timezone.now()
    reintegro.archivo_asiento = str(ruta)
    reintegro.save(update_fields=["exportado_en", "archivo_asiento"])
    return ruta


def regenerar_asiento_reintegro(reintegro: ReintegroCaja, usuario) -> Path:
    """Genera la captura del reintegro con los datos de hoy. Está disponible desde que se
    ingresa el detalle, antes de la validación del Jefe de Contabilidad (Karla, 2026-09-24),
    igual que la captura de depósito. Cada generación queda en la bitácora."""
    exigir(usuario, Permiso.OPERAR, "Tu perfil no puede generar la captura de reintegro.")
    if reintegro.estado == ReintegroCaja.Estado.REGISTRADO:
        raise ReintegroInvalido(
            "Primero completa el detalle del reintegro (banco, fecha, N° de operación y comprobante): la captura se genera con ellos."
        )
    anterior = {"archivo_asiento": Path(reintegro.archivo_asiento).name if reintegro.archivo_asiento else ""}
    ruta = _generar_asiento_reintegro(reintegro)
    EventoAuditoria.objects.create(
        dia=reintegro.dia, entidad="ReintegroCaja", entidad_id=str(reintegro.pk), accion="regenerar_asiento_reintegro",
        valor_anterior=anterior, valor_posterior={"archivo_asiento": ruta.name, "banco": reintegro.banco}, usuario=usuario,
    )
    return ruta
