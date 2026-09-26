"""Defensas para los archivos que entran y salen de la app (Karla, 2026-09-26; informe de seguridad).

ENTRADA (Excel de Resto, Transbank y Mercado Pago que sube la Encargada):
  * Solo `.xlsx` reales: se revisa el contenido, no solo la extensión (firma ZIP).
  * Sin macros (`vbaProject.bin`), sin objetos incrustados (OLE), sin rutas raras dentro del archivo.
  * Contra «bombas» de compresión: tope de tamaño descomprimido, de cantidad de partes y de razón de compresión.
  * Los importadores leen solo VALORES (pandas abre con `data_only=True` y `keep_links=False`): jamás se evalúa
    una fórmula ni se sigue un vínculo externo.

SALIDA (Excel que se descargan o se importan a Odoo):
  * «Inyección de fórmulas»: un texto que viene de un archivo cargado o de un formulario (cliente, artículo,
    glosa, comentario…) y que empieza con `=`, `+`, `-` o `@` se ejecutaría como fórmula al abrirlo en Excel.
    Antes de guardar cualquier libro, esos textos se marcan como texto (se antepone una comilla simple), que es la
    defensa estándar. Los números y las fechas no se tocan."""
from __future__ import annotations

import re
import zipfile
from pathlib import Path

from django.core.exceptions import SuspiciousFileOperation
from django.core.files.uploadedfile import UploadedFile
from django.utils.text import get_valid_filename

MAX_PARTES = 400
MAX_DESCOMPRIMIDO_MB = 400
MAX_RAZON_COMPRESION = 150          # descomprimido / comprimido de una parte
PARTES_PROHIBIDAS = ("xl/vbaproject.bin", "xl/embeddings/", "xl/activex/")
INICIOS_DE_FORMULA = ("=", "+", "-", "@", "\t", "\r")
_NUMERICO = re.compile(r"^[+-]?(\d+([.,]\d*)?|[.,]\d+)([eE][+-]?\d+)?$")


class ExcelInvalido(Exception):
    pass


def nombre_seguro(nombre: str) -> str:
    """Nombre de archivo sin carpetas ni caracteres raros (el original se guarda aparte para mostrarlo)."""
    base = Path(str(nombre).replace("\\", "/")).name
    try:
        limpio = get_valid_filename(base)
    except SuspiciousFileOperation:
        limpio = ""
    return limpio[-120:] or "archivo"


# --------------------------------------------------------------------------------------- entrada

def validar_xlsx_subido(archivo: UploadedFile) -> None:
    """Lanza `ExcelInvalido` con un mensaje para la persona si el archivo no es un .xlsx confiable."""
    if Path(archivo.name).suffix.lower() != ".xlsx":
        raise ExcelInvalido(f"Tipo de archivo no permitido ({Path(archivo.name).suffix.lower() or 'sin extensión'}). Debe ser .xlsx.")
    archivo.seek(0)
    if archivo.read(4) != b"PK\x03\x04":
        archivo.seek(0)
        raise ExcelInvalido("El contenido no corresponde a un Excel .xlsx real (¿fue renombrado o está corrupto?).")
    archivo.seek(0)
    try:
        with zipfile.ZipFile(archivo) as z:
            partes = z.infolist()
            if len(partes) > MAX_PARTES:
                raise ExcelInvalido("El archivo tiene una estructura interna anormal (demasiadas partes).")
            total = 0
            for parte in partes:
                nombre = parte.filename.replace("\\", "/")
                if nombre.startswith("/") or ".." in nombre.split("/"):
                    raise ExcelInvalido("El archivo contiene rutas internas no permitidas.")
                minusculas = nombre.lower()
                if any(minusculas == p or minusculas.startswith(p) for p in PARTES_PROHIBIDAS):
                    raise ExcelInvalido("El archivo contiene macros u objetos incrustados: no se aceptan. Guárdalo como .xlsx simple.")
                total += parte.file_size
                if parte.compress_size and parte.file_size / parte.compress_size > MAX_RAZON_COMPRESION and parte.file_size > 5_000_000:
                    raise ExcelInvalido("El archivo está comprimido de forma anormal y se rechazó por seguridad.")
            if total > MAX_DESCOMPRIMIDO_MB * 1024 * 1024:
                raise ExcelInvalido(f"El archivo se expande a más de {MAX_DESCOMPRIMIDO_MB} MB: se rechazó por seguridad.")
            if not any(p.filename == "xl/workbook.xml" for p in partes):
                raise ExcelInvalido("El archivo no tiene la estructura de un libro de Excel.")
    except zipfile.BadZipFile:
        raise ExcelInvalido("El archivo está dañado o no es un Excel .xlsx válido.")
    finally:
        archivo.seek(0)


# --------------------------------------------------------------------------------------- salida

def texto_seguro(valor):
    """Si es un texto que Excel podría ejecutar como fórmula, lo marca como texto; cualquier otro valor pasa igual."""
    if isinstance(valor, str) and len(valor) > 1 and valor.startswith(INICIOS_DE_FORMULA) and not _NUMERICO.match(valor.strip()):
        return "'" + valor
    return valor


def neutralizar_libro(wb) -> int:
    """Recorre todas las hojas y neutraliza los textos-fórmula. Devuelve cuántas celdas tocó."""
    tocadas = 0
    for ws in wb.worksheets:
        for fila in ws.iter_rows():
            for celda in fila:
                v = celda.value
                if isinstance(v, str) and len(v) > 1 and v.startswith(INICIOS_DE_FORMULA):
                    nuevo = texto_seguro(v)
                    if nuevo is not v:
                        celda.value = nuevo
                        celda.data_type = "s"  # texto, aunque empiece con "=": no queda como fórmula
                        tocadas += 1
    return tocadas


def guardar_libro(wb, ruta) -> None:
    """Único punto de guardado de los Excel que genera la app."""
    neutralizar_libro(wb)
    wb.save(ruta)
