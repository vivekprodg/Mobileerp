"""
Django Admin Configuration for Suppliers, Purchase Orders, Goods Received Notes (GRN) & Purchase Returns.
File Path: D:\Mobile Shop\Inventory\apps\purchases\admin.py

Synchronized with `apps/purchases/models.py`:
- Inward GRNs, Purchase Orders, and Purchase Returns prominently feature `bill_date`, `bill_date_bs`, and `fiscal_year`.
- Protects `current_balance` by enforcing read-only status in SupplierAdmin.
- Provides bulk balance recalculation admin action strictly driven by `SupplierUdhaariLedger`.
- Complete search, filter, and inline management for line items, scanned IMEIs, and supplier ledgers.
"""

from django.contrib import admin
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from apps.purchases.models import (
    Supplier, PurchaseOrder, PurchaseOrderItem,
    GoodsReceivedNote, GRNItem, SupplierUdhaariLedger,
    PurchaseReturn, PurchaseReturnItem
)


# =============================================================================
# INLINE ADMIN MODELS
# =============================================================================

class SupplierUdhaariLedgerInline(admin.TabularInline):
    model = SupplierUdhaariLedger
    extra = 0
    readonly_fields = [
        'transaction_type', 'amount', 'previous_balance', 'resulting_balance',
        'payment_mode', 'reference_number', 'cheque_date', 'recorded_by', 'created_at'
    ]
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class GRNItemInline(admin.TabularInline):
    model = GRNItem
    extra = 0
    fields = [
        'product', 'supplier_item_code', 'purchased_quantity',
        'conversion_factor', 'base_unit_quantity', 'purchase_rate',
        'is_vat_applicable', 'vat_rate', 'default_mdms_status',
        'new_selling_price', 'unit_landed_cost', 'line_total', 'scanned_imei_list'
    ]
    readonly_fields = ['base_unit_quantity', 'unit_landed_cost', 'line_total']


class PurchaseOrderItemInline(admin.TabularInline):
    model = PurchaseOrderItem
    extra = 0
    fields = ['product', 'unit', 'ordered_quantity', 'received_quantity', 'unit_cost_price', 'line_total']
    readonly_fields = ['line_total']


class PurchaseReturnItemInline(admin.TabularInline):
    model = PurchaseReturnItem
    extra = 0
    fields = [
        'product', 'returned_quantity', 'conversion_factor', 'base_unit_quantity',
        'purchase_rate', 'tax_rate', 'tax_amount', 'line_total',
        'returned_imei_list', 'return_reason'
    ]
    readonly_fields = ['base_unit_quantity', 'tax_amount', 'line_total']


# =============================================================================
# SUPPLIER ADMIN
# =============================================================================

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
    readonly_fields = ['code', 'current_balance', 'created_at', 'updated_at']
    actions = ['recalculate_balances']
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
        return format_html(
            '<span style="background-color: #f1f5f9; border: 1px solid #cbd5e1; padding: 2px 7px; border-radius: 999px; font-size: 10px; font-weight: 600;">{}</span>',
            obj.get_supplier_type_display()
        )
    supplier_type_badge.short_description = "Classification"

    def distributor_badge(self, obj):
        if obj.is_authorized_distributor:
            return format_html(
                '<span style="color: #0369a1; background-color: #f0f9ff; border: 1px solid #bae6fd; padding: 2px 7px; border-radius: 999px; font-weight: 700; font-size: 10px;">AUTHORIZED</span>'
            )
        return format_html('<span style="color: #64748b; font-size: 10px;">Standard</span>')
    distributor_badge.short_description = "Distributor"

    def status_badge(self, obj):
        colors = {'ACTIVE': '#10b981', 'INACTIVE': '#64748b', 'BLOCKED': '#ef4444'}
        color = colors.get(obj.status, '#64748b')
        return format_html(
            '<span style="color: white; background-color: {}; padding: 2px 8px; border-radius: 999px; font-weight: 700; font-size: 10px;">{}</span>',
            color, obj.get_status_display()
        )
    status_badge.short_description = "Status"

    @admin.action(description=_("Recalculate outstanding balance from ledger entries"))
    def recalculate_balances(self, request, queryset):
        recalculated_count = 0
        for supplier in queryset:
            supplier.recalculate_balance_from_ledger(save=True)
            recalculated_count += 1
        self.message_user(
            request,
            f"Successfully recalculated outstanding debt balance for {recalculated_count} supplier(s) from their ledger history."
        )


# =============================================================================
# PURCHASE ORDER ADMIN
# =============================================================================

@admin.register(PurchaseOrder)
class PurchaseOrderAdmin(admin.ModelAdmin):
    list_display = [
        'po_number', 'supplier', 'branch', 'order_date', 'order_date_bs',
        'fiscal_year', 'status_badge', 'subtotal', 'total_amount', 'created_by'
    ]
    list_filter = ['status', 'fiscal_year', 'branch', 'order_date']
    search_fields = ['po_number', 'supplier__company_name', 'fiscal_year', 'order_date_bs', 'notes']
    readonly_fields = ['created_at', 'updated_at']
    inlines = [PurchaseOrderItemInline]

    fieldsets = (
        ("1. Purchase Order Identification & Historical Dates", {
            'fields': (
                ('po_number', 'status'),
                ('supplier', 'branch'),
                ('order_date', 'order_date_bs', 'fiscal_year'),
                ('expected_delivery_date', 'created_by')
            )
        }),
        ("2. Financial Totals", {
            'fields': (
                ('subtotal', 'tax_amount', 'total_amount'),
            )
        }),
        ("3. Instructions & Notes", {
            'fields': (
                'notes',
                ('created_at', 'updated_at')
            )
        }),
    )

    def status_badge(self, obj):
        colors = {
            'DRAFT': '#94a3b8',
            'ISSUED': '#3b82f6',
            'PARTIALLY_RECEIVED': '#f59e0b',
            'COMPLETED': '#10b981',
            'CANCELLED': '#ef4444',
        }
        color = colors.get(obj.status, '#64748b')
        return format_html(
            '<span style="color: white; background-color: {}; padding: 2px 8px; border-radius: 999px; font-weight: 700; font-size: 10px;">{}</span>',
            color, obj.get_status_display()
        )
    status_badge.short_description = "PO Status"


# =============================================================================
# GOODS RECEIVED NOTE (GRN) ADMIN
# =============================================================================

@admin.register(GoodsReceivedNote)
class GoodsReceivedNoteAdmin(admin.ModelAdmin):
    list_display = [
        'grn_number', 'supplier', 'branch', 'supplier_bill_no',
        'bill_date', 'bill_date_bs', 'fiscal_year',
        'mdms_badge', 'status_badge', 'is_vat_bill', 'total_landed_cost',
        'net_total_amount', 'paid_amount', 'due_amount'
    ]
    list_filter = [
        'status', 'distributor_mdms_certified', 'fiscal_year', 'branch',
        'is_vat_bill', 'warranty_provider', 'bill_date'
    ]
    search_fields = [
        'grn_number', 'supplier_bill_no', 'supplier_product_code',
        'mdms_tax_invoice_ref', 'supplier__company_name', 'fiscal_year', 'bill_date_bs'
    ]
    readonly_fields = ['created_at', 'updated_at']
    inlines = [GRNItemInline]

    fieldsets = (
        ("1. Supplier & Inward Metadata (Historical Dates Supported)", {
            'description': "Permits setting historical supplier purchase dates starting from 2080 B.S.",
            'fields': (
                ('grn_number', 'status'),
                ('supplier', 'branch', 'purchase_order'),
                ('supplier_bill_no', 'supplier_product_code'),
                ('bill_date', 'bill_date_bs', 'fiscal_year')
            )
        }),
        ("2. NTA MDMS & Customs Clearance Compliance", {
            'description': "Verify distributor's official Nepal Telecommunications Authority MDMS entry certification.",
            'fields': (
                ('distributor_mdms_certified', 'mdms_tax_invoice_ref'),
            )
        }),
        ("3. Cost & Landed Calculation", {
            'fields': (
                ('gross_amount', 'discount_amount'),
                ('is_vat_bill', 'vat_amount'),
                ('extra_freight_charge', 'customs_import_charge', 'other_handling_charge'),
                ('total_landed_cost', 'net_total_amount'),
                ('paid_amount', 'due_amount')
            )
        }),
        ("4. Supplier Warranty & Support Center", {
            'fields': (
                ('warranty_provider', 'warranty_months'),
                'authorized_service_center',
                'remarks'
            )
        }),
        ("5. Audit & Approval", {
            'fields': (
                ('received_by', 'estimate_disclaimer_noted'),
                ('created_at', 'updated_at')
            )
        })
    )

    def mdms_badge(self, obj):
        if obj.distributor_mdms_certified:
            return format_html(
                '<span style="color: #065f46; background-color: #ecfdf5; border: 1px solid #a7f3d0; padding: 2px 7px; border-radius: 999px; font-weight: bold; font-size: 10px;">MDMS CERTIFIED</span>'
            )
        return format_html(
            '<span style="color: #991b1b; background-color: #fef2f2; border: 1px solid #fecaca; padding: 2px 7px; border-radius: 999px; font-weight: bold; font-size: 10px;">NON-CERTIFIED</span>'
        )
    mdms_badge.short_description = "NTA MDMS"

    def status_badge(self, obj):
        colors = {
            'DRAFT': '#94a3b8',
            'RECEIVED': '#10b981',
            'CANCELLED': '#ef4444',
        }
        color = colors.get(obj.status, '#64748b')
        return format_html(
            '<span style="color: white; background-color: {}; padding: 2px 8px; border-radius: 999px; font-weight: 700; font-size: 10px;">{}</span>',
            color, obj.get_status_display()
        )
    status_badge.short_description = "GRN Status"


# =============================================================================
# PURCHASE RETURN (DEBIT NOTE) ADMIN
# =============================================================================

@admin.register(PurchaseReturn)
class PurchaseReturnAdmin(admin.ModelAdmin):
    list_display = [
        'return_number', 'supplier', 'branch', 'return_date',
        'return_date_bs', 'fiscal_year', 'refund_mode_badge',
        'status_badge', 'net_refund_amount', 'processed_by'
    ]
    list_filter = ['status', 'refund_mode', 'fiscal_year', 'branch', 'return_date', 'supplier']
    search_fields = ['return_number', 'original_bill_reference', 'supplier__company_name', 'fiscal_year', 'return_date_bs', 'remarks']
    readonly_fields = ['return_number', 'total_return_amount', 'tax_amount', 'net_refund_amount', 'created_at', 'updated_at']
    inlines = [PurchaseReturnItemInline]

    fieldsets = (
        ("1. Voucher Header & Routing (Historical Dates Supported)", {
            'fields': (
                ('return_number', 'status'),
                ('supplier', 'branch'),
                ('return_date', 'return_date_bs', 'fiscal_year'),
                ('original_grn', 'original_bill_reference'),
                'refund_mode'
            )
        }),
        ("2. Financial Settlement (Debit Note Amount)", {
            'fields': (
                ('total_return_amount', 'tax_amount'),
                'net_refund_amount'
            )
        }),
        ("3. Remarks & Personnel", {
            'fields': (
                'remarks',
                'processed_by',
                ('created_at', 'updated_at')
            )
        }),
    )

    def refund_mode_badge(self, obj):
        colors = {
            'DEDUCT_FROM_BALANCE': '#1e40af',
            'CASH_REFUND': '#065f46',
            'REPLACEMENT': '#92400e',
        }
        color = colors.get(obj.refund_mode, '#475569')
        return format_html(
            '<span style="color: white; background-color: {}; padding: 2px 8px; border-radius: 999px; font-weight: 600; font-size: 10px;">{}</span>',
            color, obj.get_refund_mode_display()
        )
    refund_mode_badge.short_description = "Refund Mode"

    def status_badge(self, obj):
        colors = {
            'DRAFT': '#94a3b8',
            'CONFIRMED': '#10b981',
            'CANCELLED': '#ef4444',
        }
        color = colors.get(obj.status, '#64748b')
        return format_html(
            '<span style="color: white; background-color: {}; padding: 2px 8px; border-radius: 999px; font-weight: 700; font-size: 10px;">{}</span>',
            color, obj.get_status_display()
        )
    status_badge.short_description = "Status"


# =============================================================================
# PURCHASE RETURN ITEM & SUPPLIER UDHAARI LEDGER ADMINS
# =============================================================================

@admin.register(PurchaseReturnItem)
class PurchaseReturnItemAdmin(admin.ModelAdmin):
    list_display = [
        'purchase_return', 'product', 'returned_quantity',
        'purchase_rate', 'tax_rate', 'line_total', 'returned_imei_list', 'return_reason'
    ]
    list_filter = ['purchase_return__branch', 'purchase_return__supplier', 'purchase_return__return_date']
    search_fields = ['purchase_return__return_number', 'product__name', 'returned_imei_list', 'return_reason']
    readonly_fields = ['base_unit_quantity', 'tax_amount', 'line_total', 'created_at', 'updated_at']


@admin.register(SupplierUdhaariLedger)
class SupplierUdhaariLedgerAdmin(admin.ModelAdmin):
    list_display = [
        'supplier', 'transaction_type', 'amount', 'previous_balance',
        'resulting_balance', 'payment_mode', 'reference_number', 'cheque_date', 'created_at'
    ]
    list_filter = ['transaction_type', 'payment_mode', 'created_at']
    search_fields = ['supplier__company_name', 'reference_number', 'remarks']
    readonly_fields = [f.name for f in SupplierUdhaariLedger._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False