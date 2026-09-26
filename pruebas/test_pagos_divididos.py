"""Pago dividido (Karla, 2026-09-23): Odoo admite un solo medio por venta
externa, pero una comanda puede pagarse con dos (caso real: comanda 655526
del 04-09, efectivo $10.000 + débito $1.353). La venta va completa bajo un
medio principal y el resto viaja como traspaso en el asiento Arqueo Caja.
Regla validada con Tesorero, Contador Senior y Riesgo."""
from datetime import date, datetime, time
from decimal import Decimal

import pytest
from pruebas.perfiles_de_prueba import GRUPO_GERENTE, crear_usuario
from openpyxl import load_workbook

import nucleo.exportacion_servicio as svc
from nucleo.arqueo_caja_servicio import ArqueoCajaInvalido, generar_lineas_arqueo_caja
from nucleo.arqueo_servicio import calcular_teorico_por_caja
from nucleo.models import (
    Coincidencia, DiaArqueo, EventoAuditoria, LineaVenta, LoteCarga, MovimientoProveedor, PagoDeclarado, Venta,
    VersionMotor,
)
from nucleo.pagos_divididos_servicio import plan_de_pago_dividido

FECHA = date(2026, 9, 4)


@pytest.fixture
def usuario(django_user_model):
    return crear_usuario(django_user_model, "operador", GRUPO_GERENTE)


@pytest.fixture
def dia(db, usuario):
    dia = DiaArqueo.objects.create(fecha=FECHA, operado_por=usuario)
    LoteCarga.objects.create(
        dia=dia, fuente=LoteCarga.Fuente.POS, nombre_archivo="x", huella_sha256="a",
        ruta_almacenamiento="/x", fecha_declarada=FECHA, cargado_por=usuario,
    )
    VersionMotor.objects.create(dia=dia, version_motor="test", parametros={}, ejecutado_por=usuario)
    return dia


def _venta(dia, n, total, pagos):
    lote = dia.lotes.first()
    venta = Venta.objects.create(
        dia=dia, lote=lote, n_interno=n, nro_comprobante=f"B039030166{n:04d}", tipo_doc="FAC", nro_comanda=str(n),
        estado="COBRADA", caja="CAJA 5", turno="T", hora_cierre=time(12, 0), total_comanda=total,
        cliente="CLIENTE OCASIONAL",
    )
    LineaVenta.objects.create(venta=venta, articulo="X", cantidad=1, precio_unitario=total, total_linea=total)
    creados = [PagoDeclarado.objects.create(venta=venta, medio=m, monto=monto) for m, monto in pagos]
    return venta, creados


def _calzar(dia, pago, proveedor="transbank", usuario=None):
    lote = LoteCarga.objects.get_or_create(
        dia=dia, fuente=proveedor if proveedor != "mercado_pago" else LoteCarga.Fuente.MERCADO_PAGO,
        defaults=dict(nombre_archivo="p", huella_sha256=proveedor, ruta_almacenamiento="/p",
                      fecha_declarada=FECHA, cargado_por=usuario or dia.operado_por),
    )[0]
    mov = MovimientoProveedor.objects.create(
        dia=dia, lote=lote, proveedor=proveedor, tipo="Venta", estado="PENDIENTE",
        fecha_hora=datetime(2026, 9, 4, 12, 18), medio=pago.medio, tipo_tarjeta=pago.medio,
        monto_original=pago.monto, monto_venta_valido_abono=pago.monto, autorizacion=f"A{pago.pk}", terminal="T",
    )
    c = Coincidencia.objects.create(
        dia=dia, corrida=dia.corridas_motor.first(), nivel=1, score=100, diferencia_monto=0,
        diferencia_hora_segundos=0,
    )
    c.pagos.add(pago)
    c.movimientos.add(mov)
    return mov


def _caso_real(dia):
    venta, (efectivo, debito) = _venta(dia, 5526, 11353, [("efectivo", 10000), ("debito", 1353)])
    _calzar(dia, debito)
    return venta


def test_efectivo_es_el_medio_principal_y_la_tarjeta_viaja_como_traspaso(dia):
    plan = plan_de_pago_dividido(_caso_real(dia))
    assert plan.principal == "efectivo"
    assert plan.traspasos == [("transbank", Decimal(1353))]
    assert plan.problemas == []


def test_sin_efectivo_el_principal_es_el_mayor_y_en_empate_transbank(dia):
    venta, (p1, p2) = _venta(dia, 1, 2000, [("debito", 1000), ("credito", 1000)])
    _calzar(dia, p1, "mercado_pago")
    _calzar(dia, p2, "transbank")
    assert plan_de_pago_dividido(venta).principal == "transbank"


def test_dos_tarjetas_del_mismo_proveedor_no_son_pago_dividido(dia):
    venta, (p1, p2) = _venta(dia, 1, 2000, [("debito", 1000), ("credito", 1000)])
    _calzar(dia, p1)
    _calzar(dia, p2)
    assert plan_de_pago_dividido(venta) is None


def test_una_comanda_con_un_solo_medio_no_cambia(dia):
    venta, _ = _venta(dia, 1, 1000, [("efectivo", 1000)])
    assert plan_de_pago_dividido(venta) is None


def test_la_exportacion_del_dia_ya_no_se_bloquea_y_exporta_la_venta_como_efectivo(dia, usuario, tmp_path, monkeypatch):
    monkeypatch.setattr(svc, "CARPETA_EXPORTACIONES", tmp_path)
    _caso_real(dia)
    dia.estado = DiaArqueo.Estado.APROBADO
    dia.save(update_fields=["estado"])

    exportacion = svc.generar_exportacion(dia, usuario)

    filas = list(load_workbook(exportacion.archivo).active.iter_rows(values_only=True))
    columnas, fila = filas[0], dict(zip(filas[0], filas[1]))
    assert fila["Tipo de pago externo"] == "Efectivo" and fila["ID de pago externo"] == 5526
    evento = EventoAuditoria.objects.get(accion="generar_exportacion", dia=dia)
    assert evento.valor_posterior["pagos_divididos"][0]["comanda"] == 5526


def test_el_asiento_arqueo_caja_lleva_el_traspaso_debe_transbank_haber_efectivo(dia):
    _caso_real(dia)
    filas = generar_lineas_arqueo_caja(dia)
    debe = next(f for f in filas if f["Apuntes contables/Débito"])
    haber = next(f for f in filas if f["Apuntes contables/Crédito"])
    assert debe["Apuntes contables/Cuenta"] == "1101005 Transbank Débito y Crédito"
    assert haber["Apuntes contables/Cuenta"] == "1101004 Efectivo Pos Resto"
    assert debe["Apuntes contables/Débito"] == 1353 and haber["Apuntes contables/Crédito"] == 1353
    assert debe["Apuntes contables/Etiqueta"] == (
        "Pago dividido Comanda 5526 Boleta 1665526 (EF 10.000 / TBK 1.353) - Arqueo Caja Día 04/09/2026"
    )
    assert debe["Apuntes contables/Empresa"] == "TRANSBANK"


def test_el_arqueo_de_efectivo_solo_cuenta_la_parte_en_efectivo(dia):
    _caso_real(dia)
    assert calcular_teorico_por_caja(dia) == {"CAJA 5": Decimal(10000)}


def test_la_parte_de_tarjeta_sin_calzar_bloquea_y_pide_conciliar(dia, usuario, tmp_path, monkeypatch):
    monkeypatch.setattr(svc, "CARPETA_EXPORTACIONES", tmp_path)
    _venta(dia, 5526, 11353, [("efectivo", 10000), ("debito", 1353)])  # sin movimiento de Transbank
    dia.estado = DiaArqueo.Estado.APROBADO
    dia.save(update_fields=["estado"])
    with pytest.raises(svc.ExportacionInvalida, match="conciliación"):
        svc.generar_exportacion(dia, usuario)


def test_una_parte_por_transferencia_bloquea_nunca_se_omite_en_silencio(dia, usuario, tmp_path, monkeypatch):
    monkeypatch.setattr(svc, "CARPETA_EXPORTACIONES", tmp_path)
    _venta(dia, 5526, 11353, [("efectivo", 10000), ("transferencia", 1353)])
    dia.estado = DiaArqueo.Estado.APROBADO
    dia.save(update_fields=["estado"])
    with pytest.raises(svc.ExportacionInvalida, match="transferencia"):
        svc.generar_exportacion(dia, usuario)


def test_si_los_pagos_no_suman_el_total_de_la_comanda_bloquea(dia, usuario, tmp_path, monkeypatch):
    monkeypatch.setattr(svc, "CARPETA_EXPORTACIONES", tmp_path)
    venta, (_, debito) = _venta(dia, 5526, 11353, [("efectivo", 10000), ("debito", 1000)])
    _calzar(dia, debito)
    dia.estado = DiaArqueo.Estado.APROBADO
    dia.save(update_fields=["estado"])
    with pytest.raises(svc.ExportacionInvalida, match="suman"):
        svc.generar_exportacion(dia, usuario)


def test_el_cierre_muestra_las_comandas_con_pago_dividido(client, dia, usuario):
    _caso_real(dia)
    client.force_login(usuario)
    html = client.get(f"/cierre/{FECHA.isoformat()}/").content.decode()
    assert "Comandas con pago dividido" in html and "5526" in html
    assert "Qué falta para cerrar" in html


def test_el_cierre_exige_haber_ejecutado_la_conciliacion(client, db, usuario):
    """Caso real de Karla (2026-09-23): un día con tarjeta se aprobó sin
    haber corrido nunca la conciliación, y "cero excepciones abiertas" era
    un falso verde."""
    from nucleo.cierre_servicio import estado_checklist

    dia = DiaArqueo.objects.create(fecha=FECHA, operado_por=usuario)
    lote = LoteCarga.objects.create(
        dia=dia, fuente=LoteCarga.Fuente.POS, nombre_archivo="x", huella_sha256="a",
        ruta_almacenamiento="/x", fecha_declarada=FECHA, cargado_por=usuario,
    )
    _venta(dia, 1, 1000, [("debito", 1000)])
    assert estado_checklist(dia)["conciliacion_ejecutada"]["ok"] is False
    VersionMotor.objects.create(dia=dia, version_motor="test", parametros={}, ejecutado_por=usuario)
    assert estado_checklist(dia)["conciliacion_ejecutada"]["ok"] is True
