"""
Gross Profit, Markup & Margin Tier Realization Service.
File Path: apps/reports/services/gross_profit_sales_service.py

Capabilities:
1. Reconciles net merchandise revenue against true landed acquisition COGS.
2. Evaluates product-level profitability and classifies each item into 4 Margin Tiers:
   - High Margin (> 25%): Covers, Tempered Glass, Fast Chargers, Lab Repairs
   - Medium Margin (10% to 25%): Earbuds, Smartwatches, Power Banks
   - Slim Margin (3% to 10%): High-turnover official smartphones (iPhone, Galaxy)
   - Negative Margin (< 0%): Loss-making / heavy clearance sales
3. Exposes margin bracket distribution percentages to guide pricing and promotional strategy.
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
from apps.inventory.models import Product, ProductCategory, Brand
from apps.branches.models import Branch
from apps.core.nepali_calendar import NepaliCalendar


class GrossProfitSalesService:
    """
    Business logic engine for Report 12: Gross Profit & Margin Realization Report.
    """

    @classmethod
    def resolve_date_range(cls, raw_params: Dict[str, Any]) -> Tuple[date, date, str, str]:
        """Resolves date boundaries defaulting to current Nepali BS month."""
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
    def get_gross_profit_data(
        cls,
        filters: Dict[str, Any],
        user=None
    ) -> Dict[str, Any]:
        """
        Calculates profit realization across products, groups them by margin tiers,
        and aggregates capital metrics.
        """
        active_branch = getattr(user, 'assigned_branch', None) if user and not user.is_superuser else None

        start_date, end_date, start_date_bs, end_date_bs = cls.resolve_date_range(filters)
        branch_id = filters.get('branch_id') or filters.get('branch', '')
        category_id = filters.get('category_id') or filters.get('category', '')
        brand_id = filters.get('brand_id') or filters.get('brand', '')
        tier_filter = str(filters.get('margin_tier', '') or '').strip().upper()
        search_query = str(filters.get('q', '') or '').strip()

        # 1. Base Query: Only completed sales items
        items_qs = SalesEstimateItem.objects.select_related(
            'product',
            'product__category',
            'product__brand',
            'product__base_unit',
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

        if brand_id and str(brand_id).strip() not in ['', 'all', 'None']:
            items_qs = items_qs.filter(product__brand_id=brand_id)

        if search_query:
            items_qs = items_qs.filter(
                Q(product__name__icontains=search_query) |
                Q(product__sku__icontains=search_query) |
                Q(product__barcode__icontains=search_query)
            )

        # 3. Group by Product ID
        grouped_stats = items_qs.values('product_id').annotate(
            total_qty=Coalesce(Sum('quantity'), Value(Decimal('0.000'), output_field=DecimalField(max_digits=18, decimal_places=3))),
            total_base_qty=Coalesce(Sum('base_unit_quantity'), Value(Decimal('0.000'), output_field=DecimalField(max_digits=18, decimal_places=3))),
            gross_revenue=Coalesce(Sum('line_total'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            tax_sum=Coalesce(Sum('tax_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            discounts_sum=Coalesce(Sum('discount_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
        )
        stats_map = {row['product_id']: row for row in grouped_stats}
        product_ids = list(stats_map.keys())

        # Resolve COGS per product from items_qs
        cogs_items = items_qs.values('product_id', 'cost_price', 'base_unit_quantity')
        cogs_map: Dict[int, Decimal] = {}
        for itm in cogs_items:
            pid = itm['product_id']
            cost = itm['cost_price'] or Decimal('0.00')
            b_qty = itm['base_unit_quantity'] or Decimal('0.000')
            cogs_map[pid] = cogs_map.get(pid, Decimal('0.00')) + (cost * b_qty)

        # 4. Resolve Products
        products_qs = Product.objects.select_related('category', 'brand', 'base_unit').filter(id__in=product_ids)

        records: List[Dict[str, Any]] = []

        total_units_sold = Decimal('0.000')
        total_gross_rev = Decimal('0.00')
        total_net_rev = Decimal('0.00')
        total_cogs_sum = Decimal('0.00')
        total_profit_sum = Decimal('0.00')

        tier_counts = {'HIGH_MARGIN': 0, 'MEDIUM_MARGIN': 0, 'SLIM_MARGIN': 0, 'NEGATIVE_MARGIN': 0}
        tier_profits = {
            'HIGH_MARGIN': Decimal('0.00'),
            'MEDIUM_MARGIN': Decimal('0.00'),
            'SLIM_MARGIN': Decimal('0.00'),
            'NEGATIVE_MARGIN': Decimal('0.00')
        }

        for prod in products_qs:
            s_data = stats_map.get(prod.id, {})
            qty = s_data.get('total_qty', Decimal('0.000'))
            gross = s_data.get('gross_revenue', Decimal('0.00'))
            tax = s_data.get('tax_sum', Decimal('0.00'))
            disc = s_data.get('discounts_sum', Decimal('0.00'))

            net_rev = max(Decimal('0.00'), gross - tax)
            cogs = (cogs_map.get(prod.id, Decimal('0.00'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            profit = (net_rev - cogs).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            margin_pct = (
                ((profit / net_rev) * Decimal('100.00')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
                if net_rev > Decimal('0.00') else Decimal('0.0')
            )

            # Classify into 4 Margin Tiers
            if margin_pct > Decimal('25.0'):
                tier_key = 'HIGH_MARGIN'
                tier_label = 'High (>25%)'
                tier_badge = 'success'
            elif margin_pct >= Decimal('10.0'):
                tier_key = 'MEDIUM_MARGIN'
                tier_label = 'Medium (10-25%)'
                tier_badge = 'primary'
            elif margin_pct >= Decimal('0.0'):
                tier_key = 'SLIM_MARGIN'
                tier_label = 'Slim (0-10%)'
                tier_badge = 'warning'
            else:
                tier_key = 'NEGATIVE_MARGIN'
                tier_label = 'Loss (<0%)'
                tier_badge = 'danger'

            tier_counts[tier_key] += 1
            tier_profits[tier_key] += profit

            if tier_filter and tier_filter not in ['', 'ALL'] and tier_key != tier_filter:
                continue

            total_units_sold += qty
            total_gross_rev += gross
            total_net_rev += net_rev
            total_cogs_sum += cogs
            total_profit_sum += profit

            records.append({
                'product_id': prod.id,
                'name': prod.name,
                'sku': prod.sku,
                'category_name': prod.category.name if prod.category else 'General',
                'brand_name': prod.brand.name if prod.brand else '-',
                'unit_code': prod.base_unit.code if prod.base_unit else 'PCS',
                'is_serialized': bool(prod.requires_imei_tracking or prod.requires_serial_tracking),
                'quantity_sold': qty,
                'gross_revenue': gross,
                'discounts_given': disc,
                'net_revenue': net_rev,
                'cogs_total': cogs,
                'gross_profit': profit,
                'margin_percent': margin_pct,
                'margin_tier': tier_key,
                'tier_label': tier_label,
                'tier_badge': tier_badge,
                'current_mrp': prod.selling_price or Decimal('0.00'),
            })

        # Sort: Highest gross profit first
        records.sort(key=lambda x: x['gross_profit'], reverse=True)

        overall_margin_pct = (
            ((total_profit_sum / total_net_rev) * Decimal('100.00')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
            if total_net_rev > Decimal('0.00') else Decimal('0.0')
        )

        totals = {
            'products_count': len(records),
            'total_units_sold': total_units_sold,
            'total_gross_rev': total_gross_rev,
            'total_net_rev': total_net_rev,
            'total_cogs_sum': total_cogs_sum,
            'total_profit_sum': total_profit_sum,
            'overall_margin_pct': overall_margin_pct,
            'tier_counts': tier_counts,
            'tier_profits': tier_profits,
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
            'brands': Brand.objects.all().order_by('name'),
            'selected_branch': selected_branch,
            'filters': filters,
            'start_date': start_date.strftime('%Y-%m-%d'),
            'end_date': end_date.strftime('%Y-%m-%d'),
            'start_date_bs': start_date_bs,
            'end_date_bs': end_date_bs,
        }