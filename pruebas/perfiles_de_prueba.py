"""Ayudas para que las pruebas creen usuarios con un perfil funcional (los grupos los crea la migración 0031)."""
from django.contrib.auth.models import Group

from nucleo.permisos import (
    GRUPO_ADMIN_TECNICO, GRUPO_ENCARGADO, GRUPO_GERENTE, GRUPO_JEFE, GRUPO_SOLO_LECTURA, GRUPO_SOLO_VISUALIZACION,
)

__all__ = [
    "crear_usuario", "GRUPO_ADMIN_TECNICO", "GRUPO_ENCARGADO", "GRUPO_GERENTE", "GRUPO_JEFE", "GRUPO_SOLO_LECTURA",
    "GRUPO_SOLO_VISUALIZACION",
]


def crear_usuario(modelo, username, grupo=None, password="x", **extra):
    usuario = modelo.objects.create_user(username=username, password=password, **extra)
    if grupo:
        usuario.groups.add(Group.objects.get_or_create(name=grupo)[0])
    return usuario
