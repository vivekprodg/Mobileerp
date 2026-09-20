"""
Nepali (Bikram Sambat) Calendar Core Algorithm & Reference Data.

Maps Gregorian Calendar (AD) to Bikram Sambat (BS) for years 2000 BS to 2095 BS.
Accurately accounts for variable astronomical month lengths (e.g., Ashadh having 30, 31, or 32 days)
and resolves Nepali Fiscal Year (आर्थिक वर्ष) cycles dynamically.
"""

from datetime import date, datetime, timedelta
from typing import Tuple, Union, Optional


class NepaliCalendar:
    """
    Nepali (Bikram Sambat) calendar conversion reference table & algorithm.
    Reference benchmark: 2000-01-01 BS = 1943-04-14 AD.
    """

    # Format: Year: (Days in Baishakh, Jestha, Ashadh, Shrawan, Bhadra, Ashwin, Kartik, Mangsir, Poush, Magh, Falgun, Chaitra)
    BS_MONTH_DATA = {
        2000: (30, 32, 31, 32, 31, 30, 30, 30, 29, 30, 29, 31),
        2001: (31, 31, 32, 31, 31, 31, 30, 29, 30, 29, 30, 30),
        2002: (31, 31, 32, 32, 31, 30, 30, 29, 30, 29, 30, 30),
        2003: (31, 32, 31, 32, 31, 30, 30, 30, 29, 29, 30, 31),
        2004: (30, 32, 31, 32, 31, 30, 30, 30, 29, 30, 29, 31),
        2005: (31, 31, 32, 31, 31, 31, 30, 29, 30, 29, 30, 30),
        2006: (31, 31, 32, 32, 31, 30, 30, 29, 30, 29, 30, 30),
        2007: (31, 32, 31, 32, 31, 30, 30, 30, 29, 29, 30, 31),
        2008: (31, 31, 31, 32, 31, 31, 29, 30, 29, 30, 29, 31),
        2009: (31, 31, 32, 31, 31, 31, 30, 29, 30, 29, 30, 30),
        2010: (31, 31, 32, 32, 31, 30, 30, 29, 30, 29, 30, 30),
        2011: (31, 32, 31, 32, 31, 30, 30, 30, 29, 29, 30, 31),
        2012: (31, 31, 31, 32, 31, 31, 29, 30, 29, 30, 29, 31),
        2013: (31, 31, 32, 31, 31, 31, 30, 29, 30, 29, 30, 30),
        2014: (31, 31, 32, 32, 31, 30, 30, 29, 30, 29, 30, 30),
        2015: (31, 32, 31, 32, 31, 30, 30, 30, 29, 29, 30, 31),
        2016: (31, 31, 31, 32, 31, 31, 29, 30, 29, 30, 29, 31),
        2017: (31, 31, 32, 31, 31, 31, 30, 29, 30, 29, 30, 30),
        2018: (31, 32, 31, 32, 31, 30, 30, 29, 30, 29, 30, 30),
        2019: (31, 32, 31, 32, 31, 30, 30, 30, 29, 30, 29, 31),
        2020: (31, 31, 31, 32, 31, 31, 30, 29, 30, 29, 30, 30),
        2021: (31, 31, 32, 31, 31, 31, 30, 29, 30, 29, 30, 30),
        2022: (31, 32, 31, 32, 31, 30, 30, 30, 29, 29, 30, 30),
        2023: (31, 32, 31, 32, 31, 30, 30, 30, 29, 30, 29, 31),
        2024: (31, 31, 31, 32, 31, 31, 30, 29, 30, 29, 30, 30),
        2025: (31, 31, 32, 31, 31, 31, 30, 29, 30, 29, 30, 30),
        2026: (31, 32, 31, 32, 31, 30, 30, 30, 29, 29, 30, 31),
        2027: (30, 32, 31, 32, 31, 30, 30, 30, 29, 30, 29, 31),
        2028: (31, 31, 32, 31, 31, 31, 30, 29, 30, 29, 30, 30),
        2029: (31, 31, 32, 31, 32, 30, 30, 29, 30, 29, 30, 30),
        2030: (31, 32, 31, 32, 31, 30, 30, 30, 29, 29, 30, 31),
        2031: (30, 32, 31, 32, 31, 30, 30, 30, 29, 30, 29, 31),
        2032: (31, 31, 32, 31, 31, 31, 30, 29, 30, 29, 30, 30),
        2033: (31, 31, 32, 32, 31, 30, 30, 29, 30, 29, 30, 30),
        2034: (31, 32, 31, 32, 31, 30, 30, 30, 29, 29, 30, 31),
        2035: (30, 32, 31, 32, 31, 31, 29, 30, 30, 29, 29, 31),
        2036: (31, 31, 32, 31, 31, 31, 30, 29, 30, 29, 30, 30),
        2037: (31, 31, 32, 32, 31, 30, 30, 29, 30, 29, 30, 30),
        2038: (31, 32, 31, 32, 31, 30, 30, 30, 29, 29, 30, 31),
        2039: (31, 31, 31, 32, 31, 31, 29, 30, 30, 29, 30, 30),
        2040: (31, 31, 32, 31, 31, 31, 30, 29, 30, 29, 30, 30),
        2041: (31, 31, 32, 32, 31, 30, 30, 29, 30, 29, 30, 30),
        2042: (31, 32, 31, 32, 31, 30, 30, 30, 29, 29, 30, 31),
        2043: (31, 31, 31, 32, 31, 31, 29, 30, 30, 29, 30, 30),
        2044: (31, 31, 32, 31, 31, 31, 30, 29, 30, 29, 30, 30),
        2045: (31, 32, 31, 32, 31, 30, 30, 29, 30, 29, 30, 30),
        2046: (31, 32, 31, 32, 31, 30, 30, 30, 29, 29, 30, 31),
        2047: (31, 31, 31, 32, 31, 31, 30, 29, 30, 29, 30, 30),
        2048: (31, 31, 32, 31, 31, 31, 30, 29, 30, 29, 30, 30),
        2049: (31, 32, 31, 32, 31, 30, 30, 30, 29, 29, 30, 30),
        2050: (31, 32, 31, 32, 31, 30, 30, 30, 29, 30, 29, 31),
        2051: (31, 31, 31, 32, 31, 31, 30, 29, 30, 29, 30, 30),
        2052: (31, 31, 32, 31, 31, 31, 30, 29, 30, 29, 30, 30),
        2053: (31, 32, 31, 32, 31, 30, 30, 30, 29, 29, 30, 30),
        2054: (31, 32, 31, 32, 31, 30, 30, 30, 29, 30, 29, 31),
        2055: (31, 31, 32, 31, 31, 31, 30, 29, 30, 29, 30, 30),
        2056: (31, 31, 32, 31, 32, 30, 30, 29, 30, 29, 30, 30),
        2057: (31, 32, 31, 32, 31, 30, 30, 30, 29, 29, 30, 31),
        2058: (30, 32, 31, 32, 31, 30, 30, 30, 29, 30, 29, 31),
        2059: (31, 31, 32, 31, 31, 31, 30, 29, 30, 29, 30, 30),
        2060: (31, 31, 32, 32, 31, 30, 30, 29, 30, 29, 30, 30),
        2061: (31, 32, 31, 32, 31, 30, 30, 30, 29, 29, 30, 31),
        2062: (30, 32, 31, 32, 31, 31, 29, 30, 29, 30, 29, 31),
        2063: (31, 31, 32, 31, 31, 31, 30, 29, 30, 29, 30, 30),
        2064: (31, 31, 32, 32, 31, 30, 30, 29, 30, 29, 30, 30),
        2065: (31, 32, 31, 32, 31, 30, 30, 30, 29, 29, 30, 31),
        2066: (31, 31, 31, 32, 31, 31, 29, 30, 30, 29, 29, 31),
        2067: (31, 31, 32, 31, 31, 31, 30, 29, 30, 29, 30, 30),
        2068: (31, 31, 32, 32, 31, 30, 30, 29, 30, 29, 30, 30),
        2069: (31, 32, 31, 32, 31, 30, 30, 30, 29, 29, 30, 31),
        2070: (31, 31, 31, 32, 31, 31, 29, 30, 30, 29, 30, 30),
        2071: (31, 31, 32, 31, 31, 31, 30, 29, 30, 29, 30, 30),
        2072: (31, 32, 31, 32, 31, 30, 30, 29, 30, 29, 30, 30),
        2073: (31, 32, 31, 32, 31, 30, 30, 30, 29, 29, 30, 31),
        2074: (31, 31, 31, 32, 31, 31, 30, 29, 30, 29, 30, 30),
        2075: (31, 31, 32, 31, 31, 31, 30, 29, 30, 29, 30, 30),
        2076: (31, 32, 31, 32, 31, 30, 30, 30, 29, 29, 30, 30),
        2077: (31, 32, 31, 32, 31, 30, 30, 30, 29, 30, 29, 31),
        2078: (31, 31, 31, 32, 31, 31, 30, 29, 30, 29, 30, 30),
        2079: (31, 31, 32, 31, 31, 31, 30, 29, 30, 29, 30, 30),
        2080: (31, 31, 32, 32, 31, 30, 30, 29, 30, 29, 30, 30),
        2081: (31, 31, 32, 31, 31, 31, 30, 29, 30, 29, 30, 30),
        2082: (31, 32, 31, 32, 31, 30, 30, 30, 29, 29, 30, 30),
        2083: (31, 32, 31, 32, 31, 30, 30, 30, 29, 30, 29, 31),
        2084: (31, 31, 31, 32, 31, 31, 30, 29, 30, 29, 30, 30),
        2085: (31, 31, 32, 31, 31, 31, 30, 29, 30, 29, 30, 30),
        2086: (31, 32, 31, 32, 31, 30, 30, 30, 29, 29, 30, 30),
        2087: (31, 32, 31, 32, 31, 30, 30, 30, 29, 30, 29, 31),
        2088: (31, 31, 31, 32, 31, 31, 30, 29, 30, 29, 30, 30),
        2089: (31, 31, 32, 31, 31, 31, 30, 29, 30, 29, 30, 30),
        2090: (31, 32, 31, 32, 31, 30, 30, 30, 29, 29, 30, 30),
        2091: (31, 32, 31, 32, 31, 30, 30, 30, 29, 30, 29, 31),
        2092: (31, 31, 32, 31, 31, 31, 30, 29, 30, 29, 30, 30),
        2093: (31, 31, 32, 32, 31, 30, 30, 29, 30, 29, 30, 30),
        2094: (31, 32, 31, 32, 31, 30, 30, 30, 29, 29, 30, 31),
        2095: (31, 31, 31, 32, 31, 31, 29, 30, 30, 29, 29, 31),
    }

    NEPALI_MONTH_NAMES_EN = [
        "Baishakh", "Jestha", "Ashadh", "Shrawan", "Bhadra", "Ashwin",
        "Kartik", "Mangsir", "Poush", "Magh", "Falgun", "Chaitra"
    ]

    NEPALI_MONTH_NAMES_NP = [
        "वैशाख", "जेठ", "असार", "साउन", "भदौ", "असोज",
        "कार्तिक", "मंसिर", "पुस", "माघ", "फागुन", "चैत"
    ]

    NEPALI_DIGITS = {
        '0': '०', '1': '१', '2': '२', '3': '३', '4': '४',
        '5': '५', '6': '६', '7': '७', '8': '८', '9': '९'
    }

    REF_AD_DATE = date(1943, 4, 14)
    REF_BS_YEAR = 2000
    REF_BS_MONTH = 1
    REF_BS_DAY = 1

    @classmethod
    def get_days_in_month(cls, bs_year: int, bs_month: int) -> int:
        """
        Returns exact number of days in any BS month (e.g. Ashadh 2080 = 32, Ashadh 2082 = 31).
        Month 1 = Baishakh, Month 2 = Jestha, Month 3 = Ashadh, Month 4 = Shrawan, etc.
        """
        if bs_year in cls.BS_MONTH_DATA and 1 <= bs_month <= 12:
            return cls.BS_MONTH_DATA[bs_year][bs_month - 1]
        return 30

    @classmethod
    def ad_to_bs(cls, ad_date: Union[date, datetime]) -> Tuple[int, int, int]:
        """
        Converts Gregorian AD date/datetime to BS tuple (Year, Month, Day).
        """
        if isinstance(ad_date, datetime):
            ad_date = ad_date.date()

        delta_days = (ad_date - cls.REF_AD_DATE).days
        if delta_days < 0:
            return cls.REF_BS_YEAR, cls.REF_BS_MONTH, cls.REF_BS_DAY

        bs_year = cls.REF_BS_YEAR
        bs_month = cls.REF_BS_MONTH
        bs_day = cls.REF_BS_DAY

        while delta_days > 0:
            days_in_month = cls.get_days_in_month(bs_year, bs_month)
            remaining_days = days_in_month - bs_day + 1

            if delta_days >= remaining_days:
                delta_days -= remaining_days
                bs_day = 1
                if bs_month == 12:
                    bs_year += 1
                    bs_month = 1
                else:
                    bs_month += 1
            else:
                bs_day += delta_days
                delta_days = 0

        return bs_year, bs_month, bs_day

    @classmethod
    def bs_to_ad(cls, bs_year: int, bs_month: int, bs_day: int) -> date:
        """
        Converts BS date tuple (Year, Month, Day) to Gregorian AD datetime.date.
        Accurately calculates through variable month lengths up to 2095 BS.
        """
        if bs_year < cls.REF_BS_YEAR:
            year_diff = cls.REF_BS_YEAR - bs_year
            approx_days = int(year_diff * 365.25)
            return cls.REF_AD_DATE - timedelta(days=approx_days)

        total_days = 0
        curr_year = cls.REF_BS_YEAR

        while curr_year < bs_year:
            months = cls.BS_MONTH_DATA.get(curr_year, (30,) * 12)
            total_days += sum(months)
            curr_year += 1

        months = cls.BS_MONTH_DATA.get(bs_year, (30,) * 12)
        valid_month = max(1, min(12, bs_month))
        for m in range(1, valid_month):
            total_days += months[m - 1]

        valid_day = max(1, min(cls.get_days_in_month(bs_year, valid_month), bs_day))
        total_days += (valid_day - 1)

        return cls.REF_AD_DATE + timedelta(days=total_days)

    @classmethod
    def get_fiscal_year(cls, bs_year: int, bs_month: int) -> str:
        """
        Identifies official Nepali Fiscal Year (आर्थिक वर्ष) string (e.g. '2080/81', '2083/84')
        for any BS year and month.
        Cycle begins on Shrawan 1 (Month 4) and ends on Ashadh (Month 3).
        """
        if bs_month >= 4:
            next_short = str(bs_year + 1)[-2:]
            return f"{bs_year}/{next_short}"
        else:
            prev_year = bs_year - 1
            curr_short = str(bs_year)[-2:]
            return f"{prev_year}/{curr_short}"

    @classmethod
    def get_fiscal_year_range(cls, fy_string: str) -> Tuple[date, date, str, str]:
        """
        Given fiscal year label (e.g. '2080/81', '2080-81', '2080/2081'):
        - Start Date: Shrawan 1 of base year -> converted to AD.
        - End Date: Last day of Ashadh in following year -> converted to AD.
        Returns: Tuple[start_date_ad, end_date_ad, start_date_bs, end_date_bs]
        """
        clean = fy_string.replace('-', '/').strip()
        parts = clean.split('/')
        start_bs_year = int(parts[0])
        end_bs_year = start_bs_year + 1

        start_date_bs = f"{start_bs_year:04d}-04-01"
        start_date_ad = cls.bs_to_ad(start_bs_year, 4, 1)

        last_day_ashadh = cls.get_days_in_month(end_bs_year, 3)
        end_date_bs = f"{end_bs_year:04d}-03-{last_day_ashadh:02d}"
        end_date_ad = cls.bs_to_ad(end_bs_year, 3, last_day_ashadh)

        return start_date_ad, end_date_ad, start_date_bs, end_date_bs

    @classmethod
    def get_bs_month_range(cls, bs_year: int, bs_month: int) -> Tuple[date, date, str, str]:
        """
        Returns start and end dates (both in Gregorian AD date and BS ISO string)
        for any specific BS month.
        Returns: Tuple[start_date_ad, end_date_ad, start_date_bs, end_date_bs]
        """
        valid_month = max(1, min(12, bs_month))
        start_date_bs = f"{bs_year:04d}-{valid_month:02d}-01"
        start_date_ad = cls.bs_to_ad(bs_year, valid_month, 1)

        last_day = cls.get_days_in_month(bs_year, valid_month)
        end_date_bs = f"{bs_year:04d}-{valid_month:02d}-{last_day:02d}"
        end_date_ad = cls.bs_to_ad(bs_year, valid_month, last_day)

        return start_date_ad, end_date_ad, start_date_bs, end_date_bs

    @classmethod
    def format_bs(cls, bs_year: int, bs_month: int, bs_day: int, lang: str = 'en') -> str:
        """Formats BS date into standard ISO 'YYYY-MM-DD' or localized Devanagari text."""
        valid_month = max(1, min(12, bs_month))
        last_day = cls.get_days_in_month(bs_year, valid_month)
        valid_day = max(1, min(last_day, bs_day))

        if lang == 'np':
            month_name = cls.NEPALI_MONTH_NAMES_NP[valid_month - 1]
            raw = f"{valid_day:02d} {month_name} {bs_year}"
            return "".join(cls.NEPALI_DIGITS.get(ch, ch) for ch in raw)
        return f"{bs_year:04d}-{valid_month:02d}-{valid_day:02d}"