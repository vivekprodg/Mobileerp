"""
Stock Aging Analysis Business Logic Service.
File Path: apps/reports/services/stock_aging_service.py

Capabilities:
1. Evaluates in-stock physical smartphones (ItemInstance) and non-serialized accessories (ProductBatch).
2. Calculates exact age in elapsed days: (Today - Inward Purchase Date).
3. Classifies inventory into 4 standard retail aging brackets:
   - 0 to 30 Days (Fresh Stock - Low depreciation risk)
   - 31 to 60 Days (Normal Stock - Standard shelf velocity)
   - 61 to 90 Days (Slow Stock - Margin pressure alert)
   - 90+ Days (At-Risk / Old Stock - Critical depreciation write-down risk)
4. Computes total capital value locked in each age bracket, unit counts, and asset percentages.
5. Formats Bikram Sambat (BS) inward dates and supports multi-parameter filtering.
"""

from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime
from typing import Dict, Any, List, Optional
from django.db.models import Q
from django.utils import timezone

from apps.inventory.models import ItemInstance, ProductBatch, Product, ProductCategory, Brand
from apps.branches.models import Branch
from apps.core.utils.nepali_date_converter import ad_to_bs_string


class StockAgingService:
    """
    Business logic engine for Report 10: Stock Aging Report.
    """

    @classmethod
    def get_stock_aging_data(
        cls,
        filters: Dict[str, Any],
        user=None
    ) -> Dict[str, Any]:
        """
        Queries all active inventory batches and serialized handsets, calculates exact
        age in days, assigns age brackets, and aggregates capital valuations.
        """
        active_branch = getattr(user, 'assigned_branch', None) if user and not user.is_superuser else None

        branch_id = filters.get('branch_id') or filters.get('branch', '')
        category_id = filters.get('category_id') or filters.get('category', '')
        brand_id = filters.get('brand_id') or filters.get('brand', '')
        item_type_filter = str(filters.get('item_type', '') or '').strip().lower()
        bracket_filter = str(filters.get('bracket', '') or '').strip().lower()
        search_query = str(filters.get('q', '') or '').strip()

        today = timezone.now().date()

        # ---------------------------------------------------------------------
        # 1. SERIALIZED SMARTPHONES QUERY (ItemInstance)
        # ---------------------------------------------------------------------
        handset_qs = ItemInstance.objects.select_related(
            'product',
            'product__category',
            'product__brand',
            'product__base_unit',
            'branch'
        ).filter(status='IN_STOCK')

        if branch_id and str(branch_id).strip() not in ['', 'all', 'None']:
            handset_qs = handset_qs.filter(branch_id=branch_id)
        elif active_branch and not (user.is_superuser or getattr(user, 'role', '') == 'OWNER'):
            handset_qs = handset_qs.filter(branch=active_branch)

        if category_id and str(category_id).strip() not in ['', 'all', 'None']:
            handset_qs = handset_qs.filter(product__category_id=category_id)

        if brand_id and str(brand_id).strip() not in ['', 'all', 'None']:
            handset_qs = handset_qs.filter(product__brand_id=brand_id)

        if search_query:
            handset_qs = handset_qs.filter(
                Q(product__name__icontains=search_query) |
                Q(product__sku__icontains=search_query) |
                Q(product__barcode__icontains=search_query) |
                Q(imei_1__icontains=search_query) |
                Q(imei_2__icontains=search_query) |
                Q(serial_number__icontains=search_query) |
                Q(device_uid__icontains=search_query) |
                Q(purchase_reference__icontains=search_query)
            )

        # ---------------------------------------------------------------------
        # 2. NON-SERIALIZED ACCESSORIES & PARTS BATCHES (ProductBatch)
        # ---------------------------------------------------------------------
        batch_qs = ProductBatch.objects.select_related(
            'product',
            'product__category',
            'product__brand',
            'product__base_unit',
            'branch'
        ).filter(is_depleted=False, quantity_remaining__gt=Decimal('0.000'))

        if branch_id and str(branch_id).strip() not in ['', 'all', 'None']:
            batch_qs = batch_qs.filter(branch_id=branch_id)
        elif active_branch and not (user.is_superuser or getattr(user, 'role', '') == 'OWNER'):
            batch_qs = batch_qs.filter(branch=active_branch)

        if category_id and str(category_id).strip() not in ['', 'all', 'None']:
            batch_qs = batch_qs.filter(product__category_id=category_id)

        if brand_id and str(brand_id).strip() not in ['', 'all', 'None']:
            batch_qs = batch_qs.filter(product__brand_id=brand_id)

        if search_query:
            batch_qs = batch_qs.filter(
                Q(product__name__icontains=search_query) |
                Q(product__sku__icontains=search_query) |
                Q(product__barcode__icontains=search_query) |
                Q(batch_number__icontains=search_query) |
                Q(supplier_name__icontains=search_query) |
                Q(grn_reference__icontains=search_query)
            )

        # ---------------------------------------------------------------------
        # 3. BUILD AGING RECORDS & BUCKET ALLOCATIONS
        # ---------------------------------------------------------------------
        all_rows: List[Dict[str, Any]] = []

        # Process Handsets
        if item_type_filter not in ['accessory', 'batch']:
            for h in handset_qs:
                inward_dt = h.purchase_date or h.created_at.date()
                age_days = max(0, (today - inward_dt).days)
                cost = h.landed_cost or h.product.purchase_price or Decimal('0.00')
                selling_price = h.product.selling_price or Decimal('0.00')

                bracket_key, bracket_label, bracket_badge = cls._classify_age_bracket(age_days)

                all_rows.append({
                    'id': f"h_{h.id}",
                    'item_type': 'Handset (IMEI)',
                    'is_handset': True,
                    'product': h.product,
                    'name': h.product.name,
                    'sku': h.product.sku,
                    'identifier': h.imei_1 or h.serial_number or h.device_uid or '-',
                    'imei_1': h.imei_1 or '',
                    'imei_2': h.imei_2 or '',
                    'branch': h.branch,
                    'branch_name': h.branch.name,
                    'inward_date': inward_dt,
                    'inward_date_str': inward_dt.strftime('%Y-%m-%d'),
                    'inward_date_bs': ad_to_bs_string(inward_dt, lang='en'),
                    'age_days': age_days,
                    'bracket_key': bracket_key,
                    'bracket_label': bracket_label,
                    'bracket_badge': bracket_badge,
                    'quantity': Decimal('1.000'),
                    'unit_code': h.product.base_unit.code if h.product.base_unit else 'PCS',
                    'unit_cost': cost,
                    'cost_value': cost,
                    'selling_price': selling_price,
                    'retail_value': selling_price,
                    'rack_number': h.product.rack_number or '-',
                })

        # Process Accessory & Spare Part Batches
        if item_type_filter not in ['handset', 'phone']:
            for b in batch_qs:
                inward_dt = b.purchase_date
                age_days = max(0, (today - inward_dt).days)
                qty = b.quantity_remaining
                unit_cost = b.cost_price or b.product.purchase_price or Decimal('0.00')
                line_cost = (qty * unit_cost).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                unit_selling = b.selling_price or b.product.selling_price or Decimal('0.00')
                line_retail = (qty * unit_selling).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

                bracket_key, bracket_label, bracket_badge = cls._classify_age_bracket(age_days)

                all_rows.append({
                    'id': f"b_{b.id}",
                    'item_type': 'Batch (Accessory)',
                    'is_handset': False,
                    'product': b.product,
                    'name': b.product.name,
                    'sku': b.product.sku,
                    'identifier': b.batch_number,
                    'imei_1': '',
                    'imei_2': '',
                    'branch': b.branch,
                    'branch_name': b.branch.name,
                    'inward_date': inward_dt,
                    'inward_date_str': inward_dt.strftime('%Y-%m-%d'),
                    'inward_date_bs': ad_to_bs_string(inward_dt, lang='en'),
                    'age_days': age_days,
                    'bracket_key': bracket_key,
                    'bracket_label': bracket_label,
                    'bracket_badge': bracket_badge,
                    'quantity': qty,
                    'unit_code': b.product.base_unit.code if b.product.base_unit else 'PCS',
                    'unit_cost': unit_cost,
                    'cost_value': line_cost,
                    'selling_price': unit_selling,
                    'retail_value': line_retail,
                    'rack_number': b.product.rack_number or '-',
                })

        # ---------------------------------------------------------------------
        # 4. COMPUTE 4-BUCKET AGGREGATES ACROSS FULL UNFILTERED ROWS
        # ---------------------------------------------------------------------
        bucket_0_30 = [r for r in all_rows if r['bracket_key'] == '0_30']
        bucket_31_60 = [r for r in all_rows if r['bracket_key'] == '31_60']
        bucket_61_90 = [r for r in all_rows if r['bracket_key'] == '61_90']
        bucket_90_plus = [r for r in all_rows if r['bracket_key'] == '90_plus']

        cost_0_30 = sum((r['cost_value'] for r in bucket_0_30), Decimal('0.00'))
        cost_31_60 = sum((r['cost_value'] for r in bucket_31_60), Decimal('0.00'))
        cost_61_90 = sum((r['cost_value'] for r in bucket_61_90), Decimal('0.00'))
        cost_90_plus = sum((r['cost_value'] for r in bucket_90_plus), Decimal('0.00'))

        total_capital = cost_0_30 + cost_31_60 + cost_61_90 + cost_90_plus
        total_units = sum((r['quantity'] for r in all_rows), Decimal('0.000'))

        # Calculate percentages of total capital
        pct_0_30 = ((cost_0_30 / total_capital) * Decimal('100.00')).quantize(Decimal('0.1')) if total_capital > 0 else Decimal('0.0')
        pct_31_60 = ((cost_31_60 / total_capital) * Decimal('100.00')).quantize(Decimal('0.1')) if total_capital > 0 else Decimal('0.0')
        pct_61_90 = ((cost_61_90 / total_capital) * Decimal('100.00')).quantize(Decimal('0.1')) if total_capital > 0 else Decimal('0.0')
        pct_90_plus = ((cost_90_plus / total_capital) * Decimal('100.00')).quantize(Decimal('0.1')) if total_capital > 0 else Decimal('0.0')

        max_age = max((r['age_days'] for r in all_rows), default=0)
        avg_age = (sum(r['age_days'] for r in all_rows) / len(all_rows)) if all_rows else 0

        totals = {
            'total_items': len(all_rows),
            'total_units': total_units,
            'total_capital': total_capital,
            'max_age_days': max_age,
            'avg_age_days': round(avg_age, 1),
            # Bucket 1: 0 - 30 Days (Fresh)
            'count_0_30': len(bucket_0_30),
            'units_0_30': sum((r['quantity'] for r in bucket_0_30), Decimal('0.000')),
            'cost_0_30': cost_0_30,
            'pct_0_30': pct_0_30,
            # Bucket 2: 31 - 60 Days (Normal)
            'count_31_60': len(bucket_31_60),
            'units_31_60': sum((r['quantity'] for r in bucket_31_60), Decimal('0.000')),
            'cost_31_60': cost_31_60,
            'pct_31_60': pct_31_60,
            # Bucket 3: 61 - 90 Days (Slow)
            'count_61_90': len(bucket_61_90),
            'units_61_90': sum((r['quantity'] for r in bucket_61_90), Decimal('0.000')),
            'cost_61_90': cost_61_90,
            'pct_61_90': pct_61_90,
            # Bucket 4: 90+ Days (At-Risk)
            'count_90_plus': len(bucket_90_plus),
            'units_90_plus': sum((r['quantity'] for r in bucket_90_plus), Decimal('0.000')),
            'cost_90_plus': cost_90_plus,
            'pct_90_plus': pct_90_plus,
        }

        # ---------------------------------------------------------------------
        # 5. POST-FILTER BY AGING BRACKET (IF SELECTED BY USER)
        # ---------------------------------------------------------------------
        display_rows = all_rows
        if bracket_filter and bracket_filter not in ['', 'all']:
            display_rows = [r for r in display_rows if r['bracket_key'] == bracket_filter]

        # Sort: Oldest to Newest Stock (Highest age_days first)
        display_rows.sort(key=lambda x: x['age_days'], reverse=True)

        selected_branch = None
        if branch_id and branch_id != 'all':
            selected_branch = Branch.objects.filter(id=branch_id).first()
        elif active_branch:
            selected_branch = active_branch

        return {
            'records': display_rows,
            'totals': totals,
            'branches': Branch.objects.filter(is_active=True).order_by('-is_main_branch', 'name'),
            'categories': ProductCategory.objects.filter(is_active=True).order_by('name'),
            'brands': Brand.objects.all().order_by('name'),
            'selected_branch': selected_branch,
            'filters': filters,
        }

    @staticmethod
    def _classify_age_bracket(age_days: int) -> tuple:
        """Classifies age in days into key, text label, and semantic badge styling."""
        if age_days <= 30:
            return '0_30', '0 - 30 Days (Fresh)', 'success'
        elif age_days <= 60:
            return '31_60', '31 - 60 Days (Normal)', 'primary'
        elif age_days <= 90:
            return '61_90', '61 - 90 Days (Slow)', 'warning'
        else:
            return '90_plus', '90+ Days (At-Risk / Old)', 'danger'