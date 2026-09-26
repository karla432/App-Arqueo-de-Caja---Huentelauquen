"""Crea (o completa) la cuenta de Administrador técnico, sin tocar a ninguna otra.

    python manage.py crear_admin_tecnico NOMBRE_DE_USUARIO [--password-desde-entorno VARIABLE]

Sin contraseña, la cuenta queda con contraseña inutilizable: se fija después con
`python manage.py changepassword NOMBRE_DE_USUARIO` (la pide por teclado; no viaja por ningún archivo).
El Administrador técnico administra usuarios y grupos en /admin/; NO opera ni aprueba nada financiero."""
import os

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError

from nucleo import contexto_solicitud
from nucleo.permisos import GRUPO_ADMIN_TECNICO, GRUPO_DE_PERFIL, Perfil


class Command(BaseCommand):
    help = "Crea la cuenta de Administrador técnico (usuarios e infraestructura), sin facultades financieras."

    def add_arguments(self, parser):
        parser.add_argument("usuario")
        parser.add_argument("--email", default="")
        parser.add_argument("--password-desde-entorno", metavar="VARIABLE",
                            help="Nombre de la variable de entorno que contiene la contraseña (no se acepta por línea de comandos).")

    def handle(self, *args, usuario, email, password_desde_entorno, **opciones):
        modelo = get_user_model()
        password = None
        if password_desde_entorno:
            password = os.environ.get(password_desde_entorno)
            if not password:
                raise CommandError(f"La variable de entorno {password_desde_entorno} no existe o está vacía.")
        cuenta = modelo.objects.filter(username=usuario).first()
        creada = cuenta is None
        if creada:
            cuenta = modelo(username=usuario, email=email)
        ficha = contexto_solicitud.fijar(None if creada else cuenta, solo_lectura=False)
        try:
            if creada:
                cuenta.is_active, cuenta.is_staff, cuenta.is_superuser = True, True, True
                cuenta.set_password(password) if password else cuenta.set_unusable_password()
                cuenta.save()
            else:
                cuenta.is_active, cuenta.is_staff = True, True
                if password:
                    cuenta.set_password(password)
                cuenta.save()
            grupos = [Group.objects.get_or_create(name=n)[0] for n in GRUPO_DE_PERFIL.values()]
            cuenta.groups.remove(*grupos)
            cuenta.groups.add(Group.objects.get(name=GRUPO_ADMIN_TECNICO))
        finally:
            contexto_solicitud.restaurar(ficha)
        self.stdout.write(self.style.SUCCESS(f"Cuenta «{usuario}» {'creada' if creada else 'actualizada'} con perfil {Perfil.ADMIN_TECNICO.value}."))
        if not cuenta.has_usable_password():
            self.stdout.write(f"Falta fijar su contraseña:  python manage.py changepassword {usuario}")
