"""Protección de la bitácora a nivel de base de datos (Karla, 2026-09-26).

El `delete()` del modelo no impide `queryset.delete()`, `bulk_update` ni SQL directo. Estos triggers de SQLite
rechazan todo UPDATE y DELETE sobre la tabla de eventos. Quitar los triggers exige acceso a la base de datos
(queda detectable con `manage.py verificar_bitacora`, que comprueba el sello encadenado de cada evento).

Solo SQLite está implementado: con otro motor la migración avisa y no hace nada — habría que crear el
equivalente antes de pasar a producción con ese motor."""
import warnings

from django.db import migrations

TRIGGERS = [
    (
        "bitacora_sin_borrado",
        "CREATE TRIGGER IF NOT EXISTS bitacora_sin_borrado BEFORE DELETE ON nucleo_eventoauditoria "
        "BEGIN SELECT RAISE(ABORT, 'La bitacora de auditoria no se puede borrar.'); END;",
    ),
    (
        "bitacora_sin_edicion",
        "CREATE TRIGGER IF NOT EXISTS bitacora_sin_edicion BEFORE UPDATE ON nucleo_eventoauditoria "
        "BEGIN SELECT RAISE(ABORT, 'La bitacora de auditoria no se edita.'); END;",
    ),
]


def instalar(apps, schema_editor):
    if schema_editor.connection.vendor != "sqlite":
        warnings.warn("Triggers de bitácora: solo implementados para SQLite; este motor queda sin esa protección.")
        return
    for _, sql in TRIGGERS:
        schema_editor.execute(sql)


def quitar(apps, schema_editor):
    if schema_editor.connection.vendor != "sqlite":
        return
    for nombre, _ in TRIGGERS:
        schema_editor.execute(f"DROP TRIGGER IF EXISTS {nombre}")


class Migration(migrations.Migration):
    dependencies = [("nucleo", "0031_bitacora_con_sello_y_grupos_de_perfil")]
    operations = [migrations.RunPython(instalar, quitar)]
