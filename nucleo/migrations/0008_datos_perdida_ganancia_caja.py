"""Datos iniciales para pérdidas/ganancias de caja (Karla, 2026-09-16):
cuando un pago con tarjeta que el POS declaró no tiene contraparte real
del proveedor (o se vincula manualmente a un movimiento de otro monto),
la diferencia se registra contra una de estas cuentas, identificando a
la cajera responsable para poder cobrarle. Sobre $50.000 exige el visto
bueno del Jefe de Contabilidad (nucleo.perdidas_servicio) antes de poder
cerrar el día.
"""
from django.db import migrations

CUENTAS = [
    ("perdida_caja", "Pérdida de caja", "perdida"),
    ("ganancia_caja", "Ganancia de caja", "ganancia"),
]

MOTIVO_CODIGO = "perdida_ganancia_caja"
MOTIVO_TEXTO = "Diferencia de caja/tarjeta sin cobro real del proveedor"


def cargar_datos(apps, schema_editor):
    CuentaContable = apps.get_model("nucleo", "CuentaContable")
    MotivoResolucion = apps.get_model("nucleo", "MotivoResolucion")

    for codigo, nombre, tipo in CUENTAS:
        CuentaContable.objects.get_or_create(codigo=codigo, defaults={"nombre": nombre, "tipo": tipo})

    MotivoResolucion.objects.get_or_create(
        codigo=MOTIVO_CODIGO, defaults={"texto": MOTIVO_TEXTO, "aplica_a": "excepcion"},
    )


def quitar_datos(apps, schema_editor):
    CuentaContable = apps.get_model("nucleo", "CuentaContable")
    MotivoResolucion = apps.get_model("nucleo", "MotivoResolucion")

    CuentaContable.objects.filter(codigo__in=[c for c, _, _ in CUENTAS]).delete()
    MotivoResolucion.objects.filter(codigo=MOTIVO_CODIGO).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("nucleo", "0007_cajera_cuentacontable_and_more"),
    ]

    operations = [
        migrations.RunPython(cargar_datos, quitar_datos),
    ]
