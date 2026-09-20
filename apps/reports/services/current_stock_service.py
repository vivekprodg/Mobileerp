"""
Current Stock Snapshot Business Logic Service.
File Path: apps/reports/services/current_stock_service.py

Executes high-speed real-time snapshot queries on active warehouse stock,
evaluating live quantities, reserved orders, defective quarantine allocations,
and dynamic landed-cost & retail asset valuations without historical date-range loops.
"""

from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Any, List
from django.db.models import (
    F, Q, Sum, Count, DecimalField, ExpressionWrapper, Value, Case, When
)
from django.db.models.functions import Coalesce

from apps.inventory.models import BranchStock, Product, ProductCategory, Brand
from apps.branches.models import Branch


class CurrentStockService:
    """
    Business logic service for the Current Stock Snapshot Report.
    """

    @classmethod
    def get_current_stock_data(
        cls,
        filters: Dict[str, Any],
        user=None
    ) -> Dict[str, Any]:
        """
        Queries live BranchStock with annotated financial asset valuations and aggregates.
        """
        active_branch = getattr(user, 'assigned_branch', None) if user and not user.is_superuser else None

        branch_id = filters.get('branch_id') or filters.get('branch', '')
        category_id = filters.get('category_id') or filters.get('category', '')
        brand_id = filters.get('brand_id') or filters.get('brand', '')
        status_filter = str(filters.get('status', '') or '').strip().lower()
        search_query = str(filters.get('q', '') or '').strip()

        # 1. Base Queryset
        qs = BranchStock.objects.select_related(
            'product',
            'product__category',
            'product__brand',
            'product__base_unit',
            'branch'
        )

        # 2. Apply Branch Scoping
        if branch_id and str(branch_id).strip() not in ['', 'all', 'None']:
            qs = qs.filter(branch_id=branch_id)
        elif active_branch:
            qs = qs.filter(branch=active_branch)

        # 3. Apply Category & Brand
        if category_id and str(category_id).strip() not in ['', 'all', 'None']:
            qs = qs.filter(product__category_id=category_id)

        if brand_id and str(brand_id).strip() not in ['', 'all', 'None']:
            qs = qs.filter(product__brand_id=brand_id)

        # 4. Search Filter
        if search_query:
            qs = qs.filter(
                Q(product__name__icontains=search_query) |
                Q(product__sku__icontains=search_query) |
                Q(product__barcode__icontains=search_query) |
                Q(product__model_name__icontains=search_query) |
                Q(product__rack_number__icontains=search_query)
            )

        # 5. Stock Level Status Filter
        if status_filter in ['in_stock', 'healthy']:
            qs = qs.filter(quantity__gt=F('low_stock_threshold'))
        elif status_filter in ['low_stock', 'low']:
            qs = qs.filter(quantity__gt=Decimal('0.000'), quantity__lte=F('low_stock_threshold'))
        elif status_filter in ['out_of_stock', 'zero', 'critical']:
            qs = qs.filter(quantity__lte=Decimal('0.000'))

        # 6. Expression Annotations for Landed Cost & Retail Asset Valuations
        cost_expr = ExpressionWrapper(
            F('quantity') * F('product__purchase_price'),
            output_field=DecimalField(max_digits=18, decimal_places=2)
        )
        retail_expr = ExpressionWrapper(
            F('quantity') * F('product__selling_price'),
            output_field=DecimalField(max_digits=18, decimal_places=2)
        )

        annotated_qs = qs.annotate(
            cost_valuation=cost_expr,
            retail_valuation=retail_expr
        ).order_by('product__name')

        # 7. Aggregate Global Totals
        totals = annotated_qs.aggregate(
            total_units=Coalesce(Sum('quantity'), Value(Decimal('0.000'), output_field=DecimalField(max_digits=18, decimal_places=3))),
            total_reserved=Coalesce(Sum('reserved_quantity'), Value(Decimal('0.000'), output_field=DecimalField(max_digits=18, decimal_places=3))),
            total_defective=Coalesce(Sum('quarantined_defective_quantity'), Value(Decimal('0.000'), output_field=DecimalField(max_digits=18, decimal_places=3))),
            total_cost_val=Coalesce(Sum(cost_expr), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_retail_val=Coalesce(Sum(retail_expr), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_skus=Count('id')
        )

        total_cost = totals['total_cost_val']
        total_retail = totals['total_retail_val']
        projected_margin = max(Decimal('0.00'), total_retail - total_cost)
        margin_pct = (
            ((projected_margin / total_retail) * Decimal('100.00')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
            if total_retail > Decimal('0.00') else Decimal('0.0')
        )

        totals['projected_margin'] = projected_margin
        totals['margin_percentage'] = margin_pct
        totals['total_available'] = max(Decimal('0.000'), totals['total_units'] - totals['total_reserved'])

        return {
            'queryset': annotated_qs,
            'totals': totals,
        }