"""
Live IMEI & Serial Number Stock Business Logic Service.
File Path: apps/reports/services/imei_stock_service.py

Queries and audits active smartphone handsets sitting in the showcase or safe,
identifying Primary IMEI 1, Secondary IMEI 2, Dual-SIM pending status, condition grades,
landed cost asset values, and official Nepal Telecommunications Authority (NTA) MDMS registration.
"""

from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Any, List
from django.db.models import (
    F, Q, Sum, Count, DecimalField, Value, Case, When
)
from django.db.models.functions import Coalesce

from apps.inventory.models import ItemInstance, Product, Brand
from apps.branches.models import Branch


class IMEIStockService:
    """
    Business logic engine for the Live IMEI & Serial Number Stock Report.
    """

    @classmethod
    def get_imei_stock_data(
        cls,
        filters: Dict[str, Any],
        user=None
    ) -> Dict[str, Any]:
        """
        Queries all ItemInstance records where status = 'IN_STOCK' with multi-attribute filtering,
        dual-SIM verification, and asset valuation calculations.
        """
        active_branch = getattr(user, 'assigned_branch', None) if user and not user.is_superuser else None

        branch_id = filters.get('branch_id') or filters.get('branch', '')
        brand_id = filters.get('brand_id') or filters.get('brand', '')
        mdms_filter = str(filters.get('mdms_status', '') or '').strip()
        imei2_filter = str(filters.get('imei2_status', '') or '').strip().lower()
        condition_filter = str(filters.get('condition', '') or '').strip()
        search_query = str(filters.get('q', '') or filters.get('imei', '') or '').strip()

        # 1. Base Query: Only handsets and devices physically in stock
        qs = ItemInstance.objects.select_related(
            'product',
            'product__brand',
            'product__category',
            'product__base_unit',
            'branch'
        ).filter(status='IN_STOCK')

        # 2. Branch Scoping
        if branch_id and str(branch_id).strip() not in ['', 'all', 'None']:
            qs = qs.filter(branch_id=branch_id)
        elif active_branch:
            qs = qs.filter(branch=active_branch)

        # 3. Brand Filter
        if brand_id and str(brand_id).strip() not in ['', 'all', 'None']:
            qs = qs.filter(product__brand_id=brand_id)

        # 4. NTA MDMS Status Filter
        if mdms_filter and mdms_filter not in ['', 'all']:
            qs = qs.filter(mdms_status=mdms_filter)

        # 5. Dual-SIM IMEI 2 Status Filter
        if imei2_filter == 'pending':
            qs = qs.filter(
                Q(imei_2_pending_scan=True) |
                (
                    Q(product__sim_configuration__in=['DUAL_SIM', 'ESIM_DUAL']) &
                    (Q(imei_2__isnull=True) | Q(imei_2=''))
                )
            )
        elif imei2_filter == 'captured':
            qs = qs.filter(
                Q(imei_2__isnull=False) &
                ~Q(imei_2='') &
                Q(imei_2_pending_scan=False)
            )

        # 6. Physical Condition Filter
        if condition_filter and condition_filter not in ['', 'all']:
            qs = qs.filter(condition=condition_filter)

        # 7. Search Filter (IMEI 1, IMEI 2, Serial, UID, Model, SKU, Supplier)
        if search_query:
            qs = qs.filter(
                Q(imei_1__icontains=search_query) |
                Q(imei_2__icontains=search_query) |
                Q(serial_number__icontains=search_query) |
                Q(device_uid__icontains=search_query) |
                Q(product__name__icontains=search_query) |
                Q(product__sku__icontains=search_query) |
                Q(product__model_name__icontains=search_query) |
                Q(purchase_reference__icontains=search_query) |
                Q(supplier_name__icontains=search_query)
            )

        # 8. Expression Annotations for Landed Cost Valuation
        cost_expr = Coalesce(
            F('landed_cost'),
            F('product__purchase_price'),
            Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
        )

        annotated_qs = qs.annotate(
            effective_cost=cost_expr
        ).order_by('product__name', '-purchase_date', '-created_at')

        # 9. KPI Aggregations
        totals = annotated_qs.aggregate(
            total_phones=Count('id'),
            total_landed_valuation=Coalesce(
                Sum(cost_expr),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
            pending_imei2_count=Count(
                Case(
                    When(
                        Q(imei_2_pending_scan=True) |
                        (
                            Q(product__sim_configuration__in=['DUAL_SIM', 'ESIM_DUAL']) &
                            (Q(imei_2__isnull=True) | Q(imei_2=''))
                        ),
                        then=1
                    )
                )
            ),
            mdms_registered_count=Count(Case(When(mdms_status='REGISTERED_OFFICIAL', then=1))),
            gray_unregistered_count=Count(Case(When(mdms_status='GRAY_UNREGISTERED', then=1))),
            customs_paid_count=Count(Case(When(mdms_status='INDIVIDUAL_CUSTOMS_PAID', then=1))),
        )

        return {
            'queryset': annotated_qs,
            'totals': totals,
            'branches': Branch.objects.filter(is_active=True).order_by('-is_main_branch', 'name'),
            'brands': Brand.objects.all().order_by('name'),
            'conditions': ItemInstance.CONDITION_CHOICES,
            'mdms_statuses': Product.MDMS_STATUS_CHOICES,
        }