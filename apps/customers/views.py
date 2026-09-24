"""
Customer Management, Directory & Credit (Udhaari) Ledger Views.

Capabilities:
1. Customer Directory: Search and filter by name, phone number, PAN, customer type, or debt status.
2. Customer Profile & Sub-Ledger: Detailed breakdown of purchase history and Udhaari movements.
   - Provides active Nepali Fiscal Year in context to seamlessly link to Party Confirmation Statements.
3. Customer Udhaari Payment Processing:
   - Row-level lock (`select_for_update`) on Customer within an atomic transaction.
   - Sub-ledger entry creation prior to balance adjustment.
   - Synchronous, unswallowed double-entry GL receipt voucher posting.
   - Balance cache recalculation strictly from ledger entries upon posting success.
4. Unified Customer Search API:
   - When query string is empty, returns the top 15 most recently active customers or debtors.
   - Partial matching across name, phone number, and PAN.
   - Adheres to the Unified Search JSON Contract (id, title, subtitle, badge, extra_data).
   - Backwards compatible with POS auto-complete.
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
from apps.core.nepali_calendar import NepaliCalendar
from apps.accounting.models import AccountingFiscalYear

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

        # Resolve active Nepali Fiscal Year for statement link
        today = timezone.now().date()
        try:
            bs_y, bs_m, _ = NepaliCalendar.ad_to_bs(today)
            current_fy = NepaliCalendar.get_fiscal_year(bs_y, bs_m)
        except Exception:
            active_fy_obj = AccountingFiscalYear.objects.filter(is_closed=False).first()
            current_fy = active_fy_obj.name if active_fy_obj else '2083/84'

        context['active_fiscal_year'] = current_fy
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
                    customer = Customer.objects.select_for_update().get(pk=pk)
                    prev_bal = customer.current_credit_balance
                    new_bal = (prev_bal - amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

                    active_branch = getattr(request, 'active_branch', None) or customer.preferred_branch

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

                    customer.recalculate_balance_from_ledger(save=True)

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
    def get(self, request, *args, **kwargs):
        q = request.GET.get('q', '').strip()
        customer_type = request.GET.get('type', '').strip()
        has_debt = request.GET.get('has_debt', '').strip()

        qs = Customer.objects.filter(is_active=True).select_related('preferred_branch')

        if customer_type:
            qs = qs.filter(customer_type=customer_type.upper())

        if has_debt in ['1', 'true', 'True']:
            qs = qs.filter(current_credit_balance__gt=Decimal('0.00'))

        if q:
            cust_filter = (
                Q(name__icontains=q) |
                Q(phone_number__icontains=q)
            )
            if hasattr(Customer, 'pan_number'):
                cust_filter |= Q(pan_number__icontains=q)
            if hasattr(Customer, 'email'):
                cust_filter |= Q(email__icontains=q)

            qs = qs.filter(cust_filter).order_by('-current_credit_balance', 'name')[:25]
        else:
            qs = qs.order_by('-current_credit_balance', '-updated_at')[:15]

        results = []
        for c in qs:
            c_type = getattr(c, 'customer_type', 'RETAIL') or 'RETAIL'
            c_type_display = c.get_customer_type_display() if hasattr(c, 'get_customer_type_display') else c_type
            pan = getattr(c, 'pan_number', '') or ''
            phone = getattr(c, 'phone_number', '') or ''
            addr = getattr(c, 'address', '') or ''
            email = getattr(c, 'email', '') or ''
            bal = getattr(c, 'current_credit_balance', Decimal('0.00'))
            limit = getattr(c, 'credit_limit', Decimal('0.00'))

            if bal > Decimal('0.00'):
                badge = f"Due: Rs. {bal:,.2f}"
                badge_color = 'danger'
            elif pan:
                badge = f"PAN: {pan}"
                badge_color = 'primary'
            else:
                badge = c_type_display
                badge_color = 'secondary'

            subtitle_parts = [f"Ph: {phone}"]
            if pan:
                subtitle_parts.append(f"PAN: {pan}")
            if bal > Decimal('0.00'):
                subtitle_parts.append(f"Due: Rs. {bal:,.2f}")
            elif addr:
                subtitle_parts.append(addr[:30])
            subtitle = " | ".join(subtitle_parts)

            extra_data = {
                'credit_balance': str(bal),
                'credit_limit': str(limit),
                'pan_number': pan,
                'phone_number': phone,
                'address': addr,
                'email': email,
                'customer_type': c_type,
                'customer_type_display': c_type_display,
                'wholesale_or_retail_badge': c_type_display,
                'preferred_branch_id': c.preferred_branch_id if getattr(c, 'preferred_branch_id', None) else None,
                'preferred_branch_name': c.preferred_branch.name if getattr(c, 'preferred_branch', None) else '',
            }

            results.append({
                'id': c.id,
                'title': c.name,
                'subtitle': subtitle,
                'badge': badge,
                'badge_color': badge_color,
                'extra_data': extra_data,
                'text': f"{c.name} ({phone})" if phone else c.name,
                'name': c.name,
                'phone': phone,
                'pan': pan,
                'address': addr,
                'email': email,
                'customer_type': c_type,
                'credit_balance': str(bal),
                'credit_limit': str(limit),
            })

        return JsonResponse({
            'status': 'success',
            'query': q,
            'count': len(results),
            'results': results
        })