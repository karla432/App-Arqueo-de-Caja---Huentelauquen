"""Redelcom (Karla, 2026-11-04): la heladería cobra solo con Redelcom (terminal Point de Mercado Pago). VENTAS POR
ARTICULO no trae columna para ese medio y las comandas llegaban sin forma de pago; el libro contable sí la trae
(cuenta REDELCOM). Caso real del 27-10-2026: 15 comandas de la CAJA HELADERIA, $90.670."""
from datetime import date, datetime, time

import pandas as pd
import pytest
from django.urls import reverse

from arqueo_diario.importadores.pos import importar_ventas_con_libro_contable
from nucleo.carga import FormaDePagoIncompleta, cargar_pos_con_libro_contable
from nucleo.medios_libro_servicio import CompletarInvalido, comandas_sin_forma_de_pago, completar_desde_libro
from nucleo.models import DiaArqueo, EventoAuditoria, Excepcion, LoteCarga, MovimientoProveedor, PagoDeclarado, Venta
from nucleo.permisos import PermisoDenegado
from pruebas.perfiles_de_prueba import GRUPO_ENCARGADO, GRUPO_SOLO_LECTURA, crear_usuario

DIA = date(2026, 10, 27)


def _archivos(tmp_path, *, cuenta="REDELCOM", efectivo=0):
    filas = []
    for comprobante, comanda, total, hora in (("B0390301697515", 687923, 8280, "11:22 HS."),
                                              ("B0390301697882", 688305, 15160, "18:30 HS.")):
        filas.append({
            "SUCURSAL": "Huentelauquen", "CLIENTE": "CLIENTE OCASIONAL", "NRO. COMPROBANTE": comprobante,
            "TIPO_DOC": "FAC", "NRO. COMANDA": comanda, "ESTADO COMANDA": "COBRADA", "HORA APERTURA": hora,
            "HORA CIERRE": hora, "CAJA CIERRE": "CAJA HELADERIA", "FECHA": pd.Timestamp(DIA),
            "ARTíCULO": "HELADO BARQUILLO DOBLE", "CANTIDAD": 1, "PRECIO UNITARIO": total, "TOTAL POR ARTICULO": total,
            "TOTAL_COMADA": total, "EFECTVO": efectivo, "TRANSFERENCIAS": 0, "TARJETA DE DEBITO": 0,
            "TARJETA DE CREDITO": 0, "TURNO": "T",
        })
    ventas = tmp_path / "ventas.xlsx"
    pd.DataFrame(filas).to_excel(ventas, sheet_name="VENTAS POR ARTICULO", index=False)
    libro = tmp_path / "libro.xlsx"
    pd.DataFrame([
        {"N_INTERNO": 686000, "FEC_COMPR": pd.Timestamp(DIA), "COD_COMP": "FAC", "N_COMP": "B0390301697515",
         "COD_CAJA": 8, "DESC_CTA": cuenta, "DEBE": 8280, "HABER": 0},
        {"N_INTERNO": 686369, "FEC_COMPR": pd.Timestamp(DIA), "COD_COMP": "FAC", "N_COMP": "B0390301697882",
         "COD_CAJA": 8, "DESC_CTA": cuenta, "DEBE": 15160, "HABER": 0},
    ]).to_excel(libro, index=False)
    return ventas, libro


@pytest.fixture
def encargada(django_user_model):
    return crear_usuario(django_user_model, "nathaly", GRUPO_ENCARGADO)


def test_el_importador_toma_redelcom_del_libro_contable(tmp_path):
    ventas, libro = _archivos(tmp_path)
    r = importar_ventas_con_libro_contable(ventas, libro, DIA)
    assert [(p.medio, p.monto) for v in r.ventas for p in v.pagos] == [("debito", 8280), ("debito", 15160)]
    assert not any("suma de medios" in a for a in r.avisos)
    assert any("forma de pago tomada del libro contable" in a for a in r.avisos)


def test_la_carga_rechaza_comandas_sin_forma_de_pago(db, tmp_path, encargada):
    ventas, libro = _archivos(tmp_path, cuenta="CUENTA DESCONOCIDA")
    with pytest.raises(FormaDePagoIncompleta, match="2 comanda"):
        cargar_pos_con_libro_contable(ventas, libro, DIA, encargada)
    assert not LoteCarga.objects.exists() and not Venta.objects.exists()


def test_la_carga_con_redelcom_pasa_y_concilia_con_mercado_pago(db, tmp_path, encargada):
    from nucleo.conciliacion_servicio import ejecutar_conciliacion
    ventas, libro = _archivos(tmp_path)
    lote = cargar_pos_con_libro_contable(ventas, libro, DIA, encargada)
    dia = lote.dia
    lote_mp = LoteCarga.objects.create(dia=dia, fuente="mercado_pago", nombre_archivo="mp", huella_sha256="mp",
                                       ruta_almacenamiento="/mp", fecha_declarada=DIA, cargado_por=encargada)
    for monto, hora in ((8280, time(11, 22, 22)), (15160, time(18, 30, 22))):
        MovimientoProveedor.objects.create(
            dia=dia, lote=lote_mp, proveedor="mercado_pago", tipo="Venta", estado="Pago aprobado",
            fecha_hora=datetime.combine(DIA, hora), medio="debito", tipo_tarjeta="debito",
            monto_original=monto, monto_venta_valido_abono=monto, terminal="Point")
    ejecutar_conciliacion(dia, encargada)
    assert not Excepcion.objects.filter(dia=dia, abierta=True).exists()


def _dia_cargado_sin_medios(tmp_path, usuario):
    """Como quedó el 27-10 en producción: comandas guardadas sin ningún pago."""
    ventas, libro = _archivos(tmp_path)
    dia = DiaArqueo.objects.create(fecha=DIA, estado=DiaArqueo.Estado.CONCILIADO_CON_EXCEPCIONES)
    lote = LoteCarga.objects.create(dia=dia, fuente="pos", nombre_archivo="v", huella_sha256="v", ruta_almacenamiento=str(ventas),
                                    ruta_almacenamiento_secundario=str(libro), fecha_declarada=DIA, cargado_por=usuario,
                                    avisos=["Comanda 686000: total $8,280 no coincide con la suma de medios declarados $0."])
    for n, comp, total in ((686000, "B0390301697515", 8280), (686369, "B0390301697882", 15160)):
        Venta.objects.create(dia=dia, lote=lote, n_interno=n, nro_comprobante=comp, tipo_doc="FAC", estado="COBRADA",
                             caja="CAJA HELADERIA", hora_cierre=time(12), total_comanda=total)
    return dia, lote


def test_completar_un_dia_ya_cargado(db, tmp_path, encargada, django_user_model):
    dia, lote = _dia_cargado_sin_medios(tmp_path, encargada)
    assert len(comandas_sin_forma_de_pago(dia)) == 2
    with pytest.raises(PermisoDenegado):
        completar_desde_libro(dia, crear_usuario(django_user_model, "lector", GRUPO_SOLO_LECTURA))
    r = completar_desde_libro(dia, encargada)
    assert len(r["completadas"]) == 2 and r["sin_resolver"] == []
    assert set(PagoDeclarado.objects.filter(venta__dia=dia).values_list("medio", "monto")) == {("debito", 8280), ("debito", 15160)}
    assert comandas_sin_forma_de_pago(dia) == []
    lote.refresh_from_db()
    assert lote.avisos == []
    assert EventoAuditoria.objects.filter(accion="completar_medios_desde_libro").count() == 1
    assert completar_desde_libro(dia, encargada)["completadas"] == []  # idempotente


def test_no_se_completa_un_dia_cerrado(db, tmp_path, encargada):
    dia, _ = _dia_cargado_sin_medios(tmp_path, encargada)
    dia.estado = DiaArqueo.Estado.CERRADO
    dia.save()
    with pytest.raises(CompletarInvalido, match="cerrado"):
        completar_desde_libro(dia, encargada)


def test_pantalla_de_carga_avisa_y_ofrece_completar(client, db, tmp_path, encargada):
    dia, _ = _dia_cargado_sin_medios(tmp_path, encargada)
    client.force_login(encargada)
    url = reverse("carga_de_archivos", args=[DIA.isoformat()])
    html = client.get(url).content.decode()
    assert "2 comandas sin forma de pago" in html and reverse("completar_medios", args=[DIA.isoformat()]) in html
    client.post(reverse("completar_medios", args=[DIA.isoformat()]))
    assert "sin forma de pago." not in client.get(url).content.decode()


def test_mercado_pago_prepaid_card_es_prepago():
    from arqueo_diario.importadores.mercadopago import MEDIOS_PAGO
    assert MEDIOS_PAGO["prepaid_card"] == "prepago" and MEDIOS_PAGO["debit_card"] == "debito"
