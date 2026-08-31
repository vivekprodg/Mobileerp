from django.contrib import admin
from django.utils.html import format_html
from apps.purchases.models import (
    Supplier, PurchaseOrder, PurchaseOrderItem,
    GoodsReceivedNote, GRNItem, SupplierUdhaariLedger
)


class SupplierUdhaariLedgerInline(admin.TabularInline):
    model = SupplierUdhaariLedger
    extra = 0
    readonly_fields = ['transaction_type', 'amount', 'previous_balance', 'resulting_balance', 'payment_mode', 'created_at']
    can_delete = False


class GRNItemInline(admin.TabularInline):
    model = GRNItem
    extra = 0
    fields = [
        'product', 'supplier_item_code', 'purchased_quantity',
        'conversion_factor', 'base_unit_quantity', 'purchase_rate',
        'is_vat_applicable', 'vat_rate', 'default_mdms_status', 'new_selling_price', 'unit_landed_cost',
        'line_total', 'scanned_imei_list'
    ]
    readonly_fields = ['base_unit_quantity', 'unit_landed_cost', 'line_total']


class PurchaseOrderItemInline(admin.TabularInline):
    model = PurchaseOrderItem
    extra = 0


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = [
        'code', 'company_name', 'contact_person', 'phone_number',
        'supplier_type_badge', 'registration_number', 'distributor_badge',
        'current_balance', 'status_badge', 'is_preferred', 'is_active'
    ]
    list_filter = [
        'status', 'supplier_type', 'is_authorized_distributor',
        'distributor_tier', 'province', 'is_preferred', 'is_active'
    ]
    search_fields = [
        'code', 'company_name', 'contact_person', 'phone_number',
        'registration_number', 'pan_number', 'vat_number', 'email'
    ]
    readonly_fields = ['code', 'created_at', 'updated_at']
    inlines = [SupplierUdhaariLedgerInline]

    fieldsets = (
        ("1. Basic Information & Registration Identity", {
            'fields': (
                ('code', 'status'),
                ('company_name', 'supplier_type'),
                ('contact_person', 'designation'),
                ('phone_number', 'alt_phone', 'email'),
                ('registration_number', 'pan_number', 'vat_number')
            )
        }),
        ("2. Location & Geography", {
            'fields': (
                ('address', 'city'),
                ('province', 'country')
            )
        }),
        ("3. Business Terms & Credit Line", {
            'fields': (
                ('credit_period_days', 'credit_limit'),
                ('opening_balance', 'balance_type', 'current_balance')
            )
        }),
        ("4. Structured Banking & Settlement", {
            'fields': (
                'preferred_payment_method',
                ('bank_name', 'bank_account_number'),
                ('account_holder_name', 'bank_branch'),
                'qr_payment_details',
                'bank_details'
            )
        }),
        ("5. Catalog Coverage & Vendor Evaluation", {
            'fields': (
                ('product_categories_supplied', 'brands_supplied'),
                ('is_preferred', 'rating'),
                ('last_purchase_date', 'last_payment_date')
            )
        }),
        ("6. Mobile Distributor & Warranty Policies", {
            'fields': (
                ('is_authorized_distributor', 'distributor_tier', 'warranty_support_available'),
                'brand_authorization_details',
                'warranty_claim_contact',
                'doa_policy',
                'defective_return_policy'
            )
        }),
        ("7. Compliance KYC Documents & System Control", {
            'fields': (
                'kyc_document',
                'notes',
                'is_active',
                ('created_at', 'updated_at')
            )
        }),
    )

    def supplier_type_badge(self, obj):
        return format_html('<span style="background-color: #f1f5f9; border: 1px solid #cbd5e1; padding: 2px 7px; border-radius: 999px; font-size: 10px; font-weight: 600;">{}</span>', obj.get_supplier_type_display())
    supplier_type_badge.short_description = "Classification"

    def distributor_badge(self, obj):
        if obj.is_authorized_distributor:
            return format_html('<span style="color: #0369a1; background-color: #f0f9ff; border: 1px solid #bae6fd; padding: 2px 7px; border-radius: 999px; font-weight: 700; font-size: 10px;">AUTHORIZED</span>')
        return format_html('<span style="color: #64748b; font-size: 10px;">Standard</span>')
    distributor_badge.short_description = "Distributor"

    def status_badge(self, obj):
        colors = {'ACTIVE': '#10b981', 'INACTIVE': '#64748b', 'BLOCKED': '#ef4444'}
        color = colors.get(obj.status, '#64748b')
        return format_html('<span style="color: white; background-color: {}; padding: 2px 8px; border-radius: 999px; font-weight: 700; font-size: 10px;">{}</span>', color, obj.get_status_display())
    status_badge.short_description = "Status"


@admin.register(PurchaseOrder)
class PurchaseOrderAdmin(admin.ModelAdmin):
    list_display = ['po_number', 'supplier', 'branch', 'order_date', 'status', 'total_amount']
    list_filter = ['status', 'branch', 'order_date']
    search_fields = ['po_number', 'supplier__company_name']
    inlines = [PurchaseOrderItemInline]


@admin.register(GoodsReceivedNote)
class GoodsReceivedNoteAdmin(admin.ModelAdmin):
    list_display = [
        'grn_number', 'supplier', 'branch', 'supplier_bill_no',
        'mdms_badge', 'status', 'is_vat_bill', 'total_landed_cost',
        'net_total_amount', 'paid_amount', 'due_amount',
        'warranty_provider', 'bill_date'
    ]
    list_filter = ['status', 'distributor_mdms_certified', 'branch', 'is_vat_bill', 'warranty_provider', 'bill_date']
    search_fields = ['grn_number', 'supplier_bill_no', 'supplier_product_code', 'mdms_tax_invoice_ref', 'supplier__company_name']
    inlines = [GRNItemInline]

    fieldsets = (
        ("Supplier & Inward Metadata", {
            'fields': (
                ('grn_number', 'status'),
                ('supplier', 'branch', 'purchase_order'),
                ('supplier_bill_no', 'supplier_product_code'),
                ('bill_date', 'bill_date_bs')
            )
        }),
        ("NTA MDMS & Customs Clearance Compliance", {
            'description': "Verify distributor's official Nepal Telecommunications Authority MDMS entry certification.",
            'fields': (
                ('distributor_mdms_certified', 'mdms_tax_invoice_ref'),
            )
        }),
        ("Cost & Landed Calculation", {
            'fields': (
                ('gross_amount', 'discount_amount'),
                ('is_vat_bill', 'vat_amount'),
                ('extra_freight_charge', 'customs_import_charge', 'other_handling_charge'),
                ('total_landed_cost', 'net_total_amount'),
                ('paid_amount', 'due_amount')
            )
        }),
        ("Supplier Warranty & Support Center", {
            'fields': (
                ('warranty_provider', 'warranty_months'),
                'authorized_service_center',
                'remarks'
            )
        }),
        ("Audit & Approval", {
            'fields': ('received_by', 'estimate_disclaimer_noted')
        })
    )

    def mdms_badge(self, obj):
        if obj.distributor_mdms_certified:
            return format_html('<span style="color: #065f46; background-color: #ecfdf5; border: 1px solid #a7f3d0; padding: 2px 7px; border-radius: 999px; font-weight: bold; font-size: 10px;">MDMS CERTIFIED</span>')
        return format_html('<span style="color: #991b1b; background-color: #fef2f2; border: 1px solid #fecaca; padding: 2px 7px; border-radius: 999px; font-weight: bold; font-size: 10px;">NON-CERTIFIED</span>')
    mdms_badge.short_description = "NTA MDMS"


@admin.register(SupplierUdhaariLedger)
class SupplierUdhaariLedgerAdmin(admin.ModelAdmin):
    list_display = ['supplier', 'transaction_type', 'amount', 'resulting_balance', 'payment_mode', 'reference_number', 'created_at']
    list_filter = ['transaction_type', 'payment_mode', 'created_at']
    search_fields = ['supplier__company_name', 'reference_number', 'remarks']
    readonly_fields = [f.name for f in SupplierUdhaariLedger._meta.fields]

    def has_add_permission(self, request):
        return False