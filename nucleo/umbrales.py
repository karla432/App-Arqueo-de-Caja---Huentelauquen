"""Umbrales y tolerancias configurables (Karla, 2026-09-21). Módulo mínimo,
sin dependencias con los demás servicios, para poder usarse desde cualquiera
sin importaciones circulares. Se editan desde Configuración
(`nucleo.configuracion_servicio`)."""
from decimal import Decimal

from nucleo.models import Parametro

DEFINICIONES = {
    "umbral_diferencia_caja": {
        "nombre": "Diferencia de caja sin autorización",
        "descripcion": "Una caja con diferencia mayor a este monto (en más o en menos) necesita autorización para cerrar el día.",
        "defecto": 5000, "minimo": 0, "maximo": 1_000_000, "unidad": "$",
    },
    "umbral_visado_perdida": {
        "nombre": "Pérdida de caja que exige visado",
        "descripcion": "Una pérdida mayor a este monto necesita el visto bueno del Jefe de Contabilidad antes de exportar.",
        "defecto": 50_000, "minimo": 0, "maximo": 100_000_000, "unidad": "$",
    },
    "tolerancia_sencillo": {
        "nombre": "Tolerancia de ajuste de sencillo",
        "descripcion": "Diferencia máxima (positiva o negativa) entre el POS y el proveedor que se calza sola, a la cuenta de pérdida o ganancia según el signo.",
        "defecto": 10, "minimo": 0, "maximo": 50, "unidad": "$",
    },
    "umbral_reintegro_minimo": {
        "nombre": "Pérdida de cajera que no se lista para reintegro",
        "descripcion": "Las pérdidas de caja de una cajera hasta este monto no aparecen en el panel de Reintegros de Caja (no se cobrarán y ensuciarían la vista). Siguen contadas en el panel y en los reportes. Una pérdida que ya tiene abonos sí se sigue mostrando.",
        "defecto": 500, "minimo": 0, "maximo": 100000, "unidad": "$",
    },
    "tolerancia_guias": {
        "nombre": "Tolerancia de calce de guías de despacho",
        "descripcion": "Diferencia máxima entre el monto de una guía pagada con tarjeta y el cobro del proveedor para calzarlos solos, si además el horario del cobro es compatible con el de la guía. La diferencia se registra a la cuenta de pérdida o ganancia.",
        "defecto": 100, "minimo": 0, "maximo": 1000, "unidad": "$",
    },
    "umbral_diferencia_deposito": {
        "nombre": "Diferencia de depósito que obliga a Observado",
        "descripcion": "Un depósito con diferencia mayor a este monto no se puede Aprobar — el Jefe de Contabilidad solo puede dejarlo Observado.",
        "defecto": 5000, "minimo": 0, "maximo": 1_000_000, "unidad": "$",
    },
    "dias_alerta_nc": {
        "nombre": "Días hábiles para verificar una nota de crédito",
        "descripcion": "Una nota de crédito que pasa más de estos días hábiles sin verificar se marca en rojo. El cierre del día solo exige la evidencia cargada; la verificación y el folio de Odoo los exige el cierre de mes.",
        "defecto": 5, "minimo": 1, "maximo": 30, "unidad": "días hábiles",
    },
    "semaforo_cajeras_verde": {
        "nombre": "Desempeño de cajeras · límite verde",
        "descripcion": "Una cajera con hasta esta cantidad de errores cada 100 comandas queda en verde en el informe de Desempeño de cajeras.",
        "defecto": 1, "minimo": 0, "maximo": 100, "unidad": "errores c/100 comandas",
    },
    "semaforo_cajeras_amarillo": {
        "nombre": "Desempeño de cajeras · límite amarillo",
        "descripcion": "Sobre el límite verde y hasta este valor, la cajera queda en amarillo; sobre este valor, en rojo.",
        "defecto": 3, "minimo": 0, "maximo": 100, "unidad": "errores c/100 comandas",
    },
}


def umbral(clave: str) -> Decimal:
    fila = Parametro.objects.filter(clave=clave).first()
    return fila.valor if fila else Decimal(DEFINICIONES[clave]["defecto"])


def umbrales_vigentes() -> list[dict]:
    return [{"clave": c, **d, "valor": umbral(c)} for c, d in DEFINICIONES.items()]
