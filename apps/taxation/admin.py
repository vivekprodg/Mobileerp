from django.contrib import admin
from apps.taxation.models import TaxPeriodSummary

@admin.register(TaxPeriodSummary)
class TaxPeriodSummaryAdmin(admin.ModelAdmin):
    list_display = [
        'branch', 'nepali_year', 'nepali_month', 'total_sales_vat',
        'total_purchase_vat', 'net_vat_payable', 'is_locked'
    ]
    list_filter = ['nepali_year', 'nepali_month', 'branch', 'is_locked']
    search_fields = ['branch__name']