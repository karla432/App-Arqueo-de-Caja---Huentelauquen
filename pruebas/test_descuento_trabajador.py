"""Descuento a trabajadores (Karla, 2026-10-27): los clientes «TRABAJADOR» tienen un % por producto; Resto informa
las líneas a precio de lista y el total ya descontado. Casos con los montos reales del 15-10-2026."""
from datetime import date, time
from decimal import Decimal

import pytest
from django.urls import reverse

from nucleo.carga_servicio import validaciones_de_carga
from nucleo.configuracion_servicio import ConfiguracionInvalida, crear_descuento, editar_descuento
from nucleo.descuentos_servicio import DescuentoInvalido, aceptar, analizar, del_dia
from nucleo.exportacion_servicio import _filas_de_venta, validar_venta
from nucleo.models import DescuentoTrabajador, DiaArqueo, EventoAuditoria, LineaVenta, LoteCarga, PagoDeclarado, Parametro, Venta
from nucleo.permisos import PermisoDenegado
from nucleo.reporte_productos_servicio import resumen_por_producto
from nucleo.ventas_dashboard_servicio import resumen_periodo
from pruebas.perfiles_de_prueba import GRUPO_ENCARGADO, GRUPO_GERENTE, GRUPO_JEFE, crear_usuario

FECHA = date(2026, 10, 15)


@pytest.fixture
def encargada(django_user_model):
    return crear_usuario(django_user_model, "nathaly", GRUPO_ENCARGADO)


@pytest.fixture
def jefe(django_user_model):
    return crear_usuario(django_user_model, "ricardo", GRUPO_JEFE)


@pytest.fixture
def dia(db, encargada):
    d = DiaArqueo.objects.create(fecha=FECHA, estado=DiaArqueo.Estado.CERRADO)
    d.lote = LoteCarga.objects.create(dia=d, fuente="pos", nombre_archivo="x", huella_sha256="x", ruta_almacenamiento="/x",
                                      fecha_declarada=FECHA, cargado_por=encargada)
    return d


def _venta(dia, n, total, lineas, cliente="TRABAJADOR"):
    v = Venta.objects.create(dia=dia, lote=dia.lote, n_interno=n, nro_comprobante=f"B0390300{n}", tipo_doc="FAC",
                             nro_comanda=str(n), estado="COBRADA", caja="CAJA 1", hora_cierre=time(13), total_comanda=total,
                             cliente=cliente)
    for articulo, cant, precio in lineas:
        LineaVenta.objects.create(venta=v, articulo=articulo, cantidad=cant, precio_unitario=precio, total_linea=cant * precio)
    PagoDeclarado.objects.create(venta=v, medio="efectivo", monto=total)
    return v


def test_la_tabla_inicial_viene_cargada_con_el_mantecoso_al_15(db):
    assert DescuentoTrabajador.objects.count() == 24
    assert DescuentoTrabajador.objects.get(producto="PIEZA QUESO MANTECOSO REDONDO").porcentaje == 15


def test_comanda_que_calza_con_la_tabla_se_exporta_con_linea_de_descuento(dia):
    # 679722: bebida a $2.000, cobrada $1.520 (24%)
    v = _venta(dia, 679722, 1520, [("BEBIDA GASEOSA", 1, 2000)])
    a = analizar(v)
    assert a.cuadra and a.descuento == 480 and a.estado == "cuadra"
    assert validar_venta(v) == []
    filas = _filas_de_venta(v)
    assert filas[-1]["Líneas del pedido/Producto"] == "DESCUENTO TRABAJADOR"
    assert filas[-1]["Líneas del pedido/Precio un."] == -480
    assert sum(f["Líneas del pedido/Cantidad"] * f["Líneas del pedido/Precio un."] for f in filas) == 1520


def test_mezcla_de_productos_y_redondeo_dentro_de_tolerancia(dia):
    # Queso 15% + néctar 25% + un producto sin descuento; Resto redondeó $3 distinto.
    # 5.950×0,85 + 2.000×0,75 + 2.000×0,85 + 1.000 = 9.257,5 según la tabla; Resto cobró 9.261.
    v = _venta(dia, 679520, 9261, [("QUESO PIEZA", Decimal("0.5"), 11900), ("NECTAR PAPAYA 300 CC", 2, 1000),
                                   ("EMPANADA  QUESO  FRITA", 1, 2000), ("TE", 1, 1000)])
    a = analizar(v)
    assert a.esperado == 9258 and a.cuadra  # $3 de diferencia, tolerancia $10
    Parametro.objects.create(clave="tolerancia_descuento_trabajador", valor=2)
    assert not analizar(v).cuadra


def test_descuento_que_no_calza_bloquea_hasta_que_contabilidad_lo_acepta(dia, encargada, jefe):
    v = _venta(dia, 679523, 7290, [("HELADO BARQUILLO DOBLE", 3, 3000)])  # tabla 20% = 7.200; Resto cobró 7.290
    a = analizar(v)
    assert a.estado == "no_cuadra" and a.descuento_tabla == 1800 and a.descuento == 1710
    assert "no calza con la tabla" in validar_venta(v)[0]
    with pytest.raises(PermisoDenegado):
        aceptar(v, encargada, "x")
    with pytest.raises(DescuentoInvalido, match="motivo"):
        aceptar(v, jefe, "  ")
    aceptar(v, jefe, "Resto aplicó 19% al helado doble")
    v.refresh_from_db()
    assert validar_venta(v) == [] and _filas_de_venta(v)[-1]["Líneas del pedido/Precio un."] == -1710
    assert EventoAuditoria.objects.filter(accion="aceptar_descuento_trabajador", entidad_id=str(v.pk)).exists()


def test_producto_fuera_de_tabla_se_informa(dia):
    v = _venta(dia, 1, 900, [("CAFE", 1, 1000)])
    a = analizar(v)
    assert a.sin_tabla == ["CAFE"] and not a.valido


def test_cliente_ocasional_que_no_cuadra_sigue_bloqueando(dia):
    v = _venta(dia, 2, 1520, [("BEBIDA GASEOSA", 1, 2000)], cliente="CLIENTE OCASIONAL")
    assert analizar(v) is None
    assert "no cuadra con la suma de líneas" in validar_venta(v)[0]


def test_trabajador_sin_descuento_no_aparece(dia):
    v = _venta(dia, 3, 2000, [("BEBIDA GASEOSA", 1, 2000)])
    assert analizar(v) is None and del_dia(dia) == []


def test_reportes_usan_lo_cobrado_y_muestran_el_descuento(dia):
    _venta(dia, 679722, 1520, [("BEBIDA GASEOSA", 1, 2000)])
    _venta(dia, 4, 2000, [("BEBIDA GASEOSA", 1, 2000)], cliente="CLIENTE OCASIONAL")
    filas = {f["producto"]: f for f in resumen_por_producto(FECHA, FECHA)}
    assert filas["BEBIDA GASEOSA"]["venta"] == 3520 and filas["BEBIDA GASEOSA"]["descuento"] == 480
    r = resumen_periodo(FECHA, FECHA)
    assert r["control"]["descuento_trabajadores"] == 480 and r["control"]["comandas_con_descuento"] == 1
    assert r["top_productos"][0]["venta"] == 3520


def test_la_carga_cuenta_como_cuadradas_las_comandas_con_descuento_valido(dia):
    _venta(dia, 679722, 1520, [("BEBIDA GASEOSA", 1, 2000)])
    dia.lote.avisos = ["Comanda 679722: total $1,520 no coincide con la suma de líneas $2,000."]
    dia.lote.save()
    fila = next(f for f in validaciones_de_carga(dia) if f["validacion"] == "Total de comanda = suma de líneas")
    assert fila["ok"] and "1 con descuento a trabajador" in fila["detalle"]


def test_configuracion_de_la_tabla(db, django_user_model, jefe):
    karla = crear_usuario(django_user_model, "karla", GRUPO_GERENTE)
    with pytest.raises(ConfiguracionInvalida, match="ya está"):
        crear_descuento("empanada queso  cruda", 10, karla)  # mismo producto, otros espacios
    with pytest.raises(ConfiguracionInvalida, match="entre 0 y 100"):
        crear_descuento("CAFE", 120, karla)
    with pytest.raises(PermisoDenegado):
        crear_descuento("CAFE", 10, jefe)
    d = crear_descuento("cafe  grano", "12,5", karla)
    assert d.producto == "CAFE GRANO" and d.porcentaje == Decimal("12.50")
    editar_descuento(d, porcentaje="10", activo=False, usuario=karla)
    d.refresh_from_db()
    assert d.porcentaje == 10 and not d.activo


def test_cierre_del_dia_muestra_los_descuentos_y_permite_aceptar(client, dia, jefe, encargada):
    _venta(dia, 679722, 1520, [("BEBIDA GASEOSA", 1, 2000)])
    v = _venta(dia, 679523, 7290, [("HELADO BARQUILLO DOBLE", 3, 3000)])
    url = reverse("cierre_y_exportacion", args=[FECHA.isoformat()])
    client.force_login(encargada)
    html = client.get(url).content.decode()
    assert "Descuentos a trabajadores · 2" in html and "Calza con la tabla" in html and "No calza" in html
    assert reverse("aceptar_descuento", args=[v.pk]) not in html
    client.force_login(jefe)
    assert reverse("aceptar_descuento", args=[v.pk]) in client.get(url).content.decode()
    client.post(reverse("aceptar_descuento", args=[v.pk]), {"motivo": "revisado con la boleta"})
    v.refresh_from_db()
    assert v.descuento_aceptado_por == jefe
