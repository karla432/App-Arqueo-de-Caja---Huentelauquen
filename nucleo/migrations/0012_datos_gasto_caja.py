"""Motivos cerrados para gastos/devoluciones de caja (Karla, 2026-09-18):
el caso que los originó es una Nota de Crédito emitida por un problema
con el cliente, pagada originalmente con Transbank y devuelta en
efectivo — necesitábamos poder registrar esa salida de efectivo con
trazabilidad (motivo, boleta, usuario) en vez de un solo número sin
detalle en "Gastos autorizados"."""
from django.db import migrations

MOTIVOS_GASTO_CAJA = [
    ("devolucion_ncr", "Devolución en efectivo por Nota de Crédito"),
    ("otro_gasto_caja", "Otro"),
]


def cargar_motivos(apps, schema_editor):
    MotivoResolucion = apps.get_model("nucleo", "MotivoResolucion")
    for codigo, texto in MOTIVOS_GASTO_CAJA:
        MotivoResolucion.objects.get_or_create(
            codigo=codigo, defaults={"texto": texto, "aplica_a": "gasto_caja"},
        )


def quitar_motivos(apps, schema_editor):
    MotivoResolucion = apps.get_model("nucleo", "MotivoResolucion")
    MotivoResolucion.objects.filter(codigo__in=[c for c, _ in MOTIVOS_GASTO_CAJA]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("nucleo", "0011_alter_motivoresolucion_aplica_a_gastocaja"),
    ]

    operations = [
        migrations.RunPython(cargar_motivos, quitar_motivos),
    ]
