"""Casos reales levantados por la Encargada de Ingresos (correo del 15-10-2026).

1. Un cobro de Transbank que pagó dos boletas ($17.664 + $1.500 = $19.164).
2. Una boleta pagada con dos cobros ($25.900 + $2.500 = $28.400), incluso cuando
   uno de los cobros quedó marcado como «pago duplicado».
3. La cajera cobró con tarjeta y registró efectivo ($10.000): la caja aparecía
   con faltante. Y el inverso: registró tarjeta y cobró en efectivo ($15.000).
"""
from datetime import date, datetime, time
from decimal import Decimal

import pytest

from nucleo.arqueo_servicio import preparar_arqueos_del_dia
from nucleo.conciliacion_servicio import ejecutar_conciliacion
from nucleo.models import (
    Coincidencia, DiaArqueo, Excepcion, LoteCarga, MotivoResolucion, MovimientoProveedor, PagoDeclarado,
    Reclasificacion, Venta,
)
from nucleo.reapertura_excepciones_servicio import reabrir_excepcion
from nucleo.resolucion_servicio import ResolucionInvalida, resolver_excepcion
from pruebas.perfiles_de_prueba import GRUPO_ENCARGADO, GRUPO_JEFE, crear_usuario

FECHA = date(2026, 10, 3)


@pytest.fixture
def nathaly(django_user_model):
    return crear_usuario(django_user_model, "nathaly", GRUPO_ENCARGADO)


@pytest.fixture
def ricardo(django_user_model):
    return crear_usuario(django_user_model, "ricardo", GRUPO_JEFE)


@pytest.fixture
def dia(db, nathaly):
    return DiaArqueo.objects.create(fecha=FECHA, operado_por=nathaly)


@pytest.fixture
def lotes(dia, nathaly):
    pos = LoteCarga.objects.create(
        dia=dia, fuente=LoteCarga.Fuente.POS, nombre_archivo="p", huella_sha256="p",
        ruta_almacenamiento="/p", fecha_declarada=FECHA, cargado_por=nathaly,
    )
    tbk = LoteCarga.objects.create(
        dia=dia, fuente=LoteCarga.Fuente.TRANSBANK, nombre_archivo="t", huella_sha256="t",
        ruta_almacenamiento="/t", fecha_declarada=FECHA, cargado_por=nathaly,
    )
    return pos, tbk


def boleta(dia, lote, n, monto, medio, caja="CAJA 2", hora=time(10, 20)):
    venta = Venta.objects.create(
        dia=dia, lote=lote, n_interno=n, nro_comprobante=f"B039030{n:07d}", tipo_doc="FAC", nro_comanda=str(n),
        estado="COBRADA", caja=caja, turno="T", hora_cierre=hora, total_comanda=monto, cliente="CLIENTE OCASIONAL",
    )
    return PagoDeclarado.objects.create(venta=venta, medio=medio, monto=monto)


def cobro(dia, lote, hora, monto, autorizacion, medio="debito"):
    return MovimientoProveedor.objects.create(
        dia=dia, lote=lote, proveedor="transbank", tipo="Venta", estado="PENDIENTE",
        fecha_hora=datetime(2026, 10, 3, *hora), medio=medio, tipo_tarjeta=medio,
        monto_original=monto, monto_venta_valido_abono=monto, autorizacion=autorizacion, terminal="A2885577",
    )


def motivo():
    return MotivoResolucion.objects.filter(aplica_a=MotivoResolucion.AplicaA.EXCEPCION, activo=True).first()


def abiertas(dia):
    return Excepcion.objects.filter(dia=dia, abierta=True)


def esperado(dia, caja):
    return {a.caja: a for a in preparar_arqueos_del_dia(dia)}[caja]


# ======================================================================= caso 1

@pytest.fixture
def un_cobro_dos_boletas(dia, lotes, nathaly):
    pos, tbk = lotes
    p1 = boleta(dia, pos, 671010, 17664, "debito", hora=time(10, 22))
    p2 = boleta(dia, pos, 671011, 1500, "debito", hora=time(10, 23))
    mov = cobro(dia, tbk, (10, 24), 19164, "006979")
    ejecutar_conciliacion(dia, nathaly)
    return p1, p2, mov


def _excepcion_del_cobro(dia, mov):
    return Excepcion.objects.get(dia=dia, abierta=True, movimiento=mov, tipo="movimiento_sin_pago")


def test_caso1_parte_como_tres_excepciones(dia, un_cobro_dos_boletas):
    tipos = sorted(abiertas(dia).values_list("tipo", flat=True))
    assert tipos == ["movimiento_sin_pago", "pago_sin_movimiento", "pago_sin_movimiento"]


def test_caso1_un_cobro_paga_dos_boletas_y_cierra_las_tres(dia, un_cobro_dos_boletas, nathaly):
    p1, p2, mov = un_cobro_dos_boletas
    exc = _excepcion_del_cobro(dia, mov)
    resolver_excepcion(exc, resolucion="agrupar", motivo_id=motivo().pk, comentario="", usuario=nathaly,
                       pagos_agrupados=[str(p1.pk), str(p2.pk)])
    assert not abiertas(dia).exists()
    dia.refresh_from_db()
    assert dia.estado == DiaArqueo.Estado.EXCEPCIONES_RESUELTAS
    c = Coincidencia.objects.get(dia=dia, origen="manual", movimientos=mov)
    assert set(c.pagos.values_list("id", flat=True)) == {p1.pk, p2.pk}
    # Reconciliar no lo vuelve a abrir.
    ejecutar_conciliacion(dia, nathaly)
    assert not abiertas(dia).exists()


def test_caso1_las_boletas_deben_sumar_exacto_el_cobro(dia, lotes, un_cobro_dos_boletas, nathaly):
    p1, _, mov = un_cobro_dos_boletas
    p3 = boleta(dia, lotes[0], 671099, 1400, "debito")
    ejecutar_conciliacion(dia, nathaly)
    exc = _excepcion_del_cobro(dia, mov)
    with pytest.raises(ResolucionInvalida, match="deben sumar"):
        resolver_excepcion(exc, resolucion="agrupar", motivo_id=motivo().pk, comentario="", usuario=nathaly,
                           pagos_agrupados=[p1.pk, p3.pk])
    with pytest.raises(ResolucionInvalida, match="al menos dos"):
        resolver_excepcion(exc, resolucion="agrupar", motivo_id=motivo().pk, comentario="", usuario=nathaly,
                           pagos_agrupados=[p1.pk])


def test_caso1_reabrir_el_cobro_reabre_las_tres(dia, un_cobro_dos_boletas, nathaly, ricardo):
    p1, p2, mov = un_cobro_dos_boletas
    exc = _excepcion_del_cobro(dia, mov)
    resolver_excepcion(exc, resolucion="agrupar", motivo_id=motivo().pk, comentario="", usuario=nathaly,
                       pagos_agrupados=[p1.pk, p2.pk])
    reabrir_excepcion(exc, ricardo, "Eran de otra venta")
    assert abiertas(dia).count() == 3
    assert not Coincidencia.objects.filter(dia=dia, origen="manual").exists()


# ======================================================================= caso 2

@pytest.fixture
def una_boleta_dos_cobros(dia, lotes, nathaly):
    pos, tbk = lotes
    pago = boleta(dia, pos, 671555, 28400, "credito", hora=time(12, 27))
    m1 = cobro(dia, tbk, (12, 26), 25900, "111111", medio="credito")
    m2 = cobro(dia, tbk, (12, 27), 2500, "222222", medio="credito")
    ejecutar_conciliacion(dia, nathaly)
    return pago, m1, m2


def _lineas(m1, m2):
    return [
        {"medio": "credito", "monto": "25900", "movimiento_id": m1.pk},
        {"medio": "credito", "monto": "2500", "movimiento_id": m2.pk},
    ]


def test_caso2_pagar_con_dos_cobros_cierra_tambien_los_cobros(dia, una_boleta_dos_cobros, nathaly):
    pago, m1, m2 = una_boleta_dos_cobros
    assert abiertas(dia).count() == 3
    exc = Excepcion.objects.get(dia=dia, abierta=True, pago=pago)
    resolver_excepcion(exc, resolucion="mezcla_medios", motivo_id=motivo().pk, comentario="", usuario=nathaly,
                       lineas=_lineas(m1, m2))
    assert not abiertas(dia).exists()  # antes quedaban abiertos los dos «movimiento sin pago»
    ejecutar_conciliacion(dia, nathaly)
    assert not abiertas(dia).exists()


def test_caso2_tambien_cierra_un_cobro_marcado_como_duplicado(dia, una_boleta_dos_cobros, nathaly):
    pago, m1, m2 = una_boleta_dos_cobros
    # El motor marcó el segundo cobro como «pago duplicado» (caso de $3.790 del 04-10).
    Excepcion.objects.filter(dia=dia, movimiento=m2).update(tipo=Excepcion.Tipo.PAGO_DUPLICADO)
    exc = Excepcion.objects.get(dia=dia, abierta=True, pago=pago)
    resolver_excepcion(exc, resolucion="mezcla_medios", motivo_id=motivo().pk, comentario="", usuario=nathaly,
                       lineas=_lineas(m1, m2))
    assert not abiertas(dia).exists()


def test_caso2_el_mismo_cobro_no_puede_ir_en_dos_lineas(dia, una_boleta_dos_cobros, nathaly):
    pago, m1, _ = una_boleta_dos_cobros
    exc = Excepcion.objects.get(dia=dia, abierta=True, pago=pago)
    with pytest.raises(ResolucionInvalida, match="dos líneas"):
        resolver_excepcion(exc, resolucion="mezcla_medios", motivo_id=motivo().pk, comentario="", usuario=nathaly,
                           lineas=[{"medio": "credito", "monto": "25900", "movimiento_id": m1.pk},
                                   {"medio": "credito", "monto": "2500", "movimiento_id": m1.pk}])


def test_caso2_reabrir_la_boleta_reabre_los_cobros(dia, una_boleta_dos_cobros, nathaly, ricardo):
    pago, m1, m2 = una_boleta_dos_cobros
    exc = Excepcion.objects.get(dia=dia, abierta=True, pago=pago)
    resolver_excepcion(exc, resolucion="mezcla_medios", motivo_id=motivo().pk, comentario="", usuario=nathaly,
                       lineas=_lineas(m1, m2))
    reabrir_excepcion(exc, ricardo, "Revisar")
    assert abiertas(dia).count() == 3


# ======================================================================= caso 3

@pytest.fixture
def tarjeta_registrada_como_efectivo(dia, lotes, nathaly):
    pos, tbk = lotes
    boleta(dia, pos, 672000, 138629, "efectivo", caja="CAJA 1", hora=time(11, 0))
    pago = boleta(dia, pos, 672001, 10000, "efectivo", caja="CAJA 1", hora=time(17, 0))
    mov = cobro(dia, tbk, (17, 1), 10000, "003874")
    ejecutar_conciliacion(dia, nathaly)  # se concilió antes de contar: la regla automática no pudo actuar
    return pago, mov


def test_caso3_corregir_medio_baja_el_efectivo_esperado(dia, tarjeta_registrada_como_efectivo, nathaly):
    pago, mov = tarjeta_registrada_como_efectivo
    assert esperado(dia, "CAJA 1").esperado == Decimal(148629)
    exc = _excepcion_del_cobro(dia, mov)

    resolver_excepcion(exc, resolucion="corregir_medio", motivo_id=motivo().pk, comentario="Cobró con débito",
                       usuario=nathaly, pago_objetivo_id=pago.pk)

    assert not abiertas(dia).exists()
    pago.refresh_from_db()
    assert pago.medio == "efectivo" and pago.medio_aprobado == "debito"  # lo declarado no se toca
    arqueo = esperado(dia, "CAJA 1")
    assert arqueo.ajustes == Decimal(-10000) and arqueo.esperado == Decimal(138629)
    r = Reclasificacion.objects.get(pago=pago)
    assert r.medio_original == "efectivo" and r.medio_corregido == "debito" and r.evidencia["movimiento_id"] == mov.pk
    ejecutar_conciliacion(dia, nathaly)
    assert not abiertas(dia).exists()
    assert esperado(dia, "CAJA 1").esperado == Decimal(138629)


def test_caso3_valida_monto_y_medio_de_la_boleta(dia, lotes, tarjeta_registrada_como_efectivo, nathaly):
    _, mov = tarjeta_registrada_como_efectivo
    otra = boleta(dia, lotes[0], 672002, 9000, "efectivo", caja="CAJA 1")
    tarjeta = boleta(dia, lotes[0], 672003, 10000, "debito", caja="CAJA 1")
    exc = _excepcion_del_cobro(dia, mov)
    with pytest.raises(ResolucionInvalida, match="mismo monto"):
        resolver_excepcion(exc, resolucion="corregir_medio", motivo_id=motivo().pk, comentario="",
                           usuario=nathaly, pago_objetivo_id=otra.pk)
    with pytest.raises(ResolucionInvalida, match="efectivo"):
        resolver_excepcion(exc, resolucion="corregir_medio", motivo_id=motivo().pk, comentario="",
                           usuario=nathaly, pago_objetivo_id=tarjeta.pk)


def test_caso3_reabrir_devuelve_el_efectivo_esperado(dia, tarjeta_registrada_como_efectivo, nathaly, ricardo):
    pago, mov = tarjeta_registrada_como_efectivo
    exc = _excepcion_del_cobro(dia, mov)
    resolver_excepcion(exc, resolucion="corregir_medio", motivo_id=motivo().pk, comentario="",
                       usuario=nathaly, pago_objetivo_id=pago.pk)
    reabrir_excepcion(exc, ricardo, "Era otra boleta")
    exc.refresh_from_db()
    pago.refresh_from_db()
    assert exc.abierta and exc.pago_id is None and pago.medio_aprobado == ""
    assert esperado(dia, "CAJA 1").esperado == Decimal(148629)
    assert not Coincidencia.objects.filter(dia=dia, origen="manual").exists()


def test_caso3_inverso_registro_tarjeta_y_cobro_en_efectivo(dia, lotes, nathaly):
    pos, _ = lotes
    boleta(dia, pos, 678000, 298429, "efectivo", caja="CAJA 5")
    pago = boleta(dia, pos, 678752, 15000, "credito", caja="CAJA 5")
    ejecutar_conciliacion(dia, nathaly)
    exc = Excepcion.objects.get(dia=dia, abierta=True, pago=pago)
    with pytest.raises(ResolucionInvalida, match="medio"):
        resolver_excepcion(exc, resolucion="corregir_medio", motivo_id=motivo().pk, comentario="", usuario=nathaly)
    resolver_excepcion(exc, resolucion="corregir_medio", motivo_id=motivo().pk, comentario="", usuario=nathaly,
                       medio_destino="efectivo")
    arqueo = esperado(dia, "CAJA 5")
    assert arqueo.ajustes == Decimal(15000) and arqueo.esperado == Decimal(313429)


# ======================================================================= pantalla

def test_la_bandeja_ofrece_las_nuevas_salidas_y_resuelve_por_la_ruta(client, dia, un_cobro_dos_boletas, nathaly):
    from django.urls import reverse
    p1, p2, mov = un_cobro_dos_boletas
    exc = _excepcion_del_cobro(dia, mov)
    client.force_login(nathaly)
    html = client.get(reverse("bandeja_excepciones", args=[FECHA.isoformat()]) + f"?ver={exc.pk}").content.decode()
    assert "¿Este cobro pagó varias boletas?" in html and "Comanda 671010" in html and "Comanda 671011" in html
    assert "¿Es una venta que la cajera registró en efectivo?" in html

    psm = Excepcion.objects.get(dia=dia, abierta=True, pago=p1)
    html = client.get(reverse("bandeja_excepciones", args=[FECHA.isoformat()]) + f"?ver={psm.pk}").content.decode()
    assert "Pagar con varios cobros o medios" in html and "¿Se pagó en efectivo?" in html

    client.post(reverse("resolver_excepcion", args=[exc.pk]), {
        "resolucion": "agrupar", "motivo_id": motivo().pk, "pagos_agrupados": [p1.pk, p2.pk],
    })
    assert not abiertas(dia).exists()


# ======================================================================= pérdida/ganancia con varios cobros

def test_ganancia_con_dos_cobros_cajera_cobro_de_mas(dia, lotes, nathaly, ricardo):
    """Día 12, comanda 677493: boleta $29.700 cobrada con débito $24.900 + $5.000 → $200 de ganancia."""
    from nucleo.arqueo_caja_servicio import generar_lineas_arqueo_caja
    from nucleo.models import Cajera, CuentaContable
    pos, tbk = lotes
    pago = boleta(dia, pos, 677493, 29700, "debito", caja="CAJA 5", hora=time(14, 11))
    m1 = cobro(dia, tbk, (14, 12), 24900, "444444")
    m2 = cobro(dia, tbk, (14, 13), 5000, "000842")
    ejecutar_conciliacion(dia, nathaly)
    exc = Excepcion.objects.get(dia=dia, abierta=True, pago=pago)
    cajera = Cajera.objects.create(nombre="Cajera Cinco")
    ganancia = CuentaContable.objects.get(codigo="6501003")
    perdida = CuentaContable.objects.get(codigo="6501004")

    with pytest.raises(ResolucionInvalida, match="ganancia"):
        resolver_excepcion(exc, resolucion="perdida_ganancia", motivo_id=motivo().pk, comentario="", usuario=nathaly,
                           movimiento_id_alternativo=m1.pk, movimientos_adicionales=[m2.pk],
                           cuenta_contable_id=perdida.pk, cajera_id=cajera.pk)

    resolver_excepcion(exc, resolucion="perdida_ganancia", motivo_id=motivo().pk, comentario="Cobró $200 de más",
                       usuario=nathaly, movimiento_id_alternativo=m1.pk, movimientos_adicionales=[str(m2.pk), ""],
                       cuenta_contable_id=ganancia.pk, cajera_id=cajera.pk)

    exc.refresh_from_db()
    assert exc.monto_perdida_ganancia == Decimal(200) and exc.cuenta_contable == ganancia
    assert not abiertas(dia).exists()  # boleta y los dos cobros
    c = Coincidencia.objects.get(dia=dia, origen="manual", pagos=pago)
    assert set(c.movimientos.values_list("id", flat=True)) == {m1.pk, m2.pk}
    filas = generar_lineas_arqueo_caja(dia)
    assert any("6501003" in str(f) and "Cajera Cinco" in str(f) for f in filas)

    ejecutar_conciliacion(dia, nathaly)
    assert not abiertas(dia).exists()
    reabrir_excepcion(exc, ricardo, "Revisar")
    assert abiertas(dia).count() == 3
