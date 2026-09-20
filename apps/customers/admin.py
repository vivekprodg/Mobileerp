"""
Django Admin Configuration for Customer Profiles & Udhaari Sub-Ledgers.
"""

from django.contrib import admin
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from apps.customers.models import Customer, CustomerUdhaariLedger


class CustomerUdhaariLedgerInline(admin.TabularInline):
    model = CustomerUdhaariLedger
    extra = 0
    fields = [
        'created_at', 'entry_type_badge', 'amount', 'previous_balance',
        'resulting_balance', 'payment_mode', 'reference_invoice', 'remarks', 'recorded_by'
    ]
    readonly_fields = [
        'created_at', 'entry_type_badge', 'amount', 'previous_balance',
        'resulting_balance', 'payment_mode', 'reference_invoice', 'remarks', 'recorded_by'
    ]
    can_delete = False

    def entry_type_badge(self, obj):
        if obj.entry_type == 'DEBIT':
            return format_html(
                '<span style="color: #991b1b; background-color: #fef2f2; border: 1px solid #fecaca; padding: 2px 7px; border-radius: 999px; font-weight: 700; font-size: 10px;">DEBIT (UDHAARI)</span>'
            )
        elif obj.entry_type == 'CREDIT':
            return format_html(
                '<span style="color: #065f46; background-color: #ecfdf5; border: 1px solid #a7f3d0; padding: 2px 7px; border-radius: 999px; font-weight: 700; font-size: 10px;">CREDIT (PAYMENT)</span>'
            )
        return format_html(
            '<span style="color: #1e40af; background-color: #eff6ff; padding: 2px 7px; border-radius: 999px; font-size: 10px;">ADJUSTMENT</span>'
        )
    entry_type_badge.short_description = _("Entry Nature")

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = [
        'name', 'phone_number', 'customer_type_badge', 'pan_badge',
        'current_credit_balance_display', 'credit_limit', 'total_spent_display', 'is_active'
    ]
    list_filter = ['customer_type', 'preferred_branch', 'is_active', 'created_at']
    search_fields = ['name', 'phone_number', 'pan_number', 'address', 'notes']
    list_select_related = ['preferred_branch']
    readonly_fields = ['current_credit_balance', 'total_spent', 'created_at', 'updated_at']
    inlines = [CustomerUdhaariLedgerInline]
    actions = ['recalculate_customer_balances', 'ensure_default_cash_customer_action']

    fieldsets = (
        ("1. Customer & B2B Tax Identification", {
            'fields': (
                ('name', 'customer_type'),
                ('phone_number', 'alt_phone_number'),
                ('pan_number', 'email'),
                ('address', 'preferred_branch')
            )
        }),
        ("2. Udhaari (Credit) & Commercial Terms", {
            'fields': (
                ('credit_limit', 'current_credit_balance'),
                'total_spent'
            )
        }),
        ("3. Loyalty & Special Dates", {
            'fields': (
                ('date_of_birth_bs', 'date_of_birth_ad'),
                'notes',
                'is_active',
                ('created_at', 'updated_at')
            )
        }),
    )

    def customer_type_badge(self, obj):
        colors = {
            'RETAIL': '#10b981',
            'WHOLESALE': '#3b82f6',
            'VIP': '#f59e0b',
        }
        color = colors.get(obj.customer_type, '#64748b')
        return format_html(
            '<span style="color: white; background-color: {}; padding: 2px 8px; border-radius: 999px; font-weight: 700; font-size: 10px;">{}</span>',
            color, obj.get_customer_type_display()
        )
    customer_type_badge.short_description = _("Tier Type")

    def pan_badge(self, obj):
        if obj.pan_number:
            return format_html(
                '<span class="font-monospace fw-bold" style="color: #1e40af; background-color: #dbeafe; padding: 2px 6px; border-radius: 4px; font-size: 11px;">PAN: {}</span>',
                obj.pan_number
            )
        return format_html('<span style="color: #94a3b8; font-size: 10px;">Retail (No PAN)</span>')
    pan_badge.short_description = _("Tax PAN")

    def current_credit_balance_display(self, obj):
        bal = obj.current_credit_balance or 0
        if bal > 0:
            return format_html(
                '<span style="color: #dc2626; font-weight: 700; font-family: monospace;">Rs. {:,.2f}</span>',
                bal
            )
        return format_html('<span style="color: #10b981; font-family: monospace;">Rs. 0.00</span>')
    current_credit_balance_display.short_description = _("Outstanding Udhaari")

    def total_spent_display(self, obj):
        return format_html('<span class="font-monospace">Rs. {:,.2f}</span>', obj.total_spent or 0)
    total_spent_display.short_description = _("Lifetime Spend")

    @admin.action(description=_("Recalculate credit balance from sub-ledger for selected customers"))
    def recalculate_customer_balances(self, request, queryset):
        count = 0
        for customer in queryset:
            customer.recalculate_balance_from_ledger(save=True)
            count += 1
        self.message_user(
            request,
            f"Successfully recalculated outstanding debt balance from sub-ledger history for {count} customer(s)."
        )

    @admin.action(description=_("Ensure Standard Cash Customer Profile Exists"))
    def ensure_default_cash_customer_action(self, request, queryset=None):
        cash_customer = Customer.get_or_create_default_cash_customer()
        self.message_user(
            request,
            format_html(
                "Standard retail profile verified: <strong>{}</strong> (Phone: {})",
                cash_customer.name, cash_customer.phone_number
            )
        )


@admin.register(CustomerUdhaariLedger)
class CustomerUdhaariLedgerAdmin(admin.ModelAdmin):
    list_display = [
        'customer', 'entry_type', 'amount', 'previous_balance',
        'resulting_balance', 'payment_mode', 'reference_invoice', 'recorded_by', 'created_at'
    ]
    list_filter = ['entry_type', 'payment_mode', 'branch', 'created_at']
    search_fields = ['customer__name', 'customer__phone_number', 'customer__pan_number', 'reference_invoice', 'remarks']
    list_select_related = ['customer', 'branch', 'recorded_by']
    readonly_fields = [f.name for f in CustomerUdhaariLedger._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False