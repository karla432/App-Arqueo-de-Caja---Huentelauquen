"""Modelo de datos completo del Arqueo Diario (diseño funcional §7).

Convención de dinero: CLP no tiene centavos, así que todo monto es
DecimalField con decimal_places=0 — nunca float, para no arrastrar error
de redondeo en cifras que después se auditan una a una.
"""
import hashlib
import json

from django.conf import settings
from django.db import models
from django.utils import timezone


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
    # Solo se usan para POS cuando Resto entrega VENTAS POR ARTICULO sin
    # N_INTERNO y hace falta el libro contable para recuperarlo (Karla,
    # 2026-09-16) — reemplaza la consolidación manual que hacía antes.
    # Vacíos para Transbank, Mercado Pago y para una carga de POS con el
    # archivo ya consolidado (un solo archivo).
    nombre_archivo_secundario = models.CharField(max_length=255, blank=True)
    huella_sha256_secundario = models.CharField(max_length=64, blank=True)
    ruta_almacenamiento_secundario = models.CharField(max_length=500, blank=True)

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
        """Una guía es un documento DES del libro contable (Karla,
        2026-09-21) — su número parte con el prefijo G05205000, es decir "G"
        y un cero. Antes (2026-09-06) se reconocía por "GO" (letra O), que
        no coincide con los datos reales; se aceptan ambas grafías."""
        return self.tipo_doc == "DES" or self.nro_comprobante.upper().startswith(("G0", "GO"))


class LineaVenta(models.Model):
    venta = models.ForeignKey(Venta, related_name="lineas", on_delete=models.CASCADE)
    articulo = models.CharField(max_length=200)
    # 3 decimales, no 2 (Karla, 2026-09-21): un artículo vendido por peso
    # (ej. "QUESO PIEZA") trae la cantidad con 3 decimales en el archivo de
    # Resto — con 2 se pierde precisión y Odoo, que recalcula cada línea
    # como Cantidad × Precio unitario (no usa nuestro total ya calculado),
    # descuadra la boleta. Caso real: comanda 654551, 3,284 kg × $11.900 →
    # con 2 decimales (3,28) da $39.032 en vez de los $39.080 reales.
    cantidad = models.DecimalField(max_digits=10, decimal_places=3)
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


class Reclasificacion(models.Model):
    """Corrección de la forma de pago de una nota de venta, con su historia
    completa (Karla, 2026-09-21): qué informó la cajera, a qué se corrigió,
    cuánto, por qué, cuándo, quién (persona o regla automática) y con qué
    evidencia. El pago declarado nunca se modifica: esta tabla es la capa de
    trazabilidad y de ella sale el reporte de auditoría. Las reclasificaciones
    automáticas se recalculan en cada conciliación (cada creación queda además
    en la bitácora inmutable); las manuales nunca se borran."""

    class Origen(models.TextChoices):
        AUTOMATICA = "automatica", "Regla automática"
        MANUAL = "manual", "Manual"

    dia = models.ForeignKey(DiaArqueo, related_name="reclasificaciones", on_delete=models.PROTECT)
    pago = models.ForeignKey(PagoDeclarado, related_name="reclasificaciones", on_delete=models.PROTECT)
    medio_original = models.CharField(max_length=20, choices=PagoDeclarado.Medio.choices)
    medio_corregido = models.CharField(max_length=20, choices=PagoDeclarado.Medio.choices)
    monto = dinero()
    origen = models.CharField(max_length=12, choices=Origen.choices)
    regla = models.SlugField(max_length=60, help_text="Qué regla o resolución la produjo.")
    motivo = models.CharField(max_length=300)
    evidencia = models.JSONField(default=dict, blank=True)
    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
        help_text="Vacío cuando la hizo una regla automática.",
    )
    excepcion = models.ForeignKey(
        "Excepcion", null=True, blank=True, related_name="reclasificaciones", on_delete=models.SET_NULL,
    )
    creada_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["creada_en", "pk"]


class Parametro(models.Model):
    """Umbral o tolerancia que la Gerente puede ajustar desde Configuración
    (Karla, 2026-09-21). Las claves y sus valores por defecto viven en
    `nucleo.configuracion_servicio.DEFINICIONES`; si falta la fila se usa el
    valor por defecto."""

    clave = models.SlugField(max_length=60, unique=True)
    valor = dinero()

    def __str__(self) -> str:
        return f"{self.clave} = {self.valor}"


class MotivoResolucion(models.Model):
    """Lista cerrada de motivos (D9, aprobada por Karla). Sin esto el
    histórico queda en texto libre y no es analizable."""

    class AplicaA(models.TextChoices):
        EXCEPCION = "excepcion", "Resolución de excepción"
        DIFERENCIA_CAJA = "diferencia_caja", "Autorización de diferencia de caja"
        GASTO_CAJA = "gasto_caja", "Gasto o devolución de caja"

    codigo = models.SlugField(max_length=60, unique=True)
    texto = models.CharField(max_length=200)
    aplica_a = models.CharField(max_length=20, choices=AplicaA.choices)
    activo = models.BooleanField(default=True)

    def __str__(self) -> str:
        return self.texto


class CuentaContable(models.Model):
    """Mantenedor simple de cuentas contables de pérdida/ganancia de caja
    (Karla, 2026-09-16): a dónde va la diferencia cuando un pago con
    tarjeta que el POS declaró no calza con ningún cobro real del
    proveedor — o calza con uno de otro monto."""

    class Tipo(models.TextChoices):
        PERDIDA = "perdida", "Pérdida"
        GANANCIA = "ganancia", "Ganancia"
        # Karla, 2026-09-22: a diferencia de pérdida/ganancia (siempre
        # exactamente una de cada una), puede haber varias cuentas de
        # Banco — una por cada banco real con el que trabaja la empresa.
        # Alimentan los seleccionables de "Banco" en Control de Depósitos,
        # Reintegros de Caja y la cuenta del asiento de Reintegro.
        BANCO = "banco", "Banco"

    codigo = models.SlugField(max_length=60, unique=True)
    nombre = models.CharField(max_length=120)
    tipo = models.CharField(max_length=10, choices=Tipo.choices)
    activo = models.BooleanField(default=True)
    # Karla, 2026-09-24: cada banco se relaciona con su Diario y su Método de
    # pago de Odoo (ej. «Banco Estado 10397-7» → Diario «Banco Estado», Método
    # «Pagos Entrantes (Banco Estado)»). Los usa la captura de depósito; solo
    # aplican a cuentas de tipo Banco.
    diario_odoo = models.CharField(max_length=100, blank=True)
    metodo_pago_odoo = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ["nombre"]

    def __str__(self) -> str:
        return self.nombre


# Karla, 2026-09-21: "solo ocuparemos dos cuentas contables, una de pérdida
# y otra de ganancia" — toda diferencia (de caja o de tarjeta) va siempre a
# una de estas dos, nunca a una cuenta aparte por tipo de caso. Los códigos
# son los reales del plan de cuentas (antes eran heterogéneos: algunos
# slugs como "perdida_caja", otro numérico "6501002" — quedan unificados).
CUENTA_GANANCIA_CODIGO = "6501003"
CUENTA_PERDIDA_CODIGO = "6501004"


def cuenta_por_signo(diferencia) -> "CuentaContable":
    """Diferencia positiva (el POS declaró más de lo que en realidad se
    cobró o contó) = pérdida; negativa = ganancia."""
    codigo = CUENTA_PERDIDA_CODIGO if diferencia > 0 else CUENTA_GANANCIA_CODIGO
    return CuentaContable.objects.get(codigo=codigo)


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
        PAGO_DUPLICADO = "pago_duplicado", "Pago duplicado"

    class Resolucion(models.TextChoices):
        ACEPTAR = "aceptar", "Aceptar"
        RECHAZAR = "rechazar", "Rechazar"
        VINCULAR_OTRO = "vincular_otro", "Vincular otro movimiento"
        PERDIDA_GANANCIA = "perdida_ganancia", "Registrar pérdida/ganancia de caja"
        MEZCLA_MEDIOS = "mezcla_medios", "Registrar mezcla de medios"
        AJUSTE_SENCILLO = "ajuste_sencillo", "Ajuste de sencillo"
        CORREGIR_MEDIO = "corregir_medio", "Corregir el medio de la boleta"
        AGRUPAR = "agrupar", "Un cobro que paga varias boletas"

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
    # Registrar pérdida/ganancia de caja (Karla, 2026-09-16): la cajera se
    # guarda para poder cobrarle esa diferencia; sobre UMBRAL_VISADO_PERDIDA
    # (nucleo.perdidas_servicio) exige el visto bueno del Jefe de
    # Contabilidad antes de poder cerrar el día — dos pasos, igual que
    # guías de despacho y otros ingresos (nucleo.documentos_servicio).
    cuenta_contable = models.ForeignKey(CuentaContable, null=True, blank=True, on_delete=models.PROTECT)
    cajera_responsable = models.ForeignKey(
        "Cajera", null=True, blank=True, on_delete=models.PROTECT, related_name="perdidas_asignadas",
    )
    monto_perdida_ganancia = dinero(default=0)
    visado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
        related_name="perdidas_visadas",
    )
    visado_en = models.DateTimeField(null=True, blank=True)
    # Folio del asiento contable real una vez que contabilidad lo postea en
    # Odoo (Karla, 2026-09-18) — la app no lo genera, solo lo referencia
    # para poder rastrear "qué asiento respalda este ajuste".
    asiento_referencia = models.CharField(max_length=50, blank=True)

    class Meta:
        ordering = ["-monto_en_juego"]  # bandeja ordenada por monto en juego, no por aparición


class LineaConciliacion(models.Model):
    """Descomposición real de un pago cuando resolver como "mezcla de
    medios" (Karla, 2026-09-18: Resto declaró un solo medio, pero la
    conciliación comprobó que en realidad se recibió repartido en más de
    uno — ej. Transbank $45.000 + Efectivo $5.000 de una venta de
    $50.000). La suma de las líneas de una excepción debe ser exactamente
    el monto del pago original — el pago declarado nunca se modifica,
    esto es una capa aparte, cien por ciento trazable."""

    excepcion = models.ForeignKey(Excepcion, related_name="lineas_conciliacion", on_delete=models.CASCADE)
    medio_conciliado = models.CharField(max_length=20, choices=PagoDeclarado.Medio.choices)
    monto_conciliado = dinero()
    movimiento = models.ForeignKey(
        MovimientoProveedor, null=True, blank=True, on_delete=models.PROTECT,
        help_text="Evidencia externa de esta línea — vacío para una línea en efectivo, que no tiene "
                   "contraparte de un proveedor.",
    )
    fuente_evidencia = models.CharField(
        max_length=120, blank=True,
        help_text="Cuando no hay movimiento de proveedor (ej. efectivo): de dónde sale la certeza "
                   "de este monto — ej. \"Confirmado en arqueo de caja\".",
    )
    creada_en = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.get_medio_conciliado_display()} · ${self.monto_conciliado}"


UMBRAL_DIFERENCIA_CAJA = 5000  # $ — valor por defecto (D3, Karla 2026-09-05); el vigente se ajusta en Configuración


class Cajera(models.Model):
    """Mantenedor simple de cajeras (Karla, 2026-09-16): quién operó cada
    caja en el arqueo diario, para poder atribuir — y eventualmente
    cobrar — una pérdida de caja a la persona responsable."""

    nombre = models.CharField(max_length=120, unique=True)
    activo = models.BooleanField(default=True)

    class Meta:
        ordering = ["nombre"]

    def __str__(self) -> str:
        return self.nombre


class ResponsableTraslado(models.Model):
    """Mantenedor simple de quién puede retirar y llevar físicamente el
    efectivo al banco (Karla, 2026-09-22) — no necesariamente la misma
    persona que registra el depósito en el sistema. Se elige de esta lista
    en vez de escribirlo a mano en Control de Depósitos."""

    nombre = models.CharField(max_length=120, unique=True)
    activo = models.BooleanField(default=True)

    class Meta:
        ordering = ["nombre"]

    def __str__(self) -> str:
        return self.nombre


class ArqueoEfectivo(models.Model):
    dia = models.ForeignKey(DiaArqueo, related_name="arqueos_caja", on_delete=models.PROTECT)
    caja = models.CharField(max_length=30)
    cajera = models.ForeignKey(
        Cajera, null=True, blank=True, on_delete=models.PROTECT, related_name="arqueos",
        help_text="Quién operó esta caja este día — la asigna la Encargada de Ingresos.",
    )
    teorico = dinero(default=0)
    ajustes = dinero(default=0)
    esperado = dinero(default=0)
    fondo_inicial = dinero(default=0)
    gastos_autorizados = dinero(default=0)
    contado = dinero(null=True, blank=True)
    diferencia = dinero(null=True, blank=True)
    autorizado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
        help_text="Autorización operativa para cerrar el día pese a la diferencia (umbral_diferencia_caja).",
    )
    motivo_autorizacion = models.ForeignKey(MotivoResolucion, null=True, blank=True, on_delete=models.PROTECT)
    comentario = models.TextField(blank=True)
    # Visado contable (Karla, 2026-09-21): distinto de autorizado_por. Una
    # diferencia de caja sobre umbral_visado_perdida ($50.000) necesita,
    # además del cierre operativo del día, el visto bueno del Jefe de
    # Contabilidad antes de poder exportarse a la cuenta de resultado con
    # la cajera identificada — mismo control que ya existía para pérdidas
    # de tarjeta (nucleo.perdidas_servicio).
    visado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
        related_name="diferencias_caja_visadas",
    )
    visado_en = models.DateTimeField(null=True, blank=True)

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
        from nucleo.umbrales import umbral
        if abs(self.diferencia) <= umbral("umbral_diferencia_caja"):
            return "cuadrada" if self.diferencia == 0 else "dentro_de_umbral"
        return "autorizada" if self.autorizado_por_id else "requiere_autorizacion"


class GastoCaja(models.Model):
    """Línea individual de "Gastos autorizados" (Karla, 2026-09-18): antes
    era un solo número sin detalle; ahora cada devolución o gasto en
    efectivo queda trazable por separado — motivo, boleta asociada (ej.
    una Nota de Crédito) y quién lo registró. `ArqueoEfectivo.gastos_autorizados`
    es la suma de estas líneas.

    El caso que la originó: una NCR por un problema con el cliente, pagada
    originalmente con Transbank y devuelta en efectivo — su único efecto
    en esta app es que el arqueo de esa caja cuadre; el efecto contable
    completo de la NCR lo maneja Odoo cuando se emite el documento real."""

    arqueo = models.ForeignKey(ArqueoEfectivo, related_name="gastos", on_delete=models.CASCADE)
    monto = dinero()
    motivo = models.ForeignKey(MotivoResolucion, on_delete=models.PROTECT)
    boleta_asociada = models.CharField(
        max_length=50, blank=True,
        help_text="Boleta original, cuando el gasto es una devolución ligada a una NCR.",
    )
    comentario = models.TextField(blank=True)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    creado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-creado_en"]

    def __str__(self) -> str:
        return f"{self.motivo.texto} · ${self.monto}"


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
    # Segundo entregable (Karla, 2026-09-21, Fase 2): el asiento contable
    # "Arqueo Caja" — mismas versiones que el archivo de ventas, generados
    # juntos en el mismo clic.
    archivo_arqueo = models.CharField(max_length=500, blank=True)
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
    """Bitácora inmutable (control C7). Un evento se crea y nunca se toca.

    Protección en tres capas (Karla, 2026-09-26; el `delete()` del modelo por sí solo no basta):
    1. El modelo rechaza `save()` sobre un evento existente y `delete()`.
    2. Triggers de base de datos (migración 0032) rechazan cualquier UPDATE o DELETE sobre la tabla, aunque
       venga de `queryset.delete()`, de `bulk_update` o de SQL directo.
    3. Cada evento lleva un sello encadenado (sha256 del evento + el sello del anterior): si alguien
       quitara los triggers y alterara o borrara eventos, `manage.py verificar_bitacora` lo detecta."""

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
    hora = models.DateTimeField(default=timezone.now)
    sello_previo = models.CharField(max_length=64, blank=True)
    sello = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ["-hora"]

    def contenido_para_sello(self) -> str:
        return json.dumps({
            "dia": self.dia_id, "entidad": self.entidad, "entidad_id": self.entidad_id, "accion": self.accion,
            "anterior": self.valor_anterior, "posterior": self.valor_posterior, "usuario": self.usuario_id,
            "origen": self.origen, "hora": self.hora.isoformat(),
        }, sort_keys=True, ensure_ascii=False, default=str)

    @staticmethod
    def calcular_sello(sello_previo: str, contenido: str) -> str:
        return hashlib.sha256(f"{sello_previo}|{contenido}".encode("utf-8")).hexdigest()

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError("La bitácora de auditoría no se edita — solo se crean eventos nuevos.")
        anterior = EventoAuditoria.objects.exclude(sello="").order_by("-id").values_list("sello", flat=True).first()
        self.sello_previo = anterior or ""
        self.sello = self.calcular_sello(self.sello_previo, self.contenido_para_sello())
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("La bitácora de auditoría no se puede borrar.")


class ConfiguracionArqueoCaja(models.Model):
    """Datos contables fijos del asiento "Arqueo Caja" (Fase 2, Karla,
    2026-09-21) — cuentas de efectivo y de Transbank, nombre del diario en
    Odoo y código de la distribución analítica. Una sola fila (mantenedor
    en Configuración, solo Gerente); si no existe se usan estos valores por
    defecto, tomados del ejemplo real que entregó Karla."""

    diario = models.CharField(max_length=100, default="Arqueo Caja Pos Resto")
    cuenta_efectivo_codigo = models.CharField(max_length=20, default="1101004")
    cuenta_efectivo_nombre = models.CharField(max_length=120, default="Efectivo Pos Resto")
    cuenta_transbank_codigo = models.CharField(max_length=20, default="1101005")
    cuenta_transbank_nombre = models.CharField(max_length=120, default="Transbank Débito y Crédito")
    # Karla, 2026-09-21: todavía no hay un caso real de Mercado Pago con
    # diferencia o traspaso, pero se deja mapeada desde ahora para que el
    # sistema la reconozca apenas aparezca uno — no a la fuerza en
    # "cuenta_transbank", que es de otro adquirente.
    cuenta_mercado_pago_codigo = models.CharField(max_length=20, default="1104008")
    cuenta_mercado_pago_nombre = models.CharField(max_length=120, default="Redelcom (Mercado Pago)")
    # Karla, 2026-09-22: el Debe del asiento de Reintegro de Caja — se
    # configura igual que las demás, nunca se hardcodea en el código.
    cuenta_banco_codigo = models.CharField(max_length=20, default="1101001")
    cuenta_banco_nombre = models.CharField(max_length=120, default="Banco")
    # Karla, 2026-09-24: nombre con que la empresa figura como cliente/proveedor
    # en la captura de depósito de Odoo.
    razon_social = models.CharField(max_length=150, default="AGRICOLA HACIENDA HUENTELAUQUEN LTDA")
    codigo_analitico = models.CharField(
        max_length=20, blank=True, default="31",
        help_text="Código de la distribución analítica en Odoo — vacío para no incluirla.",
    )

    @classmethod
    def vigente(cls) -> "ConfiguracionArqueoCaja":
        existente = cls.objects.first()
        if existente is not None:
            return existente
        from nucleo.contexto_solicitud import solo_lectura_activo
        # Un perfil de solo lectura nunca crea filas: se devuelven los valores por defecto sin guardarlos.
        return cls() if solo_lectura_activo() else cls.objects.create()

    @property
    def cuenta_efectivo(self) -> str:
        return f"{self.cuenta_efectivo_codigo} {self.cuenta_efectivo_nombre}"

    @property
    def cuenta_transbank(self) -> str:
        return f"{self.cuenta_transbank_codigo} {self.cuenta_transbank_nombre}"

    @property
    def cuenta_mercado_pago(self) -> str:
        return f"{self.cuenta_mercado_pago_codigo} {self.cuenta_mercado_pago_nombre}"

    def cuenta_por_proveedor(self, proveedor: str) -> str:
        return self.cuenta_mercado_pago if proveedor == "mercado_pago" else self.cuenta_transbank

    @property
    def cuenta_banco(self) -> str:
        return f"{self.cuenta_banco_codigo} {self.cuenta_banco_nombre}"


class DepositoBancario(models.Model):
    """Control de Depósitos (Karla, 2026-09-21) — registro más completo que
    `Deposito`, con flujo de aprobación. Los dos conviven: `Deposito` es el
    dato rápido que ya se cargaba en Arqueo de efectivo; este es el control
    formal, que nace al cerrar el día (la Analista de Ingresos lo completa)
    y que el Jefe de Contabilidad debe aprobar u observar, dejando evidencia
    de que el dinero efectivamente llegó al banco. Una diferencia entre lo
    que debía depositarse y lo depositado nunca se compensa ni se borra
    sola — queda visible hasta que alguien la revise."""

    class Estado(models.TextChoices):
        PENDIENTE_DEPOSITO = "pendiente_deposito", "Pendiente de depósito"
        PENDIENTE_VALIDACION = "pendiente_validacion", "Pendiente de validación"
        APROBADO = "aprobado", "Aprobado"
        OBSERVADO = "observado", "Observado"

    dia = models.ForeignKey(DiaArqueo, related_name="depositos_bancarios", on_delete=models.PROTECT)
    fecha_deposito = models.DateField(
        null=True, blank=True,
        help_text="Cuándo se hizo el depósito — vacío mientras esté pendiente de depositar.",
    )
    banco = models.CharField(max_length=100, blank=True)
    cuenta_bancaria = models.CharField(max_length=60, blank=True)
    # Lo que el arqueo dice que debía depositarse — se calcula solo al
    # nacer el registro (suma de lo contado en efectivo ese día) y no se
    # vuelve a tocar, para que la diferencia sea siempre comparable.
    monto_esperado = dinero()
    monto_depositado = dinero(null=True, blank=True)
    numero_operacion = models.CharField(max_length=100, blank=True, help_text="N° de operación, comprobante o referencia bancaria.")
    comprobante_archivo = models.CharField(max_length=500, blank=True, help_text="Ruta del comprobante adjunto (fuera de rutas servidas al navegador). Obligatorio para registrar el detalle (Karla, 2026-09-22).")
    comprobante_nombre_original = models.CharField(max_length=255, blank=True)
    responsable_traslado = models.CharField(
        max_length=120, blank=True,
        help_text="Quién retiró y llevó físicamente el efectivo al banco — no necesariamente quien lo registra en el sistema (Karla, 2026-09-22).",
    )
    estado = models.CharField(max_length=25, choices=Estado.choices, default=Estado.PENDIENTE_DEPOSITO)

    registrado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name="depositos_registrados", on_delete=models.PROTECT,
    )
    registrado_en = models.DateTimeField(auto_now_add=True)
    actualizado_en = models.DateTimeField(auto_now=True)

    # Cotejo y aprobación (Jefe de Contabilidad) — no es un trámite
    # administrativo: exige la referencia del movimiento bancario real
    # antes de poder marcarse Aprobado (ver depositos_servicio.validar_deposito).
    aprobado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, related_name="depositos_revisados", on_delete=models.PROTECT,
    )
    aprobado_en = models.DateTimeField(null=True, blank=True)
    movimiento_bancario_referencia = models.CharField(
        max_length=255, blank=True,
        help_text="Con qué movimiento de la cartola bancaria se cotejó — evidencia de que el dinero entró al banco.",
    )
    resultado_cotejo = models.CharField(max_length=20, blank=True)
    comentarios = models.TextField(blank=True)
    # Karla, 2026-09-24: la captura de depósito para Odoo se puede descargar apenas
    # se ingresan los datos, antes de la validación del Jefe de Contabilidad.
    captura_exportada_en = models.DateTimeField(null=True, blank=True)
    captura_archivo = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["-dia__fecha", "-registrado_en"]

    def __str__(self) -> str:
        return f"Depósito {self.dia.fecha} · {self.get_estado_display()}"

    @property
    def diferencia(self):
        if self.monto_depositado is None:
            return None
        return self.monto_depositado - self.monto_esperado

    @property
    def cotejado(self) -> bool:
        return self.estado in (self.Estado.APROBADO, self.Estado.OBSERVADO)


class RegularizacionDocumento(models.Model):
    """Regularización de Guías de Despacho y Notas de Crédito (Karla,
    2026-09-22): DES y CRED ya NO entran a la carga masiva de ventas ni al
    arqueo automático — el archivo fuente nunca se altera. Quedan aquí,
    aparte, para que la Encargada de Ingresos las registre a mano cuando
    corresponda, sin perderlas. Trazabilidad pedida: "Documento original →
    Documento relacionado → Forma de pago → Arqueo afectado" — ese arqueo
    afectado es simplemente `dia`."""

    class Tipo(models.TextChoices):
        GUIA = "DES", "Guía de Despacho Electrónica"
        NOTA_CREDITO = "CRED", "Nota de Crédito Electrónica"
        # Código de documento que la app no conoce (Karla, 2026-09-24): no se
        # descarta ni se le supone un efecto — queda visible con su código
        # original hasta que alguien lo clasifique, y bloquea el cierre.
        SIN_CLASIFICAR = "SIN", "Sin clasificar"

    class Estado(models.TextChoices):
        PENDIENTE = "pendiente", "Pendiente"
        REGULARIZADO = "regularizado", "Regularizado"

    dia = models.ForeignKey(DiaArqueo, related_name="documentos_pendientes", on_delete=models.PROTECT)
    tipo = models.CharField(max_length=10, choices=Tipo.choices)
    n_interno = models.PositiveIntegerField()
    folio = models.CharField(max_length=50, help_text="Folio del documento (ver arqueo_diario.importadores.pos.folio_documento).")
    # COD_COMP tal como vino en el libro de Resto — nunca se modifica, aunque
    # después se clasifique el documento (trazabilidad para auditoría).
    codigo_original = models.CharField(max_length=20, blank=True)
    caja = models.CharField(max_length=30, blank=True)
    monto = dinero()
    estado = models.CharField(max_length=20, choices=Estado.choices, default=Estado.PENDIENTE)

    # Se completan al regularizar — "documento relacionado" es la factura
    # para una guía, o el documento que afecta para una nota de crédito.
    documento_relacionado = models.CharField(max_length=100, blank=True)
    fecha_documento_relacionado = models.DateField(null=True, blank=True)
    # Karla, 2026-09-22: viene con la forma de pago que ya trajo Resto en
    # el libro contable (`crear_desde_libro` la precompleta) — no hay que
    # volver a escribirla a mano, solo confirmarla o corregirla al
    # regularizar.
    forma_pago = models.CharField(max_length=20, choices=PagoDeclarado.Medio.choices, blank=True)
    referencia = models.TextField(blank=True)
    # Comprobante del pago de la factura que quedó en la contabilidad,
    # asociada a esta guía/nota de crédito (Karla, 2026-09-22) — mismo
    # patrón que Control de Depósitos y Reintegros de Caja.
    comprobante_archivo = models.CharField(max_length=500, blank=True)
    comprobante_nombre_original = models.CharField(max_length=255, blank=True)
    # Cobro del proveedor (Transbank/Mercado Pago) con el que calzó esta guía
    # pagada con tarjeta (Karla, 2026-09-23): la guía no es una comanda, así
    # que su cobro no debe salir como "movimiento sin pago" — se calza por
    # monto exacto y medio, y este vínculo es su cotejo validado.
    movimiento = models.ForeignKey(
        "MovimientoProveedor", null=True, blank=True, on_delete=models.SET_NULL, related_name="guias_calzadas",
    )

    # Verificación del medio real de la devolución — solo notas de crédito
    # (Karla, 2026-09-24): Resto registra la devolución como efectivo aunque el
    # cobro original haya sido con tarjeta o transferencia, y no vincula la
    # boleta original. Una nota solo resta del efectivo de la caja cuando el
    # medio verificado es efectivo, con evidencia adjunta y las firmas que
    # correspondan. Ver nucleo.notas_credito_servicio.
    class Verificacion(models.TextChoices):
        SIN_VERIFICAR = "sin_verificar", "Sin verificar"
        EVIDENCIA_CARGADA = "evidencia_cargada", "Evidencia cargada"
        PENDIENTE_SEGUNDA_FIRMA = "pendiente_segunda_firma", "Pendiente de segunda firma"
        VERIFICADA = "verificada", "Verificada"
        RECHAZADA = "rechazada", "Rechazada"

    class MedioVerificado(models.TextChoices):
        EFECTIVO = "efectivo", "Efectivo de la caja"
        TARJETA = "tarjeta", "Devuelto a la tarjeta"
        TRANSFERENCIA = "transferencia", "Transferencia al cliente"
        SIN_DEVOLUCION = "sin_devolucion", "Sin devolución de dinero (cambio o corrección)"

    verificacion_estado = models.CharField(max_length=30, choices=Verificacion.choices, default=Verificacion.SIN_VERIFICAR)
    medio_verificado = models.CharField(max_length=20, choices=MedioVerificado.choices, blank=True)
    boleta_original = models.CharField(max_length=60, blank=True)
    evidencia_archivo = models.CharField(max_length=500, blank=True)
    evidencia_nombre_original = models.CharField(max_length=255, blank=True)
    evidencia_comentario = models.TextField(blank=True)
    evidencia_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="evidencias_nc_cargadas",
    )
    evidencia_en = models.DateTimeField(null=True, blank=True)
    verificado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="notas_credito_verificadas",
    )
    verificado_en = models.DateTimeField(null=True, blank=True)
    segunda_firma_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="notas_credito_segunda_firma",
    )
    segunda_firma_en = models.DateTimeField(null=True, blank=True)
    verificacion_comentario = models.TextField(blank=True)

    clasificado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
        related_name="documentos_clasificados",
    )
    clasificado_en = models.DateTimeField(null=True, blank=True)
    motivo_clasificacion = models.TextField(blank=True)

    creado_en = models.DateTimeField(auto_now_add=True)
    regularizado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
        related_name="documentos_regularizados",
    )
    regularizado_en = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["dia", "tipo", "n_interno"], name="documento_pendiente_unico_por_dia"),
        ]
        ordering = ["-dia__fecha", "tipo", "folio"]

    def __str__(self) -> str:
        return f"{self.get_tipo_display()} {self.folio} · {self.dia.fecha}"

    @property
    def descuenta_efectivo(self) -> bool:
        """¿Esta nota resta hoy del efectivo esperado de su caja?"""
        return RegularizacionDocumento.objects.filter(pk=self.pk).filter(
            pk__in=[d.pk for d in __import__("nucleo.regularizacion_servicio", fromlist=["x"]).notas_que_restan(self.dia)]
        ).exists()

    @property
    def diferencia_con_cobro(self):
        """Guía − cobro del proveedor (positivo: el proveedor cobró menos)."""
        return self.monto - self.movimiento.monto_venta_valido_abono if self.movimiento_id else None

    @property
    def dias_pendiente(self) -> int:
        if self.estado != self.Estado.PENDIENTE:
            return 0
        from django.utils import timezone
        return (timezone.now() - self.creado_en).days


class ReintegroCaja(models.Model):
    """Reintegro de Caja (Karla, 2026-09-22): cuando una cajera con una
    pérdida de caja (diferencia negativa) devuelve el dinero antes de que
    se le descuente del sueldo. Reutiliza el origen de la pérdida sin
    duplicar el monto — se lee de `arqueo_origen` o `excepcion_origen`,
    exactamente uno de los dos. Permite reintegro total o en varios abonos
    parciales: cada fila es un abono, con su propio ciclo de validación
    bancaria (mismo rigor que Control de Depósitos — sin la referencia del
    movimiento bancario, el Jefe de Contabilidad no puede validarlo). El
    saldo pendiente se calcula siempre en caliente (ver
    reintegros_servicio.saldo_pendiente) sumando solo los abonos ya
    Validados — uno Registrado o Pendiente de validación todavía no reduce
    lo que se le puede descontar al trabajador."""

    class Estado(models.TextChoices):
        REGISTRADO = "registrado", "Registrado"
        PENDIENTE_VALIDACION = "pendiente_validacion_bancaria", "Pendiente de validación bancaria"
        VALIDADO = "validado", "Depósito validado"
        OBSERVADO = "observado", "Observado"

    cajera = models.ForeignKey(Cajera, related_name="reintegros", on_delete=models.PROTECT)
    arqueo_origen = models.ForeignKey(
        ArqueoEfectivo, null=True, blank=True, related_name="reintegros", on_delete=models.PROTECT,
        help_text="Origen cuando la pérdida es una diferencia de efectivo.",
    )
    excepcion_origen = models.ForeignKey(
        Excepcion, null=True, blank=True, related_name="reintegros", on_delete=models.PROTECT,
        help_text="Origen cuando la pérdida es de tarjeta, resuelta como pérdida/ganancia.",
    )

    monto = dinero(help_text="Monto de este abono — puede ser parcial.")
    fecha_reintegro = models.DateField(null=True, blank=True)
    banco = models.CharField(max_length=100, blank=True)
    cuenta_bancaria = models.CharField(max_length=60, blank=True)
    numero_operacion = models.CharField(max_length=100, blank=True)
    comprobante_archivo = models.CharField(max_length=500, blank=True)
    comprobante_nombre_original = models.CharField(max_length=255, blank=True)
    observaciones = models.TextField(blank=True)

    estado = models.CharField(max_length=30, choices=Estado.choices, default=Estado.REGISTRADO)
    registrado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name="reintegros_registrados", on_delete=models.PROTECT,
    )
    registrado_en = models.DateTimeField(auto_now_add=True)
    validado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, related_name="reintegros_validados", on_delete=models.PROTECT,
    )
    validado_en = models.DateTimeField(null=True, blank=True)
    movimiento_bancario_referencia = models.CharField(max_length=255, blank=True)
    comentario_validacion = models.TextField(blank=True)

    # Cuándo se incluyó este reintegro validado en un archivo de asiento
    # contable — evita exportarlo dos veces. El archivo mismo (Karla,
    # 2026-09-22: "nos falta el asiento de regularización del pago que
    # hizo el trabajador") — mismo formato que el asiento Arqueo Caja,
    # identificando la reversa de la pérdida original.
    exportado_en = models.DateTimeField(null=True, blank=True)
    archivo_asiento = models.CharField(max_length=500, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(arqueo_origen__isnull=False, excepcion_origen__isnull=True)
                    | models.Q(arqueo_origen__isnull=True, excepcion_origen__isnull=False)
                ),
                name="reintegro_un_solo_origen",
            ),
        ]
        ordering = ["-registrado_en"]

    def __str__(self) -> str:
        return f"Reintegro {self.cajera.nombre} · ${self.monto} · {self.get_estado_display()}"

    @property
    def monto_perdida_original(self):
        if self.arqueo_origen_id:
            return abs(self.arqueo_origen.diferencia)
        return self.excepcion_origen.monto_perdida_ganancia

    @property
    def dia(self) -> "DiaArqueo":
        return self.arqueo_origen.dia if self.arqueo_origen_id else self.excepcion_origen.dia

    @property
    def validado(self) -> bool:
        return self.estado == self.Estado.VALIDADO


class IntentoFallido(models.Model):
    """Un intento fallido de iniciar sesión (Karla, 2026-09-26). Alimenta el bloqueo temporal de `nucleo.bloqueo_acceso`;
    no es la bitácora (esa solo registra los bloqueos de cuentas reales)."""
    nombre = models.CharField(max_length=150, db_index=True)
    momento = models.DateTimeField(default=timezone.now, db_index=True)
    origen = models.CharField(max_length=100, blank=True)

    def __str__(self):
        return f"{self.nombre} · {self.momento:%Y-%m-%d %H:%M:%S}"


class DispositivoTotp(models.Model):
    """Aplicación de autenticación (Google/Microsoft Authenticator) de una persona (Karla, 2026-09-26; doble factor).
    El secreto se muestra una sola vez al activar; `ultimo_paso` impide reutilizar un código ya usado."""
    usuario = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="dispositivo_totp")
    secreto = models.CharField(max_length=64)
    confirmado = models.BooleanField(default=False)
    ultimo_paso = models.BigIntegerField(default=0)
    creado_en = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"Doble factor de {self.usuario} ({'activo' if self.confirmado else 'pendiente'})"


class CodigoDeRecuperacion(models.Model):
    """Códigos de un solo uso para entrar si se pierde el teléfono. Solo se guarda su huella, nunca el código."""
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="codigos_de_recuperacion")
    huella = models.CharField(max_length=64, db_index=True)
    usado_en = models.DateTimeField(null=True, blank=True)
