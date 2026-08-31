from decimal import Decimal
from django.db import transaction
from django.db.models import Q, Sum
from django.shortcuts import render, redirect, get_object_or_404
from django.views.generic import ListView, DetailView, CreateView, UpdateView, View
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib import messages
from django.urls import reverse_lazy, reverse
from django.http import JsonResponse

from apps.customers.models import Customer, CustomerUdhaariLedger
from apps.customers.forms import CustomerForm, CustomerPaymentForm
from apps.core.models import AuditLog


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
            qs = qs.filter(Q(name__icontains=query) | Q(phone_number__icontains=query) | Q(pan_number__icontains=query))
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


class CustomerUdhaariPaymentView(LoginRequiredMixin, UserPassesTestMixin, View):
    """
    SEC-04 Compliance: Enforces role-based permissions on customer Udhaari balance alterations.
    Only Superusers, Owners, Managers, and Accountants can record debt repayments.
    Uses database row-level locking (select_for_update) inside an atomic transaction to eliminate
    race conditions during concurrent payments or simultaneous POS credit sales.
    """

    def test_func(self):
        user = self.request.user
        return user.is_authenticated and (
            user.is_superuser or getattr(user, 'role', '') in ['OWNER', 'MANAGER', 'ACCOUNTANT']
        )

    def handle_no_permission(self):
        messages.error(self.request, "Permission Denied: Only Store Owners, Managers, or Accountants can record customer debt repayments.")
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
                    # Acquire row-level lock on the customer record
                    customer = Customer.objects.select_for_update().get(pk=pk)
                    prev_bal = customer.current_credit_balance
                    new_bal = prev_bal - amount

                    customer.current_credit_balance = new_bal
                    customer.save(update_fields=['current_credit_balance', 'updated_at'])

                    CustomerUdhaariLedger.objects.create(
                        customer=customer,
                        branch=getattr(request, 'active_branch', None),
                        entry_type='CREDIT',
                        amount=amount,
                        previous_balance=prev_bal,
                        resulting_balance=new_bal,
                        payment_mode=payment_mode,
                        reference_invoice=reference,
                        remarks=remarks,
                        recorded_by=request.user
                    )

                    AuditLog.objects.create(
                        user=request.user,
                        branch=getattr(request, 'active_branch', None),
                        action_type='UPDATE',
                        module='CustomerUdhaariPayment',
                        object_repr=f"Udhaari Payment from {customer.name}",
                        ip_address=request.META.get('REMOTE_ADDR'),
                        details={
                            'amount': str(amount),
                            'prev_balance': str(prev_bal),
                            'new_balance': str(new_bal),
                            'mode': payment_mode
                        }
                    )

                messages.success(request, f"Payment of Rs. {amount:.2f} successfully recorded for {customer.name}.")
                return redirect('customers:customer_detail', pk=customer.pk)

            except Customer.DoesNotExist:
                messages.error(request, "Customer record not found.")
                return redirect('customers:customer_list')
            except Exception as e:
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