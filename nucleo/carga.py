"""Persiste el resultado de los importadores en el modelo de datos.

Capa intermedia entre los importadores (arqueo_diario/importadores/, que no
saben nada de Django ni de base de datos) y el modelo. Cada llamada crea un
LoteCarga nuevo — nunca sobrescribe uno anterior (control C6).
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime
from pathlib import Path

from django.db import transaction

from arqueo_diario.importadores.mercadopago import importar_settlement_mercadopago
from arqueo_diario.importadores.pos import importar_ventas_con_libro_contable, importar_ventas_por_articulo
from arqueo_diario.importadores.transbank import importar_cartola_transbank
from nucleo.models import (
    DiaArqueo, LineaVenta, LoteCarga, MovimientoProveedor, PagoDeclarado, Venta,
)
from nucleo.regularizacion_servicio import crear_desde_libro


def calcular_huella(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _parse_hora(texto: str):
    """'10:23 HS.' -> time(10, 23). Devuelve None si no se puede leer."""
    limpio = str(texto).replace("HS.", "").strip()
    try:
        return datetime.strptime(limpio, "%H:%M").time()
    except ValueError:
        return None


def _persistir_ventas(dia: DiaArqueo, lote: LoteCarga, resultado) -> None:
    for venta_importada in resultado.ventas:
        venta = Venta.objects.create(
            dia=dia, lote=lote, n_interno=venta_importada.n_interno,
            nro_comprobante=venta_importada.nro_comprobante, tipo_doc=venta_importada.tipo_doc,
            nro_comanda=str(venta_importada.nro_comanda), estado=venta_importada.estado,
            caja=venta_importada.caja, turno=venta_importada.turno,
            hora_apertura=_parse_hora(venta_importada.hora_apertura),
            hora_cierre=_parse_hora(venta_importada.hora_cierre),
            total_comanda=venta_importada.total_comanda,
            cliente=venta_importada.cliente, sucursal=venta_importada.sucursal,
        )
        LineaVenta.objects.bulk_create([
            LineaVenta(
                venta=venta, articulo=linea.articulo, cantidad=linea.cantidad,
                precio_unitario=linea.precio_unitario, total_linea=linea.total_linea,
            )
            for linea in venta_importada.lineas
        ])
        PagoDeclarado.objects.bulk_create([
            PagoDeclarado(venta=venta, medio=pago.medio, monto=pago.monto)
            for pago in venta_importada.pagos
        ])


def _persistir_documentos_pendientes(dia: DiaArqueo, resultado, usuario) -> None:
    """Guías de despacho (DES) y notas de crédito (CRED) del libro contable
    (Karla, 2026-09-22): ninguna entra a la carga masiva de ventas ni al
    arqueo automático — quedan como `RegularizacionDocumento` Pendiente
    para que la Encargada de Ingresos las regularice a mano cuando
    corresponda (menú "Regularización de Documentos"), sin perderlas."""
    crear_desde_libro(dia, resultado.guias + resultado.notas_credito + resultado.sin_clasificar, usuario)


@transaction.atomic
def cargar_pos(path: str | Path, fecha: date, usuario) -> LoteCarga:
    dia, _ = DiaArqueo.objects.get_or_create(fecha=fecha)

    resultado = importar_ventas_por_articulo(path, fecha)

    lote = LoteCarga.objects.create(
        dia=dia, fuente=LoteCarga.Fuente.POS, nombre_archivo=Path(path).name,
        huella_sha256=calcular_huella(path), ruta_almacenamiento=str(path),
        fecha_declarada=fecha, cargado_por=usuario, avisos=resultado.avisos,
    )
    _persistir_ventas(dia, lote, resultado)
    return lote


@transaction.atomic
def cargar_pos_con_libro_contable(
    archivo_ventas: str | Path, archivo_libro_contable: str | Path, fecha: date, usuario,
) -> LoteCarga:
    """Como `cargar_pos`, pero a partir de los dos archivos tal cual salen
    de Resto (sin consolidación manual): VENTAS POR ARTICULO sin N_INTERNO
    + el libro contable del que se recupera (Karla, 2026-09-16)."""
    dia, _ = DiaArqueo.objects.get_or_create(fecha=fecha)

    resultado = importar_ventas_con_libro_contable(archivo_ventas, archivo_libro_contable, fecha)

    lote = LoteCarga.objects.create(
        dia=dia, fuente=LoteCarga.Fuente.POS, nombre_archivo=Path(archivo_ventas).name,
        huella_sha256=calcular_huella(archivo_ventas), ruta_almacenamiento=str(archivo_ventas),
        nombre_archivo_secundario=Path(archivo_libro_contable).name,
        huella_sha256_secundario=calcular_huella(archivo_libro_contable),
        ruta_almacenamiento_secundario=str(archivo_libro_contable),
        fecha_declarada=fecha, cargado_por=usuario, avisos=resultado.avisos,
    )
    _persistir_ventas(dia, lote, resultado)
    _persistir_documentos_pendientes(dia, resultado, usuario)
    return lote


def _cargar_movimientos_proveedor(path: str | Path, fecha: date, usuario, *, fuente: str, importar) -> LoteCarga:
    dia, _ = DiaArqueo.objects.get_or_create(fecha=fecha)

    resultado = importar(path, fecha)

    lote = LoteCarga.objects.create(
        dia=dia, fuente=fuente, nombre_archivo=Path(path).name,
        huella_sha256=calcular_huella(path), ruta_almacenamiento=str(path),
        fecha_declarada=fecha, cargado_por=usuario, avisos=resultado.avisos,
    )
    MovimientoProveedor.objects.bulk_create([
        MovimientoProveedor(
            dia=dia, lote=lote, proveedor=m.proveedor, tipo=m.tipo, estado=m.estado,
            fecha_hora=m.fecha_hora, fecha_abono=m.fecha_abono or "", medio=m.medio,
            tipo_tarjeta=m.tipo_tarjeta, numero_tarjeta=m.numero_tarjeta,
            monto_original=m.monto_original, monto_afecto=m.monto_afecto,
            monto_exento=m.monto_exento, monto_vuelto=m.monto_vuelto, propina=m.propina,
            monto_venta_valido_abono=m.monto_venta_valido_abono, monto_anulado=m.monto_anulado,
            monto_cuota=m.monto_cuota, autorizacion=m.autorizacion, terminal=m.terminal,
            moneda=m.moneda,
        )
        for m in resultado.movimientos
    ])

    return lote


@transaction.atomic
def cargar_transbank(path: str | Path, fecha: date, usuario) -> LoteCarga:
    return _cargar_movimientos_proveedor(
        path, fecha, usuario, fuente=LoteCarga.Fuente.TRANSBANK, importar=importar_cartola_transbank,
    )


@transaction.atomic
def cargar_mercado_pago(path: str | Path, fecha: date, usuario) -> LoteCarga:
    return _cargar_movimientos_proveedor(
        path, fecha, usuario, fuente=LoteCarga.Fuente.MERCADO_PAGO, importar=importar_settlement_mercadopago,
    )
