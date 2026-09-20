"""
Batch-Wise Stock Report (Accessories & Spare Parts) Business Logic Service.
File Path: apps/reports/services/batch_stock_service.py

Capabilities:
1. Queries ProductBatch records tracking non-serialized merchandise (fast chargers, cables,
   tempered glass screen protectors, smartphone cases, workshop spare batteries, screens).
2. Audits multi-date FIFO acquisition batches with individual supplier landed costs.
3. Calculates depletion rates (% sold), remaining batch asset valuation at purchase cost,
   and projected retail sales turnover at counter MRP.
4. Computes batch age in days from inward purchase date to monitor stock freshness.
"""

from decimal import Decimal, ROUND_HALF_UP
from datetime import date
from typing import Dict, Any, List, Optional
from django.db.models import Q
from django.utils import timezone

from apps.inventory.models import ProductBatch, Product, ProductCategory, Brand
from apps.branches.models import Branch
from apps.core.utils.nepali_date_converter import ad_to_bs_string


class BatchStockService:
    """
    Business logic engine for Report 13: Batch-Wise Stock Report (Accessories & Spare Parts).
    """

    @classmethod
    def get_batch_stock_data(
        cls,
        filters: Dict[str, Any],
        user=None
    ) -> Dict[str, Any]:
        """
        Queries all active or historical FIFO inventory batches with asset valuations.
        """
        active_branch = getattr(user, 'assigned_branch', None) if user and not user.is_superuser else None

        branch_id = filters.get('branch_id') or filters.get('branch', '')
        category_id = filters.get('category_id') or filters.get('category', '')
        brand_id = filters.get('brand_id') or filters.get('brand', '')
        status_filter = str(filters.get('status', 'active') or 'active').strip().lower()
        search_query = str(filters.get('q', '') or '').strip()

        today = timezone.now().date()

        # 1. Base Query with select_related
        qs = ProductBatch.objects.select_related(
            'product',
            'product__category',
            'product__brand',
            'product__base_unit',
            'branch'
        )

        # 2. Branch Scoping
        if branch_id and str(branch_id).strip() not in ['', 'all', 'None']:
            qs = qs.filter(branch_id=branch_id)
        elif active_branch and not (user.is_superuser or getattr(user, 'role', '') == 'OWNER'):
            qs = qs.filter(branch=active_branch)

        # 3. Category & Brand Filters
        if category_id and str(category_id).strip() not in ['', 'all', 'None']:
            qs = qs.filter(product__category_id=category_id)

        if brand_id and str(brand_id).strip() not in ['', 'all', 'None']:
            qs = qs.filter(product__brand_id=brand_id)

        # 4. Status Filter (Active vs Depleted)
        if status_filter == 'active':
            qs = qs.filter(is_depleted=False, quantity_remaining__gt=Decimal('0.000'))
        elif status_filter == 'depleted':
            qs = qs.filter(is_depleted=True)

        # 5. Search Filter
        if search_query:
            qs = qs.filter(
                Q(batch_number__icontains=search_query) |
                Q(product__name__icontains=search_query) |
                Q(product__sku__icontains=search_query) |
                Q(product__barcode__icontains=search_query) |
                Q(supplier_name__icontains=search_query) |
                Q(grn_reference__icontains=search_query)
            )

        # 6. Build Detailed Batch Records & Financial Calculations
        records: List[Dict[str, Any]] = []
        total_units_remaining = Decimal('0.000')
        total_units_received = Decimal('0.000')
        total_cost_valuation = Decimal('0.00')
        total_retail_valuation = Decimal('0.00')
        total_age_days = 0

        for b in qs.order_by('product__name', '-purchase_date', '-created_at'):
            inward_dt = b.purchase_date
            age_days = max(0, (today - inward_dt).days)
            total_age_days += age_days

            qty_recv = b.quantity_received or Decimal('0.000')
            qty_rem = b.quantity_remaining or Decimal('0.000')
            qty_sold = max(Decimal('0.000'), qty_recv - qty_rem)

            cost_rate = b.cost_price or b.product.purchase_price or Decimal('0.00')
            sell_rate = b.selling_price or b.product.selling_price or Decimal('0.00')

            batch_cost_val = (qty_rem * cost_rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            batch_retail_val = (qty_rem * sell_rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            depletion_pct = (
                ((qty_sold / qty_recv) * Decimal('100.00')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
                if qty_recv > Decimal('0.000') else Decimal('0.0')
            )

            total_units_received += qty_recv
            total_units_remaining += qty_rem
            total_cost_valuation += batch_cost_val
            total_retail_valuation += batch_retail_val

            records.append({
                'id': b.id,
                'batch_number': b.batch_number,
                'product': b.product,
                'name': b.product.name,
                'sku': b.product.sku,
                'barcode': b.product.barcode or '',
                'unit_code': b.product.base_unit.code if b.product.base_unit else 'PCS',
                'branch': b.branch,
                'branch_code': b.branch.code,
                'purchase_date': inward_dt,
                'purchase_date_str': inward_dt.strftime('%Y-%m-%d'),
                'purchase_date_bs': ad_to_bs_string(inward_dt, lang='en'),
                'supplier_name': b.supplier_name or 'Direct Purchase',
                'grn_reference': b.grn_reference or '-',
                'quantity_received': qty_recv,
                'quantity_remaining': qty_rem,
                'quantity_sold': qty_sold,
                'depletion_pct': depletion_pct,
                'cost_price': cost_rate,
                'selling_price': sell_rate,
                'cost_valuation': batch_cost_val,
                'retail_valuation': batch_retail_val,
                'realizable_margin': max(Decimal('0.00'), batch_retail_val - batch_cost_val),
                'is_depleted': b.is_depleted,
                'age_days': age_days,
            })

        avg_age = round(total_age_days / len(records), 1) if records else 0

        totals = {
            'total_batches': len(records),
            'total_units_received': total_units_received,
            'total_units_remaining': total_units_remaining,
            'total_cost_valuation': total_cost_valuation,
            'total_retail_valuation': total_retail_valuation,
            'projected_margin': max(Decimal('0.00'), total_retail_valuation - total_cost_valuation),
            'avg_age_days': avg_age,
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
        }