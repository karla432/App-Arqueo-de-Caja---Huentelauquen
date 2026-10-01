"""Grupos de aprobación escalada para documentos pendientes de emisión
(guías de despacho y pagos sin boleta) y el motivo cerrado
"Forma de pago sin boleta — otros ingresos", confirmados con Karla
2026-09-06.

Nivel 1 = Jefe de Contabilidad (hasta 10 casos pendientes en el día).
Nivel 2 = Gerente de Administración y Finanzas (más de 10 casos).
"""
from django.db import migrations

GRUPO_JEFE_CONTABILIDAD = "Jefe de Contabilidad"
GRUPO_GERENTE_ADM_FINANZAS = "Gerente de Administración y Finanzas"


def crear_grupos_y_motivo(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    MotivoResolucion = apps.get_model("nucleo", "MotivoResolucion")
    Usuario = apps.get_model("auth", "User")

    grupo_jefe, _ = Group.objects.get_or_create(name=GRUPO_JEFE_CONTABILIDAD)
    grupo_gerente, _ = Group.objects.get_or_create(name=GRUPO_GERENTE_ADM_FINANZAS)

    MotivoResolucion.objects.get_or_create(
        codigo="forma_pago_sin_boleta",
        defaults={
            "texto": "Forma de pago sin boleta — otros ingresos",
            "aplica_a": "excepcion",
        },
    )

    karla = Usuario.objects.filter(username="karla").first()
    if karla:
        karla.groups.add(grupo_gerente, grupo_jefe)

    # Aquí se creaba una cuenta de prueba «jefe_contabilidad» con contraseña fija. Se retiró (2026-10-01, antes de
    # publicar): una base nueva —la de producción incluida— no debe traer ninguna cuenta. Ver 0035.


def quitar_grupos_y_motivo(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    MotivoResolucion = apps.get_model("nucleo", "MotivoResolucion")
    Usuario = apps.get_model("auth", "User")

    MotivoResolucion.objects.filter(codigo="forma_pago_sin_boleta").delete()
    Usuario.objects.filter(username="jefe_contabilidad").delete()
    Group.objects.filter(name__in=[GRUPO_JEFE_CONTABILIDAD, GRUPO_GERENTE_ADM_FINANZAS]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("nucleo", "0004_excepcion_factura_asociada_venta_factura_asociada"),
    ]

    operations = [
        migrations.RunPython(crear_grupos_y_motivo, quitar_grupos_y_motivo),
    ]
