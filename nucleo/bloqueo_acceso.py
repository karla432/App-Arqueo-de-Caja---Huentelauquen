"""Bloqueo temporal por intentos fallidos de contraseña (Karla, 2026-09-26).

Cinco intentos fallidos seguidos contra el mismo usuario lo bloquean 15 minutos; el bloqueo se levanta solo. Mientras
está bloqueado ni la clave correcta entra, y los intentos hechos durante el bloqueo no lo alargan. Un ingreso
correcto borra la cuenta de fallos. Se cuenta por nombre de usuario, exista o no la cuenta, para no revelar qué
usuarios existen. El bloqueo de una cuenta real queda en la bitácora. Los fallos se guardan en la base de datos, así que
sobreviven a reinicios y valen para todos los procesos del servidor."""
from datetime import timedelta

from django.utils import timezone

MAX_INTENTOS = 5
MINUTOS_DE_BLOQUEO = 15


def _normalizar(nombre) -> str:
    return (nombre or "").strip().lower()[:150]


def _fallos_vigentes(nombre: str):
    from nucleo.models import IntentoFallido
    desde = timezone.now() - timedelta(minutes=MINUTOS_DE_BLOQUEO)
    return IntentoFallido.objects.filter(nombre=nombre, momento__gte=desde).order_by("-momento")


def minutos_restantes(nombre) -> int:
    """0 si la cuenta no está bloqueada; si lo está, los minutos (redondeados hacia arriba) que faltan."""
    nombre = _normalizar(nombre)
    if not nombre:
        return 0
    fallos = list(_fallos_vigentes(nombre).values_list("momento", flat=True)[:MAX_INTENTOS])
    if len(fallos) < MAX_INTENTOS:
        return 0
    libre_en = fallos[0] + timedelta(minutes=MINUTOS_DE_BLOQUEO)  # se cuenta desde el último de los fallos
    segundos = (libre_en - timezone.now()).total_seconds()
    return max(0, int(-(-segundos // 60)))


def esta_bloqueado(nombre) -> bool:
    return minutos_restantes(nombre) > 0


def registrar_fallo(nombre, origen: str = "") -> bool:
    """Suma un fallo. Devuelve True si con este la cuenta queda bloqueada. No suma si ya estaba bloqueada."""
    from nucleo.models import IntentoFallido
    nombre = _normalizar(nombre)
    if not nombre or esta_bloqueado(nombre):
        return False
    IntentoFallido.objects.create(nombre=nombre, origen=(origen or "")[:100])
    IntentoFallido.objects.filter(momento__lt=timezone.now() - timedelta(days=1)).delete()
    return esta_bloqueado(nombre)


def limpiar(nombre) -> None:
    from nucleo.models import IntentoFallido
    IntentoFallido.objects.filter(nombre=_normalizar(nombre)).delete()
