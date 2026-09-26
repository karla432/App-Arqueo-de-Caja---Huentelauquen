"""Consolidado por día y seguimiento de regularizaciones y reintegros (Karla,
2026-09-24): una fila por día con lo vendido, cobrado, contado, guías, notas,
excepciones, depósito y pérdidas de cajeras — más el seguimiento de lo que sigue
sin regularizar y de lo que las cajeras deben reintegrar."""
from datetime import date, time
from decimal import Decimal

import pytest
from pruebas.perfiles_de_prueba import GRUPO_GERENTE, crear_usuario
from django.urls import reverse
from openpyxl import load_workbook

from nucleo.consolidado_servicio import (
    consolidado_por_dia, generar_excel_consolidado, generar_excel_seguimiento, rango_del_mes, resumen_por_cajera,
    seguimiento_documentos, seguimiento_reintegros, totales_consolidado,
)
from nucleo.models import (
    ArqueoEfectivo, Cajera, DepositoBancario, DiaArqueo, Excepcion, LoteCarga, PagoDeclarado, RegularizacionDocumento,
    ReintegroCaja, Venta,
)

FECHA = date(2026, 9, 7)


@pytest.fixture
def usuario(django_user_model):
    return crear_usuario(django_user_model, "operador", GRUPO_GERENTE)


@pytest.fixture
def cajera(db):
    return Cajera.objects.create(nombre="Rosa Rojo")


def _dia(usuario, fecha=FECHA, estado="cerrado"):
    return DiaArqueo.objects.create(fecha=fecha, operado_por=usuario, estado=estado)


def _venta(dia, n, total, medios, caja="CAJA 3"):
    lote = LoteCarga.objects.filter(dia=dia).first() or LoteCarga.objects.create(
        dia=dia, fuente=LoteCarga.Fuente.POS, nombre_archivo="x", huella_sha256=f"h{dia.pk}",
        ruta_almacenamiento="/x", fecha_declarada=dia.fecha, cargado_por=dia.operado_por,
    )
    venta = Venta.objects.create(
        dia=dia, lote=lote, n_interno=n, nro_comprobante=f"B{n:07d}", tipo_doc="FAC", nro_comanda=str(n),
        estado="COBRADA", caja=caja, turno="T", hora_cierre=time(11, 0), total_comanda=total, cliente="X",
    )
    for medio, monto in medios:
        PagoDeclarado.objects.create(venta=venta, medio=medio, monto=monto)
    return venta


@pytest.fixture
def dia_07(usuario, cajera):
    """Caja 3 del 07-09 (caso real, en pequeño): boletas $100.000 = $60.000 tarjeta + $40.000 efectivo,
    dos notas de crédito informativas, una guía pendiente y un faltante atribuido a la cajera."""
    dia = _dia(usuario)
    _venta(dia, 1, 60000, [("debito", 60000)])
    _venta(dia, 2, 40000, [("efectivo", 40000)])
    ArqueoEfectivo.objects.create(
        dia=dia, caja="CAJA 3", cajera=cajera, teorico=Decimal(40000), esperado=Decimal(40000),
        contado=Decimal(38000), diferencia=Decimal(-2000),
    )
    RegularizacionDocumento.objects.create(dia=dia, tipo="CRED", n_interno=9, folio="F0610600008311", caja="CAJA 3", monto=9600, forma_pago="credito")
    RegularizacionDocumento.objects.create(dia=dia, tipo="CRED", n_interno=10, folio="F0610600008312", caja="CAJA 3", monto=43495, forma_pago="credito")
    RegularizacionDocumento.objects.create(dia=dia, tipo="DES", n_interno=11, folio="21197", caja="CAJA 3", monto=23830, forma_pago="efectivo")
    return dia


# ------------------------------------------------------------- consolidado

def test_una_fila_por_dia_con_ventas_cobros_y_efectivo(dia_07):
    filas = consolidado_por_dia(FECHA, FECHA)
    assert len(filas) == 1
    f = filas[0]
    assert (f["boletas_n"], f["boletas_total"]) == (2, 100000)
    assert (f["efectivo_pos"], f["tarjetas_pos"], f["otros_pos"], f["sin_explicar"]) == (40000, 60000, 0, 0)
    assert (f["efectivo_esperado"], f["efectivo_contado"], f["diferencia_efectivo"]) == (40000, 38000, -2000)
    assert f["cuadra"] is True


def test_las_notas_de_credito_y_guias_se_muestran_como_informativas(dia_07):
    f = consolidado_por_dia(FECHA, FECHA)[0]
    assert (f["notas_n"], f["notas_total"], f["notas_sin_respaldo"]) == (2, 53095, 2)
    assert (f["guias_n"], f["guias_total"], f["guias_efectivo"], f["guias_pendientes"]) == (1, 23830, 23830, 1)


def test_las_perdidas_de_cajeras_y_su_saldo(dia_07, cajera, usuario):
    f = consolidado_por_dia(FECHA, FECHA)[0]
    assert (f["perdidas_cajeras"], f["reintegrado"], f["saldo_cajeras"]) == (2000, 0, 2000)
    arqueo = ArqueoEfectivo.objects.get(dia=dia_07)
    ReintegroCaja.objects.create(arqueo_origen=arqueo, cajera=cajera, monto=Decimal(500), estado="validado", registrado_por=usuario)
    f = consolidado_por_dia(FECHA, FECHA)[0]
    assert (f["reintegrado"], f["saldo_cajeras"]) == (500, 1500)


def test_un_dia_con_cobros_que_no_suman_las_boletas_no_cuadra(usuario):
    dia = _dia(usuario)
    _venta(dia, 1, 10000, [("efectivo", 9000)])
    f = consolidado_por_dia(FECHA, FECHA)[0]
    assert f["sin_explicar"] == 1000 and f["cuadra"] is False


def test_una_excepcion_abierta_hace_que_el_dia_no_cuadre(usuario):
    dia = _dia(usuario)
    _venta(dia, 1, 10000, [("debito", 10000)])
    Excepcion.objects.create(dia=dia, tipo="pago_sin_movimiento", monto_en_juego=Decimal(10000))
    f = consolidado_por_dia(FECHA, FECHA)[0]
    assert (f["excepciones_abiertas_n"], f["excepciones_abiertas_monto"], f["cuadra"]) == (1, 10000, False)


def test_el_deposito_se_suma_por_dia(dia_07, usuario):
    DepositoBancario.objects.create(dia=dia_07, monto_esperado=Decimal(38000), monto_depositado=Decimal(37000), registrado_por=usuario, estado="aprobado")
    f = consolidado_por_dia(FECHA, FECHA)[0]
    assert (f["deposito_esperado"], f["deposito_depositado"], f["deposito_diferencia"]) == (38000, 37000, -1000)


def test_un_dia_sin_cargar_no_aparece_y_el_rango_filtra(usuario):
    _dia(usuario, estado="sin_cargar")
    _dia(usuario, fecha=date(2026, 10, 1))
    assert consolidado_por_dia(date(2026, 9, 1), date(2026, 9, 30)) == []
    assert len(consolidado_por_dia(date(2026, 10, 1), date(2026, 10, 31))) == 1


def test_los_totales_suman_los_dias(usuario):
    for i, fecha in enumerate((date(2026, 9, 1), date(2026, 9, 2)), start=1):
        dia = _dia(usuario, fecha=fecha)
        _venta(dia, i, 1000 * i, [("efectivo", 1000 * i)])
    t = totales_consolidado(consolidado_por_dia(date(2026, 9, 1), date(2026, 9, 30)))
    assert (t["dias"], t["boletas_n"], t["boletas_total"], t["dias_que_no_cuadran"]) == (2, 2, 3000, 0)


def test_rango_del_mes():
    assert rango_del_mes(2026, 2) == (date(2026, 2, 1), date(2026, 2, 28))
    assert rango_del_mes(2026, 12) == (date(2026, 12, 1), date(2026, 12, 31))


def test_el_excel_del_consolidado_tiene_encabezado_filas_y_total(dia_07, tmp_path):
    filas = list(load_workbook(generar_excel_consolidado(FECHA, FECHA, tmp_path)).active.iter_rows(values_only=True))
    assert filas[0][0] == "Fecha" and filas[0][2] == "Boletas (N)"
    assert filas[1][2] == 2 and filas[1][3] == 100000
    assert filas[3][0] == "TOTAL"


def test_el_excel_de_un_periodo_vacio_lo_dice(db, tmp_path):
    filas = list(load_workbook(generar_excel_consolidado(FECHA, FECHA, tmp_path)).active.iter_rows(values_only=True))
    assert filas[1][0].startswith("Sin días cargados")


# --------------------------------------------------------------- seguimiento

def test_seguimiento_de_documentos_con_antiguedad(dia_07):
    filas = seguimiento_documentos(FECHA, FECHA, hoy=date(2026, 9, 24))
    guia = next(f for f in filas if f["codigo"] == "DES")
    assert guia["pendiente"] and guia["dias_pendiente"] == 17 and guia["nivel"] == "critico"
    assert seguimiento_documentos(FECHA, FECHA, hoy=date(2026, 9, 12))[0]["nivel"] == "reciente"
    assert len(seguimiento_documentos(FECHA, FECHA, tipo="DES")) == 1
    assert len(seguimiento_documentos(FECHA, FECHA, estado="regularizado")) == 0


def test_seguimiento_de_reintegros_y_resumen_por_cajera(dia_07, cajera):
    filas = seguimiento_reintegros(FECHA, FECHA, hoy=date(2026, 9, 24))
    assert len(filas) == 1
    f = filas[0]
    assert (f["cajera"], f["original"], f["reintegrado"], f["saldo"], f["estado"]) == ("Rosa Rojo", 2000, 0, 2000, "pendiente")
    assert f["menor"] is False and f["dias_pendiente"] == 17
    assert resumen_por_cajera(filas)[0]["saldo"] == 2000


def test_las_perdidas_menores_se_ven_en_el_seguimiento_con_marca(usuario, cajera):
    dia = _dia(usuario)
    ArqueoEfectivo.objects.create(dia=dia, caja="CAJA 1", cajera=cajera, diferencia=Decimal(-10))
    f = seguimiento_reintegros(FECHA, FECHA)[0]
    assert f["menor"] is True and f["saldo"] == 10


def test_el_filtro_de_estado_de_reintegros(dia_07):
    assert seguimiento_reintegros(FECHA, FECHA, estado="reintegrado") == []
    assert len(seguimiento_reintegros(FECHA, FECHA, estado="pendiente")) == 1


def test_el_excel_del_seguimiento_tiene_dos_hojas(dia_07, tmp_path):
    wb = load_workbook(generar_excel_seguimiento(FECHA, FECHA, tmp_path))
    assert wb.sheetnames == ["Documentos", "Pérdidas de cajeras"]
    assert wb["Pérdidas de cajeras"]["B2"].value == "Rosa Rojo"


# ------------------------------------------------------------------ pantallas

def test_las_pantallas_se_ven_y_estan_en_el_menu(client, dia_07, usuario):
    client.force_login(usuario)
    html = client.get(reverse("consolidado_dias"), {"desde": "2026-09-01", "hasta": "2026-09-30"}).content.decode()
    assert "Auditoría de Pagos" in html and "07-09-2026" in html and "$100.000" in html
    assert reverse("seguimiento") in html and "Regularizaciones y Reintegros" in html  # menú lateral
    assert "Descargar Excel del consolidado" in html
    seg = client.get(reverse("seguimiento"), {"desde": "2026-09-01", "hasta": "2026-09-30", "vista": "documentos"}).content.decode()
    assert "21197" in seg and "8311" in seg and "F0610600008311" not in seg  # folio corto
    rein = client.get(reverse("seguimiento"), {"desde": "2026-09-01", "hasta": "2026-09-30", "vista": "reintegros"}).content.decode()
    assert "Rosa Rojo" in rein and "Cuánto debe cada cajera" in rein


def test_fechas_mal_escritas_no_rompen_las_pantallas(client, usuario, db):
    client.force_login(usuario)
    assert client.get(reverse("consolidado_dias"), {"desde": "xx", "hasta": "2026-13-45"}).status_code == 200
    assert client.get(reverse("seguimiento"), {"desde": "2026-09-30", "hasta": "2026-09-01", "vista": "otra"}).status_code == 200


def test_descargas_desde_las_pantallas(client, dia_07, usuario):
    client.force_login(usuario)
    r1 = client.get(reverse("descargar_consolidado"), {"desde": "2026-09-01", "hasta": "2026-09-30"})
    r2 = client.get(reverse("descargar_seguimiento"), {"desde": "2026-09-01", "hasta": "2026-09-30"})
    assert r1.status_code == r2.status_code == 200 and r1["Content-Disposition"].startswith("attachment")


def test_sin_iniciar_sesion_no_se_accede(client, db):
    assert client.get(reverse("consolidado_dias")).status_code == 302
    assert client.get(reverse("seguimiento")).status_code == 302


def test_las_correcciones_de_forma_de_pago_se_ven_en_el_consolidado_y_enlazan_a_la_auditoria(client, dia_07, usuario):
    """Karla, 2026-09-25: «Auditoría de pagos» sale del menú y queda como columna del consolidado."""
    from nucleo.models import Reclasificacion
    venta = dia_07.ventas.first()
    pago = venta.pagos.first()
    f = consolidado_por_dia(FECHA, FECHA)[0]
    assert f["correcciones_pago_n"] == 0
    Reclasificacion.objects.create(dia=dia_07, pago=pago, medio_original="debito", medio_corregido="credito",
                                    monto=Decimal(60000), origen="automatica", regla="x", motivo="prueba")
    f = consolidado_por_dia(FECHA, FECHA)[0]
    assert (f["correcciones_pago_n"], f["correcciones_pago_monto"]) == (1, 60000)
    client.force_login(usuario)
    html = client.get(reverse("consolidado_dias"), {"desde": "2026-09-01", "hasta": "2026-09-30"}).content.decode()
    assert reverse("auditoria_reclasificaciones", args=[FECHA.isoformat()]) in html and "Correcciones de pago" in html


def test_auditoria_de_pagos_ya_no_esta_en_el_menu_pero_la_pantalla_sigue_disponible(client, dia_07, usuario):
    client.force_login(usuario)
    html = client.get(reverse("consolidado_dias"), {"desde": "2026-09-01", "hasta": "2026-09-30"}).content.decode()
    # el ítem «Auditoría de Pagos» del menú (Karla, 2026-09-26) lleva a esta misma pantalla, que trae las correcciones
    assert reverse("consolidado_dias") in html and ">Auditoría de Pagos</a>" in html
    assert client.get(reverse("auditoria_reclasificaciones", args=[FECHA.isoformat()])).status_code == 200


def test_las_tarjetas_del_consolidado_suman_las_guias_y_calzan_con_transbank(dia_07):
    """Karla, 2026-09-25 (día 07): el cobro con tarjeta de una guía está en Transbank pero no en las ventas del
    POS; «Total tarjetas» = POS + guías debe ser comparable con lo que cobró el proveedor."""
    from datetime import datetime
    from nucleo.models import MovimientoProveedor, RegularizacionDocumento
    lote = dia_07.lotes.first()
    for monto in (60000, 26775):  # la venta del POS ($60.000 débito) y la guía cobrada con tarjeta
        MovimientoProveedor.objects.create(
            dia=dia_07, lote=lote, proveedor="transbank", tipo="Venta", estado="PENDIENTE",
            fecha_hora=datetime(2026, 9, 7, 12, 0), medio="debito", tipo_tarjeta="debito",
            monto_original=monto, monto_venta_valido_abono=monto, terminal="A1",
        )
    f = consolidado_por_dia(FECHA, FECHA)[0]
    assert (f["tarjetas_pos"], f["guias_tarjeta"], f["proveedor_tarjeta"], f["diferencia_tarjeta"]) == (60000, 0, 86775, -26775)
    RegularizacionDocumento.objects.create(dia=dia_07, tipo="DES", n_interno=20, folio="21203", caja="CAJA 3", monto=26775, forma_pago="debito")
    f = consolidado_por_dia(FECHA, FECHA)[0]
    assert (f["guias_tarjeta"], f["tarjetas_total"], f["proveedor_tarjeta"], f["diferencia_tarjeta"]) == (26775, 86775, 86775, 0)
    assert totales_consolidado([f])["diferencia_tarjeta"] == 0



def test_el_menu_esta_organizado_en_las_cinco_secciones_con_los_nombres_definidos(client, usuario, db):
    """Menú definido por Karla el 2026-09-26 (fase 1: nombres y secciones)."""
    client.force_login(usuario)
    html = client.get(reverse("consolidado_dias")).content.decode()
    ordenados = [
        "Informes", "Ventas · Mensual y Anual", "Ventas · Por Producto", "Dashboard Forma de Pago del Día",
        "Diario", "Cargar Archivos", "Excepciones", "Arqueo de Efectivo", "Cierre del Día", "Archivos para Odoo",
        "Controles y Odoo", "Depósitos bancarios", "Regularización de Documentos (GD-NCR)", "Reintegros de Cajeras",
        "Análisis", "Calendario · Estado del Proceso", "Auditoría de Pagos", "Regularizaciones y Reintegros", "Calidad de registro",
    ]
    menu = html[html.index('<div class="sidebar">'):html.index('class="contenido"')]
    posicion = -1
    for nombre in ordenados:
        nueva = menu.index(nombre, posicion + 1)
        assert nueva > posicion, nombre
        posicion = nueva
    assert "Exportaciones a Odoo" not in menu and "Control de Depósitos" not in menu
