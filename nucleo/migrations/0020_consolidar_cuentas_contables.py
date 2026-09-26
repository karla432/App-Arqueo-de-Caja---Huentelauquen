"""Solo 2 cuentas contables, siempre las mismas (Karla, 2026-09-21): una de
pérdida y una de ganancia, con los códigos reales y homogéneos del plan de
cuentas — reemplaza las 4 que había antes (heterogéneas: dos slugs de
texto, un código numérico, y una cuarta que ya no se usa)."""
from django.db import migrations


CODIGOS_ANTIGUOS = ["ganancia_caja", "perdida_caja", "6501002", "sobrante_medio_pago"]


def consolidar(apps, schema_editor):
    CuentaContable = apps.get_model("nucleo", "CuentaContable")
    CuentaContable.objects.filter(codigo__in=CODIGOS_ANTIGUOS).delete()
    CuentaContable.objects.update_or_create(
        codigo="6501003", defaults={"nombre": "Ganancia por descuento de efectivo", "tipo": "ganancia", "activo": True},
    )
    CuentaContable.objects.update_or_create(
        codigo="6501004", defaults={"nombre": "Pérdida por diferencia de efectivo", "tipo": "perdida", "activo": True},
    )


def deshacer(apps, schema_editor):
    pass  # las cuentas antiguas no se recrean; si hace falta, se agregan a mano en Configuración


class Migration(migrations.Migration):
    dependencies = [("nucleo", "0019_fase2_arqueo_caja")]
    operations = [migrations.RunPython(consolidar, deshacer)]
