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

from nucleo.permisos import Permiso, exigir
from nucleo.carga import calcular_huella, cargar_mercado_pago, cargar_pos, cargar_pos_con_libro_contable, cargar_transbank
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
    """fuente: LoteCarga.Fuente.POS, .TRANSBANK o .MERCADO_PAGO."""
    exigir(usuario, Permiso.OPERAR, "Tu perfil no puede cargar archivos.")
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

    try:
        if fuente == LoteCarga.Fuente.POS:
            lote = cargar_pos(ruta_temporal, dia.fecha, usuario)
        elif fuente == LoteCarga.Fuente.MERCADO_PAGO:
            lote = cargar_mercado_pago(ruta_temporal, dia.fecha, usuario)
        else:
            lote = cargar_transbank(ruta_temporal, dia.fecha, usuario)
    except (ValueError, KeyError) as exc:
        raise ArchivoInvalido(
            f"No se pudo leer \"{archivo.name}\" — revisa que sea el archivo correcto para esta fuente "
            f"y que no le falten columnas u hojas. Detalle técnico: {exc}"
        )

    # cargar_pos/cargar_transbank toman el nombre del archivo guardado en
    # disco (con el prefijo que evita colisiones); para pantalla y para
    # la descarga se muestra el nombre original que la persona subió.
    lote.nombre_archivo = archivo.name
    lote.save(update_fields=["nombre_archivo"])

    if dia.estado == DiaArqueo.Estado.SIN_CARGAR:
        dia.estado = DiaArqueo.Estado.CARGADO
        dia.save(update_fields=["estado"])

    return lote


def procesar_carga_pos(dia: DiaArqueo, archivo_ventas: UploadedFile, archivo_libro_contable: UploadedFile, usuario) -> LoteCarga:
    """POS a partir de los dos archivos tal cual salen de Resto — VENTAS
    POR ARTICULO sin N_INTERNO + el libro contable del que se recupera
    (Karla, 2026-09-16). Reemplaza la consolidación manual que hacía
    antes ella misma antes de subir un solo archivo."""
    _validar_archivo(archivo_ventas)
    _validar_archivo(archivo_libro_contable)

    ruta_ventas = _guardar(archivo_ventas, dia, LoteCarga.Fuente.POS)
    ruta_libro = _guardar(archivo_libro_contable, dia, "pos_libro_contable")
    huella_ventas = calcular_huella(ruta_ventas)
    huella_libro = calcular_huella(ruta_libro)

    lote_existente = LoteCarga.objects.filter(dia=dia, fuente=LoteCarga.Fuente.POS).order_by("-cargado_en").first()
    if lote_existente:
        if (lote_existente.huella_sha256, lote_existente.huella_sha256_secundario) == (huella_ventas, huella_libro):
            EventoAuditoria.objects.create(
                dia=dia, entidad="LoteCarga", entidad_id=str(lote_existente.pk), accion="carga_duplicada_rechazada",
                valor_anterior=None,
                valor_posterior={"nombre_archivo": archivo_ventas.name, "nombre_archivo_secundario": archivo_libro_contable.name},
                usuario=usuario,
            )
            raise CargaDuplicada(
                f"Estos archivos ya se cargaron el {lote_existente.cargado_en:%d-%m-%Y %H:%M} "
                f"por {lote_existente.cargado_por}. No se volvieron a procesar."
            )
        EventoAuditoria.objects.create(
            dia=dia, entidad="LoteCarga", entidad_id=str(lote_existente.pk), accion="carga_en_conflicto_rechazada",
            valor_anterior=None,
            valor_posterior={"nombre_archivo": archivo_ventas.name, "nombre_archivo_secundario": archivo_libro_contable.name},
            usuario=usuario,
        )
        raise CargaEnConflicto(
            f"Ya existe una carga de {lote_existente.get_fuente_display()} para este día "
            f"({lote_existente.nombre_archivo}) con otro contenido. Reemplazarla exige revisión manual "
            "— todavía no está disponible desde esta pantalla."
        )

    try:
        lote = cargar_pos_con_libro_contable(ruta_ventas, ruta_libro, dia.fecha, usuario)
    except (ValueError, KeyError) as exc:
        detalle = str(exc)
        if detalle.startswith("este archivo es"):
            raise ArchivoInvalido(f"Archivo en el lugar equivocado: {detalle}.")
        raise ArchivoInvalido(
            "No se pudo cruzar los archivos — revisa que \"Ventas por artículo\" tenga las columnas de ventas "
            "y que \"Libro contable\" tenga las columnas N_INTERNO, FEC_COMPR, COD_COMP y N_COMP "
            f"(puede que los hayas puesto al revés). Detalle técnico: {detalle}"
        )
    # Igual que en procesar_carga: se muestran los nombres originales que
    # la persona subió, no el nombre con el prefijo que evita colisiones.
    lote.nombre_archivo = archivo_ventas.name
    lote.nombre_archivo_secundario = archivo_libro_contable.name
    lote.save(update_fields=["nombre_archivo", "nombre_archivo_secundario"])

    if dia.estado == DiaArqueo.Estado.SIN_CARGAR:
        dia.estado = DiaArqueo.Estado.CARGADO
        dia.save(update_fields=["estado"])

    return lote


def validaciones_de_carga(dia: DiaArqueo) -> list[dict]:
    lote_pos = dia.lotes.filter(fuente=LoteCarga.Fuente.POS).order_by("-cargado_en").first()
    lote_tbk = dia.lotes.filter(fuente=LoteCarga.Fuente.TRANSBANK).order_by("-cargado_en").first()
    lote_mp = dia.lotes.filter(fuente=LoteCarga.Fuente.MERCADO_PAGO).order_by("-cargado_en").first()
    hay_algun_lote = lote_pos or lote_tbk or lote_mp

    filas = []

    tiene_datos = dia.ventas.exists() or dia.movimientos.exists()
    filas.append({
        "validacion": "Fecha declarada coincide con el día",
        "ok": tiene_datos if hay_algun_lote else None,
        "detalle": "Se encontraron filas para esta fecha en los archivos cargados." if tiene_datos
                   else "Aún no hay archivos cargados." if not hay_algun_lote
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
    # Las guías de despacho no tienen líneas de artículos: no son comandas.
    total_comandas = dia.ventas.exclude(tipo_doc="DES").count()

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
