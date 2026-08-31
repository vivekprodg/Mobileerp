import csv
from decimal import Decimal
from django.shortcuts import render, redirect, get_object_or_404
from django.views.generic import TemplateView, ListView, DetailView, FormView, View
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib import messages
from django.urls import reverse_lazy, reverse
from django.http import HttpResponse
from django.db.models import Q

from apps.pos.models import CashDrawerSession
from apps.pos.forms import OpenCashDrawerForm, CloseCashDrawerForm
from apps.pos.services import POSSessionService


class OpenShiftView(LoginRequiredMixin, FormView):
    template_name = 'pos/open_shift.html'
    form_class = OpenCashDrawerForm
    success_url = reverse_lazy('sales:pos_terminal')

    def dispatch(self, request, *args, **kwargs):
        branch = getattr(request, 'active_branch', None)
        if not branch:
            messages.error(request, "Please select an active store branch before opening register.")
            return redirect('branches:branch_list')

        active = POSSessionService.get_active_session(request.user, branch)
        if active:
            # Preserve existing query parameters when redirecting to POS terminal
            query_string = request.META.get('QUERY_STRING', '')
            url = reverse('sales:pos_terminal')
            if query_string:
                url = f"{url}?{query_string}"
            return redirect(url)
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        branch = getattr(self.request, 'active_branch', None)
        opening_cash = form.cleaned_data['opening_cash']
        remarks = form.cleaned_data.get('remarks', '')

        try:
            POSSessionService.open_shift(
                user=self.request.user,
                branch=branch,
                opening_cash=opening_cash,
                remarks=remarks
            )
            messages.success(self.request, f"Shift opened with float Rs. {opening_cash:.2f}")
            return super().form_valid(form)
        except Exception as e:
            messages.error(self.request, str(e))
            return self.form_invalid(form)


class CloseShiftView(LoginRequiredMixin, FormView):
    template_name = 'pos/close_shift.html'
    form_class = CloseCashDrawerForm
    success_url = reverse_lazy('pos:session_list')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        branch = getattr(self.request, 'active_branch', None)
        session = POSSessionService.get_active_session(self.request.user, branch)
        context['session'] = session
        return context

    def form_valid(self, form):
        branch = getattr(self.request, 'active_branch', None)
        session = POSSessionService.get_active_session(self.request.user, branch)
        if not session:
            messages.error(self.request, "No active open shift found for your account in this branch.")
            return redirect('sales:pos_terminal')

        actual_cash = form.cleaned_data['actual_closing_cash']
        remarks = form.cleaned_data.get('remarks', '')

        try:
            POSSessionService.close_shift(
                session=session,
                actual_cash=actual_cash,
                remarks=remarks,
                verifier=self.request.user
            )
            messages.success(self.request, f"Shift {session.session_number} closed and reconciled.")
            return super().form_valid(form)
        except Exception as e:
            messages.error(self.request, str(e))
            return self.form_invalid(form)


class CashDrawerSessionListView(LoginRequiredMixin, ListView):
    model = CashDrawerSession
    template_name = 'pos/session_list.html'
    context_object_name = 'sessions'
    paginate_by = 25

    def get_queryset(self):
        qs = CashDrawerSession.objects.select_related('cashier', 'branch', 'verified_by')
        branch = getattr(self.request, 'active_branch', None)
        if branch and not self.request.user.is_superuser:
            qs = qs.filter(branch=branch)

        q = self.request.GET.get('q', '').strip()
        status_filter = self.request.GET.get('status', '').strip()
        start_date = self.request.GET.get('start_date', '').strip()
        end_date = self.request.GET.get('end_date', '').strip()

        if q:
            qs = qs.filter(
                Q(session_number__icontains=q) |
                Q(cashier__username__icontains=q) |
                Q(cashier__first_name__icontains=q) |
                Q(cashier__last_name__icontains=q) |
                Q(remarks__icontains=q)
            )
        if status_filter:
            qs = qs.filter(status=status_filter)
        if start_date:
            qs = qs.filter(opening_time__date__gte=start_date)
        if end_date:
            qs = qs.filter(opening_time__date__lte=end_date)

        return qs.order_by('-created_at')

    def render_to_response(self, context, **response_kwargs):
        if self.request.GET.get('export') == 'csv':
            return self.export_csv(self.get_queryset())
        return super().render_to_response(context, **response_kwargs)

    def export_csv(self, queryset):
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = 'attachment; filename="Cash_Drawer_Sessions.csv"'
        writer = csv.writer(response)
        writer.writerow([
            'Session Number', 'Branch', 'Cashier', 'Opening Time', 'Closing Time',
            'Opening Float (NPR)', 'Expected Cash (NPR)', 'Actual Cash (NPR)',
            'Discrepancy (NPR)', 'Total Sales (NPR)', 'Status'
        ])
        for s in queryset:
            writer.writerow([
                s.session_number, s.branch.name, s.cashier.username,
                s.opening_time.strftime('%Y-%m-%d %H:%M') if s.opening_time else '',
                s.closing_time.strftime('%Y-%m-%d %H:%M') if s.closing_time else '',
                f"{s.opening_cash:.2f}", f"{s.expected_closing_cash:.2f}",
                f"{s.actual_closing_cash:.2f}", f"{s.cash_discrepancy:.2f}",
                f"{s.total_sales_amount:.2f}", s.get_status_display()
            ])
        return response


class CashDrawerSessionDetailView(LoginRequiredMixin, DetailView):
    model = CashDrawerSession
    template_name = 'pos/session_detail.html'
    context_object_name = 'session'