"""
Party-Wise Confirmation & Sub-Ledger Accounting Engine.

Capabilities:
1. Multi-Calendar Date & Fiscal Year Translation:
   - Supports official Nepali Fiscal Years (e.g., '2080/81', '2081/82', '2082/83', '2083/84').
   - Converts Bikram Sambat (BS) date inputs into Gregorian (AD) dates for database indexing.
   - Accurately resolves Shrawan 1 to dynamic Ashadh end (30, 31, or 32 days).
2. Clean Date Boundary Resolution:
   - Avoids silent fallbacks to today's date when an earlier month or historical period within
     the fiscal year is queried.
   - Padded two-digit normalization for B.S. months and days.
3. Dynamic Historical Opening Balance Calculator:
   - Reconstructs exact opening balance as of the start date by summing all transactions
     posted prior to that date (including the Mobilesoft/Hisaav migration opening journals).
   - Customer (Trade Debtor / Dr Normal): Opening Bal = Total Prior Debits - Total Prior Credits.
   - Supplier (Trade Creditor / Cr Normal): Opening Bal = Total Prior Credits - Total Prior Debits.
4. Chronological Period Ledger Aggregation:
   - Queries posted double-entry journal items filtered strictly to party control accounts
     (excluding internal counter cash/bank lines to prevent voucher double-counting).
5. Sequential Running Balance Math:
   - Calculates line-by-line running balance with debit/credit turnover summaries.
6. Formal Audit Confirmation Package:
   - Returns party details, PAN, dates, ledger lines, closing balance, and legal confirmation text.
"""

import re
from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime
from typing import Dict, Any, List, Optional, Tuple

from django.db.models import Q, Sum
from django.utils import timezone
from django.shortcuts import get_object_or_404

from apps.accounting.models import JournalItem, Account, AccountingFiscalYear
from apps.customers.models import Customer
from apps.purchases.models import Supplier
from apps.branches.models import Branch
from apps.core.nepali_calendar import NepaliCalendar
from apps.core.utils.nepali_date_converter import (
    parse_bs_date_components,
    parse_bs_date_to_ad,
    ad_to_bs_string
)

def number_to_words_nepali_format(amount: Decimal) -> str:
    """
    Converts a Decimal amount into South Asian / Nepali standard words
    (Crores, Lakhs, Thousands, Hundreds, Units and Paisa).
    Example: 150250.75 -> 'Rupees One Lakh Fifty Thousand Two Hundred Fifty and Seventy Five Paisa Only'
    """
    if amount is None:
        return "Zero Rupees Only"

    units = [
        "", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
        "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen",
        "Seventeen", "Eighteen", "Nineteen"
    ]
    tens = [
        "", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"
    ]

    abs_amount = abs(amount)
    rupees = int(abs_amount)
    paisa = int(round((abs_amount - rupees) * 100))

    if rupees == 0 and paisa == 0:
        return "Zero Rupees Only"

    def two_digits(n: int) -> str:
        if n == 0:
            return ""
        if n < 20:
            return units[n]
        ten_part = tens[n // 10]
        unit_part = units[n % 10]
        return f"{ten_part} {unit_part}".strip()

    def three_digits(n: int) -> str:
        h = n // 100
        rest = n % 100
        parts = []
        if h > 0:
            parts.append(f"{units[h]} Hundred")
        if rest > 0:
            parts.append(two_digits(rest))
        return " ".join(parts).strip()

    crore = rupees // 10000000
    rupees %= 10000000

    lakh = rupees // 100000
    rupees %= 100000

    thousand = rupees // 1000
    rupees %= 1000

    hundreds = rupees

    words_list = []
    if crore > 0:
        words_list.append(f"{three_digits(crore)} Crore")
    if lakh > 0:
        words_list.append(f"{two_digits(lakh)} Lakh")
    if thousand > 0:
        words_list.append(f"{two_digits(thousand)} Thousand")
    if hundreds > 0:
        words_list.append(three_digits(hundreds))

    rupees_str = " ".join(words_list).strip() or "Zero"
    prefix = "Minus " if amount < Decimal('0.00') else ""

    if paisa > 0:
        paisa_str = two_digits(paisa)
        return f"{prefix}Rupees {rupees_str} and {paisa_str} Paisa Only"
    return f"{prefix}Rupees {rupees_str} Only"

class PartyLedgerService:
    """
    Authoritative calculation engine for Customer & Supplier Confirmation Statements.
    """

    PAYMENT_CLEARING_TAGS = [
        'CASH', 'BANK', 'FONEPAY', 'ESEWA', 'KHALTI',
        'CARD_CLEARING', 'SALES_REVENUE', 'COGS',
        'OUTPUT_VAT', 'INPUT_VAT', 'INVENTORY_ASSET'
    ]

    @classmethod
    def get_available_fiscal_years(cls) -> List[str]:
        """Returns standard list of Nepali Fiscal Years (2080/81 to 2083/84)."""
        fys = list(
            AccountingFiscalYear.objects.order_by('-start_date_ad').values_list('name', flat=True)
        )
        if not fys:
            fys = ['2083/84', '2082/83', '2081/82', '2080/81']
        return fys

    @classmethod
    def _parse_date_input(cls, raw_val: Any) -> Optional[Tuple[date, str]]:
        """
        Parses arbitrary date strings in either Gregorian AD or Nepali BS (YYYY-MM-DD, YYYY.MM.DD, YYYY/MM/DD)
        with single or double-digit month/day padding.
        Returns a clean (ad_date, bs_date_string) tuple or None if unparseable.
        """
        if not raw_val:
            return None

        if isinstance(raw_val, datetime):
            raw_val = raw_val.date()
        if isinstance(raw_val, date):
            ad_d = raw_val
            y, m, d = NepaliCalendar.ad_to_bs(ad_d)
            return ad_d, f"{y:04d}-{m:02d}-{d:02d}"

        raw_str = str(raw_val).strip()
        if not raw_str or raw_str.lower() in ['none', 'nan', 'null', '-', '--']:
            return None

        # 1. Try B.S. Date Parsing
        try:
            bs_y, bs_m, bs_d = parse_bs_date_components(raw_str)
            ad_d = NepaliCalendar.bs_to_ad(bs_y, bs_m, bs_d)
            bs_str = f"{bs_y:04d}-{bs_m:02d}-{bs_d:02d}"
            return ad_d, bs_str
        except Exception:
            pass

        # 2. Try Gregorian AD Date Parsing (YYYY-MM-DD or YYYY/MM/DD)
        clean = re.sub(r'[^\d]', '-', raw_str)
        parts = [int(p) for p in clean.split('-') if p]
        if len(parts) == 3 and 1970 <= parts[0] <= 2050:
            try:
                ad_d = date(parts[0], parts[1], parts[2])
                y, m, d = NepaliCalendar.ad_to_bs(ad_d)
                bs_str = f"{y:04d}-{m:02d}-{d:02d}"
                return ad_d, bs_str
            except (ValueError, TypeError):
                return None

        return None

    @classmethod
    def resolve_period_dates(cls, params: Dict[str, Any]) -> Tuple[date, date, str, str, str]:
        """
        Resolves query parameters into standardized date bounds:
        Returns: (start_date_ad, end_date_ad, start_date_bs, end_date_bs, fiscal_year_label)

        Strictness Rules:
        - When custom dates are supplied within an earlier month or historical fiscal year,
          the end_date strictly respects that requested period and NEVER silently leaps forward to today!
        - If a fiscal year preset is chosen, the entire range of that fiscal year is returned.
        - If no parameters are given, defaults to the ongoing active fiscal year.
        """
        fy_param = str(params.get('fiscal_year') or '').strip()
        start_param = str(params.get('start_date') or '').strip()
        end_param = str(params.get('end_date') or '').strip()

        parsed_start = cls._parse_date_input(start_param) if start_param else None
        parsed_end = cls._parse_date_input(end_param) if end_param else None

        # Case 1: Both Start and End Custom Dates are Explicitly Provided
        if parsed_start and parsed_end:
            start_ad, start_bs = parsed_start
            end_ad, end_bs = parsed_end

            if start_ad > end_ad:
                start_ad, end_ad = end_ad, start_ad
                start_bs, end_bs = end_bs, start_bs

            bs_y, bs_m, _ = NepaliCalendar.ad_to_bs(start_ad)
            resolved_fy = NepaliCalendar.get_fiscal_year(bs_y, bs_m)
            return start_ad, end_ad, start_bs, end_bs, resolved_fy

        # Case 2: Only Start Date is Provided -> Scope to that Month or Fiscal Year
        if parsed_start and not parsed_end:
            start_ad, start_bs = parsed_start
            bs_y, bs_m, bs_d = parse_bs_date_components(start_bs)
            resolved_fy = NepaliCalendar.get_fiscal_year(bs_y, bs_m)

            # End of that specific B.S. month
            max_days = NepaliCalendar.get_days_in_month(bs_y, bs_m)
            end_ad = NepaliCalendar.bs_to_ad(bs_y, bs_m, max_days)
            end_bs = f"{bs_y:04d}-{bs_m:02d}-{max_days:02d}"

            return start_ad, end_ad, start_bs, end_bs, resolved_fy

        # Case 3: Only End Date is Provided -> Scope from Month Start to End Date
        if parsed_end and not parsed_start:
            end_ad, end_bs = parsed_end
            bs_y, bs_m, bs_d = parse_bs_date_components(end_bs)
            resolved_fy = NepaliCalendar.get_fiscal_year(bs_y, bs_m)

            start_ad = NepaliCalendar.bs_to_ad(bs_y, bs_m, 1)
            start_bs = f"{bs_y:04d}-{bs_m:02d}-01"

            return start_ad, end_ad, start_bs, end_bs, resolved_fy

        # Case 4: Fiscal Year Preset Selected
        if fy_param and fy_param.lower() not in ['all', 'none', '']:
            clean_fy = fy_param.replace('-', '/').strip()
            try:
                s_ad, e_ad, s_bs, e_bs = NepaliCalendar.get_fiscal_year_range(clean_fy)
                return s_ad, e_ad, s_bs, e_bs, clean_fy
            except Exception:
                pass

        # Case 5: Default Fallback -> Active Fiscal Year
        today = timezone.now().date()
        today_y, today_m, today_d = NepaliCalendar.ad_to_bs(today)
        curr_fy = NepaliCalendar.get_fiscal_year(today_y, today_m)

        try:
            s_ad, e_ad, s_bs, e_bs = NepaliCalendar.get_fiscal_year_range(curr_fy)
            return s_ad, e_ad, s_bs, e_bs, curr_fy
        except Exception:
            start_ad = date(today.year, 1, 1)
            end_ad = today
            sy, sm, sd = NepaliCalendar.ad_to_bs(start_ad)
            ey, em, ed = NepaliCalendar.ad_to_bs(end_ad)
            return start_ad, end_ad, f"{sy:04d}-{sm:02d}-{sd:02d}", f"{ey:04d}-{em:02d}-{ed:02d}", curr_fy

    @classmethod
    def get_party_ledger_statement(
        cls,
        party_type: str,
        party_id: int,
        params: Dict[str, Any],
        branch: Optional[Branch] = None
    ) -> Dict[str, Any]:
        """
        Produces the authoritative party ledger and confirmation statement payload.
        Handles both migrated data and new fiscal year periods seamlessly.
        """
        party_type_norm = str(party_type).strip().upper()
        if party_type_norm not in ['CUSTOMER', 'SUPPLIER']:
            party_type_norm = 'CUSTOMER'

        start_date_ad, end_date_ad, start_date_bs, end_date_bs, fiscal_year = cls.resolve_period_dates(params)

        # 1. Resolve Party Profile
        if party_type_norm == 'CUSTOMER':
            party = get_object_or_404(Customer, pk=party_id)
            party_name = party.name
            party_code = f"CUST-{party.id:04d}"
            party_phone = party.phone_number or "-"
            party_pan = party.pan_number or "-"
            party_address = party.address or "Kathmandu, Nepal"
            is_debtor = True
            control_tag = 'ACCOUNTS_RECEIVABLE'
        else:
            party = get_object_or_404(Supplier, pk=party_id)
            party_name = party.company_name
            party_code = party.code or f"SUP-{party.id:04d}"
            party_phone = party.phone_number or "-"
            party_pan = party.pan_number or "-"
            party_address = party.address or "Kathmandu, Nepal"
            is_debtor = False
            control_tag = 'ACCOUNTS_PAYABLE'

        # 2. Base QuerySet Scoped to the Specific Party Control Lines
        base_items = JournalItem.objects.filter(
            journal_entry__status='POSTED'
        ).select_related('journal_entry', 'account', 'journal_entry__branch')

        if is_debtor:
            party_filter = Q(customer=party)
        else:
            party_filter = Q(supplier=party)

        control_account_filter = (
            Q(account__system_tag=control_tag) |
            ~Q(account__system_tag__in=cls.PAYMENT_CLEARING_TAGS)
        )

        scoped_items = base_items.filter(party_filter).filter(control_account_filter)

        if branch:
            scoped_items = scoped_items.filter(journal_entry__branch=branch)

        # 3. Dynamic Opening Balance Calculation (Prior to start_date_ad)
        prior_agg = scoped_items.filter(
            journal_entry__entry_date__lt=start_date_ad
        ).aggregate(
            prior_dr=Sum('debit_amount'),
            prior_cr=Sum('credit_amount')
        )

        prior_dr = prior_agg['prior_dr'] or Decimal('0.00')
        prior_cr = prior_agg['prior_cr'] or Decimal('0.00')

        if is_debtor:
            # Customer / Debtor: Normal Debit Nature (Dr increases debt, Cr decreases debt)
            opening_balance = (prior_dr - prior_cr).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            opening_nature = 'Dr' if opening_balance >= Decimal('0.00') else 'Cr'
        else:
            # Supplier / Creditor: Normal Credit Nature (Cr increases payable, Dr decreases payable)
            opening_balance = (prior_cr - prior_dr).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            opening_nature = 'Cr' if opening_balance >= Decimal('0.00') else 'Dr'

        # 4. Period Transactions (start_date_ad <= date <= end_date_ad)
        period_items = scoped_items.filter(
            journal_entry__entry_date__gte=start_date_ad,
            journal_entry__entry_date__lte=end_date_ad
        ).order_by('journal_entry__entry_date', 'journal_entry__id', 'id')

        # 5. Sequential Running Balance Calculation Loop
        ledger_lines: List[Dict[str, Any]] = []
        running_bal = opening_balance
        total_period_debit = Decimal('0.00')
        total_period_credit = Decimal('0.00')

        for itm in period_items:
            dr = itm.debit_amount or Decimal('0.00')
            cr = itm.credit_amount or Decimal('0.00')
            total_period_debit += dr
            total_period_credit += cr

            if is_debtor:
                running_bal = running_bal + dr - cr
                line_nature = 'Dr' if running_bal >= Decimal('0.00') else 'Cr'
            else:
                running_bal = running_bal + cr - dr
                line_nature = 'Cr' if running_bal >= Decimal('0.00') else 'Dr'

            narration_text = itm.line_narration or itm.journal_entry.narration or "-"

            ledger_lines.append({
                'id': itm.id,
                'date_ad': itm.journal_entry.entry_date,
                'date_bs': itm.journal_entry.entry_date_bs or "-",
                'voucher_no': itm.journal_entry.voucher_number,
                'voucher_type': itm.journal_entry.get_voucher_type_display(),
                'voucher_type_raw': itm.journal_entry.voucher_type,
                'reference': itm.journal_entry.reference_document or "-",
                'account_name': itm.account.name,
                'account_code': itm.account.code,
                'narration': narration_text,
                'debit': dr,
                'credit': cr,
                'running_balance': abs(running_bal).quantize(Decimal('0.01')),
                'running_balance_signed': running_bal.quantize(Decimal('0.01')),
                'nature': line_nature,
                'branch_code': itm.journal_entry.branch.code if itm.journal_entry.branch else "HQ"
            })

        closing_balance = running_bal.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        if is_debtor:
            closing_nature = 'Dr' if closing_balance >= Decimal('0.00') else 'Cr'
            status_text = "Receivable (Due to Shop)" if closing_balance >= Decimal('0.00') else "Advance from Customer"
        else:
            closing_nature = 'Cr' if closing_balance >= Decimal('0.00') else 'Dr'
            status_text = "Payable (Owed by Shop)" if closing_balance >= Decimal('0.00') else "Advance to Supplier"

        closing_balance_abs = abs(closing_balance)
        amount_words = number_to_words_nepali_format(closing_balance_abs)

        return {
            'party_type': party_type_norm,
            'party_id': party.id,
            'party_object': party,
            'party_name': party_name,
            'party_code': party_code,
            'party_phone': party_phone,
            'party_pan': party_pan,
            'party_address': party_address,
            'is_debtor': is_debtor,
            'fiscal_year': fiscal_year,
            'start_date_ad': start_date_ad,
            'end_date_ad': end_date_ad,
            'start_date_bs': start_date_bs,
            'end_date_bs': end_date_bs,
            'opening_balance': abs(opening_balance).quantize(Decimal('0.01')),
            'opening_balance_signed': opening_balance,
            'opening_nature': opening_nature,
            'total_debit': total_period_debit.quantize(Decimal('0.01')),
            'total_credit': total_period_credit.quantize(Decimal('0.01')),
            'closing_balance': closing_balance_abs,
            'closing_balance_signed': closing_balance,
            'closing_nature': closing_nature,
            'closing_status_text': status_text,
            'closing_amount_words': amount_words,
            'ledger_lines': ledger_lines,
            'lines_count': len(ledger_lines),
            'available_fiscal_years': cls.get_available_fiscal_years(),
            'statement_generated_at': timezone.now()
        }