"""
Party-Wise Confirmation & Sub-Ledger Accounting Engine.

Capabilities & Architectural Rules:
1. Standard Double-Entry Subledger Running Balance Rules:
   - For Suppliers (Creditors / Accounts Payable):
     * Credit transactions (inward purchases, invoice adjustments) increase the payable balance (+).
     * Debit transactions (cash payments, digital payouts, debit notes / purchase returns) decrease the payable balance (-).
     * Net positive balance represents "Payable to Party" (Credit nature).
     * Net negative balance represents "Advance to Supplier" (Debit nature).
   - For Customers (Debtors / Accounts Receivable):
     * Debit transactions (credit sales invoices, POS estimates) increase the receivable balance (+).
     * Credit transactions (cash repayments, QR collections, credit notes / sales returns) decrease the receivable balance (-).
     * Net positive balance represents "Receivable from Party" (Debit nature).
     * Net negative balance represents "Advance from Customer" (Credit nature).
2. Dynamic Historical Opening Balance Calculator:
   - Evaluates all posted double-entry journal items strictly prior to start_date_ad.
   - Accurately includes opening migration entries (e.g. from 2080 B.S.) and preceding fiscal periods.
   - Computes dynamic "Balance Brought Forward (B/F)" with correct Dr/Cr nature tagging.
3. Multi-Calendar Date & Fiscal Year Normalization:
   - Parses arbitrary Bikram Sambat (BS) date strings (YYYY-MM-DD, YYYY.MM.DD, YYYY/MM/DD) and Devanagari numerals.
   - Converts BS dates into Gregorian (AD) dates for database indexing without altering the displayed BS dates.
   - Resolves official Nepali Fiscal Years (e.g. '2080/81', '2081/82', '2082/83', '2083/84') from Shrawan 1 to Ashadh 31/32.
   - Prevents silent date jumps to today when querying earlier months or historical years.
4. Robust Nepali/South Asian Number-to-Words Converter:
   - Formats amounts into standard words (Crores, Lakhs, Thousands, Hundreds, Rupees, and Paisa).
   - Handles zero balances ("Zero Rupees Only"), negative/credit offsets, and decimal paisa fractions accurately.
5. Anti-Double Counting Subledger Query Filter:
   - Scopes queries strictly to control accounts (ACCOUNTS_RECEIVABLE / ACCOUNTS_PAYABLE)
     and non-clearing lines, preventing double-counting of counter cash/bank lines in multi-line vouchers.
"""

import re
import logging
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from datetime import date, datetime
from typing import Dict, Any, List, Optional, Tuple, Union

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

logger = logging.getLogger(__name__)

# =============================================================================
# 1. SOUTH ASIAN / NEPALI NUMBER-TO-WORDS CONVERSION ENGINE
# =============================================================================
def number_to_words_nepali_format(amount: Union[int, float, Decimal, None]) -> str:
    """
    Converts a numerical figure into South Asian / Nepali standard English words
    (Crores, Lakhs, Thousands, Hundreds, Rupees, and Paisa).
    
    Examples:
        293800.00 -> 'Two Lakh Ninety-Three Thousand Eight Hundred Rupees Only'
        150250.75 -> 'One Lakh Fifty Thousand Two Hundred Fifty Rupees and Seventy-Five Paisa Only'
        0.00      -> 'Zero Rupees Only'
    """
    if amount is None:
        return "Zero Rupees Only"

    try:
        amount_dec = Decimal(str(amount)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        return "Zero Rupees Only"

    is_negative = (amount_dec < Decimal('0.00'))
    abs_amount = abs(amount_dec)

    total_rupees = int(abs_amount)
    total_paisa = int(round((abs_amount - total_rupees) * 100))

    if total_rupees == 0 and total_paisa == 0:
        return "Zero Rupees Only"

    units = [
        "", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
        "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen",
        "Seventeen", "Eighteen", "Nineteen"
    ]
    tens = [
        "", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"
    ]

    def _convert_two_digits(n: int) -> str:
        if n == 0:
            return ""
        if n < 20:
            return units[n]
        ten_part = tens[n // 10]
        unit_part = units[n % 10]
        return f"{ten_part} {unit_part}".strip()

    def _convert_three_digits(n: int) -> str:
        h = n // 100
        rem = n % 100
        parts = []
        if h > 0:
            parts.append(f"{units[h]} Hundred")
        if rem > 0:
            parts.append(_convert_two_digits(rem))
        return " ".join(parts).strip()

    def _convert_integer(n: int) -> str:
        if n == 0:
            return ""
        if n < 1000:
            return _convert_three_digits(n)

        parts = []
        crore = n // 10000000
        rem = n % 10000000
        lakh = rem // 100000
        rem = rem % 100000
        thousand = rem // 1000
        remainder = rem % 1000

        if crore > 0:
            parts.append(f"{_convert_integer(crore)} Crore")
        if lakh > 0:
            parts.append(f"{_convert_two_digits(lakh)} Lakh")
        if thousand > 0:
            parts.append(f"{_convert_two_digits(thousand)} Thousand")
        if remainder > 0:
            parts.append(_convert_three_digits(remainder))

        return " ".join(parts).strip()

    rupees_str = _convert_integer(total_rupees) if total_rupees > 0 else "Zero"
    prefix = "Minus " if is_negative else ""

    if total_paisa > 0:
        paisa_str = _convert_two_digits(total_paisa)
        return f"{prefix}{rupees_str} Rupees and {paisa_str} Paisa Only"
    return f"{prefix}{rupees_str} Rupees Only"

# =============================================================================
# 2. PARTY CONFIRMATION & SUB-LEDGER CALCULATION ENGINE
# =============================================================================
class PartyLedgerService:
    """
    Authoritative double-entry subledger calculation engine for Customer & Supplier
    balance confirmations, running transaction statements, and legal audit letters.
    """

    # Internal payment clearing tags to prevent line doubling in double-entry vouchers
    PAYMENT_CLEARING_TAGS = [
        'CASH', 'BANK', 'FONEPAY', 'ESEWA', 'KHALTI',
        'CARD_CLEARING', 'SALES_REVENUE', 'COGS',
        'OUTPUT_VAT', 'INPUT_VAT', 'INVENTORY_ASSET'
    ]

    @classmethod
    def get_available_fiscal_years(cls) -> List[str]:
        """Returns ordered list of official Nepali Fiscal Years (e.g. '2083/84', '2082/83')."""
        fys = list(
            AccountingFiscalYear.objects.order_by('-start_date_ad').values_list('name', flat=True)
        )
        if not fys:
            fys = ['2083/84', '2082/83', '2081/82', '2080/81']
        return fys

    @classmethod
    def _parse_date_input(cls, raw_val: Any) -> Optional[Tuple[date, str]]:
        """
        Parses arbitrary date strings in either Gregorian AD or Nepali BS
        (YYYY-MM-DD, YYYY.MM.DD, YYYY/MM/DD, or Devanagari digits).
        Returns a clean (gregorian_ad_date, standardized_bs_date_string) tuple or None.
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
        if not raw_str or raw_str.lower() in ['none', 'nan', 'null', '-', '--', '']:
            return None

        # 1. Attempt Bikram Sambat (BS) date parsing
        try:
            bs_y, bs_m, bs_d = parse_bs_date_components(raw_str)
            ad_d = NepaliCalendar.bs_to_ad(bs_y, bs_m, bs_d)
            bs_str = f"{bs_y:04d}-{bs_m:02d}-{bs_d:02d}"
            return ad_d, bs_str
        except Exception:
            pass

        # 2. Attempt Gregorian (AD) ISO date parsing (YYYY-MM-DD)
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

        Strict Date Boundary Rules:
        - When custom dates are supplied within an earlier month or historical period,
          the end_date strictly respects that requested period and NEVER silently defaults to today.
        - If a fiscal year preset is chosen, the entire range of that fiscal year is returned.
        - If no parameters are given, defaults to the ongoing active fiscal year.
        """
        fy_param = str(params.get('fiscal_year') or '').strip()
        start_param = str(params.get('start_date') or params.get('start_date_bs') or '').strip()
        end_param = str(params.get('end_date') or params.get('end_date_bs') or '').strip()

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

        # Case 2: Only Start Date is Provided -> Scope to the end of that specific B.S. Month
        if parsed_start and not parsed_end:
            start_ad, start_bs = parsed_start
            bs_y, bs_m, bs_d = parse_bs_date_components(start_bs)
            resolved_fy = NepaliCalendar.get_fiscal_year(bs_y, bs_m)

            max_days = NepaliCalendar.get_days_in_month(bs_y, bs_m)
            end_ad = NepaliCalendar.bs_to_ad(bs_y, bs_m, max_days)
            end_bs = f"{bs_y:04d}-{bs_m:02d}-{max_days:02d}"

            return start_ad, end_ad, start_bs, end_bs, resolved_fy

        # Case 3: Only End Date is Provided -> Scope from the 1st of that B.S. Month to End Date
        if parsed_end and not parsed_start:
            end_ad, end_bs = parsed_end
            bs_y, bs_m, bs_d = parse_bs_date_components(end_bs)
            resolved_fy = NepaliCalendar.get_fiscal_year(bs_y, bs_m)

            start_ad = NepaliCalendar.bs_to_ad(bs_y, bs_m, 1)
            start_bs = f"{bs_y:04d}-{bs_m:02d}-01"

            return start_ad, end_ad, start_bs, end_bs, resolved_fy

        # Case 4: Fiscal Year Preset Selected (e.g. '2083/84')
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
        Handles both historical migrated data and new fiscal year periods seamlessly.

        Accounting Equation Enforced:
        - Opening Balance (as of start_date_ad) = Net sum of all transactions prior to start_date_ad.
        - For Debtors (Customers): Running Balance = Opening (Dr) + Debits - Credits.
        - For Creditors (Suppliers): Running Balance = Opening (Cr) + Credits - Debits.
        - Closing Balance = Running Balance at period end.
        """
        party_type_norm = str(party_type).strip().upper()
        if party_type_norm not in ['CUSTOMER', 'SUPPLIER']:
            party_type_norm = 'CUSTOMER'

        start_date_ad, end_date_ad, start_date_bs, end_date_bs, fiscal_year = cls.resolve_period_dates(params)

        # -------------------------------------------------------------
        # 1. Resolve Party Profile Entity
        # -------------------------------------------------------------
        if party_type_norm == 'CUSTOMER':
            party = get_object_or_404(Customer, pk=party_id)
            party_name = party.name
            party_code = f"CUST-{party.id:04d}"
            party_phone = party.phone_number or "-"
            party_pan = party.pan_number or "-"
            party_address = party.address or "Kathmandu, Nepal"
            is_debtor = True
            control_tag = 'ACCOUNTS_RECEIVABLE'
            control_code_prefix = '1210'
        else:
            party = get_object_or_404(Supplier, pk=party_id)
            party_name = party.company_name
            party_code = party.code or f"SUP-{party.id:04d}"
            party_phone = party.phone_number or "-"
            party_pan = party.pan_number or "-"
            party_address = party.address or "Kathmandu, Nepal"
            is_debtor = False
            control_tag = 'ACCOUNTS_PAYABLE'
            control_code_prefix = '2110'

        # -------------------------------------------------------------
        # 2. Base QuerySet Scoped Strictly to Subledger Transactions
        # -------------------------------------------------------------
        base_items = JournalItem.objects.filter(
            journal_entry__status='POSTED'
        ).select_related('journal_entry', 'account', 'journal_entry__branch')

        if is_debtor:
            party_filter = Q(customer=party)
        else:
            party_filter = Q(supplier=party)

        # Scope strictly to the party control ledger line (prevents doubling against counter cash lines)
        control_account_filter = (
            Q(account__system_tag=control_tag) |
            Q(account__code__startswith=control_code_prefix) |
            ~Q(account__system_tag__in=cls.PAYMENT_CLEARING_TAGS)
        )

        scoped_items = base_items.filter(party_filter).filter(control_account_filter)

        if branch:
            scoped_items = scoped_items.filter(journal_entry__branch=branch)

        # -------------------------------------------------------------
        # 3. Dynamic Opening Balance Calculation (Prior to start_date_ad)
        # -------------------------------------------------------------
        prior_agg = scoped_items.filter(
            journal_entry__entry_date__lt=start_date_ad
        ).aggregate(
            prior_dr=Sum('debit_amount'),
            prior_cr=Sum('credit_amount')
        )

        prior_dr = prior_agg['prior_dr'] or Decimal('0.00')
        prior_cr = prior_agg['prior_cr'] or Decimal('0.00')

        if is_debtor:
            # Customer (Trade Debtor / Normal Debit Nature):
            # Debits increase debt, Credits decrease debt.
            signed_opening = (prior_dr - prior_cr).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            opening_nature = 'Dr' if signed_opening >= Decimal('0.00') else 'Cr'
        else:
            # Supplier (Trade Creditor / Normal Credit Nature):
            # Credits increase payable, Debits decrease payable.
            signed_opening = (prior_cr - prior_dr).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            opening_nature = 'Cr' if signed_opening >= Decimal('0.00') else 'Dr'

        # -------------------------------------------------------------
        # 4. Period Transactions (start_date_ad <= entry_date <= end_date_ad)
        # -------------------------------------------------------------
        period_items = scoped_items.filter(
            journal_entry__entry_date__gte=start_date_ad,
            journal_entry__entry_date__lte=end_date_ad
        ).order_by('journal_entry__entry_date', 'journal_entry__id', 'id')

        # -------------------------------------------------------------
        # 5. Sequential Running Balance Calculation Loop
        # -------------------------------------------------------------
        ledger_lines: List[Dict[str, Any]] = []
        running_bal = signed_opening
        total_period_debit = Decimal('0.00')
        total_period_credit = Decimal('0.00')

        for itm in period_items:
            dr = itm.debit_amount or Decimal('0.00')
            cr = itm.credit_amount or Decimal('0.00')
            total_period_debit += dr
            total_period_credit += cr

            # Standard Subledger Running Balance Rules:
            if is_debtor:
                # Customer: +Debit (Sales), -Credit (Receipts/Returns)
                running_bal = running_bal + dr - cr
                line_nature = 'Dr' if running_bal >= Decimal('0.00') else 'Cr'
            else:
                # Supplier: +Credit (Purchases), -Debit (Payments/Returns)
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
        closing_balance_abs = abs(closing_balance)

        # Determine Closing Nature and Standard Status Wording:
        if is_debtor:
            if closing_balance > Decimal('0.00'):
                closing_nature = 'Dr'
                status_text = "Receivable from Party"
            elif closing_balance < Decimal('0.00'):
                closing_nature = 'Cr'
                status_text = "Advance from Customer"
            else:
                closing_nature = 'Dr'
                status_text = "Settled (Zero Balance)"
        else:
            if closing_balance > Decimal('0.00'):
                closing_nature = 'Cr'
                status_text = "Payable to Party"
            elif closing_balance < Decimal('0.00'):
                closing_nature = 'Dr'
                status_text = "Advance to Supplier"
            else:
                closing_nature = 'Cr'
                status_text = "Settled (Zero Balance)"

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
            'opening_balance': abs(signed_opening).quantize(Decimal('0.01')),
            'opening_balance_signed': signed_opening,
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