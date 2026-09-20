"""
Dead & Slow-Moving Inventory Business Logic Service.
File Path: apps/reports/services/slow_moving_service.py

Capabilities:
1. Queries all products with active positive warehouse stock (quantity > 0).
2. Cross-references sales history via SalesEstimateItem to identify items with zero sales
   over a user-selected threshold window (30, 60, 90, 120, or 180 days; default 60).
3. Distinguishes between:
   - "Never Sold" (Stock received but zero sales ever logged)
   - "Dormant / Stale" (Last sale date is older than the chosen cutoff date)
4. Evaluates total dormant capital tied up at landed acquisition cost vs potential retail value.
5. Suggests proactive markdown clearance actions (e.g. 5-15% discount, bundle promotions).
"""

from decimal import Decimal, ROUND_HALF_UP
from datetime import date, timedelta
from typing import Dict, Any, List, Optional
from django.db.models import Q, F, Max
from django.utils import timezone

from apps.inventory.models import BranchStock, Product, ProductCategory, Brand
from apps.sales.models import SalesEstimateItem
from apps.branches.models import Branch
from apps.core.utils.nepali_date_converter import ad_to_bs_string


class SlowMovingStockService:
    """
    Business logic engine for Report 11: Dead & Slow-Moving Stock Report.
    """

    @classmethod
    def get_slow_moving_data(
        cls,
        filters: Dict[str, Any],
        user=None
    ) -> Dict[str, Any]:
        """
        Calculates dead and slow-moving items exceeding the inactivity threshold.
        """
        active_branch = getattr(user, 'assigned_branch', None) if user and not user.is_superuser else None

        branch_id = filters.get('branch_id') or filters.get('branch', '')
        category_id = filters.get('category_id') or filters.get('category', '')
        brand_id = filters.get('brand_id') or filters.get('brand', '')
        inactivity_type = str(filters.get('inactivity_type', '') or '').strip().lower()
        search_query = str(filters.get('q', '') or '').strip()

        # Parse threshold window (default 60 days)
        try:
            days_threshold = int(filters.get('days') or filters.get('days_threshold') or 60)
            if days_threshold not in [30, 60, 90, 120, 180]:
                days_threshold = 60
        except (ValueError, TypeError):
            days_threshold = 60

        today = timezone.now().date()
        cutoff_date = today - timedelta(days=days_threshold)

        # 1. Base Query: Only items currently sitting in stock (quantity > 0)
        stock_qs = BranchStock.objects.select_related(
            'product',
            'product__category',
            'product__brand',
            'product__base_unit',
            'branch'
        ).filter(quantity__gt=Decimal('0.000'))

        # 2. Scoping
        if branch_id and str(branch_id).strip() not in ['', 'all', 'None']:
            stock_qs = stock_qs.filter(branch_id=branch_id)
        elif active_branch and not (user.is_superuser or getattr(user, 'role', '') == 'OWNER'):
            stock_qs = stock_qs.filter(branch=active_branch)

        if category_id and str(category_id).strip() not in ['', 'all', 'None']:
            stock_qs = stock_qs.filter(product__category_id=category_id)

        if brand_id and str(brand_id).strip() not in ['', 'all', 'None']:
            stock_qs = stock_qs.filter(product__brand_id=brand_id)

        if search_query:
            stock_qs = stock_qs.filter(
                Q(product__name__icontains=search_query) |
                Q(product__sku__icontains=search_query) |
                Q(product__barcode__icontains=search_query) |
                Q(product__model_name__icontains=search_query) |
                Q(product__rack_number__icontains=search_query)
            )

        # 3. High-Performance Batch Lookup: Identify products sold since cutoff_date
        active_sold_product_ids = set(
            SalesEstimateItem.objects.filter(
                estimate__bill_date_ad__gte=cutoff_date,
                estimate__status__in=['COMPLETED', 'PARTIALLY_RETURNED']
            ).values_list('product_id', flat=True).distinct()
        )

        # Products with stock but NO sales since cutoff_date are candidates
        candidate_stocks = [bs for bs in stock_qs if bs.product_id not in active_sold_product_ids]
        candidate_product_ids = [bs.product_id for bs in candidate_stocks]

        # 4. Batch query historical last sale dates for candidate products
        last_sales_map: Dict[int, date] = {}
        if candidate_product_ids:
            hist_sales = SalesEstimateItem.objects.filter(
                product_id__in=candidate_product_ids,
                estimate__status__in=['COMPLETED', 'PARTIALLY_RETURNED']
            ).values('product_id').annotate(last_date=Max('estimate__bill_date_ad'))

            for row in hist_sales:
                if row['last_date']:
                    last_sales_map[row['product_id']] = row['last_date']

        # 5. Build Itemized Records
        records: List[Dict[str, Any]] = []
        total_dormant_capital = Decimal('0.00')
        total_dormant_units = Decimal('0.000')
        total_potential_retail = Decimal('0.00')
        never_sold_count = 0
        dormant_days_sum = 0
        items_with_history_count = 0

        for bs in candidate_stocks:
            product = bs.product
            qty = bs.quantity
            cost_rate = product.purchase_price or Decimal('0.00')
            selling_rate = product.selling_price or Decimal('0.00')

            tied_capital = (qty * cost_rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            potential_retail = (qty * selling_rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            last_sold_date = last_sales_map.get(product.id)

            if last_sold_date:
                days_dormant = max(0, (today - last_sold_date).days)
                dormancy_code = 'DORMANT'
                dormancy_badge = 'danger' if days_dormant >= 90 else 'warning'
                dormancy_label = f"{days_dormant} Days Since Sale"
                last_sold_ad_str = last_sold_date.strftime('%Y-%m-%d')
                last_sold_bs_str = ad_to_bs_string(last_sold_date, lang='en')
                dormant_days_sum += days_dormant
                items_with_history_count += 1
            else:
                days_dormant = None
                dormancy_code = 'NEVER_SOLD'
                dormancy_badge = 'purple'
                dormancy_label = "Never Sold"
                last_sold_ad_str = "No Sales Logged"
                last_sold_bs_str = "-"
                never_sold_count += 1

            # Inactivity filter check
            if inactivity_type == 'never_sold' and dormancy_code != 'NEVER_SOLD':
                continue
            if inactivity_type == 'dormant' and dormancy_code != 'DORMANT':
                continue

            # Suggested Clearance Action
            if dormancy_code == 'NEVER_SOLD' or (days_dormant and days_dormant >= 90):
                recommendation = "Clearance Sale / 10-15% Markdown"
                rec_badge = "danger"
            elif days_dormant and days_dormant >= 60:
                recommendation = "Bundle Offer / 5% Counter Concession"
                rec_badge = "warning"
            else:
                recommendation = "Promote on POS / Reposition Showcase"
                rec_badge = "info"

            total_dormant_capital += tied_capital
            total_dormant_units += qty
            total_potential_retail += potential_retail

            records.append({
                'stock_id': bs.id,
                'product': product,
                'name': product.name,
                'sku': product.sku,
                'barcode': product.barcode or '',
                'branch': bs.branch,
                'branch_name': bs.branch.name,
                'quantity': qty,
                'unit_code': product.base_unit.code if product.base_unit else 'PCS',
                'cost_price': cost_rate,
                'selling_price': selling_rate,
                'tied_capital': tied_capital,
                'potential_retail': potential_retail,
                'last_sold_date': last_sold_date,
                'last_sold_ad_str': last_sold_ad_str,
                'last_sold_bs_str': last_sold_bs_str,
                'days_dormant': days_dormant,
                'dormancy_code': dormancy_code,
                'dormancy_badge': dormancy_badge,
                'dormancy_label': dormancy_label,
                'recommendation': recommendation,
                'rec_badge': rec_badge,
                'rack_number': product.rack_number or '-',
                'shelf_identifier': product.shelf_identifier or '-',
                'is_serialized': bool(product.requires_imei_tracking or product.requires_serial_tracking),
            })

        # Sort: Highest tied capital first
        records.sort(key=lambda x: x['tied_capital'], reverse=True)

        avg_dormant_days = (
            round(dormant_days_sum / items_with_history_count, 1)
            if items_with_history_count > 0 else days_threshold
        )

        totals = {
            'total_slow_items': len(records),
            'total_dormant_units': total_dormant_units,
            'total_dormant_capital': total_dormant_capital,
            'total_potential_retail': total_potential_retail,
            'projected_markup': max(Decimal('0.00'), total_potential_retail - total_dormant_capital),
            'never_sold_count': never_sold_count,
            'avg_dormant_days': avg_dormant_days,
            'days_threshold': days_threshold,
        }

        selected_branch = None
        if branch_id and branch_id != 'all':
            selected_branch = Branch.objects.filter(id=branch_id).first()
        elif active_branch:
            selected_branch = active_branch

        return {
            'records': records,
            'totals': totals,
            'days_threshold': days_threshold,
            'branches': Branch.objects.filter(is_active=True).order_by('-is_main_branch', 'name'),
            'categories': ProductCategory.objects.filter(is_active=True).order_by('name'),
            'brands': Brand.objects.all().order_by('name'),
            'selected_branch': selected_branch,
            'filters': filters,
        }