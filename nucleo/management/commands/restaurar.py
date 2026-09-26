"""Verifica o restaura un respaldo hecho con `respaldar`.

    python manage.py restaurar ARCHIVO.zip                     # solo VERIFICA (no cambia nada)
    python manage.py restaurar ARCHIVO.zip --ensayo CARPETA    # extrae a una carpeta aparte para revisarla
    python manage.py restaurar ARCHIVO.zip --reemplazar-esta-instalacion --confirmo

Con --reemplazar-esta-instalacion la base y las carpetas de datos del sistema se reemplazan por las del respaldo. Lo anterior
no se borra: queda renombrado con el sufijo `.antes_de_restaurar_<fecha>`. La aplicación debe estar DETENIDA. No restaura
secretos (clave secreta ni variables de entorno): esos se reponen desde el gestor de claves."""
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from nucleo import respaldos


class Command(BaseCommand):
    help = "Verifica, ensaya o restaura un respaldo."

    def add_arguments(self, parser):
        parser.add_argument("archivo")
        parser.add_argument("--ensayo", metavar="CARPETA", help="Extraer a una carpeta vacía, sin tocar el sistema.")
        parser.add_argument("--reemplazar-esta-instalacion", action="store_true")
        parser.add_argument("--confirmo", action="store_true", help="Confirma que la aplicación está detenida y que se reemplaza lo actual.")

    def handle(self, *args, archivo, ensayo, reemplazar_esta_instalacion, confirmo, **opciones):
        ruta = Path(archivo)
        if not ruta.exists():
            raise CommandError(f"No existe {ruta}.")
        try:
            if reemplazar_esta_instalacion:
                if not confirmo:
                    raise CommandError("Confirma con --confirmo (la aplicación debe estar detenida): se reemplazan la base y los archivos actuales.")
                if settings.DATABASES["default"]["ENGINE"] != "django.db.backends.sqlite3":
                    raise CommandError("La restauración integrada es solo para SQLite.")
                resumen = respaldos.restaurar_reemplazando(
                    ruta, base_dir=Path(settings.BASE_DIR), ruta_db=Path(settings.DATABASES["default"]["NAME"]),
                )
                self.stdout.write(self.style.SUCCESS(
                    f"Restaurado el respaldo del {resumen['creado']}. Lo anterior quedó con el sufijo {resumen['copia_de_lo_anterior']}."
                ))
            elif ensayo:
                resumen = respaldos.restaurar_en_carpeta(ruta, Path(ensayo))
                self.stdout.write(self.style.SUCCESS(f"Ensayo correcto: respaldo del {resumen['creado']} extraído en {ensayo}."))
            else:
                resumen = respaldos.verificar_respaldo(ruta)
                self.stdout.write(self.style.SUCCESS(
                    f"Respaldo válido: creado {resumen['creado']}; {resumen['archivos']} archivos; "
                    f"{resumen['eventos_bitacora']} eventos de bitácora íntegros."
                ))
        except respaldos.RespaldoInvalido as exc:
            raise CommandError(str(exc))
