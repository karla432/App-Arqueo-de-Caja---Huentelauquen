"""Datos para los casos nuevos de conciliación de medios (Karla,
2026-09-18):

- "Sobrante de medio de pago" (Transbank/Mercado Pago cobró más que la
  boleta): reutiliza el mecanismo de pérdida/ganancia ya existente — solo
  hacía falta una cuenta contable con el nombre de negocio correcto, en
  vez de la genérica "Ganancia de caja". Mismo umbral de visado.
- "Mezcla de medios" (Resto declaró un solo medio, la conciliación
  encontró que en realidad se repartió en más de uno): motivo nuevo para
  la resolución vía LineaConciliacion.
"""
from django.db import migrations

CUENTA_SOBRANTE = ("sobrante_medio_pago", "Sobrante de medio de pago", "ganancia")
MOTIVO_CODIGO = "mezcla_medios_pago"
MOTIVO_TEXTO = "Mezcla de medios — Resto declaró un solo medio, se repartió en más de uno"


def cargar_datos(apps, schema_editor):
    CuentaContable = apps.get_model("nucleo", "CuentaContable")
    MotivoResolucion = apps.get_model("nucleo", "MotivoResolucion")

    codigo, nombre, tipo = CUENTA_SOBRANTE
    CuentaContable.objects.get_or_create(codigo=codigo, defaults={"nombre": nombre, "tipo": tipo})
    MotivoResolucion.objects.get_or_create(
        codigo=MOTIVO_CODIGO, defaults={"texto": MOTIVO_TEXTO, "aplica_a": "excepcion"},
    )


def quitar_datos(apps, schema_editor):
    CuentaContable = apps.get_model("nucleo", "CuentaContable")
    MotivoResolucion = apps.get_model("nucleo", "MotivoResolucion")

    CuentaContable.objects.filter(codigo=CUENTA_SOBRANTE[0]).delete()
    MotivoResolucion.objects.filter(codigo=MOTIVO_CODIGO).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("nucleo", "0009_excepcion_asiento_referencia_and_more"),
    ]

    operations = [
        migrations.RunPython(cargar_datos, quitar_datos),
    ]
