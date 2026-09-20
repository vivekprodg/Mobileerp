"""
Nepali Date Converter Utilities & String Sanitization Engine.

Provides robust parsing for spreadsheet migration and counter forms:
- Normalizes dots (2080.04.01), dashes (2080-04-01), slashes (2080/04/01), underscores, and spaces.
- Converts Devanagari digits (२०८०.०४.०१) to Arabic digits automatically.
- Strips timestamps (e.g., '2080.04.01 11:20:00' or '2080-04-01T00:00:00') cleanly.
- Auto-detects Year-First (YYYY.MM.DD) vs. Day-First (DD.MM.YYYY).
- Automatically clamps invalid or slightly malformed days to the month's maximum valid day.
- Converts BS date strings to standard Gregorian datetime.date objects for Django and PostgreSQL.
"""

import re
from datetime import date, datetime
from typing import Union, Tuple, Any, Optional
from apps.core.nepali_calendar import NepaliCalendar

# Devanagari to ASCII Arabic Numeral Translation Map
DEVANAGARI_TO_ARABIC = {
    '०': '0', '१': '1', '२': '2', '३': '3', '४': '4',
    '५': '5', '६': '6', '७': '7', '८': '8', '९': '9'
}


def get_current_bs_date() -> Tuple[int, int, int]:
    """Returns today's BS date as (year, month, day)."""
    return NepaliCalendar.ad_to_bs(date.today())


def get_current_fiscal_year() -> str:
    """
    Returns active ongoing Nepali Fiscal Year string (e.g. '2081/82').
    Derived from today's Nepali calendar date.
    """
    bs_year, bs_month, _ = get_current_bs_date()
    return NepaliCalendar.get_fiscal_year(bs_year, bs_month)


def ad_to_bs_string(ad_dt: Union[date, datetime, str, None], lang: str = 'en') -> str:
    """
    Converts Gregorian AD date/datetime to formatted BS string.
    Returns 'YYYY-MM-DD' or localized Devanagari text.
    """
    if not ad_dt:
        return ""
    if isinstance(ad_dt, str):
        try:
            ad_dt = datetime.strptime(ad_dt[:10], '%Y-%m-%d').date()
        except (ValueError, TypeError):
            return ""
    elif isinstance(ad_dt, datetime):
        ad_dt = ad_dt.date()

    y, m, d = NepaliCalendar.ad_to_bs(ad_dt)
    return NepaliCalendar.format_bs(y, m, d, lang=lang)


def parse_bs_date_components(bs_date_input: Any) -> Tuple[int, int, int]:
    """
    Sanitizes arbitrary text inputs and extracts numeric (year, month, day).
    
    HANDLES ALL STANDARD & SPREADSHEET EXPORT VARIATIONS:
    - Mobilesoft Dot notation: '2080.04.01' or '2080.4.1'
    - Slash notation: '2080/04/01' or '01/04/2080'
    - Dash notation: '2080-04-01' or '01-04-2080'
    - Space or timestamps: '2080.04.01 10:30:00', '2080-04-01T12:00:00'
    - Devanagari numerals: '२०८०.०४.०१' or '२०८०/०४/०१'
    """
    if bs_date_input is None:
        raise ValueError("BS date input cannot be None.")

    raw_str = str(bs_date_input).strip()
    if not raw_str or raw_str.lower() in ['', 'none', 'nan', 'null', 'n/a', '-', '--']:
        raise ValueError(f"Invalid or empty BS date input: '{bs_date_input}'.")

    # 1. Translate any Devanagari digits to ASCII Arabic digits
    for dev_char, eng_char in DEVANAGARI_TO_ARABIC.items():
        if dev_char in raw_str:
            raw_str = raw_str.replace(dev_char, eng_char)

    # 2. Strip any time component (e.g. ' 10:30:00', 'T00:00:00')
    raw_str = raw_str.split(' ')[0].split('T')[0]

    # 3. Convert all delimiters (dots, slashes, underscores, commas) to dashes
    clean = re.sub(r'[^\d]', '-', raw_str)
    parts = [int(p) for p in clean.split('-') if p]

    if len(parts) < 3:
        raise ValueError(f"Invalid BS Date string '{bs_date_input}'. Expected YYYY.MM.DD, YYYY-MM-DD, or YYYY/MM/DD.")

    # 4. Auto-detect whether Year is at position 0 (YYYY-MM-DD) or position 2 (DD-MM-YYYY)
    if 2000 <= parts[0] <= 2095:
        y, m, d = parts[0], parts[1], parts[2]
    elif 2000 <= parts[2] <= 2095:
        y, m, d = parts[2], parts[1], parts[0]
    else:
        # Fallback heuristic for 2-digit years or edge cases
        y = parts[0] if parts[0] >= 2000 else parts[0] + 2000
        m = parts[1]
        d = parts[2]

    if not (2000 <= y <= 2095):
        raise ValueError(f"BS Year {y} is outside the supported range (2000 BS to 2095 BS).")

    # 5. Month bounds check & self-healing
    m = max(1, min(12, m))

    # 6. Day bounds check & astronomical self-healing
    max_days = NepaliCalendar.get_days_in_month(y, m)
    d = max(1, min(max_days, d))

    return y, m, d


def bs_to_ad_date(bs_date_str: Any) -> date:
    """
    Parses any formatted BS string (e.g. '2080.04.01', '2080-04-01', '2080/04/01')
    to an authoritative Gregorian AD datetime.date object for database insertion.
    """
    y, m, d = parse_bs_date_components(bs_date_str)
    return NepaliCalendar.bs_to_ad(y, m, d)


def parse_bs_date_to_ad(bs_date_str: Any, default_date: Optional[date] = None) -> Tuple[date, str]:
    """
    Helper for data migration pipelines (e.g. Mobilesoft sales register imports):
    Returns: Tuple[Gregorian AD date object, standardized ISO BS string 'YYYY-MM-DD']
    If parsing fails and default_date is provided, safely returns the default converted.
    """
    try:
        y, m, d = parse_bs_date_components(bs_date_str)
        ad_date = NepaliCalendar.bs_to_ad(y, m, d)
        formatted_bs = f"{y:04d}-{m:02d}-{d:02d}"
        return ad_date, formatted_bs
    except Exception:
        if default_date is not None:
            dy, dm, dd = NepaliCalendar.ad_to_bs(default_date)
            return default_date, f"{dy:04d}-{dm:02d}-{dd:02d}"
        raise


def get_fiscal_year_bounds(fy_string: str) -> Tuple[date, date, str, str]:
    """
    Returns: Tuple[start_date_ad, end_date_ad, start_date_bs, end_date_bs]
    """
    return NepaliCalendar.get_fiscal_year_range(fy_string)


def get_bs_month_bounds(bs_year: int, bs_month: int) -> Tuple[date, date, str, str]:
    """
    Returns: Tuple[start_date_ad, end_date_ad, start_date_bs, end_date_bs]
    """
    return NepaliCalendar.get_bs_month_range(bs_year, bs_month)


def format_bs_date_for_display(bs_date_str: Any, lang: str = 'en') -> str:
    """Formats any raw BS date string or date object into localized display format."""
    try:
        y, m, d = parse_bs_date_components(bs_date_str)
        return NepaliCalendar.format_bs(y, m, d, lang=lang)
    except Exception:
        return str(bs_date_str or '')