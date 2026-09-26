"""Vía técnica de recuperación de acceso, desde la consola del servidor.

    python manage.py recuperar_acceso NOMBRE_DE_USUARIO [--reactivar] [--password-desde-entorno VARIABLE]

Sirve si se pierde la contraseña del Administrador técnico o de la Gerente, o si una cuenta quedó desactivada.
Exige acceso al servidor (no es una pantalla de la aplicación) y deja un evento en la bitácora. No cambia perfiles."""
import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from nucleo import bloqueo_acceso, contexto_solicitud
from nucleo.models import EventoAuditoria


class Command(BaseCommand):
    help = "Restablece la contraseña y/o reactiva una cuenta (recuperación desde la consola del servidor)."

    def add_arguments(self, parser):
        parser.add_argument("usuario")
        parser.add_argument("--reactivar", action="store_true", help="Volver a activar la cuenta.")
        parser.add_argument("--password-desde-entorno", metavar="VARIABLE")
        parser.add_argument("--reiniciar-dos-pasos", action="store_true", help="Quitar la verificación en dos pasos (pérdida del teléfono): se vuelve a activar al ingresar.")

    def handle(self, *args, usuario, reactivar, password_desde_entorno, reiniciar_dos_pasos=False, **opciones):
        cuenta = get_user_model().objects.filter(username=usuario).first()
        if cuenta is None:
            raise CommandError(f"No existe la cuenta «{usuario}».")
        if not reactivar and not password_desde_entorno and not reiniciar_dos_pasos:
            raise CommandError("Indica --reactivar, --password-desde-entorno VARIABLE y/o --reiniciar-dos-pasos.")
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
            if reiniciar_dos_pasos:
                from nucleo import dos_pasos
                dos_pasos.reiniciar(cuenta, por=cuenta)
                cambios.append("verificación en dos pasos reiniciada")
            cuenta.save()
            bloqueo_acceso.limpiar(cuenta.get_username())  # también levanta un bloqueo por intentos fallidos
            EventoAuditoria.objects.create(
                dia=None, entidad="Usuario", entidad_id=str(cuenta.pk), accion="recuperar_acceso_por_consola",
                valor_posterior={"usuario": cuenta.username, "cambios": cambios or ["sin cambios"]}, usuario=cuenta,
                origen="consola del servidor",
            )
        finally:
            contexto_solicitud.restaurar(ficha)
        self.stdout.write(self.style.SUCCESS(f"Cuenta «{usuario}»: {', '.join(cambios) or 'sin cambios'}."))
