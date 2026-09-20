"""
Inter-Branch Stock Transfers & Logistics Report Business Logic Service.
File Path: apps/reports/services/stock_transfer_report_service.py

Provides comprehensive oversight into merchandise logistics moving between store branches:
- Origin and Destination routing analysis.
- Consignment status (Draft, Dispatched/In-Transit, Received & Stocked, Cancelled).
- Unit counts and landed cost valuations transferred.
- Itemized breakdowns including phone models and lists of individual 15-digit IMEIs in transit.
"""

from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime
from typing import Dict, Any, List, Optional
from django.db.models import Q, Sum, F, Count
from django.utils import timezone

from apps.branches.models import StockTransferRequest, StockTransferItem, Branch
from apps.core.nepali_calendar import NepaliCalendar
from apps.core.utils.nepali_date_converter import ad_to_bs_string


class StockTransferReportService:
    """
    Business logic engine for the Inter-Branch Stock Transfers & Logistics Report.
    """

    @classmethod
    def get_transfer_report(
        cls,
        filters: Dict[str, Any],
        user=None
    ) -> Dict[str, Any]:
        """
        Queries StockTransferRequest consignments, evaluates transferred unit counts,
        landed cost totals, and gathers scanned phone IMEIs per shipment.
        """
        active_branch = getattr(user, 'assigned_branch', None) if user and not user.is_superuser else None

        source_id = filters.get('source_branch_id') or filters.get('source_branch', '')
        dest_id = filters.get('destination_branch_id') or filters.get('destination_branch', '')
        status_filter = str(filters.get('status', '') or '').strip()
        start_date_str = str(filters.get('start_date', '') or '').strip()
        end_date_str = str(filters.get('end_date', '') or '').strip()
        search_query = str(filters.get('q', '') or '').strip()

        # 1. Base Query with full relation joins
        qs = StockTransferRequest.objects.select_related(
            'source_branch',
            'destination_branch',
            'requested_by',
            'dispatched_by',
            'received_by'
        ).prefetch_related(
            'items__product',
            'items__product__base_unit',
            'items__item_instance'
        )

        # 2. Scoping: Non-superusers only see consignments involving their branch
        if active_branch and not (user.is_superuser or getattr(user, 'role', '') == 'OWNER'):
            qs = qs.filter(Q(source_branch=active_branch) | Q(destination_branch=active_branch))

        # 3. Route Origin & Destination Filters
        if source_id and str(source_id).strip() not in ['', 'all', 'None']:
            qs = qs.filter(source_branch_id=source_id)

        if dest_id and str(dest_id).strip() not in ['', 'all', 'None']:
            qs = qs.filter(destination_branch_id=dest_id)

        # 4. Status Filter
        if status_filter and status_filter not in ['', 'all']:
            qs = qs.filter(status=status_filter)

        # 5. Date Range Filters
        start_date = None
        end_date = None

        if start_date_str:
            try:
                start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
                qs = qs.filter(transfer_date__gte=start_date)
            except (ValueError, TypeError):
                pass

        if end_date_str:
            try:
                end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
                qs = qs.filter(transfer_date__lte=end_date)
            except (ValueError, TypeError):
                pass

        # 6. Keyword Search Filter
        if search_query:
            qs = qs.filter(
                Q(transfer_no__icontains=search_query) |
                Q(notes__icontains=search_query) |
                Q(items__product__name__icontains=search_query) |
                Q(items__scanned_imei_or_serial__icontains=search_query)
            ).distinct()

        # 7. Compile Consignments & Calculate Financials
        transfers_data: List[Dict[str, Any]] = []
        total_units_moved = Decimal('0.000')
        total_valuation_moved = Decimal('0.00')

        in_transit_count = 0
        in_transit_valuation = Decimal('0.00')
        received_completed_count = 0
        draft_pending_count = 0

        for trf in qs.order_by('-created_at'):
            items_list = list(trf.items.all())
            conignment_units = Decimal('0.000')
            consign_val = Decimal('0.00')
            imeis_collected: List[str] = []

            for line in items_list:
                qty = line.quantity or Decimal('0.000')
                cost_rate = line.product.purchase_price if line.product else Decimal('0.00')
                if line.item_instance and line.item_instance.landed_cost:
                    cost_rate = line.item_instance.landed_cost

                line_cost = (qty * cost_rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                conignment_units += qty
                consign_val += line_cost

                if line.scanned_imei_or_serial:
                    for im in line.scanned_imei_or_serial.splitlines():
                        if im.strip():
                            imeis_collected.append(im.strip())

            total_units_moved += conignment_units
            total_valuation_moved += consign_val

            if trf.status == 'DISPATCHED':
                in_transit_count += 1
                in_transit_valuation += consign_val
            elif trf.status == 'RECEIVED':
                received_completed_count += 1
            elif trf.status == 'DRAFT':
                draft_pending_count += 1

            date_ad_str = trf.transfer_date.strftime('%Y-%m-%d')
            date_bs_str = ad_to_bs_string(trf.transfer_date, lang='en')

            transfers_data.append({
                'id': trf.id,
                'transfer_no': trf.transfer_no,
                'source_branch': trf.source_branch,
                'destination_branch': trf.destination_branch,
                'status': trf.status,
                'status_display': trf.get_status_display(),
                'transfer_date_ad': date_ad_str,
                'transfer_date_bs': date_bs_str,
                'created_at': trf.created_at,
                'dispatched_date': trf.dispatched_date,
                'received_date': trf.received_date,
                'requested_by': trf.requested_by,
                'dispatched_by': trf.dispatched_by,
                'received_by': trf.received_by,
                'notes': trf.notes or '',
                'items_count': len(items_list),
                'total_units': conignment_units,
                'total_valuation': consign_val,
                'items_list': items_list,
                'imei_count': len(imeis_collected),
                'imei_list': imeis_collected,
            })

        totals = {
            'total_consignments': len(transfers_data),
            'total_units_moved': total_units_moved,
            'total_valuation_moved': total_valuation_moved,
            'in_transit_count': in_transit_count,
            'in_transit_valuation': in_transit_valuation,
            'received_completed_count': received_completed_count,
            'draft_pending_count': draft_pending_count,
        }

        return {
            'records': transfers_data,
            'queryset': qs,
            'totals': totals,
            'branches': Branch.objects.filter(is_active=True).order_by('-is_main_branch', 'name'),
            'statuses': StockTransferRequest.STATUS_CHOICES,
            'start_date': start_date_str,
            'end_date': end_date_str,
        }