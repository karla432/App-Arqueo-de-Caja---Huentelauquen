"""Carga la lista cerrada de motivos aprobada por Karla (decisión D9,
2026-09-05) — ver PROMPT.md sección 28 / memoria de proyecto."""
from django.db import migrations

MOTIVOS_EXCEPCION = [
    ("medio_mal_declarado", "Medio mal declarado por el cajero"),
    ("cobro_reintentado", "Cobro reintentado en otro medio"),
    ("error_config_terminal", "Error de configuración del terminal"),
    ("otro_excepcion", "Otro"),
]

MOTIVOS_DIFERENCIA_CAJA = [
    ("error_vuelto", "Error de vuelto identificado"),
    ("gasto_sin_respaldo", "Gasto sin respaldo formal"),
    ("faltante_en_investigacion", "Faltante en investigación"),
    ("otro_diferencia_caja", "Otro"),
]


def cargar_motivos(apps, schema_editor):
    MotivoResolucion = apps.get_model("nucleo", "MotivoResolucion")
    for codigo, texto in MOTIVOS_EXCEPCION:
        MotivoResolucion.objects.create(codigo=codigo, texto=texto, aplica_a="excepcion")
    for codigo, texto in MOTIVOS_DIFERENCIA_CAJA:
        MotivoResolucion.objects.create(codigo=codigo, texto=texto, aplica_a="diferencia_caja")


def quitar_motivos(apps, schema_editor):
    MotivoResolucion = apps.get_model("nucleo", "MotivoResolucion")
    codigos = [c for c, _ in MOTIVOS_EXCEPCION + MOTIVOS_DIFERENCIA_CAJA]
    MotivoResolucion.objects.filter(codigo__in=codigos).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("nucleo", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(cargar_motivos, quitar_motivos),
    ]
