from decimal import Decimal, ROUND_HALF_UP
from typing import Union


def format_npr(amount: Union[int, float, Decimal, None], show_symbol: bool = True) -> str:
    """
    Formats monetary amounts according to the South Asian / Nepali numbering system:
    e.g., 12345678.50 -> Rs. 1,23,45,678.50
    """
    if amount is None:
        amount = Decimal('0.00')
    if not isinstance(amount, Decimal):
        amount = Decimal(str(amount))

    amount = amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    sign = "-" if amount < 0 else ""
    amount_abs = abs(amount)

    parts = f"{amount_abs:.2f}".split('.')
    integer_part = parts[0]
    decimal_part = parts[1]

    if len(integer_part) <= 3:
        formatted_int = integer_part
    else:
        last_three = integer_part[-3:]
        remaining = integer_part[:-3]
        groups = []
        while len(remaining) > 2:
            groups.insert(0, remaining[-2:])
            remaining = remaining[:-2]
        if remaining:
            groups.insert(0, remaining)
        formatted_int = ",".join(groups) + "," + last_three

    result = f"{sign}{formatted_int}.{decimal_part}"
    return f"Rs. {result}" if show_symbol else result


def number_to_words_nepali(amount: Union[int, float, Decimal, None]) -> str:
    """
    Converts a numerical figure to written words (English representation of South Asian count).
    Robustly handles arbitrary numbers from 0 up to thousands/lakhs of Crores without IndexError,
    by recursively decomposing large Crore groups.
    """
    if amount is None:
        return "Zero Rupees Only"

    units = [
        "", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
        "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen",
        "Seventeen", "Eighteen", "Nineteen"
    ]
    tens = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]

    def _convert_below_thousand(n: int) -> str:
        """Converts numbers from 1 to 999 to words."""
        if n == 0:
            return ""
        elif n < 20:
            return units[n]
        elif n < 100:
            t = tens[n // 10]
            u = units[n % 10]
            return f"{t} {u}".strip()
        else:
            h = units[n // 100] + " Hundred"
            rem = n % 100
            if rem > 0:
                return f"{h} {_convert_below_thousand(rem)}".strip()
            return h

    def _convert_integer_to_words(n: int) -> str:
        """
        Recursively converts any positive integer using South Asian denomination intervals
        (Crore, Lakh, Thousand, Hundred).
        """
        if n == 0:
            return ""
        if n < 1000:
            return _convert_below_thousand(n)

        parts = []
        crore = n // 10000000
        rem = n % 10000000
        lakh = rem // 100000
        rem = rem % 100000
        thousand = rem // 1000
        remainder = rem % 1000

        if crore > 0:
            # Recursive breakdown allows 20+ Crore, 100 Crore, 2500 Crore without indexing errors
            parts.append(f"{_convert_integer_to_words(crore)} Crore")
        if lakh > 0:
            parts.append(f"{_convert_below_thousand(lakh)} Lakh")
        if thousand > 0:
            parts.append(f"{_convert_below_thousand(thousand)} Thousand")
        if remainder > 0:
            parts.append(_convert_below_thousand(remainder))

        return " ".join(parts).strip()

    amount_dec = Decimal(str(amount)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    is_negative = amount_dec < 0
    amount_abs = abs(amount_dec)

    total_rs = int(amount_abs)
    paisa = int((amount_abs - total_rs) * 100)

    if total_rs == 0:
        rs_str = "Zero Rupees"
    else:
        rs_str = f"{_convert_integer_to_words(total_rs)} Rupees"

    if is_negative:
        rs_str = f"Minus {rs_str}"

    if paisa > 0:
        paisa_str = _convert_below_thousand(paisa).strip()
        return f"{rs_str} and {paisa_str} Paisa Only"

    return f"{rs_str} Only"