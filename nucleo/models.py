"""Modelo de datos completo del Arqueo Diario (diseño funcional §7).

Convención de dinero: CLP no tiene centavos, así que todo monto es
DecimalField con decimal_places=0 — nunca float, para no arrastrar error
de redondeo en cifras que después se auditan una a una.
"""
from django.conf import settings
from django.db import models


def dinero(**kwargs):
    return models.DecimalField(max_digits=14, decimal_places=0, **kwargs)


class DiaArqueo(models.Model):
    """Día de arqueo. Estados con transición estricta (diseño funcional §6):
    sin retroceso silencioso — reabrir invalida la exportación previa."""

    class Estado(models.TextChoices):
        SIN_CARGAR = "sin_cargar", "Sin cargar"
        CARGADO = "cargado", "Cargado"
        NORMALIZADO = "normalizado", "Normalizado"
        CONCILIADO_CON_EXCEPCIONES = "conciliado_con_excepciones", "Conciliado con excepciones"
        EXCEPCIONES_RESUELTAS = "excepciones_resueltas", "Excepciones resueltas"
        APROBADO = "aprobado", "Aprobado"
        CERRADO = "cerrado", "Cerrado"
        EXPORTADO = "exportado", "Exportado"

    fecha = models.DateField(unique=True)
    estado = models.CharField(max_length=32, choices=Estado.choices, default=Estado.SIN_CARGAR)
    operado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        related_name="dias_operados", on_delete=models.PROTECT,
    )
    aprobado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        related_name="dias_aprobados", on_delete=models.PROTECT,
    )
    aprobado_en = models.DateTimeField(null=True, blank=True)
    cerrado_en = models.DateTimeField(null=True, blank=True)
    creado_en = models.DateTimeField(auto_now_add=True)
    actualizado_en = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Día {self.fecha} ({self.get_estado_display()})"


class LoteCarga(models.Model):
    """Un archivo subido. El original es inmutable — nunca se sobrescribe,
    una recarga crea un lote nuevo con versión incremental (control C6)."""

    class Fuente(models.TextChoices):
        POS = "pos", "POS"
        TRANSBANK = "transbank", "Transbank"
        MERCADO_PAGO = "mercado_pago", "Mercado Pago"
        BANCO = "banco", "Cartola bancaria"

    dia = models.ForeignKey(DiaArqueo, related_name="lotes", on_delete=models.PROTECT)
    fuente = models.CharField(max_length=20, choices=Fuente.choices)
    nombre_archivo = models.CharField(max_length=255)
    huella_sha256 = models.CharField(max_length=64)
    ruta_almacenamiento = models.CharField(
        max_length=500,
        help_text="Ruta fuera de cualquier directorio servido al navegador (control #14).",
    )
    fecha_declarada = models.DateField()
    version = models.PositiveIntegerField(default=1)
    cargado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    cargado_en = models.DateTimeField(auto_now_add=True)
    avisos = models.JSONField(
        default=list, blank=True,
        help_text="Avisos de calidad detectados al importar (no bloqueantes) — regla de conservación §8.",
    )

    class Meta:
        ordering = ["-cargado_en"]

    def __str__(self) -> str:
        return f"{self.fuente} · {self.nombre_archivo} v{self.version}"


class Venta(models.Model):
    """Cabecera de comanda. Clave de negocio: N_INTERNO (único dentro del
    día — diseño funcional §26, supuesto)."""

    dia = models.ForeignKey(DiaArqueo, related_name="ventas", on_delete=models.PROTECT)
    lote = models.ForeignKey(LoteCarga, related_name="ventas", on_delete=models.PROTECT)
    n_interno = models.PositiveIntegerField()
    nro_comprobante = models.CharField(max_length=50)
    tipo_doc = models.CharField(max_length=10)
    nro_comanda = models.CharField(max_length=50, blank=True)
    estado = models.CharField(max_length=30)
    caja = models.CharField(max_length=30)
    turno = models.CharField(max_length=50, blank=True)
    hora_apertura = models.TimeField(null=True, blank=True)
    hora_cierre = models.TimeField(null=True, blank=True)
    total_comanda = dinero()
    cliente = models.CharField(max_length=120, blank=True)
    sucursal = models.CharField(max_length=120, blank=True)
    factura_asociada = models.CharField(
        max_length=50, blank=True,
        help_text="Para ventas con guía de despacho (comprobante 'GO...'): número de la "
                   "factura que el Analista de Ingresos emite al cierre, no el día de la guía.",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["dia", "n_interno"], name="venta_unica_por_dia"),
        ]

    def __str__(self) -> str:
        return f"Comanda {self.n_interno} · {self.dia.fecha}"

    @property
    def es_guia_de_despacho(self) -> bool:
        """Confirmado con Karla (2026-09-06): el POS marca estas ventas con
        el comprobante partiendo en 'GO', no con un TIPO_DOC distinto."""
        return self.nro_comprobante.upper().startswith("GO")


class LineaVenta(models.Model):
    venta = models.ForeignKey(Venta, related_name="lineas", on_delete=models.CASCADE)
    articulo = models.CharField(max_length=200)
    cantidad = models.DecimalField(max_digits=10, decimal_places=2)
    precio_unitario = dinero()
    total_linea = dinero()


class PagoDeclarado(models.Model):
    """Un registro por medio con monto > 0 (decisión estructural del
    modelo — diseño funcional §7: permite conciliar una comanda mixta
    parcialmente)."""

    class Medio(models.TextChoices):
        EFECTIVO = "efectivo", "Efectivo"
        TRANSFERENCIA = "transferencia", "Transferencia"
        DEBITO = "debito", "Débito"
        CREDITO = "credito", "Crédito"

    venta = models.ForeignKey(Venta, related_name="pagos", on_delete=models.CASCADE)
    medio = models.CharField(max_length=20, choices=Medio.choices)
    monto = dinero()
    # Reclasificación de medio: siempre manual, nunca la aplica el motor
    # solo (control C3). Un pago reclasificado conserva el medio
    # declarado original y agrega el aprobado.
    medio_aprobado = models.CharField(max_length=20, choices=Medio.choices, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["venta", "medio"], name="un_pago_por_medio_y_venta"),
        ]


class MovimientoProveedor(models.Model):
    """Movimiento normalizado de un adquirente. El motor de conciliación
    solo ve esto — nunca el nombre del proveedor en sus reglas (control
    #11): ese dato vive aquí, como dato, no como lógica."""

    dia = models.ForeignKey(DiaArqueo, related_name="movimientos", on_delete=models.PROTECT)
    lote = models.ForeignKey(LoteCarga, related_name="movimientos", on_delete=models.PROTECT)
    proveedor = models.CharField(max_length=30)
    tipo = models.CharField(max_length=30)
    estado = models.CharField(max_length=30)
    fecha_hora = models.DateTimeField()
    fecha_abono = models.CharField(max_length=30, blank=True)
    medio = models.CharField(max_length=20)
    tipo_tarjeta = models.CharField(max_length=20)
    numero_tarjeta = models.CharField(
        max_length=30, blank=True,
        help_text="Tal como llega del adquirente, ya enmascarado. Nunca un PAN completo (control #13).",
    )
    monto_original = dinero()
    monto_afecto = dinero(default=0)
    monto_exento = dinero(default=0)
    monto_vuelto = dinero(default=0)
    propina = dinero(default=0)
    monto_venta_valido_abono = dinero()
    monto_anulado = dinero(default=0)
    monto_cuota = dinero(default=0)
    autorizacion = models.CharField(max_length=30, blank=True)
    terminal = models.CharField(max_length=30, blank=True)
    moneda = models.CharField(max_length=10, blank=True)

    def __str__(self) -> str:
        return f"{self.proveedor} · {self.fecha_hora} · ${self.monto_venta_valido_abono}"


class VersionMotor(models.Model):
    """Fija los parámetros de una corrida de conciliación (control C11):
    un día conciliado hace seis meses debe poder explicarse con las reglas
    de entonces, no con las de hoy."""

    dia = models.ForeignKey(DiaArqueo, related_name="corridas_motor", on_delete=models.PROTECT)
    version_motor = models.CharField(max_length=20)
    parametros = models.JSONField(
        help_text="Snapshot: tolerancia de monto, ventana temporal, score mínimo, etc.",
    )
    ejecutado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    ejecutado_en = models.DateTimeField(auto_now_add=True)


class Coincidencia(models.Model):
    """Vínculo pago↔movimiento resuelto por el motor. M2M en ambos lados
    para representar también el nivel 6 (agrupación: varios↔uno)."""

    class Origen(models.TextChoices):
        AUTOMATICO = "automatico", "Automático"
        MANUAL = "manual", "Manual"

    dia = models.ForeignKey(DiaArqueo, related_name="coincidencias", on_delete=models.PROTECT)
    corrida = models.ForeignKey(VersionMotor, related_name="coincidencias", on_delete=models.PROTECT)
    pagos = models.ManyToManyField(PagoDeclarado, related_name="coincidencias")
    movimientos = models.ManyToManyField(MovimientoProveedor, related_name="coincidencias")
    nivel = models.PositiveSmallIntegerField()
    score = models.PositiveSmallIntegerField()
    diferencia_monto = dinero(default=0)
    diferencia_hora_segundos = models.IntegerField(default=0)
    origen = models.CharField(max_length=12, choices=Origen.choices)
    creado_en = models.DateTimeField(auto_now_add=True)


class MotivoResolucion(models.Model):
    """Lista cerrada de motivos (D9, aprobada por Karla). Sin esto el
    histórico queda en texto libre y no es analizable."""

    class AplicaA(models.TextChoices):
        EXCEPCION = "excepcion", "Resolución de excepción"
        DIFERENCIA_CAJA = "diferencia_caja", "Autorización de diferencia de caja"

    codigo = models.SlugField(max_length=60, unique=True)
    texto = models.CharField(max_length=200)
    aplica_a = models.CharField(max_length=20, choices=AplicaA.choices)
    activo = models.BooleanField(default=True)

    def __str__(self) -> str:
        return self.texto


class Excepcion(models.Model):
    class Tipo(models.TextChoices):
        PAGO_SIN_MOVIMIENTO = "pago_sin_movimiento", "Pago sin movimiento"
        MOVIMIENTO_SIN_PAGO = "movimiento_sin_pago", "Movimiento sin pago"
        DIFERENCIA_MONTO = "diferencia_monto", "Diferencia de monto"
        MEDIO_CONTRADICTORIO = "medio_contradictorio", "Medio contradictorio"
        AMBIGUEDAD = "ambiguedad", "Ambigüedad"
        AGRUPACION = "agrupacion", "Agrupación"
        ANULACION_DESCALZADA = "anulacion_descalzada", "Anulación descalzada"
        CALIDAD_REGISTRO = "calidad_registro", "Calidad de registro"

    class Resolucion(models.TextChoices):
        ACEPTAR = "aceptar", "Aceptar"
        RECHAZAR = "rechazar", "Rechazar"
        VINCULAR_OTRO = "vincular_otro", "Vincular otro movimiento"

    dia = models.ForeignKey(DiaArqueo, related_name="excepciones", on_delete=models.PROTECT)
    tipo = models.CharField(max_length=30, choices=Tipo.choices)
    monto_en_juego = dinero(default=0)
    pago = models.ForeignKey(PagoDeclarado, null=True, blank=True, on_delete=models.PROTECT)
    movimiento = models.ForeignKey(MovimientoProveedor, null=True, blank=True, on_delete=models.PROTECT)
    decision_propuesta = models.TextField(blank=True)
    abierta = models.BooleanField(default=True)
    resolucion = models.CharField(max_length=20, choices=Resolucion.choices, blank=True)
    motivo = models.ForeignKey(MotivoResolucion, null=True, blank=True, on_delete=models.PROTECT)
    comentario = models.TextField(blank=True)
    resuelta_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
        related_name="excepciones_resueltas",
    )
    resuelta_en = models.DateTimeField(null=True, blank=True)
    creada_en = models.DateTimeField(auto_now_add=True)
    factura_asociada = models.CharField(
        max_length=50, blank=True,
        help_text="Para 'movimiento sin pago' aceptado como otros ingresos: número de la "
                   "boleta que el Analista de Ingresos emite para poder exportarlo a Odoo.",
    )

    class Meta:
        ordering = ["-monto_en_juego"]  # bandeja ordenada por monto en juego, no por aparición


UMBRAL_DIFERENCIA_CAJA = 5000  # $ — decisión D3, confirmada con Karla 2026-09-05


class ArqueoEfectivo(models.Model):
    dia = models.ForeignKey(DiaArqueo, related_name="arqueos_caja", on_delete=models.PROTECT)
    caja = models.CharField(max_length=30)
    teorico = dinero(default=0)
    ajustes = dinero(default=0)
    esperado = dinero(default=0)
    fondo_inicial = dinero(default=0)
    gastos_autorizados = dinero(default=0)
    contado = dinero(null=True, blank=True)
    diferencia = dinero(null=True, blank=True)
    autorizado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
    )
    motivo_autorizacion = models.ForeignKey(MotivoResolucion, null=True, blank=True, on_delete=models.PROTECT)
    comentario = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["dia", "caja"], name="un_arqueo_por_caja_y_dia"),
        ]

    @property
    def estado(self) -> str:
        """Calculado, no guardado — evita que quede desincronizado del
        umbral o de una autorización nueva."""
        if self.contado is None:
            return "pendiente"
        if abs(self.diferencia) <= UMBRAL_DIFERENCIA_CAJA:
            return "cuadrada" if self.diferencia == 0 else "dentro_de_umbral"
        return "autorizada" if self.autorizado_por_id else "requiere_autorizacion"


class ConteoDenominacion(models.Model):
    """Detalle por denominación (D8: Karla eligió conteo detallado, no
    solo total por caja)."""

    arqueo = models.ForeignKey(ArqueoEfectivo, related_name="denominaciones", on_delete=models.CASCADE)
    denominacion = dinero()  # ej. 20000, 10000, 5000, 1000, 500, 100, 50, 10
    cantidad = models.PositiveIntegerField()

    @property
    def subtotal(self):
        return self.denominacion * self.cantidad


class Deposito(models.Model):
    dia = models.OneToOneField(DiaArqueo, related_name="deposito", on_delete=models.PROTECT)
    monto = dinero()
    comprobante = models.CharField(max_length=100)
    fecha = models.DateField()
    fondo_retenido = dinero(default=0)

    @property
    def diferencia_deposito(self):
        total_contado = sum(
            a.contado for a in self.dia.arqueos_caja.all() if a.contado is not None
        )
        return self.monto - total_contado - self.fondo_retenido


class Exportacion(models.Model):
    class AcuseEstado(models.TextChoices):
        PENDIENTE = "pendiente", "Pendiente"
        IMPORTADO = "importado", "Importado"
        IMPORTADO_CON_OBS = "importado_con_obs", "Importado con observaciones"
        RECHAZADO = "rechazado", "Rechazado"

    dia = models.ForeignKey(DiaArqueo, related_name="exportaciones", on_delete=models.PROTECT)
    version = models.PositiveIntegerField()
    archivo = models.CharField(max_length=500)
    generado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    generado_en = models.DateTimeField(auto_now_add=True)
    invalidada = models.BooleanField(default=False)  # true si el día se reabrió después
    acuse_estado = models.CharField(max_length=20, choices=AcuseEstado.choices, default=AcuseEstado.PENDIENTE)
    acuse_detalle = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["dia", "version"], name="version_unica_por_dia"),
        ]


class EventoAuditoria(models.Model):
    """Bitácora inmutable (control C7). No se expone ningún método de
    edición o borrado a nivel de aplicación — un evento se crea y nunca
    se toca."""

    dia = models.ForeignKey(
        DiaArqueo, null=True, blank=True, related_name="eventos_auditoria", on_delete=models.PROTECT,
    )
    entidad = models.CharField(max_length=60)
    entidad_id = models.CharField(max_length=60)
    accion = models.CharField(max_length=60)
    valor_anterior = models.JSONField(null=True, blank=True)
    valor_posterior = models.JSONField(null=True, blank=True)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    origen = models.CharField(max_length=100, blank=True)
    hora = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-hora"]

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError("La bitácora de auditoría no se edita — solo se crean eventos nuevos.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("La bitácora de auditoría no se puede borrar.")
