"""
Supplier Outstanding (Udhaari) & Payables Aging Report Service.

Capabilities:
1. Multi-Calendar Date & Fiscal Year Engine:
   - Supports filtering by official Nepali Fiscal Year (आर्थिक वर्ष, e.g. '2080/81', '2081/82').
   - Resolves Shrawan 1 through the exact dynamic last day of Ashadh (accounting for 30, 31, or 32 days).
   - Accurately converts Nepali BS date inputs to Gregorian AD dates for database querying.
2. Historical Udhaari Balance Reconstruction:
   - Accurately computes what was owed to distributors at the start of the fiscal year (e.g. start of FY 2080/81).
   - Reconciles period inward purchases, payments made, and debit note deductions.
   - Calculates the exact debt carried over into the next fiscal year (e.g. carryover into FY 2081/82).
3. Classifies supplier credit status: Overdue (> Credit Period Days), Within Terms, Clear, or In Advance.
4. Provides banking account numbers, ConnectIPS details, and FonePay QR parameters for rapid bill settlement.
"""

import re
from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime
from typing import Dict, Any, List, Optional, Tuple

from django.db.models import Q, Sum, Count, F, DecimalField, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.purchases.models import Supplier, SupplierUdhaariLedger, GoodsReceivedNote, PurchaseReturn
from apps.branches.models import Branch
from apps.core.nepali_calendar import NepaliCalendar
from apps.core.utils.nepali_date_converter import ad_to_bs_string


class SupplierOutstandingService:
    """
    Business logic engine for Report 5: Supplier Outstanding / Udhaari Report.
    """

    @classmethod
    def get_available_fiscal_years(cls) -> List[str]:
        """Returns standard list of relevant Nepali Fiscal Years for selection dropdowns."""
        today = timezone.now().date()
        current_bs_y, current_bs_m, _ = NepaliCalendar.ad_to_bs(today)
        current_fy = NepaliCalendar.get_fiscal_year(current_bs_y, current_bs_m)

        base_years = [2079, 2080, 2081, 2082, 2083]
        fys = [f"{y}/{str(y + 1)[-2:]}" for y in base_years]
        if current_fy not in fys:
            fys.append(current_fy)
        fys.sort(reverse=True)
        return fys

    @classmethod
    def _parse_flexible_date(cls, val_str: str) -> Optional[Tuple[date, str]]:
        """
        Accepts arbitrary date strings in either Gregorian AD or Nepali BS (delimited by - or /)
        and returns a clean (ad_date, bs_date_string) tuple.
        """
        if not val_str:
            return None
        clean = re.sub(r'[^\d]', '-', str(val_str).strip())
        parts = [int(p) for p in clean.split('-') if p]
        if len(parts) != 3:
            return None

        # Format: YYYY-MM-DD in BS (e.g. 2080-04-01)
        if 2000 <= parts[0] <= 2095:
            bs_y, bs_m, bs_d = parts[0], parts[1], parts[2]
            bs_m = max(1, min(12, bs_m))
            max_days = NepaliCalendar.get_days_in_month(bs_y, bs_m)
            bs_d = max(1, min(max_days, bs_d))
            ad_date = NepaliCalendar.bs_to_ad(bs_y, bs_m, bs_d)
            bs_str = f"{bs_y:04d}-{bs_m:02d}-{bs_d:02d}"
            return ad_date, bs_str

        # Format: DD-MM-YYYY in BS (e.g. 01-04-2080)
        elif 2000 <= parts[2] <= 2095:
            bs_y, bs_m, bs_d = parts[2], parts[1], parts[0]
            bs_m = max(1, min(12, bs_m))
            max_days = NepaliCalendar.get_days_in_month(bs_y, bs_m)
            bs_d = max(1, min(max_days, bs_d))
            ad_date = NepaliCalendar.bs_to_ad(bs_y, bs_m, bs_d)
            bs_str = f"{bs_y:04d}-{bs_m:02d}-{bs_d:02d}"
            return ad_date, bs_str

        # Format: YYYY-MM-DD in AD (e.g. 2023-07-17)
        elif 1970 <= parts[0] <= 2050:
            try:
                ad_date = date(parts[0], parts[1], parts[2])
                y, m, d = NepaliCalendar.ad_to_bs(ad_date)
                bs_str = NepaliCalendar.format_bs(y, m, d, lang='en')
                return ad_date, bs_str
            except (ValueError, TypeError):
                return None

        return None

    @classmethod
    def resolve_date_range(cls, raw_params: Dict[str, Any]) -> Tuple[date, date, str, str, str]:
        """
        Resolves query dates and Nepali Fiscal Year with multi-format support:
        1. Fiscal Year parameter (e.g. '2080/81', '2081/82', '2080-81').
           Accurately resolves Shrawan 1 to Ashadh 31/32 (variable month length).
        2. Direct Nepali BS dates ('YYYY-MM-DD').
        3. Gregorian AD dates ('YYYY-MM-DD').
        4. Default fallback: 1st of the current BS month up to today.

        Returns:
            Tuple[date, date, str, str, str]:
            (start_date_ad, end_date_ad, start_date_bs, end_date_bs, fiscal_year)
        """
        today_ad = timezone.now().date()
        today_bs_y, today_bs_m, today_bs_d = NepaliCalendar.ad_to_bs(today_ad)

        fy_param = str(raw_params.get('fiscal_year') or raw_params.get('fy') or '').strip()
        start_param = str(raw_params.get('start_date') or raw_params.get('start_date_bs') or '').strip()
        end_param = str(raw_params.get('end_date') or raw_params.get('end_date_bs') or '').strip()

        # Case 1: Fiscal Year preset selected without explicit overriding date range
        if fy_param and fy_param.lower() not in ['all', 'none', '']:
            clean_fy = fy_param.replace('-', '/').strip()
            if not start_param and not end_param:
                try:
                    start_ad, end_ad, start_bs, end_bs = NepaliCalendar.get_fiscal_year_range(clean_fy)
                    parts = clean_fy.split('/')
                    norm_fy = f"{int(parts[0])}/{str(int(parts[0]) + 1)[-2:]}"
                    return start_ad, end_ad, start_bs, end_bs, norm_fy
                except Exception:
                    pass

        # Case 2: Parse custom start and end date inputs (supporting both AD and BS formats)
        start_date = None
        end_date = None
        start_date_bs = ""
        end_date_bs = ""

        if start_param:
            parsed_start = cls._parse_flexible_date(start_param)
            if parsed_start:
                start_date, start_date_bs = parsed_start

        if end_param:
            parsed_end = cls._parse_flexible_date(end_param)
            if parsed_end:
                end_date, end_date_bs = parsed_end

        # Case 3: If fiscal_year was chosen along with custom dates within it
        if fy_param and fy_param.lower() not in ['all', 'none', '']:
            clean_fy = fy_param.replace('-', '/').strip()
            try:
                fy_start_ad, fy_end_ad, fy_start_bs, fy_end_bs = NepaliCalendar.get_fiscal_year_range(clean_fy)
                parts = clean_fy.split('/')
                resolved_fy = f"{int(parts[0])}/{str(int(parts[0]) + 1)[-2:]}"
                start_date = start_date or fy_start_ad
                end_date = end_date or fy_end_ad
                start_date_bs = start_date_bs or fy_start_bs
                end_date_bs = end_date_bs or fy_end_bs
                if start_date > end_date:
                    start_date, end_date = end_date, start_date
                    start_date_bs, end_date_bs = end_date_bs, start_date_bs
                return start_date, end_date, start_date_bs, end_date_bs, resolved_fy
            except Exception:
                pass

        # Case 4: Fallback to current ongoing BS month up to today
        if not start_date or not end_date:
            start_of_bs_month = NepaliCalendar.bs_to_ad(today_bs_y, today_bs_m, 1)
            start_date = start_date or start_of_bs_month
            end_date = end_date or today_ad

        if start_date > end_date:
            start_date, end_date = end_date, start_date

        start_y, start_m, start_d = NepaliCalendar.ad_to_bs(start_date)
        end_y, end_m, end_d = NepaliCalendar.ad_to_bs(end_date)

        start_date_bs = NepaliCalendar.format_bs(start_y, start_m, start_d, lang='en')
        end_date_bs = NepaliCalendar.format_bs(end_y, end_m, end_d, lang='en')
        resolved_fy = NepaliCalendar.get_fiscal_year(start_y, start_m)

        return start_date, end_date, start_date_bs, end_date_bs, resolved_fy

    @classmethod
    def get_supplier_outstanding_data(
        cls,
        filters: Dict[str, Any],
        user=None
    ) -> Dict[str, Any]:
        """
        Compiles supplier accounts payable with opening balances at the start of the fiscal period,
        period transactions, carryover balance into the subsequent fiscal year, and overdue indicators.
        """
        active_branch = getattr(user, 'assigned_branch', None) if user and not user.is_superuser else None

        start_date, end_date, start_date_bs, end_date_bs, fiscal_year = cls.resolve_date_range(filters)
        branch_id = filters.get('branch_id') or filters.get('branch', '')
        supplier_type = str(filters.get('supplier_type', '') or '').strip()
        status_filter = str(filters.get('status', 'due') or 'due').strip().lower()
        search_query = str(filters.get('q', '') or '').strip()

        today = timezone.now().date()

        # 1. Base Supplier Query
        supplier_qs = Supplier.objects.filter(is_active=True)

        if supplier_type and supplier_type not in ['', 'all']:
            supplier_qs = supplier_qs.filter(supplier_type=supplier_type)

        if search_query:
            supplier_qs = supplier_qs.filter(
                Q(code__icontains=search_query) |
                Q(company_name__icontains=search_query) |
                Q(contact_person__icontains=search_query) |
                Q(phone_number__icontains=search_query) |
                Q(pan_number__icontains=search_query) |
                Q(bank_account_number__icontains=search_query)
            )

        # 2. Query In-Period Ledger Movements (start_date <= date <= end_date)
        period_ledger_qs = SupplierUdhaariLedger.objects.filter(
            created_at__date__gte=start_date,
            created_at__date__lte=end_date
        )

        # Query Ledger Movements After end_date (needed for historical period reconstruction)
        after_ledger_qs = SupplierUdhaariLedger.objects.filter(
            created_at__date__gt=end_date
        )

        if branch_id and str(branch_id).strip() not in ['', 'all', 'None']:
            period_ledger_qs = period_ledger_qs.filter(branch_id=branch_id)
            after_ledger_qs = after_ledger_qs.filter(branch_id=branch_id)
        elif active_branch and not (user.is_superuser or getattr(user, 'role', '') == 'OWNER'):
            period_ledger_qs = period_ledger_qs.filter(branch=active_branch)
            after_ledger_qs = after_ledger_qs.filter(branch=active_branch)

        # Aggregate Period Purchases, Payments, and Returns
        period_stats = period_ledger_qs.values('supplier_id').annotate(
            total_purchases=Coalesce(
                Sum('amount', filter=Q(transaction_type='PURCHASE_BILL')),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
            total_payments=Coalesce(
                Sum('amount', filter=Q(transaction_type='PAYMENT')),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
            total_returns=Coalesce(
                Sum('amount', filter=Q(transaction_type='PURCHASE_RETURN')),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
            total_adjustments=Coalesce(
                Sum('amount', filter=Q(transaction_type='ADJUSTMENT')),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            )
        )
        period_map = {row['supplier_id']: row for row in period_stats}

        # Aggregate Movements that occurred AFTER end_date up to today
        after_stats = after_ledger_qs.values('supplier_id').annotate(
            after_purchases=Coalesce(
                Sum('amount', filter=Q(transaction_type='PURCHASE_BILL')),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
            after_payments=Coalesce(
                Sum('amount', filter=Q(transaction_type='PAYMENT')),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
            after_returns=Coalesce(
                Sum('amount', filter=Q(transaction_type='PURCHASE_RETURN')),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            )
        )
        after_map = {row['supplier_id']: row for row in after_stats}

        # 3. Build Detailed Payables Records with Historical Opening & Carryover
        records: List[Dict[str, Any]] = []
        total_payable_debt = Decimal('0.00')
        total_advance_paid = Decimal('0.00')
        overdue_debt_amount = Decimal('0.00')
        overdue_vendors_count = 0

        for sup in supplier_qs.order_by('-current_balance', 'company_name'):
            curr_live_bal = sup.current_balance or Decimal('0.00')
            credit_limit = sup.credit_limit or Decimal('0.00')
            credit_days = sup.credit_period_days or 30

            # Movements in the period
            p_data = period_map.get(sup.id, {})
            period_purchases = p_data.get('total_purchases', Decimal('0.00'))
            period_payments = p_data.get('total_payments', Decimal('0.00'))
            period_returns = p_data.get('total_returns', Decimal('0.00'))
            period_adjustments = p_data.get('total_adjustments', Decimal('0.00'))

            net_movement_in_period = period_purchases - period_payments - period_returns

            # Movements strictly after end_date up to now
            a_data = after_map.get(sup.id, {})
            after_purchases = a_data.get('after_purchases', Decimal('0.00'))
            after_payments = a_data.get('after_payments', Decimal('0.00'))
            after_returns = a_data.get('after_returns', Decimal('0.00'))
            net_movement_after_period = after_purchases - after_payments - after_returns

            # Historical Balance Reconstruction:
            # 1. Closing balance at end of period (what carried over into next fiscal year):
            period_closing_balance = curr_live_bal - net_movement_after_period

            # 2. Opening balance at start of period (what was owed at the start of this fiscal year):
            period_opening_balance = period_closing_balance - net_movement_in_period

            # Filter by Balance Status based on period_closing_balance
            if status_filter == 'due' and period_closing_balance <= Decimal('0.00'):
                continue
            elif status_filter == 'advance' and period_closing_balance >= Decimal('0.00'):
                continue
            elif status_filter == 'clear' and period_closing_balance != Decimal('0.00'):
                continue

            # Overdue Calculation based on last purchase date and credit terms
            is_overdue = False
            overdue_days = 0
            if period_closing_balance > Decimal('0.00') and sup.last_purchase_date:
                days_since_purchase = (today - sup.last_purchase_date).days
                if days_since_purchase > credit_days:
                    is_overdue = True
                    overdue_days = days_since_purchase - credit_days
                    overdue_debt_amount += period_closing_balance
                    overdue_vendors_count += 1

            is_over_limit = (credit_limit > Decimal('0.00') and period_closing_balance > credit_limit)

            if period_closing_balance > Decimal('0.00'):
                total_payable_debt += period_closing_balance
            elif period_closing_balance < Decimal('0.00'):
                total_advance_paid += abs(period_closing_balance)

            last_purch_ad = sup.last_purchase_date
            last_purch_bs = ad_to_bs_string(last_purch_ad, lang='en') if last_purch_ad else "-"

            last_pay_ad = sup.last_payment_date
            last_pay_bs = ad_to_bs_string(last_pay_ad, lang='en') if last_pay_ad else "-"

            records.append({
                'supplier_id': sup.id,
                'supplier_code': sup.code,
                'company_name': sup.company_name,
                'contact_person': sup.contact_person,
                'phone_number': sup.phone_number,
                'pan_number': sup.pan_number or '-',
                'supplier_type': sup.get_supplier_type_display(),
                'credit_period_days': credit_days,
                'credit_limit': credit_limit,
                'is_over_limit': is_over_limit,
                'opening_balance': period_opening_balance,
                'period_purchases': period_purchases,
                'period_payments': period_payments,
                'period_returns': period_returns,
                'period_closing_balance': period_closing_balance,
                'current_balance': period_closing_balance,
                'live_balance_today': curr_live_bal,
                'is_payable': period_closing_balance > Decimal('0.00'),
                'is_advance': period_closing_balance < Decimal('0.00'),
                'is_clear': period_closing_balance == Decimal('0.00'),
                'is_overdue': is_overdue,
                'overdue_days': overdue_days,
                'last_purchase_date_ad': last_purch_ad,
                'last_purchase_date_str': last_purch_ad.strftime('%Y-%m-%d') if last_purch_ad else '-',
                'last_purchase_date_bs': last_purch_bs,
                'last_payment_date_ad': last_pay_ad,
                'last_payment_date_str': last_pay_ad.strftime('%Y-%m-%d') if last_pay_ad else '-',
                'last_payment_date_bs': last_pay_bs,
                'bank_name': sup.bank_name or '-',
                'bank_account_number': sup.bank_account_number or '-',
                'account_holder_name': sup.account_holder_name or '-',
                'bank_branch': sup.bank_branch or '-',
                'has_qr': bool(sup.qr_payment_details and sup.qr_payment_details.strip()),
            })

        totals = {
            'total_suppliers_count': len(records),
            'total_payable_debt': total_payable_debt,
            'total_advance_paid': total_advance_paid,
            'net_shop_liability': max(Decimal('0.00'), total_payable_debt - total_advance_paid),
            'overdue_vendors_count': overdue_vendors_count,
            'overdue_debt_amount': overdue_debt_amount,
        }

        selected_branch = None
        if branch_id and branch_id != 'all':
            selected_branch = Branch.objects.filter(id=branch_id).first()
        elif active_branch:
            selected_branch = active_branch

        return {
            'records': records,
            'totals': totals,
            'branches': Branch.objects.filter(is_active=True).order_by('-is_main_branch', 'name'),
            'supplier_types': Supplier.SUPPLIER_TYPE_CHOICES,
            'selected_branch': selected_branch,
            'filters': filters,
            'start_date': start_date.strftime('%Y-%m-%d'),
            'end_date': end_date.strftime('%Y-%m-%d'),
            'start_date_bs': start_date_bs,
            'end_date_bs': end_date_bs,
            'fiscal_year': fiscal_year,
            'available_fiscal_years': cls.get_available_fiscal_years(),
        }