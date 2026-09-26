"""Control de Depósitos (Karla, 2026-09-21): pantalla propia, no ligada a
un día puntual — cruza todos los días para que se puedan ver de un vistazo
los depósitos pendientes, aprobados, observados y con diferencias."""
from datetime import date
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from nucleo.descargas import archivo_no_disponible, entregar_archivo
from nucleo.permisos import Permiso, es_de_lectura, requiere, tiene_permiso
from nucleo.configuracion_servicio import cuentas_banco, responsables_traslado_activos
from nucleo.depositos_servicio import (
    ControlDepositoInvalido, agregar_deposito_adicional, cuenta_del_banco, generar_captura_deposito, ruta_captura_generada,
    generar_reporte_mensual, registrar_detalle, resumen_del_mes, resumen_por_estado, validar_deposito,
)
from nucleo.documentos_servicio import nivel_usuario
from nucleo.models import DepositoBancario, DiaArqueo


def _anio_y_mes(anio, mes, hoy: date) -> tuple[int, int]:
    """Año y mes del reporte; un valor mal escrito cae al mes actual en vez de romper."""
    try:
        a, m = int(anio or hoy.year), int(mes or hoy.month)
    except (TypeError, ValueError):
        return hoy.year, hoy.month
    return (a, m) if 2000 <= a <= 2100 and 1 <= m <= 12 else (hoy.year, hoy.month)


@requiere(Permiso.CONSULTAR)
def control_depositos(request):
    depositos = DepositoBancario.objects.select_related(
        "dia", "registrado_por", "aprobado_por",
    ).order_by("-dia__fecha", "-registrado_en")

    filtro = request.GET.get("estado", "")
    if filtro == "con_diferencia":
        depositos_filtrados = [d for d in depositos if d.diferencia not in (None, 0)]
    elif filtro == "sin_cotejar":
        depositos_filtrados = [d for d in depositos if not d.cotejado]
    elif filtro:
        depositos_filtrados = [d for d in depositos if d.estado == filtro]
    else:
        depositos_filtrados = list(depositos)

    deposito_id_ver = request.GET.get("deposito")
    deposito_actual = next((d for d in depositos_filtrados if str(d.pk) == deposito_id_ver), None) \
        or (depositos_filtrados[0] if depositos_filtrados else None)

    hoy = date.today()
    anio_reporte, mes_reporte = _anio_y_mes(request.GET.get("anio_reporte"), request.GET.get("mes_reporte"), hoy)
    captura_faltante = ""
    if deposito_actual and deposito_actual.banco:
        try:
            cuenta_del_banco(deposito_actual)
        except ControlDepositoInvalido as exc:
            captura_faltante = str(exc)

    return render(request, "nucleo/control_depositos.html", {
        "depositos": depositos_filtrados,
        "deposito_actual": deposito_actual,
        "resumen": resumen_por_estado(depositos),
        "filtro": filtro,
        "puede_validar": tiene_permiso(request.user, Permiso.VALIDAR), "puede_operar": tiene_permiso(request.user, Permiso.OPERAR),
        "anio_reporte": anio_reporte, "mes_reporte": mes_reporte,
        "resumen_mes": resumen_del_mes(anio_reporte, mes_reporte),
        "captura_faltante": captura_faltante,
        "bancos": cuentas_banco(),
        "responsables_traslado": responsables_traslado_activos(),
    })


@requiere(Permiso.OPERAR)
@require_POST
def registrar_detalle_deposito_view(request, deposito_id):
    deposito = get_object_or_404(DepositoBancario, pk=deposito_id)
    try:
        texto_monto = request.POST.get("monto_depositado", "").replace("$", "").replace(".", "").replace(" ", "").strip()
        try:
            monto = Decimal(texto_monto) if texto_monto else None
        except InvalidOperation:
            raise ControlDepositoInvalido("El monto depositado no es un número válido.")
        texto_fecha = request.POST.get("fecha_deposito", "").strip()
        fecha_deposito = date.fromisoformat(texto_fecha) if texto_fecha else None
        registrar_detalle(
            deposito,
            fecha_deposito=fecha_deposito,
            banco=request.POST.get("banco", ""), cuenta_bancaria=request.POST.get("cuenta_bancaria", ""),
            monto_depositado=monto,
            numero_operacion=request.POST.get("numero_operacion", ""),
            responsable_traslado=request.POST.get("responsable_traslado", ""),
            comprobante=request.FILES.get("comprobante"), usuario=request.user,
        )
        messages.success(request, "Depósito registrado — queda pendiente de validación.")
    except ControlDepositoInvalido as exc:
        messages.error(request, str(exc))
    return redirect(f"/depositos/?deposito={deposito.pk}")


@requiere(Permiso.VALIDAR)
@require_POST
def validar_deposito_view(request, deposito_id):
    deposito = get_object_or_404(DepositoBancario, pk=deposito_id)
    try:
        validar_deposito(
            deposito, aprobar=request.POST.get("accion") == "aprobar",
            movimiento_bancario_referencia=request.POST.get("movimiento_bancario_referencia", ""),
            comentarios=request.POST.get("comentarios", ""), usuario=request.user,
        )
        messages.success(request, "Depósito revisado.")
    except ControlDepositoInvalido as exc:
        messages.error(request, str(exc))
    return redirect(f"/depositos/?deposito={deposito.pk}")


@requiere(Permiso.OPERAR)
@require_POST
def agregar_deposito_adicional_view(request, dia_id):
    dia = get_object_or_404(DiaArqueo, pk=dia_id)
    try:
        texto_monto = request.POST.get("monto_esperado", "").replace("$", "").replace(".", "").replace(" ", "").strip()
        try:
            monto = Decimal(texto_monto) if texto_monto else None
        except InvalidOperation:
            raise ControlDepositoInvalido("El monto no es un número válido.")
        nuevo = agregar_deposito_adicional(dia, monto_esperado=monto, usuario=request.user)
        messages.success(request, f"Depósito adicional agregado para el {dia.fecha:%d-%m-%Y}.")
        return redirect(f"/depositos/?deposito={nuevo.pk}")
    except ControlDepositoInvalido as exc:
        messages.error(request, str(exc))
    return redirect("/depositos/")


@requiere(Permiso.DESCARGAR_GENERADOS)
def descargar_reporte_mensual_view(request):
    anio, mes = _anio_y_mes(request.GET.get("anio"), request.GET.get("mes"), date.today())
    ruta = generar_reporte_mensual(anio, mes)
    return entregar_archivo(request, ruta, ruta.name, entidad="Reporte", entidad_id="depositos_mensual", tipo="reporte_mensual_depositos",
                            detalle={"anio": anio, "mes": mes})


@requiere(Permiso.DESCARGAR_GENERADOS)
def descargar_captura_deposito_view(request, deposito_id):
    """Entrega la captura YA generada. Descargar no genera ni modifica nada: eso es la acción `generar_captura_deposito`."""
    deposito = get_object_or_404(DepositoBancario, pk=deposito_id)
    ruta = ruta_captura_generada(deposito)
    if ruta is None:
        return archivo_no_disponible(request, "Esta captura todavía no se ha generado: pide que la genere el Encargado de Ingresos.")
    return entregar_archivo(request, ruta, ruta.name, entidad="DepositoBancario", entidad_id=deposito.pk, dia=deposito.dia, tipo="captura_deposito")


@requiere(Permiso.OPERAR)
@require_POST
def generar_captura_deposito_view(request, deposito_id):
    deposito = get_object_or_404(DepositoBancario, pk=deposito_id)
    try:
        generar_captura_deposito(deposito, request.user)
        messages.success(request, "Captura de depósito generada: ya se puede descargar.")
    except ControlDepositoInvalido as exc:
        messages.error(request, str(exc))
    return redirect(request.POST.get("volver") or f"/depositos/?deposito={deposito.pk}")
