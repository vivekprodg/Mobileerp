"""
Cancelled & Voided Sales Forensic Audit Service.

Capabilities:
1. Forensic Anti-Fraud Audit Engine:
   - Queries and audits all SalesEstimate records strictly where status='CANCELLED'.
   - Tracks originating cashiers vs. the supervisor/manager who executed the void.
   - Extracts complete customer metadata: Customer Name, Customer Phone, Customer PAN.
   - Accurately captures cancellation timestamps, IP addresses, and formal cancellation reasons.
2. Stock Reversal & Inventory Integrity Verification:
   - Confirms whether physical stock was restored to BranchStock.
   - Cross-references StockMovementLog entries (reference_document=f"VOID-{estimate_number}").
   - Confirms line items returned, including serialized IMEI 1 and IMEI 2 devices.
3. Financial & Debt Reversal Analysis:
   - Reconciles reversed customer debt (Udhaari) and reversed cash/digital receipts.
   - Quantifies total revenue, trade discounts, and Output VAT voided.
"""

from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime
from typing import Dict, Any, List, Tuple, Optional

from django.db.models import (
    Q, Sum, Count, F, DecimalField, Value
)
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.sales.models import SalesEstimate, SalesEstimateItem
from apps.inventory.models import StockMovementLog, ItemInstance
from apps.branches.models import Branch
from apps.users.models import User
from apps.core.models import AuditLog
from apps.core.nepali_calendar import NepaliCalendar
from apps.core.utils.nepali_date_converter import ad_to_bs_string


class CancelledSalesService:
    """
    Business logic engine for forensic auditing of cancelled and voided sales invoices.
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
    def get_cancelled_sales_data(
        cls,
        filters: Dict[str, Any],
        user=None
    ) -> Dict[str, Any]:
        """
        Gathers all voided/cancelled sales invoices, cross-references forensic AuditLog
        entries to pinpoint the cancelling supervisor, extracts customer PAN and phone,
        and verifies stock reversal ledgers.
        """
        active_branch = getattr(user, 'assigned_branch', None) if user and not user.is_superuser else None

        start_date, end_date, start_date_bs, end_date_bs = cls.resolve_date_range(filters)
        branch_id = filters.get('branch_id') or filters.get('branch', '')
        cashier_id = filters.get('cashier_id') or filters.get('cashier', '')
        reason_query = str(filters.get('reason', '') or '').strip()
        search_query = str(filters.get('q', '') or '').strip()
        date_basis = str(filters.get('date_basis', 'bill_date') or 'bill_date').strip()

        # 1. Base Query: Sales estimates strictly with status='CANCELLED'
        estimates_qs = SalesEstimate.objects.select_related(
            'branch',
            'customer',
            'cashier',
            'salesperson',
            'manager_override_by'
        ).prefetch_related(
            'items__product',
            'items__product__base_unit',
            'items__item_instance',
            'payment_transactions'
        ).filter(status='CANCELLED')

        # Date Range Scoping (Bill Date or Cancellation Timestamp Date)
        if date_basis == 'cancel_date':
            estimates_qs = estimates_qs.filter(
                updated_at__date__gte=start_date,
                updated_at__date__lte=end_date
            )
        else:
            estimates_qs = estimates_qs.filter(
                bill_date_ad__gte=start_date,
                bill_date_ad__lte=end_date
            )

        # 2. Branch & Cashier Scoping
        if branch_id and str(branch_id).strip() not in ['', 'all', 'None']:
            estimates_qs = estimates_qs.filter(branch_id=branch_id)
        elif active_branch and not (user.is_superuser or getattr(user, 'role', '') == 'OWNER'):
            estimates_qs = estimates_qs.filter(branch=active_branch)

        if cashier_id and str(cashier_id).strip() not in ['', 'all', 'None']:
            estimates_qs = estimates_qs.filter(
                Q(cashier_id=cashier_id) | Q(salesperson_id=cashier_id)
            )

        if reason_query:
            estimates_qs = estimates_qs.filter(cancellation_reason__icontains=reason_query)

        if search_query:
            estimates_qs = estimates_qs.filter(
                Q(estimate_number__icontains=search_query) |
                Q(customer_name_manual__icontains=search_query) |
                Q(customer_phone_manual__icontains=search_query) |
                Q(customer_pan__icontains=search_query) |
                Q(customer__name__icontains=search_query) |
                Q(customer__phone_number__icontains=search_query) |
                Q(customer__pan_number__icontains=search_query) |
                Q(cancellation_reason__icontains=search_query) |
                Q(items__imei_number__icontains=search_query) |
                Q(items__secondary_imei__icontains=search_query) |
                Q(items__product__name__icontains=search_query)
            ).distinct()

        ordered_estimates = list(estimates_qs.order_by('-updated_at', '-created_at'))
        estimate_numbers = [e.estimate_number for e in ordered_estimates]

        # 3. Batch Query AuditLog for BILL_CANCEL to detect cancelling user, IP & exact timestamp
        cancel_logs_map: Dict[str, AuditLog] = {}
        if estimate_numbers:
            logs = AuditLog.objects.select_related('user').filter(
                action_type='BILL_CANCEL',
                object_repr__in=estimate_numbers
            ).order_by('-timestamp')
            for log in logs:
                if log.object_repr not in cancel_logs_map:
                    cancel_logs_map[log.object_repr] = log

        # 4. Batch Query StockMovementLog to verify stock reversal execution
        reversal_ref_docs = [f"VOID-{num}" for num in estimate_numbers]
        reversed_refs_set = set(
            StockMovementLog.objects.filter(
                reference_document__in=reversal_ref_docs,
                movement_type='SALE_RETURN'
            ).values_list('reference_document', flat=True).distinct()
        )

        # 5. Build Detailed Records & Reversal Metrics
        records: List[Dict[str, Any]] = []

        total_voided_bills = 0
        total_voided_amount = Decimal('0.00')
        total_subtotal = Decimal('0.00')
        total_discounts_voided = Decimal('0.00')
        total_vat_voided = Decimal('0.00')
        total_paid_reversed = Decimal('0.00')
        total_due_reversed = Decimal('0.00')
        total_items_count = 0

        for est in ordered_estimates:
            total_voided_bills += 1
            voided_amt = est.grand_total or Decimal('0.00')
            subtot = est.subtotal or Decimal('0.00')
            item_disc = est.item_discount_total or Decimal('0.00')
            bill_disc = est.bill_discount_amount or Decimal('0.00')
            tot_disc = item_disc + bill_disc
            vat_amt = est.vat_amount or Decimal('0.00')
            paid_rev = est.paid_amount or Decimal('0.00')
            due_rev = est.due_amount or Decimal('0.00')

            total_voided_amount += voided_amt
            total_subtotal += subtot
            total_discounts_voided += tot_disc
            total_vat_voided += vat_amt
            total_paid_reversed += paid_rev
            total_due_reversed += due_rev

            # Customer identity resolution (Manual entry takes precedence, falls back to Customer model)
            cust_name = est.recipient_display_name or (est.customer.name if est.customer else 'Walk-in Customer')
            cust_phone = est.customer_phone_manual or (est.customer.phone_number if est.customer and est.customer.phone_number else '-')
            cust_pan = est.customer_pan or (est.customer.pan_number if est.customer and est.customer.pan_number else '-')

            # Cancelling user and timestamp resolution
            audit_entry = cancel_logs_map.get(est.estimate_number)
            if audit_entry:
                cancelled_by_user = (
                    audit_entry.user.get_full_name() or audit_entry.user.username
                    if audit_entry.user else "System"
                )
                cancel_ip = audit_entry.ip_address or "-"
                cancel_timestamp = audit_entry.timestamp
            else:
                cancelled_by_user = (
                    est.manager_override_by.get_full_name() or est.manager_override_by.username
                    if est.manager_override_by
                    else (est.cashier.get_full_name() or est.cashier.username if est.cashier else "Manager / Admin")
                )
                cancel_ip = "-"
                cancel_timestamp = est.updated_at

            cancel_date_ad_str = cancel_timestamp.strftime('%Y-%m-%d %H:%M:%S') if cancel_timestamp else '-'
            cancel_date_bs_str = ad_to_bs_string(cancel_timestamp.date(), lang='en') if cancel_timestamp else '-'

            # Stock reversal check
            is_stock_reverted = f"VOID-{est.estimate_number}" in reversed_refs_set

            # Build line items summary for this voided bill
            lines_summary = []
            for item in est.items.all():
                total_items_count += int(item.quantity)
                lines_summary.append({
                    'product_name': item.product.name,
                    'sku': item.product.sku,
                    'quantity': item.quantity,
                    'unit_code': item.product.base_unit.code if item.product.base_unit else 'PCS',
                    'unit_price': item.unit_price,
                    'line_total': item.line_total,
                    'imei_1': item.imei_number or '',
                    'imei_2': item.secondary_imei or '',
                })

            records.append({
                'id': est.id,
                'estimate_number': est.estimate_number,
                'bill_date_ad': est.bill_date_ad,
                'bill_date_bs': est.bill_date_bs or ad_to_bs_string(est.bill_date_ad, lang='en'),
                'cancel_timestamp': cancel_timestamp,
                'cancel_date_ad_str': cancel_date_ad_str,
                'cancel_date_bs_str': cancel_date_bs_str,
                'branch_code': est.branch.code,
                'branch_name': est.branch.name,
                'customer_name': cust_name,
                'customer_phone': cust_phone,
                'customer_pan': cust_pan,
                'cashier': est.cashier.get_full_name() or est.cashier.username,
                'salesperson': est.salesperson.username if est.salesperson else est.cashier.username,
                'cancelled_by': cancelled_by_user,
                'cancel_ip': cancel_ip,
                'cancellation_reason': est.cancellation_reason or 'No formal reason recorded',
                'subtotal': subtot,
                'total_discounts': tot_disc,
                'vat_amount': vat_amt,
                'grand_total': voided_amt,
                'paid_amount_reversed': paid_rev,
                'due_amount_reversed': due_rev,
                'is_stock_reverted': is_stock_reverted,
                'items_count': len(lines_summary),
                'items_list': lines_summary,
            })

        totals = {
            'total_voided_bills': total_voided_bills,
            'total_voided_amount': total_voided_amount,
            'total_subtotal': total_subtotal,
            'total_discounts_voided': total_discounts_voided,
            'total_vat_voided': total_vat_voided,
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
            'cashiers': User.objects.filter(is_active=True).order_by('username'),
            'selected_branch': selected_branch,
            'filters': filters,
            'start_date': start_date.strftime('%Y-%m-%d'),
            'end_date': end_date.strftime('%Y-%m-%d'),
            'start_date_bs': start_date_bs,
            'end_date_bs': end_date_bs,
            'date_basis': date_basis,
        }