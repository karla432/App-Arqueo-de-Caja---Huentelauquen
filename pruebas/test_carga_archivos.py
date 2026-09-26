"""Carga de archivos (Pantalla 3): validación de tipo/tamaño, detección de
duplicados y de conflictos, y las 6 validaciones de carga — usando los
mismos archivos reales del golden master, subidos de verdad a través del
formulario.
"""
from datetime import date
from pathlib import Path

import pytest
from pruebas.perfiles_de_prueba import GRUPO_ENCARGADO, crear_usuario
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from nucleo.carga_servicio import (
    ArchivoInvalido, CargaDuplicada, CargaEnConflicto, procesar_carga, procesar_carga_pos, validaciones_de_carga,
)
from nucleo.models import DiaArqueo, LoteCarga

FECHA = date(2026, 9, 2)
FIXTURES = Path(__file__).parent / "fixtures" / "dia_referencia_02-09-2026"
FECHA_MP = date(2026, 9, 10)
FIXTURE_MP = Path(__file__).parent / "fixtures" / "mercado_pago_10-09-2026" / "settlement_10-09-2026.xlsx"
FECHA_RESTO = date(2026, 9, 1)
FIXTURES_RESTO = Path(__file__).parent / "fixtures" / "dia_referencia_01-09-2026"


def _archivo_ventas_resto():
    contenido = (FIXTURES_RESTO / "ventas_por_articulo_01-09-2026.xlsx").read_bytes()
    return SimpleUploadedFile("ventas_por_articulo_01-09-2026.xlsx", contenido)


def _archivo_libro_contable_resto():
    contenido = (FIXTURES_RESTO / "libro_contable_01-09-2026.xlsx").read_bytes()
    return SimpleUploadedFile("libro_contable_01-09-2026.xlsx", contenido)


@pytest.fixture
def usuario(django_user_model):
    return crear_usuario(django_user_model, "operador", GRUPO_ENCARGADO, password="x")


@pytest.fixture
def dia(db):
    return DiaArqueo.objects.create(fecha=FECHA)


def _archivo_pos():
    contenido = (FIXTURES / "Captura_Ventas_Sala_de_Ventas_02.09.26.xlsx").read_bytes()
    return SimpleUploadedFile(
        "Captura_Ventas_Sala_de_Ventas_02.09.26.xlsx", contenido,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def _archivo_transbank():
    contenido = (FIXTURES / "Cartola_Movimientos_02-sep-2026.xlsx").read_bytes()
    return SimpleUploadedFile(
        "Cartola_Movimientos_02-sep-2026.xlsx", contenido,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def test_rechaza_extension_no_permitida(dia, usuario):
    archivo = SimpleUploadedFile("cosa.csv", b"a,b,c", content_type="text/csv")
    with pytest.raises(ArchivoInvalido):
        procesar_carga(dia, archivo, LoteCarga.Fuente.POS, usuario)


def test_rechaza_archivo_vacio(dia, usuario):
    archivo = SimpleUploadedFile("vacio.xlsx", b"", content_type="application/octet-stream")
    with pytest.raises(ArchivoInvalido):
        procesar_carga(dia, archivo, LoteCarga.Fuente.POS, usuario)


def test_carga_pos_real_de_punta_a_punta(dia, usuario, tmp_path, monkeypatch):
    import nucleo.carga_servicio as svc
    monkeypatch.setattr(svc, "CARPETA_CARGAS", tmp_path)

    lote = procesar_carga(dia, _archivo_pos(), LoteCarga.Fuente.POS, usuario)
    assert lote.fuente == LoteCarga.Fuente.POS
    dia.refresh_from_db()
    assert dia.estado == DiaArqueo.Estado.CARGADO
    assert dia.ventas.count() == 336


def test_recargar_el_mismo_archivo_se_detecta_como_duplicado(dia, usuario, tmp_path, monkeypatch):
    import nucleo.carga_servicio as svc
    monkeypatch.setattr(svc, "CARPETA_CARGAS", tmp_path)

    procesar_carga(dia, _archivo_pos(), LoteCarga.Fuente.POS, usuario)
    with pytest.raises(CargaDuplicada):
        procesar_carga(dia, _archivo_pos(), LoteCarga.Fuente.POS, usuario)
    assert dia.ventas.count() == 336  # no se duplicó nada


def test_cargar_archivo_distinto_para_el_mismo_dia_y_fuente_es_un_conflicto(dia, usuario, tmp_path, monkeypatch):
    import nucleo.carga_servicio as svc
    monkeypatch.setattr(svc, "CARPETA_CARGAS", tmp_path)

    procesar_carga(dia, _archivo_pos(), LoteCarga.Fuente.POS, usuario)
    otro_archivo = SimpleUploadedFile(
        "otro.xlsx", (FIXTURES / "Cartola_Movimientos_02-sep-2026.xlsx").read_bytes(),
    )
    with pytest.raises(CargaEnConflicto):
        procesar_carga(dia, otro_archivo, LoteCarga.Fuente.POS, usuario)


def test_validaciones_reflejan_el_dia_real(dia, usuario, tmp_path, monkeypatch):
    import nucleo.carga_servicio as svc
    monkeypatch.setattr(svc, "CARPETA_CARGAS", tmp_path)

    procesar_carga(dia, _archivo_pos(), LoteCarga.Fuente.POS, usuario)
    procesar_carga(dia, _archivo_transbank(), LoteCarga.Fuente.TRANSBANK, usuario)

    validaciones = {v["validacion"]: v for v in validaciones_de_carga(dia)}
    assert validaciones["Fecha declarada coincide con el día"]["ok"] is True
    assert validaciones["Total de comanda = suma de líneas"]["ok"] is True
    assert validaciones["Total = suma de medios declarados"]["ok"] is True
    assert validaciones["Contradicción entre hojas POS"]["ok"] is None  # D1 pendiente


def test_vista_requiere_login(client, dia):
    resp = client.get(reverse("carga_de_archivos", args=[FECHA.isoformat()]))
    assert resp.status_code == 302


def test_vista_de_carga_completa(client, usuario, dia, tmp_path, monkeypatch):
    import nucleo.carga_servicio as svc
    monkeypatch.setattr(svc, "CARPETA_CARGAS", tmp_path)
    client.force_login(usuario)

    resp = client.post(
        reverse("cargar_archivo", args=[FECHA.isoformat(), "pos"]), {"archivo": _archivo_pos()},
    )
    assert resp.status_code == 302
    dia.refresh_from_db()
    assert dia.ventas.count() == 336

    resp = client.get(reverse("carga_de_archivos", args=[FECHA.isoformat()]))
    assert resp.status_code == 200
    assert resp.context["resumen_pos"]["comandas"] == 336


def test_carga_mercado_pago_real_de_punta_a_punta(usuario, tmp_path, monkeypatch):
    import nucleo.carga_servicio as svc
    monkeypatch.setattr(svc, "CARPETA_CARGAS", tmp_path)
    dia_mp = DiaArqueo.objects.create(fecha=FECHA_MP)

    archivo = SimpleUploadedFile("settlement_10-09-2026.xlsx", FIXTURE_MP.read_bytes())
    lote = procesar_carga(dia_mp, archivo, LoteCarga.Fuente.MERCADO_PAGO, usuario)

    assert lote.fuente == LoteCarga.Fuente.MERCADO_PAGO
    dia_mp.refresh_from_db()
    assert dia_mp.estado == DiaArqueo.Estado.CARGADO
    assert dia_mp.movimientos.count() == 3
    assert dia_mp.movimientos.filter(proveedor="mercado_pago", medio="debito").count() == 3


def test_carga_pos_con_libro_contable_real_de_punta_a_punta(usuario, tmp_path, monkeypatch):
    import nucleo.carga_servicio as svc
    monkeypatch.setattr(svc, "CARPETA_CARGAS", tmp_path)
    dia_resto = DiaArqueo.objects.create(fecha=FECHA_RESTO)

    lote = procesar_carga_pos(dia_resto, _archivo_ventas_resto(), _archivo_libro_contable_resto(), usuario)

    assert lote.fuente == LoteCarga.Fuente.POS
    assert lote.nombre_archivo == "ventas_por_articulo_01-09-2026.xlsx"
    assert lote.nombre_archivo_secundario == "libro_contable_01-09-2026.xlsx"
    dia_resto.refresh_from_db()
    assert dia_resto.estado == DiaArqueo.Estado.CARGADO
    assert dia_resto.ventas.count() == 239
    # Las dos guías de despacho del libro (folios 21184 y 21185) ya no se
    # guardan como Venta: quedan pendientes en Regularización de Documentos
    # (Karla, 2026-09-22) — ver test_regularizacion_documentos.py.
    from nucleo.models import RegularizacionDocumento
    guias = RegularizacionDocumento.objects.filter(dia=dia_resto, tipo="DES").order_by("folio")
    assert [g.monto for g in guias] == [117406, 20300]
    venta = dia_resto.ventas.get(nro_comprobante="B0390301666228")
    assert venta.n_interno == 654506  # recuperado del libro contable, no venía en el archivo de ventas


def test_recargar_los_mismos_dos_archivos_de_resto_se_detecta_como_duplicado(usuario, tmp_path, monkeypatch):
    import nucleo.carga_servicio as svc
    monkeypatch.setattr(svc, "CARPETA_CARGAS", tmp_path)
    dia_resto = DiaArqueo.objects.create(fecha=FECHA_RESTO)

    procesar_carga_pos(dia_resto, _archivo_ventas_resto(), _archivo_libro_contable_resto(), usuario)
    with pytest.raises(CargaDuplicada):
        procesar_carga_pos(dia_resto, _archivo_ventas_resto(), _archivo_libro_contable_resto(), usuario)
    assert dia_resto.ventas.count() == 239


def test_vista_de_carga_pos_con_libro_contable(client, usuario, tmp_path, monkeypatch):
    import nucleo.carga_servicio as svc
    monkeypatch.setattr(svc, "CARPETA_CARGAS", tmp_path)
    client.force_login(usuario)
    dia_resto = DiaArqueo.objects.create(fecha=FECHA_RESTO)

    resp = client.post(
        reverse("cargar_archivo_pos", args=[FECHA_RESTO.isoformat()]),
        {"archivo_ventas": _archivo_ventas_resto(), "archivo_libro_contable": _archivo_libro_contable_resto()},
    )
    assert resp.status_code == 302
    dia_resto.refresh_from_db()
    assert dia_resto.ventas.exclude(tipo_doc="DES").count() == 239

    resp = client.get(reverse("carga_de_archivos", args=[FECHA_RESTO.isoformat()]))
    assert resp.context["resumen_pos"]["comandas"] == 239
    assert resp.context["lote_pos"].nombre_archivo_secundario == "libro_contable_01-09-2026.xlsx"


def test_descargar_archivo_original(client, usuario, dia, tmp_path, monkeypatch):
    import nucleo.carga_servicio as svc
    monkeypatch.setattr(svc, "CARPETA_CARGAS", tmp_path)
    client.force_login(usuario)

    lote = procesar_carga(dia, _archivo_pos(), LoteCarga.Fuente.POS, usuario)
    resp = client.get(reverse("descargar_archivo_original", args=[lote.pk]))
    assert resp.status_code == 200
    assert resp["Content-Disposition"].endswith('"Captura_Ventas_Sala_de_Ventas_02.09.26.xlsx"')
