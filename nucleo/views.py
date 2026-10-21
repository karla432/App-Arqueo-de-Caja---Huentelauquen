from datetime import date
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from nucleo.descargas import archivo_no_disponible, entregar_archivo
from nucleo.permisos import Permiso, PermisoDenegado, es_de_lectura, requiere, tiene_permiso
from nucleo.arqueo_servicio import (
    AsignacionCajeraInvalida, AutorizacionInvalida, ConteoInvalido, GastoCajaInvalido,
    asignar_cajera, autorizar_diferencia, preparar_arqueos_del_dia, registrar_conteo, registrar_gasto_caja,
)
from nucleo.carga_servicio import (
    ArchivoInvalido, CargaDuplicada, CargaEnConflicto, procesar_carga, procesar_carga_pos, validaciones_de_carga,
)
from nucleo.cierre_servicio import (
    CierreInvalido, aprobar_y_cerrar_dia, estado_checklist, puede_aprobar, puede_aprobar_usuario, reabrir_dia,
    requiere_motivo_de_excepcion,
)
from nucleo.conciliacion_servicio import ejecutar_conciliacion
from nucleo.documentos_servicio import (
    AsignacionInvalida, asignar_factura_a_excepcion, asignar_factura_a_venta,
    guias_pendientes, nivel_usuario, otros_ingresos_pendientes,
)
from nucleo.dashboard_servicio import resumen_dia
from nucleo.exportacion_servicio import (
    ExportacionInvalida, exportacion_vigente, generar_exportacion, intentar_generar_exportacion, motivo_de_bloqueo,
)
from nucleo.models import Exportacion
from nucleo.historico_servicio import buscar_transversal, calendario_mes, resumen_dias
from nucleo.calidad_servicio import resumen_calidad
from nucleo.models import (
    ArqueoEfectivo, Cajera, CuentaContable, DiaArqueo, Excepcion, LoteCarga, MotivoResolucion, MovimientoProveedor,
)
from nucleo.perdidas_servicio import (
    VisadoInvalido, diferencias_de_caja_pendientes_de_visado, perdidas_pendientes_de_visado,
    visar_diferencia_caja, visar_perdida,
)
from nucleo.pagos_divididos_servicio import planes_del_dia
from nucleo.regularizacion_servicio import avisos_efectivo_guias, documentos_efectivo_de_caja
from nucleo.reintegros_servicio import estado_agregado as estado_agregado_reintegro, saldo_pendiente as saldo_pendiente_reintegro
from nucleo.configuracion_servicio import config_arqueo_caja
from nucleo.umbrales import umbral
from nucleo.resolucion_servicio import ResolucionInvalida, resolver_excepcion
from nucleo.reporte_productos_servicio import generar_excel as generar_excel_productos, resumen_por_producto, totales as totales_productos
from nucleo.ventas_dashboard_servicio import rango_del_atajo, resumen_periodo

def _sin_dia(request, fecha):
    """El día todavía no existe (nada cargado): en vez de un 404 técnico,
    se lleva a la persona a cargar los archivos de ese día."""
    messages.info(request, f"El {fecha} todavía no tiene archivos cargados — empieza cargándolos aquí.")
    return redirect("carga_de_archivos", fecha=fecha)


@requiere(Permiso.CONSULTAR)
def bandeja_excepciones(request, fecha):
    dia = DiaArqueo.objects.filter(fecha=date.fromisoformat(fecha)).first()
    if dia is None:
        return _sin_dia(request, fecha)

    tipo_filtro = request.GET.get("tipo", "")
    ver_resueltas = request.GET.get("estado") == "resueltas"
    excepciones_abiertas = dia.excepciones.filter(abierta=True).select_related("pago__venta", "movimiento")
    if tipo_filtro:
        excepciones_abiertas = excepciones_abiertas.filter(tipo=tipo_filtro)
    # Resueltas (Karla, 2026-10-21): para revisar lo decidido y, si estuvo mal,
    # volver a abrirlo. Los ajustes de sencillo los recalcula el motor solo.
    excepciones_resueltas = (
        dia.excepciones.filter(abierta=False)
        .exclude(resolucion=Excepcion.Resolucion.AJUSTE_SENCILLO)
        .select_related("pago__venta", "movimiento", "motivo", "resuelta_por")
        .order_by("-resuelta_en", "-pk")
    )
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
        excepcion_actual = (excepciones_resueltas if ver_resueltas else excepciones_abiertas).first()

    from nucleo.reapertura_excepciones_servicio import motivo_para_no_reabrir
    no_reabrir = motivo_para_no_reabrir(excepcion_actual) if excepcion_actual and not excepcion_actual.abierta else ""

    movimientos_libres = []
    if excepcion_actual and excepcion_actual.tipo == "pago_sin_movimiento":
        ids_usados = set(
            dia.movimientos.exclude(coincidencias=None).values_list("id", flat=True)
        )
        movimientos_libres = list(dia.movimientos.exclude(id__in=ids_usados).order_by("fecha_hora"))

    cajera_sugerida = None
    if excepcion_actual and excepcion_actual.pago_id:
        arqueo_caja = dia.arqueos_caja.filter(caja=excepcion_actual.pago.venta.caja).first()
        cajera_sugerida = arqueo_caja.cajera if arqueo_caja else None

    contexto = {
        "dia": dia,
        "excepciones": excepciones_abiertas,
        "lista": excepciones_resueltas if ver_resueltas else excepciones_abiertas,
        "ver_resueltas": ver_resueltas,
        "total_resueltas": excepciones_resueltas.count(),
        "puede_reabrir_excepcion": tiene_permiso(request.user, Permiso.REABRIR_EXCEPCION),
        "motivo_no_reabrir": no_reabrir,
        "excepcion_actual": excepcion_actual,
        "tipos_con_conteo": tipos_con_conteo,
        "total_abiertas": total_abiertas,
        "tipo_filtro": tipo_filtro,
        "monto_total_en_juego": monto_total_en_juego,
        "motivos": MotivoResolucion.objects.filter(
            aplica_a=MotivoResolucion.AplicaA.EXCEPCION, activo=True,
        ),
        "movimientos_libres": movimientos_libres,
        "cuentas_contables": CuentaContable.objects.filter(activo=True),
        "cajeras": Cajera.objects.filter(activo=True),
        "cajera_sugerida": cajera_sugerida,
    }
    return render(request, "nucleo/bandeja_excepciones.html", contexto)


@requiere(Permiso.OPERAR)
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


@requiere(Permiso.OPERAR)
@require_POST
def resolver_excepcion_view(request, excepcion_id):
    excepcion = get_object_or_404(Excepcion, pk=excepcion_id)
    fecha = excepcion.dia.fecha.isoformat()

    lineas = [
        {
            "medio": request.POST.get(f"linea_medio_{i}") or None,
            "monto": request.POST.get(f"linea_monto_{i}") or None,
            "movimiento_id": request.POST.get(f"linea_movimiento_{i}") or None,
        }
        for i in range(1, 4)
    ]

    try:
        resolver_excepcion(
            excepcion,
            resolucion=request.POST.get("resolucion", ""),
            motivo_id=request.POST.get("motivo_id"),
            comentario=request.POST.get("comentario", "").strip(),
            usuario=request.user,
            movimiento_id_alternativo=request.POST.get("movimiento_alternativo") or None,
            cuenta_contable_id=request.POST.get("cuenta_contable_id") or None,
            cajera_id=request.POST.get("cajera_id") or None,
            lineas=lineas,
        )
        messages.success(request, "Excepción resuelta y registrada en la bitácora.")
    except ResolucionInvalida as exc:
        messages.error(request, str(exc))

    return redirect(f"/excepciones/{fecha}/")


@requiere(Permiso.REABRIR_EXCEPCION)
@require_POST
def reabrir_excepcion_view(request, excepcion_id):
    from nucleo.reapertura_excepciones_servicio import ReaperturaInvalida, reabrir_excepcion
    excepcion = get_object_or_404(Excepcion, pk=excepcion_id)
    fecha = excepcion.dia.fecha.isoformat()
    try:
        reabrir_excepcion(excepcion, request.user, request.POST.get("motivo_reapertura", ""))
        messages.success(request, "Excepción abierta de nuevo: quedó como recién conciliada y el cambio está en la bitácora.")
        return redirect(f"/excepciones/{fecha}/?ver={excepcion.pk}")
    except ReaperturaInvalida as exc:
        messages.error(request, str(exc))
    return redirect(f"/excepciones/{fecha}/?estado=resueltas&ver={excepcion.pk}")


@requiere(Permiso.VISAR)
@require_POST
def visar_perdida_view(request, excepcion_id):
    excepcion = get_object_or_404(Excepcion, pk=excepcion_id)
    fecha = excepcion.dia.fecha.isoformat()

    try:
        visar_perdida(excepcion, request.user, request.POST.get("motivo_excepcion", ""))
        messages.success(request, "Pérdida visada.")
        _reintentar_exportacion(request, excepcion.dia)
    except VisadoInvalido as exc:
        messages.error(request, str(exc))

    return redirect(f"/excepciones/{fecha}/")


@requiere(Permiso.CONSULTAR)
def arqueo_efectivo(request, fecha):
    dia = DiaArqueo.objects.filter(fecha=date.fromisoformat(fecha)).first()
    if dia is None:
        return _sin_dia(request, fecha)
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

    reintegro_info = None
    if arqueo_actual and arqueo_actual.diferencia is not None and arqueo_actual.diferencia < 0 and arqueo_actual.cajera_id:
        from nucleo.models import ReintegroCaja
        reintegro_info = {
            "estado": estado_agregado_reintegro(arqueo_actual),
            "saldo_pendiente": saldo_pendiente_reintegro(arqueo_actual),
            "reintegros": ReintegroCaja.objects.filter(arqueo_origen=arqueo_actual).order_by("-registrado_en"),
        }

    contexto = {
        "dia": dia,
        "arqueos": arqueos,
        "consolidado": consolidado,
        "arqueo_actual": arqueo_actual,
        "motivos_diferencia": MotivoResolucion.objects.filter(
            aplica_a=MotivoResolucion.AplicaA.DIFERENCIA_CAJA, activo=True,
        ),
        "motivos_gasto_caja": MotivoResolucion.objects.filter(
            aplica_a=MotivoResolucion.AplicaA.GASTO_CAJA, activo=True,
        ),
        "puede_autorizar": tiene_permiso(request.user, Permiso.AUTORIZAR_EXCEPCION),
        "cajeras": Cajera.objects.filter(activo=True),
        "umbral_visado": umbral("umbral_visado_perdida"),
        "puede_visar_perdida": tiene_permiso(request.user, Permiso.VISAR),
        "reintegro_info": reintegro_info,
        "avisos_efectivo_guias": avisos_efectivo_guias(dia),
        "pagos_divididos_de_la_caja": [p for p in planes_del_dia(dia) if arqueo_actual and p.venta.caja == arqueo_actual.caja],
        "documentos_de_la_caja": documentos_efectivo_de_caja(dia, arqueo_actual.caja) if arqueo_actual else [],
    }
    return render(request, "nucleo/arqueo_efectivo.html", contexto)


@requiere(Permiso.VISAR)
@require_POST
def visar_diferencia_caja_view(request, arqueo_id):
    arqueo = get_object_or_404(ArqueoEfectivo, pk=arqueo_id)
    fecha = arqueo.dia.fecha.isoformat()
    try:
        visar_diferencia_caja(arqueo, request.user, request.POST.get("motivo_excepcion", ""))
        messages.success(request, f"Diferencia de {arqueo.caja} visada.")
        _reintentar_exportacion(request, arqueo.dia)
    except VisadoInvalido as exc:
        messages.error(request, str(exc))
    return redirect(f"/arqueo/{fecha}/?caja={arqueo_id}")


@requiere(Permiso.OPERAR)
@require_POST
def registrar_conteo_view(request, arqueo_id):
    arqueo = get_object_or_404(ArqueoEfectivo, pk=arqueo_id)
    fecha = arqueo.dia.fecha.isoformat()

    try:
        fondo_inicial = Decimal(request.POST.get("fondo_inicial") or "0")
        # "Gastos autorizados" ya no se teclea aquí (Karla, 2026-09-18) —
        # se arma solo de las líneas registradas en "Agregar gasto o
        # devolución"; si el formulario no lo envía, se conserva el valor
        # que ya había, nunca se resetea a $0.
        gastos_autorizados = Decimal(request.POST.get("gastos_autorizados") or str(arqueo.gastos_autorizados))
        # Se acepta como se escribe en Chile: "60.000", "$60.000" o "60000".
        texto_total = request.POST.get("total_contado", "").replace("$", "").replace(".", "").replace(" ", "").strip()
        if not texto_total:
            raise ConteoInvalido("Escribe el total de efectivo contado.")
        registrar_conteo(
            arqueo, fondo_inicial=fondo_inicial, gastos_autorizados=gastos_autorizados,
            total_contado=Decimal(texto_total), usuario=request.user,
        )
        messages.success(request, f"Conteo de {arqueo.caja} registrado.")
    except (ConteoInvalido, InvalidOperation, ValueError) as exc:
        messages.error(request, f"No se pudo registrar el conteo: {exc}")

    return redirect(f"/arqueo/{fecha}/?caja={arqueo_id}")


@requiere(Permiso.OPERAR)
@require_POST
def registrar_gasto_caja_view(request, arqueo_id):
    arqueo = get_object_or_404(ArqueoEfectivo, pk=arqueo_id)
    fecha = arqueo.dia.fecha.isoformat()

    try:
        monto = Decimal(request.POST.get("monto") or "0")
        registrar_gasto_caja(
            arqueo, monto=monto, motivo_id=request.POST.get("motivo_id"),
            boleta_asociada=request.POST.get("boleta_asociada", "").strip(),
            comentario=request.POST.get("comentario", "").strip(), usuario=request.user,
        )
        messages.success(request, f"Gasto de {arqueo.caja} registrado.")
    except (GastoCajaInvalido, InvalidOperation, ValueError) as exc:
        messages.error(request, f"No se pudo registrar el gasto: {exc}")

    return redirect(f"/arqueo/{fecha}/?caja={arqueo_id}")


@requiere(Permiso.OPERAR)
@require_POST
def asignar_cajera_view(request, arqueo_id):
    arqueo = get_object_or_404(ArqueoEfectivo, pk=arqueo_id)
    fecha = arqueo.dia.fecha.isoformat()

    try:
        asignar_cajera(arqueo, cajera_id=request.POST.get("cajera_id"), usuario=request.user)
        messages.success(request, f"Cajera de {arqueo.caja} asignada.")
    except AsignacionCajeraInvalida as exc:
        messages.error(request, str(exc))

    return redirect(f"/arqueo/{fecha}/?caja={arqueo_id}")


@requiere(Permiso.AUTORIZAR_EXCEPCION)
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


@requiere(Permiso.CONSULTAR)
def cierre_y_exportacion(request, fecha):
    dia = DiaArqueo.objects.filter(fecha=date.fromisoformat(fecha)).first()
    if dia is None:
        return _sin_dia(request, fecha)
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
        "puede_aprobar_rol": tiene_permiso(request.user, Permiso.APROBAR_CIERRE),
        "exportaciones": dia.exportaciones.order_by("-version"),
        "guias_pendientes": guias_pendientes(dia),
        "otros_ingresos_pendientes": otros_ingresos_pendientes(dia),
        "perdidas_pendientes": perdidas_pendientes_de_visado(dia).select_related("cajera_responsable", "cuenta_contable"),
        "diferencias_de_caja_pendientes": diferencias_de_caja_pendientes_de_visado(dia),
        "puede_visar_perdida": tiene_permiso(request.user, Permiso.VISAR),
        "arqueo_caja": config_arqueo_caja(),
        "pagos_divididos": planes_del_dia(dia),
    }
    contexto.update(_contexto_del_cierre(dia, contexto["checklist"], dia_ya_aprobado))
    contexto["requiere_motivo"] = (not dia_ya_aprobado) and requiere_motivo_de_excepcion(dia, request.user)
    return render(request, "nucleo/cierre_exportacion.html", contexto)


def _filas_del_checklist(dia, checklist: dict) -> list[dict]:
    """La lista de verificación con su título, si bloquea el cierre y a dónde ir a resolverla (Karla, 2026-09-25)."""
    from django.urls import reverse
    fecha = dia.fecha.isoformat()
    regularizacion = reverse("regularizacion_documentos") + "?estado=pendiente"
    plan = [
        ("notas_credito_verificadas", "Respaldo de las notas de crédito", regularizacion, "Resolver →"),
        ("documentos_sin_clasificar", "Documentos del libro clasificados", regularizacion, "Clasificar →"),
        ("conciliacion_ejecutada", "Conciliación del día", None, ""),
        ("sin_excepciones_bloqueantes", "Excepciones resueltas", reverse("bandeja_excepciones", args=[fecha]), "Resolver →"),
        ("arqueo_completo", "Arqueo de todas las cajas", reverse("arqueo_efectivo", args=[fecha]), "Resolver →"),
        ("diferencias_autorizadas", "Diferencias de caja autorizadas", reverse("arqueo_efectivo", args=[fecha]), "Resolver →"),
        ("deposito_registrado", "Depósito bancario", reverse("control_depositos"), "Ver →"),
        ("documentos_pendientes", "Documentos por emitir (guías y otros ingresos)", "#pendientes", "Asignar →"),
        ("perdidas_pendientes_de_visado", "Diferencias por visar", "#pendientes", "Visar →"),
    ]
    filas = []
    for clave, titulo, url, etiqueta in plan:
        item = checklist[clave]
        filas.append({
            "clave": clave, "titulo": titulo, "ok": item["ok"], "detalle": item["detalle"],
            "bloqueante": item.get("bloqueante", True), "url": url, "etiqueta": etiqueta,
        })
    return filas


def _contexto_del_cierre(dia, checklist: dict, dia_ya_aprobado: bool) -> dict:
    from nucleo.models import EventoAuditoria
    filas = _filas_del_checklist(dia, checklist)
    vigente = exportacion_vigente(dia) if dia_ya_aprobado else None
    reabierto = None
    if not dia_ya_aprobado and dia.exportaciones.filter(invalidada=True).exists():
        evento = EventoAuditoria.objects.filter(dia=dia, accion="reabrir").select_related("usuario").order_by("-id").first()
        if evento:
            reabierto = {"motivo": (evento.valor_posterior or {}).get("motivo", ""), "por": evento.usuario, "hora": evento.hora}
    return {
        "filas_checklist": filas,
        "faltan": [f for f in filas if f["bloqueante"] and not f["ok"]],
        "avisos": [f for f in filas if not f["bloqueante"] and not f["ok"]],
        "cumplidas": sum(1 for f in filas if f["ok"]),
        "total_verificaciones": len(filas),
        "dia_cerrado": dia_ya_aprobado,
        "exportacion_vigente": vigente,
        "motivo_bloqueo_archivos": motivo_de_bloqueo(dia) if dia_ya_aprobado and vigente is None else "",
        "reabierto": reabierto,
    }


def _reintentar_exportacion(request, dia):
    """Si el día ya está cerrado pero sus archivos estaban bloqueados, los genera apenas se resuelve el bloqueo."""
    if dia.estado in (DiaArqueo.Estado.APROBADO, DiaArqueo.Estado.CERRADO) and exportacion_vigente(dia) is None:
        exportacion, _ = intentar_generar_exportacion(dia, request.user)
        if exportacion is not None:
            messages.success(request, f"Archivos para Odoo generados (v{exportacion.version}).")


@requiere(Permiso.OPERAR)
@require_POST
def asignar_factura_venta_view(request, venta_id):
    from nucleo.models import Venta
    venta = get_object_or_404(Venta, pk=venta_id)
    fecha = venta.dia.fecha.isoformat()
    try:
        asignar_factura_a_venta(venta, request.POST.get("factura", ""), request.user)
        messages.success(request, f"Factura asignada a la comanda {venta.n_interno}.")
        _reintentar_exportacion(request, venta.dia)
    except AsignacionInvalida as exc:
        messages.error(request, str(exc))
    return redirect("cierre_y_exportacion", fecha=fecha)


@requiere(Permiso.OPERAR)
@require_POST
def asignar_factura_excepcion_view(request, excepcion_id):
    excepcion = get_object_or_404(Excepcion, pk=excepcion_id)
    fecha = excepcion.dia.fecha.isoformat()
    try:
        asignar_factura_a_excepcion(excepcion, request.POST.get("factura", ""), request.user)
        messages.success(request, "Boleta de otros ingresos registrada.")
        _reintentar_exportacion(request, excepcion.dia)
    except AsignacionInvalida as exc:
        messages.error(request, str(exc))
    return redirect("cierre_y_exportacion", fecha=fecha)


@requiere(Permiso.APROBAR_CIERRE)
@require_POST
def aprobar_dia_view(request, fecha):
    dia = get_object_or_404(DiaArqueo, fecha=date.fromisoformat(fecha))
    try:
        aprobar_y_cerrar_dia(dia, request.user, motivo_excepcion=request.POST.get("motivo_excepcion", ""))
    except CierreInvalido as exc:
        messages.error(request, str(exc))
        return redirect("cierre_y_exportacion", fecha=fecha)
    # Karla, 2026-09-25: los archivos se generan solos al aprobar. Si algo los bloquea, el cierre NO se
    # revierte: queda aprobado y se avisa el motivo (se generan apenas se resuelva, o desde Exportaciones).
    dia.refresh_from_db()
    exportacion, motivo = intentar_generar_exportacion(dia, request.user)
    if exportacion is not None:
        messages.success(request, f"Día aprobado y cerrado. Archivos para Odoo generados (v{exportacion.version}).")
    else:
        messages.warning(request, f"Día aprobado y cerrado, pero los archivos para Odoo no se pudieron generar todavía: {motivo}")
    return redirect("cierre_y_exportacion", fecha=fecha)


@requiere(Permiso.REABRIR_DIA)
@require_POST
def reabrir_dia_view(request, fecha):
    dia = get_object_or_404(DiaArqueo, fecha=date.fromisoformat(fecha))
    try:
        reabrir_dia(dia, request.user, request.POST.get("motivo", ""))
        messages.success(request, "Día reabierto. La exportación previa quedó invalidada.")
    except CierreInvalido as exc:
        messages.error(request, str(exc))
    return redirect("cierre_y_exportacion", fecha=fecha)


@requiere(Permiso.GENERAR_EXPORTACION)
@require_POST
def generar_exportacion_view(request, fecha):
    dia = get_object_or_404(DiaArqueo, fecha=date.fromisoformat(fecha))
    try:
        if exportacion_vigente(dia) is not None:
            messages.info(request, "Este día ya tiene sus archivos generados.")
        else:
            exportacion = generar_exportacion(dia, request.user)
            messages.success(request, f"Archivos para Odoo generados (v{exportacion.version}).")
    except ExportacionInvalida as exc:
        messages.error(request, str(exc))
    if request.POST.get("volver") == "exportaciones":
        return redirect("exportaciones_odoo")
    return redirect("cierre_y_exportacion", fecha=fecha)


def _autorizada_para_consulta(exportacion) -> bool:
    """Un archivo final está autorizado para consulta cuando lo generó el sistema con un día aprobado y sigue
    vigente (no fue invalidado al reabrir el día)."""
    return not exportacion.invalidada and exportacion.dia.estado in (
        DiaArqueo.Estado.APROBADO, DiaArqueo.Estado.CERRADO, DiaArqueo.Estado.EXPORTADO,
    )


def _entregar_de_exportacion(request, exportacion, campo: str, etiqueta: str):
    from pathlib import Path

    if es_de_lectura(request.user) and not _autorizada_para_consulta(exportacion):
        raise PermisoDenegado("Ese archivo no está autorizado para consulta.")
    ruta = Path(getattr(exportacion, campo)) if getattr(exportacion, campo) else None
    if not ruta or not ruta.exists():
        return archivo_no_disponible(
            request, f"El archivo de la versión {exportacion.version} ya no está en el servidor: reabre el día y vuelve a aprobarlo para generarlo de nuevo.",
        )
    return entregar_archivo(
        request, ruta, ruta.name, entidad="Exportacion", entidad_id=exportacion.pk, dia=exportacion.dia,
        tipo=etiqueta, detalle={"version": exportacion.version},
    )


@requiere(Permiso.DESCARGAR_GENERADOS)
def descargar_exportacion(request, exportacion_id):
    """Entrega la Captura Ventas de una versión ya generada (no genera nada)."""
    return _entregar_de_exportacion(request, get_object_or_404(Exportacion, pk=exportacion_id), "archivo", "captura_ventas")


@requiere(Permiso.DESCARGAR_GENERADOS)
def descargar_arqueo_caja(request, exportacion_id):
    """Entrega la Captura Arqueo Caja de una versión ya generada (no genera nada)."""
    return _entregar_de_exportacion(request, get_object_or_404(Exportacion, pk=exportacion_id), "archivo_arqueo", "captura_arqueo_caja")


@requiere(Permiso.CONSULTAR)
def dashboard_dia(request, fecha):
    dia = DiaArqueo.objects.filter(fecha=date.fromisoformat(fecha)).first()
    if dia is None:
        return _sin_dia(request, fecha)
    contexto = resumen_dia(dia)
    contexto["puede_aprobar_rol"] = tiene_permiso(request.user, Permiso.APROBAR_CIERRE)
    return render(request, "nucleo/dashboard_dia.html", contexto)


@requiere(Permiso.CONSULTAR)
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


@requiere(Permiso.CONSULTAR)
def calidad_registro(request):
    from datetime import timedelta
    hoy = date.today()
    fecha_hasta = date.fromisoformat(request.GET.get("hasta")) if request.GET.get("hasta") else hoy
    fecha_desde = date.fromisoformat(request.GET.get("desde")) if request.GET.get("desde") else fecha_hasta - timedelta(days=30)

    contexto = resumen_calidad(fecha_desde, fecha_hasta)
    contexto["fecha_desde"] = fecha_desde
    contexto["fecha_hasta"] = fecha_hasta
    return render(request, "nucleo/calidad_registro.html", contexto)


@requiere(Permiso.CONSULTAR)
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


@requiere(Permiso.CONSULTAR)
def reporte_ventas_producto(request):
    atajo = request.GET.get("atajo", "mes")
    if atajo == "personalizado" and request.GET.get("desde") and request.GET.get("hasta"):
        desde = date.fromisoformat(request.GET["desde"])
        hasta = date.fromisoformat(request.GET["hasta"])
    else:
        desde, hasta = rango_del_atajo(atajo)

    filas = resumen_por_producto(desde, hasta)
    return render(request, "nucleo/reporte_ventas_producto.html", {
        "desde": desde, "hasta": hasta, "atajo": atajo,
        "atajos": [
            ("mes", "Mes"), ("trimestre", "Trimestre"), ("anio_en_curso", "Año en curso"),
            ("12_meses", "12 meses"), ("personalizado", "Personalizado"),
        ],
        "filas": filas, "totales": totales_productos(filas),
    })


@requiere(Permiso.DESCARGAR_GENERADOS)
def descargar_reporte_ventas_producto(request):
    from django.http import FileResponse
    desde = date.fromisoformat(request.GET["desde"])
    hasta = date.fromisoformat(request.GET["hasta"])
    ruta = generar_excel_productos(desde, hasta)
    return entregar_archivo(request, ruta, ruta.name, entidad="Reporte", entidad_id="ventas_por_producto", tipo="reporte_ventas_por_producto",
                            detalle={"desde": desde.isoformat(), "hasta": hasta.isoformat()})


@requiere(Permiso.OPERAR)
def carga_de_archivos(request, fecha):
    dia, _ = DiaArqueo.objects.get_or_create(fecha=date.fromisoformat(fecha))
    contexto = {
        "dia": dia,
        "lote_pos": dia.lotes.filter(fuente=LoteCarga.Fuente.POS).order_by("-cargado_en").first(),
        "lote_tbk": dia.lotes.filter(fuente=LoteCarga.Fuente.TRANSBANK).order_by("-cargado_en").first(),
        "lote_mp": dia.lotes.filter(fuente=LoteCarga.Fuente.MERCADO_PAGO).order_by("-cargado_en").first(),
        "resumen_pos": {"comandas": dia.ventas.exclude(tipo_doc="DES").count(), "lineas": sum(v.lineas.count() for v in dia.ventas.all())},
        "resumen_tbk": {"movimientos": dia.movimientos.filter(proveedor="transbank").count()},
        "resumen_mp": {"movimientos": dia.movimientos.filter(proveedor="mercado_pago").count()},
        "validaciones": validaciones_de_carga(dia),
    }
    return render(request, "nucleo/carga_archivos.html", contexto)


@requiere(Permiso.OPERAR)
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


@requiere(Permiso.OPERAR)
@require_POST
def cargar_archivo_pos_view(request, fecha):
    dia, _ = DiaArqueo.objects.get_or_create(fecha=date.fromisoformat(fecha))
    archivo_ventas = request.FILES.get("archivo_ventas")
    archivo_libro_contable = request.FILES.get("archivo_libro_contable")
    if not archivo_ventas or not archivo_libro_contable:
        messages.error(request, "Selecciona los dos archivos: ventas por artículo y libro contable.")
        return redirect("carga_de_archivos", fecha=fecha)

    try:
        procesar_carga_pos(dia, archivo_ventas, archivo_libro_contable, request.user)
        messages.success(request, "Archivos cargados y procesados.")
    except (ArchivoInvalido, CargaDuplicada, CargaEnConflicto) as exc:
        messages.error(request, str(exc))
    return redirect("carga_de_archivos", fecha=fecha)


@requiere(Permiso.DESCARGAR_GENERADOS)
def descargar_archivo_original(request, lote_id):
    """Los archivos que se subieron (POS, Transbank, Mercado Pago) no son archivos generados: los perfiles de
    solo lectura no los descargan."""
    if es_de_lectura(request.user):
        raise PermisoDenegado("Los archivos originales cargados no están autorizados para consulta.")
    lote = get_object_or_404(LoteCarga, pk=lote_id)
    from pathlib import Path
    if request.GET.get("secundario") and lote.ruta_almacenamiento_secundario:
        ruta, nombre = Path(lote.ruta_almacenamiento_secundario), lote.nombre_archivo_secundario
    else:
        ruta, nombre = Path(lote.ruta_almacenamiento), lote.nombre_archivo
    if not ruta.exists():
        return archivo_no_disponible(request, "El archivo original ya no está en el servidor.", "carga_de_archivos" if False else "exportaciones_odoo")
    return entregar_archivo(request, ruta, nombre, entidad="LoteCarga", entidad_id=lote.pk, dia=lote.dia, tipo="archivo_original")


@login_required
def sin_perfil(request):
    """Cuenta sin perfil asignado: no opera ni consulta nada financiero."""
    return render(request, "nucleo/sin_perfil.html")
