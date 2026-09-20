"""
Brand-Wise Sales Turnover & Volume Intelligence Service.
File Path: apps/reports/services/brand_sales_service.py

Capabilities:
1. Aggregates counter turnover, units sold, and profit contributions grouped by Brand
   (Apple, Samsung, Xiaomi, Realme, Vivo, Generic accessories, etc.).
2. Distinguishes handset units sold (IMEI-tracked) from accessory units sold.
3. Computes each brand's revenue share % relative to total store turnover (critical
   for monitoring national brand distributor rebate quotas and targets).
4. Evaluates total landed COGS, realized gross margins, and customer concession totals.
"""

from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime
from typing import Dict, Any, List, Tuple, Optional

from django.db.models import (
    Q, Sum, Count, DecimalField, Value, Case, When, F
)
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.sales.models import SalesEstimateItem
from apps.inventory.models import Brand
from apps.branches.models import Branch
from apps.core.nepali_calendar import NepaliCalendar


class BrandSalesService:
    """
    Business logic engine for Report 4: Brand-Wise Sales Report.
    """

    @classmethod
    def resolve_date_range(cls, raw_params: Dict[str, Any]) -> Tuple[date, date, str, str]:
        """Resolves start and end dates with Nepali BS defaults."""
        today_ad = timezone.now().date()
        start_str = str(raw_params.get('start_date', '') or '').strip()
        end_str = str(raw_params.get('end_date', '') or '').strip()

        start_date = None
        end_date = None

        if start_str:
            try:
                start_date = datetime.strptime(start_str, '%Y-%m-%d').date()
            except (ValueError, TypeError):
                start_date = None

        if end_str:
            try:
                end_date = datetime.strptime(end_str, '%Y-%m-%d').date()
            except (ValueError, TypeError):
                end_date = None

        if not start_date or not end_date:
            bs_year, bs_month, _ = NepaliCalendar.ad_to_bs(today_ad)
            start_of_bs_month = NepaliCalendar.bs_to_ad(bs_year, bs_month, 1)
            start_date = start_date or start_of_bs_month
            end_date = end_date or today_ad

        if start_date > end_date:
            start_date, end_date = end_date, start_date

        start_y, start_m, start_d = NepaliCalendar.ad_to_bs(start_date)
        end_y, end_m, end_d = NepaliCalendar.ad_to_bs(end_date)

        start_date_bs = NepaliCalendar.format_bs(start_y, start_m, start_d, lang='en')
        end_date_bs = NepaliCalendar.format_bs(end_y, end_m, end_d, lang='en')

        return start_date, end_date, start_date_bs, end_date_bs

    @classmethod
    def get_brand_sales_data(
        cls,
        filters: Dict[str, Any],
        user=None
    ) -> Dict[str, Any]:
        """
        Groups sales transactions by product brand and calculates turnover, share %, and margins.
        """
        active_branch = getattr(user, 'assigned_branch', None) if user and not user.is_superuser else None

        start_date, end_date, start_date_bs, end_date_bs = cls.resolve_date_range(filters)
        branch_id = filters.get('branch_id') or filters.get('branch', '')
        brand_id = filters.get('brand_id') or filters.get('brand', '')
        search_query = str(filters.get('q', '') or '').strip()

        # 1. Base Query: Only completed sales lines
        items_qs = SalesEstimateItem.objects.select_related(
            'product',
            'product__brand',
            'estimate'
        ).filter(
            estimate__status__in=['COMPLETED', 'PARTIALLY_RETURNED'],
            estimate__bill_date_ad__gte=start_date,
            estimate__bill_date_ad__lte=end_date
        )

        # 2. Scoping
        if branch_id and str(branch_id).strip() not in ['', 'all', 'None']:
            items_qs = items_qs.filter(estimate__branch_id=branch_id)
        elif active_branch and not (user.is_superuser or getattr(user, 'role', '') == 'OWNER'):
            items_qs = items_qs.filter(estimate__branch=active_branch)

        if brand_id and str(brand_id).strip() not in ['', 'all', 'None']:
            items_qs = items_qs.filter(product__brand_id=brand_id)

        if search_query:
            items_qs = items_qs.filter(
                Q(product__brand__name__icontains=search_query) |
                Q(product__name__icontains=search_query)
            )

        # 3. Aggregate By Brand
        brand_stats = items_qs.values('product__brand_id').annotate(
            total_qty=Coalesce(
                Sum('quantity'),
                Value(Decimal('0.000'), output_field=DecimalField(max_digits=18, decimal_places=3))
            ),
            handset_units=Coalesce(
                Sum(Case(When(product__requires_imei_tracking=True, then=F('quantity')))),
                Value(Decimal('0.000'), output_field=DecimalField(max_digits=18, decimal_places=3))
            ),
            accessory_units=Coalesce(
                Sum(Case(When(product__requires_imei_tracking=False, then=F('quantity')))),
                Value(Decimal('0.000'), output_field=DecimalField(max_digits=18, decimal_places=3))
            ),
            invoices_count=Count('estimate_id', distinct=True),
            gross_sales=Coalesce(
                Sum('line_total'),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
            total_discounts=Coalesce(
                Sum('discount_amount'),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
            tax_sum=Coalesce(
                Sum('tax_amount'),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            )
        )

        brand_map = {row['product__brand_id']: row for row in brand_stats}

        # Resolve COGS per brand
        cogs_items = items_qs.values('product__brand_id', 'cost_price', 'base_unit_quantity')
        cogs_map: Dict[Optional[int], Decimal] = {}
        for itm in cogs_items:
            bid = itm['product__brand_id']
            cost = itm['cost_price'] or Decimal('0.00')
            b_qty = itm['base_unit_quantity'] or Decimal('0.000')
            cogs_map[bid] = cogs_map.get(bid, Decimal('0.00')) + (cost * b_qty)

        # 4. Resolve Brand Names
        known_brands = {b.id: b.name for b in Brand.objects.all()}

        # 5. Compute Grand Net Turnover first to calculate Share %
        total_store_net_turnover = Decimal('0.00')
        for b_id, row in brand_map.items():
            gross = row['gross_sales']
            tax = row['tax_sum']
            net = max(Decimal('0.00'), gross - tax)
            total_store_net_turnover += net

        records: List[Dict[str, Any]] = []

        total_units_sold = Decimal('0.000')
        total_handsets = Decimal('0.000')
        total_accessories = Decimal('0.000')
        total_gross = Decimal('0.00')
        total_discounts = Decimal('0.00')
        total_net = Decimal('0.00')
        total_cogs = Decimal('0.00')
        total_profit = Decimal('0.00')

        for b_id, b_data in brand_map.items():
            b_name = known_brands.get(b_id, "Generic / Unbranded")
            qty = b_data.get('total_qty', Decimal('0.000'))
            h_units = b_data.get('handset_units', Decimal('0.000'))
            a_units = b_data.get('accessory_units', Decimal('0.000'))
            inv_cnt = b_data.get('invoices_count', 0)
            gross = b_data.get('gross_sales', Decimal('0.00'))
            disc = b_data.get('total_discounts', Decimal('0.00'))
            tax = b_data.get('tax_sum', Decimal('0.00'))

            net = max(Decimal('0.00'), gross - tax)
            cogs = (cogs_map.get(b_id, Decimal('0.00'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            profit = (net - cogs).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            margin_pct = (
                ((profit / net) * Decimal('100.00')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
                if net > Decimal('0.00') else Decimal('0.0')
            )

            share_pct = (
                ((net / total_store_net_turnover) * Decimal('100.00')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
                if total_store_net_turnover > Decimal('0.00') else Decimal('0.0')
            )

            total_units_sold += qty
            total_handsets += h_units
            total_accessories += a_units
            total_gross += gross
            total_discounts += disc
            total_net += net
            total_cogs += cogs
            total_profit += profit

            records.append({
                'brand_id': b_id,
                'brand_name': b_name,
                'invoices_count': inv_cnt,
                'handset_units_sold': h_units,
                'accessory_units_sold': a_units,
                'total_units_sold': qty,
                'gross_sales': gross,
                'total_discounts': disc,
                'net_turnover': net,
                'total_cogs': cogs,
                'gross_profit': profit,
                'margin_percent': margin_pct,
                'turnover_share_percent': share_pct,
            })

        # Sort: Highest net turnover first
        records.sort(key=lambda x: x['net_turnover'], reverse=True)

        overall_margin_pct = (
            ((total_profit / total_net) * Decimal('100.00')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
            if total_net > Decimal('0.00') else Decimal('0.0')
        )

        totals = {
            'brands_count': len(records),
            'total_units_sold': total_units_sold,
            'total_handsets': total_handsets,
            'total_accessories': total_accessories,
            'total_gross': total_gross,
            'total_discounts': total_discounts,
            'total_net': total_net,
            'total_cogs': total_cogs,
            'total_profit': total_profit,
            'overall_margin_pct': overall_margin_pct,
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
            'brands': Brand.objects.all().order_by('name'),
            'selected_branch': selected_branch,
            'filters': filters,
            'start_date': start_date.strftime('%Y-%m-%d'),
            'end_date': end_date.strftime('%Y-%m-%d'),
            'start_date_bs': start_date_bs,
            'end_date_bs': end_date_bs,
        }