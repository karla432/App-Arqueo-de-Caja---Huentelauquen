"""Cierre, aprobación y exportación a Odoo (Etapa 6). Escenario armado a
mano: una comanda limpia, arqueo de su única caja ya cuadrado, para poder
probar el camino feliz y cada bloqueo por separado sin depender del día
completo.
"""
from datetime import date, datetime, time
from decimal import Decimal

import pytest
from pruebas.perfiles_de_prueba import GRUPO_GERENTE, crear_usuario
from django.urls import reverse
from openpyxl import load_workbook

from nucleo.arqueo_servicio import registrar_conteo
from nucleo.cierre_servicio import (
    CierreInvalido, aprobar_y_cerrar_dia, estado_checklist, puede_aprobar, reabrir_dia,
)
from nucleo.exportacion_servicio import ExportacionInvalida, generar_exportacion
from nucleo.models import (
    ArqueoEfectivo, Cajera, Coincidencia, CuentaContable, DiaArqueo, Excepcion, LineaVenta, LoteCarga,
    MotivoResolucion, MovimientoProveedor, PagoDeclarado, Venta, VersionMotor,
)
from nucleo.resolucion_servicio import resolver_excepcion

FECHA = date(2026, 9, 2)


@pytest.fixture
def usuario(django_user_model):
    return crear_usuario(django_user_model, "operador", GRUPO_GERENTE)


@pytest.fixture
def dia_listo(db, usuario):
    """Un día con una sola comanda, sin excepciones abiertas y con su
    única caja ya cuadrada — cumple todo lo bloqueante de la lista de
    verificación."""
    dia = DiaArqueo.objects.create(fecha=FECHA, operado_por=usuario)
    lote = LoteCarga.objects.create(
        dia=dia, fuente=LoteCarga.Fuente.POS, nombre_archivo="x.xlsx", huella_sha256="a",
        ruta_almacenamiento="/tmp/x", fecha_declarada=FECHA, cargado_por=usuario,
    )
    venta = Venta.objects.create(
        dia=dia, lote=lote, n_interno=654747, nro_comprobante="B0390301666467", tipo_doc="FAC",
        nro_comanda="656609", estado="COBRADA", caja="CAJA 4", turno="TURNO CAJA 4",
        hora_cierre=time(10, 25), total_comanda=17600, cliente="CLIENTE OCASIONAL", sucursal="Huentelauquen Central",
    )
    LineaVenta.objects.create(venta=venta, articulo="EMPANADA QUESO FRITA", cantidad=4,
                               precio_unitario=2500, total_linea=10000)
    LineaVenta.objects.create(venta=venta, articulo="TE", cantidad=4, precio_unitario=1900, total_linea=7600)
    PagoDeclarado.objects.create(venta=venta, medio="debito", monto=17600)
    # El día ya se concilió (el cierre lo exige cuando hay tarjeta).
    VersionMotor.objects.create(dia=dia, version_motor="test", parametros={}, ejecutado_por=usuario)
    return dia


@pytest.fixture
def caja_cuadrada(dia_listo, usuario):
    # No hay efectivo en la única venta -> el arqueo de esa caja parte en
    # $0 esperado; lo registramos igual para que "arqueo completo" pase.
    arqueo, _ = ArqueoEfectivo.objects.get_or_create(dia=dia_listo, caja="CAJA 4")
    registrar_conteo(arqueo, fondo_inicial=Decimal(0), gastos_autorizados=Decimal(0),
                      total_contado=Decimal(0), usuario=usuario)
    return arqueo


def test_checklist_bloquea_por_excepcion_abierta(dia_listo, caja_cuadrada):
    Excepcion.objects.create(dia=dia_listo, tipo="pago_sin_movimiento", monto_en_juego=1000,
                              decision_propuesta="x")
    assert puede_aprobar(dia_listo) is False


def test_checklist_no_bloquea_por_calidad_de_registro(dia_listo, caja_cuadrada):
    Excepcion.objects.create(dia=dia_listo, tipo="calidad_registro", monto_en_juego=0,
                              decision_propuesta="x")
    assert puede_aprobar(dia_listo) is True


def test_checklist_bloquea_si_falta_contar_una_caja(dia_listo):
    assert puede_aprobar(dia_listo) is False  # CAJA 4 nunca se contó


def test_deposito_no_bloquea(dia_listo, caja_cuadrada):
    assert estado_checklist(dia_listo)["deposito_registrado"]["ok"] is False
    assert puede_aprobar(dia_listo) is True  # el depósito no es bloqueante


def test_aprobar_y_cerrar_camino_feliz(dia_listo, caja_cuadrada, usuario):
    aprobar_y_cerrar_dia(dia_listo, usuario, motivo_excepcion="prueba")
    dia_listo.refresh_from_db()
    assert dia_listo.estado == DiaArqueo.Estado.CERRADO
    assert dia_listo.aprobado_por == usuario


def test_no_se_aprueba_con_pendientes(dia_listo):
    with pytest.raises(CierreInvalido):
        aprobar_y_cerrar_dia(dia_listo, dia_listo.operado_por)


def test_no_se_aprueba_dos_veces(dia_listo, caja_cuadrada, usuario):
    aprobar_y_cerrar_dia(dia_listo, usuario, motivo_excepcion="prueba")
    with pytest.raises(CierreInvalido):
        aprobar_y_cerrar_dia(dia_listo, usuario, motivo_excepcion="prueba")


def test_exportar_solo_si_aprobado(dia_listo, usuario):
    with pytest.raises(ExportacionInvalida):
        generar_exportacion(dia_listo, usuario)


def test_exportar_genera_archivo_con_el_formato_real(dia_listo, caja_cuadrada, usuario, tmp_path, monkeypatch):
    import nucleo.exportacion_servicio as svc
    monkeypatch.setattr(svc, "CARPETA_EXPORTACIONES", tmp_path)

    aprobar_y_cerrar_dia(dia_listo, usuario, motivo_excepcion="prueba")
    exportacion = generar_exportacion(dia_listo, usuario)

    assert exportacion.version == 1
    wb = load_workbook(exportacion.archivo)
    ws = wb.active
    filas = list(ws.iter_rows(values_only=True))
    assert filas[0] == tuple(svc.COLUMNAS)
    # 2 líneas de la única comanda -> 2 filas de datos.
    assert len(filas) == 3
    cabecera = filas[1]
    assert cabecera[0] == "CLIENTE OCASIONAL"
    assert cabecera[4] == "Tarjeta Transbank"  # débito -> Tarjeta Transbank
    assert cabecera[5] == 654747  # N_INTERNO
    assert cabecera[6] == "Boleta Electrónica"
    assert cabecera[7] == "1666467"  # 7 últimos del comprobante
    segunda_linea = filas[2]
    assert segunda_linea[0] is None  # fila de línea adicional, sin cabecera repetida


def test_exportar_distingue_tarjeta_mercado_pago_de_transbank(dia_listo, caja_cuadrada, usuario, tmp_path, monkeypatch):
    """Karla, 2026-09-16: la columna debe decir "Tarjeta Transbank" o
    "Tarjeta Mercado Pago" según qué adquirente procesó la tarjeta, no
    un rótulo genérico — se determina por el movimiento con el que el
    pago quedó conciliado."""
    import nucleo.exportacion_servicio as svc
    monkeypatch.setattr(svc, "CARPETA_EXPORTACIONES", tmp_path)

    pago = dia_listo.ventas.get().pagos.get()
    lote_mp = LoteCarga.objects.create(
        dia=dia_listo, fuente=LoteCarga.Fuente.MERCADO_PAGO, nombre_archivo="mp.xlsx", huella_sha256="b",
        ruta_almacenamiento="/tmp/mp", fecha_declarada=dia_listo.fecha, cargado_por=usuario,
    )
    movimiento = MovimientoProveedor.objects.create(
        dia=dia_listo, lote=lote_mp, proveedor="mercado_pago", tipo="Venta",
        estado="Pago aprobado", fecha_hora=datetime.combine(dia_listo.fecha, time(10, 25)),
        medio="debito", tipo_tarjeta="debito",
        monto_original=17600, monto_venta_valido_abono=17600, autorizacion="177303137843", terminal="Point",
    )
    corrida = VersionMotor.objects.create(dia=dia_listo, version_motor="test", parametros={}, ejecutado_por=usuario)
    coincidencia = Coincidencia.objects.create(
        dia=dia_listo, corrida=corrida, nivel=1, score=100, origen=Coincidencia.Origen.AUTOMATICO,
    )
    coincidencia.pagos.set([pago])
    coincidencia.movimientos.set([movimiento])

    aprobar_y_cerrar_dia(dia_listo, usuario, motivo_excepcion="prueba")
    exportacion = generar_exportacion(dia_listo, usuario)

    wb = load_workbook(exportacion.archivo)
    filas = list(wb.active.iter_rows(values_only=True))
    assert filas[1][4] == "Tarjeta Mercado Pago"


def test_exportacion_no_incluye_la_perdida_de_caja_pero_la_venta_sigue(dia_listo, caja_cuadrada, usuario, tmp_path, monkeypatch):
    """Karla, 2026-09-21: la pérdida ya no va en el archivo de ventas —
    "Pérdida de caja" no es un tipo de pago válido en Odoo. La venta que
    la originó se sigue exportando tal cual (el producto salió, el IVA se
    debe); la pérdida queda solo en el asiento de arqueo de caja (Fase 2,
    ver test_arqueo_caja)."""
    import nucleo.exportacion_servicio as svc
    monkeypatch.setattr(svc, "CARPETA_EXPORTACIONES", tmp_path)

    cajera = Cajera.objects.create(nombre="Juanita Pérez")
    cuenta = CuentaContable.objects.get(codigo="6501004")
    motivo = MotivoResolucion.objects.get(codigo="perdida_ganancia_caja")

    lote = dia_listo.lotes.get()
    venta2 = Venta.objects.create(
        dia=dia_listo, lote=lote, n_interno=999, nro_comprobante="B0000999", tipo_doc="FAC",
        nro_comanda="999", estado="COBRADA", caja="CAJA 4", turno="TURNO CAJA 4",
        hora_cierre=time(11, 0), total_comanda=5000, cliente="CLIENTE OCASIONAL",
    )
    LineaVenta.objects.create(venta=venta2, articulo="TE", cantidad=1, precio_unitario=5000, total_linea=5000)
    pago2 = PagoDeclarado.objects.create(venta=venta2, medio="credito", monto=5000)
    excepcion = Excepcion.objects.create(
        dia=dia_listo, tipo=Excepcion.Tipo.PAGO_SIN_MOVIMIENTO, monto_en_juego=5000, pago=pago2,
        decision_propuesta="x",
    )
    resolver_excepcion(
        excepcion, resolucion="perdida_ganancia", motivo_id=motivo.pk, comentario="",
        usuario=usuario, cuenta_contable_id=cuenta.pk, cajera_id=cajera.pk,
    )

    aprobar_y_cerrar_dia(dia_listo, usuario, motivo_excepcion="prueba")
    exportacion = generar_exportacion(dia_listo, usuario)

    wb = load_workbook(exportacion.archivo)
    filas = list(wb.active.iter_rows(values_only=True))
    assert not any(f[4] == "Pérdida de caja" or (f[4] and "6501004" in str(f[4])) for f in filas)

    # La venta que originó la pérdida se sigue exportando como venta real.
    fila_venta2 = next(f for f in filas if f[5] == 999)
    assert fila_venta2[8] == "TE"

    # La pérdida queda en el asiento de arqueo de caja, con la cajera.
    from openpyxl import load_workbook as cargar
    filas_arqueo = list(cargar(exportacion.archivo_arqueo).active.iter_rows(values_only=True))
    fila_perdida = next(f for f in filas_arqueo if f[3] and f[3].startswith("6501004"))
    assert fila_perdida[5] == "Juanita Pérez"
    assert fila_perdida[7] == 5000  # débito = el monto de la pérdida


def test_exportacion_bloquea_con_perdida_sobre_umbral_sin_visar(
    dia_listo, caja_cuadrada, usuario, tmp_path, monkeypatch, django_user_model,
):
    import nucleo.exportacion_servicio as svc
    monkeypatch.setattr(svc, "CARPETA_EXPORTACIONES", tmp_path)

    cajera = Cajera.objects.create(nombre="Juanita Pérez")
    cuenta = CuentaContable.objects.get(codigo="6501004")
    motivo = MotivoResolucion.objects.get(codigo="perdida_ganancia_caja")

    lote = dia_listo.lotes.get()
    venta2 = Venta.objects.create(
        dia=dia_listo, lote=lote, n_interno=999, nro_comprobante="B0000999", tipo_doc="FAC",
        nro_comanda="999", estado="COBRADA", caja="CAJA 4", turno="TURNO CAJA 4",
        hora_cierre=time(11, 0), total_comanda=60000, cliente="CLIENTE OCASIONAL",
    )
    LineaVenta.objects.create(venta=venta2, articulo="TE", cantidad=1, precio_unitario=60000, total_linea=60000)
    pago2 = PagoDeclarado.objects.create(venta=venta2, medio="credito", monto=60000)
    excepcion = Excepcion.objects.create(
        dia=dia_listo, tipo=Excepcion.Tipo.PAGO_SIN_MOVIMIENTO, monto_en_juego=60000, pago=pago2,
        decision_propuesta="x",
    )
    resolver_excepcion(
        excepcion, resolucion="perdida_ganancia", motivo_id=motivo.pk, comentario="",
        usuario=usuario, cuenta_contable_id=cuenta.pk, cajera_id=cajera.pk,
    )

    from django.contrib.auth.models import Group
    from nucleo.documentos_servicio import GRUPO_JEFE_CONTABILIDAD
    jefe = django_user_model.objects.create_user(username="jefe", password="x")
    grupo, _ = Group.objects.get_or_create(name=GRUPO_JEFE_CONTABILIDAD)
    jefe.groups.add(grupo)

    aprobar_y_cerrar_dia(dia_listo, jefe)  # el Jefe sí puede cerrar con la pérdida sin visar
    with pytest.raises(ExportacionInvalida):
        generar_exportacion(dia_listo, usuario)


def test_exportacion_de_mezcla_de_medios_deja_el_traspaso_en_el_arqueo_de_caja(dia_listo, caja_cuadrada, usuario, tmp_path, monkeypatch):
    """Karla, 2026-09-21: la venta se exporta tal cual (sin tocar) en el
    archivo de ventas; el traspaso por la parte que en realidad llegó por
    otro medio ya no va ahí ("Traspaso a Efectivo" no es un tipo de pago
    de Odoo) — queda en el asiento de arqueo de caja, entre las dos
    cuentas de balance, sin pérdida ni ganancia."""
    import nucleo.exportacion_servicio as svc
    monkeypatch.setattr(svc, "CARPETA_EXPORTACIONES", tmp_path)

    lote = dia_listo.lotes.get()
    venta2 = Venta.objects.create(
        dia=dia_listo, lote=lote, n_interno=999, nro_comprobante="B0000999", tipo_doc="FAC",
        nro_comanda="999", estado="COBRADA", caja="CAJA 4", turno="TURNO CAJA 4",
        hora_cierre=time(11, 0), total_comanda=50000, cliente="CLIENTE OCASIONAL",
    )
    LineaVenta.objects.create(venta=venta2, articulo="TE", cantidad=1, precio_unitario=50000, total_linea=50000)
    pago2 = PagoDeclarado.objects.create(venta=venta2, medio="debito", monto=50000)
    excepcion = Excepcion.objects.create(
        dia=dia_listo, tipo=Excepcion.Tipo.PAGO_SIN_MOVIMIENTO, monto_en_juego=50000, pago=pago2,
        decision_propuesta="x",
    )
    motivo = MotivoResolucion.objects.get(codigo="mezcla_medios_pago")
    resolver_excepcion(
        excepcion, resolucion="mezcla_medios", motivo_id=motivo.pk, comentario="", usuario=usuario,
        lineas=[{"medio": "debito", "monto": "45000"}, {"medio": "efectivo", "monto": "5000"}],
    )
    # Los $5.000 reclasificados a efectivo suben el esperado de CAJA 4 — se
    # recuenta para que quede cuadrada; no es lo que prueba este test.
    registrar_conteo(caja_cuadrada, fondo_inicial=Decimal(0), gastos_autorizados=Decimal(0),
                      total_contado=Decimal(5000), usuario=usuario)

    aprobar_y_cerrar_dia(dia_listo, usuario, motivo_excepcion="prueba")
    exportacion = generar_exportacion(dia_listo, usuario)

    wb = load_workbook(exportacion.archivo)
    filas = list(wb.active.iter_rows(values_only=True))
    filas_venta2 = [f for f in filas if f[5] == 999]
    assert len(filas_venta2) == 1
    assert filas_venta2[0][4] == "Tarjeta Transbank"  # la venta, sin tocar
    assert not any(f[4] and "Traspaso" in str(f[4]) for f in filas)  # ya no va en este archivo

    from openpyxl import load_workbook as cargar
    filas_arqueo = list(cargar(exportacion.archivo_arqueo).active.iter_rows(values_only=True))
    traspaso = [f for f in filas_arqueo if f[5] == "Comanda 999"]
    assert len(traspaso) == 2  # las dos líneas del traspaso: entre efectivo y Transbank
    assert {f[3] for f in traspaso} == {"1101004 Efectivo Pos Resto", "1101005 Transbank Débito y Crédito"}
    assert sorted(f[7] or f[8] for f in traspaso) == [5000, 5000]  # cuadra: débito = crédito
    assert all(f[6] is None for f in traspaso)  # sin distribución analítica: no es resultado


def test_reabrir_invalida_la_exportacion(dia_listo, caja_cuadrada, usuario, tmp_path, monkeypatch):
    import nucleo.exportacion_servicio as svc
    monkeypatch.setattr(svc, "CARPETA_EXPORTACIONES", tmp_path)

    aprobar_y_cerrar_dia(dia_listo, usuario, motivo_excepcion="prueba")
    exportacion = generar_exportacion(dia_listo, usuario)

    reabrir_dia(dia_listo, usuario, motivo="Corrección de una línea")
    exportacion.refresh_from_db()
    dia_listo.refresh_from_db()
    assert exportacion.invalidada is True
    assert dia_listo.estado == DiaArqueo.Estado.EXCEPCIONES_RESUELTAS


def test_no_se_reabre_sin_motivo(dia_listo, caja_cuadrada, usuario):
    aprobar_y_cerrar_dia(dia_listo, usuario, motivo_excepcion="prueba")
    with pytest.raises(CierreInvalido):
        reabrir_dia(dia_listo, usuario, motivo="  ")


def test_vista_aprobar_requiere_rol_de_aprobador(client, django_user_model, dia_listo, caja_cuadrada):
    operador_sin_rol = django_user_model.objects.create_user(username="sin_rol", password="x")
    client.force_login(operador_sin_rol)
    resp = client.post(reverse("aprobar_dia", args=[FECHA.isoformat()]), {"motivo_excepcion": "prueba"})
    assert resp.status_code == 403
    dia_listo.refresh_from_db()
    assert dia_listo.estado != DiaArqueo.Estado.CERRADO



# --------------------------------------- archivos automáticos al aprobar (Karla, 2026-09-25)

@pytest.fixture
def carpeta_tmp(tmp_path, monkeypatch):
    import nucleo.exportacion_servicio as svc
    monkeypatch.setattr(svc, "CARPETA_EXPORTACIONES", tmp_path)
    return tmp_path


def test_al_aprobar_desde_la_pantalla_los_archivos_se_generan_solos(client, dia_listo, caja_cuadrada, usuario, carpeta_tmp):
    client.force_login(usuario)
    resp = client.post(reverse("aprobar_dia", args=[FECHA.isoformat()]), {"motivo_excepcion": "prueba"}, follow=True)
    assert "Archivos para Odoo generados (v1)" in resp.content.decode()
    dia_listo.refresh_from_db()
    assert dia_listo.estado == DiaArqueo.Estado.EXPORTADO
    exp = dia_listo.exportaciones.get()
    assert exp.version == 1 and exp.invalidada is False
    assert list(carpeta_tmp.glob("captura_odoo_*_v1.xlsx")) and list(carpeta_tmp.glob("captura_arqueo_caja_*_v1.xlsx"))
    html = resp.content.decode()
    assert "Versión 1 generada" in html and "Generar versión" not in html and "Descargar en Exportaciones" not in html


def test_si_algo_bloquea_los_archivos_el_cierre_igual_queda_aprobado(client, dia_listo, caja_cuadrada, usuario, carpeta_tmp, monkeypatch):
    import nucleo.exportacion_servicio as svc
    monkeypatch.setattr(svc, "validar_venta", lambda venta: ["comanda 654747: falta el N° de factura"])
    client.force_login(usuario)
    resp = client.post(reverse("aprobar_dia", args=[FECHA.isoformat()]), {"motivo_excepcion": "prueba"}, follow=True)
    html = resp.content.decode()
    dia_listo.refresh_from_db()
    assert dia_listo.estado == DiaArqueo.Estado.CERRADO  # el cierre NO se revierte
    assert dia_listo.exportaciones.count() == 0
    assert "no se pudieron generar todavía" in html and "falta el N° de factura" in html
    assert "Cerrado · archivos para Odoo bloqueados" in html and "Los archivos todavía no se pueden generar" in html
    # apenas se resuelve, se generan (aquí: la desde Exportaciones, red de seguridad) y sin duplicar versiones
    monkeypatch.undo()
    monkeypatch.setattr(svc, "CARPETA_EXPORTACIONES", carpeta_tmp)
    for _ in range(2):  # doble clic
        client.post(reverse("generar_exportacion", args=[FECHA.isoformat()]), {"volver": "exportaciones"})
    assert dia_listo.exportaciones.count() == 1 and dia_listo.exportaciones.get().version == 1


def test_intentar_generar_nunca_lanza_y_no_duplica(dia_listo, caja_cuadrada, usuario, carpeta_tmp, monkeypatch):
    import nucleo.exportacion_servicio as svc
    assert svc.intentar_generar_exportacion(dia_listo, usuario) == (None, "")  # día sin aprobar: nada que hacer
    aprobar_y_cerrar_dia(dia_listo, usuario, motivo_excepcion="prueba")
    monkeypatch.setattr(svc, "validar_venta", lambda venta: ["problema"])
    exp, motivo = svc.intentar_generar_exportacion(dia_listo, usuario)
    assert exp is None and "problema" in motivo and svc.motivo_de_bloqueo(dia_listo) == motivo
    monkeypatch.undo()
    monkeypatch.setattr(svc, "CARPETA_EXPORTACIONES", carpeta_tmp)
    exp, motivo = svc.intentar_generar_exportacion(dia_listo, usuario)
    assert exp is not None and motivo == ""
    otra, _ = svc.intentar_generar_exportacion(dia_listo, usuario)
    assert otra.pk == exp.pk and dia_listo.exportaciones.count() == 1


def test_la_pantalla_dice_que_falta_para_cerrar_con_su_enlace(client, dia_listo, usuario):
    """Sin conteo de caja: «Qué falta para cerrar» con el enlace a la pantalla del arqueo."""
    client.force_login(usuario)
    html = client.get(reverse("cierre_y_exportacion", args=[FECHA.isoformat()])).content.decode()
    assert "Qué falta para cerrar" in html and "Arqueo de todas las cajas" in html
    assert reverse("arqueo_efectivo", args=[FECHA.isoformat()]) in html
    assert "Por aprobar" in html and "disabled" in html  # el botón de aprobar está deshabilitado con su motivo
    assert "Generar versión" not in html


def test_un_dia_reabierto_muestra_el_motivo_y_avisa_que_los_archivos_se_invalidaron(client, dia_listo, caja_cuadrada, usuario, carpeta_tmp):
    client.force_login(usuario)
    client.post(reverse("aprobar_dia", args=[FECHA.isoformat()]), {"motivo_excepcion": "prueba"})
    client.post(reverse("reabrir_dia", args=[FECHA.isoformat()]), {"motivo": "Corregir una línea"})
    html = client.get(reverse("cierre_y_exportacion", args=[FECHA.isoformat()])).content.decode()
    assert "Día reabierto" in html and "Corregir una línea" in html and "Reabierto · por aprobar" in html
    client.post(reverse("aprobar_dia", args=[FECHA.isoformat()]), {"motivo_excepcion": "prueba"})  # al volver a aprobar se genera la versión 2
    assert sorted(dia_listo.exportaciones.values_list("version", "invalidada")) == [(1, True), (2, False)]


def test_el_menu_cuenta_los_dias_cerrados_sin_archivos(client, dia_listo, caja_cuadrada, usuario, carpeta_tmp, monkeypatch):
    import nucleo.exportacion_servicio as svc
    aprobar_y_cerrar_dia(dia_listo, usuario, motivo_excepcion="prueba")  # cerrado, sin archivos todavía
    client.force_login(usuario)
    html = client.get(reverse("cierre_y_exportacion", args=[FECHA.isoformat()])).content.decode()
    assert "Días cerrados sin archivos para Odoo" in html
    svc.intentar_generar_exportacion(dia_listo, usuario)
    assert "Días cerrados sin archivos para Odoo" not in client.get(reverse("cierre_y_exportacion", args=[FECHA.isoformat()])).content.decode()
