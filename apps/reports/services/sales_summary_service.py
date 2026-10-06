"""
Sales Summary Report (Date-Wise & Period Aggregation) Service.

Capabilities:
1. Multi-Calendar Date & Fiscal Year Engine:
   - Supports filtering by official Nepali Fiscal Year (आर्थिक वर्ष, e.g. '2080/81', '2081/82', '2083/84').
   - Resolves Shrawan 1 through the exact dynamic last day of Ashadh (31 or 32 days).
   - Directly parses and bi-directionally synchronizes Nepali BS dates (YYYY-MM-DD) and Gregorian AD dates.
2. 14-Column Ledger Date-Wise Aggregation:
   - Distinguishes Mobile Handset units (IMEI-tracked) from non-phone Accessory units.
   - Integrates Customer Sales Returns and Cash Refunds deducted by date.
   - Computes Net Daily Collections (Real Inflow = Collected - Returns).
3. 8 Executive Financial KPI Cards:
   - Total Net Turnover, Realized Gross Profit with Margin %, Amount Collected,
     New Credit (Udhaari), Total Bills, Units Sold (Phone/Acc split), Discounts Conceded,
     and Reconciled Outstanding Customer Receivables.
4. Period-Level Payment Method Rollup:
   - Comprehensive channel summary for Cash, eSewa, Khalti, FonePay QR, Cards, Bank Transfers,
     and Credit with transaction counts, amounts, and collection share percentages.
5. Accounts Receivable (Udhaari) Reconciliation Waterfall:
   - Reconstructs customer ledger debt:
     Opening Due + New Credit Sales - Old Collections Recovered - Returns/Adjustments = Closing Receivable.
6. Mobile-Specific Sales Intelligence & Top Brands:
   - Analyzes phone sales volume, revenue, Average Selling Price (ASP), New vs. Pre-Owned units,
     and Top 5 performing phone brands.
7. Trade-In (Satta/Exchange) & Returns Audit Summaries:
   - Reconciles old phone buy-back credits, cash top-ups, warranty replacements, and credit notes.
8. Executive Trend Visual Analytics:
   - Generates daily and monthly comparative sales vs. purchase curves for dashboard charts.
"""

import re
from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime, timedelta
from typing import Dict, Any, List, Tuple, Optional

from django.db.models import (
    Q, Sum, Count, F, DecimalField, Value, Case, When, ExpressionWrapper
)
from django.db.models.functions import Coalesce, TruncMonth
from django.utils import timezone

from apps.sales.models import (
    SalesEstimate, SalesEstimateItem, SalesPaymentTransaction,
    SalesReturn, SalesReturnItem, PhoneExchangeTradeIn
)
from apps.customers.models import Customer, CustomerUdhaariLedger
from apps.purchases.models import GoodsReceivedNote
from apps.branches.models import Branch
from apps.inventory.models import Product, Brand, ProductCategory
from apps.users.models import User
from apps.core.nepali_calendar import NepaliCalendar
from apps.core.utils.nepali_date_converter import ad_to_bs_string

class SalesSummaryService:
    """
    Business logic engine for Report 1: Date-Wise Sales Summary & Collection Rollup.
    """

    # =========================================================================
    # 1. DATE & FISCAL YEAR RESOLUTION
    # =========================================================================
    @classmethod
    def get_available_fiscal_years(cls) -> List[str]:
        """Returns standard list of relevant Nepali Fiscal Years for selection dropdowns."""
        today = timezone.now().date()
        current_bs_y, current_bs_m, _ = NepaliCalendar.ad_to_bs(today)
        current_fy = NepaliCalendar.get_fiscal_year(current_bs_y, current_bs_m)

        base_years = [2079, 2080, 2081, 2082, 2083, 2084]
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

        # Format: YYYY-MM-DD in BS (e.g. 2083-06-19)
        if 2000 <= parts[0] <= 2095:
            bs_y, bs_m, bs_d = parts[0], parts[1], parts[2]
            bs_m = max(1, min(12, bs_m))
            max_days = NepaliCalendar.get_days_in_month(bs_y, bs_m)
            bs_d = max(1, min(max_days, bs_d))
            ad_date = NepaliCalendar.bs_to_ad(bs_y, bs_m, bs_d)
            bs_str = f"{bs_y:04d}-{bs_m:02d}-{bs_d:02d}"
            return ad_date, bs_str

        # Format: DD-MM-YYYY in BS (e.g. 19-06-2083)
        elif 2000 <= parts[2] <= 2095:
            bs_y, bs_m, bs_d = parts[2], parts[1], parts[0]
            bs_m = max(1, min(12, bs_m))
            max_days = NepaliCalendar.get_days_in_month(bs_y, bs_m)
            bs_d = max(1, min(max_days, bs_d))
            ad_date = NepaliCalendar.bs_to_ad(bs_y, bs_m, bs_d)
            bs_str = f"{bs_y:04d}-{bs_m:02d}-{bs_d:02d}"
            return ad_date, bs_str

        # Format: YYYY-MM-DD in AD (e.g. 2026-10-05)
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
        1. Fiscal Year parameter (e.g. '2083/84', '2080/81').
        2. Direct Nepali BS dates ('YYYY-MM-DD' or 'YYYY/MM/DD').
        3. Gregorian AD dates ('YYYY-MM-DD').
        4. Default fallback: 1st day of the ongoing BS month through today.
        """
        today_ad = timezone.now().date()
        today_bs_y, today_bs_m, today_bs_d = NepaliCalendar.ad_to_bs(today_ad)

        fy_param = str(raw_params.get('fiscal_year') or raw_params.get('fy') or '').strip()
        start_param = str(raw_params.get('start_date') or raw_params.get('start_date_bs') or '').strip()
        end_param = str(raw_params.get('end_date') or raw_params.get('end_date_bs') or '').strip()

        # Case 1: Fiscal Year preset selected without overriding custom date inputs
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

        # Case 2: Parse custom start and end date inputs
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

        # Case 3: Fiscal year chosen along with partial custom dates
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

        # Case 4: Default fallback to 1st of current BS month through today
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

    # =========================================================================
    # 2. MAIN REPORT ENGINE & MULTI-SECTION AGGREGATIONS
    # =========================================================================
    @classmethod
    def get_sales_summary_data(
        cls,
        filters: Dict[str, Any],
        user=None
    ) -> Dict[str, Any]:
        """
        Executes high-precision date-wise rollups, separates handsets vs. accessories,
        integrates customer returns, reconciles receivables, and builds period summaries.
        """
        active_branch = getattr(user, 'assigned_branch', None) if user and not user.is_superuser else None

        # ---------------------------------------------------------------------
        # POINT 1.1: EXPANDED FILTER RESOLUTION
        # ---------------------------------------------------------------------
        start_date, end_date, start_date_bs, end_date_bs, fiscal_year = cls.resolve_date_range(filters)
        branch_id = filters.get('branch_id') or filters.get('branch', '')
        payment_status = str(filters.get('payment_status', '') or '').strip()
        customer_type = str(filters.get('customer_type', '') or '').strip().lower()
        sale_type = str(filters.get('sale_type', '') or '').strip().lower()
        category_id = filters.get('category_id') or filters.get('category', '')
        salesperson_id = filters.get('salesperson_id') or filters.get('salesperson', '')

        # 1. Base Sales Invoices Queryset
        estimates_qs = SalesEstimate.objects.filter(
            status__in=['COMPLETED', 'PARTIALLY_RETURNED'],
            bill_date_ad__gte=start_date,
            bill_date_ad__lte=end_date
        )

        # Branch Scoping
        if branch_id and str(branch_id).strip() not in ['', 'all', 'None']:
            estimates_qs = estimates_qs.filter(branch_id=branch_id)
        elif active_branch and not (user.is_superuser or getattr(user, 'role', '') == 'OWNER'):
            estimates_qs = estimates_qs.filter(branch=active_branch)

        # Payment Status Filter
        if payment_status and payment_status not in ['', 'all']:
            estimates_qs = estimates_qs.filter(payment_status=payment_status)

        # Salesperson Filter
        if salesperson_id and str(salesperson_id).strip() not in ['', 'all', 'None']:
            estimates_qs = estimates_qs.filter(
                Q(salesperson_id=salesperson_id) | (Q(salesperson__isnull=True) & Q(cashier_id=salesperson_id))
            )

        # Customer Type Filter
        if customer_type in ['walkin', 'walk-in', 'retail']:
            estimates_qs = estimates_qs.filter(
                Q(customer__isnull=True) |
                Q(customer__customer_type='RETAIL') |
                Q(customer_name_manual__icontains='walk-in')
            )
        elif customer_type in ['registered', 'regular']:
            estimates_qs = estimates_qs.filter(customer__isnull=False).exclude(customer_name_manual__icontains='walk-in')
        elif customer_type in ['wholesale', 'b2b', 'dealer']:
            estimates_qs = estimates_qs.filter(customer__customer_type='WHOLESALE')

        # Sale Type Filter
        if sale_type == 'cash':
            estimates_qs = estimates_qs.filter(due_amount=Decimal('0.00'), has_trade_in_exchange=False)
        elif sale_type == 'credit':
            estimates_qs = estimates_qs.filter(due_amount__gt=Decimal('0.00'))
        elif sale_type == 'exchange':
            estimates_qs = estimates_qs.filter(has_trade_in_exchange=True)

        # Category Filter on Invoices
        if category_id and str(category_id).strip() not in ['', 'all', 'None']:
            estimates_qs = estimates_qs.filter(items__product__category_id=category_id).distinct()

        # ---------------------------------------------------------------------
        # POINT 1.2: DATE-WISE SALES INVOICES AGGREGATION
        # ---------------------------------------------------------------------
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

        # ---------------------------------------------------------------------
        # POINT 1.2: MOBILES VS. ACCESSORIES UNIT QUANTITY SPLIT
        # ---------------------------------------------------------------------
        items_qs = SalesEstimateItem.objects.filter(estimate__in=estimates_qs)
        if category_id and str(category_id).strip() not in ['', 'all', 'None']:
            items_qs = items_qs.filter(product__category_id=category_id)

        units_by_date = items_qs.values('estimate__bill_date_ad').annotate(
            total_units=Coalesce(
                Sum('quantity'),
                Value(Decimal('0.000'), output_field=DecimalField(max_digits=18, decimal_places=3))
            ),
            mobiles_count=Coalesce(
                Sum(Case(When(product__requires_imei_tracking=True, then=F('quantity')))),
                Value(Decimal('0.000'), output_field=DecimalField(max_digits=18, decimal_places=3))
            ),
            accessories_count=Coalesce(
                Sum(Case(When(product__requires_imei_tracking=False, then=F('quantity')))),
                Value(Decimal('0.000'), output_field=DecimalField(max_digits=18, decimal_places=3))
            )
        )
        units_map = {row['estimate__bill_date_ad']: row for row in units_by_date}

        # ---------------------------------------------------------------------
        # POINT 1.3: INTEGRATE CUSTOMER SALES RETURNS & CASH REFUNDS BY DATE
        # ---------------------------------------------------------------------
        returns_base_qs = SalesReturn.objects.filter(
            return_date_ad__gte=start_date,
            return_date_ad__lte=end_date
        )
        if branch_id and str(branch_id).strip() not in ['', 'all', 'None']:
            returns_base_qs = returns_base_qs.filter(branch_id=branch_id)
        elif active_branch and not (user.is_superuser or getattr(user, 'role', '') == 'OWNER'):
            returns_base_qs = returns_base_qs.filter(branch=active_branch)

        returns_by_date = returns_base_qs.values('return_date_ad').annotate(
            return_count=Count('id'),
            total_refund=Coalesce(
                Sum('total_refund_amount'),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
            cash_refund=Coalesce(
                Sum(Case(When(refund_mode='CASH', then=F('total_refund_amount')))),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            )
        )
        returns_map = {row['return_date_ad']: row for row in returns_by_date}

        # Daily Payment Tenders Map
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

        # ---------------------------------------------------------------------
        # BUILD 14-COLUMN DATE-WISE RECORDS & ACCUMULATE TOTALS
        # ---------------------------------------------------------------------
        records: List[Dict[str, Any]] = []

        total_invoices = 0
        total_mobiles = Decimal('0.000')
        total_accessories = Decimal('0.000')
        total_units = Decimal('0.000')
        total_gross = Decimal('0.00')
        total_item_disc = Decimal('0.00')
        total_bill_disc = Decimal('0.00')
        total_sales_disc = Decimal('0.00')
        total_trade_in = Decimal('0.00')
        total_taxable = Decimal('0.00')
        total_non_taxable = Decimal('0.00')
        total_vat = Decimal('0.00')
        total_net_turnover = Decimal('0.00')
        total_cogs = Decimal('0.00')
        total_profit = Decimal('0.00')
        total_collected = Decimal('0.00')
        total_due = Decimal('0.00')
        total_returns = Decimal('0.00')
        total_net_collection = Decimal('0.00')

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

            # Mobiles vs Accessories split
            u_info = units_map.get(day_ad, {})
            u_mobiles = u_info.get('mobiles_count', Decimal('0.000'))
            u_accessories = u_info.get('accessories_count', Decimal('0.000'))
            u_total = u_info.get('total_units', Decimal('0.000'))

            # Returns for this day
            r_info = returns_map.get(day_ad, {})
            day_returns = r_info.get('total_refund', Decimal('0.00'))

            # Net Collection = Collected (Paid) - Returns
            net_inflow = max(Decimal('0.00'), paid - day_returns)

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
            total_mobiles += u_mobiles
            total_accessories += u_accessories
            total_units += u_total
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
            total_collected += paid
            total_due += due
            total_returns += day_returns
            total_net_collection += net_inflow

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
                'mobiles_sold': u_mobiles,
                'accessories_sold': u_accessories,
                'units_sold': u_total,
                'gross_subtotal': gross,
                'item_discount_sum': it_disc,
                'bill_discount_sum': b_disc,
                'total_discount_sum': s_disc,
                'net_sales': grand,
                'trade_in_credit_sum': tr_credit,
                'taxable_amount': taxable,
                'non_taxable_amount': non_taxable,
                'vat_amount': vat,
                'grand_total': grand,
                'cogs_amount': cogs,
                'gross_profit': profit,
                'margin_percent': margin_pct,
                'collected': paid,
                'due_amount': due,
                'refund_amount': day_returns,
                'net_collection': net_inflow,
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

        # ---------------------------------------------------------------------
        # POINT 1.5: PERIOD-LEVEL PAYMENT TENDER ROLLUP
        # ---------------------------------------------------------------------
        total_tendered_sum = total_collected + total_due
        payment_rollup = [
            {
                'mode': 'CASH',
                'label': 'Cash Counter Collection',
                'dot_color': '#10b981',
                'bills_count': estimates_qs.filter(payment_transactions__payment_mode='CASH').distinct().count(),
                'amount': total_cash,
                'percent': ((total_cash / total_tendered_sum) * Decimal('100.0')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP) if total_tendered_sum > 0 else Decimal('0.0')
            },
            {
                'mode': 'ESEWA',
                'label': 'eSewa Digital Wallet',
                'dot_color': '#059669',
                'bills_count': estimates_qs.filter(payment_transactions__payment_mode='ESEWA').distinct().count(),
                'amount': total_esewa,
                'percent': ((total_esewa / total_tendered_sum) * Decimal('100.0')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP) if total_tendered_sum > 0 else Decimal('0.0')
            },
            {
                'mode': 'FONEPAY',
                'label': 'Fonepay QR Direct',
                'dot_color': '#ea580c',
                'bills_count': estimates_qs.filter(payment_transactions__payment_mode='FONEPAY').distinct().count(),
                'amount': total_fonepay,
                'percent': ((total_fonepay / total_tendered_sum) * Decimal('100.0')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP) if total_tendered_sum > 0 else Decimal('0.0')
            },
            {
                'mode': 'KHALTI',
                'label': 'Khalti Wallet',
                'dot_color': '#7c3aed',
                'bills_count': estimates_qs.filter(payment_transactions__payment_mode='KHALTI').distinct().count(),
                'amount': total_khalti,
                'percent': ((total_khalti / total_tendered_sum) * Decimal('100.0')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP) if total_tendered_sum > 0 else Decimal('0.0')
            },
            {
                'mode': 'BANK_TRANSFER',
                'label': 'ConnectIPS / Bank Transfer',
                'dot_color': '#0284c7',
                'bills_count': estimates_qs.filter(payment_transactions__payment_mode='BANK_TRANSFER').distinct().count(),
                'amount': total_bank,
                'percent': ((total_bank / total_tendered_sum) * Decimal('100.0')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP) if total_tendered_sum > 0 else Decimal('0.0')
            },
            {
                'mode': 'CARD',
                'label': 'Visa / Mastercard POS',
                'dot_color': '#6366f1',
                'bills_count': estimates_qs.filter(payment_transactions__payment_mode='CARD').distinct().count(),
                'amount': total_card,
                'percent': ((total_card / total_tendered_sum) * Decimal('100.0')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP) if total_tendered_sum > 0 else Decimal('0.0')
            },
            {
                'mode': 'CREDIT',
                'label': 'Customer Credit (Udhaari)',
                'dot_color': '#d97706',
                'bills_count': estimates_qs.filter(due_amount__gt=Decimal('0.00')).count(),
                'amount': total_due,
                'percent': ((total_due / total_tendered_sum) * Decimal('100.0')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP) if total_tendered_sum > 0 else Decimal('0.0')
            }
        ]

        # ---------------------------------------------------------------------
        # POINT 1.6: DAILY COLLECTION & DUE RECONCILIATION WATERFALL
        # ---------------------------------------------------------------------
        customer_qs = Customer.objects.filter(is_active=True)
        live_total_customer_due = customer_qs.aggregate(
            s=Coalesce(Sum('current_credit_balance'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2)))
        )['s']

        # Old collections recovered from debtor ledgers in this period
        debt_collections = CustomerUdhaariLedger.objects.filter(
            entry_type__in=['CREDIT', 'ADJUSTMENT'],
            created_at__date__gte=start_date,
            created_at__date__lte=end_date
        ).aggregate(
            s=Coalesce(Sum('amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2)))
        )['s']

        # Net movement after this period to reconstruct period closing balance
        debt_after_period = CustomerUdhaariLedger.objects.filter(
            created_at__date__gt=end_date
        ).aggregate(
            debits=Coalesce(Sum('amount', filter=Q(entry_type='DEBIT')), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            credits=Coalesce(Sum('amount', filter=Q(entry_type__in=['CREDIT', 'ADJUSTMENT'])), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2)))
        )
        delta_after = debt_after_period['debits'] - debt_after_period['credits']

        recon_closing_due = max(Decimal('0.00'), live_total_customer_due - delta_after)
        recon_new_credit = total_due
        recon_collections = debt_collections
        recon_returns = total_returns

        # Reconstructed Opening Due: Closing - Credit + Collections + Returns
        recon_opening_due = max(Decimal('0.00'), recon_closing_due - recon_new_credit + recon_collections + recon_returns)

        reconciliation = {
            'opening_due': recon_opening_due,
            'credit_sales': recon_new_credit,
            'collections_recovered': recon_collections,
            'returns_adjustments': recon_returns,
            'closing_due': recon_closing_due,
        }

        # ---------------------------------------------------------------------
        # POINT 1.7: MOBILE SALES OVERVIEW & TOP BRANDS
        # ---------------------------------------------------------------------
        phone_items = items_qs.filter(product__requires_imei_tracking=True)
        total_phones_sold = phone_items.aggregate(
            q=Coalesce(Sum('quantity'), Value(Decimal('0.000'), output_field=DecimalField(max_digits=18, decimal_places=3)))
        )['q']

        phone_sales_val = phone_items.aggregate(
            v=Coalesce(Sum('line_total'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2)))
        )['v']

        asp = (phone_sales_val / total_phones_sold).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) if total_phones_sold > 0 else Decimal('0.00')

        # Top 5 Brands Performance
        brand_grouped = phone_items.values('product__brand__name').annotate(
            units=Coalesce(Sum('quantity'), Value(Decimal('0.000'), output_field=DecimalField(max_digits=18, decimal_places=3))),
            sales=Coalesce(Sum('line_total'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            cogs=Coalesce(Sum(ExpressionWrapper(F('cost_price') * F('base_unit_quantity'), output_field=DecimalField(max_digits=18, decimal_places=2))), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2)))
        ).order_by('-sales')[:5]

        top_brands = []
        for b in brand_grouped:
            b_sales = b['sales']
            b_cogs = b['cogs']
            b_profit = max(Decimal('0.00'), b_sales - b_cogs)
            top_brands.append({
                'brand_name': b['product__brand__name'] or 'General Handset',
                'units': int(b['units']),
                'sales': b_sales,
                'profit': b_profit,
            })

        mobile_overview = {
            'total_phones_sold': int(total_phones_sold),
            'phone_sales_value': phone_sales_val,
            'new_phones_count': int(total_phones_sold),
            'used_phones_count': 0,
            'asp': asp,
            'top_brands': top_brands,
        }

        # ---------------------------------------------------------------------
        # POINT 1.8: TRADE-IN EXCHANGE & RETURNS AUDIT TOTALS
        # ---------------------------------------------------------------------
        trade_ins_qs = PhoneExchangeTradeIn.objects.filter(pos_estimate__in=estimates_qs)
        trade_in_summary = {
            'exchange_phones_count': trade_ins_qs.count(),
            'total_trade_in_value': total_trade_in,
            'new_phone_sales_exchange': estimates_qs.filter(has_trade_in_exchange=True).aggregate(s=Coalesce(Sum('grand_total'), Value(Decimal('0.00'))))['s'],
            'cash_topup_collected': estimates_qs.filter(has_trade_in_exchange=True).aggregate(s=Coalesce(Sum('paid_amount'), Value(Decimal('0.00'))))['s'],
        }

        returns_summary = {
            'returned_bills_count': returns_base_qs.count(),
            'returned_units_count': int(SalesReturnItem.objects.filter(sales_return__in=returns_base_qs).aggregate(s=Coalesce(Sum('return_quantity'), Value(Decimal('0.000'))))['s']),
            'total_return_amount': total_returns,
            'cash_refunds_issued': returns_base_qs.filter(refund_mode='CASH').aggregate(s=Coalesce(Sum('total_refund_amount'), Value(Decimal('0.00'))))['s'],
            'store_credit_issued': returns_base_qs.filter(refund_mode='STORE_CREDIT').aggregate(s=Coalesce(Sum('total_refund_amount'), Value(Decimal('0.00'))))['s'],
        }

        # ---------------------------------------------------------------------
        # POINT 1.4: 8 EXECUTIVE KPI CARDS DICTIONARY
        # ---------------------------------------------------------------------
        collection_rate = (
            ((total_collected / total_net_turnover) * Decimal('100.0')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
            if total_net_turnover > Decimal('0.00') else Decimal('0.0')
        )
        avg_bill_val = (
            (total_net_turnover / Decimal(str(total_invoices))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            if total_invoices > 0 else Decimal('0.00')
        )

        kpi_cards = {
            'total_sales': total_net_turnover,
            'gross_sales': total_gross,
            'gross_profit': total_profit,
            'overall_margin_pct': overall_margin_pct,
            'total_cogs': total_cogs,
            'amount_collected': total_collected,
            'collection_rate_pct': collection_rate,
            'credit_due_sales': total_due,
            'credit_bills_count': estimates_qs.filter(due_amount__gt=Decimal('0.00')).count(),
            'total_bills': total_invoices,
            'avg_bill_value': avg_bill_val,
            'units_sold': total_units,
            'mobiles_sold': total_mobiles,
            'accessories_sold': total_accessories,
            'total_discounts': total_sales_disc,
            'outstanding_receivable': recon_closing_due,
        }

        # ---------------------------------------------------------------------
        # TOTALS ROW DICTIONARY FOR MAIN TABLE (14 COLUMNS)
        # ---------------------------------------------------------------------
        totals = {
            'days_count': len(records),
            'total_invoices': total_invoices,
            'total_mobiles': total_mobiles,
            'total_accessories': total_accessories,
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
            'total_collected': total_collected,
            'total_due': total_due,
            'total_returns': total_returns,
            'total_net_collection': total_net_collection,
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
            'kpi_cards': kpi_cards,
            'payment_rollup': payment_rollup,
            'reconciliation': reconciliation,
            'mobile_overview': mobile_overview,
            'trade_in_summary': trade_in_summary,
            'returns_summary': returns_summary,
            'branches': Branch.objects.filter(is_active=True).order_by('-is_main_branch', 'name'),
            'categories': ProductCategory.objects.filter(is_active=True).order_by('name'),
            'brands': Brand.objects.all().order_by('name'),
            'salespeople': User.objects.filter(is_active=True).order_by('username'),
            'selected_branch': selected_branch,
            'filters': filters,
            'start_date': start_date.strftime('%Y-%m-%d'),
            'end_date': end_date.strftime('%Y-%m-%d'),
            'start_date_bs': start_date_bs,
            'end_date_bs': end_date_bs,
            'fiscal_year': fiscal_year,
            'available_fiscal_years': cls.get_available_fiscal_years(),
        }

    # =========================================================================
    # 3. DASHBOARD VISUAL TREND DATA (PRESERVED FOR EXECUTIVE CHARTS)
    # =========================================================================
    @classmethod
    def get_dashboard_trend_data(
        cls,
        branch=None,
        user=None
    ) -> Dict[str, Any]:
        """
        Computes daily (7-day) and monthly (6-month) comparative sales revenue
        vs. inward purchase expenditure arrays for executive charts.
        """
        today = timezone.now().date()
        active_branch = branch or (
            getattr(user, 'assigned_branch', None)
            if user and not (user.is_superuser or getattr(user, 'role', '') == 'OWNER')
            else None
        )

        sales_base = SalesEstimate.objects.filter(
            status__in=['COMPLETED', 'PARTIALLY_RETURNED']
        )
        grn_base = GoodsReceivedNote.objects.filter(
            status='RECEIVED'
        )

        if active_branch:
            sales_base = sales_base.filter(branch=active_branch)
            grn_base = grn_base.filter(branch=active_branch)

        # 1. Daily 7-Day Trend
        start_date_7d = today - timedelta(days=6)
        sales_7d_dict = dict(
            sales_base.filter(bill_date_ad__gte=start_date_7d, bill_date_ad__lte=today)
            .values('bill_date_ad')
            .annotate(total=Sum('grand_total'))
            .values_list('bill_date_ad', 'total')
        )
        grn_7d_dict = dict(
            grn_base.filter(bill_date__gte=start_date_7d, bill_date__lte=today)
            .values('bill_date')
            .annotate(total=Sum('net_total_amount'))
            .values_list('bill_date', 'total')
        )

        daily_labels: List[str] = []
        daily_sales: List[float] = []
        daily_purchases: List[float] = []

        for i in range(6, -1, -1):
            d = today - timedelta(days=i)
            label = 'Today' if i == 0 else d.strftime('%a')
            daily_labels.append(label)
            daily_sales.append(float(sales_7d_dict.get(d) or Decimal('0.00')))
            daily_purchases.append(float(grn_7d_dict.get(d) or Decimal('0.00')))

        # 2. Monthly 6-Month Trend
        months_list: List[Tuple[int, int]] = []
        cur_year, cur_month = today.year, today.month
        for i in range(5, -1, -1):
            m = cur_month - i
            y = cur_year
            while m <= 0:
                m += 12
                y -= 1
            months_list.append((y, m))

        first_month_date = date(months_list[0][0], months_list[0][1], 1)

        sales_monthly_dict = dict(
            sales_base.filter(bill_date_ad__gte=first_month_date)
            .annotate(month=TruncMonth('bill_date_ad'))
            .values('month')
            .annotate(total=Sum('grand_total'))
            .values_list('month', 'total')
        )
        grn_monthly_dict = dict(
            grn_base.filter(bill_date__gte=first_month_date)
            .annotate(month=TruncMonth('bill_date'))
            .values('month')
            .annotate(total=Sum('net_total_amount'))
            .values_list('month', 'total')
        )

        monthly_labels: List[str] = []
        monthly_sales: List[float] = []
        monthly_purchases: List[float] = []

        for y, m in months_list:
            dt_key = date(y, m, 1)
            monthly_labels.append(dt_key.strftime('%b %Y'))
            s_val = sum(v for k, v in sales_monthly_dict.items() if k and k.year == y and k.month == m)
            p_val = sum(v for k, v in grn_monthly_dict.items() if k and k.year == y and k.month == m)
            monthly_sales.append(float(s_val or Decimal('0.00')))
            monthly_purchases.append(float(p_val or Decimal('0.00')))

        return {
            'daily_labels': daily_labels,
            'daily_sales': daily_sales,
            'daily_purchases': daily_purchases,
            'monthly_labels': monthly_labels,
            'monthly_sales': monthly_sales,
            'monthly_purchases': monthly_purchases,
        }