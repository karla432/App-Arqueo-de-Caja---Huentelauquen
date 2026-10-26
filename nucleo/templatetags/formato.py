from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django import template

register = template.Library()


@register.filter
def abs_valor(valor):
    try:
        return abs(valor)
    except TypeError:
        return valor


@register.filter
def miles(valor):
    """60000 -> "60.000" (separador de miles chileno, sin decimales; redondea como floatformat:0)."""
    if valor is None or valor == "":
        return ""
    try:
        entero = int(Decimal(str(valor)).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        return f"{entero:,}".replace(",", ".")
    except (InvalidOperation, TypeError, ValueError):
        return valor


@register.filter
def folio_corto(valor):
    """"F0610600008311" -> "8311": el folio real son los últimos 7 dígitos sin
    ceros a la izquierda (mismo criterio que la boleta en el archivo a Odoo)."""
    texto = str(valor or "")
    if len(texto) > 7 and texto[0].isalpha() and texto[1:].isdigit():
        return texto[-7:].lstrip("0") or texto
    return texto


@register.filter
def pesos(valor):
    """1234 -> "$1.234"; -500 -> "−$500"; None -> "—"."""
    if valor is None or valor == "":
        return "—"
    try:
        entero = int(Decimal(valor))
    except (InvalidOperation, TypeError, ValueError):
        return valor
    texto = f"{abs(entero):,}".replace(",", ".")
    return f"−${texto}" if entero < 0 else f"${texto}"
