"""
Product-Wise & Handset Purchase Intelligence Service.
File Path: apps/reports/services/product_purchase_service.py

Capabilities:
1. Audits smartphone and accessory purchasing volumes and costs across custom date ranges.
2. Evaluates total base quantities received, net purchase expenditure, and trade discounts.
3. Computes the weighted average purchase rate: (Total Inward Spend / Total Base Units Purchased).
4. Determines the latest purchase rate and links the primary distributor from the most recent GRN.
5. Displays current counter retail selling MRP to audit realized markup buffers.
"""

from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime
from typing import Dict, Any, List, Optional, Tuple

from django.db.models import Q, Sum, Count, F, DecimalField, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.purchases.models import GoodsReceivedNote, GRNItem, Supplier
from apps.inventory.models import Product, ProductCategory, Brand
from apps.branches.models import Branch
from apps.core.nepali_calendar import NepaliCalendar
from apps.core.utils.nepali_date_converter import ad_to_bs_string


class ProductPurchaseService:
    """
    Business logic engine for Report 3: Product Purchase Report.
    """

    @classmethod
    def resolve_date_range(cls, raw_params: Dict[str, Any]) -> Tuple[date, date, str, str]:
        """
        Resolves start and end dates. Defaults to the 1st of the current
        Nepali Bikram Sambat (BS) month through today.
        Returns: (start_date, end_date, start_date_bs, end_date_bs)
        """
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
    def get_product_purchase_data(
        cls,
        filters: Dict[str, Any],
        user=None
    ) -> Dict[str, Any]:
        """
        Aggregates product purchase volume, costs, average rates, and distributor sources.
        """
        active_branch = getattr(user, 'assigned_branch', None) if user and not user.is_superuser else None

        start_date, end_date, start_date_bs, end_date_bs = cls.resolve_date_range(filters)
        branch_id = filters.get('branch_id') or filters.get('branch', '')
        category_id = filters.get('category_id') or filters.get('category', '')
        brand_id = filters.get('brand_id') or filters.get('brand', '')
        supplier_id = filters.get('supplier_id') or filters.get('supplier', '')
        tracking_type = str(filters.get('tracking_type', '') or '').strip()
        search_query = str(filters.get('q', '') or '').strip()

        # 1. Base Inward Items Query from Verified GRNs
        items_qs = GRNItem.objects.select_related(
            'product',
            'product__category',
            'product__brand',
            'product__base_unit',
            'grn',
            'grn__supplier',
            'grn__branch'
        ).filter(
            grn__status='RECEIVED',
            grn__bill_date__gte=start_date,
            grn__bill_date__lte=end_date
        )

        # 2. Apply Filters
        if branch_id and str(branch_id).strip() not in ['', 'all', 'None']:
            items_qs = items_qs.filter(grn__branch_id=branch_id)
        elif active_branch and not (user.is_superuser or getattr(user, 'role', '') == 'OWNER'):
            items_qs = items_qs.filter(grn__branch=active_branch)

        if supplier_id and str(supplier_id).strip() not in ['', 'all', 'None']:
            items_qs = items_qs.filter(grn__supplier_id=supplier_id)

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
                Q(product__model_name__icontains=search_query) |
                Q(supplier_item_code__icontains=search_query) |
                Q(grn__supplier__company_name__icontains=search_query)
            )

        # 3. Aggregate Inward Volume and Spend grouped by Product
        grouped_stats = items_qs.values('product_id').annotate(
            total_qty=Coalesce(
                Sum('base_unit_quantity'),
                Value(Decimal('0.000'), output_field=DecimalField(max_digits=18, decimal_places=3))
            ),
            total_spend=Coalesce(
                Sum('line_total'),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
            grn_count=Count('grn_id', distinct=True)
        )

        stats_map = {row['product_id']: row for row in grouped_stats}
        product_ids = list(stats_map.keys())

        # 4. Resolve Product Meta and Most Recent Purchase Record
        products_qs = Product.objects.select_related(
            'category', 'brand', 'base_unit'
        ).filter(id__in=product_ids)

        # Pre-fetch latest GRNItem per product for rate & supplier identification
        latest_items = items_qs.order_by('product_id', '-grn__bill_date', '-created_at')
        latest_map: Dict[int, GRNItem] = {}
        for itm in latest_items:
            if itm.product_id not in latest_map:
                latest_map[itm.product_id] = itm

        records: List[Dict[str, Any]] = []
        total_units_purchased = Decimal('0.000')
        total_procurement_spend = Decimal('0.00')

        for prod in products_qs:
            p_data = stats_map.get(prod.id, {})
            qty = p_data.get('total_qty', Decimal('0.000'))
            spend = p_data.get('total_spend', Decimal('0.00'))
            grn_count = p_data.get('grn_count', 1)

            latest_itm = latest_map.get(prod.id)
            latest_rate = latest_itm.purchase_rate if latest_itm else prod.purchase_price
            latest_supplier = latest_itm.grn.supplier.company_name if latest_itm and latest_itm.grn and latest_itm.grn.supplier else "Direct Inward"
            last_date_ad = latest_itm.grn.bill_date if latest_itm and latest_itm.grn else None
            last_date_bs = ad_to_bs_string(last_date_ad, lang='en') if last_date_ad else "-"

            # Calculate Weighted Average Purchase Rate: Total Spend / Total Units
            weighted_avg_rate = (
                (spend / qty).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                if qty > Decimal('0.000') else latest_rate
            )

            current_mrp = prod.selling_price or Decimal('0.00')
            margin_potential = max(Decimal('0.00'), current_mrp - weighted_avg_rate)
            markup_pct = (
                ((margin_potential / weighted_avg_rate) * Decimal('100.00')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
                if weighted_avg_rate > Decimal('0.00') else Decimal('0.0')
            )

            total_units_purchased += qty
            total_procurement_spend += spend

            is_serialized = bool(prod.requires_imei_tracking or prod.requires_serial_tracking)

            variant_desc = prod.variant_name or ""
            if prod.ram and prod.internal_storage:
                variant_desc = f"{prod.ram}/{prod.internal_storage}"
                if prod.color_variant:
                    variant_desc += f" {prod.color_variant}"

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
                'quantity_purchased': qty,
                'total_spend': spend,
                'weighted_avg_rate': weighted_avg_rate,
                'latest_purchase_rate': latest_rate,
                'current_mrp': current_mrp,
                'margin_potential': margin_potential,
                'markup_percentage': markup_pct,
                'primary_supplier': latest_supplier,
                'last_purchase_date_ad': last_date_ad,
                'last_purchase_date_str': last_date_ad.strftime('%Y-%m-%d') if last_date_ad else '-',
                'last_purchase_date_bs': last_date_bs,
                'grn_count': grn_count,
            })

        # Sort: Highest spend first
        records.sort(key=lambda x: x['total_spend'], reverse=True)

        totals = {
            'distinct_products_count': len(records),
            'total_units_purchased': total_units_purchased,
            'total_procurement_spend': total_procurement_spend,
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
            'suppliers': Supplier.objects.filter(is_active=True).order_by('company_name'),
            'selected_branch': selected_branch,
            'filters': filters,
            'start_date': start_date.strftime('%Y-%m-%d'),
            'end_date': end_date.strftime('%Y-%m-%d'),
            'start_date_bs': start_date_bs,
            'end_date_bs': end_date_bs,
        }