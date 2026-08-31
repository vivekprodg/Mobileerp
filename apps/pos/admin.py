from django.contrib import admin
from apps.pos.models import CashDrawerSession, POSHoldCart

@admin.register(CashDrawerSession)
class CashDrawerSessionAdmin(admin.ModelAdmin):
    list_display = [
        'session_number', 'branch', 'cashier', 'opening_time',
        'opening_cash', 'total_sales_amount', 'actual_closing_cash',
        'cash_discrepancy', 'status'
    ]
    list_filter = ['status', 'branch', 'opening_time']
    search_fields = ['session_number', 'cashier__username', 'remarks']
    readonly_fields = ['session_number', 'opening_time', 'expected_closing_cash', 'cash_discrepancy']

@admin.register(POSHoldCart)
class POSHoldCartAdmin(admin.ModelAdmin):
    list_display = ['hold_reference', 'branch', 'cashier', 'customer_name', 'subtotal', 'created_at']
    list_filter = ['branch', 'created_at']
    search_fields = ['hold_reference', 'customer_name', 'customer_phone']