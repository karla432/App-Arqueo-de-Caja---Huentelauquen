"""Migra las cuentas existentes al modelo de perfiles SIN bloquear el trabajo diario y sin borrar nada.

    python manage.py migrar_perfiles                 # informe y plan (no cambia nada)
    python manage.py migrar_perfiles --aplicar       # aplica el plan

Qué hace:
1. Informa, cuenta por cuenta, qué puede hacer hoy y qué le da (o le quita) cada cosa: el perfil del grupo o el
   hecho de ser superusuario/staff.
2. Solo con --aplicar y ÚNICAMENTE si ya existe un Administrador técnico activo, con contraseña utilizable, retira
   is_superuser e is_staff a las cuentas que ya tienen un perfil funcional (Gerente, Jefe, Encargado, Solo Lectura,
   Solo Visualización). Sin ese Administrador técnico se niega a tocar nada: primero hay que crearlo y probarlo
   (`crear_admin_tecnico` + `changepassword`).
3. Nunca borra usuarios, datos financieros ni eventos. Las cuentas sin grupo NO se les asigna un perfil
   automáticamente: quedan sin perfil (no operan) hasta que la Gerente les asigne uno.
4. Se niega a dejar al sistema sin al menos una Gerente activa."""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from nucleo import contexto_solicitud
from nucleo.permisos import PERMISOS_POR_PERFIL, Perfil, Permiso, perfil_de

FUNCIONALES = {Perfil.GERENTE, Perfil.JEFE, Perfil.ENCARGADO, Perfil.SOLO_LECTURA}


def hay_administrador_tecnico_listo() -> list:
    return [
        u for u in get_user_model().objects.filter(is_active=True, is_staff=True)
        if perfil_de(u) == Perfil.ADMIN_TECNICO and u.has_usable_password()
    ]


class Command(BaseCommand):
    help = "Migra las cuentas existentes a los perfiles funcionales y retira el superusuario que ya no corresponde."

    def add_arguments(self, parser):
        parser.add_argument("--aplicar", action="store_true", help="Aplicar el plan (por defecto solo se informa).")
        parser.add_argument("--conservar", action="append", default=[], metavar="USUARIO",
                            help="Cuenta funcional a la que NO se le retira el superusuario (decisión expresa de la Gerente). Se puede repetir.")
        parser.add_argument("--actor", metavar="USUARIO", help="Administrador técnico que ejecuta la migración (queda en la bitácora).")

    def handle(self, *args, aplicar, actor, conservar=(), **opciones):
        modelo = get_user_model()
        cuentas = list(modelo.objects.order_by("username"))
        self.stdout.write("== Permisos efectivos de cada cuenta ==")
        plan = []
        for u in cuentas:
            perfil = perfil_de(u)
            permisos = sorted(p.value for p in PERMISOS_POR_PERFIL[perfil])
            hoy = []
            if u.is_superuser:
                hoy.append("superusuario (hasta ahora aprobaba, reabría, generaba archivos y autorizaba por serlo)")
            if u.is_staff:
                hoy.append("staff: acceso a /admin/ (edición de ventas, pagos, arqueos… por fuera de la app)")
            self.stdout.write(
                f"- {u.username} [{'activa' if u.is_active else 'INACTIVA'}] perfil: {perfil.value}\n"
                f"    permisos por perfil: {', '.join(permisos) or 'ninguno'}\n"
                f"    privilegios técnicos: {'; '.join(hoy) or 'ninguno'}"
            )
            if perfil in FUNCIONALES and (u.is_superuser or u.is_staff) and u.username not in conservar:
                plan.append(u)
            if perfil == Perfil.SIN_PERFIL and u.is_active:
                self.stdout.write(self.style.WARNING("    ! sin perfil: no operará ni consultará nada hasta que la Gerente le asigne uno."))

        gerentes = [u for u in cuentas if u.is_active and perfil_de(u) == Perfil.GERENTE]
        if not gerentes:
            raise CommandError("No hay ninguna Gerente activa: asigna ese perfil antes de migrar.")

        self.stdout.write("\n== Plan ==")
        if not plan:
            self.stdout.write("No hay cuentas funcionales con privilegios de superusuario/staff que retirar.")
        for u in plan:
            self.stdout.write(f"- retirar superusuario y staff a «{u.username}» (conserva su perfil {perfil_de(u).value})")

        listos = hay_administrador_tecnico_listo()
        self.stdout.write(f"\nAdministrador técnico listo (activo, staff, con contraseña): {', '.join(u.username for u in listos) or 'NINGUNO'}")
        if not aplicar:
            self.stdout.write(self.style.WARNING("\nInforme solamente: no se cambió nada. Para aplicar el plan: --aplicar"))
            return
        if not listos:
            raise CommandError(
                "No hay un Administrador técnico listo. Primero:\n"
                "  python manage.py crear_admin_tecnico NOMBRE\n"
                "  python manage.py changepassword NOMBRE\n"
                "y comprueba que puede entrar a /admin/. Después vuelve a ejecutar con --aplicar."
            )
        actor_usuario = modelo.objects.filter(username=actor).first() if actor else listos[0]
        if actor and actor_usuario is None:
            raise CommandError(f"No existe el usuario «{actor}».")
        ficha = contexto_solicitud.fijar(actor_usuario, solo_lectura=False)
        try:
            for u in plan:
                u.is_superuser = False
                u.is_staff = False
                u.save()
        finally:
            contexto_solicitud.restaurar(ficha)
        self.stdout.write(self.style.SUCCESS(f"Listo: {len(plan)} cuenta(s) sin privilegios técnicos. No se borró nada."))
