"""Control de Depósitos (Karla, 2026-09-21): un segundo registro de
depósito, más completo que el de Arqueo de efectivo, que nace cuando la
Analista de Ingresos cierra el día y debe ser cotejado y aprobado por el
Jefe de Contabilidad antes de darse por bueno. Los dos registros de
depósito conviven — este no reemplaza al simple, es el control formal.

Principio (Karla, 2026-09-21): "La aprobación no debe ser solamente
administrativa... el depósito debe considerarse validado únicamente cuando
exista evidencia de que el dinero ingresó efectivamente al banco." Por
eso `validar_deposito` exige la referencia del movimiento bancario para
poder marcar Aprobado. Una diferencia entre lo esperado y lo depositado
nunca se compensa ni se borra sola: queda visible hasta que alguien la
revise (Aprobado u Observado), nunca desaparece de la lista.
"""
from datetime import datetime
from pathlib import Path

import openpyxl
from django.conf import settings
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.utils import timezone

from nucleo.permisos import Permiso, exigir
from nucleo.comprobantes import ComprobanteInvalido, validar_comprobante
from nucleo.documentos_servicio import GRUPO_JEFE_CONTABILIDAD, nivel_usuario
from nucleo.models import (
    ArqueoEfectivo, ConfiguracionArqueoCaja, CuentaContable, DepositoBancario, DiaArqueo, EventoAuditoria,
)
from nucleo.umbrales import umbral

CARPETA_COMPROBANTES = Path(settings.BASE_DIR) / "archivos_cargados" / "comprobantes_deposito"
CARPETA_REPORTES = Path(settings.BASE_DIR) / "exportaciones"


class ControlDepositoInvalido(Exception):
    pass


def _monto_esperado(dia: DiaArqueo):
    return sum(a.contado for a in dia.arqueos_caja.all() if a.contado is not None)


@transaction.atomic
def crear_al_cerrar(dia: DiaArqueo, usuario) -> DepositoBancario:
    """Nace del cierre (Karla, 2026-09-21): se llama desde
    `cierre_servicio.aprobar_y_cerrar_dia`. Si ya existe uno para el día
    (ej. el día se reabrió y se vuelve a cerrar) no se duplica."""
    existente = DepositoBancario.objects.filter(dia=dia).order_by("-registrado_en").first()
    if existente and existente.estado == DepositoBancario.Estado.PENDIENTE_DEPOSITO:
        return existente
    return DepositoBancario.objects.create(
        dia=dia, monto_esperado=_monto_esperado(dia), registrado_por=usuario,
    )


@transaction.atomic
def agregar_deposito_adicional(dia: DiaArqueo, *, monto_esperado, usuario) -> DepositoBancario:
    """Karla, 2026-09-22: "podrían ser más de un depósito por día" — una
    recaudación puede ir al banco en más de una remesa (distinto banco,
    distinto horario, tope de la boca de depósito).

    Karla, 2026-09-24: el adicional REPARTE el total del día, no se suma encima
    (el día 05 quedó con $50.000 de más y una diferencia falsa de −$42.021): su
    monto se le resta al depósito principal — el primero del día que todavía no fue
    revisado por el Jefe de Contabilidad — y la suma de los esperados sigue siendo
    lo contado. Queda en la bitácora."""
    exigir(usuario, Permiso.OPERAR, "Tu perfil no puede agregar depósitos.")
    if monto_esperado is None or monto_esperado <= 0:
        raise ControlDepositoInvalido("Indica el monto que corresponde a este depósito adicional.")
    principal = (
        DepositoBancario.objects.filter(dia=dia)
        .exclude(estado__in=(DepositoBancario.Estado.APROBADO, DepositoBancario.Estado.OBSERVADO))
        .order_by("registrado_en", "pk").first()
    )
    if principal is None:
        raise ControlDepositoInvalido(
            "Todos los depósitos de este día ya fueron revisados: no queda un depósito del que repartir el monto. "
            "Si falta plata por depositar, déjalo como observación en el depósito revisado."
        )
    if monto_esperado >= principal.monto_esperado:
        raise ControlDepositoInvalido(
            f"El adicional (${monto_esperado:,.0f}) debe ser menor a lo que aún se espera del depósito principal "
            f"(${principal.monto_esperado:,.0f}): se reparte el total del día, no se suma."
        )
    anterior = principal.monto_esperado
    principal.monto_esperado = anterior - monto_esperado
    principal.save(update_fields=["monto_esperado"])
    nuevo = DepositoBancario.objects.create(dia=dia, monto_esperado=monto_esperado, registrado_por=usuario)
    EventoAuditoria.objects.create(
        dia=dia, entidad="DepositoBancario", entidad_id=str(nuevo.pk), accion="agregar_deposito_adicional",
        valor_anterior={"principal_id": principal.pk, "monto_esperado_principal": str(anterior)},
        valor_posterior={"monto_esperado_principal": str(principal.monto_esperado), "monto_esperado_adicional": str(monto_esperado)},
        usuario=usuario,
    )
    return nuevo


def _guardar_comprobante(archivo: UploadedFile, deposito: DepositoBancario) -> Path:
    carpeta = CARPETA_COMPROBANTES / deposito.dia.fecha.isoformat()
    carpeta.mkdir(parents=True, exist_ok=True)
    marca = datetime.now().strftime("%Y%m%d%H%M%S")
    ruta = carpeta / f"{marca}_{Path(archivo.name).name}"
    with open(ruta, "wb") as destino:
        for parte in archivo.chunks():
            destino.write(parte)
    return ruta


@transaction.atomic
def registrar_detalle(
    deposito: DepositoBancario, *, fecha_deposito, banco: str, cuenta_bancaria: str,
    monto_depositado, numero_operacion: str, responsable_traslado: str,
    comprobante: UploadedFile | None, usuario,
) -> DepositoBancario:
    """La Analista completa el depósito ya hecho: banco, monto, número de
    operación, quién llevó el efectivo al banco, y el comprobante. Pasa a
    Pendiente de validación — el Jefe de Contabilidad todavía no lo ha
    revisado. El comprobante es obligatorio (Karla, 2026-09-22): sin él no
    hay respaldo documental del depósito."""
    exigir(usuario, Permiso.OPERAR, "Tu perfil no puede registrar depósitos.")
    if deposito.cotejado:
        raise ControlDepositoInvalido("Este depósito ya fue revisado — no se puede editar.")
    if not banco.strip():
        raise ControlDepositoInvalido("Indica el banco de destino.")
    if monto_depositado is None or monto_depositado <= 0:
        raise ControlDepositoInvalido("Indica el monto efectivamente depositado.")
    if not numero_operacion.strip():
        raise ControlDepositoInvalido("Indica el número de operación o comprobante.")
    if not responsable_traslado.strip():
        raise ControlDepositoInvalido("Indica quién retiró y llevó el efectivo al banco.")
    if not comprobante:
        raise ControlDepositoInvalido("Adjunta el comprobante del depósito — es obligatorio.")

    valor_anterior = {"estado": deposito.estado}
    deposito.fecha_deposito = fecha_deposito
    deposito.banco = banco.strip()
    deposito.cuenta_bancaria = cuenta_bancaria.strip()
    deposito.monto_depositado = monto_depositado
    deposito.numero_operacion = numero_operacion.strip()
    deposito.responsable_traslado = responsable_traslado.strip()
    try:
        validar_comprobante(comprobante)
    except ComprobanteInvalido as exc:
        raise ControlDepositoInvalido(str(exc))
    deposito.comprobante_archivo = str(_guardar_comprobante(comprobante, deposito))
    deposito.comprobante_nombre_original = comprobante.name
    deposito.estado = DepositoBancario.Estado.PENDIENTE_VALIDACION
    deposito.save()

    EventoAuditoria.objects.create(
        dia=deposito.dia, entidad="DepositoBancario", entidad_id=str(deposito.pk), accion="registrar_detalle_deposito",
        valor_anterior=valor_anterior,
        valor_posterior={
            "estado": deposito.estado, "monto_depositado": str(monto_depositado),
            "diferencia": str(deposito.diferencia), "numero_operacion": deposito.numero_operacion,
        },
        usuario=usuario,
    )
    return deposito


@transaction.atomic
def validar_deposito(
    deposito: DepositoBancario, *, aprobar: bool, movimiento_bancario_referencia: str, comentarios: str, usuario,
) -> DepositoBancario:
    """El Jefe de Contabilidad coteja contra la cartola y aprueba u
    observa. Exige la referencia del movimiento bancario real — sin eso no
    es una aprobación formal, es un trámite (Karla, 2026-09-21)."""
    exigir(usuario, Permiso.VALIDAR, "Tu perfil no puede validar depósitos.")
    if deposito.estado != DepositoBancario.Estado.PENDIENTE_VALIDACION:
        raise ControlDepositoInvalido("Este depósito no está pendiente de validación.")
    if nivel_usuario(usuario) < 1:
        raise ControlDepositoInvalido(f"Solo el {GRUPO_JEFE_CONTABILIDAD} o un rol superior puede validar un depósito.")
    if aprobar and not movimiento_bancario_referencia.strip():
        raise ControlDepositoInvalido(
            "Para aprobar, indica con qué movimiento de la cartola bancaria se cotejó — "
            "la aprobación exige evidencia de que el dinero entró al banco, no es solo administrativa."
        )
    limite = umbral("umbral_diferencia_deposito")
    if aprobar and deposito.diferencia is not None and abs(deposito.diferencia) > limite:
        raise ControlDepositoInvalido(
            f"La diferencia (${deposito.diferencia:,.0f}) supera el umbral de ${limite:,.0f} — "
            "este depósito solo se puede dejar Observado, no Aprobado."
        )

    valor_anterior = {"estado": deposito.estado}
    deposito.estado = DepositoBancario.Estado.APROBADO if aprobar else DepositoBancario.Estado.OBSERVADO
    deposito.aprobado_por = usuario
    deposito.aprobado_en = timezone.now()
    deposito.movimiento_bancario_referencia = movimiento_bancario_referencia.strip()
    deposito.resultado_cotejo = "coincide" if (deposito.diferencia == 0) else "con_diferencia"
    deposito.comentarios = comentarios.strip()
    deposito.save()

    EventoAuditoria.objects.create(
        dia=deposito.dia, entidad="DepositoBancario", entidad_id=str(deposito.pk), accion="validar_deposito",
        valor_anterior=valor_anterior,
        valor_posterior={
            "estado": deposito.estado, "movimiento_bancario_referencia": deposito.movimiento_bancario_referencia,
            "resultado_cotejo": deposito.resultado_cotejo, "comentarios": deposito.comentarios,
        },
        usuario=usuario,
    )
    return deposito


def depositos_del_dia(dia: DiaArqueo):
    return DepositoBancario.objects.filter(dia=dia).select_related("registrado_por", "aprobado_por")


def resumen_por_estado(depositos) -> dict:
    resumen = {c: 0 for c in DepositoBancario.Estado.values}
    for d in depositos:
        resumen[d.estado] += 1
    resumen["con_diferencia"] = sum(1 for d in depositos if d.diferencia not in (None, 0))
    resumen["sin_cotejar"] = resumen[DepositoBancario.Estado.PENDIENTE_DEPOSITO] + resumen[DepositoBancario.Estado.PENDIENTE_VALIDACION]
    return resumen


# ------------------------------------------------- captura de depósito (Odoo)

COLUMNAS_CAPTURA_DEPOSITO = [
    "Tipo de pago", "Cliente/proveedor", "Importe en moneda de la empresa firmado", "Fecha",
    "Cuenta Contable", "Memo", "Diario",
]


def cuenta_del_banco(deposito: DepositoBancario) -> CuentaContable:
    """La cuenta de Banco (con su Diario de Odoo) del banco
    que se eligió al registrar el depósito. Nunca se adivina ni se usa un valor
    por defecto: si falta algo se dice exactamente qué y dónde corregirlo
    (Karla, 2026-09-24)."""
    nombre = deposito.banco.strip()
    if not nombre:
        raise ControlDepositoInvalido("Este depósito no tiene banco: complétalo antes de descargar la captura.")
    cuenta = CuentaContable.objects.filter(tipo=CuentaContable.Tipo.BANCO, nombre=nombre).first()
    if cuenta is None:
        raise ControlDepositoInvalido(
            f"El banco «{nombre}» no existe entre las cuentas de Banco de Configuración: agrégalo ahí con su Diario."
        )
    if not cuenta.diario_odoo.strip():
        raise ControlDepositoInvalido(
            f"Al banco «{cuenta.nombre}» le falta el Diario de Odoo: complétalo en Configuración → Cuentas contables."
        )
    return cuenta


def _memo_captura(deposito: DepositoBancario) -> str:
    memo = f"Deposito Efectivo Caja del dia {deposito.dia.fecha:%d-%m-%Y}"
    if DepositoBancario.objects.filter(dia=deposito.dia).exclude(estado=DepositoBancario.Estado.PENDIENTE_DEPOSITO).count() > 1:
        memo += f" - Op. {deposito.numero_operacion}"  # varios depósitos el mismo día: se distinguen por su operación
    return memo


def ruta_captura_generada(deposito: DepositoBancario):
    """La captura ya generada, si el archivo sigue en el servidor; nunca genera nada."""
    if deposito.captura_archivo and Path(deposito.captura_archivo).exists():
        return Path(deposito.captura_archivo)
    return None


def generar_lineas_captura_deposito(deposito: DepositoBancario) -> list[dict]:
    if deposito.estado == DepositoBancario.Estado.PENDIENTE_DEPOSITO or deposito.monto_depositado is None:
        raise ControlDepositoInvalido(
            "Primero ingresa los datos del depósito (banco, monto, fecha y comprobante): la captura se genera con ellos."
        )
    cuenta = cuenta_del_banco(deposito)
    config = ConfiguracionArqueoCaja.vigente()
    return [{
        "Tipo de pago": "Recibir", "Cliente/proveedor": config.razon_social,
        "Importe en moneda de la empresa firmado": deposito.monto_depositado, "Fecha": deposito.fecha_deposito,
        "Cuenta Contable": config.cuenta_efectivo, "Memo": _memo_captura(deposito),
        "Diario": cuenta.diario_odoo,
    }]


def generar_captura_deposito(deposito: DepositoBancario, usuario, carpeta: Path | None = None) -> Path:
    """Excel de «Captura de Depósito» para Odoo (Karla, 2026-09-24). Se puede
    descargar apenas se ingresan los datos del depósito, antes de que el Jefe de
    Contabilidad lo valide; cada descarga queda en la bitácora."""
    exigir(usuario, Permiso.OPERAR, "Tu perfil no puede generar la captura de depósito.")
    filas = generar_lineas_captura_deposito(deposito)
    carpeta = carpeta or CARPETA_REPORTES
    carpeta.mkdir(exist_ok=True)
    marca = timezone.now().strftime("%Y%m%d%H%M%S")
    ruta = carpeta / f"captura_deposito_{deposito.dia.fecha.isoformat()}_{deposito.pk}_{marca}.xlsx"

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Captura Deposito"
    ws.append(COLUMNAS_CAPTURA_DEPOSITO)
    for fila in filas:
        ws.append([fila[c] for c in COLUMNAS_CAPTURA_DEPOSITO])
    wb.save(ruta)

    deposito.captura_exportada_en = timezone.now()
    deposito.captura_archivo = str(ruta)
    deposito.save(update_fields=["captura_exportada_en", "captura_archivo"])
    EventoAuditoria.objects.create(
        dia=deposito.dia, entidad="DepositoBancario", entidad_id=str(deposito.pk), accion="exportar_captura_deposito",
        valor_anterior=None,
        valor_posterior={"archivo": ruta.name, "monto": str(deposito.monto_depositado), "estado": deposito.estado},
        usuario=usuario,
    )
    return ruta


# ---------------------------------------------------------- reporte a Gerencia

COLUMNAS_REPORTE_MENSUAL = [
    "Fecha del día", "Estado", "Banco", "Monto esperado", "Monto depositado", "Diferencia", "Con diferencia",
    "N° de operación", "Responsable del traslado", "Registrado por", "Revisado por", "Fecha de revisión",
    "Movimiento bancario cotejado", "Comentarios",
]


def depositos_del_mes(anio: int, mes: int):
    return list(
        DepositoBancario.objects.filter(dia__fecha__year=anio, dia__fecha__month=mes)
        .select_related("dia", "registrado_por", "aprobado_por").order_by("dia__fecha", "registrado_en")
    )


def depositos_con_diferencia_del_mes(anio: int, mes: int):
    """Depósitos del mes con una diferencia distinta de cero, en cualquier estado
    (los aún sin validar también: una diferencia no puede quedar fuera del
    reporte solo porque nadie la revisó todavía)."""
    return [d for d in depositos_del_mes(anio, mes) if d.diferencia not in (None, 0)]


def resumen_del_mes(anio: int, mes: int) -> dict:
    depositos = depositos_del_mes(anio, mes)
    con_dif = [d for d in depositos if d.diferencia not in (None, 0)]
    return {
        "total": len(depositos), "con_diferencia": len(con_dif),
        "sin_validar": sum(1 for d in depositos if not d.cotejado),
    }


def generar_reporte_mensual(anio: int, mes: int, carpeta: Path | None = None) -> Path:
    """Reporte mensual a Gerencia (Karla, 2026-09-22; ampliado 2026-09-24 porque
    salía vacío): TODOS los depósitos del mes, con la columna «Con diferencia»
    y su estado, para que se vea qué se validó y qué falta. Sin depósitos, el
    archivo lo dice."""
    depositos = depositos_del_mes(anio, mes)
    carpeta = carpeta or CARPETA_REPORTES
    carpeta.mkdir(exist_ok=True)
    ruta = carpeta / f"reporte_depositos_{anio:04d}-{mes:02d}.xlsx"

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Depósitos del mes"
    ws.append(COLUMNAS_REPORTE_MENSUAL)
    for d in depositos:
        ws.append([
            d.dia.fecha, d.get_estado_display(), d.banco, d.monto_esperado, d.monto_depositado, d.diferencia,
            "Sí" if d.diferencia not in (None, 0) else "No",
            d.numero_operacion, d.responsable_traslado,
            d.registrado_por.get_full_name() or d.registrado_por.username,
            (d.aprobado_por.get_full_name() or d.aprobado_por.username) if d.aprobado_por else "",
            d.aprobado_en.replace(tzinfo=None) if d.aprobado_en else None, d.movimiento_bancario_referencia, d.comentarios,
        ])
    if not depositos:
        ws.append([f"Sin depósitos registrados en {mes:02d}/{anio}"])
    else:
        resumen = resumen_del_mes(anio, mes)
        ws.append([])
        ws.append([
            "TOTAL", f"{resumen['total']} depósitos", None,
            sum(d.monto_esperado or 0 for d in depositos), sum(d.monto_depositado or 0 for d in depositos),
            sum(d.diferencia or 0 for d in depositos), f"{resumen['con_diferencia']} con diferencia · {resumen['sin_validar']} sin validar",
        ])
    wb.save(ruta)
    return ruta
