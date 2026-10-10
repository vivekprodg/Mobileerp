"""
Taxation, VAT Registers & Drill-Down Routing Configuration.

Namespace: taxation

Routes Hierarchy:
1. Executive VAT Overview:
   - path('', TaxDashboardView)                                                 → 'dashboard'
2. Daily VAT & Net Settlement Ledger:
   - path('daily-report/', DailyVatReportView)                                  → 'daily_vat_report'
3. Movement Books / Statutory Registers:
   - path('sales-register/', SalesRegisterReportView)                           → 'sales_register'
   - path('purchase-register/', PurchaseRegisterReportView)                     → 'purchase_register'
4. 6-Month Consecutive VAT Report & Period Comparison:
   - path('six-month-report/', SixMonthVatReportView)                           → 'six_month_report'
5. VAT-to-General-Ledger Audit Reconciliation Report:
   - path('reconciliation/', VatGlReconciliationView)                           → 'reconciliation'
   - path('vat-gl-reconciliation/', VatGlReconciliationView)                   → 'vat_gl_reconciliation'
6. Hierarchical Multi-Tier Drill-Down Endpoints:
   - path('drilldown/', VatDrilldownAPIView)                                    → 'vat_drilldown_api'
   - path('drilldown/invoice/<str:pk_or_number>/', InvoiceVatDrilldownView)     → 'invoice_vat_drilldown'
   - path('drilldown/grn/<str:pk_or_number>/', GRNVatDrilldownView)             → 'grn_vat_drilldown'
   - path('drilldown/sales-return/<str:pk_or_number>/', SalesReturnVatDrilldownView)   → 'sales_return_vat_drilldown'
   - path('drilldown/purchase-return/<str:pk_or_number>/', PurchaseReturnVatDrilldownView) → 'purchase_return_vat_drilldown'
"""

from django.urls import path
from apps.taxation.views import (
    TaxDashboardView,
    SalesRegisterReportView,
    PurchaseRegisterReportView,
    DailyVatReportView,
    SixMonthVatReportView,
    VatGlReconciliationView,
    VatDrilldownAPIView,
    InvoiceVatDrilldownView,
    GRNVatDrilldownView,
    SalesReturnVatDrilldownView,
    PurchaseReturnVatDrilldownView,
)

app_name = 'taxation'

urlpatterns = [
    # 1. Executive VAT Overview & KPI Dashboard
    path('', TaxDashboardView.as_view(), name='dashboard'),

    # 2. Chronological Day-Wise VAT & Assessment Ledger (Tier 1 Drill-Down)
    path('daily-report/', DailyVatReportView.as_view(), name='daily_vat_report'),

    # 3. Movement Books (Annex 5 Sales Book & Annex 7 Purchase Book)
    path('sales-register/', SalesRegisterReportView.as_view(), name='sales_register'),
    path('purchase-register/', PurchaseRegisterReportView.as_view(), name='purchase_register'),

    # 4. 6-Month Consecutive VAT Summary & Period Comparison
    path('six-month-report/', SixMonthVatReportView.as_view(), name='six_month_report'),

    # 5. VAT-to-General-Ledger Audit Reconciliation
    path('reconciliation/', VatGlReconciliationView.as_view(), name='reconciliation'),
    path('vat-gl-reconciliation/', VatGlReconciliationView.as_view(), name='vat_gl_reconciliation'),

    # 6. Multi-Tier Drill-Down Endpoints (Tier 2 Document ↔ Tier 3 Item ↔ Tier 4 GL Entry)
    # Universal AJAX endpoint for Day, Invoice, GRN, Sales Return, or Purchase Return queries
    path('drilldown/', VatDrilldownAPIView.as_view(), name='vat_drilldown_api'),

    # Direct modal / standalone inspection routes for specific documents
    path(
        'drilldown/invoice/<str:pk_or_number>/',
        InvoiceVatDrilldownView.as_view(),
        name='invoice_vat_drilldown',
    ),
    path(
        'drilldown/grn/<str:pk_or_number>/',
        GRNVatDrilldownView.as_view(),
        name='grn_vat_drilldown',
    ),
    path(
        'drilldown/sales-return/<str:pk_or_number>/',
        SalesReturnVatDrilldownView.as_view(),
        name='sales_return_vat_drilldown',
    ),
    path(
        'drilldown/purchase-return/<str:pk_or_number>/',
        PurchaseReturnVatDrilldownView.as_view(),
        name='purchase_return_vat_drilldown',
    ),
]