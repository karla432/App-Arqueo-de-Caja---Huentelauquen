from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from nucleo.conciliacion_servicio import ejecutar_conciliacion
from nucleo.models import DiaArqueo, Excepcion, MotivoResolucion, MovimientoProveedor
from nucleo.resolucion_servicio import ResolucionInvalida, resolver_excepcion


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
