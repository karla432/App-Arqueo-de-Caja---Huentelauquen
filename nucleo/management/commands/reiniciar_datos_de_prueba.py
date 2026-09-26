"""Limpia los datos de los días cargados (ventas, conciliaciones, arqueos, depósitos, reintegros, documentos,
exportaciones, archivos) conservando la configuración (cajeras, bancos, cuentas, parámetros, motivos, usuarios).

    python manage.py reiniciar_datos_de_prueba --confirmo-que-es-un-ambiente-de-pruebas

SOLO en ENTORNO=desarrollo: en producción se niega siempre. Es la única vía que quita temporalmente los triggers de la
bitácora (para borrar los eventos de esos días) y los vuelve a instalar al terminar; queda constancia en la consola."""
import importlib
import shutil
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.models.deletion import ProtectedError

from nucleo import models as m


class Command(BaseCommand):
    help = "Borra los datos de los días (solo ambiente de pruebas), conservando la configuración."

    def add_arguments(self, parser):
        parser.add_argument("--confirmo-que-es-un-ambiente-de-pruebas", action="store_true", dest="confirmo")

    def handle(self, *args, confirmo, **opciones):
        if settings.PRODUCCION:
            raise CommandError("Este comando no se puede usar en producción.")
        if not confirmo:
            raise CommandError("Confirma con --confirmo-que-es-un-ambiente-de-pruebas: borra los datos de todos los días.")
        if connection.vendor != "sqlite":
            raise CommandError("Solo implementado para SQLite.")
        triggers = importlib.import_module("nucleo.migrations.0032_bitacora_protegida_por_triggers")

        a_borrar = [
            m.Reclasificacion, m.LineaConciliacion, m.Coincidencia, m.GastoCaja, m.ConteoDenominacion, m.Deposito,
            m.Exportacion, m.DepositoBancario, m.ReintegroCaja, m.RegularizacionDocumento, m.Excepcion, m.ArqueoEfectivo,
            m.PagoDeclarado, m.LineaVenta, m.Venta, m.MovimientoProveedor, m.VersionMotor, m.LoteCarga, m.DiaArqueo,
        ]
        with connection.cursor() as c:
            for nombre, _ in triggers.TRIGGERS:
                c.execute(f"DROP TRIGGER IF EXISTS {nombre}")
        self.stdout.write(self.style.WARNING("Triggers de la bitácora quitados temporalmente."))
        try:
            with transaction.atomic():
                m.EventoAuditoria.objects.filter(dia__isnull=False).delete()
                pendientes = list(a_borrar)
                for _ in range(20):
                    siguientes = []
                    for modelo in pendientes:
                        try:
                            modelo.objects.all().delete()
                        except ProtectedError:
                            siguientes.append(modelo)
                    pendientes = siguientes
                    if not pendientes:
                        break
                if pendientes:
                    raise CommandError("No se pudo borrar: " + ", ".join(x.__name__ for x in pendientes))
        finally:
            with connection.cursor() as c:
                for _, sql in triggers.TRIGGERS:
                    c.execute(sql)
            self.stdout.write(self.style.SUCCESS("Triggers de la bitácora instalados de nuevo."))

        base = Path(settings.BASE_DIR).resolve()
        for carpeta in ("archivos_cargados", "exportaciones"):
            ruta = (base / carpeta).resolve()
            if ruta.parent != base or not ruta.exists():
                continue
            for hijo in ruta.iterdir():
                shutil.rmtree(hijo) if hijo.is_dir() else hijo.unlink()
        self.stdout.write(self.style.SUCCESS("Datos de los días borrados. Se conservó la configuración."))
