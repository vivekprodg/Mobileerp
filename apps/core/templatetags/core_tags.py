from decimal import Decimal, InvalidOperation
from django import template

register = template.Library()


@register.filter(name='get_item')
def get_item(dictionary, key):
    """
    Allows dictionary lookups in Django templates using dynamic keys:
    Usage: {{ dictionary|get_item:key_variable }}
    """
    if isinstance(dictionary, dict):
        return dictionary.get(key, '')
    return ''


@register.filter(name='sub')
def sub(value, arg):
    """
    Subtracts arg from value with Decimal precision and graceful error handling.
    Usage: {{ value|sub:arg }}
    Example: {{ product.selling_price|sub:tier.price_per_unit|floatformat:2 }}
    """
    if value is None or str(value).strip() == '':
        value = 0
    if arg is None or str(arg).strip() == '':
        arg = 0

    try:
        val_dec = Decimal(str(value))
        arg_dec = Decimal(str(arg))
        return val_dec - arg_dec
    except (ValueError, TypeError, InvalidOperation):
        try:
            return float(value) - float(arg)
        except (ValueError, TypeError):
            return 0


@register.filter(name='mul')
def mul(value, arg):
    """
    Multiplies value by arg with Decimal precision.
    Usage: {{ value|mul:arg }}
    """
    if value is None or str(value).strip() == '':
        value = 0
    if arg is None or str(arg).strip() == '':
        arg = 0

    try:
        val_dec = Decimal(str(value))
        arg_dec = Decimal(str(arg))
        return val_dec * arg_dec
    except (ValueError, TypeError, InvalidOperation):
        try:
            return float(value) * float(arg)
        except (ValueError, TypeError):
            return 0


@register.filter(name='div')
def div(value, arg):
    """
    Divides value by arg with safe division-by-zero protection.
    Usage: {{ value|div:arg }}
    """
    if value is None or str(value).strip() == '':
        value = 0
    if arg is None or str(arg).strip() == '':
        return 0

    try:
        val_dec = Decimal(str(value))
        arg_dec = Decimal(str(arg))
        if arg_dec == Decimal('0'):
            return Decimal('0.00')
        return val_dec / arg_dec
    except (ValueError, TypeError, InvalidOperation):
        try:
            f_arg = float(arg)
            if f_arg == 0:
                return 0
            return float(value) / f_arg
        except (ValueError, TypeError):
            return 0