"""
Category-Wise Departmental Sales Intelligence Service.
File Path: apps/reports/services/category_sales_service.py

Capabilities:
1. Evaluates sales turnover and gross margins across store product categories:
   Smartphones, Fast Chargers & Adapters, Glass & Back Covers, Audio/AirPods, Repair Parts, Eyewear.
2. Quantifies volume, gross billing, line concessions, net turnover, and COGS.
3. Computes the revenue contribution % of each merchandise category.
4. Identifies high-margin departments vs. high-volume low-margin categories.
"""

from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime
from typing import Dict, Any, List, Tuple, Optional

from django.db.models import (
    Q, Sum, Count, DecimalField, Value
)
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.sales.models import SalesEstimateItem
from apps.inventory.models import ProductCategory
from apps.branches.models import Branch
from apps.core.nepali_calendar import NepaliCalendar


class CategorySalesService:
    """
    Business logic engine for Report 5: Category-Wise Sales Report.
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
    def get_category_sales_data(
        cls,
        filters: Dict[str, Any],
        user=None
    ) -> Dict[str, Any]:
        """
        Groups sales transactions by product category and evaluates departmental revenue and margins.
        """
        active_branch = getattr(user, 'assigned_branch', None) if user and not user.is_superuser else None

        start_date, end_date, start_date_bs, end_date_bs = cls.resolve_date_range(filters)
        branch_id = filters.get('branch_id') or filters.get('branch', '')
        category_id = filters.get('category_id') or filters.get('category', '')
        search_query = str(filters.get('q', '') or '').strip()

        # 1. Base Query: Only completed sales lines
        items_qs = SalesEstimateItem.objects.select_related(
            'product',
            'product__category',
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

        if category_id and str(category_id).strip() not in ['', 'all', 'None']:
            items_qs = items_qs.filter(product__category_id=category_id)

        if search_query:
            items_qs = items_qs.filter(
                Q(product__category__name__icontains=search_query) |
                Q(product__name__icontains=search_query)
            )

        # 3. Aggregate By Category
        cat_stats = items_qs.values('product__category_id').annotate(
            total_qty=Coalesce(
                Sum('quantity'),
                Value(Decimal('0.000'), output_field=DecimalField(max_digits=18, decimal_places=3))
            ),
            lines_count=Count('id'),
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

        cat_map = {row['product__category_id']: row for row in cat_stats}

        # Resolve COGS per category
        cogs_items = items_qs.values('product__category_id', 'cost_price', 'base_unit_quantity')
        cogs_map: Dict[Optional[int], Decimal] = {}
        for itm in cogs_items:
            cid = itm['product__category_id']
            cost = itm['cost_price'] or Decimal('0.00')
            b_qty = itm['base_unit_quantity'] or Decimal('0.000')
            cogs_map[cid] = cogs_map.get(cid, Decimal('0.00')) + (cost * b_qty)

        # 4. Resolve Category Metadata
        known_cats = {c.id: {'name': c.name, 'code': c.code, 'name_np': c.name_np} for c in ProductCategory.objects.all()}

        # 5. Compute Grand Net Turnover first to calculate Contribution %
        total_store_net_turnover = Decimal('0.00')
        for c_id, row in cat_map.items():
            gross = row['gross_sales']
            tax = row['tax_sum']
            net = max(Decimal('0.00'), gross - tax)
            total_store_net_turnover += net

        records: List[Dict[str, Any]] = []

        total_units_sold = Decimal('0.000')
        total_lines_count = 0
        total_gross = Decimal('0.00')
        total_discounts = Decimal('0.00')
        total_net = Decimal('0.00')
        total_cogs = Decimal('0.00')
        total_profit = Decimal('0.00')

        for c_id, c_data in cat_map.items():
            cat_meta = known_cats.get(c_id, {'name': 'General / Unclassified', 'code': 'GEN', 'name_np': ''})
            c_name = cat_meta['name']
            c_code = cat_meta['code']
            c_np = cat_meta['name_np']

            qty = c_data.get('total_qty', Decimal('0.000'))
            l_cnt = c_data.get('lines_count', 0)
            inv_cnt = c_data.get('invoices_count', 0)
            gross = c_data.get('gross_sales', Decimal('0.00'))
            disc = c_data.get('total_discounts', Decimal('0.00'))
            tax = c_data.get('tax_sum', Decimal('0.00'))

            net = max(Decimal('0.00'), gross - tax)
            cogs = (cogs_map.get(c_id, Decimal('0.00'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            profit = (net - cogs).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            margin_pct = (
                ((profit / net) * Decimal('100.00')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
                if net > Decimal('0.00') else Decimal('0.0')
            )

            contrib_pct = (
                ((net / total_store_net_turnover) * Decimal('100.00')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
                if total_store_net_turnover > Decimal('0.00') else Decimal('0.0')
            )

            total_units_sold += qty
            total_lines_count += l_cnt
            total_gross += gross
            total_discounts += disc
            total_net += net
            total_cogs += cogs
            total_profit += profit

            records.append({
                'category_id': c_id,
                'category_name': c_name,
                'category_code': c_code,
                'category_name_np': c_np,
                'lines_count': l_cnt,
                'invoices_count': inv_cnt,
                'total_units_sold': qty,
                'gross_sales': gross,
                'total_discounts': disc,
                'net_turnover': net,
                'total_cogs': cogs,
                'gross_profit': profit,
                'margin_percent': margin_pct,
                'contribution_percent': contrib_pct,
            })

        # Sort: Highest net turnover first
        records.sort(key=lambda x: x['net_turnover'], reverse=True)

        overall_margin_pct = (
            ((total_profit / total_net) * Decimal('100.00')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
            if total_net > Decimal('0.00') else Decimal('0.0')
        )

        totals = {
            'categories_count': len(records),
            'total_units_sold': total_units_sold,
            'total_lines_count': total_lines_count,
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
            'categories': ProductCategory.objects.filter(is_active=True).order_by('name'),
            'selected_branch': selected_branch,
            'filters': filters,
            'start_date': start_date.strftime('%Y-%m-%d'),
            'end_date': end_date.strftime('%Y-%m-%d'),
            'start_date_bs': start_date_bs,
            'end_date_bs': end_date_bs,
        }