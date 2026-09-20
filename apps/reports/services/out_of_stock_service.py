"""
Out of Stock (Zero Inventory) Business Logic Service.
File Path: apps/reports/services/out_of_stock_service.py

Detects exhausted stock items (quantity <= 0), measures stockout duration
in elapsed days since the last sale via StockMovementLog, evaluates potential
lost retail revenue, and identifies critical depleted smartphone handsets.
"""

from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Any, List
from django.db.models import F, Q, Sum, Count, Max
from django.utils import timezone

from apps.inventory.models import BranchStock, Product, StockMovementLog
from apps.branches.models import Branch


class OutOfStockService:
    """
    Business logic service for the Out of Stock / Zero Inventory Report.
    """

    @classmethod
    def get_out_of_stock_data(
        cls,
        filters: Dict[str, Any],
        user=None
    ) -> Dict[str, Any]:
        """
        Calculates exhausted inventory items with optimized batch-scanned last sale dates.
        """
        active_branch = getattr(user, 'assigned_branch', None) if user and not user.is_superuser else None

        branch_id = filters.get('branch_id') or filters.get('branch', '')
        category_id = filters.get('category_id') or filters.get('category', '')
        brand_id = filters.get('brand_id') or filters.get('brand', '')
        search_query = filters.get('q', '').strip()

        # 1. Base Query: Zero or negative physical quantity
        qs = BranchStock.objects.select_related(
            'product',
            'product__category',
            'product__brand',
            'product__base_unit',
            'branch'
        ).filter(quantity__lte=Decimal('0.000'))

        # 2. Apply Filters
        if branch_id and str(branch_id).strip() not in ['', 'all', 'None']:
            qs = qs.filter(branch_id=branch_id)
        elif active_branch:
            qs = qs.filter(branch=active_branch)

        if category_id and str(category_id).strip() not in ['', 'all', 'None']:
            qs = qs.filter(product__category_id=category_id)

        if brand_id and str(brand_id).strip() not in ['', 'all', 'None']:
            qs = qs.filter(product__brand_id=brand_id)

        if search_query:
            qs = qs.filter(
                Q(product__name__icontains=search_query) |
                Q(product__sku__icontains=search_query) |
                Q(product__barcode__icontains=search_query) |
                Q(product__model_name__icontains=search_query) |
                Q(product__rack_number__icontains=search_query)
            )

        today = timezone.now().date()
        product_ids = list(qs.values_list('product_id', flat=True).distinct())

        # 3. High-Performance Batch Lookup: Last sale date across all products
        last_sales_qs = StockMovementLog.objects.filter(
            product_id__in=product_ids,
            movement_type__in=['SALE', 'TRADE_IN_SALE']
        )
        if branch_id and str(branch_id).strip() not in ['', 'all', 'None']:
            last_sales_qs = last_sales_qs.filter(branch_id=branch_id)
        elif active_branch:
            last_sales_qs = last_sales_qs.filter(branch=active_branch)

        last_sales_map: Dict[int, Any] = {}
        for row in last_sales_qs.values('product_id').annotate(last_date=Max('created_at')):
            if row['last_date']:
                last_sales_map[row['product_id']] = row['last_date'].date()

        # 4. Construct Item Details
        items_data: List[Dict[str, Any]] = []
        total_phones_out = 0
        potential_lost_revenue = Decimal('0.00')
        longest_depleted_days = 0

        for bs in qs.order_by('product__name'):
            product = bs.product
            last_sold_date = last_sales_map.get(product.id)

            if last_sold_date:
                days_out = max(0, (today - last_sold_date).days)
                if days_out > longest_depleted_days:
                    longest_depleted_days = days_out
            else:
                days_out = None

            is_phone = bool(product.requires_imei_tracking or product.requires_serial_tracking)
            if is_phone:
                total_phones_out += 1

            selling_rate = product.selling_price or Decimal('0.00')
            potential_lost_revenue += selling_rate

            items_data.append({
                'stock_id': bs.id,
                'product': product,
                'branch': bs.branch,
                'last_sold_date': last_sold_date,
                'days_out_of_stock': days_out,
                'cost_price': product.purchase_price or Decimal('0.00'),
                'selling_price': selling_rate,
                'is_serialized': is_phone,
                'rack_number': product.rack_number or '-',
                'shelf_identifier': product.shelf_identifier or '-',
                'unit_code': product.base_unit.code if product.base_unit else 'PCS',
            })

        return {
            'records': items_data,
            'total_zero_stock_items': len(items_data),
            'total_phones_out': total_phones_out,
            'potential_lost_revenue': potential_lost_revenue,
            'longest_depleted_days': longest_depleted_days,
        }