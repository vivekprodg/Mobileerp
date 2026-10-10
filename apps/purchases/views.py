"""
Purchases, Suppliers, GRN & Commercial Purchase Return (Debit Note) Views.

Key Architectural Improvements & Capabilities:
1. Reversal Architecture Correction (Fixes Double-Reversal Bug in cancel_grn_view):
   - Adopts the standard double-entry audit convention: the original purchase journal entry
     remains POSTED (annotated with reversal cross-references) while a distinct reversing entry
     is posted with status='POSTED' that inverts debits and credits.
   - Prevents balance recalculations from double-subtracting cancelled bills into negative balances.

2. Settlement-Aware Supplier Debt Cancellation (Fixes Unearned Debt Deduction):
   - Differentiates between unpaid credit balances (due_amount > 0) and spot payments (paid_amount > 0).
   - If a bill was an unpaid credit purchase, reverses strictly the unpaid debt added to supplier AP.
   - If a bill was paid in full on delivery (due_amount == 0), does not penalize unrelated debts;
     records the disbursed cash as an advance due from the supplier / pending refund.

3. Supplier Payout Historical Date & Calendar Ingestion (Fixes Backdating Amnesia):
   - SupplierPaymentRecordView extracts and synchronizes entry_date (AD) and entry_date_bs (BS).
   - Stamps the sub-ledger and passes ledger_entry and date_ad directly to the General Ledger
     so historical payment dates are preserved in both ledgers.

4. Standardized Chart of Accounts Fallback Bindings:
   - Fallback GL poster adheres to official Nepal COA codes:
     Cash in Hand (1110), Bank (1120), and Accounts Payable (2110).

5. Optimized GRNDetailView (The Kitchen Chef):
   - Point A: Prefetches products, brands, categories, base units, and packaging conversions
     in one single optimized query, eliminating N+1 database round trips.
   - Point B: Pre-packages the 5-Card Financial reconciliation dictionary directly in the view
     (including true non-taxable merchandise base) to guarantee zero calculation discrepancies.
   - Point C: Prepares and checks parsed_imei_pairs across all items, aggregating handset counts,
     dual-SIM indicators, and warranty providers cleanly for the expandable drawer template.
   - Safe Staff Preparer: Defensively handles nullable grn.received_by, resolving full name,
     username, or a polite fallback string ('Warehouse Staff') so template rendering never crashes.

6. Dedicated Physical IMEI Scanner Controller (GRNManageIMEIsView):
   - Displays all items on the purchase bill.
   - Automatically enables requires_imei_tracking = True if IMEIs are added to an untracked product.
   - Zero Stock Inflation: Does NOT touch BranchStock quantities.
   - Zero Accounting Impact: Does NOT alter purchase costs, VAT, or supplier debt.

7. High-Performance Multi-Token Search APIs:
   - Asynchronous lookup for suppliers and verified inward GRN consignments.

8. Comprehensive Purchase Return VAT ➔ GL Audit Linkage (PurchaseReturnDetailView):
   - Resolves the complete accounting chain when accessed from VAT reporting drill-down:
     Debit Note ➔ GL Journal Entry ➔ Journal Items ➔ Input VAT 1410 Reversal,
     with the Original GRN / Bill Reference as reference.
"""

import csv
import json
import uuid
import re
import logging
from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime

from django.shortcuts import render, redirect, get_object_or_404
from django.views.generic import ListView, DetailView, CreateView, UpdateView, View
from django.views.decorators.http import require_http_methods
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib import messages
from django.urls import reverse_lazy, reverse
from django.db import transaction
from django.db.models import Q, Sum, Count, F, Prefetch
from django.utils import timezone
from django.http import HttpResponse, JsonResponse
from django.core.exceptions import ValidationError

from apps.purchases.models import (
    Supplier, PurchaseOrder, PurchaseOrderItem,
    GoodsReceivedNote, GRNItem, SupplierUdhaariLedger,
    PurchaseReturn, PurchaseReturnItem
)
from apps.purchases.forms import (
    SupplierForm, PurchaseOrderForm, PurchaseOrderItemFormSet,
    GoodsReceivedNoteForm, GRNItemFormSet, SupplierPaymentForm,
    PurchaseReturnForm, PurchaseReturnItemFormSet
)
from apps.purchases.services import PurchaseService, PurchaseReturnService
from apps.branches.models import Branch, BranchDocumentSequence
from apps.inventory.models import Product, ItemInstance, ProductBatch, StockMovementLog
from apps.inventory.services import InventoryService
from apps.reports.exports import sanitize_csv_row
from apps.core.models import SystemConfiguration, AuditLog
from apps.core.nepali_calendar import NepaliCalendar
from apps.core.utils.nepali_date_converter import parse_bs_date_components
from apps.accounting.models import AccountingFiscalYear

logger = logging.getLogger(__name__)

# =============================================================================
# GENERAL LEDGER DISPATCHER BRIDGE (STANDARDIZED TO MASTER COA 1110/1120/2110)
# =============================================================================
try:
    import apps.accounting.services.auto_posting as auto_posting_mod

    if not hasattr(auto_posting_mod, 'post_supplier_payout_journal'):
        def _post_supplier_payout_journal(
            supplier,
            amount,
            payment_mode,
            ref_no=None,
            user=None,
            branch=None,
            ledger_entry=None,
            date_ad=None,
            **kwargs
        ):
            """
            Fallback double-entry poster for supplier payout settlements:
            - Dr: Accounts Payable 2110 (Supplier Control Account)
            - Cr: Cash in Hand 1110 (if CASH) or Bank Current Account 1120
            """
            try:
                from apps.accounting.models import JournalEntry
                from apps.accounting.services.auto_posting import JournalEngine, AutoPostingService
                from apps.branches.models import Branch

                amount_dec = Decimal(str(amount)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                if amount_dec <= Decimal('0.00'):
                    raise ValidationError("Payout amount must be greater than zero.")

                ref_document = ref_no or f"SUP-PAY-{supplier.id}-{timezone.now().strftime('%Y%m%d%H%M%S')}"
                existing = JournalEntry.objects.filter(
                    voucher_type='PAYMENT',
                    reference_document=ref_document,
                    status='POSTED'
                ).first()
                if existing:
                    return existing

                target_branch = branch or getattr(supplier, 'preferred_branch', None) or Branch.get_default_main_branch()

                # Official Nepal Master Chart of Accounts Standard Codes
                cash_acc = AutoPostingService.get_or_create_control_account(
                    target_branch, 'CASH', '1110', 'Cash in Hand (Main Drawer)', 'ASSET', 'DEBIT'
                )
                bank_acc = AutoPostingService.get_or_create_control_account(
                    target_branch, 'BANK', '1120', 'Primary Bank Current Account', 'ASSET', 'DEBIT'
                )
                ap_acc = AutoPostingService.get_or_create_control_account(
                    target_branch, 'ACCOUNTS_PAYABLE', '2110', 'Accounts Payable (Trade Creditors)', 'LIABILITY', 'CREDIT'
                )

                mode_upper = str(payment_mode or '').upper().strip()
                if mode_upper in ['CASH']:
                    src_acc = cash_acc
                elif mode_upper in ['FONEPAY', 'QR', 'DYNAMIC_QR']:
                    src_acc = AutoPostingService.get_or_create_control_account(
                        target_branch, 'FONEPAY', '1130', 'FonePay / QR Settlement Clearing', 'ASSET', 'DEBIT'
                    )
                else:
                    src_acc = bank_acc

                # Determine True Historical Date
                effective_date = date_ad
                if not effective_date and ledger_entry:
                    effective_date = getattr(ledger_entry, 'entry_date', None)
                if not effective_date:
                    effective_date = timezone.now().date()
                elif isinstance(effective_date, datetime):
                    effective_date = effective_date.date()

                lines = [
                    {
                        'account': ap_acc,
                        'debit': amount_dec,
                        'credit': Decimal('0.00'),
                        'supplier': supplier,
                        'narration': f"Payout settlement to {supplier.company_name}"
                    },
                    {
                        'account': src_acc,
                        'debit': Decimal('0.00'),
                        'credit': amount_dec,
                        'supplier': supplier,
                        'narration': f"Disbursement via {mode_upper} (Ref: {ref_document})"
                    }
                ]

                narration = f"Supplier debt payout: {supplier.company_name} (Rs. {amount_dec:.2f}) via {mode_upper}"
                return JournalEngine.create_balanced_entry(
                    voucher_type='PAYMENT',
                    date_ad=effective_date,
                    branch=target_branch,
                    lines=lines,
                    narration=narration,
                    reference_doc=ref_document,
                    source_module='SUPPLIER_PAYMENT',
                    source_id=str(getattr(ledger_entry, 'id', ref_document)),
                    user=user,
                    auto_post=True
                )
            except Exception as err:
                logger.error(f"[post_supplier_payout_journal Fallback Error] Supplier {supplier.id}: {err}", exc_info=True)
                raise

        auto_posting_mod.post_supplier_payout_journal = _post_supplier_payout_journal
except Exception:
    pass

class PurchaseModuleAccessMixin(LoginRequiredMixin, UserPassesTestMixin):
    """
    Enforces strict role-based access control across all supplier records,
    wholesale purchase costs, landed cost calculations, POs, GRN inward, and purchase returns.
    Restricts access strictly to Superusers, Shop Owners, Branch Managers, and Accountants.
    """

    def test_func(self):
        user = self.request.user
        return user.is_authenticated and (
            user.is_superuser or getattr(user, 'role', '') in ['OWNER', 'MANAGER', 'ACCOUNTANT']
        )

    def handle_no_permission(self):
        messages.error(
            self.request,
            "Permission Denied: Access to procurement records, supplier debit notes, and wholesale data is restricted to Store Owners, Managers, and Accountants."
        )
        return redirect('core:dashboard')

# ==============================================================================
# ASYNC SEARCH APIS (SUPPLIER & GRN)
# ==============================================================================
class SupplierSearchAPIView(PurchaseModuleAccessMixin, View):
    """
    High-performance API endpoint for async Supplier search/autocomplete in Purchase Orders,
    GRNs, Debit Notes, and Payment Modals.
    """

    def get(self, request, *args, **kwargs):
        query = request.GET.get('q', '').strip()
        limit = min(int(request.GET.get('limit', 150)), 200)

        qs = Supplier.objects.filter(status='ACTIVE')

        if query:
            tokens = [t.strip() for t in query.split() if t.strip()]
            for token in tokens:
                qs = qs.filter(
                    Q(code__icontains=token) |
                    Q(company_name__icontains=token) |
                    Q(contact_person__icontains=token) |
                    Q(phone_number__icontains=token) |
                    Q(registration_number__icontains=token) |
                    Q(pan_number__icontains=token)
                )
        else:
            qs = qs.order_by('company_name')

        results = []
        for s in qs[:limit]:
            bal = s.current_balance or Decimal('0.00')
            if bal > Decimal('0.00'):
                badge = 'PAYABLE'
                badge_text = f"Due: Rs. {bal:,.2f}"
            elif bal < Decimal('0.00'):
                badge = 'ADVANCE'
                badge_text = f"Advance: Rs. {abs(bal):,.2f}"
            else:
                badge = 'SETTLED'
                badge_text = "Settled (Rs. 0.00)"

            clean_classification = re.sub(r'\s*\([^)]*\)', '', s.get_supplier_type_display()).strip()

            results.append({
                'id': s.id,
                'code': s.code,
                'company_name': s.company_name,
                'contact_person': s.contact_person,
                'phone_number': s.phone_number,
                'city': s.city or '',
                'registration_number': s.registration_number or '',
                'pan_number': s.pan_number or '',
                'current_balance': float(bal),
                'current_balance_formatted': f"Rs. {bal:,.2f}",
                'balance_badge': badge,
                'balance_badge_text': badge_text,
                'supplier_type': clean_classification,
                'text': f"{s.company_name} ({s.contact_person} - {s.phone_number})"
            })

        return JsonResponse({'results': results, 'count': len(results)})

class GRNSearchAPIView(PurchaseModuleAccessMixin, View):
    """
    API endpoint for async lookup of Goods Received Notes (GRNs) during Purchase Return / Debit Note creation.
    Allows staff to locate verified inward consignment bills by grn_number, supplier_bill_no, or supplier.
    """

    def get(self, request, *args, **kwargs):
        query = request.GET.get('q', '').strip()
        supplier_id = request.GET.get('supplier_id', '').strip()
        active_branch = getattr(request, 'active_branch', None)
        limit = min(int(request.GET.get('limit', 25)), 50)

        qs = GoodsReceivedNote.objects.filter(status='RECEIVED').select_related('supplier', 'branch')

        if active_branch and not request.user.is_superuser:
            qs = qs.filter(branch=active_branch)

        if supplier_id:
            qs = qs.filter(supplier_id=supplier_id)

        if query:
            qs = qs.filter(
                Q(grn_number__icontains=query) |
                Q(supplier_bill_no__icontains=query) |
                Q(challan_no__icontains=query) |
                Q(supplier__company_name__icontains=query) |
                Q(bill_date_bs__icontains=query)
            )

        qs = qs.order_by('-bill_date', '-id')[:limit]

        results = []
        for grn in qs:
            results.append({
                'id': grn.id,
                'grn_number': grn.grn_number,
                'supplier_bill_no': grn.supplier_bill_no,
                'challan_no': grn.challan_no or '',
                'supplier_id': grn.supplier_id,
                'supplier_name': grn.supplier.company_name,
                'supplier_code': grn.supplier.code,
                'bill_date': str(grn.bill_date),
                'bill_date_bs': grn.bill_date_bs or '',
                'fiscal_year': grn.fiscal_year or '',
                'gross_amount': float(grn.gross_amount),
                'taxable_amount': float(grn.taxable_amount),
                'vat_amount': float(grn.vat_amount),
                'total_landed_cost': float(grn.total_landed_cost),
                'net_total_amount': float(grn.net_total_amount),
                'net_total_amount_formatted': f"Rs. {grn.net_total_amount:,.2f}",
                'due_amount': float(grn.due_amount),
                'status': grn.status,
                'items_count': grn.items.count(),
                'text': f"{grn.grn_number} | Bill: {grn.supplier_bill_no} | {grn.supplier.company_name} (Rs. {grn.net_total_amount:,.2f})"
            })

        return JsonResponse({'results': results, 'count': len(results)})

# ==============================================================================
# SUPPLIER CRUD & LEDGER VIEWS
# ==============================================================================
class SupplierListView(PurchaseModuleAccessMixin, ListView):
    model = Supplier
    template_name = 'purchases/supplier_list.html'
    context_object_name = 'suppliers'
    paginate_by = 25

    def get_queryset(self):
        qs = Supplier.objects.all()
        query = self.request.GET.get('q', '').strip()
        s_type = self.request.GET.get('type', '').strip()
        status_filter = self.request.GET.get('status', '').strip()
        has_due = self.request.GET.get('has_due', '').strip()

        if query:
            tokens = [t.strip() for t in query.split() if t.strip()]
            for token in tokens:
                qs = qs.filter(
                    Q(code__icontains=token) |
                    Q(company_name__icontains=token) |
                    Q(contact_person__icontains=token) |
                    Q(phone_number__icontains=token) |
                    Q(registration_number__icontains=token) |
                    Q(pan_number__icontains=token)
                )
        if s_type:
            qs = qs.filter(supplier_type=s_type)
        if status_filter:
            qs = qs.filter(status=status_filter)
        if has_due == 'true':
            qs = qs.filter(current_balance__gt=Decimal('0.00'))

        return qs.order_by('-current_balance', 'company_name')

class SupplierDetailView(PurchaseModuleAccessMixin, DetailView):
    model = Supplier
    template_name = 'purchases/supplier_detail.html'
    context_object_name = 'supplier'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['recent_grns'] = self.object.goods_receipts.select_related('branch')[:15]
        context['recent_pos'] = self.object.purchase_orders.select_related('branch')[:10]
        context['recent_returns'] = self.object.purchase_returns.select_related('branch')[:10]
        context['ledger_entries'] = self.object.ledger_entries.select_related('branch', 'recorded_by')[:30]

        # Initialize Payment Form with default today's dates
        today_ad = timezone.now().date()
        try:
            bs_y, bs_m, bs_d = NepaliCalendar.ad_to_bs(today_ad)
            today_bs = NepaliCalendar.format_bs(bs_y, bs_m, bs_d, lang='en')
        except Exception:
            today_bs = ''

        context['payment_form'] = SupplierPaymentForm(initial={
            'entry_date': today_ad,
            'entry_date_bs': today_bs
        })

        # Resolve active Nepali Fiscal Year for party confirmation link
        try:
            current_fy = NepaliCalendar.get_fiscal_year(bs_y, bs_m)
        except Exception:
            active_fy_obj = AccountingFiscalYear.objects.filter(is_closed=False).first()
            current_fy = active_fy_obj.name if active_fy_obj else '2083/84'

        context['active_fiscal_year'] = current_fy
        return context

class SupplierCreateView(PurchaseModuleAccessMixin, CreateView):
    model = Supplier
    form_class = SupplierForm
    template_name = 'purchases/supplier_form.html'
    success_url = reverse_lazy('purchases:supplier_list')

    def form_valid(self, form):
        response = super().form_valid(form)
        AuditLog.objects.create(
            user=self.request.user,
            branch=getattr(self.request, 'active_branch', None),
            action_type='CREATE',
            module='Supplier',
            object_repr=str(self.object),
            ip_address=self.request.META.get('REMOTE_ADDR'),
            details={
                'code': self.object.code,
                'company_name': self.object.company_name,
                'phone': self.object.phone_number,
                'reg_no': self.object.registration_number
            }
        )
        messages.success(self.request, f"Supplier '{self.object.company_name}' ({self.object.code}) registered successfully.")
        return response

class SupplierUpdateView(PurchaseModuleAccessMixin, UpdateView):
    model = Supplier
    form_class = SupplierForm
    template_name = 'purchases/supplier_form.html'

    def get_success_url(self):
        return reverse('purchases:supplier_detail', kwargs={'pk': self.object.pk})

    def form_valid(self, form):
        response = super().form_valid(form)
        AuditLog.objects.create(
            user=self.request.user,
            branch=getattr(self.request, 'active_branch', None),
            action_type='UPDATE',
            module='Supplier',
            object_repr=str(self.object),
            ip_address=self.request.META.get('REMOTE_ADDR'),
            details={'updated_fields': list(form.changed_data)}
        )
        messages.success(self.request, f"Supplier '{self.object.company_name}' updated successfully.")
        return response

class SupplierPaymentRecordView(PurchaseModuleAccessMixin, View):
    """
    Processes payouts made to suppliers via Cash, Bank Transfer, or Cheque.
    Features:
    - Ingests true historical dates (entry_date and entry_date_bs) with dual-calendar synchronization.
    - Validates that retroactive dates do not fall into closed fiscal periods.
    - Acquires row-level locks on Supplier inside an atomic database transaction.
    - Synchronizes SupplierUdhaariLedger, updates supplier.last_payment_date, and auto-posts to the General Ledger.
    """

    def post(self, request, pk, *args, **kwargs):
        form = SupplierPaymentForm(request.POST)

        if form.is_valid():
            amount = form.cleaned_data['amount']
            payment_mode = form.cleaned_data['payment_mode']
            ref_no = form.cleaned_data.get('reference_number', '')
            cheque_dt = form.cleaned_data.get('cheque_date')
            remarks = form.cleaned_data.get('remarks', '')

            # 1. Extract and Synchronize Historical Payment Dates
            entry_date = form.cleaned_data.get('entry_date')
            entry_date_bs = form.cleaned_data.get('entry_date_bs')

            if entry_date_bs and not entry_date:
                try:
                    bs_y, bs_m, bs_d = parse_bs_date_components(str(entry_date_bs).strip())
                    entry_date = NepaliCalendar.bs_to_ad(bs_y, bs_m, bs_d)
                except Exception as ex:
                    messages.error(request, f"Invalid Nepali payment date: {ex}")
                    return redirect('purchases:supplier_detail', pk=pk)
            elif entry_date and not entry_date_bs:
                try:
                    ad_d = entry_date.date() if isinstance(entry_date, datetime) else entry_date
                    bs_y, bs_m, bs_d = NepaliCalendar.ad_to_bs(ad_d)
                    entry_date_bs = NepaliCalendar.format_bs(bs_y, bs_m, bs_d, lang='en')
                except Exception:
                    pass

            if not entry_date:
                entry_date = timezone.now().date()
            elif isinstance(entry_date, datetime):
                entry_date = entry_date.date()

            # Closed Fiscal Year Pre-check
            try:
                bs_y, bs_m, _ = NepaliCalendar.ad_to_bs(entry_date)
                fy_name = NepaliCalendar.get_fiscal_year(bs_y, bs_m)
                locked_fy = AccountingFiscalYear.objects.filter(name=fy_name, is_closed=True).first()
                if locked_fy:
                    active_open_fy = AccountingFiscalYear.objects.filter(is_closed=False).order_by('-start_date_ad').first()
                    open_name = active_open_fy.name if active_open_fy else "an active fiscal year (2083/84)"
                    messages.error(
                        request,
                        f"Payment Rejected: Date ({entry_date_bs or entry_date}) falls into Fiscal Year {fy_name}, "
                        f"which is audited and closed. Only payouts within {open_name} are permitted."
                    )
                    return redirect('purchases:supplier_detail', pk=pk)
            except Exception as e:
                logger.warning(f"[SupplierPaymentRecordView] FY check: {e}")

            try:
                with transaction.atomic():
                    supplier = Supplier.objects.select_for_update().get(pk=pk)
                    prev_bal = supplier.current_balance or Decimal('0.00')

                    active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()

                    # 2. Record sub-ledger entry with historical entry dates
                    ledger_entry = SupplierUdhaariLedger.objects.create(
                        supplier=supplier,
                        branch=active_branch,
                        transaction_type='PAYMENT',
                        amount=amount,
                        previous_balance=prev_bal,
                        resulting_balance=prev_bal - amount,
                        payment_mode=payment_mode,
                        reference_number=ref_no,
                        entry_date=entry_date,
                        entry_date_bs=entry_date_bs,
                        cheque_date=cheque_dt,
                        remarks=remarks,
                        recorded_by=request.user
                    )

                    # 3. Recalculate balance strictly from sub-ledger entries
                    new_bal = supplier.recalculate_balance_from_ledger(save=True)
                    if ledger_entry.resulting_balance != new_bal:
                        ledger_entry.resulting_balance = new_bal
                        ledger_entry.save(update_fields=['resulting_balance'])

                    # Update last payment date on supplier profile
                    supplier.last_payment_date = entry_date
                    supplier.save(update_fields=['last_payment_date', 'updated_at'])

                    # 4. Post Double-Entry Journal to General Ledger with historical date
                    import apps.accounting.services.auto_posting as auto_posting_service
                    gl_entry = auto_posting_service.post_supplier_payout_journal(
                        ledger_entry=ledger_entry,
                        supplier=supplier,
                        amount=amount,
                        payment_mode=payment_mode,
                        ref_no=ref_no or f"SUP-PAY-{ledger_entry.id}",
                        date_ad=entry_date,
                        user=request.user,
                        branch=active_branch
                    )
                    if gl_entry is None:
                        raise ValidationError(
                            f"General Ledger auto-posting failed for supplier payout to '{supplier.company_name}'. "
                            "Transaction rolled back to preserve ledger consistency."
                        )

                    # 5. Record Audit Log
                    AuditLog.objects.create(
                        user=request.user,
                        branch=active_branch,
                        action_type='UPDATE',
                        module='SupplierPayment',
                        object_repr=f"Payout to {supplier.company_name}",
                        ip_address=request.META.get('REMOTE_ADDR'),
                        details={
                            'amount': str(amount),
                            'entry_date_ad': str(entry_date),
                            'entry_date_bs': entry_date_bs or '',
                            'prev_balance': str(prev_bal),
                            'new_balance': str(new_bal),
                            'payment_mode': payment_mode,
                            'gl_entry_id': getattr(gl_entry, 'id', str(gl_entry))
                        }
                    )

                date_label = f" ({entry_date_bs} BS)" if entry_date_bs else ""
                messages.success(
                    request,
                    f"Payment of Rs. {amount:,.2f} recorded for {supplier.company_name} on {entry_date}{date_label} "
                    f"(New Balance: Rs. {new_bal:,.2f})."
                )
                return redirect('purchases:supplier_detail', pk=supplier.pk)

            except Supplier.DoesNotExist:
                messages.error(request, "Supplier record not found.")
                return redirect('purchases:supplier_list')
            except (ValidationError, Exception) as e:
                logger.error(f"[Supplier Payment Error] Supplier {pk}: {e}", exc_info=True)
                messages.error(request, f"Error processing payment: {str(e)}")
                return redirect('purchases:supplier_detail', pk=pk)
        else:
            err_msgs = []
            for field, errs in form.errors.items():
                err_msgs.append(f"{field.replace('_', ' ').title()}: {', '.join(errs)}")
            messages.error(request, f"Invalid payment values: {'; '.join(err_msgs)}")
            return redirect('purchases:supplier_detail', pk=pk)

# ==============================================================================
# PURCHASE ORDER (PO) VIEWS
# ==============================================================================
class PurchaseOrderListView(PurchaseModuleAccessMixin, ListView):
    model = PurchaseOrder
    template_name = 'purchases/po_list.html'
    context_object_name = 'orders'
    paginate_by = 25

    def get_queryset(self):
        qs = PurchaseOrder.objects.select_related('supplier', 'branch', 'created_by').prefetch_related('items__product')
        branch = getattr(self.request, 'active_branch', None)
        if branch and not self.request.user.is_superuser:
            qs = qs.filter(branch=branch)

        q = self.request.GET.get('q', '').strip()
        status_filter = self.request.GET.get('status', '').strip()
        supplier_id = self.request.GET.get('supplier', '').strip()
        start_date = self.request.GET.get('start_date', '').strip()
        end_date = self.request.GET.get('end_date', '').strip()

        if q:
            qs = qs.filter(
                Q(po_number__icontains=q) |
                Q(supplier__company_name__icontains=q) |
                Q(supplier__phone_number__icontains=q) |
                Q(notes__icontains=q)
            )
        if status_filter:
            qs = qs.filter(status=status_filter)
        if supplier_id:
            qs = qs.filter(supplier_id=supplier_id)
        if start_date:
            qs = qs.filter(order_date__gte=start_date)
        if end_date:
            qs = qs.filter(order_date__lte=end_date)

        return qs.order_by('-order_date', '-created_at')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        branch = getattr(self.request, 'active_branch', None)
        base_qs = PurchaseOrder.objects.all()
        if branch and not self.request.user.is_superuser:
            base_qs = base_qs.filter(branch=branch)

        context.update({
            'kpi_total_pos': base_qs.count(),
            'kpi_issued_pos': base_qs.filter(status__in=['ISSUED', 'PARTIALLY_RECEIVED']).count(),
            'kpi_pending_value': base_qs.filter(status__in=['ISSUED', 'PARTIALLY_RECEIVED']).aggregate(val=Sum('total_amount'))['val'] or Decimal('0.00'),
            'kpi_completed_pos': base_qs.filter(status='COMPLETED').count(),
            'suppliers': Supplier.objects.filter(is_active=True).order_by('company_name'),
        })
        return context

class PurchaseOrderCreateView(PurchaseModuleAccessMixin, View):
    template_name = 'purchases/po_form.html'

    def get(self, request, *args, **kwargs):
        branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()
        form = PurchaseOrderForm()
        formset = PurchaseOrderItemFormSet()
        products = Product.objects.filter(is_active=True).select_related('base_unit').order_by('name')
        return render(request, self.template_name, {
            'form': form,
            'formset': formset,
            'source_branch': branch,
            'products': products
        })

    def post(self, request, *args, **kwargs):
        branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()
        form = PurchaseOrderForm(request.POST)
        formset = PurchaseOrderItemFormSet(request.POST)

        if form.is_valid() and formset.is_valid():
            try:
                with transaction.atomic():
                    po = form.save(commit=False)
                    po.branch = branch
                    po.created_by = request.user

                    po_seq = f"PO-{branch.code}-{uuid.uuid4().hex[:6].upper()}"
                    po.po_number = po_seq
                    po.status = 'DRAFT'
                    po.subtotal = Decimal('0.00')
                    po.total_amount = Decimal('0.00')
                    po.save()

                    items = formset.save(commit=False)
                    valid_items_count = 0
                    running_subtotal = Decimal('0.00')

                    for item in items:
                        if item.product and item.ordered_quantity > Decimal('0.000'):
                            item.purchase_order = po
                            if not item.unit_id and item.product.base_unit_id:
                                item.unit = item.product.base_unit

                            cost = item.unit_cost_price if item.unit_cost_price is not None else item.product.purchase_price
                            item.unit_cost_price = cost
                            line_tot = (item.ordered_quantity * cost).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                            item.line_total = line_tot
                            item.received_quantity = Decimal('0.000')
                            item.save()

                            running_subtotal += line_tot
                            valid_items_count += 1

                    for del_item in formset.deleted_objects:
                        del_item.delete()

                    if valid_items_count == 0:
                        raise ValidationError("Please add at least one valid product line item with ordered quantity > 0.")

                    po.subtotal = running_subtotal
                    po.total_amount = running_subtotal + po.tax_amount
                    po.save(update_fields=['subtotal', 'total_amount', 'updated_at'])

                    AuditLog.objects.create(
                        user=request.user,
                        branch=branch,
                        action_type='CREATE',
                        module='PurchaseOrder',
                        object_repr=po.po_number,
                        ip_address=request.META.get('REMOTE_ADDR'),
                        details={
                            'supplier': po.supplier.company_name,
                            'total_amount': str(po.total_amount),
                            'items_count': valid_items_count
                        }
                    )

                messages.success(request, f"Purchase Order {po.po_number} created with {valid_items_count} item lines (Total: Rs. {po.total_amount:,.2f}).")
                return redirect('purchases:po_detail', pk=po.pk)

            except Exception as e:
                messages.error(request, f"Error generating purchase order: {str(e)}")
        else:
            messages.error(request, "Validation errors occurred in the purchase order submission. Please check all line entries.")

        products = Product.objects.filter(is_active=True).select_related('base_unit').order_by('name')
        return render(request, self.template_name, {
            'form': form,
            'formset': formset,
            'source_branch': branch,
            'products': products
        })

class PurchaseOrderDetailView(PurchaseModuleAccessMixin, DetailView):
    model = PurchaseOrder
    template_name = 'purchases/po_detail.html'
    context_object_name = 'order'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['items'] = self.object.items.select_related('product', 'unit').all()
        context['linked_grns'] = self.object.grn_vouchers.select_related('branch', 'received_by').all()
        return context

class PurchaseOrderStatusUpdateView(PurchaseModuleAccessMixin, View):
    """Updates the workflow status of an existing Purchase Order."""

    def post(self, request, pk, *args, **kwargs):
        order = get_object_or_404(PurchaseOrder, pk=pk)
        new_status = request.POST.get('status', '').strip().upper()

        valid_statuses = [choice[0] for choice in PurchaseOrder.PO_STATUS]
        if new_status not in valid_statuses:
            messages.error(request, "Invalid status choice selected.")
            return redirect('purchases:po_detail', pk=order.pk)

        prev_status = order.status
        order.status = new_status
        order.save(update_fields=['status', 'updated_at'])

        AuditLog.objects.create(
            user=request.user,
            branch=order.branch,
            action_type='UPDATE',
            module='PurchaseOrder',
            object_repr=f"Status: {order.po_number}",
            ip_address=request.META.get('REMOTE_ADDR'),
            details={'old_status': prev_status, 'new_status': new_status}
        )

        messages.success(request, f"Purchase Order {order.po_number} status updated to '{order.get_status_display()}'.")
        return redirect('purchases:po_detail', pk=order.pk)

# ==============================================================================
# GOODS RECEIVED NOTE (GRN) & INWARD STOCK VIEWS
# ==============================================================================
class GRNListView(PurchaseModuleAccessMixin, ListView):
    model = GoodsReceivedNote
    template_name = 'purchases/grn_list.html'
    context_object_name = 'grns'
    paginate_by = 25

    def get_queryset(self):
        qs = GoodsReceivedNote.objects.select_related('supplier', 'branch', 'received_by', 'purchase_order')
        active_branch = getattr(self.request, 'active_branch', None)
        if active_branch and not self.request.user.is_superuser:
            qs = qs.filter(branch=active_branch)

        query = self.request.GET.get('q', '').strip()
        status_filter = self.request.GET.get('status', '').strip()
        vat_mode = self.request.GET.get('vat_mode', '').strip()
        is_vat = self.request.GET.get('is_vat', '').strip()
        supplier_id = self.request.GET.get('supplier', '').strip()
        start_date = self.request.GET.get('start_date', '').strip()
        end_date = self.request.GET.get('end_date', '').strip()

        if query:
            qs = qs.filter(
                Q(grn_number__icontains=query) |
                Q(supplier_bill_no__icontains=query) |
                Q(challan_no__icontains=query) |
                Q(supplier__company_name__icontains=query) |
                Q(fiscal_year__icontains=query) |
                Q(bill_date_bs__icontains=query)
            )
        if status_filter:
            qs = qs.filter(status=status_filter)
        if vat_mode:
            qs = qs.filter(vat_handling_mode=vat_mode)
        if is_vat == 'true':
            qs = qs.filter(is_vat_bill=True)
        elif is_vat == 'false':
            qs = qs.filter(is_vat_bill=False)
        if supplier_id:
            qs = qs.filter(supplier_id=supplier_id)
        if start_date:
            qs = qs.filter(bill_date__gte=start_date)
        if end_date:
            qs = qs.filter(bill_date__lte=end_date)

        return qs.order_by('-bill_date', '-created_at')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        base_qs = self.get_queryset()

        aggregates = base_qs.aggregate(
            total_gross=Sum('gross_amount'),
            total_vat=Sum('vat_amount'),
            total_landed=Sum('total_landed_cost'),
            total_bill=Sum('net_total_amount'),
            total_due=Sum('due_amount')
        )

        context.update({
            'kpi_total_gross': aggregates['total_gross'] or Decimal('0.00'),
            'kpi_total_vat': aggregates['total_vat'] or Decimal('0.00'),
            'kpi_total_landed': aggregates['total_landed'] or Decimal('0.00'),
            'kpi_total_bill': aggregates['total_bill'] or Decimal('0.00'),
            'kpi_total_due': aggregates['total_due'] or Decimal('0.00'),
            'suppliers': Supplier.objects.filter(is_active=True).order_by('company_name'),
            'filters': self.request.GET,
        })
        return context

class GRNDetailView(PurchaseModuleAccessMixin, DetailView):
    """
    Authoritative Procurement Goods Received Note (GRN) Detail Controller.
    Point A: Optimized prefetching eliminates N+1 queries.
    Point B: Pre-packages exact 5-card financial metrics and dual-pot reconciliation numbers.
    Point C: Prepares structured IMEI pairs, dual-SIM states, and handset unit analytics.
    Safe Staff Preparer: Defensively handles nullable grn.received_by, resolving full name,
    username, or a polite fallback string ('Warehouse Staff') so template rendering never crashes.
    """
    model = GoodsReceivedNote
    template_name = 'purchases/grn_detail.html'
    context_object_name = 'grn'

    def get_queryset(self):
        return GoodsReceivedNote.objects.select_related(
            'supplier',
            'branch',
            'received_by',
            'purchase_order'
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        grn = self.object

        # Point A: Intelligent Database Prefetching (Eliminates N+1 queries completely)
        items = list(
            grn.items.select_related(
                'product',
                'product__brand',
                'product__category',
                'product__base_unit',
                'unit_conversion'
            ).all()
        )
        context['items'] = items

        # Safe Staff Name Preparer (Defensive against None received_by)
        if grn.received_by:
            full_name = grn.received_by.get_full_name()
            if full_name and full_name.strip():
                verified_by_name = full_name.strip()
            elif grn.received_by.username:
                verified_by_name = grn.received_by.username
            else:
                verified_by_name = "Warehouse Staff"
        else:
            verified_by_name = "Warehouse Staff"

        context['verified_by_name'] = verified_by_name

        # Point B: Supplying the 5-Card Financial Numbers Pre-Packaged
        gross_val = grn.gross_amount or Decimal('0.00')
        taxable_val = grn.taxable_amount or Decimal('0.00')
        vat_val = grn.vat_amount or Decimal('0.00')
        overhead_val = grn.overhead_total or Decimal('0.00')
        landed_val = grn.total_landed_cost or Decimal('0.00')
        net_invoice_val = grn.net_total_amount or Decimal('0.00')
        paid_val = grn.paid_amount or Decimal('0.00')
        due_val = grn.due_amount or Decimal('0.00')

        # Mathematically strict calculation of Non-Taxable / Exempt merchandise base
        net_merchandise = max(Decimal('0.00'), net_invoice_val - vat_val)
        non_taxable_val = max(Decimal('0.00'), net_merchandise - taxable_val)

        context['summary_cards'] = {
            'gross_merchandise_value': gross_val,
            'taxable_amount': taxable_val,
            'non_taxable_amount': non_taxable_val,
            'vat_amount': vat_val,
            'total_overheads': overhead_val,
            'total_landed_cost': landed_val,
            'net_total_amount': net_invoice_val,
            'paid_amount': paid_val,
            'due_amount': due_val,
        }
        context['non_taxable_base'] = non_taxable_val

        # Point C: Preparing Structured IMEI Sets and Handset Analytics
        total_handsets_count = 0
        total_imeis_tracked = 0
        has_imei_products = False
        has_dual_sim_products = False

        for item in items:
            p = item.product
            if p and (p.requires_imei_tracking or p.requires_serial_tracking):
                has_imei_products = True
                base_qty = int(item.base_unit_quantity or item.purchased_quantity or 1)
                total_handsets_count += base_qty

                pairs = item.parsed_imei_pairs
                total_imeis_tracked += len(pairs)

                if getattr(p, 'sim_configuration', 'DUAL_SIM') in ['DUAL_SIM', 'ESIM_DUAL']:
                    has_dual_sim_products = True

        context['has_imei_products'] = has_imei_products
        context['has_dual_sim_products'] = has_dual_sim_products
        context['total_handsets_count'] = total_handsets_count
        context['total_imeis_tracked'] = total_imeis_tracked
        context['is_print'] = (self.request.GET.get('print') == 'true')
        context['SYS_CONFIG'] = SystemConfiguration.get_solo()

        return context

class GRNCreateView(PurchaseModuleAccessMixin, View):
    """
    Inward Procurement Verification Controller.
    Supports dual-action workflow:
    1. 'Save Draft': Validates partial data, calculates line projections, and saves as DRAFT
       without touching physical inventory or posting to GL.
    2. 'Verify & Update Warehouse Stock': Executes complete approval, updates live inventory,
       registers physical IMEIs, creates FIFO batches, and dispatches General Ledger postings.
    """
    template_name = 'purchases/grn_form.html'

    def get(self, request, *args, **kwargs):
        branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()
        config = SystemConfiguration.get_solo()

        today = timezone.now().date()
        try:
            bs_y, bs_m, bs_d = NepaliCalendar.ad_to_bs(today)
            current_fy = NepaliCalendar.get_fiscal_year(bs_y, bs_m)
            today_bs = NepaliCalendar.format_bs(bs_y, bs_m, bs_d, lang='en')
        except Exception:
            current_fy = '2083/84'
            today_bs = ''

        initial_header = {
            'bill_date': today,
            'bill_date_bs': today_bs,
            'challan_date': today,
            'challan_date_bs': today_bs,
            'branch': branch,
            'vat_handling_mode': 'EXCLUSIVE',
            'is_vat_bill': (getattr(config, 'tax_system_mode', 'VAT') == 'VAT'),
            'vat_rate': getattr(config, 'default_vat_rate', Decimal('13.00')),
        }

        po_id = request.GET.get('po_id')
        if po_id:
            po = PurchaseOrder.objects.filter(id=po_id, status__in=['ISSUED', 'PARTIALLY_RECEIVED']).first()
            if po:
                initial_header.update({
                    'supplier': po.supplier,
                    'purchase_order': po,
                    'supplier_bill_no': f"PO-{po.po_number}",
                })

        form = GoodsReceivedNoteForm(initial=initial_header)
        formset = GRNItemFormSet()

        context = {
            'form': form,
            'formset': formset,
            'active_branch': branch,
            'branches': Branch.objects.filter(is_active=True).order_by('-is_main_branch', 'name'),
            'suppliers': Supplier.objects.filter(status='ACTIVE').order_by('company_name'),
            'active_fiscal_year': current_fy,
            'default_vat_rate': getattr(config, 'default_vat_rate', Decimal('13.00')),
            'today': today,
            'today_bs': today_bs,
            'SYS_CONFIG': config,
            'config': config,
        }
        return render(request, self.template_name, context)

    def post(self, request, *args, **kwargs):
        branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()
        config = SystemConfiguration.get_solo()

        action = (request.POST.get('action') or request.POST.get('submit_action') or '').strip().lower()
        is_draft = (action in ['save_draft', 'draft'] or 'save_draft' in request.POST)

        form = GoodsReceivedNoteForm(request.POST, is_draft=is_draft)
        formset = GRNItemFormSet(request.POST, is_draft=is_draft)

        if form.is_valid() and formset.is_valid():
            try:
                with transaction.atomic():
                    grn = form.save(commit=False)
                    selected_branch_id = request.POST.get('branch') or request.POST.get('warehouse')
                    if selected_branch_id:
                        br_obj = Branch.objects.filter(id=selected_branch_id, is_active=True).first()
                        if br_obj:
                            branch = br_obj
                    grn.branch = branch

                    if not grn.grn_number:
                        grn.grn_number = BranchDocumentSequence.get_next_sequence_number(
                            branch=branch,
                            document_type='GOODS_RECEIPT'
                        )

                    # PATH A: SAVE AS DRAFT
                    if is_draft:
                        grn.status = 'DRAFT'
                        grn.save()

                        items = formset.save(commit=False)
                        for item in items:
                            item.grn = grn
                            if item.product_id:
                                factor = item.conversion_factor if (item.conversion_factor and item.conversion_factor > Decimal('0.000')) else Decimal('1.000')
                                item.base_unit_quantity = (item.purchased_quantity * factor).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)
                                item.save()

                        for del_item in formset.deleted_objects:
                            del_item.delete()

                        PurchaseService.save_grn_draft(grn=grn, items=None, user=request.user)

                        msg = f"GRN Draft {grn.grn_number} saved successfully into local store."
                        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.content_type == 'application/json':
                            return JsonResponse({
                                'status': 'success',
                                'message': msg,
                                'grn_id': grn.id,
                                'grn_number': grn.grn_number,
                                'redirect_url': reverse('purchases:grn_detail', kwargs={'pk': grn.pk})
                            })

                        messages.success(request, msg)
                        return redirect('purchases:grn_detail', pk=grn.pk)

                    # PATH B: FINAL VERIFICATION & PHYSICAL STOCK INWARD
                    grn.status = 'DRAFT'
                    grn.save()

                    items = formset.save(commit=False)
                    valid_items_count = 0

                    for item in items:
                        if item.product and item.purchased_quantity > Decimal('0.000'):
                            item.grn = grn
                            factor = item.conversion_factor if (item.conversion_factor and item.conversion_factor > Decimal('0.000')) else Decimal('1.000')
                            item.base_unit_quantity = (item.purchased_quantity * factor).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)
                            item.save()
                            valid_items_count += 1

                    for deleted_item in formset.deleted_objects:
                        deleted_item.delete()

                    if valid_items_count == 0:
                        raise ValidationError("Please add at least one product line item with quantity > 0.")

                    # Full Approval & Stock Update Pipeline
                    PurchaseService.process_grn_approval_and_stock_in(grn=grn, user=request.user)

                    if grn.supplier:
                        grn.supplier.recalculate_balance_from_ledger(save=True)

                    if grn.purchase_order:
                        po = grn.purchase_order
                        po.status = 'COMPLETED'
                        po.save(update_fields=['status', 'updated_at'])

                success_msg = (
                    f"GRN Voucher {grn.grn_number} verified successfully! Warehouse stock updated. "
                    f"Total Bill: Rs. {grn.net_total_amount:,.2f} (Pre-VAT Base: Rs. {grn.taxable_amount:,.2f}, "
                    f"VAT: Rs. {grn.vat_amount:,.2f}, Net Due: Rs. {grn.due_amount:,.2f})."
                )

                if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.content_type == 'application/json':
                    return JsonResponse({
                        'status': 'success',
                        'message': success_msg,
                        'grn_id': grn.id,
                        'grn_number': grn.grn_number,
                        'redirect_url': reverse('purchases:grn_detail', kwargs={'pk': grn.pk})
                    })

                messages.success(request, success_msg)
                return redirect('purchases:grn_detail', pk=grn.pk)

            except ValidationError as ve:
                err_text = str(ve.message if hasattr(ve, 'message') else ve)
                if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.content_type == 'application/json':
                    return JsonResponse({'status': 'error', 'message': err_text}, status=400)
                messages.error(request, f"Validation Error: {err_text}")

            except Exception as e:
                logger.error(f"[GRN Create Error]: {e}", exc_info=True)
                err_text = f"Error processing inward consignment: {str(e)}"
                if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.content_type == 'application/json':
                    return JsonResponse({'status': 'error', 'message': err_text}, status=500)
                messages.error(request, err_text)

        else:
            messages.error(request, "Validation errors occurred. Please check all line entries, discounts, and IMEI fields.")

        today = timezone.now().date()
        try:
            bs_y, bs_m, bs_d = NepaliCalendar.ad_to_bs(today)
            current_fy = NepaliCalendar.get_fiscal_year(bs_y, bs_m)
            today_bs = NepaliCalendar.format_bs(bs_y, bs_m, bs_d, lang='en')
        except Exception:
            current_fy = '2083/84'
            today_bs = ''

        context = {
            'form': form,
            'formset': formset,
            'active_branch': branch,
            'branches': Branch.objects.filter(is_active=True).order_by('-is_main_branch', 'name'),
            'suppliers': Supplier.objects.filter(status='ACTIVE').order_by('company_name'),
            'active_fiscal_year': current_fy,
            'default_vat_rate': getattr(config, 'default_vat_rate', Decimal('13.00')),
            'today': today,
            'today_bs': today_bs,
            'SYS_CONFIG': config,
            'config': config,
        }
        return render(request, self.template_name, context)

# ==============================================================================
# DEDICATED PHYSICAL IMEI SCANNER CONTROLLER (AUTO-ENABLES SERIALIZATION ON DEMAND)
# ==============================================================================
class GRNManageIMEIsView(PurchaseModuleAccessMixin, View):
    """
    Dedicated view allowing warehouse staff and managers to scan/type physical
    15-digit IMEI numbers for mobile phones received on a completed GRN.
    
    Guarantees:
    - Displays all items on the bill. If a product (such as 'test') was created without
      the 'Track 15-Digit IMEI' box checked, adding IMEIs to it automatically enables
      requires_imei_tracking = True for that product.
    - Zero Stock Inflation: Does NOT touch BranchStock quantities (units already in stock).
    - Zero Accounting Impact: Does NOT alter purchase costs, VAT, or supplier debt.
    - Preserves POS-Sold Phones: Detects and protects any handsets already sold under Backlog Mode.
    - Reuses existing validation and ItemInstance creation logic.
    """
    template_name = 'purchases/grn_manage_imeis.html'

    def get(self, request, pk, *args, **kwargs):
        grn = get_object_or_404(
            GoodsReceivedNote.objects.select_related('supplier', 'branch'),
            pk=pk
        )
        if grn.status == 'CANCELLED':
            messages.error(request, f"Cannot manage IMEIs: Goods Received Note {grn.grn_number} is cancelled.")
            return redirect('purchases:grn_detail', pk=grn.pk)

        # Include all line items on this bill so any product can have IMEIs added
        grn_items = grn.items.select_related('product', 'product__base_unit').all()

        if not grn_items.exists():
            messages.info(
                request,
                f"Goods Received Note {grn.grn_number} has no line items."
            )
            return redirect('purchases:grn_detail', pk=grn.pk)

        phone_items = []
        initial_data = {}
        total_handsets = 0
        total_linked = 0

        for grn_item in grn_items:
            product = grn_item.product
            base_qty = int(grn_item.base_unit_quantity or grn_item.purchased_quantity or 1)
            total_handsets += base_qty

            # Find existing ItemInstance records linked to this GRN and product
            existing_instances = list(
                ItemInstance.objects.filter(
                    purchase_reference=grn.grn_number,
                    product=product,
                    branch=grn.branch
                ).order_by('id')
            )

            imeis_list = []
            seen_imei1 = set()

            for inst in existing_instances:
                im1 = inst.imei_1 or ''
                im2 = inst.imei_2 or ''
                if im1:
                    seen_imei1.add(im1)
                imeis_list.append({
                    'imei_1': im1,
                    'imei_2': im2,
                    'is_already_in_db': True,
                    'status': inst.status,
                    'is_sold': (inst.status == 'SOLD'),
                })

            # Check if any IMEIs in scanned_imei_list were textually saved but not yet in ItemInstance
            if grn_item.scanned_imei_list:
                tokens = [t.strip() for t in re.split(r'[\n,;]+', grn_item.scanned_imei_list) if t.strip()]
                for token in tokens:
                    parts = token.split('|')
                    im1 = parts[0].strip() if len(parts) > 0 else ''
                    im2 = parts[1].strip() if len(parts) > 1 else ''
                    if im1 and im1 not in seen_imei1:
                        seen_imei1.add(im1)
                        imeis_list.append({
                            'imei_1': im1,
                            'imei_2': im2,
                            'is_already_in_db': False,
                            'status': 'IN_STOCK',
                            'is_sold': False,
                        })

            existing_count = len(imeis_list)
            total_linked += existing_count
            remaining_count = max(0, base_qty - existing_count)

            is_dual_sim = getattr(product, 'sim_configuration', 'DUAL_SIM') in ['DUAL_SIM', 'ESIM_DUAL']

            item_data = {
                'item': grn_item,
                'product': product,
                'base_qty': base_qty,
                'existing_instances': existing_instances,
                'existing_imeis_count': existing_count,
                'remaining_count': remaining_count,
                'is_dual_sim': is_dual_sim,
                'existing_imei_list_text': grn_item.scanned_imei_list or '',
            }
            phone_items.append(item_data)

            initial_data[str(grn_item.id)] = {
                'item_id': grn_item.id,
                'product_id': product.id,
                'product_name': product.name,
                'sku': product.sku,
                'base_qty': base_qty,
                'is_dual_sim': is_dual_sim,
                'batch': grn_item.batch_number or '',
                'imeis': imeis_list,
            }

        total_remaining = max(0, total_handsets - total_linked)

        context = {
            'grn': grn,
            'phone_items': phone_items,
            'initial_data_json': initial_data,
            'total_handsets': total_handsets,
            'total_linked': total_linked,
            'total_remaining': total_remaining,
            'SYS_CONFIG': SystemConfiguration.get_solo(),
            'config': SystemConfiguration.get_solo(),
        }
        return render(request, self.template_name, context)

    def post(self, request, pk, *args, **kwargs):
        grn = get_object_or_404(
            GoodsReceivedNote.objects.select_related('supplier', 'branch'),
            pk=pk
        )
        if grn.status == 'CANCELLED':
            messages.error(request, f"Cannot update IMEIs: Goods Received Note {grn.grn_number} is cancelled.")
            return redirect('purchases:grn_detail', pk=grn.pk)

        payload_json = request.POST.get('imei_payload_json', '').strip()
        if not payload_json:
            if request.content_type == 'application/json':
                try:
                    body_data = json.loads(request.body.decode('utf-8'))
                    payload_json = json.dumps(body_data)
                except Exception:
                    payload_json = ''

        if not payload_json:
            messages.error(request, "No IMEI scan data was received.")
            return redirect('purchases:grn_manage_imeis', pk=grn.pk)

        try:
            items_payload = json.loads(payload_json)
        except Exception as e:
            messages.error(request, f"Invalid IMEI payload format: {e}")
            return redirect('purchases:grn_manage_imeis', pk=grn.pk)

        try:
            with transaction.atomic():
                # Auto-Enable: If user added IMEIs to a product that was requires_imei_tracking=False,
                # automatically turn ON requires_imei_tracking = True!
                for item_id_str, item_info in items_payload.items():
                    raw_imeis = item_info.get('imeis', [])
                    if raw_imeis:
                        try:
                            item_id = int(item_id_str)
                            target_item = grn.items.select_related('product').filter(id=item_id).first()
                            if target_item and target_item.product and not target_item.product.requires_imei_tracking:
                                target_item.product.requires_imei_tracking = True
                                target_item.product.save(update_fields=['requires_imei_tracking', 'updated_at'])
                        except (ValueError, TypeError):
                            pass

                total_attached = PurchaseService.attach_imeis_to_received_grn(
                    grn=grn,
                    items_payload=items_payload,
                    user=request.user
                )

            messages.success(
                request,
                f"Successfully linked {total_attached} physical IMEI number(s) to warehouse stock for GRN {grn.grn_number}."
            )
            return redirect('purchases:grn_detail', pk=grn.pk)

        except ValidationError as ve:
            err_msg = str(ve.message if hasattr(ve, 'message') else ve)
            messages.error(request, f"Validation Error: {err_msg}")
            return redirect('purchases:grn_manage_imeis', pk=grn.pk)
        except Exception as err:
            logger.error(f"[GRN Manage IMEIs Error] GRN {grn.grn_number}: {err}", exc_info=True)
            messages.error(request, f"Error saving IMEIs: {err}")
            return redirect('purchases:grn_manage_imeis', pk=grn.pk)

# ==============================================================================
# DEDICATED PURCHASE BILL (GRN) CANCELLATION CONTROLLER (AUDIT COMPLIANT)
# ==============================================================================
@login_required
@require_http_methods(["POST"])
def cancel_grn_view(request, pk):
    """
    Dedicated view function allowing store managers and authorized users to atomically
    cancel / void an erroneous purchase bill (GRN):
    1. Checks role authorization (Superuser, Owner, Manager).
    2. Mandates a formal cancellation justification reason.
    3. Verifies that received handsets (IMEIs) have not already been sold to retail customers.
    4. Deducts inward merchandise quantities back out of BranchStock via InventoryService.
    5. Depletes / archives active FIFO ProductBatch records associated with this GRN.
    6. Archives unsold ItemInstance handset records so they cannot be sold.
    7. Atomically reconciles SupplierUdhaariLedger and updates supplier balance:
       - If unpaid credit purchase: reverses exact due debt added to supplier AP.
       - If paid on delivery: records spot cash disbursed as an advance due from supplier / pending refund.
    8. Adopts Method 1 (Audit Standard) for General Ledger reversal:
       - Leaves original purchase journal voucher with status POSTED (annotated with reversal ref).
       - Posts a distinct reversing double-entry journal entry with status POSTED that inverts debits/credits.
    9. Sets GRN status to 'CANCELLED'.
    10. Records an immutable forensic event in AuditLog.
    """
    grn = get_object_or_404(GoodsReceivedNote, pk=pk)

    is_authorized = (
        request.user.is_superuser or
        getattr(request.user, 'role', '') in ['OWNER', 'MANAGER']
    )
    if not is_authorized:
        err_msg = "Permission Denied: Only store owners and managers can cancel inward purchase bills."
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.content_type == 'application/json':
            return JsonResponse({'status': 'error', 'message': err_msg}, status=403)
        messages.error(request, err_msg)
        return redirect('purchases:grn_detail', pk=grn.pk)

    reason = ""
    if request.content_type == 'application/json':
        try:
            body_data = json.loads(request.body.decode('utf-8'))
            reason = body_data.get('reason') or body_data.get('cancellation_reason', '')
        except Exception:
            reason = ""
    else:
        reason = request.POST.get('cancellation_reason') or request.POST.get('reason', '')

    reason = str(reason or '').strip()

    if not reason or len(reason) < 3:
        err_msg = "A valid, mandatory cancellation reason is required to void this purchase bill (minimum 3 characters)."
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.content_type == 'application/json':
            return JsonResponse({'status': 'error', 'message': err_msg}, status=400)
        messages.error(request, err_msg)
        return redirect('purchases:grn_detail', pk=grn.pk)

    if grn.status == 'CANCELLED':
        err_msg = f"Purchase GRN {grn.grn_number} is already cancelled."
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.content_type == 'application/json':
            return JsonResponse({'status': 'error', 'message': err_msg}, status=400)
        messages.warning(request, err_msg)
        return redirect('purchases:grn_detail', pk=grn.pk)

    try:
        with transaction.atomic():
            grn = GoodsReceivedNote.objects.select_for_update().get(pk=pk)

            # 1. Verify that no serialized handsets received on this GRN have already been sold
            instances = ItemInstance.objects.select_for_update().filter(
                purchase_reference=grn.grn_number
            )
            sold_handsets = instances.filter(status='SOLD')
            if sold_handsets.exists():
                sold_imeis = list(sold_handsets.values_list('imei_1', flat=True)[:5])
                raise ValidationError(
                    f"Cannot cancel GRN {grn.grn_number}: {sold_handsets.count()} handset(s) from this inward bill "
                    f"have already been sold to customers (IMEI: {', '.join(sold_imeis)}). "
                    f"Please process a customer sales return first before voiding the inward consignment."
                )

            # 2. Deduct physical branch inventory and deplete batches for all line items
            for item in grn.items.select_related('product', 'product__base_unit').all():
                base_qty = item.base_unit_quantity if item.base_unit_quantity > Decimal('0.000') else item.purchased_quantity

                InventoryService.adjust_stock(
                    product=item.product,
                    branch=grn.branch,
                    quantity_delta=-base_qty,
                    movement_type='ADJUSTMENT_SUB',
                    reference_doc=f"VOID-{grn.grn_number}",
                    remarks=f"Purchase Voided / Cancelled: {grn.grn_number} (Bill: {grn.supplier_bill_no}). Reason: {reason}",
                    user=request.user,
                    allow_negative=True
                )

                ProductBatch.objects.filter(
                    grn_reference=grn.grn_number,
                    product=item.product,
                    branch=grn.branch
                ).update(
                    quantity_remaining=Decimal('0.000'),
                    is_depleted=True,
                    updated_at=timezone.now()
                )

            # 3. Archive unsold handset instances
            instances.filter(status='IN_STOCK').update(
                status='ARCHIVED',
                mdms_remarks=f"Consignment voided with GRN {grn.grn_number}: {reason}",
                updated_at=timezone.now()
            )

            # 4. Settle Supplier Balance & Udhaari Ledger with Full Mathematical Continuity
            supplier = Supplier.objects.select_for_update().get(pk=grn.supplier_id)
            prev_bal = supplier.current_balance or Decimal('0.00')

            due = grn.due_amount or Decimal('0.00')
            paid = grn.paid_amount or Decimal('0.00')

            if due > Decimal('0.00') and paid == Decimal('0.00'):
                # Case A: Entirely unpaid credit bill
                new_bal = prev_bal - due
                supplier.current_balance = new_bal
                supplier.save(update_fields=['current_balance', 'updated_at'])

                SupplierUdhaariLedger.objects.create(
                    supplier=supplier,
                    branch=grn.branch,
                    transaction_type='PURCHASE_RETURN',
                    amount=due,
                    previous_balance=prev_bal,
                    resulting_balance=new_bal,
                    payment_mode='OTHER',
                    reference_number=f"VOID-{grn.grn_number}",
                    recorded_by=request.user,
                    remarks=f"Cancellation of unpaid bill {grn.grn_number} (Ref: {grn.supplier_bill_no}). Unpaid debt reversed: Rs. {due:,.2f}. Reason: {reason}"
                )

            elif due > Decimal('0.00') and paid > Decimal('0.00'):
                # Case B: Partially paid bill
                total_reversal = due + paid
                new_bal = prev_bal - total_reversal
                supplier.current_balance = new_bal
                supplier.save(update_fields=['current_balance', 'updated_at'])

                SupplierUdhaariLedger.objects.create(
                    supplier=supplier,
                    branch=grn.branch,
                    transaction_type='PURCHASE_RETURN',
                    amount=total_reversal,
                    previous_balance=prev_bal,
                    resulting_balance=new_bal,
                    payment_mode='OTHER',
                    reference_number=f"VOID-{grn.grn_number}",
                    recorded_by=request.user,
                    remarks=(
                        f"Cancellation of partially paid bill {grn.grn_number} (Ref: {grn.supplier_bill_no}). "
                        f"Unpaid debt reversed: Rs. {due:,.2f}. Cash paid on delivery (Rs. {paid:,.2f}) recorded as advance due from supplier. Reason: {reason}"
                    )
                )

            else:
                # Case C: Fully paid on delivery (due == 0.00)
                new_bal = prev_bal - paid
                supplier.current_balance = new_bal
                supplier.save(update_fields=['current_balance', 'updated_at'])

                SupplierUdhaariLedger.objects.create(
                    supplier=supplier,
                    branch=grn.branch,
                    transaction_type='PURCHASE_RETURN',
                    amount=paid,
                    previous_balance=prev_bal,
                    resulting_balance=new_bal,
                    payment_mode='OTHER',
                    reference_number=f"VOID-{grn.grn_number}",
                    recorded_by=request.user,
                    remarks=(
                        f"Cancellation of fully paid bill {grn.grn_number} (Ref: {grn.supplier_bill_no}). "
                        f"No credit debt existed. Cash paid on delivery (Rs. {paid:,.2f}) recorded as advance due from supplier / pending refund. Reason: {reason}"
                    )
                )

            # 5. Method 1 (Audit Standard) General Ledger Reversal
            try:
                from apps.accounting.models import JournalEntry
                from apps.accounting.services.auto_posting import JournalEngine

                orig_entry = JournalEntry.objects.filter(
                    voucher_type='PURCHASE',
                    reference_document=grn.grn_number,
                    status='POSTED'
                ).first()

                if orig_entry:
                    cross_ref_tag = f"[REVERSED by REV-{grn.grn_number} on {timezone.now().strftime('%Y-%m-%d')}]"
                    if cross_ref_tag not in (orig_entry.narration or ""):
                        orig_entry.narration = f"{orig_entry.narration or ''} {cross_ref_tag}".strip()
                        orig_entry.save(update_fields=['narration', 'updated_at'])

                    reversing_lines = []
                    for itm in orig_entry.items.select_related('account'):
                        reversing_lines.append({
                            'account': itm.account,
                            'debit': itm.credit_amount,
                            'credit': itm.debit_amount,
                            'supplier': itm.supplier,
                            'narration': f"Cancellation reversal of {orig_entry.voucher_number} for {grn.grn_number}"
                        })

                    if reversing_lines:
                        JournalEngine.create_balanced_entry(
                            voucher_type='JOURNAL',
                            date_ad=timezone.now().date(),
                            branch=grn.branch,
                            lines=reversing_lines,
                            narration=f"Full Reversal of Cancelled Purchase GRN {grn.grn_number}. Reason: {reason}",
                            reference_doc=f"REV-{grn.grn_number}",
                            source_module='PURCHASE',
                            source_id=f"REV-{grn.grn_number}",
                            user=request.user,
                            auto_post=True
                        )
            except Exception as gl_err:
                logger.warning(f"[GRN Cancellation GL Reversal]: {gl_err}")

            # 6. Mark GRN Status as CANCELLED
            grn.status = 'CANCELLED'
            if hasattr(grn, 'cancellation_reason'):
                grn.cancellation_reason = reason
                grn.save(update_fields=['status', 'cancellation_reason', 'updated_at'])
            elif hasattr(grn, 'remarks'):
                grn.remarks = f"CANCELLED: {reason}"
                grn.save(update_fields=['status', 'remarks', 'updated_at'])
            else:
                grn.save(update_fields=['status', 'updated_at'])

            # 7. Forensic AuditLog Record
            AuditLog.objects.create(
                user=request.user,
                branch=grn.branch,
                action_type='BILL_CANCEL',
                module='Purchases',
                object_repr=grn.grn_number,
                ip_address=request.META.get('REMOTE_ADDR'),
                details={
                    'supplier': supplier.company_name,
                    'supplier_bill_no': grn.supplier_bill_no,
                    'reason': reason,
                    'net_amount': str(grn.net_total_amount),
                    'due_amount_reversed': str(due),
                    'paid_amount_reversed': str(paid),
                    'new_supplier_balance': str(new_bal),
                    'items_count': grn.items.count()
                }
            )

        success_msg = f"Purchase Bill {grn.grn_number} (Supplier Ref: {grn.supplier_bill_no}) was successfully voided and stock was reversed."
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.content_type == 'application/json':
            return JsonResponse({'status': 'success', 'message': success_msg, 'grn_number': grn.grn_number})

        messages.success(request, success_msg)
        return redirect('purchases:grn_detail', pk=grn.pk)

    except ValidationError as ve:
        err_msg = str(ve.message if hasattr(ve, 'message') else ve)
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.content_type == 'application/json':
            return JsonResponse({'status': 'error', 'message': err_msg}, status=400)
        messages.error(request, err_msg)
        return redirect('purchases:grn_detail', pk=grn.pk)
    except Exception as err:
        err_msg = f"Error voiding purchase bill {grn.grn_number}: {str(err)}"
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.content_type == 'application/json':
            return JsonResponse({'status': 'error', 'message': err_msg}, status=500)
        messages.error(request, err_msg)
        return redirect('purchases:grn_detail', pk=grn.pk)

class GRNCancelView(PurchaseModuleAccessMixin, View):
    """Class-based wrapper around cancel_grn_view for class routing compatibility."""
    def post(self, request, pk, *args, **kwargs):
        return cancel_grn_view(request, pk)

# ==============================================================================
# COMMERCIAL PURCHASE RETURN & DEBIT NOTE VIEWS
# ==============================================================================
class PurchaseReturnListView(PurchaseModuleAccessMixin, ListView):
    """
    The Commercial Purchase Return Report screen.
    Displays all debit notes issued to suppliers with multi-parameter filtering
    (Supplier, Status, Refund Mode, Date Range) and summary metrics.
    Includes CSV export handling via ?export=csv.
    """
    model = PurchaseReturn
    template_name = 'purchases/purchase_return_list.html'
    context_object_name = 'returns'
    paginate_by = 25

    def get_queryset(self):
        qs = PurchaseReturn.objects.select_related('supplier', 'branch', 'processed_by', 'original_grn').prefetch_related('items__product')
        active_branch = getattr(self.request, 'active_branch', None)
        if active_branch and not self.request.user.is_superuser:
            qs = qs.filter(branch=active_branch)

        query = self.request.GET.get('q', '').strip()
        supplier_id = self.request.GET.get('supplier', '').strip()
        status_filter = self.request.GET.get('status', '').strip()
        refund_mode = self.request.GET.get('refund_mode', '').strip()
        start_date = self.request.GET.get('start_date', '').strip()
        end_date = self.request.GET.get('end_date', '').strip()

        if query:
            tokens = [t.strip() for t in query.split() if t.strip()]
            for token in tokens:
                qs = qs.filter(
                    Q(return_number__icontains=token) |
                    Q(original_bill_reference__icontains=token) |
                    Q(supplier__company_name__icontains=token) |
                    Q(remarks__icontains=token) |
                    Q(items__returned_imei_list__icontains=token)
                )

        if supplier_id:
            qs = qs.filter(supplier_id=supplier_id)

        if status_filter:
            qs = qs.filter(status=status_filter)

        if refund_mode:
            qs = qs.filter(refund_mode=refund_mode)

        if start_date:
            qs = qs.filter(return_date__gte=start_date)

        if end_date:
            qs = qs.filter(return_date__lte=end_date)

        return qs.distinct().order_by('-return_date', '-created_at')

    def render_to_response(self, context, **response_kwargs):
        if self.request.GET.get('export') == 'csv':
            return self.export_csv(self.get_queryset())
        return super().render_to_response(context, **response_kwargs)

    def export_csv(self, queryset):
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = 'attachment; filename="Purchase_Returns_Debit_Notes.csv"'
        writer = csv.writer(response)
        writer.writerow([
            'Debit Note No', 'Return Date (AD)', 'Return Date (BS)', 'Supplier Code',
            'Supplier Name', 'Original Bill / GRN Ref', 'Items Count', 'Refund Mode',
            'Gross Total (NPR)', 'Tax (NPR)', 'Net Refund Amount (NPR)', 'Status',
            'Processed By', 'Remarks'
        ])
        for r in queryset:
            writer.writerow(sanitize_csv_row([
                r.return_number,
                r.return_date,
                r.return_date_bs or '',
                r.supplier.code,
                r.supplier.company_name,
                r.original_bill_reference or (r.original_grn.grn_number if r.original_grn else ''),
                r.items.count(),
                r.get_refund_mode_display(),
                f"{r.total_return_amount:.2f}",
                f"{r.tax_amount:.2f}",
                f"{r.net_refund_amount:.2f}",
                r.get_status_display(),
                r.processed_by.username if r.processed_by else 'System',
                r.remarks or ''
            ]))
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        base_qs = self.get_queryset()

        total_returns_count = base_qs.count()
        total_refund_amount = base_qs.aggregate(s=Sum('net_refund_amount'))['s'] or Decimal('0.00')
        total_deducted_balance = base_qs.filter(refund_mode='DEDUCT_FROM_BALANCE').aggregate(s=Sum('net_refund_amount'))['s'] or Decimal('0.00')
        total_cash_refunded = base_qs.filter(refund_mode='CASH_REFUND').aggregate(s=Sum('net_refund_amount'))['s'] or Decimal('0.00')

        context.update({
            'total_returns_count': total_returns_count,
            'total_refund_amount': total_refund_amount,
            'total_deducted_balance': total_deducted_balance,
            'total_cash_refunded': total_cash_refunded,
            'suppliers': Supplier.objects.filter(status='ACTIVE').order_by('company_name'),
            'filters': self.request.GET,
        })
        return context

class PurchaseReturnCreateView(PurchaseModuleAccessMixin, View):
    """
    Commercial Purchase Return entry form:
    Allows staff to select supplier, choose return date, pick products,
    input quantities, rates, and scanned IMEIs to issue a formal Debit Note voucher.
    """
    template_name = 'purchases/purchase_return_form.html'

    def get(self, request, *args, **kwargs):
        branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()
        form = PurchaseReturnForm(branch=branch)
        formset = PurchaseReturnItemFormSet()

        grn_id = request.GET.get('grn_id')
        supplier_id = request.GET.get('supplier_id')

        if grn_id:
            grn = GoodsReceivedNote.objects.filter(id=grn_id, branch=branch, status='RECEIVED').first()
            if grn:
                form = PurchaseReturnForm(branch=branch, initial={
                    'supplier': grn.supplier,
                    'original_grn': grn,
                    'original_bill_reference': grn.supplier_bill_no,
                    'refund_mode': 'DEDUCT_FROM_BALANCE',
                })
        elif supplier_id:
            sup = Supplier.objects.filter(id=supplier_id, status='ACTIVE').first()
            if sup:
                form = PurchaseReturnForm(branch=branch, initial={'supplier': sup})

        products = Product.objects.filter(is_active=True).select_related('base_unit').order_by('name')

        return render(request, self.template_name, {
            'form': form,
            'formset': formset,
            'branch': branch,
            'products': products
        })

    def post(self, request, *args, **kwargs):
        branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()
        form = PurchaseReturnForm(request.POST, branch=branch)
        formset = PurchaseReturnItemFormSet(request.POST)

        if form.is_valid() and formset.is_valid():
            try:
                with transaction.atomic():
                    purchase_return = form.save(commit=False)
                    purchase_return.branch = branch
                    purchase_return.return_number = PurchaseReturnService.generate_return_number(branch)
                    purchase_return.processed_by = request.user
                    purchase_return.status = 'DRAFT'
                    purchase_return.save()

                    items = formset.save(commit=False)
                    valid_items_count = 0
                    running_gross = Decimal('0.00')
                    running_tax = Decimal('0.00')

                    for item in items:
                        if item.product and item.returned_quantity > Decimal('0.000'):
                            item.purchase_return = purchase_return
                            factor = item.conversion_factor if (item.conversion_factor and item.conversion_factor > Decimal('0.000')) else Decimal('1.000')
                            item.base_unit_quantity = (item.returned_quantity * factor).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)

                            rate = item.purchase_rate or Decimal('0.00')
                            gross = (item.returned_quantity * rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                            tax_rate = item.tax_rate or Decimal('0.00')
                            tax = (gross * (tax_rate / Decimal('100.00'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) if tax_rate > Decimal('0.00') else Decimal('0.00')

                            item.tax_amount = tax
                            item.line_total = gross + tax
                            item.save()

                            running_gross += gross
                            running_tax += tax
                            valid_items_count += 1

                    for del_item in formset.deleted_objects:
                        del_item.delete()

                    if valid_items_count == 0:
                        raise ValidationError("Please add at least one product line item to the return voucher.")

                    purchase_return.total_return_amount = running_gross
                    purchase_return.tax_amount = running_tax
                    purchase_return.net_refund_amount = running_gross + running_tax
                    purchase_return.save(update_fields=['total_return_amount', 'tax_amount', 'net_refund_amount'])

                    # Process Stock Deduction, IMEI Locking & Supplier Udhaari Adjustment
                    PurchaseReturnService.process_purchase_return(purchase_return, user=request.user)

                    # Recalculate supplier balance strictly from sub-ledger entries
                    purchase_return.supplier.recalculate_balance_from_ledger(save=True)

                messages.success(
                    request,
                    f"Purchase Return / Debit Note '{purchase_return.return_number}' processed successfully! "
                    f"Total refund value: Rs. {purchase_return.net_refund_amount:,.2f} ({purchase_return.get_refund_mode_display()})."
                )
                return redirect('purchases:purchase_return_detail', pk=purchase_return.pk)

            except ValidationError as ve:
                messages.error(request, str(ve.message if hasattr(ve, 'message') else ve))
            except Exception as e:
                messages.error(request, f"Error processing purchase return: {str(e)}")
        else:
            messages.error(request, "Validation errors occurred. Please verify product selections, quantities, and scanned IMEIs.")

        products = Product.objects.filter(is_active=True).select_related('base_unit').order_by('name')
        return render(request, self.template_name, {
            'form': form,
            'formset': formset,
            'branch': branch,
            'products': products
        })

class PurchaseReturnDetailView(PurchaseModuleAccessMixin, DetailView):
    """
    Renders detailed voucher overview and printable A4 Debit Note slip
    for a completed Purchase Return.
    Provides complete GL and VAT audit trail when accessed from VAT reporting:
    Debit Note ➔ GL Journal ➔ Journal Items ➔ Input VAT 1410 Reversal,
    with Original GRN / Bill Reference as reference.
    """
    model = PurchaseReturn
    template_name = 'purchases/purchase_return_detail.html'
    context_object_name = 'purchase_return'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        pret = self.object
        items = list(pret.items.select_related('product', 'product__base_unit', 'unit_conversion', 'item_instance').all())
        context['items'] = items
        config = SystemConfiguration.get_solo()
        context['config'] = config
        context['SYS_CONFIG'] = config

        # -------------------------------------------------------------
        # RESOLVE GL JOURNAL & VAT 1410 DRILLDOWN DATA
        # -------------------------------------------------------------
        from apps.accounting.models import JournalEntry
        from apps.taxation.services.vat_ledger_service import VATLedgerService

        # Fetch authoritative VAT drill-down resolution
        vat_drilldown = None
        try:
            vat_drilldown = VATLedgerService.get_purchase_return_vat_drilldown(pret.id)
        except Exception as e:
            logger.warning(f"[PurchaseReturnDetailView] VAT drilldown resolution error for PR #{pret.id}: {e}")

        # Direct GL Journal Entry Lookup (matching AutoPostingService.post_purchase_return)
        je_obj = None
        # Link 1: source_module='PURCHASE_RETURN' and source_id=str(pret.id)
        je_obj = JournalEntry.objects.filter(
            source_module='PURCHASE_RETURN',
            source_id=str(pret.id),
            status='POSTED'
        ).prefetch_related('items__account', 'items__supplier').first()

        # Link 2: reference_document matching return_number with voucher_type='DEBIT_NOTE'
        if not je_obj:
            je_obj = JournalEntry.objects.filter(
                reference_document__iexact=pret.return_number,
                voucher_type='DEBIT_NOTE',
                status='POSTED'
            ).prefetch_related('items__account', 'items__supplier').first()

        # Link 3: reference_document matching return_number (any voucher type)
        if not je_obj:
            je_obj = JournalEntry.objects.filter(
                reference_document__iexact=pret.return_number,
                status='POSTED'
            ).prefetch_related('items__account', 'items__supplier').first()

        # Link 4: voucher_number matching return_number
        if not je_obj:
            je_obj = JournalEntry.objects.filter(
                voucher_number__iexact=pret.return_number,
                status='POSTED'
            ).prefetch_related('items__account', 'items__supplier').first()

        # Extract Journal Items and locate Account 1410 (Input VAT Reversal)
        journal_items_list = []
        gl_account_1410_detail = None
        if je_obj:
            for itm in je_obj.items.select_related('account', 'supplier').all():
                is_1410 = bool(
                    itm.account.code == '1410' or
                    itm.account.code.startswith('1410-') or
                    itm.account.system_tag == 'INPUT_VAT'
                )
                item_dict = {
                    'id': itm.id,
                    'account_code': itm.account.code,
                    'account_name': itm.account.name,
                    'debit_amount': itm.debit_amount,
                    'credit_amount': itm.credit_amount,
                    'line_narration': itm.line_narration or '',
                    'is_input_vat_1410': is_1410,
                }
                journal_items_list.append(item_dict)

                if is_1410 and not gl_account_1410_detail:
                    gl_account_1410_detail = item_dict

        # If vat_drilldown was resolved, prefer its structured details
        if vat_drilldown and vat_drilldown.get('status') == 'SUCCESS':
            context['vat_drilldown'] = vat_drilldown
            context['gl_entry'] = vat_drilldown.get('gl_entry')
            context['gl_journal_entry'] = vat_drilldown.get('gl_journal_entry') or je_obj
            context['journal_items'] = vat_drilldown.get('journal_items') or journal_items_list
            context['gl_account_1410'] = vat_drilldown.get('gl_account_1410') or gl_account_1410_detail
            context['input_vat_1410'] = vat_drilldown.get('input_vat_1410') or gl_account_1410_detail
            context['debit_note'] = vat_drilldown.get('debit_note')
            context['audit_chain'] = vat_drilldown.get('audit_chain')
        else:
            context['vat_drilldown'] = None
            context['gl_journal_entry'] = je_obj
            context['gl_entry'] = {
                'id': je_obj.id,
                'voucher_number': je_obj.voucher_number,
                'voucher_type': je_obj.voucher_type,
                'voucher_type_display': je_obj.get_voucher_type_display(),
                'entry_date': je_obj.entry_date,
                'entry_date_bs': je_obj.entry_date_bs or '',
                'fiscal_year': je_obj.fiscal_year or '',
                'total_debit': je_obj.total_debit,
                'total_credit': je_obj.total_credit,
                'narration': je_obj.narration or '',
            } if je_obj else None
            context['journal_items'] = journal_items_list
            context['gl_account_1410'] = gl_account_1410_detail
            context['input_vat_1410'] = gl_account_1410_detail
            context['debit_note'] = {
                'voucher_number': je_obj.voucher_number,
                'id': je_obj.id,
                'total_amount': je_obj.total_debit,
            } if je_obj else None
            context['audit_chain'] = {
                'purchase_return_number': pret.return_number,
                'debit_note_number': je_obj.voucher_number if je_obj else pret.return_number,
                'gl_journal_voucher': je_obj.voucher_number if je_obj else 'N/A',
                'gl_journal_id': je_obj.id if je_obj else None,
                'journal_items_count': len(journal_items_list),
                'input_vat_account': gl_account_1410_detail['account_code'] if gl_account_1410_detail else '1410',
                'input_vat_reversal_amount': gl_account_1410_detail['credit_amount'] if gl_account_1410_detail else pret.tax_amount,
                'original_grn_number': pret.original_grn.grn_number if pret.original_grn else (pret.original_bill_reference or 'N/A'),
                'is_gl_linked': bool(je_obj is not None),
                'is_vat_1410_linked': bool(gl_account_1410_detail is not None),
            }

        # Tax Summary payload matching SalesReturnDetailView structure
        context['tax_summary'] = {
            'total_return_amount': pret.total_return_amount or Decimal('0.00'),
            'taxable_amount': pret.total_return_amount or Decimal('0.00'),
            'vat_amount': pret.tax_amount or Decimal('0.00'),
            'net_refund_amount': pret.net_refund_amount or Decimal('0.00'),
            'has_vat': (pret.tax_amount or Decimal('0.00')) > Decimal('0.00'),
            'original_bill_reference': pret.original_bill_reference or (pret.original_grn.supplier_bill_no if pret.original_grn else ''),
            'original_grn': pret.original_grn,
        }

        # Check if opened specifically from VAT report drilldown
        source = (self.request.GET.get('source') or self.request.GET.get('from') or '').lower()
        context['is_from_vat_drilldown'] = bool(source in ['vat', 'taxation', 'report', 'audit'] or 'drilldown' in self.request.GET)

        return context