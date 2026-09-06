"""Carga de archivos del día (diseño funcional §8, M1, Pantalla 3).

Control #14: los archivos se validan (tipo, tamaño, estructura) antes de
procesarse y se guardan fuera de cualquier ruta servida al navegador.
Control C6: el original es inmutable — una recarga con el mismo archivo
se detecta y no se reprocesa; una recarga con un archivo distinto para un
día que ya tiene datos se rechaza explícitamente en esta primera versión
(ver nota en `procesar_carga`) en vez de improvisar un reemplazo parcial.
"""
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.files.uploadedfile import UploadedFile

from nucleo.carga import calcular_huella, cargar_pos, cargar_transbank
from nucleo.models import DiaArqueo, EventoAuditoria, LoteCarga

EXTENSIONES_PERMITIDAS = {".xlsx"}
TAMANO_MAXIMO_MB = 30

CARPETA_CARGAS = Path(settings.BASE_DIR) / "archivos_cargados"


class ArchivoInvalido(Exception):
    pass


class CargaDuplicada(Exception):
    """El mismo archivo (misma huella) ya se cargó para este día y fuente."""


class CargaEnConflicto(Exception):
    """Ya existe una carga distinta para este día y fuente — no se
    reemplaza automáticamente (§8, caso de prueba 15: el operador debe
    elegir explícitamente, y esta primera versión todavía no ofrece un
    reemplazo seguro en pantalla)."""


def _validar_archivo(archivo: UploadedFile) -> None:
    extension = Path(archivo.name).suffix.lower()
    if extension not in EXTENSIONES_PERMITIDAS:
        raise ArchivoInvalido(f"Tipo de archivo no permitido ({extension or 'sin extensión'}). Debe ser .xlsx.")
    if archivo.size > TAMANO_MAXIMO_MB * 1024 * 1024:
        raise ArchivoInvalido(f"El archivo pesa más de {TAMANO_MAXIMO_MB} MB.")
    if archivo.size == 0:
        raise ArchivoInvalido("El archivo está vacío.")


def _guardar(archivo: UploadedFile, dia: DiaArqueo, fuente: str) -> Path:
    carpeta = CARPETA_CARGAS / dia.fecha.isoformat()
    carpeta.mkdir(parents=True, exist_ok=True)
    marca = datetime.now().strftime("%Y%m%d%H%M%S")
    nombre_seguro = f"{fuente}_{marca}_{Path(archivo.name).name}"
    ruta = carpeta / nombre_seguro
    with open(ruta, "wb") as destino:
        for parte in archivo.chunks():
            destino.write(parte)
    return ruta


def procesar_carga(dia: DiaArqueo, archivo: UploadedFile, fuente: str, usuario) -> LoteCarga:
    """fuente: LoteCarga.Fuente.POS o .TRANSBANK."""
    _validar_archivo(archivo)

    ruta_temporal = _guardar(archivo, dia, fuente)
    huella = calcular_huella(ruta_temporal)

    lote_existente = LoteCarga.objects.filter(dia=dia, fuente=fuente).order_by("-cargado_en").first()
    if lote_existente:
        if lote_existente.huella_sha256 == huella:
            EventoAuditoria.objects.create(
                dia=dia, entidad="LoteCarga", entidad_id=str(lote_existente.pk), accion="carga_duplicada_rechazada",
                valor_anterior=None, valor_posterior={"nombre_archivo": archivo.name}, usuario=usuario,
            )
            raise CargaDuplicada(
                f"Este archivo ya se cargó el {lote_existente.cargado_en:%d-%m-%Y %H:%M} "
                f"por {lote_existente.cargado_por}. No se volvió a procesar."
            )
        EventoAuditoria.objects.create(
            dia=dia, entidad="LoteCarga", entidad_id=str(lote_existente.pk), accion="carga_en_conflicto_rechazada",
            valor_anterior=None, valor_posterior={"nombre_archivo": archivo.name}, usuario=usuario,
        )
        raise CargaEnConflicto(
            f"Ya existe una carga de {lote_existente.get_fuente_display()} para este día "
            f"({lote_existente.nombre_archivo}) con otro contenido. Reemplazarla exige revisión manual "
            "— todavía no está disponible desde esta pantalla."
        )

    if fuente == LoteCarga.Fuente.POS:
        lote = cargar_pos(ruta_temporal, dia.fecha, usuario)
    else:
        lote = cargar_transbank(ruta_temporal, dia.fecha, usuario)

    # cargar_pos/cargar_transbank toman el nombre del archivo guardado en
    # disco (con el prefijo que evita colisiones); para pantalla y para
    # la descarga se muestra el nombre original que la persona subió.
    lote.nombre_archivo = archivo.name
    lote.save(update_fields=["nombre_archivo"])

    if dia.estado == DiaArqueo.Estado.SIN_CARGAR:
        dia.estado = DiaArqueo.Estado.CARGADO
        dia.save(update_fields=["estado"])

    return lote


def validaciones_de_carga(dia: DiaArqueo) -> list[dict]:
    lote_pos = dia.lotes.filter(fuente=LoteCarga.Fuente.POS).order_by("-cargado_en").first()
    lote_tbk = dia.lotes.filter(fuente=LoteCarga.Fuente.TRANSBANK).order_by("-cargado_en").first()

    filas = []

    tiene_datos = dia.ventas.exists() or dia.movimientos.exists()
    filas.append({
        "validacion": "Fecha declarada coincide con el día",
        "ok": tiene_datos if (lote_pos or lote_tbk) else None,
        "detalle": "Se encontraron filas para esta fecha en los archivos cargados." if tiene_datos
                   else "Aún no hay archivos cargados." if not (lote_pos or lote_tbk)
                   else "Ningún archivo cargado trae filas de esta fecha.",
    })

    filas.append({
        "validacion": "Carga duplicada",
        "ok": True,
        "detalle": "Sin duplicados: cada huella se compara antes de procesar.",
    })

    filas.append({
        "validacion": "Filas no interpretadas",
        "ok": True,
        "detalle": "0 — ningún importador descarta filas (regla de conservación, §8).",
    })

    avisos_pos = lote_pos.avisos if lote_pos else []
    avisos_lineas = [a for a in avisos_pos if "suma de líneas" in a]
    avisos_medios = [a for a in avisos_pos if "suma de medios" in a]
    total_comandas = dia.ventas.count()

    filas.append({
        "validacion": "Total de comanda = suma de líneas",
        "ok": not avisos_lineas if lote_pos else None,
        "detalle": (
            f"{total_comandas - len(avisos_lineas)} de {total_comandas} comandas cuadran"
            if lote_pos else "Sin archivo POS cargado."
        ),
    })
    filas.append({
        "validacion": "Total = suma de medios declarados",
        "ok": not avisos_medios if lote_pos else None,
        "detalle": (
            f"{total_comandas - len(avisos_medios)} de {total_comandas} comandas cuadran"
            if lote_pos else "Sin archivo POS cargado."
        ),
    })

    filas.append({
        "validacion": "Contradicción entre hojas POS",
        "ok": None,
        "detalle": "No evaluado — depende de VENTA CONSOLIDADA, pendiente de la decisión D1.",
    })

    return filas
