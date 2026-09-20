from django.contrib import admin
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from apps.pos.models import CashDrawerSession, POSHoldCart

@admin.register(CashDrawerSession)
class CashDrawerSessionAdmin(admin.ModelAdmin):
    list_display = [
        'session_number', 'branch', 'cashier', 'opening_time',
        'opening_cash', 'total_sales_amount', 'actual_closing_cash',
        'cash_discrepancy_badge', 'status_badge'
    ]
    list_filter = ['status', 'branch', 'opening_time']
    search_fields = ['session_number', 'cashier__username', 'remarks']
    readonly_fields = [
        'session_number', 'opening_time', 'closing_time',
        'expected_closing_cash', 'cash_discrepancy'
    ]

    fieldsets = (
        ("Shift Identification", {
            'fields': (
                ('session_number', 'status'),
                ('branch', 'cashier'),
                ('opening_time', 'closing_time')
            )
        }),
        ("Cash Reconciliation", {
            'fields': (
                ('opening_cash', 'expected_closing_cash'),
                ('actual_closing_cash', 'cash_discrepancy'),
                'verified_by'
            )
        }),
        ("Sales Aggregations", {
            'fields': (
                ('total_sales_amount', 'total_cash_sales'),
                ('total_digital_sales', 'total_credit_sales'),
                'total_returns_amount'
            )
        }),
        ("Audit Notes", {
            'fields': ('remarks',)
        })
    )

    def cash_discrepancy_badge(self, obj):
        if obj.cash_discrepancy < 0:
            return format_html(
                '<span style="color: #dc2626; font-weight: 700; font-family: monospace;">-Rs. {} (Shortage)</span>',
                abs(obj.cash_discrepancy)
            )
        elif obj.cash_discrepancy > 0:
            return format_html(
                '<span style="color: #16a34a; font-weight: 700; font-family: monospace;">+Rs. {} (Excess)</span>',
                obj.cash_discrepancy
            )
        return format_html('<span style="color: #64748b; font-family: monospace;">Rs. 0.00 (Balanced)</span>')
    cash_discrepancy_badge.short_description = "Discrepancy"

    def status_badge(self, obj):
        colors = {'OPEN': '#10b981', 'CLOSED': '#64748b'}
        color = colors.get(obj.status, '#64748b')
        return format_html(
            '<span style="color: white; background-color: {}; padding: 3px 8px; border-radius: 999px; font-weight: 600; font-size: 11px;">{}</span>',
            color, obj.get_status_display()
        )
    status_badge.short_description = "Status"

@admin.register(POSHoldCart)
class POSHoldCartAdmin(admin.ModelAdmin):
    list_display = [
        'hold_reference', 'branch', 'cashier', 'customer_name',
        'subtotal', 'discount_display', 'items_count_display', 'created_at'
    ]
    list_filter = ['branch', 'bill_discount_type', 'created_at']
    search_fields = ['hold_reference', 'customer_name', 'customer_phone', 'notes']
    readonly_fields = ['hold_reference', 'created_at', 'updated_at']

    fieldsets = (
        ("Held Cart Header", {
            'fields': (
                ('hold_reference', 'branch', 'cashier'),
                ('customer', 'customer_name', 'customer_phone'),
                'notes'
            )
        }),
        ("Financial Totals & Discounts", {
            'fields': (
                'subtotal',
                ('bill_discount_type', 'bill_discount_value', 'discount_percent')
            )
        }),
        ("Cart Snapshot Payload", {
            'fields': ('cart_payload',)
        }),
        ("Timestamps", {
            'fields': (('created_at', 'updated_at'),),
            'classes': ('collapse',)
        })
    )

    def discount_display(self, obj):
        if obj.bill_discount_value > 0:
            if obj.bill_discount_type in ['AMOUNT', 'FIXED']:
                eff = f" (Eff. {obj.discount_percent:.1f}%)" if obj.discount_percent > 0 else ""
                return format_html(
                    '<span style="color: #dc2626; font-weight: bold; font-family: monospace;">Rs. {}{}</span>',
                    obj.bill_discount_value, eff
                )
            else:
                return format_html(
                    '<span style="color: #dc2626; font-weight: bold; font-family: monospace;">{}%</span>',
                    obj.bill_discount_value
                )
        return format_html('<span style="color: #94a3b8;">No Discount</span>')
    discount_display.short_description = "Bill Discount"

    def items_count_display(self, obj):
        items = obj.cart_payload.get('items', []) if isinstance(obj.cart_payload, dict) else []
        count = len(items)
        return format_html('<span class="badge bg-light text-dark border">{} Item(s)</span>', count)
    items_count_display.short_description = "Items In Cart"