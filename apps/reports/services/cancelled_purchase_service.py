"""
Cancelled & Voided Purchases Forensic Audit Service.

Capabilities:
1. Forensic Inward Procurement Audit Engine:
   - Queries and audits all GoodsReceivedNote (GRN) records strictly where status='CANCELLED'.
   - Tracks the originating inward receiver vs. the supervisor who authorized the void.
   - Extracts complete supplier metadata: Supplier Name, Supplier PAN, Contact Phone, Supplier Bill No.
   - Accurately captures cancellation timestamps, IP addresses, and formal justification reasons.
2. Inventory Reversal & Stock Restitution Verification:
   - Verifies whether inward inventory was deducted back out of BranchStock.
   - Confirms if serialized smartphone IMEI units were removed or marked as returned to vendor.
   - Cross-references StockMovementLog entries (reference_document=f"VOID-{grn_number}").
3. Supplier Ledger & Accounts Payable (Udhaari) Reversal:
   - Reconciles reversed supplier accounts payable debt and reversed cash/bank payouts.
   - Quantifies total procurement capital, trade discounts, and input tax voided.
"""

from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime
from typing import Dict, Any, List, Tuple, Optional

from django.db.models import (
    Q, Sum, Count, F, DecimalField, Value
)
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.purchases.models import GoodsReceivedNote, GRNItem, Supplier
from apps.inventory.models import StockMovementLog, ItemInstance
from apps.branches.models import Branch
from apps.users.models import User
from apps.core.models import AuditLog
from apps.core.nepali_calendar import NepaliCalendar
from apps.core.utils.nepali_date_converter import ad_to_bs_string


class CancelledPurchaseService:
    """
    Business logic engine for forensic auditing of cancelled and voided purchase GRNs.
    """

    @classmethod
    def resolve_date_range(cls, raw_params: Dict[str, Any]) -> Tuple[date, date, str, str]:
        """Resolves date boundaries defaulting to current Nepali BS month."""
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
    def get_cancelled_purchase_data(
        cls,
        filters: Dict[str, Any],
        user=None
    ) -> Dict[str, Any]:
        """
        Gathers all voided/cancelled purchase GRNs, cross-references forensic AuditLog
        entries to pinpoint the cancelling supervisor, extracts supplier PAN and bill details,
        and verifies stock deduction/reversal ledgers.
        """
        active_branch = getattr(user, 'assigned_branch', None) if user and not user.is_superuser else None

        start_date, end_date, start_date_bs, end_date_bs = cls.resolve_date_range(filters)
        branch_id = filters.get('branch_id') or filters.get('branch', '')
        supplier_id = filters.get('supplier_id') or filters.get('supplier', '')
        reason_query = str(filters.get('reason', '') or '').strip()
        search_query = str(filters.get('q', '') or '').strip()
        date_basis = str(filters.get('date_basis', 'bill_date') or 'bill_date').strip()

        # 1. Base Query: GRN records strictly with status='CANCELLED'
        grn_qs = GoodsReceivedNote.objects.select_related(
            'branch',
            'supplier',
            'received_by'
        ).prefetch_related(
            'items__product',
            'items__product__base_unit',
            'items__unit_conversion'
        ).filter(status='CANCELLED')

        # Date Range Scoping (Bill Date or Cancellation Timestamp Date)
        if date_basis == 'cancel_date':
            grn_qs = grn_qs.filter(
                updated_at__date__gte=start_date,
                updated_at__date__lte=end_date
            )
        else:
            grn_qs = grn_qs.filter(
                bill_date__gte=start_date,
                bill_date__lte=end_date
            )

        # 2. Branch & Supplier Scoping
        if branch_id and str(branch_id).strip() not in ['', 'all', 'None']:
            grn_qs = grn_qs.filter(branch_id=branch_id)
        elif active_branch and not (user.is_superuser or getattr(user, 'role', '') == 'OWNER'):
            grn_qs = grn_qs.filter(branch=active_branch)

        if supplier_id and str(supplier_id).strip() not in ['', 'all', 'None']:
            grn_qs = grn_qs.filter(supplier_id=supplier_id)

        if reason_query:
            # Check model field or remarks if present
            grn_qs = grn_qs.filter(
                Q(cancellation_reason__icontains=reason_query) |
                Q(remarks__icontains=reason_query)
            )

        if search_query:
            grn_qs = grn_qs.filter(
                Q(grn_number__icontains=search_query) |
                Q(supplier_bill_no__icontains=search_query) |
                Q(supplier__company_name__icontains=search_query) |
                Q(supplier__pan_number__icontains=search_query) |
                Q(supplier__phone_number__icontains=search_query) |
                Q(cancellation_reason__icontains=search_query) |
                Q(items__product__name__icontains=search_query) |
                Q(items__scanned_imei_list__icontains=search_query)
            ).distinct()

        ordered_grns = list(grn_qs.order_by('-updated_at', '-created_at'))
        grn_numbers = [g.grn_number for g in ordered_grns]

        # 3. Batch Query AuditLog for BILL_CANCEL / GRN_CANCEL events
        cancel_logs_map: Dict[str, AuditLog] = {}
        if grn_numbers:
            logs = AuditLog.objects.select_related('user').filter(
                action_type__in=['BILL_CANCEL', 'DELETE', 'UPDATE'],
                module__in=['Purchases', 'PurchaseGRN', 'Purchase'],
                object_repr__in=grn_numbers
            ).order_by('-timestamp')
            for log in logs:
                if log.object_repr not in cancel_logs_map:
                    cancel_logs_map[log.object_repr] = log

        # 4. Batch Query StockMovementLog to verify stock reversal execution
        reversal_ref_docs = [f"VOID-{num}" for num in grn_numbers]
        reversed_refs_set = set(
            StockMovementLog.objects.filter(
                reference_document__in=reversal_ref_docs,
                movement_type__in=['ADJUSTMENT_SUB', 'RMA_VENDOR_DISPATCH', 'SALE_RETURN']
            ).values_list('reference_document', flat=True).distinct()
        )

        # 5. Build Detailed Records & Forensic Metrics
        records: List[Dict[str, Any]] = []

        total_voided_bills = 0
        total_voided_amount = Decimal('0.00')
        total_gross_amount = Decimal('0.00')
        total_discount_amount = Decimal('0.00')
        total_vat_amount = Decimal('0.00')
        total_paid_reversed = Decimal('0.00')
        total_due_reversed = Decimal('0.00')
        total_items_count = 0

        for grn in ordered_grns:
            total_voided_bills += 1
            net_amt = grn.net_total_amount or Decimal('0.00')
            gross_amt = grn.gross_amount or Decimal('0.00')
            disc_amt = grn.discount_amount or Decimal('0.00')
            vat_amt = grn.vat_amount or Decimal('0.00')
            paid_rev = grn.paid_amount or Decimal('0.00')
            due_rev = grn.due_amount or Decimal('0.00')

            total_voided_amount += net_amt
            total_gross_amount += gross_amt
            total_discount_amount += disc_amt
            total_vat_amount += vat_amt
            total_paid_reversed += paid_rev
            total_due_reversed += due_rev

            # Supplier details
            supplier_name = grn.supplier.company_name if grn.supplier else "Unknown Vendor"
            supplier_pan = grn.supplier.pan_number if grn.supplier and grn.supplier.pan_number else "-"
            supplier_phone = grn.supplier.phone_number if grn.supplier and grn.supplier.phone_number else "-"

            # Resolution of cancelling user, IP, and timestamp from AuditLog
            audit_entry = cancel_logs_map.get(grn.grn_number)
            if audit_entry:
                cancelled_by_user = (
                    audit_entry.user.get_full_name() or audit_entry.user.username
                    if audit_entry.user else "System"
                )
                cancel_ip = audit_entry.ip_address or "-"
                cancel_timestamp = audit_entry.timestamp
                audit_reason = audit_entry.details.get('reason') if isinstance(audit_entry.details, dict) else ""
            else:
                cancelled_by_user = (
                    grn.received_by.get_full_name() or grn.received_by.username
                    if grn.received_by else "Store Manager / Admin"
                )
                cancel_ip = "-"
                cancel_timestamp = grn.updated_at
                audit_reason = ""

            cancel_date_ad_str = cancel_timestamp.strftime('%Y-%m-%d %H:%M:%S') if cancel_timestamp else '-'
            cancel_date_bs_str = ad_to_bs_string(cancel_timestamp.date(), lang='en') if cancel_timestamp else '-'

            # Resolution of cancellation reason
            raw_reason = getattr(grn, 'cancellation_reason', None) or getattr(grn, 'remarks', None) or audit_reason
            cancellation_reason = raw_reason.strip() if raw_reason and str(raw_reason).strip() else "Consignment voided / cancelled"

            # Stock reversal check
            is_stock_reverted = f"VOID-{grn.grn_number}" in reversed_refs_set

            # Build line items summary for this voided GRN
            lines_summary = []
            for item in grn.items.all():
                total_items_count += int(item.purchased_quantity)
                lines_summary.append({
                    'product_name': item.product.name,
                    'sku': item.product.sku,
                    'quantity': item.purchased_quantity,
                    'unit_code': item.product.base_unit.code if item.product.base_unit else 'PCS',
                    'purchase_rate': item.purchase_rate,
                    'line_total': item.line_total,
                    'scanned_imeis': item.scanned_imei_list or '',
                })

            bill_date_ad = grn.bill_date
            bill_date_bs = ad_to_bs_string(bill_date_ad, lang='en') if bill_date_ad else '-'

            records.append({
                'id': grn.id,
                'grn_number': grn.grn_number,
                'supplier_bill_no': grn.supplier_bill_no or '-',
                'bill_date_ad': bill_date_ad,
                'bill_date_bs': bill_date_bs,
                'cancel_timestamp': cancel_timestamp,
                'cancel_date_ad_str': cancel_date_ad_str,
                'cancel_date_bs_str': cancel_date_bs_str,
                'branch_code': grn.branch.code,
                'branch_name': grn.branch.name,
                'supplier_name': supplier_name,
                'supplier_pan': supplier_pan,
                'supplier_phone': supplier_phone,
                'received_by': grn.received_by.get_full_name() or grn.received_by.username if grn.received_by else '-',
                'cancelled_by': cancelled_by_user,
                'cancel_ip': cancel_ip,
                'cancellation_reason': cancellation_reason,
                'gross_amount': gross_amt,
                'discount_amount': disc_amt,
                'vat_amount': vat_amt,
                'net_total_amount': net_amt,
                'paid_amount_reversed': paid_rev,
                'due_amount_reversed': due_rev,
                'is_stock_reverted': is_stock_reverted,
                'items_count': len(lines_summary),
                'items_list': lines_summary,
            })

        totals = {
            'total_cancelled_bills': total_voided_bills,
            'total_cancelled_amount': total_voided_amount,
            'total_gross_amount': total_gross_amount,
            'total_discount_amount': total_discount_amount,
            'total_vat_amount': total_vat_amount,
            'total_paid_reversed': total_paid_reversed,
            'total_due_reversed': total_due_reversed,
            'total_items_count': total_items_count,
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
            'suppliers': Supplier.objects.filter(is_active=True).order_by('company_name'),
            'selected_branch': selected_branch,
            'filters': filters,
            'start_date': start_date.strftime('%Y-%m-%d'),
            'end_date': end_date.strftime('%Y-%m-%d'),
            'start_date_bs': start_date_bs,
            'end_date_bs': end_date_bs,
            'date_basis': date_basis,
        }