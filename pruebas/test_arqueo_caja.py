"""Fase 2 (Karla, 2026-09-21): el asiento de arqueo de caja, segundo
entregable junto al archivo de ventas. Refleja lo realmente comprobado,
usando siempre las dos cuentas fijas (pérdida/ganancia), con la cajera (o
TRANSBANK) como contacto, y bloquea la exportación mientras haya una
diferencia sobre $50.000 sin visar — mismo control que ya existía para
pérdidas de tarjeta."""
from datetime import date, datetime, time
from decimal import Decimal

import pytest
from nucleo.permisos import PermisoDenegado
from pruebas.perfiles_de_prueba import GRUPO_ENCARGADO, GRUPO_GERENTE, crear_usuario
from django.urls import reverse

from nucleo.arqueo_caja_servicio import ArqueoCajaInvalido, generar_lineas_arqueo_caja
from nucleo.arqueo_servicio import preparar_arqueos_del_dia, registrar_conteo
from nucleo.cierre_servicio import aprobar_y_cerrar_dia
from nucleo.conciliacion_servicio import ejecutar_conciliacion
from nucleo.exportacion_servicio import ExportacionInvalida, generar_exportacion
from nucleo.models import (
    ArqueoEfectivo, Cajera, ConfiguracionArqueoCaja, CuentaContable, DiaArqueo, Excepcion, LineaVenta,
    LoteCarga, MotivoResolucion, MovimientoProveedor, PagoDeclarado, Venta,
)
from nucleo.perdidas_servicio import VisadoInvalido, diferencias_de_caja_pendientes_de_visado, visar_diferencia_caja
from nucleo.resolucion_servicio import resolver_excepcion

FECHA = date(2026, 9, 1)


@pytest.fixture
def usuario(django_user_model):
    return crear_usuario(django_user_model, "operador", GRUPO_GERENTE)


@pytest.fixture
def jefe(django_user_model):
    from django.contrib.auth.models import Group
    from nucleo.documentos_servicio import GRUPO_JEFE_CONTABILIDAD
    u = django_user_model.objects.create_user(username="jefe", password="x")
    u.groups.add(Group.objects.get_or_create(name=GRUPO_JEFE_CONTABILIDAD)[0])
    return u


@pytest.fixture
def cajera():
    return Cajera.objects.create(nombre="Josefina Lorca")


@pytest.fixture
def dia(db, usuario):
    return DiaArqueo.objects.create(fecha=FECHA, operado_por=usuario)


def _venta_efectivo(dia, n, monto, caja="CAJA 1"):
    lote, _ = LoteCarga.objects.get_or_create(
        dia=dia, fuente=LoteCarga.Fuente.POS, defaults={
            "nombre_archivo": "x", "huella_sha256": "a", "ruta_almacenamiento": "/x",
            "fecha_declarada": FECHA, "cargado_por": dia.operado_por,
        },
    )
    venta = Venta.objects.create(
        dia=dia, lote=lote, n_interno=n, nro_comprobante=f"B{n:07d}", tipo_doc="FAC", nro_comanda=str(n),
        estado="COBRADA", caja=caja, turno="T", hora_cierre=time(10, 0), total_comanda=monto,
        cliente="CLIENTE OCASIONAL",
    )
    LineaVenta.objects.create(venta=venta, articulo="X", cantidad=1, precio_unitario=monto, total_linea=monto)
    return PagoDeclarado.objects.create(venta=venta, medio="efectivo", monto=monto)


def _contar(dia, caja, total, cajera_obj=None):
    preparar_arqueos_del_dia(dia)  # calcula el esperado (teórico ± ajustes) antes de contar
    arqueo = ArqueoEfectivo.objects.get(dia=dia, caja=caja)
    if cajera_obj:
        arqueo.cajera = cajera_obj
        arqueo.save(update_fields=["cajera"])
    registrar_conteo(arqueo, fondo_inicial=Decimal(0), gastos_autorizados=Decimal(0),
                      total_contado=Decimal(total), usuario=dia.operado_por)
    return ArqueoEfectivo.objects.get(dia=dia, caja=caja)


# --------------------------------------------------------- diferencia de caja

def test_un_sobrante_de_caja_sale_como_ganancia_con_la_cajera_como_contacto(dia, cajera):
    _venta_efectivo(dia, 1, 20000)
    arqueo = _contar(dia, "CAJA 1", 20194, cajera)  # sobran $194, como el ejemplo real de Karla

    filas = generar_lineas_arqueo_caja(dia)
    assert len(filas) == 2
    balance, resultado = filas
    campos_de_pareja = {k: v for k, v in balance.items() if k not in ("Diario", "Fecha", "Referencia")}
    assert campos_de_pareja == {
        "Apuntes contables/Cuenta": "1101004 Efectivo Pos Resto",
        "Apuntes contables/Etiqueta": f"Diferencia Efectivo -Arqueo Caja Día {FECHA.strftime('%d/%m/%Y')}",
        "Apuntes contables/Empresa": "Josefina Lorca", "Apuntes contables/Distribución analítica": None,
        "Apuntes contables/Débito": 194, "Apuntes contables/Crédito": None,
    }
    assert resultado["Apuntes contables/Cuenta"] == "6501003 Ganancia por descuento de efectivo"
    assert resultado["Apuntes contables/Crédito"] == 194 and resultado["Apuntes contables/Débito"] is None
    assert resultado["Apuntes contables/Distribución analítica"] == '{"31": 100.0}'
    assert filas[0]["Diario"] == "Arqueo Caja Pos Resto" and filas[0]["Fecha"] == FECHA
    assert filas[1]["Diario"] is None and filas[1]["Fecha"] is None  # solo la primera fila del día
    assert filas[0]["Referencia"] == f"Arqueo Caja Día {FECHA:%d/%m/%Y}" and filas[1]["Referencia"] is None


def test_un_faltante_de_caja_sale_como_perdida(dia, cajera):
    _venta_efectivo(dia, 1, 300000, caja="CAJA 3")
    _contar(dia, "CAJA 3", 244046, cajera)  # esperado 300.000, contado 244.046 → faltan $55.954

    filas = generar_lineas_arqueo_caja(dia)
    resultado = next(f for f in filas if f["Apuntes contables/Cuenta"].startswith("6501004"))
    assert resultado["Apuntes contables/Débito"] == 55954
    assert resultado["Apuntes contables/Empresa"] == "Josefina Lorca"


def test_una_caja_sin_diferencia_no_deja_ninguna_linea(dia, cajera):
    _venta_efectivo(dia, 1, 20000)
    _contar(dia, "CAJA 1", 20000, cajera)
    assert generar_lineas_arqueo_caja(dia) == []


def test_una_diferencia_sin_cajera_asignada_impide_generar_el_asiento(dia):
    _venta_efectivo(dia, 1, 20000)
    _contar(dia, "CAJA 1", 20194)  # sin cajera
    with pytest.raises(ArqueoCajaInvalido, match="sin cajera asignada"):
        generar_lineas_arqueo_caja(dia)


def test_cada_caja_con_diferencia_es_una_linea_aparte_nunca_se_netean(dia, cajera):
    otra = Cajera.objects.create(nombre="María González")
    _venta_efectivo(dia, 1, 10000, caja="CAJA 1")
    _venta_efectivo(dia, 2, 10000, caja="CAJA 2")
    _contar(dia, "CAJA 1", 10100, cajera)   # +100
    _contar(dia, "CAJA 2", 9900, otra)      # -100

    filas = generar_lineas_arqueo_caja(dia)
    empresas_resultado = {f["Apuntes contables/Empresa"] for f in filas if f["Apuntes contables/Cuenta"].startswith(("6501003", "6501004"))}
    assert empresas_resultado == {"Josefina Lorca", "María González"}  # no se cancelan entre sí


# --------------------------------------------------- pérdida/ganancia (tarjeta)

def test_una_perdida_de_tarjeta_resuelta_a_mano_sale_con_su_cajera(dia, cajera):
    pago = _venta_efectivo(dia, 1, 10000)
    pago.medio, pago.monto = "credito", Decimal(10000)
    pago.save()
    excepcion = Excepcion.objects.create(
        dia=dia, tipo=Excepcion.Tipo.PAGO_SIN_MOVIMIENTO, monto_en_juego=10000, pago=pago,
        decision_propuesta="x",
    )
    cuenta = CuentaContable.objects.get(codigo="6501004")
    motivo = MotivoResolucion.objects.get(codigo="perdida_ganancia_caja")
    resolver_excepcion(
        excepcion, resolucion="perdida_ganancia", motivo_id=motivo.pk, comentario="",
        usuario=dia.operado_por, cuenta_contable_id=cuenta.pk, cajera_id=cajera.pk,
    )

    filas = generar_lineas_arqueo_caja(dia)
    resultado = next(f for f in filas if f["Apuntes contables/Cuenta"].startswith("6501004"))
    assert resultado["Apuntes contables/Empresa"] == "Josefina Lorca"
    assert resultado["Apuntes contables/Débito"] == 10000
    balance = next(f for f in filas if f["Apuntes contables/Cuenta"].startswith("1101005"))
    assert balance["Apuntes contables/Crédito"] == 10000


def test_una_perdida_de_tarjeta_sin_cajera_no_deberia_poder_resolverse_pero_si_ocurre_se_avisa(dia):
    pago = _venta_efectivo(dia, 1, 10000)
    pago.medio = "credito"
    pago.save()
    excepcion = Excepcion.objects.create(
        dia=dia, tipo=Excepcion.Tipo.PAGO_SIN_MOVIMIENTO, monto_en_juego=10000, pago=pago,
        cuenta_contable=CuentaContable.objects.get(codigo="6501004"), monto_perdida_ganancia=10000,
        resolucion=Excepcion.Resolucion.PERDIDA_GANANCIA, abierta=False, decision_propuesta="x",
    )
    with pytest.raises(ArqueoCajaInvalido, match="cajera responsable"):
        generar_lineas_arqueo_caja(dia)


# -------------------------------------------------------------- visado ($50.000)

@pytest.fixture
def dia_con_faltante_grande(dia, cajera, jefe):
    _venta_efectivo(dia, 1, 300000, caja="CAJA 3")
    arqueo = _contar(dia, "CAJA 3", 244046, cajera)  # faltan $55.954, sobre el umbral de $50.000
    arqueo.autorizado_por = dia.operado_por  # ya autorizada operativamente (umbral $5.000)
    arqueo.save(update_fields=["autorizado_por"])
    aprobar_y_cerrar_dia(dia, jefe)  # el Jefe sí puede cerrar con la diferencia sin visar
    return dia


def test_una_diferencia_de_caja_sobre_50000_bloquea_la_exportacion_sin_visar(dia_con_faltante_grande, tmp_path, monkeypatch):
    import nucleo.exportacion_servicio as svc
    monkeypatch.setattr(svc, "CARPETA_EXPORTACIONES", tmp_path)
    assert len(diferencias_de_caja_pendientes_de_visado(dia_con_faltante_grande)) == 1
    with pytest.raises(ExportacionInvalida, match="sin visar"):
        generar_exportacion(dia_con_faltante_grande, dia_con_faltante_grande.operado_por)


def test_al_visar_la_diferencia_ya_se_puede_exportar(dia_con_faltante_grande, jefe, tmp_path, monkeypatch):
    import nucleo.exportacion_servicio as svc
    monkeypatch.setattr(svc, "CARPETA_EXPORTACIONES", tmp_path)
    arqueo = ArqueoEfectivo.objects.get(dia=dia_con_faltante_grande, caja="CAJA 3")
    visar_diferencia_caja(arqueo, jefe)
    assert not diferencias_de_caja_pendientes_de_visado(dia_con_faltante_grande)

    exportacion = generar_exportacion(dia_con_faltante_grande, jefe)
    assert exportacion.archivo_arqueo


def test_una_encargada_no_puede_visar(dia_con_faltante_grande, django_user_model):
    encargada = crear_usuario(django_user_model, "encargada", GRUPO_ENCARGADO)
    arqueo = ArqueoEfectivo.objects.get(dia=dia_con_faltante_grande, caja="CAJA 3")
    with pytest.raises(PermisoDenegado):
        visar_diferencia_caja(arqueo, encargada)


def test_recontar_la_caja_borra_el_visado_anterior(dia_con_faltante_grande, jefe, usuario):
    arqueo = ArqueoEfectivo.objects.get(dia=dia_con_faltante_grande, caja="CAJA 3")
    visar_diferencia_caja(arqueo, jefe)
    registrar_conteo(arqueo, fondo_inicial=Decimal(0), gastos_autorizados=Decimal(0),
                      total_contado=Decimal(300000), usuario=usuario)  # ahora cuadra (lo recuenta quien opera)
    arqueo.refresh_from_db()
    assert arqueo.visado_por_id is None


# --------------------------------------------------------------- configuración

def test_solo_la_gerente_puede_cambiar_los_datos_del_asiento_de_arqueo(client, django_user_model):
    from django.contrib.auth.models import Group
    from nucleo.documentos_servicio import GRUPO_GERENTE_ADM_FINANZAS
    gerente = django_user_model.objects.create_user(username="gerente", password="x")
    gerente.groups.add(Group.objects.get_or_create(name=GRUPO_GERENTE_ADM_FINANZAS)[0])
    client.force_login(gerente)

    cuenta_banco = CuentaContable.objects.get(codigo="1101001")
    client.post(reverse("guardar_arqueo_caja"), {
        "diario": "Arqueo Caja Sala", "cuenta_efectivo_codigo": "1101004", "cuenta_efectivo_nombre": "Caja",
        "cuenta_transbank_codigo": "1101005", "cuenta_transbank_nombre": "Transbank",
        "cuenta_mercado_pago_codigo": "1104008", "cuenta_mercado_pago_nombre": "Redelcom (Mercado Pago)",
        "cuenta_banco_id": cuenta_banco.pk,
        "codigo_analitico": "99",
    })
    config = ConfiguracionArqueoCaja.vigente()
    assert (config.diario, config.codigo_analitico) == ("Arqueo Caja Sala", "99")


# ----------------------------------------------------- proveedor Mercado Pago

def test_sin_movimiento_vinculado_se_asume_transbank_por_defecto(dia, cajera):
    """Karla, 2026-09-21: todavía no hay un caso real de Mercado Pago —
    hasta que aparezca uno con su propio movimiento vinculado, una pérdida
    sin contraparte se asume Transbank, igual que en el archivo de ventas."""
    pago = _venta_efectivo(dia, 1, 10000)
    pago.medio, pago.monto = "credito", Decimal(10000)
    pago.save()
    excepcion = Excepcion.objects.create(
        dia=dia, tipo=Excepcion.Tipo.PAGO_SIN_MOVIMIENTO, monto_en_juego=10000, pago=pago,
        decision_propuesta="x",
    )
    cuenta = CuentaContable.objects.get(codigo="6501004")
    motivo = MotivoResolucion.objects.get(codigo="perdida_ganancia_caja")
    resolver_excepcion(
        excepcion, resolucion="perdida_ganancia", motivo_id=motivo.pk, comentario="",
        usuario=dia.operado_por, cuenta_contable_id=cuenta.pk, cajera_id=cajera.pk,
    )
    filas = generar_lineas_arqueo_caja(dia)
    balance = next(f for f in filas if f["Apuntes contables/Cuenta"].startswith(("1101005", "1104008")))
    assert balance["Apuntes contables/Cuenta"] == "1101005 Transbank Débito y Crédito"


def test_una_perdida_vinculada_a_un_movimiento_de_mercado_pago_usa_la_cuenta_redelcom(dia, cajera):
    from nucleo.models import VersionMotor
    VersionMotor.objects.create(dia=dia, version_motor="test", parametros={}, ejecutado_por=dia.operado_por)
    pago = _venta_efectivo(dia, 1, 8000)
    pago.medio, pago.monto = "credito", Decimal(8000)
    pago.save()
    lote_mp = LoteCarga.objects.create(
        dia=dia, fuente=LoteCarga.Fuente.MERCADO_PAGO, nombre_archivo="mp", huella_sha256="mp",
        ruta_almacenamiento="/mp", fecha_declarada=dia.fecha, cargado_por=dia.operado_por,
    )
    movimiento = MovimientoProveedor.objects.create(
        dia=dia, lote=lote_mp, proveedor="mercado_pago", tipo="Venta", estado="PENDIENTE",
        fecha_hora=datetime(2026, 9, 1, 10, 0), medio="credito", tipo_tarjeta="credito",
        monto_original=6000, monto_venta_valido_abono=6000, autorizacion="MP1", terminal="MP",
    )
    excepcion = Excepcion.objects.create(
        dia=dia, tipo=Excepcion.Tipo.DIFERENCIA_MONTO, monto_en_juego=2000, pago=pago, movimiento=movimiento,
        decision_propuesta="x",
    )
    cuenta = CuentaContable.objects.get(codigo="6501004")
    motivo = MotivoResolucion.objects.get(codigo="perdida_ganancia_caja")
    resolver_excepcion(
        excepcion, resolucion="perdida_ganancia", motivo_id=motivo.pk, comentario="",
        usuario=dia.operado_por, cuenta_contable_id=cuenta.pk, cajera_id=cajera.pk,
    )
    filas = generar_lineas_arqueo_caja(dia)
    balance = next(f for f in filas if f["Apuntes contables/Cuenta"].startswith(("1101005", "1104008")))
    assert balance["Apuntes contables/Cuenta"] == "1104008 Redelcom (Mercado Pago)"
    etiqueta = next(f for f in filas if f["Apuntes contables/Cuenta"].startswith("6501004"))
    assert etiqueta["Apuntes contables/Etiqueta"] == f"Diferencia Mercado Pago -Arqueo Caja Día {FECHA.strftime('%d/%m/%Y')}"


def test_config_arqueo_caja_incluye_la_cuenta_redelcom_por_defecto(db):
    config = ConfiguracionArqueoCaja.vigente()
    assert config.cuenta_mercado_pago == "1104008 Redelcom (Mercado Pago)"
    assert config.cuenta_por_proveedor("mercado_pago") == "1104008 Redelcom (Mercado Pago)"
    assert config.cuenta_por_proveedor("transbank") == "1101005 Transbank Débito y Crédito"
