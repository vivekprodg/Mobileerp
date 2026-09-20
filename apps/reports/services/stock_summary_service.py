"""
Stock Summary Business Logic Service.
File Path: apps/reports/services/stock_summary_service.py

Calculates complete inventory stock summaries across custom date ranges,
aggregating historical opening balances, movement transaction types (inward,
outward, transfers, returns, adjustments), and current warehouse stock levels.
"""

from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime, time, timedelta
from typing import Dict, Any, List, Optional, Tuple

from django.db.models import (
    Q, Sum, Case, When, F, Value, DecimalField, IntegerField
)
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.inventory.models import (
    Product, BranchStock, StockMovementLog, ProductCategory,
    ProductSubCategory, Brand, UnitOfMeasurement
)
from apps.branches.models import Branch
from apps.core.nepali_calendar import NepaliCalendar

class StockSummaryService:
    """
    Dedicated calculation engine for Report 1: Stock Summary Report.
    Processes all 10 filter parameters and returns all 21 required columns
    with report-level summary metrics.
    """

    @classmethod
    def resolve_date_range(cls, raw_params: Dict[str, Any]) -> Tuple[date, date, str, str]:
        """
        Parses start_date and end_date. Defaults to the 1st of the current
        Nepali Bikram Sambat (BS) month through today.
        Returns: (start_date, end_date, start_date_bs, end_date_bs)
        """
        today_ad = timezone.now().date()
        start_str = raw_params.get('start_date', '').strip()
        end_str = raw_params.get('end_date', '').strip()

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
    def get_stock_summary(
        cls,
        filters: Dict[str, Any],
        user=None
    ) -> Dict[str, Any]:
        """
        Executes date-scoped movement aggregation, snapshot joins, and valuation math.
        """
        start_date, end_date, start_date_bs, end_date_bs = cls.resolve_date_range(filters)

        start_datetime = timezone.make_aware(datetime.combine(start_date, time.min))
        end_datetime = timezone.make_aware(datetime.combine(end_date, time.max))
        now_datetime = timezone.now()

        # ---------------------------------------------------------------------
        # 1. BASE STOCK QUERYSET WITH SELECT_RELATED
        # ---------------------------------------------------------------------
        stock_qs = BranchStock.objects.select_related(
            'product',
            'product__category',
            'product__subcategory',
            'product__brand',
            'product__base_unit',
            'branch'
        )

        branch_id = filters.get('branch_id')
        if branch_id and str(branch_id).strip() not in ['', 'all']:
            stock_qs = stock_qs.filter(branch_id=branch_id)
        elif user and not user.is_superuser:
            active_branch = getattr(user, 'assigned_branch', None)
            if active_branch:
                stock_qs = stock_qs.filter(branch=active_branch)

        category_id = filters.get('category_id')
        if category_id and str(category_id).strip() not in ['', 'all']:
            stock_qs = stock_qs.filter(product__category_id=category_id)

        subcategory_id = filters.get('subcategory_id')
        if subcategory_id and str(subcategory_id).strip() not in ['', 'all']:
            stock_qs = stock_qs.filter(product__subcategory_id=subcategory_id)

        brand_id = filters.get('brand_id')
        if brand_id and str(brand_id).strip() not in ['', 'all']:
            stock_qs = stock_qs.filter(product__brand_id=brand_id)

        product_id = filters.get('product_id')
        if product_id and str(product_id).strip() not in ['', 'all']:
            stock_qs = stock_qs.filter(product_id=product_id)

        sku_query = filters.get('sku', '').strip()
        if sku_query:
            stock_qs = stock_qs.filter(product__sku__icontains=sku_query)

        search_q = filters.get('q', '').strip()
        if search_q:
            stock_qs = stock_qs.filter(
                Q(product__name__icontains=search_q) |
                Q(product__sku__icontains=search_q) |
                Q(product__barcode__icontains=search_q) |
                Q(product__model_name__icontains=search_q)
            )

        warehouse_query = filters.get('warehouse', '').strip()
        if warehouse_query:
            stock_qs = stock_qs.filter(
                Q(product__rack_number__icontains=warehouse_query) |
                Q(product__shelf_identifier__icontains=warehouse_query) |
                Q(product__bin_location__icontains=warehouse_query) |
                Q(branch__name__icontains=warehouse_query)
            )

        is_serialized = filters.get('is_serialized', '').strip()
        if is_serialized == 'serialized':
            stock_qs = stock_qs.filter(
                Q(product__requires_imei_tracking=True) | Q(product__requires_serial_tracking=True)
            )
        elif is_serialized == 'non_serialized':
            stock_qs = stock_qs.filter(
                product__requires_imei_tracking=False,
                product__requires_serial_tracking=False
            )

        # ---------------------------------------------------------------------
        # 2. MOVEMENT LOG CONDITIONAL AGGREGATIONS
        # ---------------------------------------------------------------------
        movements_in_period = StockMovementLog.objects.filter(
            created_at__gte=start_datetime,
            created_at__lte=end_datetime
        ).values('product_id', 'branch_id').annotate(
            inward=Coalesce(
                Sum(
                    Case(
                        When(
                            movement_type__in=['PURCHASE', 'TRADE_IN_ACQUISITION', 'RMA_VENDOR_REPLACEMENT_IN'],
                            then=F('quantity_delta')
                        ),
                        default=Value(Decimal('0.000')),
                        output_field=DecimalField(max_digits=14, decimal_places=3)
                    )
                ),
                Value(Decimal('0.000'), output_field=DecimalField(max_digits=14, decimal_places=3))
            ),
            outward=Coalesce(
                Sum(
                    Case(
                        When(
                            movement_type__in=['SALE', 'TRADE_IN_SALE', 'SERVICE_REPLACED_PART_DEDUCT'],
                            then=-F('quantity_delta')
                        ),
                        default=Value(Decimal('0.000')),
                        output_field=DecimalField(max_digits=14, decimal_places=3)
                    )
                ),
                Value(Decimal('0.000'), output_field=DecimalField(max_digits=14, decimal_places=3))
            ),
            transfer_in=Coalesce(
                Sum(
                    Case(
                        When(movement_type='TRANSFER_IN', then=F('quantity_delta')),
                        default=Value(Decimal('0.000')),
                        output_field=DecimalField(max_digits=14, decimal_places=3)
                    )
                ),
                Value(Decimal('0.000'), output_field=DecimalField(max_digits=14, decimal_places=3))
            ),
            transfer_out=Coalesce(
                Sum(
                    Case(
                        When(movement_type='TRANSFER_OUT', then=-F('quantity_delta')),
                        default=Value(Decimal('0.000')),
                        output_field=DecimalField(max_digits=14, decimal_places=3)
                    )
                ),
                Value(Decimal('0.000'), output_field=DecimalField(max_digits=14, decimal_places=3))
            ),
            returns=Coalesce(
                Sum(
                    Case(
                        When(movement_type='SALE_RETURN', then=F('quantity_delta')),
                        default=Value(Decimal('0.000')),
                        output_field=DecimalField(max_digits=14, decimal_places=3)
                    )
                ),
                Value(Decimal('0.000'), output_field=DecimalField(max_digits=14, decimal_places=3))
            ),
            adjustments=Coalesce(
                Sum(
                    Case(
                        When(movement_type__in=['ADJUSTMENT_ADD', 'ADJUSTMENT_SUB'], then=F('quantity_delta')),
                        default=Value(Decimal('0.000')),
                        output_field=DecimalField(max_digits=14, decimal_places=3)
                    )
                ),
                Value(Decimal('0.000'), output_field=DecimalField(max_digits=14, decimal_places=3))
            )
        )

        period_movements_map = {
            (m['product_id'], m['branch_id']): m for m in movements_in_period
        }

        # Movements after end_datetime (needed when reporting a past date window)
        after_end_map = {}
        if end_datetime < now_datetime:
            movements_after_end = StockMovementLog.objects.filter(
                created_at__gt=end_datetime
            ).values('product_id', 'branch_id').annotate(
                net_delta=Coalesce(
                    Sum('quantity_delta'),
                    Value(Decimal('0.000'), output_field=DecimalField(max_digits=14, decimal_places=3))
                )
            )
            after_end_map = {
                (m['product_id'], m['branch_id']): m['net_delta'] for m in movements_after_end
            }

        # ---------------------------------------------------------------------
        # 3. BUILD 21-COLUMN ROW DICTIONARIES
        # ---------------------------------------------------------------------
        rows = []
        stock_status_filter = filters.get('stock_status', '').strip().lower()

        for bs in stock_qs:
            prod = bs.product
            key = (prod.id, bs.branch_id)
            current_live_qty = bs.quantity

            # Reconcile closing quantity as of end_datetime
            delta_after_end = after_end_map.get(key, Decimal('0.000'))
            closing_qty = current_live_qty - delta_after_end

            # Extract movement metrics during selected period
            p_mov = period_movements_map.get(key, {})
            inward_qty = p_mov.get('inward', Decimal('0.000'))
            outward_qty = p_mov.get('outward', Decimal('0.000'))
            transfer_in = p_mov.get('transfer_in', Decimal('0.000'))
            transfer_out = p_mov.get('transfer_out', Decimal('0.000'))
            returns_qty = p_mov.get('returns', Decimal('0.000'))
            adjustments = p_mov.get('adjustments', Decimal('0.000'))

            net_period_movement = (
                inward_qty - outward_qty + transfer_in - transfer_out + returns_qty + adjustments
            )

            # Opening = Closing - Net Period Movement
            opening_qty = closing_qty - net_period_movement

            reserved_qty = bs.reserved_quantity or Decimal('0.000')
            defective_qty = bs.quarantined_defective_quantity or Decimal('0.000')
            available_qty = max(Decimal('0.000'), closing_qty - reserved_qty)

            cost_rate = prod.purchase_price or Decimal('0.00')
            retail_rate = prod.selling_price or Decimal('0.00')

            cost_value = (closing_qty * cost_rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            retail_value = (closing_qty * retail_rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            # Evaluate Stock Status
            low_threshold = bs.low_stock_threshold or Decimal('5.00')
            if closing_qty <= Decimal('0.000'):
                status_code = 'OUT_OF_STOCK'
                status_label = 'Out of Stock / Zero'
                status_badge = 'danger'
            elif closing_qty <= low_threshold:
                status_code = 'LOW_STOCK'
                status_label = 'Low Stock'
                status_badge = 'warning'
            else:
                status_code = 'IN_STOCK'
                status_label = 'In Stock'
                status_badge = 'success'

            # Post-calculation filter for stock status
            if stock_status_filter in ['in_stock', 'healthy'] and status_code != 'IN_STOCK':
                continue
            elif stock_status_filter == 'low_stock' and status_code != 'LOW_STOCK':
                continue
            elif stock_status_filter in ['zero_stock', 'critical', 'out_of_stock'] and status_code != 'OUT_OF_STOCK':
                continue

            # Warehouse Rack / Shelf location formatting
            warehouse_parts = [p for p in [prod.rack_number, prod.shelf_identifier, prod.bin_location] if p]
            warehouse_location = " / ".join(warehouse_parts) if warehouse_parts else f"{bs.branch.name} Store"

            rows.append({
                # 1 to 8: Identity & Location
                'sku': prod.sku,
                'barcode': prod.barcode or '',
                'product_name': prod.name,
                'category_name': prod.category.name if prod.category else 'General',
                'subcategory_name': prod.subcategory.name if prod.subcategory else '-',
                'brand_name': prod.brand.name if prod.brand else '-',
                'branch_name': bs.branch.name,
                'branch_code': bs.branch.code,
                'warehouse': warehouse_location,
                'unit_code': prod.base_unit.code if prod.base_unit else 'PCS',

                # 9 to 15: Historical Movements
                'opening_qty': opening_qty,
                'inward_qty': inward_qty,
                'outward_qty': outward_qty,
                'transfer_in': transfer_in,
                'transfer_out': transfer_out,
                'returns': returns_qty,
                'adjustments': adjustments,

                # 16 to 19: Real-time Snapshot Balances
                'reserved_qty': reserved_qty,
                'defective_qty': defective_qty,
                'available_qty': available_qty,
                'closing_qty': closing_qty,

                # 20 to 21: Financial Asset Valuation
                'cost_price': cost_rate,
                'selling_price': retail_rate,
                'cost_value': cost_value,
                'retail_value': retail_value,

                # Metadata for badges and styling
                'stock_status': status_code,
                'stock_status_label': status_label,
                'stock_status_badge': status_badge,
                'is_serialized': bool(prod.requires_imei_tracking or prod.requires_serial_tracking),
                'product_id': prod.id,
            })

        # Sort alphabetically by category and product name
        rows.sort(key=lambda r: (r['category_name'], r['product_name']))

        # ---------------------------------------------------------------------
        # 4. REPORT-LEVEL TOTALS AGGREGATION
        # ---------------------------------------------------------------------
        summary_totals = {
            'total_opening_qty': sum((r['opening_qty'] for r in rows), Decimal('0.000')),
            'total_inward_qty': sum((r['inward_qty'] for r in rows), Decimal('0.000')),
            'total_outward_qty': sum((r['outward_qty'] for r in rows), Decimal('0.000')),
            'total_transfer_in': sum((r['transfer_in'] for r in rows), Decimal('0.000')),
            'total_transfer_out': sum((r['transfer_out'] for r in rows), Decimal('0.000')),
            'total_returns': sum((r['returns'] for r in rows), Decimal('0.000')),
            'total_adjustments': sum((r['adjustments'] for r in rows), Decimal('0.000')),
            'total_reserved_qty': sum((r['reserved_qty'] for r in rows), Decimal('0.000')),
            'total_defective_qty': sum((r['defective_qty'] for r in rows), Decimal('0.000')),
            'total_available_qty': sum((r['available_qty'] for r in rows), Decimal('0.000')),
            'total_closing_qty': sum((r['closing_qty'] for r in rows), Decimal('0.000')),
            'total_cost_value': sum((r['cost_value'] for r in rows), Decimal('0.00')),
            'total_retail_value': sum((r['retail_value'] for r in rows), Decimal('0.00')),
            'records_count': len(rows),
        }

        projected_margin = summary_totals['total_retail_value'] - summary_totals['total_cost_value']
        summary_totals['projected_margin'] = projected_margin
        summary_totals['margin_percentage'] = (
            ((projected_margin / summary_totals['total_retail_value']) * Decimal('100.00')).quantize(
                Decimal('0.1'), rounding=ROUND_HALF_UP
            )
            if summary_totals['total_retail_value'] > Decimal('0.00') else Decimal('0.0')
        )

        return {
            'rows': rows,
            'summary_totals': summary_totals,
            'start_date': start_date,
            'end_date': end_date,
            'start_date_bs': start_date_bs,
            'end_date_bs': end_date_bs,
        }