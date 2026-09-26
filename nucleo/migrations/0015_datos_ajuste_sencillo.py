"""Ajuste de sencillo (Karla, 2026-09-21): diferencias de hasta $10, positivas
o negativas, se calzan solas y se contabilizan siempre en la cuenta
6501002 – Pérdida por Ajuste de Sencillo, con el motivo "Ajuste de
sencillo" para mantener la trazabilidad."""
from django.db import migrations

CUENTA = ("6501002", "Pérdida por Ajuste de Sencillo", "perdida")
MOTIVO = ("ajuste_sencillo", "Ajuste de sencillo")


def cargar(apps, schema_editor):
    CuentaContable = apps.get_model("nucleo", "CuentaContable")
    MotivoResolucion = apps.get_model("nucleo", "MotivoResolucion")
    CuentaContable.objects.get_or_create(
        codigo=CUENTA[0], defaults={"nombre": CUENTA[1], "tipo": CUENTA[2]},
    )
    MotivoResolucion.objects.get_or_create(
        codigo=MOTIVO[0], defaults={"texto": MOTIVO[1], "aplica_a": "excepcion"},
    )


def quitar(apps, schema_editor):
    apps.get_model("nucleo", "CuentaContable").objects.filter(codigo=CUENTA[0]).delete()
    apps.get_model("nucleo", "MotivoResolucion").objects.filter(codigo=MOTIVO[0]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("nucleo", "0014_alter_excepcion_resolucion"),
    ]

    operations = [
        migrations.RunPython(cargar, quitar),
    ]
