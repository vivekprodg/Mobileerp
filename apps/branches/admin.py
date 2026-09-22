from django.contrib import admin
from django.contrib import messages
from django.utils.html import format_html
from apps.branches.models import Branch, StockTransferRequest, StockTransferItem, BranchDocumentSequence

class StockTransferItemInline(admin.TabularInline):
    model = StockTransferItem
    extra = 1
    fields = ['product', 'quantity', 'scanned_imei_or_serial', 'item_instance', 'notes']
    autocomplete_fields = ['product', 'item_instance']

@admin.register(Branch)
class BranchAdmin(admin.ModelAdmin):
    """
    Branch administration interface aligned with the cached model architecture.
    Automatically clears memory caches on save or delete to ensure immediate consistency.
    """
    list_display = [
        'logo_thumbnail', 'code', 'display_company_name_column', 
        'name', 'city', 'district', 'phone_number', 'is_main_branch', 
        'invoice_prefix', 'is_active'
    ]
    list_filter = ['is_main_branch', 'province', 'is_active']
    search_fields = ['code', 'name', 'company_name', 'city', 'district', 'phone_number']
    fieldsets = (
        ("1. White-Label Branding & Company Identity", {
            'description': "Configure company/shop business name and upload optional store logo.",
            'fields': (
                ('company_name', 'company_name_np'),
                ('logo', 'logo_preview_display')
            )
        }),
        ("2. Outlet Details & Manager", {
            'fields': (
                ('code', 'name', 'name_np'),
                ('is_main_branch', 'manager', 'is_active')
            )
        }),
        ("3. Location & Contacts (Nepal)", {
            'fields': (
                ('address', 'city'),
                ('district', 'province'),
                ('phone_number', 'email')
            )
        }),
        ("4. Slip / Estimation Prefix & Notes", {
            'fields': (
                'invoice_prefix',
                'header_contact_info',
                'footer_estimate_note'
            )
        }),
    )
    readonly_fields = ['logo_preview_display']

    def logo_thumbnail(self, obj):
        # Uses cached obj.logo_url without executing SQL queries per row
        url = obj.logo_url
        if url:
            return format_html(
                '<img src="{}" style="height: 32px; width: 32px; object-fit: contain; border-radius: 6px; border: 1px solid #cbd5e1; background: #fff;" />',
                url
            )
        return format_html('<span style="color: #94a3b8; font-size: 11px;">(No Logo)</span>')
    logo_thumbnail.short_description = "Logo"

    def display_company_name_column(self, obj):
        return obj.display_company_name
    display_company_name_column.short_description = "Company Name"

    def logo_preview_display(self, obj):
        url = obj.logo_url
        if url:
            return format_html(
                '<img src="{}" style="max-height: 80px; max-width: 240px; object-fit: contain; border-radius: 8px; border: 1px solid #cbd5e1; padding: 4px; background: #fff;" /><br><small style="color: #64748b;">Current Active Logo</small>',
                url
            )
        return format_html('<span style="color: #94a3b8;">No logo uploaded (System uses clean text company name).</span>')
    logo_preview_display.short_description = "Current Logo Preview"

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        obj.invalidate_branch_caches()

    def delete_model(self, request, obj):
        obj.invalidate_branch_caches()
        super().delete_model(request, obj)

    def has_delete_permission(self, request, obj=None):
        """Prevent accidental deletion of the central main branch or the sole remaining branch."""
        if obj is not None:
            if obj.is_main_branch or Branch.objects.count() <= 1:
                return False
        return super().has_delete_permission(request, obj)

    def delete_queryset(self, request, queryset):
        """Prevent bulk-deletion from wiping out the main branch or all branches."""
        if queryset.filter(is_main_branch=True).exists() or queryset.count() >= Branch.objects.count():
            messages.error(request, "Cannot delete the Primary Main Branch or wipe out all branches.")
            queryset = queryset.exclude(is_main_branch=True)
            if not queryset.exists():
                return
        for b in queryset:
            b.invalidate_branch_caches()
        super().delete_queryset(request, queryset)

@admin.register(BranchDocumentSequence)
class BranchDocumentSequenceAdmin(admin.ModelAdmin):
    list_display = ['branch', 'document_type', 'prefix', 'last_number', 'padding_digits', 'updated_at']
    list_filter = ['document_type', 'branch']
    search_fields = ['branch__code', 'branch__name', 'prefix']
    readonly_fields = ['created_at', 'updated_at']

@admin.register(StockTransferRequest)
class StockTransferRequestAdmin(admin.ModelAdmin):
    list_display = [
        'transfer_no', 'source_branch', 'destination_branch',
        'status', 'transfer_date', 'requested_by', 'dispatched_by', 'received_by'
    ]
    list_filter = ['status', 'source_branch', 'destination_branch', 'transfer_date']
    search_fields = ['transfer_no', 'notes']
    readonly_fields = ['transfer_no', 'created_at', 'updated_at', 'dispatched_date', 'received_date']
    inlines = [StockTransferItemInline]

@admin.register(StockTransferItem)
class StockTransferItemAdmin(admin.ModelAdmin):
    list_display = ['transfer_request', 'product', 'quantity', 'scanned_imei_or_serial', 'item_instance']
    list_filter = ['transfer_request__status', 'transfer_request__source_branch', 'transfer_request__destination_branch']
    search_fields = ['transfer_request__transfer_no', 'product__name', 'scanned_imei_or_serial']
    autocomplete_fields = ['product', 'item_instance']