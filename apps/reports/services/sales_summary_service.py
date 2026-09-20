"""
Sales Summary Report (Date-Wise & Period Aggregation) Service.

Capabilities:
1. Multi-Calendar Date & Fiscal Year Engine:
   - Supports filtering by official Nepali Fiscal Year (आर्थिक वर्ष, e.g. '2080/81', '2081/82').
   - Resolves Shrawan 1 through the exact dynamic last day of Ashadh (31 or 32 days).
   - Directly accepts Nepali BS dates ('YYYY-MM-DD' or 'YYYY/MM/DD'), converting them to Gregorian AD
     behind the scenes for accurate database indexing.
2. Reconciles sales volume, billing, concessions, taxes, and margins without listing raw invoices.
3. Joins SalesPaymentTransaction to break down daily collections into:
   Cash, FonePay QR, eSewa, Khalti, Card, Bank Transfer, and Customer Credit (Udhaari).
4. Synchronizes dual-calendar date fields: Gregorian AD and Nepali Bikram Sambat (BS).
"""

import re
from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime
from typing import Dict, Any, List, Tuple, Optional

from django.db.models import (
    Q, Sum, Count, F, DecimalField, Value, Case, When
)
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.sales.models import SalesEstimate, SalesEstimateItem, SalesPaymentTransaction
from apps.branches.models import Branch
from apps.core.nepali_calendar import NepaliCalendar
from apps.core.utils.nepali_date_converter import ad_to_bs_string


class SalesSummaryService:
    """
    Business logic engine for Report 1: Sales Summary Report (Date-Wise Rollup).
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
        2. Direct Nepali BS dates (e.g. '2080-04-01' or '2080/04/01').
        3. Gregorian AD dates (e.g. '2023-07-17').
        4. Default fallback: 1st day of the ongoing BS month through today.

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
    def get_sales_summary_data(
        cls,
        filters: Dict[str, Any],
        user=None
    ) -> Dict[str, Any]:
        """
        Calculates daily rollups of turnover, discounts, payment tenders, COGS, and profits.
        """
        active_branch = getattr(user, 'assigned_branch', None) if user and not user.is_superuser else None

        start_date, end_date, start_date_bs, end_date_bs, fiscal_year = cls.resolve_date_range(filters)
        branch_id = filters.get('branch_id') or filters.get('branch', '')
        payment_status = str(filters.get('payment_status', '') or '').strip()

        # 1. Base Query: Only completed and partially returned sales estimates
        estimates_qs = SalesEstimate.objects.filter(
            status__in=['COMPLETED', 'PARTIALLY_RETURNED'],
            bill_date_ad__gte=start_date,
            bill_date_ad__lte=end_date
        )

        # 2. Branch Scoping
        if branch_id and str(branch_id).strip() not in ['', 'all', 'None']:
            estimates_qs = estimates_qs.filter(branch_id=branch_id)
        elif active_branch and not (user.is_superuser or getattr(user, 'role', '') == 'OWNER'):
            estimates_qs = estimates_qs.filter(branch=active_branch)

        if payment_status and payment_status not in ['', 'all']:
            estimates_qs = estimates_qs.filter(payment_status=payment_status)

        # 3. Aggregate By Date (bill_date_ad)
        daily_estimates = estimates_qs.values('bill_date_ad').annotate(
            invoice_count=Count('id'),
            gross_subtotal=Coalesce(
                Sum('subtotal'),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
            item_discount_sum=Coalesce(
                Sum('item_discount_total'),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
            bill_discount_sum=Coalesce(
                Sum('bill_discount_amount'),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
            trade_in_credit_sum=Coalesce(
                Sum('trade_in_discount_amount'),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
            taxable_sum=Coalesce(
                Sum('taxable_amount'),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
            non_taxable_sum=Coalesce(
                Sum('non_taxable_amount'),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
            vat_sum=Coalesce(
                Sum('vat_amount'),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
            grand_total_sum=Coalesce(
                Sum('grand_total'),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
            cogs_sum=Coalesce(
                Sum('total_cost_amount'),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
            profit_sum=Coalesce(
                Sum('total_gross_profit'),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
            paid_sum=Coalesce(
                Sum('paid_amount'),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
            due_sum=Coalesce(
                Sum('due_amount'),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
        ).order_by('-bill_date_ad')

        # 4. Aggregate Quantity of Units Sold By Date from SalesEstimateItem
        items_by_date = SalesEstimateItem.objects.filter(
            estimate__in=estimates_qs
        ).values('estimate__bill_date_ad').annotate(
            total_units=Coalesce(
                Sum('quantity'),
                Value(Decimal('0.000'), output_field=DecimalField(max_digits=18, decimal_places=3))
            )
        )
        units_map = {row['estimate__bill_date_ad']: row['total_units'] for row in items_by_date}

        # 5. Aggregate Payment Mode Transactions By Date
        payments_by_date = SalesPaymentTransaction.objects.filter(
            estimate__in=estimates_qs
        ).values('estimate__bill_date_ad').annotate(
            cash_sum=Coalesce(
                Sum(Case(When(payment_mode='CASH', then=F('amount'))), default=Value(Decimal('0.00'))),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
            fonepay_sum=Coalesce(
                Sum(Case(When(payment_mode='FONEPAY', then=F('amount'))), default=Value(Decimal('0.00'))),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
            esewa_sum=Coalesce(
                Sum(Case(When(payment_mode='ESEWA', then=F('amount'))), default=Value(Decimal('0.00'))),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
            khalti_sum=Coalesce(
                Sum(Case(When(payment_mode='KHALTI', then=F('amount'))), default=Value(Decimal('0.00'))),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
            card_sum=Coalesce(
                Sum(Case(When(payment_mode='CARD', then=F('amount'))), default=Value(Decimal('0.00'))),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
            bank_sum=Coalesce(
                Sum(Case(When(payment_mode='BANK_TRANSFER', then=F('amount'))), default=Value(Decimal('0.00'))),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
            credit_sum=Coalesce(
                Sum(Case(When(payment_mode='CREDIT', then=F('amount'))), default=Value(Decimal('0.00'))),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            )
        )
        pay_map = {row['estimate__bill_date_ad']: row for row in payments_by_date}

        # 6. Build Daily Records & Compute Totals
        records: List[Dict[str, Any]] = []

        total_invoices = 0
        total_units = Decimal('0.000')
        total_gross = Decimal('0.00')
        total_item_disc = Decimal('0.00')
        total_bill_disc = Decimal('0.00')
        total_trade_in = Decimal('0.00')
        total_sales_disc = Decimal('0.00')
        total_taxable = Decimal('0.00')
        total_non_taxable = Decimal('0.00')
        total_vat = Decimal('0.00')
        total_net_turnover = Decimal('0.00')
        total_cogs = Decimal('0.00')
        total_profit = Decimal('0.00')
        total_paid = Decimal('0.00')
        total_due = Decimal('0.00')

        total_cash = Decimal('0.00')
        total_fonepay = Decimal('0.00')
        total_esewa = Decimal('0.00')
        total_khalti = Decimal('0.00')
        total_card = Decimal('0.00')
        total_bank = Decimal('0.00')
        total_credit = Decimal('0.00')

        for d in daily_estimates:
            day_ad = d['bill_date_ad']
            day_bs = ad_to_bs_string(day_ad, lang='en')

            inv_cnt = d['invoice_count']
            u_sold = units_map.get(day_ad, Decimal('0.000'))
            gross = d['gross_subtotal']
            it_disc = d['item_discount_sum']
            b_disc = d['bill_discount_sum']
            s_disc = it_disc + b_disc
            tr_credit = d['trade_in_credit_sum']
            taxable = d['taxable_sum']
            non_taxable = d['non_taxable_sum']
            vat = d['vat_sum']
            grand = d['grand_total_sum']
            cogs = d['cogs_sum']
            profit = d['profit_sum']
            paid = d['paid_sum']
            due = d['due_sum']

            net_revenue = max(Decimal('0.00'), grand - vat + tr_credit)
            margin_pct = (
                ((profit / net_revenue) * Decimal('100.00')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
                if net_revenue > Decimal('0.00') else Decimal('0.0')
            )

            p_data = pay_map.get(day_ad, {})
            c_cash = p_data.get('cash_sum', Decimal('0.00'))
            c_fonepay = p_data.get('fonepay_sum', Decimal('0.00'))
            c_esewa = p_data.get('esewa_sum', Decimal('0.00'))
            c_khalti = p_data.get('khalti_sum', Decimal('0.00'))
            c_card = p_data.get('card_sum', Decimal('0.00'))
            c_bank = p_data.get('bank_sum', Decimal('0.00'))
            c_credit = p_data.get('credit_sum', Decimal('0.00'))

            total_invoices += inv_cnt
            total_units += u_sold
            total_gross += gross
            total_item_disc += it_disc
            total_bill_disc += b_disc
            total_sales_disc += s_disc
            total_trade_in += tr_credit
            total_taxable += taxable
            total_non_taxable += non_taxable
            total_vat += vat
            total_net_turnover += grand
            total_cogs += cogs
            total_profit += profit
            total_paid += paid
            total_due += due

            total_cash += c_cash
            total_fonepay += c_fonepay
            total_esewa += c_esewa
            total_khalti += c_khalti
            total_card += c_card
            total_bank += c_bank
            total_credit += c_credit

            records.append({
                'date_ad': day_ad,
                'date_ad_str': day_ad.strftime('%Y-%m-%d'),
                'date_bs': day_bs,
                'invoice_count': inv_cnt,
                'units_sold': u_sold,
                'gross_subtotal': gross,
                'item_discount_sum': it_disc,
                'bill_discount_sum': b_disc,
                'total_discount_sum': s_disc,
                'trade_in_credit_sum': tr_credit,
                'taxable_amount': taxable,
                'non_taxable_amount': non_taxable,
                'vat_amount': vat,
                'grand_total': grand,
                'cogs_amount': cogs,
                'gross_profit': profit,
                'margin_percent': margin_pct,
                'paid_amount': paid,
                'due_amount': due,
                'cash_collected': c_cash,
                'fonepay_collected': c_fonepay,
                'esewa_collected': c_esewa,
                'khalti_collected': c_khalti,
                'card_collected': c_card,
                'bank_collected': c_bank,
                'credit_authorized': c_credit,
            })

        overall_net_rev = max(Decimal('0.00'), total_net_turnover - total_vat + total_trade_in)
        overall_margin_pct = (
            ((total_profit / overall_net_rev) * Decimal('100.00')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
            if overall_net_rev > Decimal('0.00') else Decimal('0.0')
        )

        totals = {
            'days_count': len(records),
            'total_invoices': total_invoices,
            'total_units_sold': total_units,
            'total_gross': total_gross,
            'total_item_disc': total_item_disc,
            'total_bill_disc': total_bill_disc,
            'total_sales_disc': total_sales_disc,
            'total_trade_in': total_trade_in,
            'total_taxable': total_taxable,
            'total_non_taxable': total_non_taxable,
            'total_vat': total_vat,
            'total_net_turnover': total_net_turnover,
            'total_cogs': total_cogs,
            'total_profit': total_profit,
            'overall_margin_pct': overall_margin_pct,
            'total_paid': total_paid,
            'total_due': total_due,
            'total_cash': total_cash,
            'total_fonepay': total_fonepay,
            'total_esewa': total_esewa,
            'total_khalti': total_khalti,
            'total_card': total_card,
            'total_bank': total_bank,
            'total_credit': total_credit,
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
            'selected_branch': selected_branch,
            'filters': filters,
            'start_date': start_date.strftime('%Y-%m-%d'),
            'end_date': end_date.strftime('%Y-%m-%d'),
            'start_date_bs': start_date_bs,
            'end_date_bs': end_date_bs,
            'fiscal_year': fiscal_year,
            'available_fiscal_years': cls.get_available_fiscal_years(),
        }