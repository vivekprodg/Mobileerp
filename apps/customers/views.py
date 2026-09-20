"""
Customer Management, Directory & Credit (Udhaari) Ledger Views.

Capabilities:
1. Customer Directory: Search and filter by name, phone number, PAN, customer type, or debt status.
2. Customer Profile & Sub-Ledger: Detailed breakdown of purchase history and Udhaari movements.
3. Customer Udhaari Payment Processing:
   - Row-level lock (`select_for_update`) on Customer within an atomic transaction.
   - Sub-ledger entry creation prior to balance adjustment.
   - Synchronous, unswallowed double-entry GL receipt voucher posting.
   - Balance cache recalculation strictly from ledger entries upon posting success.
4. POS Fast Auto-Complete Lookup API: Real-time search by phone number or name.
"""

import logging
from decimal import Decimal, ROUND_HALF_UP
from django.db import transaction
from django.db.models import Q
from django.shortcuts import render, redirect, get_object_or_404
from django.views.generic import ListView, DetailView, CreateView, UpdateView, View
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib import messages
from django.urls import reverse_lazy, reverse
from django.http import JsonResponse
from django.utils import timezone

from apps.customers.models import Customer, CustomerUdhaariLedger
from apps.customers.forms import CustomerForm, CustomerPaymentForm
from apps.core.models import AuditLog

logger = logging.getLogger(__name__)

# =============================================================================
# GENERAL LEDGER DISPATCHER BRIDGE (DEFENSIVE COMPATIBILITY)
# =============================================================================
try:
    import apps.accounting.services.auto_posting as auto_posting_mod

    if not hasattr(auto_posting_mod, 'post_customer_repayment_journal'):
        def _post_customer_repayment_journal(customer, amount, payment_mode, reference=None, user=None, branch=None, **kwargs):
            """
            Fallback double-entry poster for customer debt repayment:
            - Dr: Cash in Hand (if CASH) or Bank & Digital Wallets
            - Cr: Accounts Receivable (Debtors Control Account)
            Does not swallow errors; propagates exceptions to enforce atomic integrity.
            """
            from apps.accounting.models import JournalEntry
            from apps.accounting.services.auto_posting import JournalEngine, AutoPostingService
            from apps.branches.models import Branch

            amount_dec = Decimal(str(amount)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            if amount_dec <= Decimal('0.00'):
                return None

            ref_doc = reference or f"UDH-CUST-{customer.id}-{timezone.now().strftime('%Y%m%d%H%M%S')}"
            existing = JournalEntry.objects.filter(
                voucher_type='RECEIPT',
                reference_document=ref_doc,
                status='POSTED'
            ).first()
            if existing:
                return existing

            target_branch = branch or getattr(customer, 'preferred_branch', None) or Branch.get_default_main_branch()

            cash_acc = AutoPostingService.get_or_create_control_account(
                target_branch, 'CASH', '1010', 'Cash in Hand', 'ASSET', 'DEBIT'
            )
            bank_acc = AutoPostingService.get_or_create_control_account(
                target_branch, 'BANK', '1020', 'Bank & Digital Wallets', 'ASSET', 'DEBIT'
            )
            ar_acc = AutoPostingService.get_or_create_control_account(
                target_branch, 'ACCOUNTS_RECEIVABLE', '1030', 'Accounts Receivable (Debtors)', 'ASSET', 'DEBIT'
            )

            dest_acc = cash_acc if str(payment_mode).upper() == 'CASH' else bank_acc

            lines = [
                {
                    'account': dest_acc,
                    'debit': amount_dec,
                    'credit': Decimal('0.00'),
                    'customer': customer,
                    'narration': f"Customer Udhaari repayment from {customer.name} via {payment_mode}"
                },
                {
                    'account': ar_acc,
                    'debit': Decimal('0.00'),
                    'credit': amount_dec,
                    'customer': customer,
                    'narration': f"Settlement of outstanding debt by {customer.name}"
                }
            ]

            narration = f"Customer debt repayment: {customer.name} (Rs. {amount_dec:.2f}) via {payment_mode}"
            return JournalEngine.create_balanced_entry(
                voucher_type='RECEIPT',
                date_ad=timezone.now().date(),
                branch=target_branch,
                lines=lines,
                narration=narration,
                reference_doc=ref_doc,
                user=user,
                auto_post=True
            )

        auto_posting_mod.post_customer_repayment_journal = _post_customer_repayment_journal
except Exception:
    pass


# =============================================================================
# CUSTOMER DIRECTORY & CRUD VIEWS
# =============================================================================

class CustomerListView(LoginRequiredMixin, ListView):
    model = Customer
    template_name = 'customers/customer_list.html'
    context_object_name = 'customers'
    paginate_by = 25

    def get_queryset(self):
        qs = Customer.objects.select_related('preferred_branch')
        query = self.request.GET.get('q', '').strip()
        c_type = self.request.GET.get('type', '').strip()
        has_debt = self.request.GET.get('has_debt', '').strip()

        if query:
            qs = qs.filter(
                Q(name__icontains=query) |
                Q(phone_number__icontains=query) |
                Q(pan_number__icontains=query)
            )
        if c_type:
            qs = qs.filter(customer_type=c_type)
        if has_debt == 'true':
            qs = qs.filter(current_credit_balance__gt=Decimal('0.00'))

        return qs.order_by('-current_credit_balance', 'name')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        page_customers = context['customers']
        total_page_debt = sum((c.current_credit_balance for c in page_customers), Decimal('0.00'))
        context['total_page_debt'] = total_page_debt
        return context


class CustomerDetailView(LoginRequiredMixin, DetailView):
    model = Customer
    template_name = 'customers/customer_detail.html'
    context_object_name = 'customer'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['ledger_entries'] = self.object.credit_ledger_entries.select_related('branch', 'recorded_by')[:50]
        context['payment_form'] = CustomerPaymentForm()
        return context


class CustomerCreateView(LoginRequiredMixin, UserPassesTestMixin, CreateView):
    model = Customer
    form_class = CustomerForm
    template_name = 'customers/customer_form.html'
    success_url = reverse_lazy('customers:customer_list')

    def test_func(self):
        return self.request.user.is_superuser or getattr(self.request.user, 'role', '') in ['OWNER', 'MANAGER', 'STAFF', 'ACCOUNTANT']

    def form_valid(self, form):
        response = super().form_valid(form)
        AuditLog.objects.create(
            user=self.request.user,
            branch=getattr(self.request, 'active_branch', None),
            action_type='CREATE',
            module='Customer',
            object_repr=str(self.object),
            ip_address=self.request.META.get('REMOTE_ADDR'),
            details={'phone': self.object.phone_number, 'type': self.object.customer_type}
        )
        messages.success(self.request, f"Customer '{self.object.name}' created successfully.")
        return response


class CustomerUpdateView(LoginRequiredMixin, UserPassesTestMixin, UpdateView):
    model = Customer
    form_class = CustomerForm
    template_name = 'customers/customer_form.html'

    def test_func(self):
        return self.request.user.is_superuser or getattr(self.request.user, 'role', '') in ['OWNER', 'MANAGER', 'STAFF', 'ACCOUNTANT']

    def get_success_url(self):
        return reverse('customers:customer_detail', kwargs={'pk': self.object.pk})

    def form_valid(self, form):
        response = super().form_valid(form)
        AuditLog.objects.create(
            user=self.request.user,
            branch=getattr(self.request, 'active_branch', None),
            action_type='UPDATE',
            module='Customer',
            object_repr=str(self.object),
            ip_address=self.request.META.get('REMOTE_ADDR'),
            details={'updated_fields': list(form.changed_data)}
        )
        messages.success(self.request, f"Customer '{self.object.name}' updated successfully.")
        return response


class CustomerUdhaariPaymentView(LoginRequiredMixin, UserPassesTestMixin, View):
    """
    SEC-04 Compliance: Enforces role-based permissions on customer Udhaari balance alterations.
    Only Superusers, Owners, Managers, and Accountants can record debt repayments.

    Sub-Ledger & General Ledger Protection Protocol:
    1. Locks the Customer row with `select_for_update()` inside an atomic transaction.
    2. Appends the CustomerUdhaariLedger entry without upfront balance mutation.
    3. Synchronously executes General Ledger double-entry posting without swallowing errors.
    4. Updates and caches the Customer balance from the sub-ledger ONLY after GL posting succeeds.
    """

    def test_func(self):
        user = self.request.user
        return user.is_authenticated and (
            user.is_superuser or getattr(user, 'role', '') in ['OWNER', 'MANAGER', 'ACCOUNTANT']
        )

    def handle_no_permission(self):
        messages.error(
            self.request,
            "Permission Denied: Only Store Owners, Managers, or Accountants can record customer debt repayments."
        )
        return redirect('customers:customer_list')

    def post(self, request, pk, *args, **kwargs):
        form = CustomerPaymentForm(request.POST)

        if form.is_valid():
            amount = form.cleaned_data['amount']
            payment_mode = form.cleaned_data['payment_mode']
            reference = form.cleaned_data.get('reference_invoice', '')
            remarks = form.cleaned_data.get('remarks', '')

            try:
                with transaction.atomic():
                    # 1. Acquire row-level lock on customer record
                    customer = Customer.objects.select_for_update().get(pk=pk)
                    prev_bal = customer.current_credit_balance
                    new_bal = (prev_bal - amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

                    active_branch = getattr(request, 'active_branch', None) or customer.preferred_branch

                    # 2. Record CustomerUdhaariLedger sub-ledger entry FIRST (Do NOT mutate customer balance yet)
                    ledger_entry = CustomerUdhaariLedger.objects.create(
                        customer=customer,
                        branch=active_branch,
                        entry_type='CREDIT',
                        amount=amount,
                        previous_balance=prev_bal,
                        resulting_balance=new_bal,
                        payment_mode=payment_mode,
                        reference_invoice=reference,
                        remarks=remarks,
                        recorded_by=request.user
                    )

                    # 3. Synchronously post Double-Entry Journal to GL without swallowing errors
                    import apps.accounting.services.auto_posting as auto_posting_mod
                    voucher_ref = reference or f"UDH-CUST-{ledger_entry.id}"
                    auto_posting_mod.post_customer_repayment_journal(
                        customer=customer,
                        amount=amount,
                        payment_mode=payment_mode,
                        reference=voucher_ref,
                        user=request.user,
                        branch=active_branch
                    )

                    # 4. Update the cached balance strictly from sub-ledger after GL entry succeeds
                    customer.recalculate_balance_from_ledger(save=True)

                    # 5. Record immutable audit log
                    AuditLog.objects.create(
                        user=request.user,
                        branch=active_branch,
                        action_type='UPDATE',
                        module='CustomerUdhaariPayment',
                        object_repr=f"Udhaari Payment from {customer.name}",
                        ip_address=request.META.get('REMOTE_ADDR'),
                        details={
                            'amount': str(amount),
                            'prev_balance': str(prev_bal),
                            'new_balance': str(customer.current_credit_balance),
                            'mode': payment_mode,
                            'ledger_entry_id': ledger_entry.id,
                            'reference': voucher_ref
                        }
                    )

                messages.success(request, f"Payment of Rs. {amount:.2f} successfully recorded for {customer.name}.")
                return redirect('customers:customer_detail', pk=customer.pk)

            except Customer.DoesNotExist:
                messages.error(request, "Customer record not found.")
                return redirect('customers:customer_list')
            except Exception as e:
                logger.error(f"[Customer Udhaari Payment Error] Customer PK {pk}: {e}", exc_info=True)
                messages.error(request, f"Error processing payment: {str(e)}")
                return redirect('customers:customer_detail', pk=pk)
        else:
            messages.error(request, "Invalid payment submission. Please verify the amount.")
            return redirect('customers:customer_detail', pk=pk)


class CustomerSearchAPIView(LoginRequiredMixin, View):
    """Fast lookup endpoint for POS screen auto-completion (expanded to 20 results)."""

    def get(self, request, *args, **kwargs):
        term = request.GET.get('q', '').strip()
        if len(term) < 2:
            return JsonResponse({'results': []})

        qs = Customer.objects.filter(
            Q(name__icontains=term) | Q(phone_number__icontains=term) | Q(pan_number__icontains=term),
            is_active=True
        )[:20]

        results = [
            {
                'id': c.id,
                'name': c.name,
                'phone': c.phone_number,
                'pan': c.pan_number or '',
                'customer_type': c.customer_type,
                'credit_balance': str(c.current_credit_balance),
                'credit_limit': str(c.credit_limit),
            }
            for c in qs
        ]
        return JsonResponse({'results': results})