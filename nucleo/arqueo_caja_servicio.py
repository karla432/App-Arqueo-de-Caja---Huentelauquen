"""Fase 2 (Karla, 2026-09-21): el asiento contable diario "Arqueo Caja",
segundo entregable junto al archivo de ventas. Refleja lo realmente
comprobado — efectivo contado y abonos de Transbank — no lo declarado por
la cajera: exactamente lo que quedó pendiente después de que Fase A ya
corrigió automáticamente todo lo que se pudo explicar.

Formato: el mismo que el asiento real que Karla entrega como ejemplo
(Diario, Fecha, Referencia, Cuenta, Etiqueta, Empresa, Distribución analítica, Débito,
Crédito). Es una sola entrada contable por día — Diario, Fecha y Referencia solo van en
la primera fila; el resto del día queda en blanco, igual que un pedido de
varias líneas en el archivo de ventas.

Solo existen dos cuentas de resultado (Karla, 2026-09-21): pérdida
(CUENTA_PERDIDA_CODIGO) y ganancia (CUENTA_GANANCIA_CODIGO) — ver
`nucleo.models.cuenta_por_signo`. Toda diferencia, sea de caja o de
tarjeta, cae siempre en una de esas dos.
"""
from pathlib import Path

import openpyxl
from django.conf import settings

from nucleo.archivos_seguros import guardar_libro
from nucleo.models import (
    ArqueoEfectivo, ConfiguracionArqueoCaja, CUENTA_GANANCIA_CODIGO, CUENTA_PERDIDA_CODIGO, DiaArqueo,
    Excepcion, PagoDeclarado, Reclasificacion,
)
from nucleo.pagos_divididos_servicio import EFECTIVO, cuenta_de_medio_odoo, planes_del_dia


def _cuenta_texto(codigo: str) -> str:
    from nucleo.models import CuentaContable
    cuenta = CuentaContable.objects.get(codigo=codigo)
    return f"{cuenta.codigo} {cuenta.nombre}"

COLUMNAS = [
    "Diario", "Fecha", "Referencia", "Apuntes contables/Cuenta", "Apuntes contables/Etiqueta",
    "Apuntes contables/Empresa", "Apuntes contables/Distribución analítica",
    "Apuntes contables/Débito", "Apuntes contables/Crédito",
]

CONTACTO_TRANSBANK = "TRANSBANK"
CONTACTO_MERCADO_PAGO = "MERCADO PAGO"
PROVEEDOR_POR_DEFECTO = "transbank"  # único adquirente hasta ahora — mismo supuesto que en exportacion_servicio

NOMBRE_ETIQUETA_POR_PROVEEDOR = {"transbank": "Transbank", "mercado_pago": "Mercado Pago"}
CONTACTO_POR_PROVEEDOR = {"transbank": CONTACTO_TRANSBANK, "mercado_pago": CONTACTO_MERCADO_PAGO}


def _proveedor_del_pago(pago) -> str:
    """Karla, 2026-09-21: hoy solo hay casos de Transbank, pero se deja
    reconociendo Mercado Pago desde ya (cuenta 1104008 – Redelcom) para
    cuando aparezca un caso real. Mismo criterio que ya usa el archivo de
    ventas: el movimiento con que calzó, o Transbank por defecto."""
    coincidencia = pago.coincidencias.first()
    movimiento = coincidencia.movimientos.first() if coincidencia else None
    return movimiento.proveedor if movimiento else PROVEEDOR_POR_DEFECTO


def _etiqueta(concepto: str, dia: DiaArqueo) -> str:
    return f"Diferencia {concepto} -Arqueo Caja Día {dia.fecha.strftime('%d/%m/%Y')}"


def _analitica(config: ConfiguracionArqueoCaja) -> str | None:
    return f'{{"{config.codigo_analitico}": 100.0}}' if config.codigo_analitico else None


def _par(*, cuenta_resultado, cuenta_balance, etiqueta, empresa, monto, es_perdida, config) -> list[dict]:
    """Una diferencia siempre son dos líneas que cuadran entre sí. Pérdida:
    baja el activo (Cr balance) y sube el gasto (Dr resultado). Ganancia:
    sube el activo (Dr balance) y sube el ingreso (Cr resultado). La
    distribución analítica va solo en la línea de resultado (así viene en
    el ejemplo real de Karla)."""
    debe_resultado, haber_resultado = (monto, None) if es_perdida else (None, monto)
    debe_balance, haber_balance = (None, monto) if es_perdida else (monto, None)
    return [
        {
            "Apuntes contables/Cuenta": cuenta_balance, "Apuntes contables/Etiqueta": etiqueta,
            "Apuntes contables/Empresa": empresa, "Apuntes contables/Distribución analítica": None,
            "Apuntes contables/Débito": debe_balance, "Apuntes contables/Crédito": haber_balance,
        },
        {
            "Apuntes contables/Cuenta": cuenta_resultado, "Apuntes contables/Etiqueta": etiqueta,
            "Apuntes contables/Empresa": empresa, "Apuntes contables/Distribución analítica": _analitica(config),
            "Apuntes contables/Débito": debe_resultado, "Apuntes contables/Crédito": haber_resultado,
        },
    ]


def _lineas_diferencias_de_caja(dia: DiaArqueo, config: ConfiguracionArqueoCaja) -> list[dict]:
    """Una por caja con diferencia — nunca se netean entre cajas (control
    §16, ya vigente en el arqueo de efectivo). Contacto: la cajera, para
    poder identificarla; no implica un descuento automático."""
    lineas = []
    etiqueta = _etiqueta("Efectivo", dia)
    for arqueo in ArqueoEfectivo.objects.filter(dia=dia).exclude(diferencia=None).exclude(diferencia=0).select_related("cajera").order_by("caja"):
        if not arqueo.cajera:
            raise ArqueoCajaInvalido(
                f"{arqueo.caja} tiene una diferencia de ${arqueo.diferencia:,.0f} sin cajera asignada — "
                "asígnala en Arqueo de efectivo antes de generar el asiento."
            )
        es_perdida = arqueo.diferencia < 0  # faltante = pérdida; sobrante = ganancia
        lineas.extend(_par(
            cuenta_resultado=_cuenta_texto(CUENTA_PERDIDA_CODIGO if es_perdida else CUENTA_GANANCIA_CODIGO),
            cuenta_balance=config.cuenta_efectivo, etiqueta=etiqueta, empresa=arqueo.cajera.nombre,
            monto=abs(arqueo.diferencia), es_perdida=es_perdida, config=config,
        ))
    return lineas


def _lineas_perdida_ganancia_tarjeta(dia: DiaArqueo, config: ConfiguracionArqueoCaja) -> list[dict]:
    """Diferencias de tarjeta resueltas a mano, una por una porque cada una
    ya se atribuyó a una cajera responsable (Karla, 2026-09-16) — a
    diferencia del ajuste de sencillo, que es automático y no se le puede
    atribuir a nadie en particular."""
    lineas = []
    excepciones = Excepcion.objects.filter(
        dia=dia, resolucion=Excepcion.Resolucion.PERDIDA_GANANCIA,
    ).select_related("cuenta_contable", "cajera_responsable", "movimiento", "pago").order_by("pk")
    for e in excepciones:
        if not e.cajera_responsable:
            raise ArqueoCajaInvalido(f"La pérdida/ganancia PG-{e.pk} no tiene cajera responsable asignada.")
        proveedor = e.movimiento.proveedor if e.movimiento_id else (_proveedor_del_pago(e.pago) if e.pago_id else PROVEEDOR_POR_DEFECTO)
        lineas.extend(_par(
            cuenta_resultado=f"{e.cuenta_contable.codigo} {e.cuenta_contable.nombre}",
            cuenta_balance=config.cuenta_por_proveedor(proveedor),
            etiqueta=_etiqueta(NOMBRE_ETIQUETA_POR_PROVEEDOR.get(proveedor, "Transbank"), dia),
            empresa=e.cajera_responsable.nombre,
            monto=e.monto_perdida_ganancia, es_perdida=e.cuenta_contable.tipo == "perdida", config=config,
        ))
    return lineas


def _linea_ajuste_sencillo(dia: DiaArqueo, config: ConfiguracionArqueoCaja) -> list[dict]:
    """Los ajustes de sencillo del día se agrupan en una sola línea neta
    por proveedor (igual que en el ejemplo real de Karla: cuatro
    diferencias de $1 de Transbank salen como un solo "Diferencia
    Transbank" de $2) — son automáticos, sin cajera responsable, así que
    el contacto es el nombre del proveedor."""
    excepciones = Excepcion.objects.filter(
        dia=dia, resolucion=Excepcion.Resolucion.AJUSTE_SENCILLO,
    ).select_related("cuenta_contable", "movimiento")
    netos: dict[str, float] = {}
    for e in excepciones:
        proveedor = e.movimiento.proveedor if e.movimiento_id else PROVEEDOR_POR_DEFECTO
        signo = 1 if e.cuenta_contable.tipo == "perdida" else -1
        netos[proveedor] = netos.get(proveedor, 0) + signo * e.monto_perdida_ganancia

    lineas = []
    for proveedor, neto in sorted(netos.items()):
        if not neto:
            continue
        lineas.extend(_par(
            cuenta_resultado=_cuenta_texto(CUENTA_PERDIDA_CODIGO if neto > 0 else CUENTA_GANANCIA_CODIGO),
            cuenta_balance=config.cuenta_por_proveedor(proveedor),
            etiqueta=_etiqueta(NOMBRE_ETIQUETA_POR_PROVEEDOR.get(proveedor, "Transbank"), dia),
            empresa=CONTACTO_POR_PROVEEDOR.get(proveedor, CONTACTO_TRANSBANK),
            monto=abs(neto), es_perdida=neto > 0, config=config,
        ))
    return lineas


_MEDIOS_TARJETA = {PagoDeclarado.Medio.DEBITO, PagoDeclarado.Medio.CREDITO}


def _cuenta_de_medio(medio: str, proveedor: str, config: ConfiguracionArqueoCaja) -> str | None:
    if medio == PagoDeclarado.Medio.EFECTIVO:
        return config.cuenta_efectivo
    if medio in _MEDIOS_TARJETA:
        return config.cuenta_por_proveedor(proveedor)
    return None  # ej. transferencia: sin cuenta configurada todavía


def _lineas_mezcla_de_medios(dia: DiaArqueo, config: ConfiguracionArqueoCaja) -> list[dict]:
    """Mezcla de medios (Karla, 2026-09-18): la venta ya se exportó entera
    bajo el medio declarado en el archivo de ventas — acá solo el traspaso
    de la parte que en realidad llegó por otro medio. Es un movimiento
    entre dos cuentas de balance, sin pérdida ni ganancia (confirmado con
    Karla, sigue vigente). Contacto: el número de comanda — no hay una
    cajera responsable ni un proveedor al que atribuírselo, es un error de
    clasificación del medio, no una diferencia real."""
    from nucleo.reclasificacion_servicio import REGLA_MEZCLA_DE_MEDIOS

    lineas = []
    etiqueta = _etiqueta("Mezcla de Medios", dia)
    reclasificaciones = Reclasificacion.objects.filter(
        dia=dia, regla=REGLA_MEZCLA_DE_MEDIOS,
    ).select_related("pago__venta").order_by("pk")
    for r in reclasificaciones:
        proveedor = r.evidencia.get("proveedor") or PROVEEDOR_POR_DEFECTO
        cuenta_corregida = _cuenta_de_medio(r.medio_corregido, proveedor, config)
        cuenta_original = _cuenta_de_medio(r.medio_original, proveedor, config)
        if not cuenta_original or not cuenta_corregida:
            # Nunca en silencio (Riesgo/Tesorero, 2026-09-23): un traspaso
            # omitido deja el efectivo o la tarjeta con un saldo falso.
            raise ArqueoCajaInvalido(
                f"La comanda {r.pago.venta.n_interno} tiene una mezcla de medios ({r.medio_original} → "
                f"{r.medio_corregido}) y uno de esos medios no tiene cuenta configurada (ej. transferencia): "
                "no se puede armar el traspaso."
            )
        empresa = f"Comanda {r.pago.venta.n_interno}"
        lineas.append({
            "Apuntes contables/Cuenta": cuenta_corregida, "Apuntes contables/Etiqueta": etiqueta,
            "Apuntes contables/Empresa": empresa, "Apuntes contables/Distribución analítica": None,
            "Apuntes contables/Débito": r.monto, "Apuntes contables/Crédito": None,
        })
        lineas.append({
            "Apuntes contables/Cuenta": cuenta_original, "Apuntes contables/Etiqueta": etiqueta,
            "Apuntes contables/Empresa": empresa, "Apuntes contables/Distribución analítica": None,
            "Apuntes contables/Débito": None, "Apuntes contables/Crédito": r.monto,
        })
    return lineas


class ArqueoCajaInvalido(Exception):
    pass


def _lineas_pago_dividido(dia: DiaArqueo, config: ConfiguracionArqueoCaja) -> list[dict]:
    """Comandas pagadas con más de un medio (Karla, 2026-09-23): la venta
    se exportó entera bajo el medio principal (Odoo admite uno solo por
    venta); acá viaja el traspaso de cada otra parte — Debe la cuenta del
    medio real / Haber la cuenta del medio de exportación. Una línea por
    medio, sin netear, sin IVA ni resultado. Ver pagos_divididos_servicio."""
    lineas = []
    for plan in sorted(planes_del_dia(dia), key=lambda p: p.venta.n_interno):
        venta = plan.venta
        boleta = venta.nro_comprobante[-7:].lstrip("0") or venta.nro_comprobante
        etiqueta = (
            f"Pago dividido Comanda {venta.n_interno} Boleta {boleta} ({plan.resumen}) "
            f"- Arqueo Caja Día {dia.fecha.strftime('%d/%m/%Y')}"
        )
        cuenta_principal = cuenta_de_medio_odoo(plan.principal, config)
        for medio, monto in plan.traspasos:
            cuenta_real = cuenta_de_medio_odoo(medio, config)
            if not cuenta_real or not cuenta_principal:
                raise ArqueoCajaInvalido(
                    f"Comanda {venta.n_interno}: falta la cuenta configurada de un medio del pago dividido."
                )
            proveedor_tarjeta = next((m for m in (medio, plan.principal) if m != EFECTIVO), None)
            empresa = CONTACTO_POR_PROVEEDOR.get(proveedor_tarjeta, CONTACTO_TRANSBANK)
            lineas.append({
                "Apuntes contables/Cuenta": cuenta_real, "Apuntes contables/Etiqueta": etiqueta,
                "Apuntes contables/Empresa": empresa, "Apuntes contables/Distribución analítica": None,
                "Apuntes contables/Débito": monto, "Apuntes contables/Crédito": None,
            })
            lineas.append({
                "Apuntes contables/Cuenta": cuenta_principal, "Apuntes contables/Etiqueta": etiqueta,
                "Apuntes contables/Empresa": empresa, "Apuntes contables/Distribución analítica": None,
                "Apuntes contables/Débito": None, "Apuntes contables/Crédito": monto,
            })
    return lineas


def generar_lineas_arqueo_caja(dia: DiaArqueo) -> list[dict]:
    """Construye las filas del asiento — sin escribir el archivo, para
    poder probarlas o mostrarlas aparte."""
    config = ConfiguracionArqueoCaja.vigente()
    filas = (
        _lineas_diferencias_de_caja(dia, config)
        + _lineas_perdida_ganancia_tarjeta(dia, config)
        + _linea_ajuste_sencillo(dia, config)
        + _lineas_mezcla_de_medios(dia, config)
        + _lineas_pago_dividido(dia, config)
    )
    for i, fila in enumerate(filas):
        fila["Diario"] = config.diario if i == 0 else None
        fila["Fecha"] = dia.fecha if i == 0 else None
        fila["Referencia"] = f"Arqueo Caja Día {dia.fecha:%d/%m/%Y}" if i == 0 else None
    return filas


def generar_archivo_arqueo_caja(
    dia: DiaArqueo, version: int, carpeta: Path | None = None, filas: list[dict] | None = None,
) -> tuple[Path, list[dict]]:
    carpeta = carpeta or Path(settings.BASE_DIR) / "exportaciones"
    carpeta.mkdir(exist_ok=True)
    ruta = carpeta / f"captura_arqueo_caja_{dia.fecha.isoformat()}_v{version}.xlsx"
    filas = generar_lineas_arqueo_caja(dia) if filas is None else filas

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Arqueo Caja"
    ws.append(COLUMNAS)
    for fila in filas:
        ws.append([fila[c] for c in COLUMNAS])
    guardar_libro(wb, ruta)
    return ruta, filas
