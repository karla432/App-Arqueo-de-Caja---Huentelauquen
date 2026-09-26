"""Validación de comprobantes (Karla, 2026-09-22, a pedido de Seguridad y
Riesgo): además de la extensión, se revisan los primeros bytes reales del
archivo — evita un .exe o script disfrazado de PDF con solo cambiarle el
nombre. Compartida por Control de Depósitos y Reintegros de Caja."""
from django.core.files.uploadedfile import SimpleUploadedFile

import pytest

from nucleo.comprobantes import ComprobanteInvalido, TAMANO_MAXIMO_MB, validar_comprobante


def _imagen(formato: str) -> bytes:
    """Una imagen de verdad (desde 2026-09-26 no basta con los primeros bytes: la imagen se abre y se verifica)."""
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buf, formato)
    return buf.getvalue()


def test_un_pdf_real_pasa():
    archivo = SimpleUploadedFile("x.pdf", b"%PDF-1.4 resto del archivo", content_type="application/pdf")
    validar_comprobante(archivo)  # no lanza


def test_un_jpg_real_pasa():
    archivo = SimpleUploadedFile("x.jpg", _imagen("JPEG"), content_type="image/jpeg")
    validar_comprobante(archivo)


def test_un_png_real_pasa():
    archivo = SimpleUploadedFile("x.png", _imagen("PNG"), content_type="image/png")
    validar_comprobante(archivo)


def test_extension_no_permitida_se_rechaza():
    archivo = SimpleUploadedFile("x.exe", b"MZ resto", content_type="application/octet-stream")
    with pytest.raises(ComprobanteInvalido, match="no permitido"):
        validar_comprobante(archivo)


def test_un_archivo_renombrado_con_extension_de_pdf_pero_contenido_distinto_se_rechaza():
    """El caso que Seguridad pidió cubrir: alguien sube un .exe pero le
    cambia el nombre a "comprobante.pdf" antes de subirlo."""
    archivo = SimpleUploadedFile("comprobante.pdf", b"MZ\x90\x00\x03 esto no es un pdf", content_type="application/pdf")
    with pytest.raises(ComprobanteInvalido, match="no corresponde"):
        validar_comprobante(archivo)


def test_archivo_vacio_se_rechaza():
    archivo = SimpleUploadedFile("x.pdf", b"", content_type="application/pdf")
    with pytest.raises(ComprobanteInvalido, match="vacío"):
        validar_comprobante(archivo)


def test_archivo_demasiado_pesado_se_rechaza():
    archivo = SimpleUploadedFile("x.pdf", b"%PDF-" + b"0" * (TAMANO_MAXIMO_MB * 1024 * 1024 + 1), content_type="application/pdf")
    with pytest.raises(ComprobanteInvalido, match="MB"):
        validar_comprobante(archivo)
