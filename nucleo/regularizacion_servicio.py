"""Regularización de Documentos — Guías de Despacho (DES) y Notas de
Crédito (CRED) (Karla, 2026-09-22): estos documentos ya NO entran a la
carga masiva de ventas ni al arqueo automático del día — el archivo fuente
del libro contable nunca se altera, solo se deja de crear una `Venta` por
cada uno. Quedan aquí, aparte, para que la Encargada de Ingresos los
regularice a mano cuando corresponda (cuando se emite la factura de la
guía, o se identifica a qué documento afecta la nota de crédito), sin
perderlos.

Trazabilidad pedida: "Documento original → Documento relacionado → Forma
de pago → Arqueo afectado" — ese arqueo afectado es simplemente `dia`.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from django.core.files.uploadedfile import UploadedFile
from django.db import transaction

from nucleo.permisos import Permiso, exigir
from nucleo.archivos_seguros import nombre_seguro
from nucleo.comprobantes import ComprobanteInvalido, validar_comprobante
from nucleo.templatetags.formato import folio_corto
from nucleo.models import DiaArqueo, EventoAuditoria, PagoDeclarado, RegularizacionDocumento


class RegularizacionInvalida(Exception):
    pass


def _forma_pago_declarada_por_resto(doc) -> str:
    """Karla, 2026-09-22: "debe traer por defecto la forma de pago que
    traía Resto" — no hay que volver a escribirla a mano. Si el documento
    trae un solo medio (el caso normal), se usa ese; si viniera mezclado
    entre más de un medio, se deja en blanco para que la Encargada elija
    (no se puede "adivinar" cuál corresponde)."""
    medios = {p.medio for p in doc.pagos}
    return medios.pop() if len(medios) == 1 else ""


@transaction.atomic
def crear_desde_libro(dia: DiaArqueo, documentos, usuario) -> list[RegularizacionDocumento]:
    """Recibe `resultado.guias + resultado.notas_credito` (dataclasses
    `DocumentoLibro` del importador) y crea un `RegularizacionDocumento`
    Pendiente por cada uno — nunca una `Venta`. Idempotente por
    (dia, tipo, n_interno), igual que antes se evitaba duplicar la guía."""
    creados = []
    for doc in documentos:
        if RegularizacionDocumento.objects.filter(dia=dia, tipo=doc.tipo, n_interno=doc.n_interno).exists():
            continue
        creados.append(RegularizacionDocumento.objects.create(
            dia=dia, tipo=doc.tipo, n_interno=doc.n_interno, folio=doc.folio,
            codigo_original=getattr(doc, "codigo_original", "") or doc.tipo,
            caja=doc.caja, monto=doc.total, forma_pago=_forma_pago_declarada_por_resto(doc),
        ))
    return creados


MARGEN_HORARIO = timedelta(minutes=10)
MOTIVO_AJUSTE_SENCILLO = "ajuste_sencillo"


def ventana_horaria_de_guia(guia: RegularizacionDocumento):
    """Hora aproximada en que se cobró una guía (Karla, 2026-09-24: "según el
    horario... identifiquemos que es un pago por una guía"). El libro contable
    no trae la hora de la guía, pero su N_INTERNO es correlativo en el tiempo:
    queda entre el de la comanda anterior y el de la siguiente, que sí traen
    hora de cierre. Devuelve (desde, hasta) como datetime, o None si no hay
    comandas a ambos lados para acotarla."""
    from nucleo.models import Venta

    ventas = Venta.objects.filter(dia=guia.dia).exclude(hora_cierre=None)
    anterior = ventas.filter(n_interno__lt=guia.n_interno).order_by("-n_interno").first()
    siguiente = ventas.filter(n_interno__gt=guia.n_interno).order_by("n_interno").first()
    if anterior is None or siguiente is None:
        return None
    desde = datetime.combine(guia.dia.fecha, min(anterior.hora_cierre, siguiente.hora_cierre)) - MARGEN_HORARIO
    hasta = datetime.combine(guia.dia.fecha, max(anterior.hora_cierre, siguiente.hora_cierre)) + MARGEN_HORARIO
    return desde, hasta


def calzar_guias_con_cobros(dia: DiaArqueo, resultado, movimientos_por_id_db, usuario=None) -> int:
    """Karla, 2026-09-23/24: una guía pagada con tarjeta no es una comanda, así
    que su cobro de Transbank/Mercado Pago llega al motor "sin pago" y salía
    como excepción. Se calza aquí contra esas excepciones y el vínculo queda
    como el cotejo validado de la guía (`RegularizacionDocumento.movimiento`).

    Reglas (solo cuando la evidencia es única, nunca a la fuerza):
    * mismo medio (débito/crédito; prepago cuenta como débito);
    * monto igual, o con una diferencia de hasta `tolerancia_guias` ($100 por
      defecto) — y entonces el horario del cobro debe ser compatible con el
      de la guía (ver `ventana_horaria_de_guia`);
    * si hay varios candidatos, el horario decide; si aún no es único, queda
      como excepción para decidir a mano.
    La diferencia, si la hay, se registra como ajuste a la cuenta de pérdida o
    ganancia según el signo (como el ajuste de sencillo). Se recalcula en cada
    conciliación."""
    from nucleo.conciliacion_servicio import _registrar_ajuste_guia
    from nucleo.umbrales import umbral

    RegularizacionDocumento.objects.filter(dia=dia).exclude(movimiento=None).update(movimiento=None)
    tolerancia = umbral("tolerancia_guias")

    guias = list(RegularizacionDocumento.objects.filter(
        dia=dia, tipo=RegularizacionDocumento.Tipo.GUIA,
        forma_pago__in=[PagoDeclarado.Medio.DEBITO, PagoDeclarado.Medio.CREDITO],
    ))
    candidatas = [
        e for e in resultado.excepciones
        if e.tipo in ("movimiento_sin_pago", "pago_duplicado") and len(e.movimientos) == 1 and not e.pagos
    ]

    def _compatible(guia, mov) -> bool:
        return mov.medio == guia.forma_pago or (guia.forma_pago == PagoDeclarado.Medio.DEBITO and mov.medio == "prepago")

    elegidas: dict[int, tuple] = {}  # guia.pk -> (excepcion, movimiento, diferencia)
    for guia in guias:
        opciones = []
        for e in candidatas:
            mov = movimientos_por_id_db[e.movimientos[0]]
            diferencia = abs(guia.monto - mov.monto_venta_valido_abono)
            if _compatible(guia, mov) and diferencia <= tolerancia:
                opciones.append((e, mov, diferencia))
        ventana = ventana_horaria_de_guia(guia) if opciones else None
        if ventana:
            en_horario = [o for o in opciones if ventana[0] <= o[1].fecha_hora <= ventana[1]]
            # Fuera de horario solo se acepta un cobro de monto exacto.
            opciones = en_horario or [o for o in opciones if o[2] == 0]
        if opciones:
            mejor = min(o[2] for o in opciones)
            opciones = [o for o in opciones if o[2] == mejor]
        if len(opciones) == 1:
            elegidas[guia.pk] = opciones[0]

    usados = [mov.pk for _, mov, _ in elegidas.values()]
    calzadas = 0
    for guia in guias:
        if guia.pk not in elegidas:
            continue
        excepcion, movimiento, diferencia = elegidas[guia.pk]
        if usados.count(movimiento.pk) > 1:
            continue  # dos guías reclaman el mismo cobro: no se adivina
        guia.movimiento = movimiento
        guia.save(update_fields=["movimiento"])
        resultado.excepciones.remove(excepcion)
        candidatas.remove(excepcion)
        if diferencia:
            _registrar_ajuste_guia(dia, usuario, guia, movimiento, tolerancia)
        calzadas += 1
    return calzadas


def efectivo_documentos_por_caja(dia: DiaArqueo) -> dict[str, Decimal]:
    """Efecto en el efectivo de cada caja de los documentos de Regularización.

    * Guía de despacho (DES) pagada en efectivo: el dinero ENTRÓ → suma
      (Karla, 2026-09-23).
    * Nota de crédito (CRED/CRE): NO resta por sí sola. Resto ya trae la nota
      descontada del efectivo en su cierre de caja (y el libro de ventas sale
      neto), así que restarla de nuevo la descontaría dos veces (Karla,
      2026-09-24, caso real 07-09 caja 3). Solo resta una nota verificada con
      evidencia como devuelta en efectivo de la caja.
    * Sin clasificar: ningún efecto.

    Nunca en silencio: ver `avisos_efectivo_guias`."""
    totales: dict[str, Decimal] = {}
    guias = RegularizacionDocumento.objects.filter(
        dia=dia, tipo=RegularizacionDocumento.Tipo.GUIA, forma_pago=PagoDeclarado.Medio.EFECTIVO,
    ).exclude(caja="")
    for doc in guias:
        totales[doc.caja] = totales.get(doc.caja, Decimal(0)) + doc.monto
    return totales


def notas_que_restan(dia: DiaArqueo):
    """Ninguna (Karla, 2026-09-25): una nota de crédito NUNCA cambia el efectivo esperado de la caja,
    ni siquiera verificada como «devuelta en efectivo». Resto ya trae la nota descontada de las ventas
    y de su cierre de caja, así que restarla otra vez la descuenta dos veces. Caso real: al verificar
    las dos notas del 07-09 como efectivo, la caja 3 mostró un sobrante falso de $53.102 (y una
    ganancia atribuida a la cajera en el asiento). La verificación es solo el respaldo documental."""
    return RegularizacionDocumento.objects.none()


def documentos_efectivo_de_caja(dia: DiaArqueo, caja: str):
    """Los documentos que mueven el efectivo de una caja, para mostrarlos
    junto a su arqueo ("dejar estas guías relacionadas con la caja")."""
    return RegularizacionDocumento.objects.filter(
        dia=dia, caja=caja, forma_pago=PagoDeclarado.Medio.EFECTIVO,
        tipo__in=[RegularizacionDocumento.Tipo.GUIA, RegularizacionDocumento.Tipo.NOTA_CREDITO],
    ).order_by("tipo", "folio")


def avisos_efectivo_guias(dia: DiaArqueo) -> list[str]:
    """Casos que no se pueden sumar o restar solos — quedan visibles para que
    alguien los revise a mano, en vez de perderse."""
    avisos = []
    for doc in RegularizacionDocumento.objects.filter(
        dia=dia, tipo=RegularizacionDocumento.Tipo.GUIA, forma_pago=PagoDeclarado.Medio.EFECTIVO, caja="",
    ):
        avisos.append(
            f"Guía {folio_corto(doc.folio)} (${doc.monto:,.0f}, efectivo) sin caja asignada en el libro contable — "
            "no se sumó a ningún arqueo, revísala a mano."
        )
    for doc in RegularizacionDocumento.objects.filter(dia=dia, tipo=RegularizacionDocumento.Tipo.GUIA, forma_pago=""):
        avisos.append(
            f"Guía {folio_corto(doc.folio)} (${doc.monto:,.0f}): Resto declaró más de un medio de pago — "
            "revisa si parte es efectivo antes de cuadrar la caja."
        )
    for doc in RegularizacionDocumento.objects.filter(
        dia=dia, tipo=RegularizacionDocumento.Tipo.NOTA_CREDITO, caja="",
    ):
        avisos.append(
            f"Nota de crédito {folio_corto(doc.folio)} (${doc.monto:,.0f}) sin caja asignada en el libro contable — "
            "no se descontó de ningún arqueo, revísala a mano."
        )
    from nucleo.notas_credito_servicio import notas_sin_evidencia
    for doc in notas_sin_evidencia(dia).exclude(caja=""):
        avisos.append(
            f"Nota de crédito {folio_corto(doc.folio)} (${doc.monto:,.0f}, {doc.caja}): falta el respaldo de la devolución (vale "
            "firmado y boleta original) y bloquea el cierre del día. No se descuenta del efectivo: el cierre de Resto "
            "ya la trae descontada."
        )
    for doc in RegularizacionDocumento.objects.filter(dia=dia, tipo=RegularizacionDocumento.Tipo.SIN_CLASIFICAR):
        avisos.append(
            f"Documento con código {doc.codigo_original!r} (folio {doc.folio}, ${doc.monto:,.0f}) sin clasificar — "
            "no tiene ningún efecto en caja ni en ventas hasta que se clasifique en Regularización de Documentos."
        )
    return avisos


def documentos_sin_clasificar(dia: DiaArqueo | None = None):
    qs = RegularizacionDocumento.objects.filter(
        tipo=RegularizacionDocumento.Tipo.SIN_CLASIFICAR, estado=RegularizacionDocumento.Estado.PENDIENTE,
    )
    return qs.filter(dia=dia) if dia is not None else qs


@transaction.atomic
def clasificar_documento(documento: RegularizacionDocumento, *, tipo: str, motivo: str, usuario) -> RegularizacionDocumento:
    """Clasifica un documento de código desconocido (Karla, 2026-09-24). El
    código original NO se toca; la clasificación queda con quién, cuándo y
    por qué. Solo el Jefe de Contabilidad o superior. Por ahora solo puede
    ser guía de despacho o nota de crédito; otros códigos requieren
    definirse antes en el mantenedor de códigos (pendiente)."""
    exigir(usuario, Permiso.VALIDAR, "Tu perfil no puede clasificar documentos.")
    from nucleo.documentos_servicio import nivel_usuario
    from django.utils import timezone

    if documento.tipo != RegularizacionDocumento.Tipo.SIN_CLASIFICAR:
        raise RegularizacionInvalida("Este documento ya está clasificado.")
    if nivel_usuario(usuario) < 1:
        raise RegularizacionInvalida("Solo el Jefe de Contabilidad o un rol superior puede clasificar un documento.")
    if tipo not in (RegularizacionDocumento.Tipo.GUIA, RegularizacionDocumento.Tipo.NOTA_CREDITO):
        raise RegularizacionInvalida("Elige si es una guía de despacho o una nota de crédito.")
    if not motivo.strip():
        raise RegularizacionInvalida("Indica el motivo o la evidencia de la clasificación (ej. el PDF del documento en el SII).")

    anterior = {"tipo": documento.tipo, "codigo_original": documento.codigo_original}
    documento.tipo = tipo
    documento.clasificado_por = usuario
    documento.clasificado_en = timezone.now()
    documento.motivo_clasificacion = motivo.strip()
    documento.save(update_fields=["tipo", "clasificado_por", "clasificado_en", "motivo_clasificacion"])
    EventoAuditoria.objects.create(
        dia=documento.dia, entidad="RegularizacionDocumento", entidad_id=str(documento.pk),
        accion="clasificar_documento", valor_anterior=anterior,
        valor_posterior={"tipo": tipo, "codigo_original": documento.codigo_original, "motivo": motivo.strip()},
        usuario=usuario,
    )
    return documento


def documentos_pendientes(dia: DiaArqueo | None = None):
    qs = RegularizacionDocumento.objects.filter(estado=RegularizacionDocumento.Estado.PENDIENTE)
    if dia is not None:
        qs = qs.filter(dia=dia)
    return qs.select_related("dia").order_by("dia__fecha", "tipo", "folio")


def documentos_regularizados(dia: DiaArqueo | None = None):
    qs = RegularizacionDocumento.objects.filter(estado=RegularizacionDocumento.Estado.REGULARIZADO)
    if dia is not None:
        qs = qs.filter(dia=dia)
    return qs.select_related("dia", "regularizado_por").order_by("-regularizado_en")


def _guardar_comprobante(archivo: UploadedFile, documento: RegularizacionDocumento):
    from datetime import datetime
    from pathlib import Path

    from django.conf import settings

    carpeta = Path(settings.BASE_DIR) / "archivos_cargados" / "comprobantes_regularizacion" / documento.dia.fecha.isoformat()
    carpeta.mkdir(parents=True, exist_ok=True)
    marca = datetime.now().strftime("%Y%m%d%H%M%S")
    ruta = carpeta / f"{marca}_{nombre_seguro(archivo.name)}"
    with open(ruta, "wb") as destino:
        for parte in archivo.chunks():
            destino.write(parte)
    return ruta


@transaction.atomic
def regularizar(
    documento: RegularizacionDocumento, *, documento_relacionado: str,
    fecha_documento_relacionado: date | None, referencia: str,
    comprobante: UploadedFile | None, usuario,
) -> RegularizacionDocumento:
    """Karla, 2026-09-22 (segunda vuelta): la forma de pago NO se puede
    cambiar acá — debe ser exactamente la que Resto ya declaró para este
    documento (`crear_desde_libro` la dejó en `documento.forma_pago`), no
    una que la Encargada elija a mano. Si viene vacía (medios mezclados,
    caso raro) hay que corregirlo en el origen, no adivinar cuál puso."""
    exigir(usuario, Permiso.OPERAR, "Tu perfil no puede regularizar documentos.")
    if documento.estado == RegularizacionDocumento.Estado.REGULARIZADO:
        raise RegularizacionInvalida("Este documento ya fue regularizado.")
    documento_relacionado = documento_relacionado.strip()
    if not documento_relacionado:
        campo = "N° de factura" if documento.tipo == RegularizacionDocumento.Tipo.GUIA else "documento afectado"
        raise RegularizacionInvalida(f"Indica el {campo} asociado.")
    if documento.forma_pago not in PagoDeclarado.Medio.values:
        raise RegularizacionInvalida(
            "Este documento no trae una forma de pago única declarada por Resto — "
            "corrígelo en el libro contable antes de regularizarlo."
        )
    if not comprobante:
        raise RegularizacionInvalida(
            "Adjunta el comprobante del pago de la factura que quedó en la contabilidad — es obligatorio."
        )
    try:
        validar_comprobante(comprobante)
    except ComprobanteInvalido as exc:
        raise RegularizacionInvalida(str(exc))

    from django.utils import timezone
    anterior = {"estado": documento.estado}
    documento.documento_relacionado = documento_relacionado
    documento.fecha_documento_relacionado = fecha_documento_relacionado
    documento.referencia = referencia.strip()
    documento.comprobante_archivo = str(_guardar_comprobante(comprobante, documento))
    documento.comprobante_nombre_original = comprobante.name
    documento.estado = RegularizacionDocumento.Estado.REGULARIZADO
    documento.regularizado_por = usuario
    documento.regularizado_en = timezone.now()
    documento.save(update_fields=[
        "documento_relacionado", "fecha_documento_relacionado", "referencia",
        "comprobante_archivo", "comprobante_nombre_original",
        "estado", "regularizado_por", "regularizado_en",
    ])
    EventoAuditoria.objects.create(
        dia=documento.dia, entidad="RegularizacionDocumento", entidad_id=str(documento.pk),
        accion="regularizar_documento", valor_anterior=anterior,
        valor_posterior={
            "estado": documento.estado, "documento_relacionado": documento.documento_relacionado,
            "forma_pago": documento.forma_pago,
        },
        usuario=usuario,
    )
    return documento
