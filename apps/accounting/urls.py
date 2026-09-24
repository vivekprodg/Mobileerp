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
- Upgraded Bank Reconciliation & Cheque Audit
- Automated Accounting Integrity Diagnostic Dashboard
- Digital Gateway Batch Settlement Engine
- Fast Account Autocomplete Search API
"""

from django.urls import path
from apps.accounting import views

app_name = 'accounting'

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
    path('cash-flow/', views.CashFlowView.as_view(), name='cash_flow'),

    # Bank Reconciliation & Cheque Audit
    path('reconciliation/', views.BankReconciliationView.as_view(), name='reconciliation'),

    # Automated Accounting Integrity Diagnostic Dashboard
    path('integrity-check/', views.AccountingIntegrityCheckView.as_view(), name='integrity_check'),

    # Digital Gateway Batch Settlement Engine
    path('gateway-settlement/', views.GatewaySettlementView.as_view(), name='gateway_settlement'),

    # High-Performance Account Search API (Autocomplete & Dynamic Lookups)
    path('api/accounts/search/', views.AccountSearchAPIView.as_view(), name='account_search_api'),
]