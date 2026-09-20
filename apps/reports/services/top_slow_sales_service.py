"""
Fast-Moving & Slow-Moving Sales Velocity Intelligence Service.
File Path: apps/reports/services/top_slow_sales_service.py

Capabilities:
1. Fast-Moving / Top Sellers:
   - Ranks products by units sold (volume) and net revenue in the selected date window.
   - Computes realized gross profit, average selling price, and share % of total store sales.
   - Annotates live available showcase stock to avoid stockouts on high-demand items.
2. Slow-Moving / Dormant Sales:
   - Identifies in-stock products with zero or low sales velocity (<= threshold units)
     over 30, 60, 90, 120, or 180-day windows.
   - Quantifies capital stagnation (unsold units * landed acquisition cost).
   - Generates automated clearance action suggestions (markdowns, bundle promos).
"""

from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime, timedelta
from typing import Dict, Any, List, Tuple, Optional

from django.db.models import (
    Q, Sum, Count, F, DecimalField, Value, Case, When
)
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.sales.models import SalesEstimateItem
from apps.inventory.models import Product, ProductCategory, Brand, BranchStock
from apps.branches.models import Branch
from apps.core.nepali_calendar import NepaliCalendar
from apps.core.utils.nepali_date_converter import ad_to_bs_string


class TopSlowSalesService:
    """
    Business analytics engine for Product Sales Velocity (Top Performers & Slow-Moving Items).
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
    def get_sales_velocity_data(
        cls,
        filters: Dict[str, Any],
        user=None
    ) -> Dict[str, Any]:
        """
        Executes sales velocity calculation:
        - Evaluates top-selling products by quantity and revenue.
        - Identifies slow-moving / low-turnover items currently tying up capital on shelves.
        """
        active_branch = getattr(user, 'assigned_branch', None) if user and not user.is_superuser else None

        start_date, end_date, start_date_bs, end_date_bs = cls.resolve_date_range(filters)
        branch_id = filters.get('branch_id') or filters.get('branch', '')
        category_id = filters.get('category_id') or filters.get('category', '')
        brand_id = filters.get('brand_id') or filters.get('brand', '')
        search_query = str(filters.get('q', '') or '').strip()

        try:
            top_limit = max(5, min(100, int(filters.get('top_limit') or filters.get('limit') or 20)))
        except (ValueError, TypeError):
            top_limit = 20

        try:
            slow_threshold_qty = Decimal(str(filters.get('slow_threshold') or 2.0))
        except (ValueError, TypeError):
            slow_threshold_qty = Decimal('2.000')

        # 1. Base Query: Only completed sales items in date range
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

        # 2. Branch & Attribute Scoping
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
                Q(product__barcode__icontains=search_query) |
                Q(product__model_name__icontains=search_query)
            )

        # 3. Aggregate Sales Metrics Grouped by Product ID
        grouped_sales = items_qs.values('product_id').annotate(
            total_qty=Coalesce(Sum('quantity'), Value(Decimal('0.000'), output_field=DecimalField(max_digits=18, decimal_places=3))),
            total_base_qty=Coalesce(Sum('base_unit_quantity'), Value(Decimal('0.000'), output_field=DecimalField(max_digits=18, decimal_places=3))),
            invoice_count=Count('estimate_id', distinct=True),
            gross_revenue=Coalesce(Sum('line_total'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            tax_sum=Coalesce(Sum('tax_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            discounts_sum=Coalesce(Sum('discount_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
        )
        sales_map = {row['product_id']: row for row in grouped_sales}

        # Calculate exact Landed COGS per product from items_qs
        cogs_items = items_qs.values('product_id', 'cost_price', 'base_unit_quantity')
        cogs_map: Dict[int, Decimal] = {}
        for itm in cogs_items:
            pid = itm['product_id']
            cost = itm['cost_price'] or Decimal('0.00')
            b_qty = itm['base_unit_quantity'] or Decimal('0.000')
            cogs_map[pid] = cogs_map.get(pid, Decimal('0.00')) + (cost * b_qty)

        # 4. Resolve Live Branch Stock Balances
        stock_qs = BranchStock.objects.select_related(
            'product',
            'product__category',
            'product__brand',
            'product__base_unit',
            'branch'
        )
        if branch_id and str(branch_id).strip() not in ['', 'all', 'None']:
            stock_qs = stock_qs.filter(branch_id=branch_id)
        elif active_branch:
            stock_qs = stock_qs.filter(branch=active_branch)

        if category_id and str(category_id).strip() not in ['', 'all', 'None']:
            stock_qs = stock_qs.filter(product__category_id=category_id)

        if brand_id and str(brand_id).strip() not in ['', 'all', 'None']:
            stock_qs = stock_qs.filter(product__brand_id=brand_id)

        # Build map of on-hand inventory per product
        stock_map: Dict[int, Decimal] = {}
        in_stock_products_set = set()
        for bs in stock_qs:
            pid = bs.product_id
            stock_map[pid] = stock_map.get(pid, Decimal('0.000')) + (bs.quantity or Decimal('0.000'))
            if bs.quantity > Decimal('0.000'):
                in_stock_products_set.add(pid)

        # 5. Fetch Product Models
        all_relevant_pids = set(sales_map.keys()) | in_stock_products_set
        products_qs = Product.objects.select_related('category', 'brand', 'base_unit').filter(id__in=all_relevant_pids)
        product_obj_map = {p.id: p for p in products_qs}

        # Grand store net turnover for share % calculation
        total_store_net_revenue = sum(
            max(Decimal('0.00'), r['gross_revenue'] - r['tax_sum']) for r in sales_map.values()
        )

        # ---------------------------------------------------------------------
        # 6. BUILD TOP-SELLING PERFORMERS LIST
        # ---------------------------------------------------------------------
        top_candidates = []
        for pid, s_data in sales_map.items():
            prod = product_obj_map.get(pid)
            if not prod:
                continue

            qty = s_data['total_qty']
            gross = s_data['gross_revenue']
            tax = s_data['tax_sum']
            disc = s_data['discounts_sum']
            inv_cnt = s_data['invoice_count']

            net_rev = max(Decimal('0.00'), gross - tax)
            cogs = cogs_map.get(pid, Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            profit = (net_rev - cogs).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            margin_pct = (
                ((profit / net_rev) * Decimal('100.00')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
                if net_rev > Decimal('0.00') else Decimal('0.0')
            )
            revenue_share_pct = (
                ((net_rev / total_store_net_revenue) * Decimal('100.00')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
                if total_store_net_revenue > Decimal('0.00') else Decimal('0.0')
            )
            avg_selling_price = (
                (net_rev / qty).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                if qty > Decimal('0.000') else prod.selling_price
            )

            current_stock = stock_map.get(pid, Decimal('0.000'))
            is_serialized = bool(prod.requires_imei_tracking or prod.requires_serial_tracking)

            variant_desc = prod.variant_name or ""
            if prod.ram and prod.internal_storage:
                variant_desc = f"{prod.ram}/{prod.internal_storage}"
                if prod.color_variant:
                    variant_desc += f" {prod.color_variant}"

            top_candidates.append({
                'product_id': prod.id,
                'name': prod.name,
                'sku': prod.sku,
                'barcode': prod.barcode or '',
                'category_name': prod.category.name if prod.category else 'General',
                'brand_name': prod.brand.name if prod.brand else '-',
                'variant_specs': variant_desc,
                'unit_code': prod.base_unit.code if prod.base_unit else 'PCS',
                'is_serialized': is_serialized,
                'quantity_sold': qty,
                'invoices_count': inv_cnt,
                'gross_revenue': gross,
                'total_discounts': disc,
                'net_revenue': net_rev,
                'cogs_total': cogs,
                'gross_profit': profit,
                'margin_percent': margin_pct,
                'revenue_share_percent': revenue_share_pct,
                'avg_selling_price': avg_selling_price,
                'current_mrp': prod.selling_price or Decimal('0.00'),
                'current_stock_on_hand': current_stock,
                'stock_status': 'IN_STOCK' if current_stock > Decimal('5.000') else ('LOW_STOCK' if current_stock > 0 else 'OUT_OF_STOCK'),
            })

        # Rank Top Performers by Volume (Quantity Sold)
        top_by_volume = sorted(top_candidates, key=lambda x: x['quantity_sold'], reverse=True)[:top_limit]
        # Rank Top Performers by Revenue (Net Sales)
        top_by_revenue = sorted(top_candidates, key=lambda x: x['net_revenue'], reverse=True)[:top_limit]

        # ---------------------------------------------------------------------
        # 7. BUILD SLOW-SELLING & DORMANT STOCK LIST
        # ---------------------------------------------------------------------
        slow_selling_records = []
        total_dormant_capital = Decimal('0.00')
        total_slow_units_on_shelf = Decimal('0.000')

        for pid in in_stock_products_set:
            prod = product_obj_map.get(pid)
            if not prod:
                continue

            on_hand_qty = stock_map.get(pid, Decimal('0.000'))
            if on_hand_qty <= Decimal('0.000'):
                continue

            s_data = sales_map.get(pid)
            qty_sold = s_data['total_qty'] if s_data else Decimal('0.000')
            net_revenue = max(Decimal('0.00'), s_data['gross_revenue'] - s_data['tax_sum']) if s_data else Decimal('0.00')

            # Filter criterion: Sold <= threshold in the selected time window
            if qty_sold <= slow_threshold_qty:
                cost_rate = prod.purchase_price or Decimal('0.00')
                selling_rate = prod.selling_price or Decimal('0.00')
                tied_capital = (on_hand_qty * cost_rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                potential_retail = (on_hand_qty * selling_rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

                total_dormant_capital += tied_capital
                total_slow_units_on_shelf += on_hand_qty

                # Recommend commercial clearance action
                if qty_sold == Decimal('0.000'):
                    recommendation = "Zero Velocity: Initiate Clearance / Bundle Offer (10-15% Off)"
                    rec_badge = "danger"
                else:
                    recommendation = "Low Velocity: Counter Promotion / Move to Front Showcase"
                    rec_badge = "warning"

                variant_desc = prod.variant_name or ""
                if prod.ram and prod.internal_storage:
                    variant_desc = f"{prod.ram}/{prod.internal_storage}"
                    if prod.color_variant:
                        variant_desc += f" {prod.color_variant}"

                slow_selling_records.append({
                    'product_id': prod.id,
                    'name': prod.name,
                    'sku': prod.sku,
                    'barcode': prod.barcode or '',
                    'category_name': prod.category.name if prod.category else 'General',
                    'brand_name': prod.brand.name if prod.brand else '-',
                    'variant_specs': variant_desc,
                    'unit_code': prod.base_unit.code if prod.base_unit else 'PCS',
                    'is_serialized': bool(prod.requires_imei_tracking or prod.requires_serial_tracking),
                    'quantity_sold_in_period': qty_sold,
                    'net_revenue_in_period': net_revenue,
                    'on_hand_stock': on_hand_qty,
                    'cost_price': cost_rate,
                    'selling_price': selling_rate,
                    'tied_capital_cost': tied_capital,
                    'potential_retail_value': potential_retail,
                    'recommendation': recommendation,
                    'rec_badge': rec_badge,
                    'rack_number': prod.rack_number or '-',
                })

        # Sort Slow Sellers: Highest tied-up capital first
        slow_selling_records.sort(key=lambda x: x['tied_capital_cost'], reverse=True)

        # ---------------------------------------------------------------------
        # 8. SUMMARY TOTALS
        # ---------------------------------------------------------------------
        totals = {
            'top_by_volume_count': len(top_by_volume),
            'top_by_revenue_count': len(top_by_revenue),
            'slow_selling_count': len(slow_selling_records),
            'total_dormant_capital': total_dormant_capital,
            'total_slow_units_on_shelf': total_slow_units_on_shelf,
            'total_store_net_revenue': total_store_net_revenue,
            'slow_threshold_applied': slow_threshold_qty,
            'top_limit_applied': top_limit,
        }

        selected_branch = None
        if branch_id and branch_id != 'all':
            selected_branch = Branch.objects.filter(id=branch_id).first()
        elif active_branch:
            selected_branch = active_branch

        return {
            'top_by_volume': top_by_volume,
            'top_by_revenue': top_by_revenue,
            'slow_selling': slow_selling_records,
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