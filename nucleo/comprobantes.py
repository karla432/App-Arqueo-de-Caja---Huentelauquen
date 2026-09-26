"""Validación de comprobantes adjuntos (Karla, 2026-09-22, a pedido de
Seguridad y Riesgo): la extensión del archivo la elige quien lo sube — no
basta para confirmar qué es. Se revisan también los primeros bytes reales
del archivo ("firma" o "magic bytes") contra lo que la extensión promete,
para un PDF/JPG/PNG disfrazado de otra cosa. Comparte esta regla Control de
Depósitos y Reintegros de Caja — los dos únicos lugares de la app donde se
sube un comprobante."""
import io
import re
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


# Elementos «activos» de un PDF (Karla, 2026-09-26): un comprobante escaneado no necesita ejecutar nada.
_PDF_ACTIVO = (b"/javascript", b"/js", b"/launch", b"/embeddedfile", b"/richmedia", b"/submitform")
MAX_PIXELES = 60_000_000  # contra imágenes «bomba» (enormes en pantalla, pequeñas en disco)


class ComprobanteInvalido(Exception):
    pass


def _sin_ofuscar(contenido: bytes) -> bytes:
    """Los nombres de un PDF pueden escribirse con #xx (por ejemplo /J#61vaScript): se decodifican antes de buscar."""
    return re.sub(rb"#([0-9a-fA-F]{2})", lambda m: bytes([int(m.group(1), 16)]), contenido).lower()


def _revisar_contenido(archivo: UploadedFile, extension: str) -> None:
    archivo.seek(0)
    contenido = archivo.read()
    archivo.seek(0)
    if extension == ".pdf":
        plano = _sin_ofuscar(contenido)
        if any(re.search(re.escape(marca) + rb"(?![a-z])", plano) for marca in _PDF_ACTIVO):
            raise ComprobanteInvalido(
                "El PDF contiene elementos activos (scripts o archivos incrustados) y no se acepta. "
                "Vuelve a generarlo como imagen o PDF simple."
            )
        return
    try:
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = MAX_PIXELES
        with Image.open(io.BytesIO(contenido)) as imagen:
            imagen.verify()
    except Exception:
        raise ComprobanteInvalido("La imagen está dañada o no es una imagen válida.")


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
    _revisar_contenido(archivo, extension)
