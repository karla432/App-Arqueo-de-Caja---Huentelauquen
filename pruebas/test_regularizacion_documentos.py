"""Regularización de Documentos (Karla, 2026-09-22): guías de despacho
(DES) y notas de crédito (CRED) del libro contable no entran a la carga
masiva ni al arqueo automático — quedan Pendiente en su propio menú hasta
que la Encargada de Ingresos las regularice a mano, con trazabilidad
completa (documento original → relacionado → forma de pago → arqueo).

Karla, 2026-09-22 (segunda vuelta de feedback, ya probando en vivo): la
forma de pago viene precompletada con la que ya trajo Resto — no hay que
volver a escribirla — y queda obligatorio el comprobante del pago de la
factura que dejó contabilidad."""
from datetime import date

import pytest
from pruebas.perfiles_de_prueba import GRUPO_GERENTE, crear_usuario
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from nucleo.models import DiaArqueo, EventoAuditoria, PagoDeclarado, RegularizacionDocumento
from nucleo.regularizacion_servicio import (
    RegularizacionInvalida, crear_desde_libro, documentos_pendientes, documentos_regularizados, regularizar,
)

FECHA = date(2026, 9, 1)


def _comprobante():
    return SimpleUploadedFile("comprobante.pdf", b"%PDF-1.4 contenido", content_type="application/pdf")


@pytest.fixture
def usuario(django_user_model):
    return crear_usuario(django_user_model, "operador", GRUPO_GERENTE)


@pytest.fixture
def dia(db, usuario):
    return DiaArqueo.objects.create(fecha=FECHA, operado_por=usuario)


class _Pago:
    def __init__(self, medio, monto):
        self.medio = medio
        self.monto = monto


class _DocumentoLibro:
    """Doble simple del dataclass `DocumentoLibro` del importador — solo
    necesita los campos que `crear_desde_libro` lee."""
    def __init__(self, tipo, n_interno, folio, total, caja="CAJA 1", pagos=None):
        self.tipo = tipo
        self.n_interno = n_interno
        self.folio = folio
        self.total = total
        self.caja = caja
        self.pagos = pagos if pagos is not None else [_Pago("debito", total)]


def test_crear_desde_libro_deja_pendiente_y_no_duplica(dia):
    guia = _DocumentoLibro("DES", 200, "21184", 117406)
    creados = crear_desde_libro(dia, [guia], None)
    assert len(creados) == 1
    documento = creados[0]
    assert (documento.tipo, documento.folio, documento.monto, documento.estado) == (
        "DES", "21184", 117406, RegularizacionDocumento.Estado.PENDIENTE,
    )
    assert crear_desde_libro(dia, [guia], None) == []  # idempotente
    assert RegularizacionDocumento.objects.filter(dia=dia).count() == 1


def test_crear_desde_libro_precompleta_la_forma_de_pago_de_resto(dia):
    guia = _DocumentoLibro("DES", 200, "21184", 117406, pagos=[_Pago("credito", 117406)])
    documento = crear_desde_libro(dia, [guia], None)[0]
    assert documento.forma_pago == "credito"


def test_crear_desde_libro_deja_en_blanco_si_el_documento_trae_medios_mezclados(dia):
    guia = _DocumentoLibro("DES", 200, "21184", 117406, pagos=[_Pago("debito", 100000), _Pago("efectivo", 17406)])
    documento = crear_desde_libro(dia, [guia], None)[0]
    assert documento.forma_pago == ""


def test_documentos_pendientes_y_regularizados_separan_por_estado(dia, usuario):
    crear_desde_libro(dia, [
        _DocumentoLibro("DES", 200, "21184", 117406),
        _DocumentoLibro("CRED", 300, "N77", 5000),
    ], usuario)
    guia = RegularizacionDocumento.objects.get(folio="21184")
    assert documentos_pendientes(dia).count() == 2
    assert documentos_regularizados(dia).count() == 0

    regularizar(
        guia, documento_relacionado="123456", fecha_documento_relacionado=date(2026, 9, 5),
        referencia="Factura emitida al cierre", comprobante=_comprobante(), usuario=usuario,
    )
    assert documentos_pendientes(dia).count() == 1
    assert documentos_regularizados(dia).count() == 1


def test_regularizar_guarda_la_trazabilidad_completa_y_bitacora(dia, usuario):
    guia = crear_desde_libro(dia, [_DocumentoLibro("DES", 200, "21184", 117406, pagos=[_Pago("debito", 117406)])], usuario)[0]

    regularizar(
        guia, documento_relacionado="123456", fecha_documento_relacionado=date(2026, 9, 5),
        referencia="Transbank", comprobante=_comprobante(), usuario=usuario,
    )
    guia.refresh_from_db()
    assert guia.estado == RegularizacionDocumento.Estado.REGULARIZADO
    assert guia.documento_relacionado == "123456"
    assert guia.fecha_documento_relacionado == date(2026, 9, 5)
    assert guia.forma_pago == PagoDeclarado.Medio.DEBITO  # la que ya traía Resto, sin cambiarla
    assert guia.comprobante_archivo and guia.comprobante_nombre_original == "comprobante.pdf"
    assert guia.regularizado_por == usuario
    assert guia.regularizado_en is not None
    assert guia.dia_id == dia.pk  # el "arqueo afectado" de la trazabilidad

    evento = EventoAuditoria.objects.get(entidad="RegularizacionDocumento", accion="regularizar_documento")
    assert evento.usuario == usuario
    assert evento.valor_anterior == {"estado": "pendiente"}
    assert evento.valor_posterior["estado"] == "regularizado"


def test_el_comprobante_es_obligatorio_para_regularizar(dia, usuario):
    guia = crear_desde_libro(dia, [_DocumentoLibro("DES", 200, "21184", 117406)], usuario)[0]
    with pytest.raises(RegularizacionInvalida, match="[Cc]omprobante"):
        regularizar(
            guia, documento_relacionado="123456", fecha_documento_relacionado=None,
            referencia="", comprobante=None, usuario=usuario,
        )


def test_no_se_puede_regularizar_dos_veces(dia, usuario):
    guia = crear_desde_libro(dia, [_DocumentoLibro("DES", 200, "21184", 117406)], usuario)[0]
    regularizar(
        guia, documento_relacionado="123456", fecha_documento_relacionado=None,
        referencia="", comprobante=_comprobante(), usuario=usuario,
    )
    with pytest.raises(RegularizacionInvalida):
        regularizar(
            guia, documento_relacionado="999999", fecha_documento_relacionado=None,
            referencia="", comprobante=_comprobante(), usuario=usuario,
        )


def test_regularizar_exige_documento_relacionado(dia, usuario):
    guia = crear_desde_libro(dia, [_DocumentoLibro("DES", 200, "21184", 117406)], usuario)[0]
    with pytest.raises(RegularizacionInvalida):
        regularizar(
            guia, documento_relacionado="", fecha_documento_relacionado=None,
            referencia="", comprobante=_comprobante(), usuario=usuario,
        )


def test_no_se_puede_regularizar_sin_una_forma_de_pago_unica_declarada(dia, usuario):
    """Karla, 2026-09-22: la forma de pago no se puede escribir a mano —
    si Resto declaró más de un medio para el documento (caso raro), hay
    que corregirlo en el origen, no completar el hueco a mano acá."""
    guia = crear_desde_libro(dia, [
        _DocumentoLibro("DES", 200, "21184", 117406, pagos=[_Pago("debito", 100000), _Pago("efectivo", 17406)]),
    ], usuario)[0]
    assert guia.forma_pago == ""
    with pytest.raises(RegularizacionInvalida, match="forma de pago"):
        regularizar(
            guia, documento_relacionado="123456", fecha_documento_relacionado=None,
            referencia="", comprobante=_comprobante(), usuario=usuario,
        )


def test_flujo_completo_por_la_pantalla(client, dia, usuario):
    guia = crear_desde_libro(dia, [_DocumentoLibro("DES", 200, "21184", 117406, pagos=[_Pago("debito", 117406)])], usuario)[0]
    client.force_login(usuario)

    pantalla = client.get(reverse("regularizacion_documentos"))
    assert pantalla.status_code == 200
    assert "21184" in pantalla.content.decode()

    resp = client.post(reverse("regularizar_documento", args=[guia.pk]), {
        "documento_relacionado": "123456", "fecha_documento_relacionado": "2026-09-05",
        "referencia": "Factura al cierre", "comprobante": _comprobante(),
    })
    assert resp.status_code == 302
    guia.refresh_from_db()
    assert guia.estado == RegularizacionDocumento.Estado.REGULARIZADO
    assert guia.documento_relacionado == "123456"
    assert guia.comprobante_archivo


def test_solo_visualizacion_no_puede_regularizar(client, django_user_model, dia, usuario):
    from django.contrib.auth.models import Group
    from nucleo.documentos_servicio import GRUPO_SOLO_VISUALIZACION

    guia = crear_desde_libro(dia, [_DocumentoLibro("DES", 200, "21184", 117406)], usuario)[0]
    viewer = django_user_model.objects.create_user(username="viewer", password="x")
    viewer.groups.add(Group.objects.get_or_create(name=GRUPO_SOLO_VISUALIZACION)[0])
    client.force_login(viewer)

    client.post(reverse("regularizar_documento", args=[guia.pk]), {
        "documento_relacionado": "123456", "fecha_documento_relacionado": "2026-09-05",
        "referencia": "", "comprobante": _comprobante(),
    })
    guia.refresh_from_db()
    assert guia.estado == RegularizacionDocumento.Estado.PENDIENTE
