"""Reintegros de Caja (Karla, 2026-09-22): registrar cuando una cajera
devuelve, total o parcialmente, una pérdida de caja que se le atribuyó,
antes de que se le descuente de la remuneración. Pantalla propia, no ligada
a un día puntual — igual que Control de Depósitos."""
from datetime import date
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from nucleo.descargas import archivo_no_disponible, entregar_archivo
from nucleo.permisos import Permiso, es_de_lectura, requiere, tiene_permiso
from nucleo.configuracion_servicio import cuentas_banco
from nucleo.documentos_servicio import nivel_usuario
from nucleo.models import ArqueoEfectivo, ReintegroCaja
from nucleo.umbrales import umbral
from nucleo.reintegros_servicio import (
    ReintegroInvalido, completar_detalle_reintegro, estado_agregado, monto_perdida_original,
    nombre_captura_reintegro, origen_desde_tipo_y_id, ruta_captura_generada, perdidas_menores_ocultas, perdidas_pendientes_de_reintegro,
    regenerar_asiento_reintegro,
    registrar_reintegro, saldo_pendiente, validar_reintegro,
)


def _monto_desde_post(texto: str) -> Decimal | None:
    texto = texto.replace("$", "").replace(".", "").replace(" ", "").strip()
    if not texto:
        return None
    try:
        return Decimal(texto)
    except InvalidOperation:
        return None


@requiere(Permiso.CONSULTAR)
def reintegros_caja(request):
    ver_menores = request.GET.get("ver_menores") == "1"
    pendientes = perdidas_pendientes_de_reintegro(incluir_menores=ver_menores)
    menores_ocultas = [] if ver_menores else perdidas_menores_ocultas()

    tipo = request.GET.get("tipo", "")
    origen_id = request.GET.get("origen")
    origen_actual = None
    if tipo and origen_id:
        try:
            origen_actual = origen_desde_tipo_y_id(tipo, int(origen_id))
        except (ReintegroInvalido, ValueError):
            origen_actual = None
    if origen_actual is None and pendientes:
        origen_actual = pendientes[0]
        tipo = "arqueo" if isinstance(origen_actual, ArqueoEfectivo) else "excepcion"

    reintegros_del_origen = []
    if origen_actual is not None:
        if isinstance(origen_actual, ArqueoEfectivo):
            reintegros_del_origen = list(ReintegroCaja.objects.filter(arqueo_origen=origen_actual).order_by("-registrado_en"))
        else:
            reintegros_del_origen = list(ReintegroCaja.objects.filter(excepcion_origen=origen_actual).order_by("-registrado_en"))

    return render(request, "nucleo/reintegros_caja.html", {
        "pendientes": [
            {
                "tipo": "arqueo" if isinstance(o, ArqueoEfectivo) else "excepcion",
                "id": o.pk, "origen": o, "cajera": o.cajera if isinstance(o, ArqueoEfectivo) else o.cajera_responsable,
                "dia": o.dia, "monto_perdida_original": monto_perdida_original(o), "saldo_pendiente": saldo_pendiente(o),
                "estado_agregado": estado_agregado(o),
            }
            for o in pendientes
        ],
        "menores_ocultas": len(menores_ocultas), "ver_menores": ver_menores,
        "umbral_menores": umbral("umbral_reintegro_minimo"),
        "tipo_actual": tipo,
        "origen_actual": origen_actual,
        "cajera_actual": (origen_actual.cajera if isinstance(origen_actual, ArqueoEfectivo) else origen_actual.cajera_responsable) if origen_actual else None,
        "monto_perdida_original": monto_perdida_original(origen_actual) if origen_actual else None,
        "saldo_pendiente": saldo_pendiente(origen_actual) if origen_actual else None,
        "reintegrado_validado": (
            monto_perdida_original(origen_actual) - saldo_pendiente(origen_actual)
        ) if origen_actual else None,
        "reintegros_del_origen": reintegros_del_origen,
        "puede_validar": tiene_permiso(request.user, Permiso.VALIDAR),
        "hoy": date.today(),
        "bancos": cuentas_banco(),
    })


@requiere(Permiso.OPERAR)
@require_POST
def registrar_reintegro_view(request, tipo, origen_id):
    try:
        origen = origen_desde_tipo_y_id(tipo, origen_id)
        cajera = origen.cajera if isinstance(origen, ArqueoEfectivo) else origen.cajera_responsable
        monto = _monto_desde_post(request.POST.get("monto", ""))
        reintegro = registrar_reintegro(origen, cajera=cajera, monto=monto, usuario=request.user)
        messages.success(request, "Reintegro registrado — completa el detalle bancario para enviarlo a validación.")
        return redirect(f"/reintegros-caja/?tipo={tipo}&origen={origen_id}&reintegro={reintegro.pk}")
    except ReintegroInvalido as exc:
        messages.error(request, str(exc))
    return redirect(f"/reintegros-caja/?tipo={tipo}&origen={origen_id}")


@requiere(Permiso.OPERAR)
@require_POST
def completar_detalle_reintegro_view(request, reintegro_id):
    reintegro = get_object_or_404(ReintegroCaja, pk=reintegro_id)
    tipo = "arqueo" if reintegro.arqueo_origen_id else "excepcion"
    origen_id = reintegro.arqueo_origen_id or reintegro.excepcion_origen_id
    try:
        texto_fecha = request.POST.get("fecha_reintegro", "").strip()
        fecha_reintegro = date.fromisoformat(texto_fecha) if texto_fecha else None
        completar_detalle_reintegro(
            reintegro, fecha_reintegro=fecha_reintegro,
            banco=request.POST.get("banco", ""), cuenta_bancaria=request.POST.get("cuenta_bancaria", ""),
            numero_operacion=request.POST.get("numero_operacion", ""),
            comprobante=request.FILES.get("comprobante"), observaciones=request.POST.get("observaciones", ""),
            usuario=request.user,
        )
        messages.success(request, "Detalle del reintegro guardado — queda pendiente de validación bancaria.")
    except ReintegroInvalido as exc:
        messages.error(request, str(exc))
    return redirect(f"/reintegros-caja/?tipo={tipo}&origen={origen_id}&reintegro={reintegro.pk}")


@requiere(Permiso.VALIDAR)
@require_POST
def validar_reintegro_view(request, reintegro_id):
    reintegro = get_object_or_404(ReintegroCaja, pk=reintegro_id)
    tipo = "arqueo" if reintegro.arqueo_origen_id else "excepcion"
    origen_id = reintegro.arqueo_origen_id or reintegro.excepcion_origen_id
    try:
        validar_reintegro(
            reintegro, aprobar=request.POST.get("accion") == "aprobar",
            movimiento_bancario_referencia=request.POST.get("movimiento_bancario_referencia", ""),
            comentario=request.POST.get("comentario", ""), usuario=request.user,
        )
        messages.success(request, "Reintegro revisado.")
    except ReintegroInvalido as exc:
        messages.error(request, str(exc))
    return redirect(f"/reintegros-caja/?tipo={tipo}&origen={origen_id}&reintegro={reintegro.pk}")


@requiere(Permiso.DESCARGAR_GENERADOS)
def descargar_asiento_reintegro(request, reintegro_id):
    """Entrega la captura YA generada; descargar no genera ni modifica nada."""
    reintegro = get_object_or_404(ReintegroCaja, pk=reintegro_id)
    ruta = ruta_captura_generada(reintegro)
    if ruta is None:
        return archivo_no_disponible(request, "Esta captura todavía no se ha generado: pide que la genere el Encargado de Ingresos.")
    return entregar_archivo(
        request, ruta, nombre_captura_reintegro(reintegro), entidad="ReintegroCaja", entidad_id=reintegro.pk,
        dia=reintegro.dia, tipo="captura_reintegro",
    )


@requiere(Permiso.OPERAR)
@require_POST
def generar_captura_reintegro_view(request, reintegro_id):
    reintegro = get_object_or_404(ReintegroCaja, pk=reintegro_id)
    try:
        regenerar_asiento_reintegro(reintegro, request.user)
        messages.success(request, "Captura del reintegro generada: ya se puede descargar.")
    except ReintegroInvalido as exc:
        messages.error(request, str(exc))
    if request.POST.get("volver"):
        return redirect(request.POST["volver"])
    tipo = "arqueo" if reintegro.arqueo_origen_id else "excepcion"
    origen_id = reintegro.arqueo_origen_id or reintegro.excepcion_origen_id
    return redirect(f"/reintegros-caja/?tipo={tipo}&origen={origen_id}&reintegro={reintegro.pk}")
