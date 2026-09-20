"""
Low Stock & Reorder Intelligence Business Logic Service.
File Path: apps/reports/services/low_stock_service.py

Calculates products reaching or falling below their minimum reorder thresholds,
evaluates suggested replenishment orders using the standard retail formula:
    Suggested Order = (2 * low_stock_threshold) - current_quantity
and retrieves the last active supplier from inward Goods Received Notes (GRN).
"""

from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Any, List, Optional
from django.db.models import F, Q, Sum, Count, Max
from django.utils import timezone

from apps.inventory.models import BranchStock, Product, ProductCategory, Brand
from apps.purchases.models import GRNItem
from apps.branches.models import Branch


class LowStockService:
    """
    Business logic service for the Low Stock & Reorder Alert Report.
    """

    @classmethod
    def get_low_stock_data(
        cls,
        filters: Dict[str, Any],
        user=None
    ) -> Dict[str, Any]:
        """
        Queries and computes low-stock items with intelligent reorder suggestions,
        value calculations, and supplier mappings.
        """
        active_branch = getattr(user, 'assigned_branch', None) if user and not user.is_superuser else None

        branch_id = filters.get('branch_id') or filters.get('branch', '')
        category_id = filters.get('category_id') or filters.get('category', '')
        brand_id = filters.get('brand_id') or filters.get('brand', '')
        search_query = filters.get('q', '').strip()

        # 1. Base Query: Stocks below or equal to threshold and strictly positive
        qs = BranchStock.objects.select_related(
            'product',
            'product__category',
            'product__brand',
            'product__base_unit',
            'branch'
        ).filter(
            quantity__lte=F('low_stock_threshold'),
            quantity__gt=Decimal('0.000')
        )

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

        # 3. Optimize Supplier Lookup: Batch-query latest suppliers to eliminate N+1
        product_ids = list(qs.values_list('product_id', flat=True).distinct())
        recent_grns = GRNItem.objects.filter(
            product_id__in=product_ids
        ).select_related('grn__supplier').order_by('product_id', '-created_at')

        supplier_map: Dict[int, str] = {}
        for g_item in recent_grns:
            if g_item.product_id not in supplier_map and g_item.grn and g_item.grn.supplier:
                supplier_map[g_item.product_id] = g_item.grn.supplier.company_name

        # 4. Construct Item Details and Math Calculations
        items_data: List[Dict[str, Any]] = []
        total_reorder_units = Decimal('0.000')
        total_reorder_cost = Decimal('0.00')

        for bs in qs.order_by('quantity', 'product__name'):
            product = bs.product
            current_qty = bs.quantity
            threshold = bs.low_stock_threshold or Decimal('5.00')

            # Standard procurement formula: (2 * threshold) - current_quantity
            suggested_order = max(Decimal('1.000'), (threshold * Decimal('2.000')) - current_qty)
            cost_rate = product.purchase_price or Decimal('0.00')
            est_cost = (suggested_order * cost_rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            total_reorder_units += suggested_order
            total_reorder_cost += est_cost

            items_data.append({
                'stock_id': bs.id,
                'product': product,
                'branch': bs.branch,
                'current_qty': current_qty,
                'threshold': threshold,
                'suggested_reorder_qty': suggested_order,
                'cost_price': cost_rate,
                'selling_price': product.selling_price or Decimal('0.00'),
                'estimated_reorder_cost': est_cost,
                'last_supplier': supplier_map.get(product.id, 'Direct Purchase / Unassigned'),
                'rack_number': product.rack_number or '-',
                'shelf_identifier': product.shelf_identifier or '-',
                'unit_code': product.base_unit.code if product.base_unit else 'PCS',
                'is_serialized': bool(product.requires_imei_tracking or product.requires_serial_tracking),
            })

        return {
            'records': items_data,
            'total_low_stock_items': len(items_data),
            'total_reorder_units': total_reorder_units,
            'total_reorder_cost': total_reorder_cost,
        }