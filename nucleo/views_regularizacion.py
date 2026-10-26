"""Regularización de Documentos (Karla, 2026-09-22): guías de despacho y
notas de crédito del libro contable, pendientes de que la Encargada de
Ingresos las regularice a mano. Pantalla propia, no ligada a un día
puntual — igual que Control de Depósitos."""
from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from nucleo.permisos import Permiso, es_de_lectura, requiere, tiene_permiso
from nucleo.models import RegularizacionDocumento
from nucleo.documentos_servicio import nivel_usuario
from nucleo import notas_credito_servicio as nc
from nucleo.regularizacion_servicio import (
    RegularizacionInvalida, clasificar_documento, regularizar,
)


@requiere(Permiso.CONSULTAR)
def regularizacion_documentos(request):
    documentos = RegularizacionDocumento.objects.select_related("dia", "regularizado_por").order_by(
        "-dia__fecha", "tipo", "folio",
    )

    filtro = request.GET.get("estado", RegularizacionDocumento.Estado.PENDIENTE)
    documentos_filtrados = [d for d in documentos if d.estado == filtro] if filtro else list(documentos)

    documento_id_ver = request.GET.get("documento")
    documento_actual = next((d for d in documentos_filtrados if str(d.pk) == documento_id_ver), None) \
        or (documentos_filtrados[0] if documentos_filtrados else None)

    pendientes = documentos.filter(estado=RegularizacionDocumento.Estado.PENDIENTE)

    return render(request, "nucleo/regularizacion_documentos.html", {
        "documentos": documentos_filtrados,
        "documento_actual": documento_actual,
        "filtro": filtro,
        "cantidad_pendientes": pendientes.count(),
        "medios_pago": RegularizacionDocumento._meta.get_field("forma_pago").choices,
        "hoy": date.today(),
        "puede_clasificar": tiene_permiso(request.user, Permiso.VALIDAR),
        "nivel": nivel_usuario(request.user),
        "medios_verificados": RegularizacionDocumento.MedioVerificado.choices,
        "boletas_candidatas": nc.boletas_candidatas(documento_actual) if documento_actual and documento_actual.tipo == "CRED" else [],
        "requiere_segunda_firma": nc.requiere_segunda_firma(documento_actual) if documento_actual and documento_actual.tipo == "CRED" else False,
        "umbral_firma": nc.umbral("umbral_visado_perdida"),
    })


@requiere(Permiso.OPERAR)
@require_POST
def regularizar_documento_view(request, documento_id):
    documento = get_object_or_404(RegularizacionDocumento, pk=documento_id)
    try:
        texto_fecha = request.POST.get("fecha_documento_relacionado", "").strip()
        fecha_documento_relacionado = date.fromisoformat(texto_fecha) if texto_fecha else None
        regularizar(
            documento,
            documento_relacionado=request.POST.get("documento_relacionado", ""),
            fecha_documento_relacionado=fecha_documento_relacionado,
            referencia=request.POST.get("referencia", ""),
            comprobante=request.FILES.get("comprobante"),
            usuario=request.user,
        )
        messages.success(request, f"{documento.get_tipo_display()} {documento.folio} regularizada.")
    except RegularizacionInvalida as exc:
        messages.error(request, str(exc))
    return redirect(f"/regularizacion-documentos/?documento={documento.pk}")


@requiere(Permiso.VALIDAR)
@require_POST
def clasificar_documento_view(request, documento_id):
    documento = get_object_or_404(RegularizacionDocumento, pk=documento_id)
    try:
        clasificar_documento(
            documento, tipo=request.POST.get("tipo", ""), motivo=request.POST.get("motivo", ""), usuario=request.user,
        )
        messages.success(request, f"Documento {documento.folio} clasificado como {documento.get_tipo_display()}.")
    except RegularizacionInvalida as exc:
        messages.error(request, str(exc))
    return redirect(f"/regularizacion-documentos/?estado=&documento={documento.pk}")


def _volver_a(documento):
    return redirect(f"/regularizacion-documentos/?estado=&documento={documento.pk}")


@requiere(Permiso.OPERAR)
@require_POST
def registrar_evidencia_nc_view(request, documento_id):
    documento = get_object_or_404(RegularizacionDocumento, pk=documento_id)
    boleta = request.POST.get("boleta_original_texto", "").strip() or request.POST.get("boleta_original", "")
    try:
        nc.registrar_evidencia(
            documento, medio=request.POST.get("medio", ""), boleta_original=boleta,
            comentario=request.POST.get("comentario", ""), comprobante=request.FILES.get("comprobante"),
            usuario=request.user,
        )
        messages.success(request, "Evidencia registrada — queda pendiente de verificación del Jefe de Contabilidad.")
    except RegularizacionInvalida as exc:
        messages.error(request, str(exc))
    return _volver_a(documento)


@requiere(Permiso.VALIDAR)
@require_POST
def verificar_nc_view(request, documento_id):
    documento = get_object_or_404(RegularizacionDocumento, pk=documento_id)
    try:
        nc.verificar(
            documento, aprobar=request.POST.get("accion") == "aprobar",
            comentario=request.POST.get("comentario", ""), usuario=request.user,
        )
        messages.success(request, "Verificación registrada.")
    except RegularizacionInvalida as exc:
        messages.error(request, str(exc))
    return _volver_a(documento)


@requiere(Permiso.VALIDAR)
@require_POST
def folio_odoo_nc_view(request, documento_id):
    documento = get_object_or_404(RegularizacionDocumento, pk=documento_id)
    try:
        nc.registrar_folio_odoo(documento, request.POST.get("folio_odoo", ""), request.user)
        messages.success(request, "Folio de Odoo registrado.")
    except RegularizacionInvalida as exc:
        messages.error(request, str(exc))
    return _volver_a(documento)


@requiere(Permiso.AUTORIZAR_EXCEPCION)
@require_POST
def segunda_firma_nc_view(request, documento_id):
    documento = get_object_or_404(RegularizacionDocumento, pk=documento_id)
    try:
        nc.segunda_firma(
            documento, aprobar=request.POST.get("accion") == "aprobar",
            comentario=request.POST.get("comentario", ""), usuario=request.user,
        )
        messages.success(request, "Segunda firma registrada.")
    except RegularizacionInvalida as exc:
        messages.error(request, str(exc))
    return _volver_a(documento)
