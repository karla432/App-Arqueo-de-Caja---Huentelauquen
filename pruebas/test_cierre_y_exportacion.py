"""Cierre, aprobación y exportación a Odoo (Etapa 6). Escenario armado a
mano: una comanda limpia, arqueo de su única caja ya cuadrado, para poder
probar el camino feliz y cada bloqueo por separado sin depender del día
completo.
"""
from datetime import date, time
from decimal import Decimal

import pytest
from django.urls import reverse
from openpyxl import load_workbook

from nucleo.arqueo_servicio import registrar_conteo
from nucleo.cierre_servicio import (
    CierreInvalido, aprobar_y_cerrar_dia, estado_checklist, puede_aprobar, reabrir_dia,
)
from nucleo.exportacion_servicio import ExportacionInvalida, generar_exportacion
from nucleo.models import (
    ArqueoEfectivo, DiaArqueo, Excepcion, LineaVenta, LoteCarga, PagoDeclarado, Venta,
)

FECHA = date(2026, 9, 2)


@pytest.fixture
def usuario(django_user_model):
    return django_user_model.objects.create_user(username="operador", password="x", is_superuser=True)


@pytest.fixture
def dia_listo(db, usuario):
    """Un día con una sola comanda, sin excepciones abiertas y con su
    única caja ya cuadrada — cumple todo lo bloqueante de la lista de
    verificación."""
    dia = DiaArqueo.objects.create(fecha=FECHA, operado_por=usuario)
    lote = LoteCarga.objects.create(
        dia=dia, fuente=LoteCarga.Fuente.POS, nombre_archivo="x.xlsx", huella_sha256="a",
        ruta_almacenamiento="/tmp/x", fecha_declarada=FECHA, cargado_por=usuario,
    )
    venta = Venta.objects.create(
        dia=dia, lote=lote, n_interno=654747, nro_comprobante="B0390301666467", tipo_doc="FAC",
        nro_comanda="656609", estado="COBRADA", caja="CAJA 4", turno="TURNO CAJA 4",
        hora_cierre=time(10, 25), total_comanda=17600, cliente="CLIENTE OCASIONAL", sucursal="Huentelauquen Central",
    )
    LineaVenta.objects.create(venta=venta, articulo="EMPANADA QUESO FRITA", cantidad=4,
                               precio_unitario=2500, total_linea=10000)
    LineaVenta.objects.create(venta=venta, articulo="TE", cantidad=4, precio_unitario=1900, total_linea=7600)
    PagoDeclarado.objects.create(venta=venta, medio="debito", monto=17600)
    return dia


@pytest.fixture
def caja_cuadrada(dia_listo, usuario):
    # No hay efectivo en la única venta -> el arqueo de esa caja parte en
    # $0 esperado; lo registramos igual para que "arqueo completo" pase.
    arqueo, _ = ArqueoEfectivo.objects.get_or_create(dia=dia_listo, caja="CAJA 4")
    registrar_conteo(arqueo, fondo_inicial=Decimal(0), gastos_autorizados=Decimal(0),
                      denominaciones={}, usuario=usuario)
    return arqueo


def test_checklist_bloquea_por_excepcion_abierta(dia_listo, caja_cuadrada):
    Excepcion.objects.create(dia=dia_listo, tipo="pago_sin_movimiento", monto_en_juego=1000,
                              decision_propuesta="x")
    assert puede_aprobar(dia_listo) is False


def test_checklist_no_bloquea_por_calidad_de_registro(dia_listo, caja_cuadrada):
    Excepcion.objects.create(dia=dia_listo, tipo="calidad_registro", monto_en_juego=0,
                              decision_propuesta="x")
    assert puede_aprobar(dia_listo) is True


def test_checklist_bloquea_si_falta_contar_una_caja(dia_listo):
    assert puede_aprobar(dia_listo) is False  # CAJA 4 nunca se contó


def test_deposito_no_bloquea(dia_listo, caja_cuadrada):
    assert estado_checklist(dia_listo)["deposito_registrado"]["ok"] is False
    assert puede_aprobar(dia_listo) is True  # el depósito no es bloqueante


def test_aprobar_y_cerrar_camino_feliz(dia_listo, caja_cuadrada, usuario):
    aprobar_y_cerrar_dia(dia_listo, usuario)
    dia_listo.refresh_from_db()
    assert dia_listo.estado == DiaArqueo.Estado.CERRADO
    assert dia_listo.aprobado_por == usuario


def test_no_se_aprueba_con_pendientes(dia_listo):
    with pytest.raises(CierreInvalido):
        aprobar_y_cerrar_dia(dia_listo, dia_listo.operado_por)


def test_no_se_aprueba_dos_veces(dia_listo, caja_cuadrada, usuario):
    aprobar_y_cerrar_dia(dia_listo, usuario)
    with pytest.raises(CierreInvalido):
        aprobar_y_cerrar_dia(dia_listo, usuario)


def test_exportar_solo_si_aprobado(dia_listo, usuario):
    with pytest.raises(ExportacionInvalida):
        generar_exportacion(dia_listo, usuario)


def test_exportar_genera_archivo_con_el_formato_real(dia_listo, caja_cuadrada, usuario, tmp_path, monkeypatch):
    import nucleo.exportacion_servicio as svc
    monkeypatch.setattr(svc, "CARPETA_EXPORTACIONES", tmp_path)

    aprobar_y_cerrar_dia(dia_listo, usuario)
    exportacion = generar_exportacion(dia_listo, usuario)

    assert exportacion.version == 1
    wb = load_workbook(exportacion.archivo)
    ws = wb.active
    filas = list(ws.iter_rows(values_only=True))
    assert filas[0] == tuple(svc.COLUMNAS)
    # 2 líneas de la única comanda -> 2 filas de datos.
    assert len(filas) == 3
    cabecera = filas[1]
    assert cabecera[0] == "CLIENTE OCASIONAL"
    assert cabecera[4] == "Tarjeta Transbank"  # débito -> Tarjeta Transbank
    assert cabecera[5] == 654747  # N_INTERNO
    assert cabecera[6] == "Boleta Electrónica"
    assert cabecera[7] == "1666467"  # 7 últimos del comprobante
    segunda_linea = filas[2]
    assert segunda_linea[0] is None  # fila de línea adicional, sin cabecera repetida


def test_reabrir_invalida_la_exportacion(dia_listo, caja_cuadrada, usuario, tmp_path, monkeypatch):
    import nucleo.exportacion_servicio as svc
    monkeypatch.setattr(svc, "CARPETA_EXPORTACIONES", tmp_path)

    aprobar_y_cerrar_dia(dia_listo, usuario)
    exportacion = generar_exportacion(dia_listo, usuario)

    reabrir_dia(dia_listo, usuario, motivo="Corrección de una línea")
    exportacion.refresh_from_db()
    dia_listo.refresh_from_db()
    assert exportacion.invalidada is True
    assert dia_listo.estado == DiaArqueo.Estado.EXCEPCIONES_RESUELTAS


def test_no_se_reabre_sin_motivo(dia_listo, caja_cuadrada, usuario):
    aprobar_y_cerrar_dia(dia_listo, usuario)
    with pytest.raises(CierreInvalido):
        reabrir_dia(dia_listo, usuario, motivo="  ")


def test_vista_aprobar_requiere_rol_de_aprobador(client, django_user_model, dia_listo, caja_cuadrada):
    operador_sin_rol = django_user_model.objects.create_user(username="sin_rol", password="x")
    client.force_login(operador_sin_rol)
    resp = client.post(reverse("aprobar_dia", args=[FECHA.isoformat()]))
    assert resp.status_code == 403
    dia_listo.refresh_from_db()
    assert dia_listo.estado != DiaArqueo.Estado.CERRADO
