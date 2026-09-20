"""
Product-Wise Sales & Volume Intelligence Service.
File Path: apps/reports/services/product_sales_service.py

Capabilities:
1. Aggregates sales volume, revenue, trade concessions, and margins grouped by catalog product.
2. Computes the average realized unit selling price: (Net Revenue / Total Quantity Sold).
3. Evaluates total landed COGS and realized gross profit per smartphone / accessory.
4. Annotates real-time warehouse stock available to flag fast replenishment needs.
"""

from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime
from typing import Dict, Any, List, Tuple

from django.db.models import (
    Q, Sum, Count, DecimalField, Value
)
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.sales.models import SalesEstimateItem
from apps.inventory.models import Product, ProductCategory, Brand, BranchStock
from apps.branches.models import Branch
from apps.core.nepali_calendar import NepaliCalendar


class ProductSalesService:
    """
    Business logic engine for Report 3: Product-Wise Sales Report.
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
    def get_product_sales_data(
        cls,
        filters: Dict[str, Any],
        user=None
    ) -> Dict[str, Any]:
        """
        Computes volume, net revenue, margins, and stock balances for each sold product.
        """
        active_branch = getattr(user, 'assigned_branch', None) if user and not user.is_superuser else None

        start_date, end_date, start_date_bs, end_date_bs = cls.resolve_date_range(filters)
        branch_id = filters.get('branch_id') or filters.get('branch', '')
        category_id = filters.get('category_id') or filters.get('category', '')
        brand_id = filters.get('brand_id') or filters.get('brand', '')
        tracking_type = str(filters.get('tracking_type', '') or '').strip()
        search_query = str(filters.get('q', '') or '').strip()

        # 1. Base Query: Only items from finalized/completed sales estimates
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

        if tracking_type == 'serialized':
            items_qs = items_qs.filter(
                Q(product__requires_imei_tracking=True) | Q(product__requires_serial_tracking=True)
            )
        elif tracking_type == 'bulk':
            items_qs = items_qs.filter(
                product__requires_imei_tracking=False,
                product__requires_serial_tracking=False
            )

        if search_query:
            items_qs = items_qs.filter(
                Q(product__name__icontains=search_query) |
                Q(product__sku__icontains=search_query) |
                Q(product__barcode__icontains=search_query) |
                Q(product__model_name__icontains=search_query)
            )

        # 3. Aggregate By Product ID
        grouped_stats = items_qs.values('product_id').annotate(
            total_qty=Coalesce(
                Sum('quantity'),
                Value(Decimal('0.000'), output_field=DecimalField(max_digits=18, decimal_places=3))
            ),
            total_base_qty=Coalesce(
                Sum('base_unit_quantity'),
                Value(Decimal('0.000'), output_field=DecimalField(max_digits=18, decimal_places=3))
            ),
            invoices_count=Count('estimate_id', distinct=True),
            gross_revenue=Coalesce(
                Sum('line_total'),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
            item_discounts=Coalesce(
                Sum('item_discount_amount'),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
            allocated_bill_discounts=Coalesce(
                Sum('allocated_bill_discount_amount'),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
            total_discounts=Coalesce(
                Sum('discount_amount'),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
            tax_sum=Coalesce(
                Sum('tax_amount'),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
            taxable_base_sum=Coalesce(
                Sum('base_taxable_amount'),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            )
        )

        stats_map = {row['product_id']: row for row in grouped_stats}
        product_ids = list(stats_map.keys())

        # 4. Resolve Catalog Details
        products_qs = Product.objects.select_related(
            'category', 'brand', 'base_unit'
        ).filter(id__in=product_ids)

        # 5. Pre-query Available Branch Stock across selected branch
        stock_qs = BranchStock.objects.filter(product_id__in=product_ids)
        if branch_id and str(branch_id).strip() not in ['', 'all', 'None']:
            stock_qs = stock_qs.filter(branch_id=branch_id)
        elif active_branch:
            stock_qs = stock_qs.filter(branch=active_branch)

        stock_aggregates = stock_qs.values('product_id').annotate(
            avail_stock=Coalesce(
                Sum('quantity'),
                Value(Decimal('0.000'), output_field=DecimalField(max_digits=18, decimal_places=3))
            )
        )
        stock_map = {row['product_id']: row['avail_stock'] for row in stock_aggregates}

        # 6. Pre-calculate COGS from items_qs to account for exact landed costs
        cogs_items = items_qs.values('product_id', 'cost_price', 'base_unit_quantity')
        cogs_map: Dict[int, Decimal] = {}
        for itm in cogs_items:
            pid = itm['product_id']
            cost = itm['cost_price'] or Decimal('0.00')
            b_qty = itm['base_unit_quantity'] or Decimal('0.000')
            cogs_map[pid] = cogs_map.get(pid, Decimal('0.00')) + (cost * b_qty)

        # 7. Construct Records
        records: List[Dict[str, Any]] = []

        total_units_sold = Decimal('0.000')
        total_gross_rev = Decimal('0.00')
        total_disc_deducted = Decimal('0.00')
        total_net_rev = Decimal('0.00')
        total_cogs_sum = Decimal('0.00')
        total_profit_sum = Decimal('0.00')

        for prod in products_qs:
            s_data = stats_map.get(prod.id, {})
            qty = s_data.get('total_qty', Decimal('0.000'))
            inv_cnt = s_data.get('invoices_count', 0)
            gross = s_data.get('gross_revenue', Decimal('0.00'))
            disc = s_data.get('total_discounts', Decimal('0.00'))
            tax = s_data.get('tax_sum', Decimal('0.00'))

            net_rev = max(Decimal('0.00'), gross - tax)
            cogs = (cogs_map.get(prod.id, Decimal('0.00'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            profit = (net_rev - cogs).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            margin_pct = (
                ((profit / net_rev) * Decimal('100.00')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
                if net_rev > Decimal('0.00') else Decimal('0.0')
            )

            avg_price = (
                (net_rev / qty).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                if qty > Decimal('0.000') else prod.selling_price
            )

            current_stock = stock_map.get(prod.id, Decimal('0.000'))
            is_serialized = bool(prod.requires_imei_tracking or prod.requires_serial_tracking)

            variant_desc = prod.variant_name or ""
            if prod.ram and prod.internal_storage:
                variant_desc = f"{prod.ram}/{prod.internal_storage}"
                if prod.color_variant:
                    variant_desc += f" {prod.color_variant}"

            total_units_sold += qty
            total_gross_rev += gross
            total_disc_deducted += disc
            total_net_rev += net_rev
            total_cogs_sum += cogs
            total_profit_sum += profit

            records.append({
                'product_id': prod.id,
                'product': prod,
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
                'avg_realized_price': avg_price,
                'counter_mrp': prod.selling_price or Decimal('0.00'),
                'available_stock': current_stock,
            })

        # Sort: Highest revenue first
        records.sort(key=lambda x: x['net_revenue'], reverse=True)

        overall_margin_pct = (
            ((total_profit_sum / total_net_rev) * Decimal('100.00')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
            if total_net_rev > Decimal('0.00') else Decimal('0.0')
        )

        totals = {
            'products_count': len(records),
            'total_units_sold': total_units_sold,
            'total_gross_rev': total_gross_rev,
            'total_disc_deducted': total_disc_deducted,
            'total_net_rev': total_net_rev,
            'total_cogs_sum': total_cogs_sum,
            'total_profit_sum': total_profit_sum,
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
            'brands': Brand.objects.all().order_by('name'),
            'selected_branch': selected_branch,
            'filters': filters,
            'start_date': start_date.strftime('%Y-%m-%d'),
            'end_date': end_date.strftime('%Y-%m-%d'),
            'start_date_bs': start_date_bs,
            'end_date_bs': end_date_bs,
        }