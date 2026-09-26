"""Arqueo de efectivo (Etapa 5): cálculo del teórico por caja, registro del
conteo por denominación (D8), y la autorización de diferencias sobre el
umbral de $5.000 (D3), reservada a un aprobador (D10).
"""
from datetime import date, time
from decimal import Decimal

import pytest
from pruebas.perfiles_de_prueba import GRUPO_ENCARGADO, GRUPO_GERENTE, crear_usuario
from django.urls import reverse

from nucleo.arqueo_servicio import (
    AutorizacionInvalida, GastoCajaInvalido, autorizar_diferencia, calcular_teorico_por_caja,
    preparar_arqueos_del_dia, registrar_conteo, registrar_gasto_caja,
)
from nucleo.models import ArqueoEfectivo, DiaArqueo, GastoCaja, LoteCarga, MotivoResolucion, PagoDeclarado, Venta

FECHA = date(2026, 9, 2)


@pytest.fixture
def usuario(django_user_model):
    return crear_usuario(django_user_model, "operador", GRUPO_ENCARGADO, password="clave-segura-123")


@pytest.fixture
def aprobador(django_user_model):
    return crear_usuario(django_user_model, "karla", GRUPO_GERENTE, password="clave-segura-123")


def _crear_venta(dia, lote, n_interno, caja, total, medio="efectivo"):
    venta = Venta.objects.create(
        dia=dia, lote=lote, n_interno=n_interno, nro_comprobante=f"B{n_interno}", tipo_doc="FAC",
        nro_comanda=str(n_interno), estado="COBRADA", caja=caja, turno="TURNO 1",
        hora_cierre=time(14, 0), total_comanda=total, cliente="CLIENTE OCASIONAL",
    )
    PagoDeclarado.objects.create(venta=venta, medio=medio, monto=total)
    return venta


@pytest.fixture
def dia_con_ventas(db, usuario):
    dia = DiaArqueo.objects.create(fecha=FECHA)
    lote = LoteCarga.objects.create(
        dia=dia, fuente=LoteCarga.Fuente.POS, nombre_archivo="x.xlsx", huella_sha256="a",
        ruta_almacenamiento="/tmp/x", fecha_declarada=FECHA, cargado_por=usuario,
    )
    _crear_venta(dia, lote, 1, "CAJA 1", 30000, medio="efectivo")
    _crear_venta(dia, lote, 2, "CAJA 1", 20000, medio="efectivo")
    _crear_venta(dia, lote, 3, "CAJA 2", 15000, medio="efectivo")
    _crear_venta(dia, lote, 4, "CAJA 2", 5000, medio="debito")  # no debe contar como efectivo
    return dia


def test_calcula_teorico_por_caja(dia_con_ventas):
    totales = calcular_teorico_por_caja(dia_con_ventas)
    assert totales == {"CAJA 1": Decimal(50000), "CAJA 2": Decimal(15000)}


def test_preparar_arqueos_crea_uno_por_caja(dia_con_ventas):
    arqueos = preparar_arqueos_del_dia(dia_con_ventas)
    assert {a.caja for a in arqueos} == {"CAJA 1", "CAJA 2"}
    caja1 = next(a for a in arqueos if a.caja == "CAJA 1")
    assert caja1.teorico == 50000
    assert caja1.esperado == 50000
    assert caja1.estado == "pendiente"


def test_registrar_conteo_calcula_diferencia_con_el_total_contado(dia_con_ventas, usuario):
    arqueo = preparar_arqueos_del_dia(dia_con_ventas)[0]  # CAJA 1, esperado 50.000
    registrar_conteo(
        arqueo, fondo_inicial=Decimal(10000), gastos_autorizados=Decimal(0),
        total_contado=Decimal(62000), usuario=usuario,  # 60.000 + 2.000 = 62.000
    )
    arqueo.refresh_from_db()
    assert arqueo.contado == 62000
    # esperado(50.000) + fondo(10.000) - gastos(0) = 60.000 ; contado 62.000 -> +2.000
    assert arqueo.diferencia == 2000
    assert arqueo.estado == "dentro_de_umbral"


def test_diferencia_sobre_el_umbral_requiere_autorizacion(dia_con_ventas, usuario):
    arqueo = preparar_arqueos_del_dia(dia_con_ventas)[0]
    registrar_conteo(
        arqueo, fondo_inicial=Decimal(0), gastos_autorizados=Decimal(0),
        total_contado=Decimal(60000), usuario=usuario,  # 60.000 vs esperado 50.000 -> +10.000
    )
    arqueo.refresh_from_db()
    assert arqueo.diferencia == 10000
    assert arqueo.estado == "requiere_autorizacion"


def test_autorizar_diferencia_exige_motivo_de_la_lista_cerrada(dia_con_ventas, usuario, aprobador):
    arqueo = preparar_arqueos_del_dia(dia_con_ventas)[0]
    registrar_conteo(arqueo, fondo_inicial=Decimal(0), gastos_autorizados=Decimal(0),
                      total_contado=Decimal(60000), usuario=usuario)
    with pytest.raises(AutorizacionInvalida):
        autorizar_diferencia(arqueo, motivo_id=None, comentario="", usuario=aprobador)

    motivo = MotivoResolucion.objects.get(codigo="faltante_en_investigacion")
    arqueo.refresh_from_db()
    autorizar_diferencia(arqueo, motivo_id=motivo.pk, comentario="Revisado con el cajero.", usuario=aprobador)
    arqueo.refresh_from_db()
    assert arqueo.autorizado_por == aprobador
    assert arqueo.estado == "autorizada"


def test_no_se_puede_autorizar_dentro_del_umbral(dia_con_ventas, usuario, aprobador):
    arqueo = preparar_arqueos_del_dia(dia_con_ventas)[0]
    registrar_conteo(arqueo, fondo_inicial=Decimal(0), gastos_autorizados=Decimal(0),
                      total_contado=Decimal(50000), usuario=usuario)  # 50.000, diferencia 0
    motivo = MotivoResolucion.objects.get(codigo="faltante_en_investigacion")
    with pytest.raises(AutorizacionInvalida):
        autorizar_diferencia(arqueo, motivo_id=motivo.pk, comentario="", usuario=aprobador)


def test_nueva_cuenta_invalida_autorizacion_anterior(dia_con_ventas, usuario, aprobador):
    arqueo = preparar_arqueos_del_dia(dia_con_ventas)[0]
    registrar_conteo(arqueo, fondo_inicial=Decimal(0), gastos_autorizados=Decimal(0),
                      total_contado=Decimal(60000), usuario=usuario)
    motivo = MotivoResolucion.objects.get(codigo="faltante_en_investigacion")
    autorizar_diferencia(arqueo, motivo_id=motivo.pk, comentario="", usuario=aprobador)

    registrar_conteo(arqueo, fondo_inicial=Decimal(0), gastos_autorizados=Decimal(0),
                      total_contado=Decimal(80000), usuario=usuario)  # cifra distinta
    arqueo.refresh_from_db()
    assert arqueo.autorizado_por is None  # la autorización anterior no se arrastra


def test_vista_arqueo_requiere_login(client, dia_con_ventas):
    resp = client.get(reverse("arqueo_efectivo", args=[FECHA.isoformat()]))
    assert resp.status_code == 302


def test_operador_no_puede_autorizar_diferencia(client, usuario, dia_con_ventas):
    client.force_login(usuario)
    arqueo = preparar_arqueos_del_dia(dia_con_ventas)[0]
    registrar_conteo(arqueo, fondo_inicial=Decimal(0), gastos_autorizados=Decimal(0),
                      total_contado=Decimal(60000), usuario=usuario)
    motivo = MotivoResolucion.objects.get(codigo="faltante_en_investigacion")
    resp = client.post(
        reverse("autorizar_diferencia", args=[arqueo.pk]),
        {"motivo_id": motivo.pk, "comentario": "intento sin ser aprobador"},
    )
    assert resp.status_code == 403
    arqueo.refresh_from_db()
    assert arqueo.autorizado_por is None


def test_aprobador_si_puede_autorizar_por_la_vista(client, aprobador, dia_con_ventas, usuario):
    client.force_login(aprobador)
    arqueo = preparar_arqueos_del_dia(dia_con_ventas)[0]
    registrar_conteo(arqueo, fondo_inicial=Decimal(0), gastos_autorizados=Decimal(0),
                      total_contado=Decimal(60000), usuario=usuario)
    motivo = MotivoResolucion.objects.get(codigo="faltante_en_investigacion")
    resp = client.post(
        reverse("autorizar_diferencia", args=[arqueo.pk]),
        {"motivo_id": motivo.pk, "comentario": "confirmado"},
    )
    assert resp.status_code == 302
    arqueo.refresh_from_db()
    assert arqueo.autorizado_por == aprobador


def test_registrar_gasto_caja_suma_al_total_y_recalcula_diferencia(dia_con_ventas, usuario):
    """Karla, 2026-09-18: una devolución en efectivo por NCR (pagada
    originalmente con Transbank) sale como gasto de caja — trazable por
    separado, no un solo número."""
    arqueo = preparar_arqueos_del_dia(dia_con_ventas)[0]  # CAJA 1, esperado 50.000
    registrar_conteo(
        arqueo, fondo_inicial=Decimal(0), gastos_autorizados=Decimal(0),
        total_contado=Decimal(50000), usuario=usuario,  # 50.000, diferencia 0
    )
    motivo = MotivoResolucion.objects.get(codigo="devolucion_ncr")

    gasto = registrar_gasto_caja(
        arqueo, monto=Decimal(5000), motivo_id=motivo.pk, boleta_asociada="B0000123",
        comentario="NCR por producto defectuoso", usuario=usuario,
    )
    arqueo.refresh_from_db()
    assert gasto.boleta_asociada == "B0000123"
    assert arqueo.gastos_autorizados == 5000
    # esperado(50.000) + fondo(0) - gastos(5.000) = 45.000 ; contado sigue en 50.000 -> +5.000
    assert arqueo.diferencia == 5000

    # Una segunda línea se suma a la primera, no la reemplaza.
    registrar_gasto_caja(
        arqueo, monto=Decimal(1000), motivo_id=motivo.pk, boleta_asociada="",
        comentario="", usuario=usuario,
    )
    arqueo.refresh_from_db()
    assert arqueo.gastos_autorizados == 6000
    assert arqueo.gastos.count() == 2


def test_registrar_gasto_caja_exige_monto_positivo_y_motivo_valido(dia_con_ventas, usuario):
    arqueo = preparar_arqueos_del_dia(dia_con_ventas)[0]
    motivo = MotivoResolucion.objects.get(codigo="devolucion_ncr")
    with pytest.raises(GastoCajaInvalido):
        registrar_gasto_caja(arqueo, monto=Decimal(0), motivo_id=motivo.pk, boleta_asociada="",
                              comentario="", usuario=usuario)
    with pytest.raises(GastoCajaInvalido):
        registrar_gasto_caja(arqueo, monto=Decimal(1000), motivo_id=None, boleta_asociada="",
                              comentario="", usuario=usuario)
    # Un motivo de otra lista (ej. diferencia de caja) no sirve para gasto de caja.
    otro_motivo = MotivoResolucion.objects.get(codigo="faltante_en_investigacion")
    with pytest.raises(GastoCajaInvalido):
        registrar_gasto_caja(arqueo, monto=Decimal(1000), motivo_id=otro_motivo.pk, boleta_asociada="",
                              comentario="", usuario=usuario)
    assert not GastoCaja.objects.exists()


def test_registrar_conteo_de_nuevo_no_borra_los_gastos_ya_registrados(dia_con_ventas, usuario):
    arqueo = preparar_arqueos_del_dia(dia_con_ventas)[0]
    motivo = MotivoResolucion.objects.get(codigo="devolucion_ncr")
    registrar_gasto_caja(arqueo, monto=Decimal(5000), motivo_id=motivo.pk, boleta_asociada="",
                          comentario="", usuario=usuario)
    arqueo.refresh_from_db()
    assert arqueo.gastos_autorizados == 5000

    # Recontar (ej. corrección del total) no debe resetear los gastos ya registrados.
    registrar_conteo(arqueo, fondo_inicial=Decimal(0), gastos_autorizados=arqueo.gastos_autorizados,
                      total_contado=Decimal(50000), usuario=usuario)
    arqueo.refresh_from_db()
    assert arqueo.gastos_autorizados == 5000


def test_vista_registrar_gasto_caja(client, usuario, dia_con_ventas):
    client.force_login(usuario)
    arqueo = preparar_arqueos_del_dia(dia_con_ventas)[0]
    motivo = MotivoResolucion.objects.get(codigo="devolucion_ncr")
    resp = client.post(
        reverse("registrar_gasto_caja", args=[arqueo.pk]),
        {"monto": "5000", "motivo_id": motivo.pk, "boleta_asociada": "B0000123", "comentario": ""},
    )
    assert resp.status_code == 302
    arqueo.refresh_from_db()
    assert arqueo.gastos_autorizados == 5000
    assert arqueo.gastos.get().boleta_asociada == "B0000123"


@pytest.mark.parametrize("escrito", ["62000", "62.000", "$62.000", " 62 000 "])
def test_la_vista_acepta_el_total_contado_como_se_escribe_en_chile(client, usuario, dia_con_ventas, escrito):
    client.force_login(usuario)
    arqueo = ArqueoEfectivo.objects.filter(dia=dia_con_ventas).first() or preparar_arqueos_del_dia(dia_con_ventas)[0]
    client.post(reverse("registrar_conteo", args=[arqueo.pk]), {"fondo_inicial": "0", "total_contado": escrito})
    arqueo.refresh_from_db()
    assert arqueo.contado == 62000


def test_la_vista_rechaza_un_conteo_sin_total(client, usuario, dia_con_ventas):
    client.force_login(usuario)
    arqueo = preparar_arqueos_del_dia(dia_con_ventas)[0]
    client.post(reverse("registrar_conteo", args=[arqueo.pk]), {"fondo_inicial": "0", "total_contado": ""})
    arqueo.refresh_from_db()
    assert arqueo.contado is None


# ---------------------- guías de despacho pagadas en efectivo (Tesorero, 2026-09-22)

def _crear_guia(dia, tipo, n_interno, folio, monto, caja, forma_pago="efectivo"):
    from nucleo.models import RegularizacionDocumento
    return RegularizacionDocumento.objects.create(
        dia=dia, tipo=tipo, n_interno=n_interno, folio=folio, caja=caja, monto=monto, forma_pago=forma_pago,
    )


def test_una_guia_pagada_en_efectivo_se_suma_al_teorico_de_su_caja(dia_con_ventas):
    _crear_guia(dia_con_ventas, "DES", 500, "21184", 10000, "CAJA 1")
    totales = calcular_teorico_por_caja(dia_con_ventas)
    assert totales["CAJA 1"] == Decimal(60000)  # 50.000 de ventas + 10.000 de la guía


def test_una_guia_pagada_con_tarjeta_no_se_suma_al_efectivo(dia_con_ventas):
    _crear_guia(dia_con_ventas, "DES", 500, "21184", 10000, "CAJA 1", forma_pago="debito")
    totales = calcular_teorico_por_caja(dia_con_ventas)
    assert totales["CAJA 1"] == Decimal(50000)


def test_por_defecto_una_nota_de_credito_no_resta_del_efectivo(dia_con_ventas):
    """Karla, 2026-09-24: la nota no resta del efectivo de la caja (caso real del
    07-09: la caja 3 se contó igual al esperado sin descontar las notas)."""
    _crear_guia(dia_con_ventas, "CRED", 500, "N77", 5000, "CAJA 1", forma_pago="credito")
    assert calcular_teorico_por_caja(dia_con_ventas)["CAJA 1"] == Decimal(50000)


def test_una_nota_nunca_resta_ni_verificada_como_efectivo(dia_con_ventas):
    """Caso real 07-09: al verificar las 2 notas como «efectivo» la caja 3 mostró un sobrante falso de
    $53.102, porque Resto ya trae la nota descontada. La verificación es solo respaldo documental."""
    nota = _crear_guia(dia_con_ventas, "CRED", 500, "N77", 5000, "CAJA 1")
    assert calcular_teorico_por_caja(dia_con_ventas)["CAJA 1"] == Decimal(50000)
    nota.verificacion_estado, nota.medio_verificado = "verificada", "efectivo"
    nota.save()
    assert calcular_teorico_por_caja(dia_con_ventas)["CAJA 1"] == Decimal(50000)


def test_la_nota_de_efectivo_sin_evidencia_queda_avisada_como_no_descontada(dia_con_ventas):
    from nucleo.regularizacion_servicio import avisos_efectivo_guias
    _crear_guia(dia_con_ventas, "CRED", 500, "N77", 5000, "CAJA 1")
    assert any("N77" in a and "falta el respaldo" in a for a in avisos_efectivo_guias(dia_con_ventas))


def test_las_guias_de_la_caja_se_listan_junto_a_su_arqueo(dia_con_ventas, client, usuario):
    _crear_guia(dia_con_ventas, "DES", 500, "21197", 23830, "CAJA 1")
    _crear_guia(dia_con_ventas, "DES", 501, "21198", 18301, "CAJA 1", forma_pago="debito")
    client.force_login(usuario)
    html = client.get(reverse("arqueo_efectivo", args=["2026-09-02"])).content.decode()
    assert "Guías y notas de crédito en efectivo de CAJA 1" in html
    assert "21197" in html and "21198" not in html  # la de débito no mueve efectivo


def test_una_guia_en_efectivo_sin_caja_no_se_suma_y_queda_avisada(dia_con_ventas):
    from nucleo.regularizacion_servicio import avisos_efectivo_guias
    _crear_guia(dia_con_ventas, "DES", 500, "21184", 10000, "")
    totales = calcular_teorico_por_caja(dia_con_ventas)
    assert totales.get("") is None
    assert totales["CAJA 1"] == Decimal(50000)
    avisos = avisos_efectivo_guias(dia_con_ventas)
    assert any("21184" in a and "sin caja" in a for a in avisos)


def test_una_guia_con_medios_mezclados_no_se_adivina_y_queda_avisada(dia_con_ventas):
    from nucleo.regularizacion_servicio import avisos_efectivo_guias
    _crear_guia(dia_con_ventas, "DES", 500, "21184", 117406, "CAJA 1", forma_pago="")
    totales = calcular_teorico_por_caja(dia_con_ventas)
    assert totales["CAJA 1"] == Decimal(50000)  # no se adivinó cuánto de eso era efectivo
    avisos = avisos_efectivo_guias(dia_con_ventas)
    assert any("21184" in a and "más de un medio" in a for a in avisos)
