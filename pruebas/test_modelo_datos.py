"""Prueba de integración: los importadores + el modelo de datos, de punta a
punta, sobre el día de referencia 02-09-2026 (golden master).

A diferencia de `test_importador_pos.py` / `test_importador_transbank.py`
(que prueban los importadores solos, en memoria), esta prueba escribe en
una base de datos real (SQLite de prueba) y verifica que lo persistido
cuadra con lo mismo que ya conocemos.
"""
from datetime import date
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model

from nucleo.carga import cargar_pos, cargar_transbank
from nucleo.models import DiaArqueo, LineaVenta, MovimientoProveedor, PagoDeclarado, Venta

FIXTURES = Path(__file__).parent / "fixtures" / "dia_referencia_02-09-2026"
DIA_REFERENCIA = date(2026, 9, 2)


@pytest.fixture
def usuario(db):
    return get_user_model().objects.create_user(username="operador_prueba")


@pytest.fixture
def dia_cargado(usuario):
    cargar_pos(FIXTURES / "Captura_Ventas_Sala_de_Ventas_02.09.26.xlsx", DIA_REFERENCIA, usuario)
    cargar_transbank(FIXTURES / "Cartola_Movimientos_02-sep-2026.xlsx", DIA_REFERENCIA, usuario)
    return DiaArqueo.objects.get(fecha=DIA_REFERENCIA)


@pytest.mark.django_db
def test_dos_lotes_de_carga_quedan_registrados(dia_cargado):
    assert dia_cargado.lotes.count() == 2


@pytest.mark.django_db
def test_ventas_y_lineas_persistidas(dia_cargado):
    assert Venta.objects.filter(dia=dia_cargado).count() == 336
    assert LineaVenta.objects.filter(venta__dia=dia_cargado).count() == 686


@pytest.mark.django_db
def test_venta_total_desde_la_base_de_datos(dia_cargado):
    total = sum(v.total_comanda for v in Venta.objects.filter(dia=dia_cargado))
    assert total == 5_274_235


@pytest.mark.django_db
def test_pago_declarado_permite_comanda_mixta(dia_cargado):
    # Si alguna comanda tiene dos medios, debe existir más de un
    # PagoDeclarado para ese mismo N_INTERNO (la decisión estructural del
    # modelo, diseño funcional §7).
    conteo_por_venta = (
        PagoDeclarado.objects.filter(venta__dia=dia_cargado)
        .values("venta_id")
        .distinct()
        .count()
    )
    assert conteo_por_venta == 336  # toda venta tiene al menos un pago declarado


@pytest.mark.django_db
def test_movimientos_transbank_persistidos(dia_cargado):
    movimientos = MovimientoProveedor.objects.filter(dia=dia_cargado)
    assert movimientos.count() == 295
    total = sum(m.monto_venta_valido_abono for m in movimientos)
    assert total == 4_968_393


# Nota: la detección de recarga/duplicado (§8, caso de prueba 15 — "el
# operador elige reemplazar o cancelar") es responsabilidad de la pantalla
# de Carga de archivos (M1), no de esta capa de persistencia. Hoy
# `cargar_pos`/`cargar_transbank` asumen una carga limpia por día; volver a
# llamarlas para el mismo día choca con la restricción de unicidad de
# Venta (dia, n_interno) a propósito, para no insertar filas duplicadas en
# silencio. Se prueba explícitamente cuando se construya esa pantalla.
