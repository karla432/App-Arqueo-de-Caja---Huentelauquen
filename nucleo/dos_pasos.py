"""Verificación en dos pasos con app de autenticación (TOTP) — Karla, 2026-09-26.

* Obligatoria para Gerente, Jefe de Contabilidad y Administrador técnico cuando `DOS_PASOS_OBLIGATORIO` está activo
  (por defecto: en producción). En desarrollo no molesta.
* Al primer ingreso se activa: la persona escanea un QR con Google/Microsoft Authenticator, confirma con un código y
  recibe 8 códigos de recuperación de un solo uso (se muestran una vez; solo se guarda su huella).
* En cada ingreso, después de la contraseña, se pide el código de 6 dígitos. Un código no se puede usar dos veces.
* Los intentos fallidos cuentan para el mismo bloqueo temporal que las contraseñas (5 fallos, 15 minutos).
* Si se pierde el teléfono: código de recuperación, o `manage.py recuperar_acceso USUARIO --reiniciar-dos-pasos` desde la
  consola del servidor.
* Todo queda en la bitácora. El secreto TOTP se guarda en la base de datos tal cual (hace falta para calcular el
  código): por eso los respaldos deben protegerse como la base misma."""
from __future__ import annotations

import hashlib
import io
import secrets

import pyotp
import qrcode
import qrcode.image.svg
from django.conf import settings
from django.utils import timezone

from nucleo import bloqueo_acceso
from nucleo.models import CodigoDeRecuperacion, DispositivoTotp, EventoAuditoria
from nucleo.permisos import Perfil, perfil_de

PERFILES_CON_DOS_PASOS = {Perfil.GERENTE, Perfil.JEFE, Perfil.ADMIN_TECNICO}
CANTIDAD_DE_CODIGOS = 8
EMISOR = "Arqueo Diario Huentelauquén"
CLAVE_DE_SESION = "dos_pasos_ok"


class DosPasosInvalido(Exception):
    pass


def es_obligatorio() -> bool:
    return bool(getattr(settings, "DOS_PASOS_OBLIGATORIO", False))


def requiere_verificacion(usuario) -> bool:
    """¿Esta persona debe pasar por el segundo paso en esta instalación?"""
    return es_obligatorio() and perfil_de(usuario) in PERFILES_CON_DOS_PASOS


def tiene_activo(usuario) -> bool:
    return DispositivoTotp.objects.filter(usuario=usuario, confirmado=True).exists()


def _bitacora(usuario, accion, detalle=None, por=None):
    EventoAuditoria.objects.create(
        dia=None, entidad="Usuario", entidad_id=str(usuario.pk), accion=accion,
        valor_posterior={"usuario": usuario.username, **(detalle or {})}, usuario=por or usuario,
    )


def _huella(codigo: str) -> str:
    return hashlib.sha256(codigo.strip().replace("-", "").replace(" ", "").lower().encode()).hexdigest()


def _clave_de_bloqueo(usuario) -> str:
    return f"2fa:{usuario.get_username()}"


# ------------------------------------------------------------------------------ activar

def iniciar_activacion(usuario) -> DispositivoTotp:
    """Crea (o reinicia, si aún no estaba confirmado) el secreto de la persona. No sirve para cambiar un doble factor ya activo."""
    if tiene_activo(usuario):
        raise DosPasosInvalido("Ya tienes la verificación en dos pasos activa.")
    dispositivo, _ = DispositivoTotp.objects.get_or_create(usuario=usuario, defaults={"secreto": pyotp.random_base32()})
    if dispositivo.confirmado is False:
        dispositivo.secreto = dispositivo.secreto or pyotp.random_base32()
        dispositivo.save(update_fields=["secreto"])
    return dispositivo


def uri_de_aprovisionamiento(dispositivo: DispositivoTotp) -> str:
    return pyotp.TOTP(dispositivo.secreto).provisioning_uri(name=dispositivo.usuario.get_username(), issuer_name=EMISOR)


def qr_svg(uri: str) -> str:
    buf = io.BytesIO()
    qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, box_size=8).save(buf)
    return buf.getvalue().decode("utf-8")


def _paso_valido(dispositivo: DispositivoTotp, codigo: str) -> int | None:
    """Paso de tiempo al que corresponde el código (con una tolerancia de ±30 s), o None."""
    codigo = (codigo or "").strip().replace(" ", "")
    if not (codigo.isdigit() and len(codigo) == 6):
        return None
    totp = pyotp.TOTP(dispositivo.secreto)
    ahora = timezone.now().timestamp()
    for desfase in (0, -1, 1):
        instante = ahora + desfase * totp.interval
        if secrets.compare_digest(totp.at(instante), codigo):
            return int(instante // totp.interval)
    return None


def confirmar_activacion(usuario, codigo: str) -> list[str]:
    """Confirma con un código de la app y devuelve los códigos de recuperación EN CLARO (única vez que existen)."""
    dispositivo = DispositivoTotp.objects.filter(usuario=usuario).first()
    if dispositivo is None or dispositivo.confirmado:
        raise DosPasosInvalido("Empieza de nuevo la activación.")
    paso = _paso_valido(dispositivo, codigo)
    if paso is None:
        raise DosPasosInvalido("El código no coincide. Revisa que el reloj del teléfono esté en hora y prueba con el código vigente.")
    dispositivo.confirmado = True
    dispositivo.ultimo_paso = paso
    dispositivo.save(update_fields=["confirmado", "ultimo_paso"])
    CodigoDeRecuperacion.objects.filter(usuario=usuario).delete()
    codigos = [f"{secrets.token_hex(3)}-{secrets.token_hex(3)}" for _ in range(CANTIDAD_DE_CODIGOS)]
    CodigoDeRecuperacion.objects.bulk_create([CodigoDeRecuperacion(usuario=usuario, huella=_huella(c)) for c in codigos])
    _bitacora(usuario, "dos_pasos_activado")
    return codigos


# ------------------------------------------------------------------------------ verificar en cada ingreso

def verificar(usuario, codigo: str, origen: str = "") -> str:
    """Devuelve "totp" o "recuperacion" si el código sirve; lanza DosPasosInvalido si no o si está bloqueado."""
    clave = _clave_de_bloqueo(usuario)
    minutos = bloqueo_acceso.minutos_restantes(clave)
    if minutos:
        raise DosPasosInvalido(f"Demasiados intentos fallidos. Vuelve a intentar en {minutos} minuto(s).")
    dispositivo = DispositivoTotp.objects.filter(usuario=usuario, confirmado=True).first()
    if dispositivo is None:
        raise DosPasosInvalido("Aún no activaste la verificación en dos pasos.")

    paso = _paso_valido(dispositivo, codigo)
    if paso is not None and paso > dispositivo.ultimo_paso:
        dispositivo.ultimo_paso = paso
        dispositivo.save(update_fields=["ultimo_paso"])
        bloqueo_acceso.limpiar(clave)
        return "totp"

    huella = _huella(codigo or "")
    recuperacion = CodigoDeRecuperacion.objects.filter(usuario=usuario, huella=huella, usado_en__isnull=True).first()
    if recuperacion is not None:
        recuperacion.usado_en = timezone.now()
        recuperacion.save(update_fields=["usado_en"])
        bloqueo_acceso.limpiar(clave)
        restantes = CodigoDeRecuperacion.objects.filter(usuario=usuario, usado_en__isnull=True).count()
        _bitacora(usuario, "dos_pasos_codigo_de_recuperacion_usado", {"codigos_restantes": restantes})
        return "recuperacion"

    se_bloquea = bloqueo_acceso.registrar_fallo(clave, origen)
    _bitacora(usuario, "dos_pasos_verificacion_fallida", {"bloqueada": se_bloquea})
    if se_bloquea:
        raise DosPasosInvalido(f"Demasiados intentos fallidos. Vuelve a intentar en {bloqueo_acceso.MINUTOS_DE_BLOQUEO} minutos.")
    raise DosPasosInvalido("El código no es correcto.")


def reiniciar(usuario, por=None) -> None:
    """Quita el doble factor de la persona (pierde el teléfono): al próximo ingreso lo activa de nuevo."""
    DispositivoTotp.objects.filter(usuario=usuario).delete()
    CodigoDeRecuperacion.objects.filter(usuario=usuario).delete()
    bloqueo_acceso.limpiar(_clave_de_bloqueo(usuario))
    _bitacora(usuario, "dos_pasos_reiniciado", por=por)
