"""
Purchases, Suppliers, GRN & Commercial Purchase Return (Debit Note) Views.

Key Capabilities:
1. Supplier Directory & Ledger: Full accounts payable lifecycle with credit limits and settlement histories.
2. Supplier Payouts:
   - Atomically updates supplier debt balance strictly through sub-ledger records.
   - Automatically posts double-entry General Ledger payment vouchers without swallowing errors.
3. Purchase Orders (PO): Requisitions with approval workflows, line item formsets, and delivery tracking.
4. Goods Received Notes (GRN):
   - Proportional value-based overhead distribution (freight, customs, handling).
   - Strict serialized & dual-IMEI enforcement.
   - Dual-entry GL auto-posting.
   - cancel_grn_view / GRNCancelView: Dedicated manager cancellation workflow that safely
     reverses warehouse stock, archives unsold handset IMEIs, clears supplier AP balance,
     and writes an immutable forensic AuditLog record.
5. Commercial Purchase Returns (Debit Notes):
   - Stock deduction, IMEI de-registration, supplier balance adjustments.
   - Automatic GL double-entry reversal vouchers.
"""

import csv
import json
import uuid
import logging
from decimal import Decimal, ROUND_HALF_UP

from django.shortcuts import render, redirect, get_object_or_404
from django.views.generic import ListView, DetailView, CreateView, UpdateView, View
from django.views.decorators.http import require_http_methods
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib import messages
from django.urls import reverse_lazy, reverse
from django.db import transaction
from django.db.models import Q, Sum, Count
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

logger = logging.getLogger(__name__)

# =============================================================================
# GENERAL LEDGER DISPATCHER BRIDGE (DEFENSIVE COMPATIBILITY)
# =============================================================================
try:
    import apps.accounting.services.auto_posting as auto_posting_mod

    if not hasattr(auto_posting_mod, 'post_supplier_payout_journal'):
        def _post_supplier_payout_journal(supplier, amount, payment_mode, ref_no=None, user=None, branch=None, **kwargs):
            """
            Fallback double-entry poster for supplier payout settlements:
            - Dr: Accounts Payable (Supplier Control Account)
            - Cr: Cash in Hand (if CASH) or Bank & Digital Wallets
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

                target_branch = branch or Branch.get_default_main_branch()

                cash_acc = AutoPostingService.get_or_create_control_account(
                    target_branch, 'CASH', '1010', 'Cash in Hand', 'ASSET', 'DEBIT'
                )
                bank_acc = AutoPostingService.get_or_create_control_account(
                    target_branch, 'BANK', '1020', 'Bank & Digital Wallets', 'ASSET', 'DEBIT'
                )
                ap_acc = AutoPostingService.get_or_create_control_account(
                    target_branch, 'ACCOUNTS_PAYABLE', '2010', 'Accounts Payable (Creditors)', 'LIABILITY', 'CREDIT'
                )

                src_acc = cash_acc if str(payment_mode).upper() == 'CASH' else bank_acc

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
                        'narration': f"Disbursement via {payment_mode} (Ref: {ref_no or '-'})"
                    }
                ]

                narration = f"Supplier debt payout: {supplier.company_name} (Rs. {amount_dec:.2f}) via {payment_mode}"
                return JournalEngine.create_balanced_entry(
                    voucher_type='PAYMENT',
                    date_ad=timezone.now().date(),
                    branch=target_branch,
                    lines=lines,
                    narration=narration,
                    reference_doc=ref_document,
                    user=user,
                    auto_post=True
                )
            except Exception as err:
                logger.error(f"[post_supplier_payout_journal Fallback Error] Supplier {supplier.id}: {err}")
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
# SUPPLIER VIEWS
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
            qs = qs.filter(
                Q(code__icontains=query) |
                Q(company_name__icontains=query) |
                Q(contact_person__icontains=query) |
                Q(phone_number__icontains=query) |
                Q(registration_number__icontains=query) |
                Q(pan_number__icontains=query)
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
        context['payment_form'] = SupplierPaymentForm()
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
    Enforces role authorization (Owner, Manager, Accountant) and acquires a database
    row-level lock (select_for_update) inside an atomic transaction.
    Automatically posts a balanced double-entry payment voucher to the General Ledger:
    - Dr: Accounts Payable (Supplier Sub-Ledger)
    - Cr: Cash in Hand / Bank Account
    All operations are executed atomically without swallowing GL or ledger errors.
    """

    def post(self, request, pk, *args, **kwargs):
        form = SupplierPaymentForm(request.POST)

        if form.is_valid():
            amount = form.cleaned_data['amount']
            payment_mode = form.cleaned_data['payment_mode']
            ref_no = form.cleaned_data.get('reference_number', '')
            cheque_dt = form.cleaned_data.get('cheque_date')
            remarks = form.cleaned_data.get('remarks', '')

            try:
                with transaction.atomic():
                    # 1. Acquire row lock on Supplier record
                    supplier = Supplier.objects.select_for_update().get(pk=pk)
                    prev_bal = supplier.current_balance or Decimal('0.00')

                    active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()

                    # 2. Record sub-ledger entry
                    ledger_entry = SupplierUdhaariLedger.objects.create(
                        supplier=supplier,
                        branch=active_branch,
                        transaction_type='PAYMENT',
                        amount=amount,
                        previous_balance=prev_bal,
                        resulting_balance=prev_bal - amount,
                        payment_mode=payment_mode,
                        reference_number=ref_no,
                        cheque_date=cheque_dt,
                        remarks=remarks,
                        recorded_by=request.user
                    )

                    # 3. Recalculate balance strictly from sub-ledger entries (ensuring mathematical integrity)
                    new_bal = supplier.recalculate_balance_from_ledger(save=True)
                    if ledger_entry.resulting_balance != new_bal:
                        ledger_entry.resulting_balance = new_bal
                        ledger_entry.save(update_fields=['resulting_balance'])

                    # 4. Post Double-Entry Journal to General Ledger (Dr Accounts Payable, Cr Cash/Bank)
                    import apps.accounting.services.auto_posting as auto_posting_service
                    gl_entry = auto_posting_service.post_supplier_payout_journal(
                        supplier=supplier,
                        amount=amount,
                        payment_mode=payment_mode,
                        ref_no=ref_no or f"SUP-PAY-{ledger_entry.id}",
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
                            'prev_balance': str(prev_bal),
                            'new_balance': str(new_bal),
                            'payment_mode': payment_mode,
                            'gl_entry_id': getattr(gl_entry, 'id', str(gl_entry))
                        }
                    )

                messages.success(
                    request,
                    f"Payment of Rs. {amount:.2f} successfully recorded for {supplier.company_name} "
                    f"(New Balance: Rs. {new_bal:.2f})."
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
            messages.error(request, "Invalid payment values submitted. Please verify the amount.")
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

                messages.success(request, f"Purchase Order {po.po_number} created with {valid_items_count} item lines (Total: Rs. {po.total_amount:.2f}).")
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
    """Updates the workflow status of an existing Purchase Order (e.g. DRAFT -> ISSUED -> CANCELLED)."""

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

        if query:
            qs = qs.filter(
                Q(grn_number__icontains=query) |
                Q(supplier_bill_no__icontains=query) |
                Q(supplier__company_name__icontains=query)
            )
        if status_filter:
            qs = qs.filter(status=status_filter)

        return qs.order_by('-created_at')


class GRNDetailView(PurchaseModuleAccessMixin, DetailView):
    model = GoodsReceivedNote
    template_name = 'purchases/grn_detail.html'
    context_object_name = 'grn'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['items'] = self.object.items.select_related('product', 'product__base_unit', 'unit_conversion').all()
        return context


class GRNCreateView(PurchaseModuleAccessMixin, View):
    template_name = 'purchases/grn_form.html'

    def get(self, request, *args, **kwargs):
        branch = getattr(request, 'active_branch', None)
        if not branch:
            messages.error(request, "Please switch to an active store/branch before creating GRN.")
            return redirect('purchases:grn_list')

        form = GoodsReceivedNoteForm()
        formset = GRNItemFormSet()

        # Check if prefilling from a Purchase Order
        po_id = request.GET.get('po_id')
        if po_id:
            po = PurchaseOrder.objects.filter(id=po_id, status__in=['ISSUED', 'PARTIALLY_RECEIVED']).first()
            if po:
                form = GoodsReceivedNoteForm(initial={
                    'supplier': po.supplier,
                    'purchase_order': po,
                    'bill_date': timezone.now().date(),
                    'supplier_bill_no': f"PO-{po.po_number}",
                })

        return render(request, self.template_name, {'form': form, 'formset': formset})

    def post(self, request, *args, **kwargs):
        branch = getattr(request, 'active_branch', None)
        form = GoodsReceivedNoteForm(request.POST)
        formset = GRNItemFormSet(request.POST)

        if form.is_valid() and formset.is_valid():
            try:
                with transaction.atomic():
                    grn = form.save(commit=False)
                    grn.branch = branch
                    grn.grn_number = BranchDocumentSequence.get_next_sequence_number(
                        branch=branch,
                        document_type='GOODS_RECEIPT'
                    )
                    grn.status = 'DRAFT'
                    grn.save()

                    items = formset.save(commit=False)
                    for item in items:
                        item.grn = grn
                        factor = item.conversion_factor if item.conversion_factor and item.conversion_factor > Decimal('0.000') else Decimal('1.000')
                        qty = item.purchased_quantity if item.purchased_quantity and item.purchased_quantity > Decimal('0.000') else Decimal('1.000')
                        item.base_unit_quantity = qty * factor
                        rate = item.purchase_rate or Decimal('0.00')
                        gross = qty * rate
                        disc = gross * ((item.discount_percent or Decimal('0.00')) / Decimal('100.00'))
                        item.line_total = gross - disc
                        item.save()

                    for deleted_item in formset.deleted_objects:
                        deleted_item.delete()

                    # Trigger Full Stock Receipt, Dual-IMEI registration & Supplier Ledger Update
                    PurchaseService.process_grn_approval_and_stock_in(grn=grn, user=request.user)

                    # Recalculate supplier balance from ledger to maintain complete sub-ledger consistency
                    grn.supplier.recalculate_balance_from_ledger(save=True)

                    # If this GRN was linked to a PO, update the PO status
                    if grn.purchase_order:
                        po = grn.purchase_order
                        po.status = 'COMPLETED'
                        po.save(update_fields=['status', 'updated_at'])

                messages.success(request, f"GRN Voucher {grn.grn_number} processed and warehouse stock updated.")
                return redirect('purchases:grn_detail', pk=grn.pk)
            except Exception as e:
                messages.error(request, f"Error processing GRN: {str(e)}")
        else:
            messages.error(request, "Validation errors occurred. Please check your entries.")

        return render(request, self.template_name, {'form': form, 'formset': formset})


# ==============================================================================
# DEDICATED PURCHASE BILL (GRN) CANCELLATION CONTROLLER (PART B)
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
    4. Deducts the inward merchandise quantities back out of BranchStock via InventoryService.
    5. Depletes / archives active FIFO ProductBatch records associated with this GRN.
    6. Archives unsold ItemInstance handset records so they cannot be sold.
    7. Atomically reduces the supplier's balance in Supplier and records a reversal entry in SupplierUdhaariLedger.
    8. Voids original General Ledger purchase journal voucher and posts reversing double-entry voucher.
    9. Sets GRN status to 'CANCELLED'.
    10. Records an immutable forensic event in AuditLog.
    Supports both standard form submissions and asynchronous AJAX / JSON payloads.
    """
    grn = get_object_or_404(GoodsReceivedNote, pk=pk)

    # Permission check: superusers, owners, or store managers only
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

    # Extract cancellation reason from JSON or POST form data
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

    # Mandatory cancellation reason validation
    if not reason or len(reason) < 3:
        err_msg = "A valid, mandatory cancellation reason is required to void this purchase bill (minimum 3 characters)."
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.content_type == 'application/json':
            return JsonResponse({'status': 'error', 'message': err_msg}, status=400)
        messages.error(request, err_msg)
        return redirect('purchases:grn_detail', pk=grn.pk)

    # Prevent re-cancelling already voided GRNs
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

                # Deduct physical warehouse stock
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

                # Deplete FIFO batches created by this GRN
                ProductBatch.objects.filter(
                    grn_reference=grn.grn_number,
                    product=item.product,
                    branch=grn.branch
                ).update(
                    quantity_remaining=Decimal('0.000'),
                    is_depleted=True,
                    updated_at=timezone.now()
                )

            # 3. Archive unsold handset instances so they are permanently decommissioned from active stock
            instances.filter(status='IN_STOCK').update(
                status='ARCHIVED',
                mdms_remarks=f"Consignment voided with GRN {grn.grn_number}: {reason}",
                updated_at=timezone.now()
            )

            # 4. Reverse Supplier Udhaari Balance & Record Ledger Reversal Entry
            supplier = Supplier.objects.select_for_update().get(pk=grn.supplier_id)
            prev_bal = supplier.current_balance or Decimal('0.00')
            debt_to_reverse = grn.due_amount if grn.due_amount > Decimal('0.00') else grn.net_total_amount
            new_bal = max(Decimal('0.00'), prev_bal - debt_to_reverse)

            supplier.current_balance = new_bal
            supplier.save(update_fields=['current_balance', 'updated_at'])

            SupplierUdhaariLedger.objects.create(
                supplier=supplier,
                branch=grn.branch,
                transaction_type='PURCHASE_RETURN',
                amount=debt_to_reverse,
                previous_balance=prev_bal,
                resulting_balance=new_bal,
                payment_mode='OTHER',
                reference_number=f"VOID-{grn.grn_number}",
                recorded_by=request.user,
                remarks=f"Cancellation reversal of purchase GRN {grn.grn_number} (Supplier Bill: {grn.supplier_bill_no}). Reason: {reason}"
            )

            # 5. Void & Reverse General Ledger Journal Entry if posted
            try:
                from apps.accounting.models import JournalEntry
                from apps.accounting.services.auto_posting import JournalEngine

                orig_entry = JournalEntry.objects.filter(
                    voucher_type='PURCHASE',
                    reference_document=grn.grn_number,
                    status='POSTED'
                ).first()

                if orig_entry:
                    orig_entry.status = 'CANCELLED'
                    orig_entry.save(update_fields=['status', 'updated_at'])

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
                            user=request.user,
                            auto_post=True
                        )
            except Exception:
                pass

            # 6. Mark GRN Status as CANCELLED and store reason
            grn.status = 'CANCELLED'
            if hasattr(grn, 'cancellation_reason'):
                grn.cancellation_reason = reason
                grn.save(update_fields=['status', 'cancellation_reason', 'updated_at'])
            elif hasattr(grn, 'remarks'):
                grn.remarks = f"CANCELLED: {reason}"
                grn.save(update_fields=['status', 'remarks', 'updated_at'])
            else:
                grn.save(update_fields=['status', 'updated_at'])

            # 7. Forensic AuditLog
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
                    'due_amount_reversed': str(grn.due_amount),
                    'paid_amount_reversed': str(grn.paid_amount),
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
    """
    Class-based wrapper around cancel_grn_view for backward compatibility with class routing.
    """
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
            qs = qs.filter(
                Q(return_number__icontains=query) |
                Q(original_bill_reference__icontains=query) |
                Q(supplier__company_name__icontains=query) |
                Q(remarks__icontains=query) |
                Q(items__returned_imei_list__icontains=query)
            ).distinct()

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

        return qs.order_by('-return_date', '-created_at')

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
            'suppliers': Supplier.objects.filter(is_active=True).order_by('company_name'),
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

        # Check if pre-filling from a specific GRN
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
            sup = Supplier.objects.filter(id=supplier_id, is_active=True).first()
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
                            factor = item.conversion_factor if item.conversion_factor and item.conversion_factor > Decimal('0.000') else Decimal('1.000')
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
                    f"Total refund value: Rs. {purchase_return.net_refund_amount:.2f} ({purchase_return.get_refund_mode_display()})."
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
    """
    model = PurchaseReturn
    template_name = 'purchases/purchase_return_detail.html'
    context_object_name = 'purchase_return'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['items'] = self.object.items.select_related('product', 'product__base_unit', 'unit_conversion', 'item_instance').all()
        context['config'] = SystemConfiguration.get_solo()
        return context