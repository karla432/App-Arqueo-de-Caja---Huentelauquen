"""Validación de comprobantes adjuntos (Karla, 2026-09-22, a pedido de
Seguridad y Riesgo): la extensión del archivo la elige quien lo sube — no
basta para confirmar qué es. Se revisan también los primeros bytes reales
del archivo ("firma" o "magic bytes") contra lo que la extensión promete,
para un PDF/JPG/PNG disfrazado de otra cosa. Comparte esta regla Control de
Depósitos y Reintegros de Caja — los dos únicos lugares de la app donde se
sube un comprobante."""
from pathlib import Path

from django.core.files.uploadedfile import UploadedFile

EXTENSIONES_PERMITIDAS = {".pdf", ".jpg", ".jpeg", ".png"}
TAMANO_MAXIMO_MB = 15

_FIRMAS = {
    ".pdf": (b"%PDF-",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".png": (b"\x89PNG\r\n\x1a\n",),
}


class ComprobanteInvalido(Exception):
    pass


def validar_comprobante(archivo: UploadedFile) -> None:
    extension = Path(archivo.name).suffix.lower()
    if extension not in EXTENSIONES_PERMITIDAS:
        raise ComprobanteInvalido(
            f"Tipo de archivo no permitido ({extension or 'sin extensión'}). Debe ser PDF, JPG o PNG."
        )
    if archivo.size > TAMANO_MAXIMO_MB * 1024 * 1024:
        raise ComprobanteInvalido(f"El archivo pesa más de {TAMANO_MAXIMO_MB} MB.")
    if archivo.size == 0:
        raise ComprobanteInvalido("El archivo está vacío.")

    inicio = archivo.read(16)
    archivo.seek(0)
    firmas = _FIRMAS[extension]
    if not any(inicio.startswith(firma) for firma in firmas):
        raise ComprobanteInvalido(
            f"El contenido del archivo no corresponde a un {extension[1:].upper()} real — "
            "revisa que no esté corrupto o mal renombrado."
        )
