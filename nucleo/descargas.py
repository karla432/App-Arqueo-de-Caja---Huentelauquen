"""Entrega de archivos financieros con registro de quién los descarga (Karla, 2026-09-26).

Toda descarga pasa por `entregar_archivo`: deja un evento `descargar_archivo` en la bitácora (persona, perfil, qué
archivo y de qué día) y responde con el archivo. Ninguna descarga escribe datos de negocio: los archivos se
generan por una acción aparte (POST) y acá solo se entregan los que ya existen.

Registrar la descarga es una escritura en la bitácora, no en los datos: se hace también para los perfiles de
solo lectura, porque saber quién bajó un archivo financiero es justamente lo que se quiere auditar."""
from __future__ import annotations

from pathlib import Path

from django.contrib import messages
from django.http import FileResponse
from django.shortcuts import redirect

from nucleo.models import EventoAuditoria
from nucleo.permisos import perfil_de


def entregar_archivo(request, ruta: Path, nombre: str, *, entidad: str, entidad_id, dia=None, tipo: str, detalle: dict | None = None):
    EventoAuditoria.objects.create(
        dia=dia, entidad=entidad, entidad_id=str(entidad_id), accion="descargar_archivo",
        valor_anterior=None,
        valor_posterior={"tipo": tipo, "archivo": nombre, "perfil": perfil_de(request.user).value, **(detalle or {})},
        usuario=request.user, origen=request.META.get("REMOTE_ADDR", "")[:100],
    )
    return FileResponse(open(ruta, "rb"), as_attachment=True, filename=nombre)


def archivo_no_disponible(request, mensaje: str, destino: str = "exportaciones_odoo"):
    messages.error(request, mensaje)
    return redirect(destino)
