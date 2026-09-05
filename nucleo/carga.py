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

from arqueo_diario.importadores.pos import importar_ventas_por_articulo
from arqueo_diario.importadores.transbank import importar_cartola_transbank
from nucleo.models import (
    DiaArqueo, LineaVenta, LoteCarga, MovimientoProveedor, PagoDeclarado, Venta,
)


def _huella(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _parse_hora(texto: str):
    """'10:23 HS.' -> time(10, 23). Devuelve None si no se puede leer."""
    limpio = str(texto).replace("HS.", "").strip()
    try:
        return datetime.strptime(limpio, "%H:%M").time()
    except ValueError:
        return None


@transaction.atomic
def cargar_pos(path: str | Path, fecha: date, usuario) -> LoteCarga:
    dia, _ = DiaArqueo.objects.get_or_create(fecha=fecha)

    lote = LoteCarga.objects.create(
        dia=dia, fuente=LoteCarga.Fuente.POS, nombre_archivo=Path(path).name,
        huella_sha256=_huella(path), ruta_almacenamiento=str(path),
        fecha_declarada=fecha, cargado_por=usuario,
    )

    resultado = importar_ventas_por_articulo(path, fecha)
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

    return lote


@transaction.atomic
def cargar_transbank(path: str | Path, fecha: date, usuario) -> LoteCarga:
    dia, _ = DiaArqueo.objects.get_or_create(fecha=fecha)

    lote = LoteCarga.objects.create(
        dia=dia, fuente=LoteCarga.Fuente.TRANSBANK, nombre_archivo=Path(path).name,
        huella_sha256=_huella(path), ruta_almacenamiento=str(path),
        fecha_declarada=fecha, cargado_por=usuario,
    )

    resultado = importar_cartola_transbank(path, fecha)
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
