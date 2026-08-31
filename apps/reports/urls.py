from django.urls import path
from apps.reports.views import (
    DailySalesReportView, InventoryValuationReportView,
    CustomerUdhaariReportView, ExportReportCSVView
)

app_name = 'reports'

urlpatterns = [
    path('daily-sales/', DailySalesReportView.as_view(), name='daily_sales'),
    path('inventory-valuation/', InventoryValuationReportView.as_view(), name='inventory_valuation'),
    path('customer-udhaari/', CustomerUdhaariReportView.as_view(), name='customer_udhaari'),
    path('export/<str:report_type>/', ExportReportCSVView.as_view(), name='export_csv'),
]