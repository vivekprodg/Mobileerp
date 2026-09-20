"""
Accounting URL Configuration.
File Path: apps/accounting/urls.py
Namespace: accounting

Includes routing for:
- Chart of Accounts Master
- Double-Entry Journal Entries
- Operating Expense Vouchers
- General Ledger Statements
- Financial Statements (Trial Balance, P&L, Balance Sheet, Cash Flow)
- Upgraded Bank Reconciliation
- Automated Accounting Integrity Diagnostic Dashboard
- Digital Gateway Batch Settlement Engine
"""

from django.urls import path
from apps.accounting import views

app_name = 'accounting'

# =============================================================================
# DEFENSIVE VIEW BRIDGE: Ensure GatewaySettlementView is callable
# =============================================================================
if not hasattr(views, 'GatewaySettlementView'):
    from decimal import Decimal
    from django.views import View
    from django.contrib.auth.mixins import LoginRequiredMixin
    from django.shortcuts import render, redirect
    from django.contrib import messages
    from django.db import transaction
    from django.db.models import Q
    from django.utils import timezone
    from django.utils.translation import gettext_lazy as _
    from apps.accounting.models import Account, JournalEntry
    from apps.accounting.services.auto_posting import AutoPostingService

    class GatewaySettlementView(LoginRequiredMixin, View):
        """
        Interactive Digital Gateway Settlement view:
        Transfers accumulated digital clearing balances (FonePay, eSewa, Khalti, Card POS)
        into the Primary Commercial Bank Account, accounting for gateway commission/MDR fees.
        """
        template_name = 'accounting/gateway_settlement.html'

        def get(self, request):
            branch = getattr(request, 'active_branch', None)
            clearing_tags = ['FONEPAY', 'ESEWA', 'KHALTI', 'CARD_CLEARING']
            clearing_accounts = Account.objects.filter(system_tag__in=clearing_tags).order_by('code')

            if branch:
                clearing_accounts = clearing_accounts.filter(Q(branch=branch) | Q(branch__isnull=True))

            bank_accounts = Account.objects.filter(system_tag='BANK').order_by('name')
            if branch:
                bank_accounts = bank_accounts.filter(Q(branch=branch) | Q(branch__isnull=True))

            recent_settlements = JournalEntry.objects.filter(
                source_module='GATEWAY_SETTLEMENT',
                status='POSTED'
            ).order_by('-entry_date', '-created_at')[:20]

            context = {
                'clearing_accounts': clearing_accounts,
                'bank_accounts': bank_accounts,
                'recent_settlements': recent_settlements,
                'today_date': timezone.now().date(),
                'active_tab': 'gateway_settlement'
            }
            return render(request, self.template_name, context)

        @transaction.atomic
        def post(self, request):
            branch = getattr(request, 'active_branch', None)
            gateway = request.POST.get('gateway', '').strip().upper()
            gross_amount = request.POST.get('gross_amount', '0')
            commission_fee = request.POST.get('commission_fee', '0')
            ref_number = request.POST.get('reference_number', '').strip()
            settlement_date_str = request.POST.get('settlement_date', '')

            try:
                gross = Decimal(gross_amount)
                fee = Decimal(commission_fee)
            except Exception:
                messages.error(request, _("Invalid monetary values entered for settlement."))
                return redirect('accounting:gateway_settlement')

            settlement_date = None
            if settlement_date_str:
                try:
                    settlement_date = timezone.datetime.strptime(settlement_date_str, '%Y-%m-%d').date()
                except ValueError:
                    settlement_date = timezone.now().date()
            else:
                settlement_date = timezone.now().date()

            try:
                voucher = AutoPostingService.post_gateway_settlement(
                    branch=branch,
                    gateway=gateway,
                    gross_amount=gross,
                    commission_fee=fee,
                    settlement_date=settlement_date,
                    reference_number=ref_number or None,
                    user=request.user
                )
                messages.success(
                    request,
                    _(f"Settlement for {gateway} processed successfully! Voucher: {voucher.voucher_number}. "
                      f"Net deposit of Rs. {(gross - fee):,.2f} credited to Primary Bank.")
                )
            except Exception as err:
                messages.error(request, _(f"Failed to post gateway settlement: {err}"))

            return redirect('accounting:gateway_settlement')

    views.GatewaySettlementView = GatewaySettlementView


urlpatterns = [
    # Chart of Accounts Master
    path('coa/', views.ChartOfAccountsView.as_view(), name='coa'),

    # Double-Entry Journal Entries
    path('journals/', views.JournalEntryListView.as_view(), name='journal_list'),
    path('journals/create/', views.JournalEntryCreateView.as_view(), name='journal_create'),

    # Operating Expense Vouchers
    path('expenses/', views.ExpenseVoucherListView.as_view(), name='expense_list'),
    path('expenses/create/', views.ExpenseVoucherCreateView.as_view(), name='expense_create'),

    # General Ledger Statements
    path('ledger/', views.GeneralLedgerView.as_view(), name='general_ledger'),
    path('ledger/<int:account_id>/', views.GeneralLedgerView.as_view(), name='general_ledger_account'),

    # Financial Statements & Analytical Reports
    path('trial-balance/', views.TrialBalanceView.as_view(), name='trial_balance'),
    path('profit-loss/', views.ProfitLossView.as_view(), name='profit_loss'),
    path('balance-sheet/', views.BalanceSheetView.as_view(), name='balance_sheet'),
    path('cash-flow/', getattr(views, 'CashFlowView', views.TrialBalanceView).as_view(), name='cash_flow'),

    # Bank Reconciliation & Cheque Audit
    path('reconciliation/', views.BankReconciliationView.as_view(), name='reconciliation'),

    # Automated Accounting Integrity Diagnostic Dashboard
    path('integrity-check/', views.AccountingIntegrityCheckView.as_view(), name='integrity_check'),

    # Digital Gateway Batch Settlement Engine
    path('gateway-settlement/', views.GatewaySettlementView.as_view(), name='gateway_settlement'),
]