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
    path('', TaxDashboardView.as_view(), name='dashboard'),
    path('daily-report/', DailyVatReportView.as_view(), name='daily_vat_report'),
    path('sales-register/', SalesRegisterReportView.as_view(), name='sales_register'),
    path('purchase-register/', PurchaseRegisterReportView.as_view(), name='purchase_register'),
    path('six-month-report/', SixMonthVatReportView.as_view(), name='six_month_report'),
    path('reconciliation/', VatGlReconciliationView.as_view(), name='reconciliation'),
    path('vat-gl-reconciliation/', VatGlReconciliationView.as_view(), name='vat_gl_reconciliation'),
    path('drilldown/', VatDrilldownAPIView.as_view(), name='vat_drilldown_api'),
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