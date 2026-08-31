from django.urls import path
from apps.taxation.views import (
    TaxDashboardView, SalesRegisterReportView, PurchaseRegisterReportView
)

app_name = 'taxation'

urlpatterns = [
    path('', TaxDashboardView.as_view(), name='dashboard'),
    path('sales-register/', SalesRegisterReportView.as_view(), name='sales_register'),
    path('purchase-register/', PurchaseRegisterReportView.as_view(), name='purchase_register'),
]