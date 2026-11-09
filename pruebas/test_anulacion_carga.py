"""Anular la carga de un día (Karla, 2026-11-09). Caso real del 05-11-2026: la Encargada cargó mal ventas,
Transbank y Mercado Pago. Solicita la Encargada; aprueba el Jefe de Contabilidad o la Gerente; se borra todo
lo cargado (incluido el arqueo de efectivo) y el día queda para volver a cargar."""
from datetime import date, datetime
from decimal import Decimal

import pandas as pd
import pytest
from django.urls import reverse

from nucleo.anulacion_carga_servicio import AnulacionInvalida, aprobar, rechazar, solicitar
from nucleo.arqueo_servicio import registrar_conteo
from nucleo.carga import cargar_pos_con_libro_contable
from nucleo.cierre_servicio import puede_aprobar_usuario
from nucleo.conciliacion_servicio import ejecutar_conciliacion
from nucleo.models import (
    ArqueoEfectivo, Coincidencia, DiaArqueo, EventoAuditoria, Excepcion, LoteCarga, MovimientoProveedor,
    PagoDeclarado, RegularizacionDocumento, SolicitudAnulacionCarga, Venta,
)
from nucleo.permisos import PermisoDenegado
from pruebas.perfiles_de_prueba import GRUPO_ENCARGADO, GRUPO_GERENTE, GRUPO_JEFE, crear_usuario

DIA = date(2026, 11, 5)


def _archivos(tmp_path):
    filas = []
    for comp, n, total, efectivo, debito in (("B0390301700001", 1, 5000, 5000, 0), ("B0390301700002", 2, 8000, 0, 8000)):
        filas.append({
            "SUCURSAL": "H", "CLIENTE": "CLIENTE OCASIONAL", "NRO. COMPROBANTE": comp, "TIPO_DOC": "FAC",
            "NRO. COMANDA": n, "ESTADO COMANDA": "COBRADA", "HORA APERTURA": "12:00 HS.", "HORA CIERRE": "12:05 HS.",
            "CAJA CIERRE": "CAJA 1", "FECHA": pd.Timestamp(DIA), "ARTíCULO": "TE", "CANTIDAD": 1, "PRECIO UNITARIO": total,
            "TOTAL POR ARTICULO": total, "TOTAL_COMADA": total, "EFECTVO": efectivo, "TRANSFERENCIAS": 0,
            "TARJETA DE DEBITO": debito, "TARJETA DE CREDITO": 0, "TURNO": "T",
        })
    ventas = tmp_path / "ventas.xlsx"
    pd.DataFrame(filas).to_excel(ventas, sheet_name="VENTAS POR ARTICULO", index=False)
    libro = tmp_path / "libro.xlsx"
    pd.DataFrame([
        {"N_INTERNO": 700001, "FEC_COMPR": pd.Timestamp(DIA), "COD_COMP": "FAC", "N_COMP": "B0390301700001",
         "COD_CAJA": 1, "DESC_CTA": "EFECTIVO", "DEBE": 5000, "HABER": 0},
        {"N_INTERNO": 700002, "FEC_COMPR": pd.Timestamp(DIA), "COD_COMP": "FAC", "N_COMP": "B0390301700002",
         "COD_CAJA": 1, "DESC_CTA": "TARJETA DE DEBITO", "DEBE": 8000, "HABER": 0},
        {"N_INTERNO": 700003, "FEC_COMPR": pd.Timestamp(DIA), "COD_COMP": "DES", "N_COMP": "G0520500021400",
         "COD_CAJA": 1, "DESC_CTA": "EFECTIVO", "DEBE": 10000, "HABER": 0},
    ]).to_excel(libro, index=False)
    return ventas, libro


@pytest.fixture
def encargada(django_user_model):
    return crear_usuario(django_user_model, "nathaly", GRUPO_ENCARGADO)


@pytest.fixture
def jefe(django_user_model):
    return crear_usuario(django_user_model, "ricardo", GRUPO_JEFE)


@pytest.fixture
def gerente(django_user_model):
    return crear_usuario(django_user_model, "karla", GRUPO_GERENTE)


def _lote(dia, fuente, usuario):
    return LoteCarga.objects.create(dia=dia, fuente=fuente, nombre_archivo=f"{fuente}.xlsx", huella_sha256=fuente,
                                    ruta_almacenamiento=f"/{fuente}", fecha_declarada=DIA, cargado_por=usuario)


@pytest.fixture
def dia(db, tmp_path, encargada):
    """Día con las tres fuentes, conciliado (con una excepción) y con el conteo de la CAJA 1."""
    ventas, libro = _archivos(tmp_path)
    d = cargar_pos_con_libro_contable(ventas, libro, DIA, encargada).dia
    d.estado = DiaArqueo.Estado.CARGADO
    d.save()
    tbk = _lote(d, "transbank", encargada)
    MovimientoProveedor.objects.create(
        dia=d, lote=tbk, proveedor="transbank", tipo="Venta", estado="Abonado", fecha_hora=datetime(2026, 11, 5, 12, 5),
        medio="debito", tipo_tarjeta="debito", monto_original=8000, monto_venta_valido_abono=8000, terminal="A1")
    mp = _lote(d, "mercado_pago", encargada)
    MovimientoProveedor.objects.create(
        dia=d, lote=mp, proveedor="mercado_pago", tipo="Venta", estado="Pago aprobado", fecha_hora=datetime(2026, 11, 5, 15),
        medio="debito", tipo_tarjeta="debito", monto_original=3000, monto_venta_valido_abono=3000, terminal="Point")
    ejecutar_conciliacion(d, encargada)
    arqueo, _ = ArqueoEfectivo.objects.get_or_create(dia=d, caja="CAJA 1")
    registrar_conteo(arqueo, fondo_inicial=Decimal(0), gastos_autorizados=Decimal(0), total_contado=Decimal(15000), usuario=encargada)
    d.refresh_from_db()
    return d


def test_flujo_completo_borra_todo_y_deja_el_dia_para_recargar(dia, encargada, jefe, tmp_path):
    assert Coincidencia.objects.filter(dia=dia).exists() and Excepcion.objects.filter(dia=dia).exists()
    s = solicitar(dia, ["pos", "transbank", "mercado_pago"], "Nathaly cargó los archivos del 04-11", encargada)
    assert s.estado == "pendiente"
    aprobar(s, jefe, "revisado")
    s.refresh_from_db()
    dia.refresh_from_db()
    assert s.estado == "ejecutada" and s.resuelta_por == jefe
    assert dia.estado == DiaArqueo.Estado.SIN_CARGAR
    for modelo in (Venta, MovimientoProveedor, LoteCarga, Coincidencia, Excepcion, ArqueoEfectivo, RegularizacionDocumento):
        assert not modelo.objects.filter(dia=dia).exists(), modelo.__name__
    assert not PagoDeclarado.objects.exists()
    pos = next(l for l in s.resumen["lotes"] if l["fuente"] == "pos")
    assert pos["comandas"] == 2 and pos["total"] == 13000 and pos["archivo_secundario"]
    assert s.resumen["documentos"] and s.resumen["arqueos"] == ["CAJA 1"]
    ev = EventoAuditoria.objects.get(accion="anular_carga")
    assert ev.usuario == jefe and ev.valor_anterior["estado_anterior"] == "conciliado_con_excepciones"
    # se puede volver a cargar el mismo día
    ventas, libro = _archivos(tmp_path)
    cargar_pos_con_libro_contable(ventas, libro, DIA, encargada)
    assert Venta.objects.filter(dia=dia).count() == 2


def test_anular_solo_mercado_pago_conserva_ventas_y_arqueo(dia, encargada, gerente):
    s = solicitar(dia, ["mercado_pago"], "cartola de otro día", encargada)
    aprobar(s, gerente)
    dia.refresh_from_db()
    assert dia.estado == DiaArqueo.Estado.CARGADO
    assert not dia.lotes.filter(fuente="mercado_pago").exists() and dia.lotes.count() == 2
    assert Venta.objects.filter(dia=dia).count() == 2 and ArqueoEfectivo.objects.filter(dia=dia).exists()
    assert MovimientoProveedor.objects.filter(dia=dia).count() == 1
    assert not Coincidencia.objects.filter(dia=dia).exists()  # hay que volver a conciliar


def test_controles(dia, encargada, jefe, gerente):
    with pytest.raises(AnulacionInvalida, match="motivo"):
        solicitar(dia, ["pos"], "  ", encargada)
    with pytest.raises(AnulacionInvalida, match="al menos"):
        solicitar(dia, [], "x", encargada)
    with pytest.raises(PermisoDenegado):
        solicitar(dia, ["pos"], "x", jefe)  # el Jefe no opera
    s = solicitar(dia, ["pos"], "error", encargada)
    with pytest.raises(AnulacionInvalida, match="pendiente"):
        solicitar(dia, ["transbank"], "otra", encargada)
    with pytest.raises(PermisoDenegado):
        aprobar(s, encargada)
    # mientras esté pendiente no se puede cerrar el día
    assert "anulación de carga pendiente" in puede_aprobar_usuario(dia, gerente)[1]
    with pytest.raises(AnulacionInvalida, match="por qué"):
        rechazar(s, jefe, "")
    rechazar(s, jefe, "los archivos están bien")
    assert s.estado == "rechazada" and Venta.objects.filter(dia=dia).exists()
    with pytest.raises(AnulacionInvalida, match="resuelta"):
        aprobar(s, jefe)


def test_quien_solicita_no_aprueba(dia, gerente, jefe):
    s = solicitar(dia, ["transbank"], "error", gerente)
    with pytest.raises(AnulacionInvalida, match="otra persona"):
        aprobar(s, gerente)
    aprobar(s, jefe)


def test_dia_aprobado_no_se_anula(dia, encargada, jefe):
    s = solicitar(dia, ["pos"], "error", encargada)
    DiaArqueo.objects.filter(pk=dia.pk).update(estado=DiaArqueo.Estado.CERRADO)
    with pytest.raises(AnulacionInvalida, match="reabrirlo"):
        aprobar(s, jefe)
    assert Venta.objects.filter(dia=dia).exists()


def test_pantallas(client, dia, encargada, jefe):
    client.force_login(encargada)
    url_carga = reverse("carga_de_archivos", args=[DIA.isoformat()])
    assert "Solicitar anulación" in client.get(url_carga).content.decode()
    client.post(reverse("solicitar_anulacion", args=[DIA.isoformat()]),
                {"fuentes": ["pos", "transbank", "mercado_pago"], "motivo": "archivos del 04-11"})
    s = SolicitudAnulacionCarga.objects.get()
    html = client.get(url_carga).content.decode()
    assert "pendiente de aprobación" in html and reverse("aprobar_anulacion", args=[s.pk]) not in html
    client.force_login(jefe)
    url_cierre = reverse("cierre_y_exportacion", args=[DIA.isoformat()])
    html = client.get(url_cierre).content.decode()
    assert reverse("aprobar_anulacion", args=[s.pk]) in html and "Anular carga 05-11" in html
    r = client.post(reverse("aprobar_anulacion", args=[s.pk]), {"volver": "cierre_y_exportacion"})
    assert r.status_code == 302
    s.refresh_from_db()
    assert s.estado == "ejecutada" and not Venta.objects.exists()
