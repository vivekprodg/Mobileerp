from django.contrib import admin
from apps.customers.models import Customer, CustomerUdhaariLedger

class CustomerUdhaariLedgerInline(admin.TabularInline):
    model = CustomerUdhaariLedger
    extra = 0
    readonly_fields = ['entry_type', 'amount', 'previous_balance', 'resulting_balance', 'payment_mode', 'created_at']
    can_delete = False

@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ['name', 'phone_number', 'customer_type', 'current_credit_balance', 'credit_limit', 'total_spent', 'is_active']
    list_filter = ['customer_type', 'preferred_branch', 'is_active']
    search_fields = ['name', 'phone_number', 'pan_number', 'address']
    inlines = [CustomerUdhaariLedgerInline]

@admin.register(CustomerUdhaariLedger)
class CustomerUdhaariLedgerAdmin(admin.ModelAdmin):
    list_display = ['customer', 'entry_type', 'amount', 'resulting_balance', 'payment_mode', 'recorded_by', 'created_at']
    list_filter = ['entry_type', 'payment_mode', 'branch', 'created_at']
    search_fields = ['customer__name', 'customer__phone_number', 'reference_invoice', 'remarks']
    readonly_fields = [f.name for f in CustomerUdhaariLedger._meta.fields]

    def has_add_permission(self, request):
        return False