"""
Taxation & Proforma Book Report Generator (Annex 5 Sales Book, Annex 7 Purchase Book & Daily VAT Ledger).
File Path: apps/taxation/reports.py

Capabilities:
1. Generates Sales Register (Annex 5 style) in estimation / proforma breakdown format.
   - Includes COMPLETED and PARTIALLY_RETURNED sales estimates.
   - Accurately deducts return refunds and tax adjustments from taxable and non-taxable columns.
2. Generates Purchase Register (Annex 7 style) for all verified GRN vouchers.
   - Retrieves the complete set of GRN fields: gross subtotal, trade discount, input VAT,
     shipping overheads (freight, customs, handling), landed inventory cost, net total,
     spot cash paid, and outstanding balance added to the supplier ledger.
   - Computes cumulative summary totals across all financial columns.
3. Generates Unified Day-Wise VAT Ledger:
   - Traverses date-by-date across any Gregorian or Bikram Sambat date boundary.
   - Merges daily sales output VAT and inward GRN purchase input VAT.
   - Accounts for customer sales returns and distributor debit notes (purchase returns).
   - Computes Daily Net VAT (Output VAT - Input VAT) and a Progressive Cumulative Running Balance.
"""

from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from django.db.models import Sum, Q, F, DecimalField, Value
from django.db.models.functions import Coalesce

from apps.sales.models import SalesEstimate, SalesReturn, SalesReturnItem
from apps.purchases.models import GoodsReceivedNote, PurchaseReturn
from apps.branches.models import Branch
from apps.core.models import SystemConfiguration
from apps.core.nepali_calendar import NepaliCalendar


class TaxationReportGenerator:
    """
    Generates Sales Register (Annex 5 style), Purchase Register (Annex 7 style),
    and the Day-Wise Combined VAT & Tax Assessment Ledger.
    """

    @classmethod
    def generate_sales_book(cls, branch: Branch, start_date, end_date):
        """
        Compiles the Annex 5 Sales Book reconciling gross billing, customer returns,
        taxable base, and output VAT.
        """
        config = SystemConfiguration.get_solo()
        is_vat_shop = (config.tax_system_mode == 'VAT')

        qs = SalesEstimate.objects.filter(
            branch=branch,
            bill_date_ad__gte=start_date,
            bill_date_ad__lte=end_date,
            status__in=['COMPLETED', 'PARTIALLY_RETURNED']
        ).prefetch_related('returns', 'returns__items').order_by('bill_date_ad', 'estimate_number')

        totals = qs.aggregate(
            total_taxable=Coalesce(Sum('taxable_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_non_taxable=Coalesce(Sum('non_taxable_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_vat=Coalesce(Sum('vat_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_grand=Coalesce(Sum('grand_total'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_subtotal=Coalesce(Sum('subtotal'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2)))
        )

        taxable_sum = totals['total_taxable']
        non_taxable_sum = totals['total_non_taxable']
        vat_sum = totals['total_vat']
        grand_sum = totals['total_grand']

        returns_aggregate = SalesReturn.objects.filter(original_estimate__in=qs).aggregate(
            total_refund=Coalesce(Sum('total_refund_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2)))
        )
        total_refund_amount = returns_aggregate['total_refund']

        if is_vat_shop:
            returned_items = SalesReturnItem.objects.filter(sales_return__original_estimate__in=qs).select_related('estimate_item')
            return_taxable_deduct = Decimal('0.00')
            return_vat_deduct = Decimal('0.00')
            return_non_taxable_deduct = Decimal('0.00')

            for r_item in returned_items:
                est_item = r_item.estimate_item
                if est_item.is_vat_applicable and est_item.vat_rate > Decimal('0.00'):
                    rate = est_item.vat_rate
                    if est_item.tax_pricing_type == 'INCLUSIVE':
                        base_val = (r_item.refund_amount / (Decimal('1.00') + (rate / Decimal('100.00')))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                        vat_val = r_item.refund_amount - base_val
                    else:
                        base_val = r_item.refund_amount
                        vat_val = (base_val * (rate / Decimal('100.00'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                    return_taxable_deduct += base_val
                    return_vat_deduct += vat_val
                else:
                    return_non_taxable_deduct += r_item.refund_amount

            taxable_sum = max(Decimal('0.00'), taxable_sum - return_taxable_deduct)
            vat_sum = max(Decimal('0.00'), vat_sum - return_vat_deduct)
            non_taxable_sum = max(Decimal('0.00'), non_taxable_sum - return_non_taxable_deduct)
            grand_sum = max(Decimal('0.00'), grand_sum - total_refund_amount)
        else:
            taxable_sum = Decimal('0.00')
            vat_sum = Decimal('0.00')
            grand_sum = max(Decimal('0.00'), grand_sum - total_refund_amount)
            non_taxable_sum = grand_sum

        return {
            'records': qs,
            'is_vat_shop': is_vat_shop,
            'totals': {
                'taxable': taxable_sum,
                'non_taxable': non_taxable_sum,
                'vat': vat_sum,
                'grand_total': grand_sum,
                'total_refunds': total_refund_amount,
            }
        }

    @classmethod
    def generate_purchase_book(cls, branch: Branch, start_date, end_date):
        """
        Compiles the Annex 7 Purchase Register retrieving all commercial GRN metrics:
        gross, trade discount, input VAT, freight/customs overheads, landed costs, paid amounts, and due debts.
        """
        qs = GoodsReceivedNote.objects.filter(
            branch=branch,
            bill_date__gte=start_date,
            bill_date__lte=end_date,
            status='RECEIVED'
        ).select_related('supplier', 'branch', 'received_by').order_by('bill_date', 'grn_number')

        totals = qs.aggregate(
            total_gross=Coalesce(Sum('gross_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_discount=Coalesce(Sum('discount_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_vat=Coalesce(Sum('vat_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_freight=Coalesce(Sum('extra_freight_charge'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_customs=Coalesce(Sum('customs_import_charge'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_handling=Coalesce(Sum('other_handling_charge'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_landed=Coalesce(Sum('total_landed_cost'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_net=Coalesce(Sum('net_total_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_paid=Coalesce(Sum('paid_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_due=Coalesce(Sum('due_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2)))
        )

        total_overheads = (
            totals['total_freight'] + totals['total_customs'] + totals['total_handling']
        ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        return {
            'records': qs,
            'totals': {
                'gross': totals['total_gross'],
                'discount': totals['total_discount'],
                'vat': totals['total_vat'],
                'freight': totals['total_freight'],
                'customs': totals['total_customs'],
                'handling': totals['total_handling'],
                'overheads': total_overheads,
                'landed': totals['total_landed'],
                'net': totals['total_net'],
                'paid': totals['total_paid'],
                'due': totals['total_due'],
            }
        }

    @classmethod
    def generate_daily_vat_ledger(cls, branch: Branch, start_date: date, end_date: date):
        """
        Generates the chronological Day-Wise Combined VAT & Tax Assessment Ledger.
        
        Performs high-speed bulk querying (4 single database hits) and groups records
        in memory to prevent N+1 query overhead across large multi-month/yearly date ranges.
        
        For each individual calendar day:
        - Output VAT (Sales Tax Collected) minus Sales Returns (Credit Notes)
        - Input VAT (Purchase Tax Paid) minus Purchase Returns (Debit Notes)
        - Daily Net VAT = Daily Output VAT - Daily Input VAT
        - Progressive Cumulative Running Balance of Government VAT Liability
        """
        config = SystemConfiguration.get_solo()
        is_vat_shop = (config.tax_system_mode == 'VAT')

        # ---------------------------------------------------------------------
        # 1. Bulk Query Transactions in Scope (4 Optimized Database Hits)
        # ---------------------------------------------------------------------
        # A. Sales Estimates in range
        sales_qs = SalesEstimate.objects.filter(
            branch=branch,
            bill_date_ad__gte=start_date,
            bill_date_ad__lte=end_date,
            status__in=['COMPLETED', 'PARTIALLY_RETURNED']
        ).only('id', 'bill_date_ad', 'taxable_amount', 'non_taxable_amount', 'vat_amount', 'grand_total')

        # B. Sales Return Items in range (grouped by return date)
        sales_return_items = SalesReturnItem.objects.filter(
            sales_return__branch=branch,
            sales_return__return_date_ad__gte=start_date,
            sales_return__return_date_ad__lte=end_date
        ).select_related('estimate_item', 'sales_return')

        # C. Goods Received Notes (GRN Inward Purchases) in range
        grn_qs = GoodsReceivedNote.objects.filter(
            branch=branch,
            bill_date__gte=start_date,
            bill_date__lte=end_date,
            status='RECEIVED'
        ).only('id', 'bill_date', 'taxable_amount', 'gross_amount', 'vat_amount', 'net_total_amount')

        # D. Purchase Returns (Debit Notes to Suppliers) in range
        purchase_returns_qs = PurchaseReturn.objects.filter(
            branch=branch,
            return_date__gte=start_date,
            return_date__lte=end_date,
            status='CONFIRMED'
        ).only('id', 'return_date', 'total_return_amount', 'tax_amount', 'net_refund_amount')

        # ---------------------------------------------------------------------
        # 2. In-Memory Grouping by Calendar Date
        # ---------------------------------------------------------------------
        daily_sales_map = {}
        for s in sales_qs:
            d = s.bill_date_ad
            if d not in daily_sales_map:
                daily_sales_map[d] = {
                    'taxable': Decimal('0.00'),
                    'non_taxable': Decimal('0.00'),
                    'vat': Decimal('0.00'),
                    'grand_total': Decimal('0.00'),
                    'count': 0
                }
            daily_sales_map[d]['taxable'] += s.taxable_amount
            daily_sales_map[d]['non_taxable'] += s.non_taxable_amount
            daily_sales_map[d]['vat'] += s.vat_amount
            daily_sales_map[d]['grand_total'] += s.grand_total
            daily_sales_map[d]['count'] += 1

        # Sales return adjustments
        daily_sales_returns_map = {}
        for r_item in sales_return_items:
            d = r_item.sales_return.return_date_ad
            if d not in daily_sales_returns_map:
                daily_sales_returns_map[d] = {
                    'taxable_deduct': Decimal('0.00'),
                    'vat_deduct': Decimal('0.00'),
                    'non_taxable_deduct': Decimal('0.00'),
                    'refund_total': Decimal('0.00'),
                }
            daily_sales_returns_map[d]['refund_total'] += r_item.refund_amount
            if is_vat_shop:
                est_item = r_item.estimate_item
                if est_item and est_item.is_vat_applicable and est_item.vat_rate > Decimal('0.00'):
                    rate = est_item.vat_rate
                    if est_item.tax_pricing_type == 'INCLUSIVE':
                        base_val = (r_item.refund_amount / (Decimal('1.00') + (rate / Decimal('100.00')))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                        vat_val = r_item.refund_amount - base_val
                    else:
                        base_val = r_item.refund_amount
                        vat_val = (base_val * (rate / Decimal('100.00'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                    daily_sales_returns_map[d]['taxable_deduct'] += base_val
                    daily_sales_returns_map[d]['vat_deduct'] += vat_val
                else:
                    daily_sales_returns_map[d]['non_taxable_deduct'] += r_item.refund_amount
            else:
                daily_sales_returns_map[d]['non_taxable_deduct'] += r_item.refund_amount

        # Purchase GRN grouping
        daily_purchase_map = {}
        for p in grn_qs:
            d = p.bill_date
            if d not in daily_purchase_map:
                daily_purchase_map[d] = {
                    'taxable': Decimal('0.00'),
                    'gross': Decimal('0.00'),
                    'vat': Decimal('0.00'),
                    'net_total': Decimal('0.00'),
                    'count': 0
                }
            daily_purchase_map[d]['taxable'] += p.taxable_amount
            daily_purchase_map[d]['gross'] += p.gross_amount
            daily_purchase_map[d]['vat'] += p.vat_amount
            daily_purchase_map[d]['net_total'] += p.net_total_amount
            daily_purchase_map[d]['count'] += 1

        # Purchase returns grouping (Debit Notes)
        daily_purchase_returns_map = {}
        for pret in purchase_returns_qs:
            d = pret.return_date
            if d not in daily_purchase_returns_map:
                daily_purchase_returns_map[d] = {
                    'taxable_deduct': Decimal('0.00'),
                    'vat_deduct': Decimal('0.00'),
                    'refund_total': Decimal('0.00')
                }
            daily_purchase_returns_map[d]['taxable_deduct'] += pret.total_return_amount
            daily_purchase_returns_map[d]['vat_deduct'] += pret.tax_amount
            daily_purchase_returns_map[d]['refund_total'] += pret.net_refund_amount

        # ---------------------------------------------------------------------
        # 3. Date-by-Date Iteration & Progressive Running Balance Calculation
        # ---------------------------------------------------------------------
        days_count = (end_date - start_date).days + 1
        daily_rows = []
        cumulative_balance = Decimal('0.00')

        # Cumulative Grand Totals Accumulators
        period_sales_taxable = Decimal('0.00')
        period_sales_non_taxable = Decimal('0.00')
        period_sales_vat = Decimal('0.00')
        period_sales_grand = Decimal('0.00')
        period_purchase_taxable = Decimal('0.00')
        period_purchase_vat = Decimal('0.00')
        period_purchase_net = Decimal('0.00')
        period_net_vat = Decimal('0.00')
        active_tax_days_count = 0

        current_day = start_date
        while current_day <= end_date:
            # 1. Bikram Sambat Date Representation
            try:
                bs_y, bs_m, bs_d = NepaliCalendar.ad_to_bs(current_day)
                date_bs_str = f"{bs_y:04d}-{bs_m:02d}-{bs_d:02d}"
                bs_month_name = NepaliCalendar.NEPALI_MONTH_NAMES_EN[bs_m - 1]
            except Exception:
                date_bs_str = ""
                bs_month_name = ""

            # 2. Sales Math for current day
            raw_s = daily_sales_map.get(current_day, {
                'taxable': Decimal('0.00'), 'non_taxable': Decimal('0.00'),
                'vat': Decimal('0.00'), 'grand_total': Decimal('0.00'), 'count': 0
            })
            ret_s = daily_sales_returns_map.get(current_day, {
                'taxable_deduct': Decimal('0.00'), 'vat_deduct': Decimal('0.00'),
                'non_taxable_deduct': Decimal('0.00'), 'refund_total': Decimal('0.00')
            })

            if is_vat_shop:
                net_day_sales_taxable = max(Decimal('0.00'), raw_s['taxable'] - ret_s['taxable_deduct'])
                net_day_sales_vat = max(Decimal('0.00'), raw_s['vat'] - ret_s['vat_deduct'])
                net_day_sales_non_taxable = max(Decimal('0.00'), raw_s['non_taxable'] - ret_s['non_taxable_deduct'])
                net_day_sales_grand = max(Decimal('0.00'), raw_s['grand_total'] - ret_s['refund_total'])
            else:
                net_day_sales_taxable = Decimal('0.00')
                net_day_sales_vat = Decimal('0.00')
                net_day_sales_grand = max(Decimal('0.00'), raw_s['grand_total'] - ret_s['refund_total'])
                net_day_sales_non_taxable = net_day_sales_grand

            # 3. Purchase Math for current day
            raw_p = daily_purchase_map.get(current_day, {
                'taxable': Decimal('0.00'), 'gross': Decimal('0.00'),
                'vat': Decimal('0.00'), 'net_total': Decimal('0.00'), 'count': 0
            })
            ret_p = daily_purchase_returns_map.get(current_day, {
                'taxable_deduct': Decimal('0.00'), 'vat_deduct': Decimal('0.00'),
                'refund_total': Decimal('0.00')
            })

            net_day_purchase_taxable = max(Decimal('0.00'), raw_p['taxable'] - ret_p['taxable_deduct'])
            net_day_purchase_vat = max(Decimal('0.00'), raw_p['vat'] - ret_p['vat_deduct'])
            net_day_purchase_net = max(Decimal('0.00'), raw_p['net_total'] - ret_p['refund_total'])

            # 4. Net VAT Assessment for current day: Output VAT - Input VAT
            daily_net_vat = (net_day_sales_vat - net_day_purchase_vat).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            cumulative_balance = (cumulative_balance + daily_net_vat).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            has_activity = (
                raw_s['count'] > 0 or raw_p['count'] > 0 or
                ret_s['refund_total'] > Decimal('0.00') or ret_p['refund_total'] > Decimal('0.00')
            )
            if has_activity:
                active_tax_days_count += 1

            row_data = {
                'date_ad': current_day,
                'date_ad_str': current_day.strftime('%Y-%m-%d'),
                'date_bs': date_bs_str,
                'bs_month_name': bs_month_name,
                'day_name': current_day.strftime('%a'),
                'sales_taxable': net_day_sales_taxable,
                'sales_non_taxable': net_day_sales_non_taxable,
                'sales_vat': net_day_sales_vat,
                'sales_grand_total': net_day_sales_grand,
                'sales_count': raw_s['count'],
                'purchase_taxable': net_day_purchase_taxable,
                'purchase_vat': net_day_purchase_vat,
                'purchase_net_total': net_day_purchase_net,
                'purchase_count': raw_p['count'],
                'daily_net_vat': daily_net_vat,
                'is_daily_payable': (daily_net_vat > Decimal('0.00')),
                'is_daily_credit': (daily_net_vat < Decimal('0.00')),
                'cumulative_balance': cumulative_balance,
                'is_cumulative_payable': (cumulative_balance > Decimal('0.00')),
                'is_cumulative_credit': (cumulative_balance < Decimal('0.00')),
                'has_activity': has_activity,
            }
            daily_rows.append(row_data)

            # Accumulate Grand Totals
            period_sales_taxable += net_day_sales_taxable
            period_sales_non_taxable += net_day_sales_non_taxable
            period_sales_vat += net_day_sales_vat
            period_sales_grand += net_day_sales_grand
            period_purchase_taxable += net_day_purchase_taxable
            period_purchase_vat += net_day_purchase_vat
            period_purchase_net += net_day_purchase_net
            period_net_vat += daily_net_vat

            current_day += timedelta(days=1)

        return {
            'daily_rows': daily_rows,
            'is_vat_shop': is_vat_shop,
            'total_days_in_period': days_count,
            'active_tax_days_count': active_tax_days_count,
            'totals': {
                'sales_taxable': period_sales_taxable,
                'sales_non_taxable': period_sales_non_taxable,
                'sales_vat': period_sales_vat,
                'sales_grand_total': period_sales_grand,
                'purchase_taxable': period_purchase_taxable,
                'purchase_vat': period_purchase_vat,
                'purchase_net_total': period_purchase_net,
                'net_vat': period_net_vat,
                'final_cumulative_balance': cumulative_balance,
                'is_net_payable': (period_net_vat > Decimal('0.00')),
                'is_net_credit': (period_net_vat < Decimal('0.00')),
            }
        }