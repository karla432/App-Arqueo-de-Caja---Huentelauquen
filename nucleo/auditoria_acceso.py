"""Registro de accesos y de cambios de perfil en la bitácora (Karla, 2026-09-26).

Deja un evento por cada inicio y cierre de sesión, por cada intento fallido contra una cuenta existente y por
cada cambio de grupos o de privilegios (superusuario, staff, activo) de una cuenta — vengan de la pantalla de
Configuración, del sitio de administración o de un comando. Los intentos fallidos contra usuarios que no existen
no caben en la bitácora (exige un usuario): quedan en el log de seguridad del servidor."""
from __future__ import annotations

import logging

from django.contrib.auth import get_user_model
from django.contrib.auth.models import User
from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.db.models.signals import m2m_changed, pre_save
from django.dispatch import receiver

from nucleo import bloqueo_acceso, contexto_solicitud
from nucleo.models import EventoAuditoria
from nucleo.permisos import perfil_de

registro = logging.getLogger("nucleo.seguridad")


def _ip(request) -> str:
    if request is None:
        return ""
    return (request.META.get("REMOTE_ADDR") or "")[:100]


@receiver(user_logged_in)
def registrar_inicio_de_sesion(sender, request, user, **kwargs):
    bloqueo_acceso.limpiar(user.get_username())
    EventoAuditoria.objects.create(
        dia=None, entidad="Usuario", entidad_id=str(user.pk), accion="iniciar_sesion",
        valor_posterior={"usuario": user.username, "perfil": perfil_de(user).value}, usuario=user, origen=_ip(request),
    )


@receiver(user_logged_out)
def registrar_cierre_de_sesion(sender, request, user, **kwargs):
    if user is None:
        return
    EventoAuditoria.objects.create(
        dia=None, entidad="Usuario", entidad_id=str(user.pk), accion="cerrar_sesion",
        valor_posterior={"usuario": user.username}, usuario=user, origen=_ip(request),
    )


@receiver(user_login_failed)
def registrar_inicio_fallido(sender, credentials, request=None, **kwargs):
    nombre = (credentials or {}).get("username", "")
    registro.warning("Inicio de sesión fallido para %r desde %s", nombre, _ip(request))
    usuario = get_user_model().objects.filter(username__iexact=nombre).first() if nombre else None
    ya_bloqueada = bloqueo_acceso.esta_bloqueado(nombre)
    se_bloquea = bloqueo_acceso.registrar_fallo(nombre, _ip(request))
    if usuario is not None and not ya_bloqueada:
        EventoAuditoria.objects.create(
            dia=None, entidad="Usuario", entidad_id=str(usuario.pk), accion="inicio_sesion_fallido",
            valor_posterior={"usuario": usuario.username}, usuario=usuario, origen=_ip(request),
        )
    if usuario is not None and se_bloquea:
        EventoAuditoria.objects.create(
            dia=None, entidad="Usuario", entidad_id=str(usuario.pk), accion="cuenta_bloqueada_por_intentos",
            valor_posterior={"usuario": usuario.username, "intentos": bloqueo_acceso.MAX_INTENTOS,
                             "minutos": bloqueo_acceso.MINUTOS_DE_BLOQUEO},
            usuario=usuario, origen=_ip(request),
        )
        registro.warning("Cuenta %r bloqueada %s minutos por intentos fallidos desde %s", nombre,
                         bloqueo_acceso.MINUTOS_DE_BLOQUEO, _ip(request))


@receiver(m2m_changed, sender=User.groups.through)
def registrar_cambio_de_grupos(sender, instance, action, pk_set, reverse, **kwargs):
    if action not in ("post_add", "post_remove", "post_clear") or reverse:
        return
    EventoAuditoria.objects.create(
        dia=None, entidad="Usuario", entidad_id=str(instance.pk), accion="cambiar_grupos_usuario",
        valor_posterior={"usuario": instance.username, "accion": action, "grupos_ids": sorted(pk_set or []),
                         "perfil_resultante": perfil_de(instance).value},
        usuario=contexto_solicitud.usuario_actual() or instance,
    )


@receiver(pre_save, sender=User)
def registrar_cambio_de_privilegios(sender, instance, **kwargs):
    if instance.pk is None:
        return
    anterior = User.objects.filter(pk=instance.pk).values("is_superuser", "is_staff", "is_active").first()
    if anterior is None:
        return
    cambios = {c: {"antes": anterior[c], "despues": getattr(instance, c)} for c in anterior if anterior[c] != getattr(instance, c)}
    if cambios:
        EventoAuditoria.objects.create(
            dia=None, entidad="Usuario", entidad_id=str(instance.pk), accion="cambiar_privilegios_usuario",
            valor_posterior={"usuario": instance.username, "cambios": cambios},
            usuario=contexto_solicitud.usuario_actual() or instance,
        )
