"""Hace un respaldo completo (base + archivos cargados + exportaciones) en UN archivo zip.

    python manage.py respaldar [--destino CARPETA]

La carpeta sale de --destino, de la variable de entorno ARQUEO_RESPALDOS o, por defecto, de `respaldos/` junto al
proyecto. Verifica el respaldo recién hecho (huellas, integridad de la base y cadena de sellos de la bitácora) y aplica
la conservación: últimos 30 respaldos + el último de cada uno de los últimos 12 meses. Sale con código 1 si algo falla,
para que el programador de tareas lo detecte.

IMPORTANTE: guarda también una copia FUERA de este equipo (otro disco, otra oficina o la nube). Un respaldo en el mismo
computador no protege de una falla o robo de ese computador."""
import os
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from nucleo import respaldos


class Command(BaseCommand):
    help = "Respaldo completo verificado (base, archivos cargados y exportaciones), con conservación automática."

    def add_arguments(self, parser):
        parser.add_argument("--destino", help="Carpeta donde guardar los respaldos.")
        parser.add_argument("--sin-conservacion", action="store_true", help="No borrar respaldos antiguos.")

    def handle(self, *args, destino, sin_conservacion, **opciones):
        carpeta = Path(destino or os.environ.get("ARQUEO_RESPALDOS") or Path(settings.BASE_DIR) / "respaldos")
        base_de_datos = settings.DATABASES["default"]
        if base_de_datos["ENGINE"] != "django.db.backends.sqlite3":
            raise CommandError("El respaldo integrado es solo para SQLite. Con PostgreSQL usa pg_dump (ver README_DESPLIEGUE.md).")
        try:
            zip_final = respaldos.crear_respaldo(carpeta, ruta_db=Path(base_de_datos["NAME"]), base_dir=Path(settings.BASE_DIR))
            resumen = respaldos.verificar_respaldo(zip_final)
        except respaldos.RespaldoInvalido as exc:
            raise CommandError(f"El respaldo falló: {exc}")
        self.stdout.write(self.style.SUCCESS(
            f"Respaldo listo y verificado: {zip_final} ({zip_final.stat().st_size / 1_048_576:.1f} MB; "
            f"{resumen['archivos']} archivos; {resumen['eventos_bitacora']} eventos de bitácora íntegros)."
        ))
        if not sin_conservacion:
            borrados = respaldos.aplicar_conservacion(carpeta)
            if borrados:
                self.stdout.write(f"Conservación: se eliminaron {len(borrados)} respaldo(s) antiguo(s).")
        self.stdout.write("Recuerda guardar una copia de este archivo FUERA de este equipo.")
