from datetime import date
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from nucleo.arqueo_servicio import (
    DENOMINACIONES_CLP, AutorizacionInvalida, ConteoInvalido, autorizar_diferencia,
    preparar_arqueos_del_dia, registrar_conteo,
)
from nucleo.carga_servicio import ArchivoInvalido, CargaDuplicada, CargaEnConflicto, procesar_carga, validaciones_de_carga
from nucleo.cierre_servicio import (
    CierreInvalido, aprobar_y_cerrar_dia, estado_checklist, puede_aprobar, puede_aprobar_usuario, reabrir_dia,
)
from nucleo.conciliacion_servicio import ejecutar_conciliacion
from nucleo.documentos_servicio import (
    AsignacionInvalida, asignar_factura_a_excepcion, asignar_factura_a_venta,
    guias_pendientes, otros_ingresos_pendientes,
)
from nucleo.dashboard_servicio import resumen_dia
from nucleo.exportacion_servicio import ExportacionInvalida, generar_exportacion
from nucleo.historico_servicio import buscar_transversal, calendario_mes, resumen_dias
from nucleo.calidad_servicio import resumen_calidad
from nucleo.models import ArqueoEfectivo, DiaArqueo, Excepcion, LoteCarga, MotivoResolucion, MovimientoProveedor
from nucleo.resolucion_servicio import ResolucionInvalida, resolver_excepcion
from nucleo.ventas_dashboard_servicio import rango_del_atajo, resumen_periodo

# Mientras no exista el Mantenedor de usuarios y roles (D10 solo define
# QUIÉNES son Karla, el Jefe de Contabilidad y el Sub Gerente — no CÓMO
# se marca eso en el sistema), se usa is_superuser como equivalente
# provisorio del rol Aprobador. Control C1 (segregación de funciones)
# sigue pendiente de un mantenedor real.
def es_aprobador(view):
    """403 explícito para un usuario ya autenticado sin rol de aprobador
    — no lo devuelve a la pantalla de login, que sería confuso (ya inició
    sesión; lo que le falta es permiso, no autenticación)."""
    from functools import wraps

    from django.core.exceptions import PermissionDenied

    @wraps(view)
    def envoltura(request, *args, **kwargs):
        if not request.user.is_superuser:
            raise PermissionDenied("Solo un aprobador puede autorizar una diferencia sobre el umbral.")
        return view(request, *args, **kwargs)
    return envoltura


@login_required
def bandeja_excepciones(request, fecha):
    dia = get_object_or_404(DiaArqueo, fecha=date.fromisoformat(fecha))

    tipo_filtro = request.GET.get("tipo", "")
    excepciones_abiertas = dia.excepciones.filter(abierta=True).select_related("pago__venta", "movimiento")
    if tipo_filtro:
        excepciones_abiertas = excepciones_abiertas.filter(tipo=tipo_filtro)
    # Excepcion.Meta.ordering ya es "-monto_en_juego" (§15: por monto en
    # juego, no por orden de aparición).

    tipos_con_conteo = [
        (valor, etiqueta, dia.excepciones.filter(abierta=True, tipo=valor).count())
        for valor, etiqueta in Excepcion.Tipo.choices
    ]
    tipos_con_conteo = [t for t in tipos_con_conteo if t[2] > 0]
    total_abiertas = dia.excepciones.filter(abierta=True).count()
    monto_total_en_juego = sum(e.monto_en_juego for e in excepciones_abiertas)

    excepcion_id_ver = request.GET.get("ver")
    if excepcion_id_ver:
        excepcion_actual = get_object_or_404(Excepcion, pk=excepcion_id_ver, dia=dia)
    else:
        excepcion_actual = excepciones_abiertas.first()

    movimientos_libres = []
    if excepcion_actual and excepcion_actual.tipo == "pago_sin_movimiento":
        ids_usados = set(
            dia.movimientos.exclude(coincidencias=None).values_list("id", flat=True)
        )
        movimientos_libres = list(dia.movimientos.exclude(id__in=ids_usados).order_by("fecha_hora"))

    contexto = {
        "dia": dia,
        "excepciones": excepciones_abiertas,
        "excepcion_actual": excepcion_actual,
        "tipos_con_conteo": tipos_con_conteo,
        "total_abiertas": total_abiertas,
        "tipo_filtro": tipo_filtro,
        "monto_total_en_juego": monto_total_en_juego,
        "motivos": MotivoResolucion.objects.filter(
            aplica_a=MotivoResolucion.AplicaA.EXCEPCION, activo=True,
        ),
        "movimientos_libres": movimientos_libres,
    }
    return render(request, "nucleo/bandeja_excepciones.html", contexto)


@login_required
@require_POST
def reconciliar_dia(request, fecha):
    dia = get_object_or_404(DiaArqueo, fecha=date.fromisoformat(fecha))
    resultado = ejecutar_conciliacion(dia, request.user)
    messages.success(
        request,
        f"Conciliación ejecutada: {len(resultado.coincidencias)} coincidencias, "
        f"{len(resultado.excepciones)} excepciones.",
    )
    return redirect("bandeja_excepciones", fecha=fecha)


@login_required
@require_POST
def resolver_excepcion_view(request, excepcion_id):
    excepcion = get_object_or_404(Excepcion, pk=excepcion_id)
    fecha = excepcion.dia.fecha.isoformat()

    try:
        resolver_excepcion(
            excepcion,
            resolucion=request.POST.get("resolucion", ""),
            motivo_id=request.POST.get("motivo_id"),
            comentario=request.POST.get("comentario", "").strip(),
            usuario=request.user,
            movimiento_id_alternativo=request.POST.get("movimiento_alternativo") or None,
        )
        messages.success(request, "Excepción resuelta y registrada en la bitácora.")
    except ResolucionInvalida as exc:
        messages.error(request, str(exc))

    return redirect(f"/excepciones/{fecha}/")


@login_required
def arqueo_efectivo(request, fecha):
    dia = get_object_or_404(DiaArqueo, fecha=date.fromisoformat(fecha))
    arqueos = preparar_arqueos_del_dia(dia)

    consolidado = {
        "teorico": sum(a.teorico for a in arqueos),
        "ajustes": sum(a.ajustes for a in arqueos),
        "esperado": sum(a.esperado for a in arqueos),
        "fondo_inicial": sum(a.fondo_inicial for a in arqueos),
        "gastos_autorizados": sum(a.gastos_autorizados for a in arqueos),
        "contado": sum(a.contado for a in arqueos if a.contado is not None),
        "diferencia": sum(a.diferencia for a in arqueos if a.diferencia is not None),
        "todas_contadas": all(a.contado is not None for a in arqueos),
    }

    caja_id_ver = request.GET.get("caja")
    arqueo_actual = next((a for a in arqueos if str(a.pk) == caja_id_ver), None) or (arqueos[0] if arqueos else None)

    denominaciones_form = []
    if arqueo_actual:
        conteo_actual = {
            int(c.denominacion): c.cantidad for c in arqueo_actual.denominaciones.all()
        }
        denominaciones_form = [(d, conteo_actual.get(d, "")) for d in DENOMINACIONES_CLP]

    contexto = {
        "dia": dia,
        "arqueos": arqueos,
        "consolidado": consolidado,
        "arqueo_actual": arqueo_actual,
        "denominaciones_form": denominaciones_form,
        "motivos_diferencia": MotivoResolucion.objects.filter(
            aplica_a=MotivoResolucion.AplicaA.DIFERENCIA_CAJA, activo=True,
        ),
        "deposito": getattr(dia, "deposito", None),
        "puede_autorizar": request.user.is_superuser,
    }
    return render(request, "nucleo/arqueo_efectivo.html", contexto)


@login_required
@require_POST
def registrar_conteo_view(request, arqueo_id):
    arqueo = get_object_or_404(ArqueoEfectivo, pk=arqueo_id)
    fecha = arqueo.dia.fecha.isoformat()

    try:
        fondo_inicial = Decimal(request.POST.get("fondo_inicial") or "0")
        gastos_autorizados = Decimal(request.POST.get("gastos_autorizados") or "0")
        denominaciones = {
            int(d): int(request.POST.get(f"denom_{d}") or 0) for d in DENOMINACIONES_CLP
        }
        registrar_conteo(
            arqueo, fondo_inicial=fondo_inicial, gastos_autorizados=gastos_autorizados,
            denominaciones=denominaciones, usuario=request.user,
        )
        messages.success(request, f"Conteo de {arqueo.caja} registrado.")
    except (ConteoInvalido, InvalidOperation, ValueError) as exc:
        messages.error(request, f"No se pudo registrar el conteo: {exc}")

    return redirect(f"/arqueo/{fecha}/?caja={arqueo_id}")


@login_required
@es_aprobador
@require_POST
def autorizar_diferencia_view(request, arqueo_id):
    arqueo = get_object_or_404(ArqueoEfectivo, pk=arqueo_id)
    fecha = arqueo.dia.fecha.isoformat()

    try:
        autorizar_diferencia(
            arqueo, motivo_id=request.POST.get("motivo_id"),
            comentario=request.POST.get("comentario", "").strip(), usuario=request.user,
        )
        messages.success(request, f"Diferencia de {arqueo.caja} autorizada.")
    except AutorizacionInvalida as exc:
        messages.error(request, str(exc))

    return redirect(f"/arqueo/{fecha}/?caja={arqueo_id}")


@login_required
@require_POST
def registrar_deposito_view(request, fecha):
    from nucleo.models import Deposito
    dia = get_object_or_404(DiaArqueo, fecha=date.fromisoformat(fecha))
    try:
        Deposito.objects.update_or_create(
            dia=dia,
            defaults={
                "monto": Decimal(request.POST.get("monto") or "0"),
                "comprobante": request.POST.get("comprobante", "").strip(),
                "fecha": request.POST.get("fecha") or dia.fecha,
                "fondo_retenido": Decimal(request.POST.get("fondo_retenido") or "0"),
            },
        )
        messages.success(request, "Depósito registrado.")
    except (InvalidOperation, ValueError) as exc:
        messages.error(request, f"No se pudo registrar el depósito: {exc}")
    return redirect(f"/arqueo/{fecha}/")


@login_required
def cierre_y_exportacion(request, fecha):
    dia = get_object_or_404(DiaArqueo, fecha=date.fromisoformat(fecha))
    dia_ya_aprobado = dia.estado in (
        DiaArqueo.Estado.APROBADO, DiaArqueo.Estado.CERRADO, DiaArqueo.Estado.EXPORTADO,
    )
    puede_usuario, razon_usuario = puede_aprobar_usuario(dia, request.user)
    contexto = {
        "dia": dia,
        "checklist": estado_checklist(dia),
        "puede_aprobar": puede_usuario and not dia_ya_aprobado,
        "razon_aprobacion": razon_usuario,
        "puede_exportar": dia.estado in (DiaArqueo.Estado.APROBADO, DiaArqueo.Estado.CERRADO),
        "puede_aprobar_rol": request.user.is_superuser,
        "exportaciones": dia.exportaciones.order_by("-version"),
        "guias_pendientes": guias_pendientes(dia),
        "otros_ingresos_pendientes": otros_ingresos_pendientes(dia),
    }
    return render(request, "nucleo/cierre_exportacion.html", contexto)


@login_required
@require_POST
def asignar_factura_venta_view(request, venta_id):
    from nucleo.models import Venta
    venta = get_object_or_404(Venta, pk=venta_id)
    fecha = venta.dia.fecha.isoformat()
    try:
        asignar_factura_a_venta(venta, request.POST.get("factura", ""), request.user)
        messages.success(request, f"Factura asignada a la comanda {venta.n_interno}.")
    except AsignacionInvalida as exc:
        messages.error(request, str(exc))
    return redirect("cierre_y_exportacion", fecha=fecha)


@login_required
@require_POST
def asignar_factura_excepcion_view(request, excepcion_id):
    excepcion = get_object_or_404(Excepcion, pk=excepcion_id)
    fecha = excepcion.dia.fecha.isoformat()
    try:
        asignar_factura_a_excepcion(excepcion, request.POST.get("factura", ""), request.user)
        messages.success(request, "Boleta de otros ingresos registrada.")
    except AsignacionInvalida as exc:
        messages.error(request, str(exc))
    return redirect("cierre_y_exportacion", fecha=fecha)


@login_required
@es_aprobador
@require_POST
def aprobar_dia_view(request, fecha):
    dia = get_object_or_404(DiaArqueo, fecha=date.fromisoformat(fecha))
    try:
        aprobar_y_cerrar_dia(dia, request.user)
        messages.success(request, "Día aprobado y cerrado.")
    except CierreInvalido as exc:
        messages.error(request, str(exc))
    return redirect("cierre_y_exportacion", fecha=fecha)


@login_required
@es_aprobador
@require_POST
def reabrir_dia_view(request, fecha):
    dia = get_object_or_404(DiaArqueo, fecha=date.fromisoformat(fecha))
    try:
        reabrir_dia(dia, request.user, request.POST.get("motivo", ""))
        messages.success(request, "Día reabierto. La exportación previa quedó invalidada.")
    except CierreInvalido as exc:
        messages.error(request, str(exc))
    return redirect("cierre_y_exportacion", fecha=fecha)


@login_required
@es_aprobador
@require_POST
def generar_exportacion_view(request, fecha):
    dia = get_object_or_404(DiaArqueo, fecha=date.fromisoformat(fecha))
    try:
        exportacion = generar_exportacion(dia, request.user)
        messages.success(request, f"Exportación v{exportacion.version} generada.")
    except ExportacionInvalida as exc:
        messages.error(request, str(exc))
    return redirect("cierre_y_exportacion", fecha=fecha)


@login_required
def dashboard_dia(request, fecha):
    dia = get_object_or_404(DiaArqueo, fecha=date.fromisoformat(fecha))
    contexto = resumen_dia(dia)
    contexto["puede_aprobar_rol"] = request.user.is_superuser
    return render(request, "nucleo/dashboard_dia.html", contexto)


@login_required
def historico(request):
    from datetime import timedelta
    hoy = date.today()
    fecha_hasta = date.fromisoformat(request.GET.get("hasta")) if request.GET.get("hasta") else hoy
    fecha_desde = date.fromisoformat(request.GET.get("desde")) if request.GET.get("desde") else fecha_hasta - timedelta(days=30)
    anio = int(request.GET.get("anio", fecha_hasta.year))
    mes = int(request.GET.get("mes", fecha_hasta.month))

    contexto = {
        "dias": resumen_dias(fecha_desde, fecha_hasta),
        "semanas": calendario_mes(anio, mes),
        "anio": anio,
        "mes": mes,
        "fecha_desde": fecha_desde,
        "fecha_hasta": fecha_hasta,
        "busqueda": request.GET.get("q", ""),
        "resultados": buscar_transversal(request.GET.get("q", "")) if request.GET.get("q") else None,
    }
    return render(request, "nucleo/historico.html", contexto)


@login_required
def calidad_registro(request):
    from datetime import timedelta
    hoy = date.today()
    fecha_hasta = date.fromisoformat(request.GET.get("hasta")) if request.GET.get("hasta") else hoy
    fecha_desde = date.fromisoformat(request.GET.get("desde")) if request.GET.get("desde") else fecha_hasta - timedelta(days=30)

    contexto = resumen_calidad(fecha_desde, fecha_hasta)
    contexto["fecha_desde"] = fecha_desde
    contexto["fecha_hasta"] = fecha_hasta
    return render(request, "nucleo/calidad_registro.html", contexto)


@login_required
def ventas_dashboard(request):
    atajo = request.GET.get("atajo", "mes")
    if atajo == "personalizado" and request.GET.get("desde") and request.GET.get("hasta"):
        desde = date.fromisoformat(request.GET["desde"])
        hasta = date.fromisoformat(request.GET["hasta"])
    else:
        desde, hasta = rango_del_atajo(atajo)

    contexto = resumen_periodo(desde, hasta)
    contexto["atajo"] = atajo
    contexto["atajos"] = [
        ("mes", "Mes"), ("trimestre", "Trimestre"), ("anio_en_curso", "Año en curso"),
        ("12_meses", "12 meses"), ("personalizado", "Personalizado"),
    ]
    contexto["max_mes"] = max([m for _, m in contexto["por_mes"]], default=0) or 1
    contexto["max_dia"] = max([m for _, m in contexto["por_dia"]], default=0) or 1
    contexto["max_hora"] = max([m for _, m in contexto["por_hora"]], default=0) or 1
    contexto["total_excluido"] = sum(de["monto_provisorio"] for de in contexto["dias_excluidos"])
    return render(request, "nucleo/ventas_dashboard.html", contexto)


@login_required
def carga_de_archivos(request, fecha):
    dia, _ = DiaArqueo.objects.get_or_create(fecha=date.fromisoformat(fecha))
    contexto = {
        "dia": dia,
        "lote_pos": dia.lotes.filter(fuente=LoteCarga.Fuente.POS).order_by("-cargado_en").first(),
        "lote_tbk": dia.lotes.filter(fuente=LoteCarga.Fuente.TRANSBANK).order_by("-cargado_en").first(),
        "resumen_pos": {"comandas": dia.ventas.count(), "lineas": sum(v.lineas.count() for v in dia.ventas.all())},
        "resumen_tbk": {"movimientos": dia.movimientos.count()},
        "validaciones": validaciones_de_carga(dia),
    }
    return render(request, "nucleo/carga_archivos.html", contexto)


@login_required
@require_POST
def cargar_archivo_view(request, fecha, fuente):
    dia, _ = DiaArqueo.objects.get_or_create(fecha=date.fromisoformat(fecha))
    archivo = request.FILES.get("archivo")
    if not archivo:
        messages.error(request, "Selecciona un archivo primero.")
        return redirect("carga_de_archivos", fecha=fecha)

    try:
        procesar_carga(dia, archivo, fuente, request.user)
        messages.success(request, "Archivo cargado y procesado.")
    except (ArchivoInvalido, CargaDuplicada, CargaEnConflicto) as exc:
        messages.error(request, str(exc))
    return redirect("carga_de_archivos", fecha=fecha)


@login_required
def descargar_archivo_original(request, lote_id):
    from django.http import FileResponse
    lote = get_object_or_404(LoteCarga, pk=lote_id)
    return FileResponse(open(lote.ruta_almacenamiento, "rb"), as_attachment=True, filename=lote.nombre_archivo)
