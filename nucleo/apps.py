from django.apps import AppConfig


class NucleoConfig(AppConfig):
    name = 'nucleo'

    def ready(self):
        from nucleo import auditoria_acceso  # noqa: F401  (registra los avisos de inicio de sesión y cambios de perfil)
