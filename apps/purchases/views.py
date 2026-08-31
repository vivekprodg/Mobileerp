import uuid
from decimal import Decimal, ROUND_HALF_UP
from django.shortcuts import render, redirect, get_object_or_404
from django.views.generic import ListView, DetailView, CreateView, UpdateView, View
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib import messages
from django.urls import reverse_lazy, reverse
from django.db import transaction
from django.db.models import Q, Sum, Count
from django.utils import timezone
from django.core.exceptions import ValidationError

from apps.purchases.models import (
    Supplier, PurchaseOrder, PurchaseOrderItem,
    GoodsReceivedNote, GRNItem, SupplierUdhaariLedger
)
from apps.purchases.forms import (
    SupplierForm, PurchaseOrderForm, PurchaseOrderItemFormSet,
    GoodsReceivedNoteForm, GRNItemFormSet, SupplierPaymentForm
)
from apps.purchases.services import PurchaseService
from apps.branches.models import Branch, BranchDocumentSequence
from apps.inventory.models import Product
from apps.core.models import AuditLog


class PurchaseModuleAccessMixin(LoginRequiredMixin, UserPassesTestMixin):
    """
    Enforces strict role-based access control across all supplier records,
    wholesale purchase costs, landed cost calculations, POs, and GRN inventory inward pages.
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
            "Permission Denied: Access to procurement records, purchase orders, wholesale rates, and GRN data is restricted to Store Owners, Managers, and Accountants."
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
                    supplier = Supplier.objects.select_for_update().get(pk=pk)
                    prev_bal = supplier.current_balance
                    new_bal = prev_bal - amount

                    supplier.current_balance = new_bal
                    supplier.save(update_fields=['current_balance', 'updated_at'])

                    SupplierUdhaariLedger.objects.create(
                        supplier=supplier,
                        branch=getattr(request, 'active_branch', None),
                        transaction_type='PAYMENT',
                        amount=amount,
                        previous_balance=prev_bal,
                        resulting_balance=new_bal,
                        payment_mode=payment_mode,
                        reference_number=ref_no,
                        cheque_date=cheque_dt,
                        remarks=remarks,
                        recorded_by=request.user
                    )

                    AuditLog.objects.create(
                        user=request.user,
                        branch=getattr(request, 'active_branch', None),
                        action_type='UPDATE',
                        module='SupplierPayment',
                        object_repr=f"Payout to {supplier.company_name}",
                        ip_address=request.META.get('REMOTE_ADDR'),
                        details={
                            'amount': str(amount),
                            'prev_balance': str(prev_bal),
                            'new_balance': str(new_bal),
                            'payment_mode': payment_mode
                        }
                    )

                messages.success(request, f"Payment of Rs. {amount:.2f} successfully recorded for {supplier.company_name}.")
                return redirect('purchases:supplier_detail', pk=supplier.pk)

            except Supplier.DoesNotExist:
                messages.error(request, "Supplier record not found.")
                return redirect('purchases:supplier_list')
            except Exception as e:
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