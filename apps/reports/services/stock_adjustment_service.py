"""
Stock Adjustment & Write-Off Report Business Logic Service.
File Path: apps/reports/services/stock_adjustment_service.py

Capabilities:
1. Detects stock theft, shrinkage, breakages, expired stock, damage write-offs,
   and physical inventory count audit adjustments.
2. Queries StockMovementLog where movement_type is either ADJUSTMENT_ADD (found stock)
   or ADJUSTMENT_SUB (damaged / lost / expired / write-off).
3. Pulls user audit trail (who made the change), quantity deltas, timestamps (AD & BS),
   previous vs. new stock quantities, and mandatory justification notes.
4. Calculates financial impact with Decimal precision:
   - Total Loss / Shrinkage Value: (Quantity Deducted * Product Purchase Cost).
   - Total Found / Added Value: (Quantity Added * Product Purchase Cost).
   - Net Inventory Financial Impact: (Added Value - Loss Value).
"""

from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime, time
from typing import Dict, Any, List, Optional, Tuple

from django.db.models import (
    Q, F, Sum, Count, DecimalField, Value, ExpressionWrapper, Case, When
)
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.inventory.models import StockMovementLog, Product, ProductCategory, Brand
from apps.branches.models import Branch
from apps.core.nepali_calendar import NepaliCalendar
from apps.core.utils.nepali_date_converter import ad_to_bs_string


class StockAdjustmentService:
    """
    Business logic engine for Report 7: Stock Adjustment & Write-Off Report.
    """

    @classmethod
    def resolve_date_range(cls, raw_params: Dict[str, Any]) -> Tuple[Optional[date], Optional[date], str, str]:
        """
        Parses start_date and end_date from filter parameters.
        Returns: (start_date, end_date, start_date_bs, end_date_bs)
        """
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

        if start_date and end_date and start_date > end_date:
            start_date, end_date = end_date, start_date

        start_date_bs = ad_to_bs_string(start_date, lang='en') if start_date else ''
        end_date_bs = ad_to_bs_string(end_date, lang='en') if end_date else ''

        return start_date, end_date, start_date_bs, end_date_bs

    @classmethod
    def get_adjustment_data(
        cls,
        filters: Dict[str, Any],
        user=None
    ) -> Dict[str, Any]:
        """
        Queries and compiles all manual count corrections, shrinkage losses,
        and damage write-offs with comprehensive financial analytics.
        """
        active_branch = getattr(user, 'assigned_branch', None) if user and not user.is_superuser else None

        branch_id = filters.get('branch_id') or filters.get('branch', '')
        adj_type = str(filters.get('type', '') or filters.get('adjustment_type', '') or '').strip().upper()
        category_id = filters.get('category_id') or filters.get('category', '')
        brand_id = filters.get('brand_id') or filters.get('brand', '')
        search_query = str(filters.get('q', '') or '').strip()

        start_date, end_date, start_date_bs, end_date_bs = cls.resolve_date_range(filters)

        # 1. Base Query: Only manual inventory additions and subtractions
        qs = StockMovementLog.objects.select_related(
            'product',
            'product__category',
            'product__brand',
            'product__base_unit',
            'branch',
            'user'
        ).filter(
            movement_type__in=['ADJUSTMENT_ADD', 'ADJUSTMENT_SUB']
        )

        # 2. Branch Scoping
        if branch_id and str(branch_id).strip() not in ['', 'all', 'None']:
            qs = qs.filter(branch_id=branch_id)
        elif active_branch and not (user.is_superuser or getattr(user, 'role', '') == 'OWNER'):
            qs = qs.filter(branch=active_branch)

        # 3. Adjustment Type Filter (ADD vs SUB)
        if adj_type in ['ADD', 'ADJUSTMENT_ADD', 'PLUS']:
            qs = qs.filter(movement_type='ADJUSTMENT_ADD')
        elif adj_type in ['SUB', 'ADJUSTMENT_SUB', 'MINUS', 'LOSS']:
            qs = qs.filter(movement_type='ADJUSTMENT_SUB')

        # 4. Category & Brand Filtering
        if category_id and str(category_id).strip() not in ['', 'all', 'None']:
            qs = qs.filter(product__category_id=category_id)

        if brand_id and str(brand_id).strip() not in ['', 'all', 'None']:
            qs = qs.filter(product__brand_id=brand_id)

        # 5. Date Range Filtering
        if start_date:
            qs = qs.filter(created_at__date__gte=start_date)

        if end_date:
            qs = qs.filter(created_at__date__lte=end_date)

        # 6. Keyword Search Filter
        if search_query:
            qs = qs.filter(
                Q(product__name__icontains=search_query) |
                Q(product__sku__icontains=search_query) |
                Q(product__barcode__icontains=search_query) |
                Q(reference_document__icontains=search_query) |
                Q(imei_or_serial_number__icontains=search_query) |
                Q(remarks__icontains=search_query) |
                Q(user__username__icontains=search_query) |
                Q(user__first_name__icontains=search_query) |
                Q(user__last_name__icontains=search_query)
            )

        # 7. Database Financial Expression Annotations
        cost_rate_expr = Coalesce(
            F('product__purchase_price'),
            Value(Decimal('0.00'), output_field=DecimalField(max_digits=14, decimal_places=2))
        )
        financial_impact_expr = ExpressionWrapper(
            F('quantity_delta') * cost_rate_expr,
            output_field=DecimalField(max_digits=18, decimal_places=2)
        )

        annotated_qs = qs.annotate(
            unit_cost=cost_rate_expr,
            financial_impact=financial_impact_expr
        ).order_by('-created_at')

        # 8. Compute Financial and Quantity Aggregates
        aggregates = annotated_qs.aggregate(
            total_events=Count('id'),
            total_added_qty=Coalesce(
                Sum(
                    Case(
                        When(movement_type='ADJUSTMENT_ADD', then=F('quantity_delta')),
                        default=Value(Decimal('0.000')),
                        output_field=DecimalField(max_digits=18, decimal_places=3)
                    )
                ),
                Value(Decimal('0.000'), output_field=DecimalField(max_digits=18, decimal_places=3))
            ),
            total_deducted_qty=Coalesce(
                Sum(
                    Case(
                        When(movement_type='ADJUSTMENT_SUB', then=-F('quantity_delta')),
                        default=Value(Decimal('0.000')),
                        output_field=DecimalField(max_digits=18, decimal_places=3)
                    )
                ),
                Value(Decimal('0.000'), output_field=DecimalField(max_digits=18, decimal_places=3))
            ),
            total_added_value=Coalesce(
                Sum(
                    Case(
                        When(movement_type='ADJUSTMENT_ADD', then=F('quantity_delta') * cost_rate_expr),
                        default=Value(Decimal('0.00')),
                        output_field=DecimalField(max_digits=18, decimal_places=2)
                    )
                ),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
            total_loss_value=Coalesce(
                Sum(
                    Case(
                        When(movement_type='ADJUSTMENT_SUB', then=-F('quantity_delta') * cost_rate_expr),
                        default=Value(Decimal('0.00')),
                        output_field=DecimalField(max_digits=18, decimal_places=2)
                    )
                ),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            )
        )

        adds_qty = aggregates['total_added_qty']
        subs_qty = aggregates['total_deducted_qty']
        net_qty = adds_qty - subs_qty

        added_val = aggregates['total_added_value']
        loss_val = aggregates['total_loss_value']
        net_impact = added_val - loss_val

        totals = {
            'total_events': aggregates['total_events'],
            'total_added_qty': adds_qty,
            'total_deducted_qty': subs_qty,
            'net_adjustment_qty': net_qty,
            'total_added_value': added_val,
            'total_loss_value': loss_val,
            'net_financial_impact': net_impact,
        }

        # 9. Build Serialized Item Dictionaries
        records: List[Dict[str, Any]] = []
        for log in annotated_qs:
            delta = log.quantity_delta or Decimal('0.000')
            cost = log.product.purchase_price or Decimal('0.00')
            impact = (delta * cost).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            date_ad_str = log.created_at.strftime('%Y-%m-%d')
            date_bs_str = ad_to_bs_string(log.created_at.date(), lang='en')

            user_display = (
                log.user.get_full_name() or log.user.username
                if log.user else "System Automation"
            )

            records.append({
                'id': log.id,
                'log': log,
                'created_at': log.created_at,
                'date_ad': date_ad_str,
                'date_bs': date_bs_str,
                'time': log.created_at.strftime('%H:%M:%S'),
                'product': log.product,
                'branch': log.branch,
                'user': log.user,
                'user_display': user_display,
                'movement_type': log.movement_type,
                'movement_type_display': log.get_movement_type_display(),
                'is_add': (log.movement_type == 'ADJUSTMENT_ADD'),
                'quantity_delta': delta,
                'previous_quantity': log.previous_quantity,
                'new_quantity': log.new_quantity,
                'unit_code': log.product.base_unit.code if log.product.base_unit else 'PCS',
                'cost_price': cost,
                'financial_impact': impact,
                'reference_document': log.reference_document or '-',
                'imei_or_serial_number': log.imei_or_serial_number or '',
                'remarks': log.remarks or 'Stock audit count adjustment',
            })

        selected_branch = None
        if branch_id and branch_id != 'all':
            selected_branch = Branch.objects.filter(id=branch_id).first()
        elif active_branch:
            selected_branch = active_branch

        return {
            'records': records,
            'queryset': annotated_qs,
            'totals': totals,
            'total_added_qty': totals['total_added_qty'],
            'total_deducted_qty': totals['total_deducted_qty'],
            'net_adjustment_qty': totals['net_adjustment_qty'],
            'total_loss_value': totals['total_loss_value'],
            'total_added_value': totals['total_added_value'],
            'net_financial_impact': totals['net_financial_impact'],
            'total_events': totals['total_events'],
            'branches': Branch.objects.filter(is_active=True).order_by('-is_main_branch', 'name'),
            'categories': ProductCategory.objects.filter(is_active=True).order_by('name'),
            'brands': Brand.objects.all().order_by('name'),
            'selected_branch': selected_branch,
            'filters': filters,
            'start_date': start_date.strftime('%Y-%m-%d') if start_date else '',
            'end_date': end_date.strftime('%Y-%m-%d') if end_date else '',
            'start_date_bs': start_date_bs,
            'end_date_bs': end_date_bs,
        }