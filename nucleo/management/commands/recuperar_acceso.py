"""Vía técnica de recuperación de acceso, desde la consola del servidor.

    python manage.py recuperar_acceso NOMBRE_DE_USUARIO [--reactivar] [--password-desde-entorno VARIABLE]

Sirve si se pierde la contraseña del Administrador técnico o de la Gerente, o si una cuenta quedó desactivada.
Exige acceso al servidor (no es una pantalla de la aplicación) y deja un evento en la bitácora. No cambia perfiles."""
import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from nucleo import contexto_solicitud
from nucleo.models import EventoAuditoria


class Command(BaseCommand):
    help = "Restablece la contraseña y/o reactiva una cuenta (recuperación desde la consola del servidor)."

    def add_arguments(self, parser):
        parser.add_argument("usuario")
        parser.add_argument("--reactivar", action="store_true", help="Volver a activar la cuenta.")
        parser.add_argument("--password-desde-entorno", metavar="VARIABLE")

    def handle(self, *args, usuario, reactivar, password_desde_entorno, **opciones):
        cuenta = get_user_model().objects.filter(username=usuario).first()
        if cuenta is None:
            raise CommandError(f"No existe la cuenta «{usuario}».")
        if not reactivar and not password_desde_entorno:
            raise CommandError("Indica --reactivar y/o --password-desde-entorno VARIABLE.")
        cambios = []
        ficha = contexto_solicitud.fijar(cuenta, solo_lectura=False)
        try:
            if reactivar and not cuenta.is_active:
                cuenta.is_active = True
                cambios.append("reactivada")
            if password_desde_entorno:
                clave = os.environ.get(password_desde_entorno)
                if not clave:
                    raise CommandError(f"La variable de entorno {password_desde_entorno} no existe o está vacía.")
                cuenta.set_password(clave)
                cambios.append("contraseña restablecida")
            cuenta.save()
            EventoAuditoria.objects.create(
                dia=None, entidad="Usuario", entidad_id=str(cuenta.pk), accion="recuperar_acceso_por_consola",
                valor_posterior={"usuario": cuenta.username, "cambios": cambios or ["sin cambios"]}, usuario=cuenta,
                origen="consola del servidor",
            )
        finally:
            contexto_solicitud.restaurar(ficha)
        self.stdout.write(self.style.SUCCESS(f"Cuenta «{usuario}»: {', '.join(cambios) or 'sin cambios'}."))
