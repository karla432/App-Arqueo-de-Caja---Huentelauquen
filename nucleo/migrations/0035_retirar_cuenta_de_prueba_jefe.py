"""Retira la cuenta de prueba «jefe_contabilidad» que creaba 0005 con una contraseña fija escrita en el repositorio.

Solo actúa si la cuenta sigue teniendo esa contraseña de prueba: la desactiva, le quita superusuario/staff y deja la
contraseña inutilizable. No se borra (puede figurar en la bitácora o en días ya operados). Si alguien ya le había puesto
una contraseña propia, no se toca."""
from django.contrib.auth.hashers import check_password, make_password
from django.db import migrations

CONTRASENA_DE_PRUEBA = "Huentelauquen2026"


def retirar_cuenta_de_prueba(apps, schema_editor):
    Usuario = apps.get_model("auth", "User")
    cuenta = Usuario.objects.filter(username="jefe_contabilidad").first()
    if cuenta and check_password(CONTRASENA_DE_PRUEBA, cuenta.password):
        cuenta.password = make_password(None)
        cuenta.is_active = cuenta.is_staff = cuenta.is_superuser = False
        cuenta.save(update_fields=["password", "is_active", "is_staff", "is_superuser"])


class Migration(migrations.Migration):

    dependencies = [
        ("nucleo", "0034_doble_factor_totp"),
    ]

    operations = [
        migrations.RunPython(retirar_cuenta_de_prueba, migrations.RunPython.noop),
    ]
