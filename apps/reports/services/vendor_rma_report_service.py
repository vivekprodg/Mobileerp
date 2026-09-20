"""
Vendor RMA & Distributor Warranty Claims Report Business Logic Service.
File Path: apps/reports/services/vendor_rma_report_service.py

Capabilities:
1. Monitors defective parts & handsets dispatched to authorized manufacturer /
   distributor service centers (e.g., Samsung / Apple / Xiaomi national labs).
2. Tracks resolution numbers:
   - Items replaced with brand new factory stock.
   - Credit notes reimbursed to supplier running udhaari ledgers.
   - Items rejected or returned unrepaired (warranty voided by distributor lab).
3. Evaluates overall distributor settlement recovery rates:
   (Settled Items / Total Claimed Parts) * 100%.
4. Provides multi-parameter filtering across Supplier/Distributor, Branch, Status,
   Resolution, and Dispatch Date Ranges (with Nepali BS conversion).
"""

from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime
from typing import Dict, Any, List, Optional, Tuple

from django.db.models import (
    Q, F, Sum, Count, DecimalField, Value, Case, When
)
from django.db.models.functions import Coalesce

from apps.inventory.models import VendorRMAClaim, VendorRMAClaimItem, Product
from apps.purchases.models import Supplier
from apps.branches.models import Branch
from apps.core.nepali_calendar import NepaliCalendar
from apps.core.utils.nepali_date_converter import ad_to_bs_string


class VendorRMAReportService:
    """
    Business logic engine for Report 9: Vendor RMA & Distributor Claim Report.
    """

    @classmethod
    def resolve_date_range(cls, raw_params: Dict[str, Any]) -> Tuple[Optional[date], Optional[date], str, str]:
        """
        Parses dispatch start and end dates.
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
    def get_vendor_rma_data(
        cls,
        filters: Dict[str, Any],
        user=None
    ) -> Dict[str, Any]:
        """
        Queries all Vendor RMA claim consignments and evaluates settlement recovery metrics.
        """
        active_branch = getattr(user, 'assigned_branch', None) if user and not user.is_superuser else None

        supplier_id = filters.get('supplier_id') or filters.get('supplier', '')
        branch_id = filters.get('branch_id') or filters.get('branch', '')
        status_filter = str(filters.get('status', '') or '').strip()
        resolution_filter = str(filters.get('resolution', '') or '').strip()
        search_query = str(filters.get('q', '') or '').strip()

        start_date, end_date, start_date_bs, end_date_bs = cls.resolve_date_range(filters)

        # 1. Base Query with select_related & prefetch_related
        qs = VendorRMAClaim.objects.select_related(
            'supplier',
            'branch',
            'dispatched_by',
            'resolved_by'
        ).prefetch_related(
            'claimed_items__product',
            'claimed_items__product__base_unit'
        )

        # 2. Branch Scoping
        if branch_id and str(branch_id).strip() not in ['', 'all', 'None']:
            qs = qs.filter(branch_id=branch_id)
        elif active_branch and not (user.is_superuser or getattr(user, 'role', '') == 'OWNER'):
            qs = qs.filter(branch=active_branch)

        # 3. Supplier Filter
        if supplier_id and str(supplier_id).strip() not in ['', 'all', 'None']:
            qs = qs.filter(supplier_id=supplier_id)

        # 4. Status Filter
        if status_filter and status_filter not in ['', 'all']:
            qs = qs.filter(status=status_filter)

        # 5. Item-Level Resolution Filter (Filters claims having items with specified resolution)
        if resolution_filter and resolution_filter not in ['', 'all']:
            qs = qs.filter(claimed_items__resolution=resolution_filter).distinct()

        # 6. Date Range Filter
        if start_date:
            qs = qs.filter(dispatch_date__gte=start_date)

        if end_date:
            qs = qs.filter(dispatch_date__lte=end_date)

        # 7. Keyword Search Filter
        if search_query:
            qs = qs.filter(
                Q(rma_number__icontains=search_query) |
                Q(supplier__company_name__icontains=search_query) |
                Q(distributor_tracking_ref__icontains=search_query) |
                Q(distributor_service_center__icontains=search_query) |
                Q(resolution_notes__icontains=search_query) |
                Q(claimed_items__defective_serial_or_imei__icontains=search_query) |
                Q(claimed_items__defect_description__icontains=search_query) |
                Q(claimed_items__product__name__icontains=search_query)
            ).distinct()

        # 8. High-Level Aggregates
        claim_aggregates = qs.aggregate(
            total_claims=Count('id'),
            total_claimed_parts=Coalesce(Sum('total_claimed_parts_count'), Value(0)),
            total_credit_recovered=Coalesce(
                Sum('total_credit_amount'),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
            pending_claims_count=Count(
                Case(
                    When(status__in=['DRAFT', 'DISPATCHED_TO_VENDOR', 'PARTIALLY_SETTLED'], then=1)
                )
            ),
            completed_claims_count=Count(Case(When(status='COMPLETED', then=1))),
            rejected_claims_count=Count(Case(When(status='REJECTED', then=1)))
        )

        # 9. Item-Level Granular Breakdown
        claim_ids = list(qs.values_list('id', flat=True))
        item_stats = VendorRMAClaimItem.objects.filter(rma_claim_id__in=claim_ids).aggregate(
            total_items=Count('id'),
            pending_items_count=Count(Case(When(resolution='PENDING', then=1))),
            replaced_items_count=Count(Case(When(resolution='REPLACED_WITH_NEW_PART', then=1))),
            credit_note_items_count=Count(Case(When(resolution='CREDIT_NOTE_ISSUED', then=1))),
            returned_unrepaired_count=Count(Case(When(resolution='RETURNED_UNREPAIRED', then=1)))
        )

        total_items = item_stats['total_items'] or 0
        settled_items = (item_stats['replaced_items_count'] or 0) + (item_stats['credit_note_items_count'] or 0)
        settlement_rate = (
            ((Decimal(str(settled_items)) / Decimal(str(total_items))) * Decimal('100.00')).quantize(
                Decimal('0.1'), rounding=ROUND_HALF_UP
            )
            if total_items > 0 else Decimal('0.0')
        )

        totals = {
            'total_claims': claim_aggregates['total_claims'] or 0,
            'total_claimed_parts': claim_aggregates['total_claimed_parts'] or 0,
            'total_credit_recovered': claim_aggregates['total_credit_recovered'] or Decimal('0.00'),
            'pending_claims_count': claim_aggregates['pending_claims_count'] or 0,
            'completed_claims_count': claim_aggregates['completed_claims_count'] or 0,
            'rejected_claims_count': claim_aggregates['rejected_claims_count'] or 0,
            'total_items': total_items,
            'pending_items_count': item_stats['pending_items_count'] or 0,
            'replaced_items_count': item_stats['replaced_items_count'] or 0,
            'credit_note_items_count': item_stats['credit_note_items_count'] or 0,
            'returned_unrepaired_count': item_stats['returned_unrepaired_count'] or 0,
            'settlement_rate': settlement_rate,
        }

        # 10. Compile Serialized Voucher Records
        records: List[Dict[str, Any]] = []
        for claim in qs.order_by('-created_at'):
            items_list = list(claim.claimed_items.all())
            pending_count = sum(1 for it in items_list if it.resolution == 'PENDING')
            replaced_count = sum(1 for it in items_list if it.resolution == 'REPLACED_WITH_NEW_PART')
            credit_count = sum(1 for it in items_list if it.resolution == 'CREDIT_NOTE_ISSUED')
            rejected_count = sum(1 for it in items_list if it.resolution == 'RETURNED_UNREPAIRED')

            dispatch_bs = ad_to_bs_string(claim.dispatch_date, lang='en') if claim.dispatch_date else '-'

            records.append({
                'id': claim.id,
                'claim': claim,
                'rma_number': claim.rma_number,
                'supplier': claim.supplier,
                'branch': claim.branch,
                'status': claim.status,
                'status_display': claim.get_status_display(),
                'dispatch_date': claim.dispatch_date,
                'dispatch_date_str': claim.dispatch_date.strftime('%Y-%m-%d') if claim.dispatch_date else 'Draft',
                'dispatch_date_bs': dispatch_bs,
                'tracking_ref': claim.distributor_tracking_ref or '-',
                'service_center': claim.distributor_service_center or 'Authorized National Service Center',
                'items_count': len(items_list),
                'items_list': items_list,
                'pending_count': pending_count,
                'replaced_count': replaced_count,
                'credit_count': credit_count,
                'rejected_count': rejected_count,
                'total_credit_amount': claim.total_credit_amount,
                'dispatched_by': claim.dispatched_by,
                'resolved_by': claim.resolved_by,
                'resolution_notes': claim.resolution_notes or '',
                'created_at': claim.created_at,
            })

        selected_branch = None
        if branch_id and branch_id != 'all':
            selected_branch = Branch.objects.filter(id=branch_id).first()
        elif active_branch:
            selected_branch = active_branch

        return {
            'records': records,
            'queryset': qs,
            'totals': totals,
            'suppliers': Supplier.objects.filter(is_active=True).order_by('company_name'),
            'branches': Branch.objects.filter(is_active=True).order_by('-is_main_branch', 'name'),
            'statuses': VendorRMAClaim.STATUS_CHOICES,
            'resolutions': VendorRMAClaimItem.RESOLUTION_CHOICES,
            'selected_branch': selected_branch,
            'filters': filters,
            'start_date': start_date.strftime('%Y-%m-%d') if start_date else '',
            'end_date': end_date.strftime('%Y-%m-%d') if end_date else '',
            'start_date_bs': start_date_bs,
            'end_date_bs': end_date_bs,
        }