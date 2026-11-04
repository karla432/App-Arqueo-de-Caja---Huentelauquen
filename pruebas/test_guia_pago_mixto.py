"""Guía de despacho con pago mixto (Karla, 2026-11-04). Caso real del 28-10-2026: guía 21367 por $228.004 pagada
con $200.000 en efectivo (CAJA 1) y $28.004 con débito Transbank. Antes la app dejaba la forma de pago en blanco:
el efectivo no sumaba a la caja (sobrante falso de $200.010) y el cobro quedaba sin pago (se aceptó como «otros
ingresos» por error)."""
from datetime import date, datetime

import pandas as pd
import pytest

from nucleo.carga import cargar_pos_con_libro_contable
from nucleo.conciliacion_servicio import ejecutar_conciliacion
from nucleo.medios_libro_servicio import completar_desde_libro, guias_sin_desglose
from nucleo.models import Excepcion, LoteCarga, MotivoResolucion, MovimientoProveedor, RegularizacionDocumento
from nucleo.reapertura_excepciones_servicio import ReaperturaInvalida, motivo_para_no_reabrir, reabrir_excepcion
from nucleo.regularizacion_servicio import efectivo_documentos_por_caja
from nucleo.resolucion_servicio import resolver_excepcion
from pruebas.perfiles_de_prueba import GRUPO_ENCARGADO, GRUPO_GERENTE, GRUPO_JEFE, crear_usuario

DIA = date(2026, 10, 28)


def _archivos(tmp_path):
    ventas = tmp_path / "ventas.xlsx"
    pd.DataFrame([{
        "SUCURSAL": "H", "CLIENTE": "CLIENTE OCASIONAL", "NRO. COMPROBANTE": "B0390301698000", "TIPO_DOC": "FAC",
        "NRO. COMANDA": 1, "ESTADO COMANDA": "COBRADA", "HORA APERTURA": "14:00 HS.", "HORA CIERRE": "14:01 HS.",
        "CAJA CIERRE": "CAJA 1", "FECHA": pd.Timestamp(DIA), "ARTíCULO": "TE", "CANTIDAD": 1, "PRECIO UNITARIO": 1000,
        "TOTAL POR ARTICULO": 1000, "TOTAL_COMADA": 1000, "EFECTVO": 1000, "TRANSFERENCIAS": 0,
        "TARJETA DE DEBITO": 0, "TARJETA DE CREDITO": 0, "TURNO": "T",
    }]).to_excel(ventas, sheet_name="VENTAS POR ARTICULO", index=False)
    libro = tmp_path / "libro.xlsx"
    pd.DataFrame([
        {"N_INTERNO": 686500, "FEC_COMPR": pd.Timestamp(DIA), "COD_COMP": "FAC", "N_COMP": "B0390301698000",
         "COD_CAJA": 1, "DESC_CTA": "EFECTIVO", "DEBE": 1000, "HABER": 0},
        {"N_INTERNO": 686574, "FEC_COMPR": pd.Timestamp(DIA), "COD_COMP": "DES", "N_COMP": "G0520500021367",
         "COD_CAJA": 1, "DESC_CTA": "EFECTIVO", "DEBE": 200000, "HABER": 0},
        {"N_INTERNO": 686574, "FEC_COMPR": pd.Timestamp(DIA), "COD_COMP": "DES", "N_COMP": "G0520500021367",
         "COD_CAJA": 1, "DESC_CTA": "TARJETA DE DEBITO", "DEBE": 28004, "HABER": 0},
    ]).to_excel(libro, index=False)
    return ventas, libro


@pytest.fixture
def encargada(django_user_model):
    return crear_usuario(django_user_model, "nathaly", GRUPO_ENCARGADO)


def _cobro(dia, usuario):
    lote = LoteCarga.objects.create(dia=dia, fuente="transbank", nombre_archivo="t", huella_sha256="t",
                                    ruta_almacenamiento="/t", fecha_declarada=DIA, cargado_por=usuario)
    return MovimientoProveedor.objects.create(
        dia=dia, lote=lote, proveedor="transbank", tipo="Venta", estado="Abonado", fecha_hora=datetime(2026, 10, 28, 14, 20),
        medio="debito", tipo_tarjeta="debito", monto_original=28004, monto_venta_valido_abono=28004,
        autorizacion="007282", terminal="A2380302")


def test_la_carga_guarda_el_desglose_y_el_efectivo_suma_a_la_caja(db, tmp_path, encargada):
    ventas, libro = _archivos(tmp_path)
    dia = cargar_pos_con_libro_contable(ventas, libro, DIA, encargada).dia
    guia = RegularizacionDocumento.objects.get(dia=dia, tipo="DES")
    assert guia.forma_pago == "" and guia.desglose_pagos == {"efectivo": 200000.0, "debito": 28004.0}
    assert guia.monto_efectivo == 200000 and guia.monto_tarjeta == 28004 and guia.medio_tarjeta == "debito"
    assert efectivo_documentos_por_caja(dia) == {"CAJA 1": 200000}
    assert "Efectivo $200.000 + Débito $28.004" == guia.forma_pago_texto


def test_la_parte_con_tarjeta_se_calza_con_su_cobro(db, tmp_path, encargada):
    ventas, libro = _archivos(tmp_path)
    dia = cargar_pos_con_libro_contable(ventas, libro, DIA, encargada).dia
    mov = _cobro(dia, encargada)
    ejecutar_conciliacion(dia, encargada)
    guia = RegularizacionDocumento.objects.get(dia=dia, tipo="DES")
    assert guia.movimiento == mov and guia.diferencia_con_cobro == 0
    assert not Excepcion.objects.filter(dia=dia, abierta=True, movimiento=mov).exists()


def test_dia_ya_cargado_se_completa_desde_el_libro(db, tmp_path, encargada):
    ventas, libro = _archivos(tmp_path)
    dia = cargar_pos_con_libro_contable(ventas, libro, DIA, encargada).dia
    RegularizacionDocumento.objects.filter(dia=dia, tipo="DES").update(desglose_pagos={})  # como quedó el 28-10
    assert len(guias_sin_desglose(dia)) == 1 and efectivo_documentos_por_caja(dia) == {}
    r = completar_desde_libro(dia, encargada)
    assert r["guias"] and guias_sin_desglose(dia) == []
    assert efectivo_documentos_por_caja(dia) == {"CAJA 1": 200000}


def test_solo_la_gerente_deshace_otros_ingresos_con_boleta(db, tmp_path, encargada, django_user_model):
    ventas, libro = _archivos(tmp_path)
    dia = cargar_pos_con_libro_contable(ventas, libro, DIA, encargada).dia
    RegularizacionDocumento.objects.filter(dia=dia, tipo="DES").update(desglose_pagos={})
    mov = _cobro(dia, encargada)
    ejecutar_conciliacion(dia, encargada)
    exc = Excepcion.objects.get(dia=dia, abierta=True, movimiento=mov)
    resolver_excepcion(exc, resolucion="aceptar", motivo_id=MotivoResolucion.objects.get(codigo="forma_pago_sin_boleta").pk,
                       comentario="era la guía", usuario=encargada)
    exc.refresh_from_db()
    exc.factura_asociada = "51749"
    exc.save()
    jefe = crear_usuario(django_user_model, "jefe", GRUPO_JEFE)
    assert "solo la Gerente" in motivo_para_no_reabrir(exc, jefe)
    with pytest.raises(ReaperturaInvalida):
        reabrir_excepcion(exc, jefe, "x")
    karla = crear_usuario(django_user_model, "karla", GRUPO_GERENTE)
    assert motivo_para_no_reabrir(exc, karla) == ""
    reabrir_excepcion(exc, karla, "El cobro es la parte con tarjeta de la guía 21367; no se emitió boleta")
    exc.refresh_from_db()
    assert exc.abierta and exc.factura_asociada == ""
    completar_desde_libro(dia, karla)  # completa el desglose
    ejecutar_conciliacion(dia, karla)
    assert RegularizacionDocumento.objects.get(dia=dia, tipo="DES").movimiento == mov
