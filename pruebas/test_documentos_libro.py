"""Tres tipos de documento en el libro contable (Karla, 2026-09-21/22): FAC
(boleta electrónica), DES (guía de despacho electrónica) y CRED (nota de
crédito electrónica). Solo las boletas traen detalle en VENTAS POR ARTICULO
y entran a la carga masiva de ventas / arqueo automático. Las guías y notas
de crédito NUNCA se guardan como Venta ni participan del motor de
conciliación (Karla, 2026-09-22): quedan como `RegularizacionDocumento`
Pendiente, para que la Encargada de Ingresos las regularice a mano desde el
menú "Regularización de Documentos" — ver test_regularizacion_documentos.py.
"""
from datetime import date, datetime

import pandas as pd
import pytest
from pruebas.perfiles_de_prueba import GRUPO_GERENTE, crear_usuario

from arqueo_diario.importadores.pos import folio_documento, importar_ventas_con_libro_contable
from nucleo.carga import cargar_pos_con_libro_contable
from nucleo.conciliacion_servicio import ejecutar_conciliacion
from nucleo.models import DiaArqueo, Excepcion, LoteCarga, MovimientoProveedor, RegularizacionDocumento, Venta

FECHA = date(2026, 9, 1)


@pytest.fixture
def usuario(django_user_model):
    return crear_usuario(django_user_model, "operador", GRUPO_GERENTE)


def _ventas_df():
    return pd.DataFrame([{
        "SUCURSAL": "Huentelauquen Central", "CLIENTE": "CLIENTE OCASIONAL",
        "NRO. COMPROBANTE": "B0000001", "TIPO_DOC": "FAC", "NRO. COMANDA": 1,
        "ESTADO COMANDA": "COBRADA", "HORA APERTURA": "10:00 HS.", "HORA CIERRE": "10:01 HS.",
        "CAJA CIERRE": "CAJA 1", "FECHA": pd.Timestamp(FECHA), "ARTíCULO": "TE",
        "CANTIDAD": 1, "PRECIO UNITARIO": 1000, "TOTAL POR ARTICULO": 1000, "TOTAL_COMADA": 1000,
        "EFECTVO": 1000, "TRANSFERENCIAS": 0, "TARJETA DE DEBITO": 0, "TARJETA DE CREDITO": 0,
        "TURNO": "TURNO CAJA 1",
    }])


def _fila_libro(n_interno, cod_comp, n_comp, desc_cta, debe=0, haber=0, caja=1):
    return {
        "N_INTERNO": n_interno, "FEC_COMPR": pd.Timestamp(FECHA), "COD_COMP": cod_comp,
        "N_COMP": n_comp, "DESC_CTA": desc_cta, "DEBE": debe, "HABER": haber, "COD_CAJA": caja,
    }


def _libro_con_los_tres_tipos():
    return pd.DataFrame([
        _fila_libro(100, "FAC", "B0000001", "EFECTIVO", debe=1000),
        _fila_libro(200, "DES", "G0520500021184", "TARJETA DE DEBITO", debe=117406),
        _fila_libro(201, "DES", "G0520500021185", "TARJETA DE CREDITO", debe=20300),
        _fila_libro(300, "CRED", "N0000000000077", "EFECTIVO", haber=5000),
    ])


@pytest.fixture
def archivos(tmp_path):
    ventas, libro = tmp_path / "ventas.xlsx", tmp_path / "libro.xlsx"
    _ventas_df().to_excel(ventas, sheet_name="VENTAS POR ARTICULO", index=False)
    _libro_con_los_tres_tipos().to_excel(libro, index=False)
    return ventas, libro


def test_folio_de_cada_tipo_de_documento():
    assert folio_documento("DES", "G0520500021184") == "21184"
    assert folio_documento("DES", "G0520500000007") == "7"
    assert folio_documento("FAC", "B0390301666228") == "1666228"
    assert folio_documento("CRED", "N0000000000077") == "N0000000000077"


def test_las_guias_se_leen_del_libro_con_su_folio_y_medio(archivos):
    resultado = importar_ventas_con_libro_contable(*archivos, FECHA)
    guias = sorted(resultado.guias, key=lambda g: g.folio)
    assert [(g.folio, g.n_interno, g.total, [p.medio for p in g.pagos]) for g in guias] == [
        ("21184", 200, 117406, ["debito"]),
        ("21185", 201, 20300, ["credito"]),
    ]
    assert resultado.cantidad_comandas == 1  # la boleta; las guías no son comandas


def test_las_notas_de_credito_se_leen_del_libro_sin_avisos_especiales(archivos):
    resultado = importar_ventas_con_libro_contable(*archivos, FECHA)
    assert len(resultado.notas_credito) == 1
    nota = resultado.notas_credito[0]
    assert (nota.folio, nota.n_interno, nota.total) == ("N0000000000077", 300, 5000)
    # Karla, 2026-09-22: ya no queda como aviso "todavía no la procesa" —
    # se procesa, en Regularización de Documentos.
    assert not any("todavía no la procesa" in a for a in resultado.avisos)


def test_un_tipo_de_documento_desconocido_queda_en_avisos(tmp_path):
    ventas, libro = tmp_path / "v.xlsx", tmp_path / "l.xlsx"
    _ventas_df().to_excel(ventas, sheet_name="VENTAS POR ARTICULO", index=False)
    pd.DataFrame([
        _fila_libro(100, "FAC", "B0000001", "EFECTIVO", debe=1000),
        _fila_libro(400, "XYZ", "X1", "EFECTIVO", debe=10),
    ]).to_excel(libro, index=False)
    resultado = importar_ventas_con_libro_contable(ventas, libro, FECHA)
    assert any("'XYZ'" in a for a in resultado.avisos)


def test_es_guia_de_despacho_acepta_g0_y_go():
    # El "GO/G0" en el número de comprobante de una boleta (FAC) normal es
    # un caso distinto (diseño original, decisión Karla 2026-09-06): una
    # comanda que sí pasa por la carga masiva, pero espera su Factura al
    # cierre. No tiene relación con el DES/CRED del libro contable.
    assert Venta(tipo_doc="FAC", nro_comprobante="G0520500021184").es_guia_de_despacho
    assert Venta(tipo_doc="FAC", nro_comprobante="GO520500021184").es_guia_de_despacho
    assert not Venta(tipo_doc="FAC", nro_comprobante="B0390301666228").es_guia_de_despacho


def _movimiento(dia, lote, medio, monto, hora, autorizacion):
    return MovimientoProveedor.objects.create(
        dia=dia, lote=lote, proveedor="transbank", tipo="Venta", estado="PENDIENTE",
        fecha_hora=datetime(2026, 9, 1, hora, 30), medio=medio, tipo_tarjeta=medio,
        monto_original=monto, monto_venta_valido_abono=monto, autorizacion=autorizacion, terminal="T1",
    )


@pytest.fixture
def dia_cargado(db, usuario, archivos):
    dia = DiaArqueo.objects.create(fecha=FECHA, operado_por=usuario)
    cargar_pos_con_libro_contable(*archivos, FECHA, usuario)
    lote_tbk = LoteCarga.objects.create(
        dia=dia, fuente=LoteCarga.Fuente.TRANSBANK, nombre_archivo="t.xlsx", huella_sha256="t",
        ruta_almacenamiento="/tmp/t", fecha_declarada=FECHA, cargado_por=usuario,
    )
    return dia, lote_tbk


def test_las_guias_y_notas_de_credito_quedan_pendientes_de_regularizar_no_como_venta(dia_cargado):
    dia, _ = dia_cargado
    assert dia.ventas.count() == 1  # solo la boleta
    assert dia.ventas.filter(tipo_doc__in=["DES", "CRED"]).count() == 0

    pendientes = RegularizacionDocumento.objects.filter(dia=dia).order_by("tipo", "folio")
    assert [(d.tipo, d.folio, d.monto, d.estado) for d in pendientes] == [
        ("CRED", "N0000000000077", 5000, "pendiente"),
        ("DES", "21184", 117406, "pendiente"),
        ("DES", "21185", 20300, "pendiente"),
    ]


def test_crear_desde_libro_no_duplica_si_se_llama_dos_veces(db, usuario, archivos):
    """Karla, 2026-09-22: idempotente igual que antes se evitaba duplicar
    la guía — si el mismo libro contable se vuelve a leer (ej. reintento
    de carga), no se duplican los documentos pendientes."""
    from arqueo_diario.importadores.pos import importar_ventas_con_libro_contable
    from nucleo.regularizacion_servicio import crear_desde_libro

    dia = DiaArqueo.objects.create(fecha=FECHA, operado_por=usuario)
    resultado = importar_ventas_con_libro_contable(*archivos, FECHA)
    documentos = resultado.guias + resultado.notas_credito

    creados_primero = crear_desde_libro(dia, documentos, usuario)
    creados_segundo = crear_desde_libro(dia, documentos, usuario)

    assert len(creados_primero) == 3
    assert len(creados_segundo) == 0
    assert RegularizacionDocumento.objects.filter(dia=dia).count() == 3


def test_el_cobro_de_una_guia_se_calza_solo_con_la_guia_y_no_sale_como_excepcion(dia_cargado, usuario):
    """Karla, 2026-09-23: la guía no es una comanda, pero su cobro de
    tarjeta tampoco es una "venta sin registrar": se calza por monto y
    medio con la guía, y ese vínculo queda como su cotejo validado."""
    dia, lote_tbk = dia_cargado
    debito = _movimiento(dia, lote_tbk, "debito", 117406, 15, "A1")
    credito = _movimiento(dia, lote_tbk, "credito", 20300, 16, "A2")
    ejecutar_conciliacion(dia, usuario)
    assert not Excepcion.objects.filter(dia=dia, abierta=True).exists()
    guias = {g.folio: g.movimiento_id for g in RegularizacionDocumento.objects.filter(dia=dia, tipo="DES")}
    assert guias == {"21184": debito.pk, "21185": credito.pk}


def test_dos_cobros_iguales_para_una_guia_no_se_adivinan_quedan_como_excepcion(dia_cargado, usuario):
    dia, lote_tbk = dia_cargado
    _movimiento(dia, lote_tbk, "debito", 117406, 15, "A1")
    _movimiento(dia, lote_tbk, "debito", 117406, 17, "A3")
    ejecutar_conciliacion(dia, usuario)
    assert Excepcion.objects.filter(dia=dia, tipo="movimiento_sin_pago", abierta=True).count() == 2
    assert RegularizacionDocumento.objects.get(dia=dia, folio="21184").movimiento_id is None


def test_un_cobro_sin_guia_ni_comanda_sigue_siendo_excepcion(dia_cargado, usuario):
    dia, lote_tbk = dia_cargado
    _movimiento(dia, lote_tbk, "debito", 55555, 15, "A9")
    ejecutar_conciliacion(dia, usuario)
    assert Excepcion.objects.filter(dia=dia, tipo="movimiento_sin_pago", abierta=True).count() == 1


def test_reconciliar_de_nuevo_no_duplica_ni_pierde_el_vinculo_de_la_guia(dia_cargado, usuario):
    dia, lote_tbk = dia_cargado
    _movimiento(dia, lote_tbk, "debito", 117406, 15, "A1")
    ejecutar_conciliacion(dia, usuario)
    ejecutar_conciliacion(dia, usuario)
    assert RegularizacionDocumento.objects.get(dia=dia, folio="21184").movimiento_id is not None
    assert not Excepcion.objects.filter(dia=dia, abierta=True).exists()


def test_las_fechas_como_numero_de_serie_de_excel_tambien_se_leen(tmp_path):
    """Caso real de Karla (2026-09-23): Resto exportó los archivos del 04-09
    con la fecha como número de serie de Excel (46269), no como fecha — el
    importador no reconocía ninguna fila y el día quedaba en 0 comandas."""
    serie = (pd.Timestamp(FECHA) - pd.Timestamp("1899-12-30")).days
    ventas, libro = tmp_path / "v.xlsx", tmp_path / "l.xlsx"
    df_ventas = _ventas_df()
    df_ventas["FECHA"] = serie
    df_ventas.to_excel(ventas, sheet_name="VENTAS POR ARTICULO", index=False)
    df_libro = _libro_con_los_tres_tipos()
    df_libro["FEC_COMPR"] = serie
    df_libro.to_excel(libro, index=False)

    resultado = importar_ventas_con_libro_contable(ventas, libro, FECHA)

    assert resultado.cantidad_comandas == 1
    assert len(resultado.guias) == 2 and len(resultado.notas_credito) == 1


def test_la_caja_de_una_guia_coincide_con_la_de_las_boletas_aunque_venga_como_decimal(tmp_path):
    """Caso real de Karla (2026-09-23): COD_CAJA llegó como 1.0 y la guía
    quedó en "CAJA 1.0", una caja fantasma distinta de la "CAJA 1" de las
    boletas — su efectivo nunca se sumaba al arqueo."""
    ventas, libro = tmp_path / "v.xlsx", tmp_path / "l.xlsx"
    _ventas_df().to_excel(ventas, sheet_name="VENTAS POR ARTICULO", index=False)
    df = _libro_con_los_tres_tipos()
    df["COD_CAJA"] = [1.0, 1.0, 5.0, 4.0]
    df.to_excel(libro, index=False)
    resultado = importar_ventas_con_libro_contable(ventas, libro, FECHA)
    cajas = {d.folio: d.caja for d in resultado.guias + resultado.notas_credito}
    assert cajas == {"21184": "CAJA 1", "21185": "CAJA 5", "N0000000000077": "CAJA 4"}


def test_la_hoja_de_ventas_se_reconoce_aunque_no_se_llame_ventas_por_articulo(tmp_path):
    """Caso real de Karla (2026-09-24): el archivo del 05-09 trae la hoja
    como "Hoja1". Se busca por columnas, no por nombre — pero un archivo
    equivocado (ej. el libro contable en lugar de las ventas) se rechaza."""
    ventas, libro = tmp_path / "v.xlsx", tmp_path / "l.xlsx"
    _ventas_df().to_excel(ventas, sheet_name="Hoja1", index=False)
    _libro_con_los_tres_tipos().to_excel(libro, index=False)
    assert importar_ventas_con_libro_contable(ventas, libro, FECHA).cantidad_comandas == 1

    with pytest.raises(ValueError, match="no las ventas por art"):
        importar_ventas_con_libro_contable(libro, libro, FECHA)


def test_un_archivo_en_el_lugar_equivocado_dice_cual_es(tmp_path):
    ventas, libro = tmp_path / "v.xlsx", tmp_path / "l.xlsx"
    _ventas_df().to_excel(ventas, sheet_name="VENTAS POR ARTICULO", index=False)
    _libro_con_los_tres_tipos().to_excel(libro, index=False)
    cartola = tmp_path / "c.xlsx"
    pd.DataFrame([{"a": 1}]).to_excel(cartola, sheet_name="CartolaMovimientos", index=False)
    with pytest.raises(ValueError, match="cartola de Transbank"):
        importar_ventas_con_libro_contable(cartola, libro, FECHA)
    with pytest.raises(ValueError, match="libro contable"):
        importar_ventas_con_libro_contable(libro, libro, FECHA)


def test_la_nota_de_credito_escrita_como_cre_tambien_se_reconoce(tmp_path):
    """Caso real del 07-09-2026: Resto escribió las dos notas de crédito como
    "CRE" (no "CRED"), la app las tomó como un tipo desconocido y quedaron
    solo como aviso, sin llegar a Regularización de Documentos."""
    ventas, libro = tmp_path / "v.xlsx", tmp_path / "l.xlsx"
    _ventas_df().to_excel(ventas, sheet_name="VENTAS POR ARTICULO", index=False)
    pd.DataFrame([
        _fila_libro(100, "FAC", "B0000001", "EFECTIVO", debe=1000),
        _fila_libro(656967, "CRE", "F0610600008311", "TARJETA DE CREDITO", haber=9600, caja=3),
    ]).to_excel(libro, index=False)
    resultado = importar_ventas_con_libro_contable(ventas, libro, FECHA)
    assert [(d.tipo, d.folio, d.total, d.caja) for d in resultado.notas_credito] == [
        ("CRED", "F0610600008311", 9600, "CAJA 3"),
    ]
    assert not any("no reconocido" in a for a in resultado.avisos)
