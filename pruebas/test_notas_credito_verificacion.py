"""Respaldo de la devolución de una nota de crédito (Karla, 2026-09-24). Resto
registra la devolución como efectivo aunque el cobro original haya sido con
tarjeta, y no vincula la boleta original. Por defecto la nota NO resta del
efectivo de la caja (caso real del 07-09); solo resta cuando se verifica con
evidencia que salió en efectivo de la caja. La verificación la hace el Jefe de Contabilidad,
con segunda firma de la Gerente sobre $50.000; una nota sin verificar bloquea el
cierre del día."""
from datetime import date, time
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from nucleo.arqueo_servicio import GastoCajaInvalido, calcular_teorico_por_caja, preparar_arqueos_del_dia, registrar_gasto_caja
from nucleo.cierre_servicio import estado_checklist
from nucleo.documentos_servicio import GRUPO_GERENTE_ADM_FINANZAS, GRUPO_JEFE_CONTABILIDAD
from nucleo.models import (
    DiaArqueo, EventoAuditoria, LoteCarga, MotivoResolucion, PagoDeclarado, RegularizacionDocumento, Venta,
)
from nucleo.notas_credito_servicio import boletas_candidatas, registrar_evidencia, segunda_firma, verificar
from nucleo.permisos import PermisoDenegado
from nucleo.regularizacion_servicio import RegularizacionInvalida

FECHA = date(2026, 9, 18)


def _pdf():
    return SimpleUploadedFile("vale.pdf", b"%PDF-1.4 vale firmado", content_type="application/pdf")


def _usuario(django_user_model, nombre, grupo=None):
    u = django_user_model.objects.create_user(username=nombre, password="x")
    if grupo:
        u.groups.add(Group.objects.get_or_create(name=grupo)[0])
    return u


@pytest.fixture
def operador(django_user_model):
    return _usuario(django_user_model, "operador", "Encargada de Ingresos")


@pytest.fixture
def jefe(django_user_model):
    return _usuario(django_user_model, "jefe", GRUPO_JEFE_CONTABILIDAD)


@pytest.fixture
def gerente(django_user_model):
    return _usuario(django_user_model, "gerente", GRUPO_GERENTE_ADM_FINANZAS)


@pytest.fixture
def dia(db, operador):
    dia = DiaArqueo.objects.create(fecha=FECHA, operado_por=operador)
    lote = LoteCarga.objects.create(
        dia=dia, fuente=LoteCarga.Fuente.POS, nombre_archivo="x", huella_sha256="a",
        ruta_almacenamiento="/x", fecha_declarada=FECHA, cargado_por=operador,
    )
    venta = Venta.objects.create(
        dia=dia, lote=lote, n_interno=1, nro_comprobante="B0390301668000", tipo_doc="FAC", nro_comanda="1",
        estado="COBRADA", caja="CAJA 2", turno="T", hora_cierre=time(11, 0), total_comanda=18400, cliente="X",
    )
    PagoDeclarado.objects.create(venta=venta, medio="debito", monto=18400)
    otra = Venta.objects.create(
        dia=dia, lote=lote, n_interno=2, nro_comprobante="B0390301668001", tipo_doc="FAC", nro_comanda="2",
        estado="COBRADA", caja="CAJA 2", turno="T", hora_cierre=time(12, 0), total_comanda=20000, cliente="X",
    )
    PagoDeclarado.objects.create(venta=otra, medio="efectivo", monto=20000)
    return dia


def _nota(dia, folio="F0610600008317", monto=18400, forma_pago="efectivo", caja="CAJA 2"):
    return RegularizacionDocumento.objects.create(
        dia=dia, tipo="CRED", codigo_original="CRE", n_interno=int(folio[-4:]), folio=folio, caja=caja,
        monto=monto, forma_pago=forma_pago,
    )


def test_una_nota_en_efectivo_sin_verificar_no_resta_y_bloquea_el_cierre(dia):
    _nota(dia)
    assert calcular_teorico_por_caja(dia)["CAJA 2"] == Decimal(20000)
    item = estado_checklist(dia)["notas_credito_verificadas"]
    assert item["ok"] is False and "8317" in item["detalle"]


def test_una_nota_anotada_como_tarjeta_tambien_bloquea_el_cierre_pero_no_resta(dia):
    """Resto anota la devolución como tarjeta Transbank aunque se pagó en efectivo."""
    _nota(dia, folio="F0610600008311", monto=9600, forma_pago="credito", caja="CAJA 3")
    assert calcular_teorico_por_caja(dia).get("CAJA 3", Decimal(0)) == Decimal(0)
    assert estado_checklist(dia)["notas_credito_verificadas"]["ok"] is False


def test_la_evidencia_es_obligatoria_y_valida(dia, operador):
    nota = _nota(dia)
    with pytest.raises(RegularizacionInvalida, match="evidencia"):
        registrar_evidencia(nota, medio="efectivo", boleta_original="", comentario="", comprobante=None, usuario=operador)
    with pytest.raises(RegularizacionInvalida, match="medio real"):
        registrar_evidencia(nota, medio="", boleta_original="", comentario="", comprobante=_pdf(), usuario=operador)
    falso = SimpleUploadedFile("vale.pdf", b"MZ no es pdf")
    with pytest.raises(RegularizacionInvalida, match="no corresponde"):
        registrar_evidencia(nota, medio="efectivo", boleta_original="", comentario="", comprobante=falso, usuario=operador)


def test_sin_devolucion_exige_la_boleta_de_reemplazo(dia, operador):
    nota = _nota(dia)
    with pytest.raises(RegularizacionInvalida, match="boleta de reemplazo"):
        registrar_evidencia(nota, medio="sin_devolucion", boleta_original="", comentario="", comprobante=_pdf(), usuario=operador)


def test_verificada_como_efectivo_no_cambia_el_efectivo_de_la_caja(dia, operador, jefe):
    nota = _nota(dia)
    registrar_evidencia(nota, medio="efectivo", boleta_original="8000", comentario="Vale firmado", comprobante=_pdf(), usuario=operador)
    assert calcular_teorico_por_caja(dia)["CAJA 2"] == Decimal(20000)  # con evidencia cargada aún no resta
    verificar(nota, aprobar=True, comentario="ok", usuario=jefe)
    nota.refresh_from_db()
    assert nota.verificacion_estado == "verificada" and nota.estado == "regularizado" and nota.documento_relacionado == "8000"
    assert calcular_teorico_por_caja(dia)["CAJA 2"] == Decimal(20000)  # Resto ya la descontó: no se resta dos veces
    assert estado_checklist(dia)["notas_credito_verificadas"]["ok"] is True


def test_verificada_como_tarjeta_libera_el_cierre_sin_tocar_el_efectivo(dia, operador, jefe):
    nota = _nota(dia)
    registrar_evidencia(nota, medio="tarjeta", boleta_original="8000", comentario="Reverso Transbank", comprobante=_pdf(), usuario=operador)
    verificar(nota, aprobar=True, comentario="", usuario=jefe)
    assert calcular_teorico_por_caja(dia)["CAJA 2"] == Decimal(20000)
    assert estado_checklist(dia)["notas_credito_verificadas"]["ok"] is True


def test_el_jefe_no_carga_evidencia_y_la_gerente_puede_verificar_la_que_cargo(dia, jefe, gerente):
    nota = _nota(dia)
    with pytest.raises(PermisoDenegado):  # el Jefe revisa, no prepara: así nunca valida su propio trabajo
        registrar_evidencia(nota, medio="efectivo", boleta_original="", comentario="", comprobante=_pdf(), usuario=jefe)
    registrar_evidencia(nota, medio="efectivo", boleta_original="", comentario="", comprobante=_pdf(), usuario=gerente)
    verificar(nota, aprobar=True, comentario="", usuario=gerente)  # la Gerente sí puede verificar lo que ella cargó


def test_la_operadora_no_puede_verificar(dia, operador):
    nota = _nota(dia)
    registrar_evidencia(nota, medio="efectivo", boleta_original="", comentario="", comprobante=_pdf(), usuario=operador)
    with pytest.raises(PermisoDenegado):
        verificar(nota, aprobar=True, comentario="", usuario=operador)


def test_rechazar_pide_motivo_y_permite_cargar_otra_evidencia(dia, operador, jefe):
    nota = _nota(dia)
    registrar_evidencia(nota, medio="efectivo", boleta_original="", comentario="", comprobante=_pdf(), usuario=operador)
    with pytest.raises(RegularizacionInvalida, match="por qué"):
        verificar(nota, aprobar=False, comentario="", usuario=jefe)
    verificar(nota, aprobar=False, comentario="Falta la firma del cliente", usuario=jefe)
    nota.refresh_from_db()
    assert nota.verificacion_estado == "rechazada"
    registrar_evidencia(nota, medio="efectivo", boleta_original="", comentario="", comprobante=_pdf(), usuario=operador)
    assert nota.verificacion_estado == "evidencia_cargada"


def test_sobre_50000_exige_la_segunda_firma_de_la_gerente(dia, operador, jefe, gerente):
    nota = _nota(dia, folio="F0610600008400", monto=60000)
    registrar_evidencia(nota, medio="efectivo", boleta_original="", comentario="", comprobante=_pdf(), usuario=operador)
    verificar(nota, aprobar=True, comentario="", usuario=jefe)
    nota.refresh_from_db()
    assert nota.verificacion_estado == "pendiente_segunda_firma" and nota.estado == "pendiente"
    assert estado_checklist(dia)["notas_credito_verificadas"]["ok"] is False
    with pytest.raises(PermisoDenegado):
        segunda_firma(nota, aprobar=True, comentario="", usuario=jefe)
    segunda_firma(nota, aprobar=True, comentario="", usuario=gerente)
    nota.refresh_from_db()
    assert nota.verificacion_estado == "verificada" and nota.segunda_firma_por == gerente


def test_la_firma_de_la_gerente_cuenta_como_primera_y_segunda(dia, operador, gerente):
    nota = _nota(dia, folio="F0610600008400", monto=60000)
    registrar_evidencia(nota, medio="efectivo", boleta_original="", comentario="", comprobante=_pdf(), usuario=operador)
    verificar(nota, aprobar=True, comentario="", usuario=gerente)
    nota.refresh_from_db()
    assert nota.verificacion_estado == "verificada" and nota.segunda_firma_por == gerente


def test_cada_paso_queda_en_la_bitacora(dia, operador, jefe):
    nota = _nota(dia)
    registrar_evidencia(nota, medio="efectivo", boleta_original="", comentario="", comprobante=_pdf(), usuario=operador)
    verificar(nota, aprobar=True, comentario="", usuario=jefe)
    acciones = list(EventoAuditoria.objects.filter(entidad="RegularizacionDocumento").values_list("accion", flat=True))
    assert "registrar_evidencia_nota_credito" in acciones and "verificar_nota_credito" in acciones


def test_las_boletas_candidatas_salen_por_monto_y_caja(dia):
    nota = _nota(dia)
    assert [b["boleta"] for b in boletas_candidatas(nota)] == ["1668000"]


def test_flujo_por_la_pantalla(client, dia, operador, jefe):
    nota = _nota(dia)
    client.force_login(operador)
    html = client.get(reverse("regularizacion_documentos") + f"?estado=&documento={nota.pk}").content.decode()
    assert "No se descuenta del efectivo" in html and "bloquea el cierre" in html
    client.post(reverse("registrar_evidencia_nc", args=[nota.pk]), {
        "medio": "efectivo", "boleta_original": "1668000", "comentario": "vale", "comprobante": _pdf(),
    })
    nota.refresh_from_db()
    assert nota.verificacion_estado == "evidencia_cargada" and nota.boleta_original == "1668000"
    client.force_login(jefe)
    client.post(reverse("verificar_nc", args=[nota.pk]), {"accion": "aprobar", "comentario": ""})
    nota.refresh_from_db()
    assert nota.verificacion_estado == "verificada"
    html = client.get(f"/arqueo/{FECHA.isoformat()}/").content.decode()
    assert "informativa" in html and "Resto ya la descontó" in html
