import uuid
import re
from decimal import Decimal
from django.db import transaction
from django.db.models import Q
from django.shortcuts import render, redirect, get_object_or_404
from django.views.generic import ListView, DetailView, CreateView, UpdateView, View
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib import messages
from django.urls import reverse_lazy, reverse
from django.utils import timezone
from django.core.exceptions import ValidationError

from apps.branches.models import Branch, StockTransferRequest, StockTransferItem, BranchDocumentSequence
from apps.branches.forms import BranchForm, BranchSwitchForm, StockTransferRequestForm, StockTransferItemFormSet
from apps.inventory.models import Product, ItemInstance, BranchStock
from apps.inventory.services import InventoryService
from apps.core.models import AuditLog


class BranchListView(LoginRequiredMixin, ListView):
    model = Branch
    template_name = 'branches/branch_list.html'
    context_object_name = 'branches'

    def get_queryset(self):
        return Branch.objects.select_related('manager').order_by('-is_main_branch', 'name')


class BranchCreateView(LoginRequiredMixin, UserPassesTestMixin, CreateView):
    model = Branch
    form_class = BranchForm
    template_name = 'branches/branch_form.html'
    success_url = reverse_lazy('branches:branch_list')

    def test_func(self):
        return self.request.user.is_superuser or getattr(self.request.user, 'role', '') in ['OWNER', 'MANAGER']

    def form_valid(self, form):
        with transaction.atomic():
            if form.cleaned_data.get('is_main_branch'):
                Branch.objects.filter(is_main_branch=True).update(is_main_branch=False)

            response = super().form_valid(form)

            AuditLog.objects.create(
                user=self.request.user,
                branch=self.object,
                action_type='CREATE',
                module='Branch',
                object_repr=str(self.object),
                ip_address=self.request.META.get('REMOTE_ADDR'),
                details={'code': self.object.code, 'name': self.object.name, 'is_main': self.object.is_main_branch}
            )

        messages.success(self.request, f"Branch '{self.object.name}' successfully registered.")
        return response


class BranchUpdateView(LoginRequiredMixin, UserPassesTestMixin, UpdateView):
    model = Branch
    form_class = BranchForm
    template_name = 'branches/branch_form.html'
    success_url = reverse_lazy('branches:branch_list')

    def test_func(self):
        return self.request.user.is_superuser or getattr(self.request.user, 'role', '') in ['OWNER', 'MANAGER']

    def form_valid(self, form):
        with transaction.atomic():
            if form.cleaned_data.get('is_main_branch'):
                Branch.objects.filter(is_main_branch=True).exclude(pk=self.object.pk).update(is_main_branch=False)

            response = super().form_valid(form)

            AuditLog.objects.create(
                user=self.request.user,
                branch=self.object,
                action_type='UPDATE',
                module='Branch',
                object_repr=str(self.object),
                ip_address=self.request.META.get('REMOTE_ADDR'),
                details={'updated_fields': list(form.changed_data), 'is_main': self.object.is_main_branch}
            )

        messages.success(self.request, f"Branch '{self.object.name}' updated successfully.")
        return response


class SwitchBranchContextView(LoginRequiredMixin, View):
    """
    Sets session-level branch ID for multi-branch monitoring and POS isolation.
    Enforces role-based isolation: standard Staff/Cashiers cannot hop across branches
    and are strictly locked to their assigned branch.
    """

    def post(self, request, *args, **kwargs):
        branch_id = request.POST.get('branch')
        next_url = request.POST.get('next', request.META.get('HTTP_REFERER', '/'))
        user = request.user

        branch = get_object_or_404(Branch, id=branch_id, is_active=True)
        is_privileged = user.is_superuser or getattr(user, 'role', '') in ['OWNER', 'MANAGER']

        if not is_privileged:
            assigned_branch = getattr(user, 'assigned_branch', None)
            if assigned_branch and assigned_branch.is_active and assigned_branch.id != branch.id:
                messages.error(
                    request,
                    f"Permission Denied: Staff accounts are restricted to their assigned branch ({assigned_branch.name})."
                )
                request.session['active_branch_id'] = assigned_branch.id
                return redirect(next_url)

        request.session['active_branch_id'] = branch.id
        messages.success(request, f"Active store context switched to: {branch.name} ({branch.code})")
        return redirect(next_url)


class StockTransferListView(LoginRequiredMixin, ListView):
    model = StockTransferRequest
    template_name = 'branches/transfer_list.html'
    context_object_name = 'transfers'
    paginate_by = 25

    def get_queryset(self):
        active_branch = getattr(self.request, 'active_branch', None) or Branch.get_default_main_branch()
        qs = StockTransferRequest.objects.select_related(
            'source_branch', 'destination_branch', 'requested_by', 'dispatched_by', 'received_by'
        ).prefetch_related('items__product')

        if active_branch and not self.request.user.is_superuser:
            qs = qs.filter(Q(source_branch=active_branch) | Q(destination_branch=active_branch))

        return qs.order_by('-created_at')


class StockTransferCreateView(LoginRequiredMixin, View):
    template_name = 'branches/transfer_form.html'

    def get(self, request, *args, **kwargs):
        source = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()
        form = StockTransferRequestForm(source_branch=source)
        formset = StockTransferItemFormSet()
        return render(request, self.template_name, {
            'form': form,
            'formset': formset,
            'source_branch': source
        })

    def post(self, request, *args, **kwargs):
        source = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()
        form = StockTransferRequestForm(request.POST, source_branch=source)
        formset = StockTransferItemFormSet(request.POST)

        if form.is_valid() and formset.is_valid():
            try:
                with transaction.atomic():
                    transfer = form.save(commit=False)
                    transfer.source_branch = source
                    transfer.requested_by = request.user
                    transfer.transfer_no = BranchDocumentSequence.get_next_sequence_number(
                        branch=source,
                        document_type='STOCK_TRANSFER'
                    )
                    transfer.status = 'DRAFT'
                    transfer.save()

                    items = formset.save(commit=False)
                    valid_item_count = 0

                    for item in items:
                        if item.product and item.quantity > Decimal('0.000'):
                            item.transfer_request = transfer
                            
                            # Validate IMEI if product requires IMEI tracking
                            if item.product.requires_imei_tracking:
                                clean_imei = str(item.scanned_imei_or_serial or '').strip()
                                if not clean_imei:
                                    raise ValidationError(
                                        f"Primary IMEI is required for transferring serialized smartphone '{item.product.name}'."
                                    )

                                instance = ItemInstance.objects.select_for_update().filter(
                                    Q(imei_1=clean_imei) | Q(imei_2=clean_imei) | Q(serial_number=clean_imei),
                                    branch=source,
                                    status='IN_STOCK'
                                ).first()

                                if not instance:
                                    raise ValidationError(
                                        f"Device with IMEI '{clean_imei}' was not found in available stock at {source.name}."
                                    )
                                item.item_instance = instance

                            item.save()
                            valid_item_count += 1

                    if valid_item_count == 0:
                        raise ValidationError("Please add at least one valid product line item to the transfer requisition.")

                    AuditLog.objects.create(
                        user=request.user,
                        branch=source,
                        action_type='CREATE',
                        module='StockTransfer',
                        object_repr=transfer.transfer_no,
                        ip_address=request.META.get('REMOTE_ADDR'),
                        details={
                            'destination': transfer.destination_branch.name,
                            'item_count': valid_item_count
                        }
                    )

                messages.success(request, f"Transfer requisition '{transfer.transfer_no}' generated with {valid_item_count} item line(s).")
                return redirect('branches:transfer_list')

            except ValidationError as ve:
                messages.error(request, str(ve.message if hasattr(ve, 'message') else ve))
            except Exception as e:
                messages.error(request, f"Error creating transfer requisition: {str(e)}")
        else:
            messages.error(request, "Validation errors occurred. Please verify product selections and quantities.")

        return render(request, self.template_name, {
            'form': form,
            'formset': formset,
            'source_branch': source
        })


class StockTransferDispatchView(LoginRequiredMixin, UserPassesTestMixin, View):
    """
    Executes physical inventory dispatch from source branch:
    - Deducts sellable stock from Source Branch via InventoryService.
    - Marks serialized ItemInstances as 'TRANSFERRED'.
    - Advances transfer status to 'DISPATCHED'.
    """

    def test_func(self):
        return self.request.user.is_superuser or getattr(self.request.user, 'role', '') in ['OWNER', 'MANAGER', 'STAFF']

    def post(self, request, pk, *args, **kwargs):
        transfer = get_object_or_404(StockTransferRequest, pk=pk)

        if transfer.status != 'DRAFT':
            messages.error(request, f"Transfer '{transfer.transfer_no}' is already {transfer.get_status_display().lower()}.")
            return redirect('branches:transfer_list')

        try:
            with transaction.atomic():
                transfer = StockTransferRequest.objects.select_for_update().get(pk=pk)

                for line in transfer.items.select_related('product', 'item_instance').all():
                    # Deduct quantity from Source Branch stock
                    InventoryService.adjust_stock(
                        product=line.product,
                        branch=transfer.source_branch,
                        quantity_delta=-line.quantity,
                        movement_type='TRANSFER_OUT',
                        reference_doc=transfer.transfer_no,
                        imei_or_serial=line.scanned_imei_or_serial or "",
                        remarks=f"Dispatched to {transfer.destination_branch.name}",
                        user=request.user,
                        allow_negative=False
                    )

                    # Update ItemInstance status to TRANSFERRED
                    if line.item_instance:
                        line.item_instance.status = 'TRANSFERRED'
                        line.item_instance.save(update_fields=['status', 'updated_at'])

                transfer.status = 'DISPATCHED'
                transfer.dispatched_by = request.user
                transfer.dispatched_date = timezone.now()
                transfer.save(update_fields=['status', 'dispatched_by', 'dispatched_date', 'updated_at'])

                AuditLog.objects.create(
                    user=request.user,
                    branch=transfer.source_branch,
                    action_type='UPDATE',
                    module='StockTransferDispatch',
                    object_repr=transfer.transfer_no,
                    ip_address=request.META.get('REMOTE_ADDR'),
                    details={'destination': transfer.destination_branch.name}
                )

            messages.success(request, f"Stock Transfer '{transfer.transfer_no}' dispatched. Inventory deducted from {transfer.source_branch.name}.")
        except Exception as e:
            messages.error(request, f"Dispatch failed: {str(e)}")

        return redirect('branches:transfer_list')


class StockTransferReceiveView(LoginRequiredMixin, UserPassesTestMixin, View):
    """
    Executes physical inventory receipt at destination branch:
    - Increments sellable stock at Destination Branch via InventoryService.
    - Re-points serialized ItemInstances to Destination Branch and resets status to 'IN_STOCK'.
    - Finalizes transfer status as 'RECEIVED'.
    """

    def test_func(self):
        return self.request.user.is_superuser or getattr(self.request.user, 'role', '') in ['OWNER', 'MANAGER', 'STAFF']

    def post(self, request, pk, *args, **kwargs):
        transfer = get_object_or_404(StockTransferRequest, pk=pk)

        if transfer.status != 'DISPATCHED':
            messages.error(request, f"Cannot receive transfer '{transfer.transfer_no}' (Current Status: {transfer.get_status_display()}).")
            return redirect('branches:transfer_list')

        try:
            with transaction.atomic():
                transfer = StockTransferRequest.objects.select_for_update().get(pk=pk)

                for line in transfer.items.select_related('product', 'item_instance').all():
                    # Increment quantity in Destination Branch stock
                    InventoryService.adjust_stock(
                        product=line.product,
                        branch=transfer.destination_branch,
                        quantity_delta=line.quantity,
                        movement_type='TRANSFER_IN',
                        reference_doc=transfer.transfer_no,
                        imei_or_serial=line.scanned_imei_or_serial or "",
                        remarks=f"Received from {transfer.source_branch.name}",
                        user=request.user,
                        allow_negative=True
                    )

                    # Update ItemInstance branch ownership and mark IN_STOCK
                    if line.item_instance:
                        line.item_instance.branch = transfer.destination_branch
                        line.item_instance.status = 'IN_STOCK'
                        line.item_instance.save(update_fields=['branch', 'status', 'updated_at'])

                transfer.status = 'RECEIVED'
                transfer.received_by = request.user
                transfer.received_date = timezone.now()
                transfer.save(update_fields=['status', 'received_by', 'received_date', 'updated_at'])

                AuditLog.objects.create(
                    user=request.user,
                    branch=transfer.destination_branch,
                    action_type='UPDATE',
                    module='StockTransferReceive',
                    object_repr=transfer.transfer_no,
                    ip_address=request.META.get('REMOTE_ADDR'),
                    details={'source': transfer.source_branch.name}
                )

            messages.success(request, f"Transfer '{transfer.transfer_no}' received & verified. Stock added to {transfer.destination_branch.name}.")
        except Exception as e:
            messages.error(request, f"Stock receipt failed: {str(e)}")

        return redirect('branches:transfer_list')