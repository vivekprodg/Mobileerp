from datetime import date, datetime
from typing import Union, Tuple
from apps.core.nepali_calendar import NepaliCalendar


def get_current_bs_date() -> Tuple[int, int, int]:
    """Returns today's BS date as (year, month, day)."""
    return NepaliCalendar.ad_to_bs(date.today())


def ad_to_bs_string(ad_dt: Union[date, datetime], lang: str = 'en') -> str:
    """Utility wrapper to convert any AD date to formatted BS string."""
    if isinstance(ad_dt, datetime):
        ad_dt = ad_dt.date()
    if not ad_dt:
        return ""
    y, m, d = NepaliCalendar.ad_to_bs(ad_dt)
    return NepaliCalendar.format_bs(y, m, d, lang=lang)


def bs_to_ad_date(bs_date_str: str) -> date:
    """
    Parses 'YYYY-MM-DD' formatted BS string to AD date object.
    Example: '2081-05-15' -> datetime.date(2024, 8, 31)
    """
    try:
        parts = bs_date_str.strip().split('-')
        if len(parts) != 3:
            raise ValueError
        y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
        return NepaliCalendar.bs_to_ad(y, m, d)
    except Exception as e:
        raise ValueError(f"Invalid BS Date string '{bs_date_str}'. Expected format YYYY-MM-DD.") from e