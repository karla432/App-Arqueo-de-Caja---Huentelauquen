"""Autenticación con bloqueo temporal por intentos fallidos (ver `nucleo.bloqueo_acceso`).

Sirve tanto para la pantalla de ingreso de la aplicación como para /admin/login/: el bloqueo se aplica en el
respaldo de autenticación, no solo en un formulario."""
from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import ValidationError

from nucleo import bloqueo_acceso


class RespaldoConBloqueo(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        if bloqueo_acceso.esta_bloqueado(username):
            return None  # ni con la clave correcta mientras dure el bloqueo
        return super().authenticate(request, username=username, password=password, **kwargs)


class FormularioDeIngreso(AuthenticationForm):
    """Igual que el estándar, pero avisa cuánto falta si la cuenta está bloqueada."""

    def clean(self):
        minutos = bloqueo_acceso.minutos_restantes(self.data.get(self.add_prefix("username")))
        if minutos:
            raise ValidationError(
                "Demasiados intentos fallidos. Por seguridad la cuenta está bloqueada; vuelve a intentar en %(m)s minuto(s).",
                code="bloqueada", params={"m": minutos},
            )
        return super().clean()
