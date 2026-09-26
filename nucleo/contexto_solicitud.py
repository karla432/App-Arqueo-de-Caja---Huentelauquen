"""Estado de la petición en curso, visible desde los servicios sin pasarlo por cada función:

* `solo_lectura_activo()` — la persona es de Solo Lectura / Solo Visualización: ninguna lectura de
  pantalla puede escribir en la base de datos (ni siquiera datos derivados como el efectivo teórico).
* `usuario_actual()` — quién hace la petición, para registrar en la bitácora cambios que ocurren fuera de
  una vista (por ejemplo, cambios de grupos hechos desde el sitio de administración).
"""
from __future__ import annotations

from contextvars import ContextVar

_solo_lectura: ContextVar[bool] = ContextVar("nucleo_solo_lectura", default=False)
_usuario: ContextVar[object | None] = ContextVar("nucleo_usuario_actual", default=None)


def fijar(usuario, solo_lectura: bool):
    return _solo_lectura.set(solo_lectura), _usuario.set(usuario)


def restaurar(fichas) -> None:
    _solo_lectura.reset(fichas[0])
    _usuario.reset(fichas[1])


def solo_lectura_activo() -> bool:
    return _solo_lectura.get()


def usuario_actual():
    return _usuario.get()
