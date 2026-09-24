"""
Django Admin Configuration for Suppliers, Purchase Orders, Goods Received Notes (GRN),
Commercial Purchase Returns (Debit Notes) & Supplier Udhaari Ledgers.
File Path: apps/purchases/admin.py

Synchronized with `apps/purchases/models.py`:
- Inward GRN features 5-Section Financial Summary Cards:
    1. Gross Merchandise Value (Pre-VAT Base)
    2. Dedicated 13% VAT Amount
    3. Total Overheads (Freight + Customs + Unloading)
    4. Total Landed Valuation / Inventory COGS
    5. Net Due / Supplier Debt (Total Bill - Paid Amount)
- Inline item support for dual discount types: Flat Amount (रू) vs Percentage (%).
- Automatic overhead landed cost allocation across line items upon save.
- Comprehensive date synchronization between Gregorian (AD) and Bikram Sambat (BS).
- Supplier subledger double-entry recalculation actions.
"""

from decimal import Decimal
from django.contrib import admin
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from apps.purchases.models import (
    Supplier,
    PurchaseOrder,
    PurchaseOrderItem,
    GoodsReceivedNote,
    GRNItem,
    SupplierUdhaariLedger,
    PurchaseReturn,
    PurchaseReturnItem,
)

# =============================================================================
# INLINE ADMIN MODELS
# =============================================================================
class SupplierUdhaariLedgerInline(admin.TabularInline):
    model = SupplierUdhaariLedger
    extra = 0
    can_delete = False
    readonly_fields = [
        'transaction_type', 'amount', 'previous_balance', 'resulting_balance',
        'payment_mode', 'reference_number', 'cheque_date', 'cheque_cleared',
        'recorded_by', 'created_at'
    ]

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

class GRNItemInline(admin.TabularInline):
    model = GRNItem
    extra = 0
    autocomplete_fields = ['product', 'unit_conversion']
    fields = [
        'product', 'supplier_item_code', 'purchased_quantity', 'conversion_factor',
        'base_unit_quantity', 'purchase_rate', 'gross_amount',
        'discount_type', 'discount_input_value', 'item_discount_amount', 'discount_percent',
        'line_total', 'unit_landed_cost', 'is_vat_applicable',
        'new_selling_price', 'default_mdms_status', 'scanned_imei_list'
    ]
    readonly_fields = [
        'gross_amount', 'base_unit_quantity', 'item_discount_amount',
        'discount_percent', 'line_total', 'unit_landed_cost'
    ]

class PurchaseOrderItemInline(admin.TabularInline):
    model = PurchaseOrderItem
    extra = 0
    autocomplete_fields = ['product', 'unit']
    fields = [
        'product', 'unit', 'ordered_quantity', 'received_quantity',
        'unit_cost_price', 'line_total'
    ]
    readonly_fields = ['line_total']

class PurchaseReturnItemInline(admin.TabularInline):
    model = PurchaseReturnItem
    extra = 0
    autocomplete_fields = ['product', 'unit_conversion', 'item_instance']
    fields = [
        'product', 'returned_quantity', 'conversion_factor', 'base_unit_quantity',
        'purchase_rate', 'tax_rate', 'tax_amount', 'line_total',
        'returned_imei_list', 'item_instance', 'return_reason'
    ]
    readonly_fields = ['base_unit_quantity', 'tax_amount', 'line_total']

# =============================================================================
# SUPPLIER ADMIN
# =============================================================================
@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = [
        'code', 'company_name', 'contact_person', 'phone_number',
        'supplier_type_badge', 'registration_number', 'pan_number',
        'distributor_badge', 'formatted_balance', 'status_badge', 'is_preferred'
    ]
    list_filter = [
        'status', 'supplier_type', 'is_authorized_distributor',
        'distributor_tier', 'province', 'is_preferred', 'balance_type'
    ]
    search_fields = [
        'code', 'company_name', 'contact_person', 'phone_number',
        'registration_number', 'pan_number', 'vat_number', 'email'
    ]
    readonly_fields = ['code', 'current_balance', 'created_at', 'updated_at']
    actions = ['recalculate_balances']
    inlines = [SupplierUdhaariLedgerInline]

    fieldsets = (
        (_("1. Basic Information & Registration Identity"), {
            'fields': (
                ('code', 'status'),
                ('company_name', 'supplier_type'),
                ('contact_person', 'designation'),
                ('phone_number', 'alt_phone', 'email'),
                ('registration_number', 'pan_number', 'vat_number')
            )
        }),
        (_("2. Location & Geography"), {
            'fields': (
                ('address', 'city'),
                ('province', 'country')
            )
        }),
        (_("3. Business Terms & Credit Line"), {
            'fields': (
                ('credit_period_days', 'credit_limit'),
                ('opening_balance', 'balance_type', 'current_balance')
            )
        }),
        (_("4. Structured Banking & Settlement"), {
            'fields': (
                'preferred_payment_method',
                ('bank_name', 'bank_account_number'),
                ('account_holder_name', 'bank_branch'),
                'qr_payment_details',
                'bank_details'
            )
        }),
        (_("5. Catalog Coverage & Vendor Evaluation"), {
            'fields': (
                ('product_categories_supplied', 'brands_supplied'),
                ('is_preferred', 'rating'),
                ('last_purchase_date', 'last_payment_date')
            )
        }),
        (_("6. Mobile Distributor & Warranty Policies"), {
            'fields': (
                ('is_authorized_distributor', 'distributor_tier', 'warranty_support_available'),
                'brand_authorization_details',
                'warranty_claim_contact',
                'doa_policy',
                'defective_return_policy'
            )
        }),
        (_("7. Compliance KYC Documents & System Control"), {
            'fields': (
                'kyc_document',
                'notes',
                ('created_at', 'updated_at')
            )
        }),
    )

    def supplier_type_badge(self, obj):
        return format_html(
            '<span style="background-color: #f1f5f9; border: 1px solid #cbd5e1; padding: 2px 7px; '
            'border-radius: 999px; font-size: 10px; font-weight: 600;">{}</span>',
            obj.get_supplier_type_display()
        )
    supplier_type_badge.short_description = _("Classification")

    def distributor_badge(self, obj):
        if obj.is_authorized_distributor:
            return format_html(
                '<span style="color: #0369a1; background-color: #f0f9ff; border: 1px solid #bae6fd; '
                'padding: 2px 7px; border-radius: 999px; font-weight: 700; font-size: 10px;">AUTHORIZED</span>'
            )
        return format_html('<span style="color: #64748b; font-size: 10px;">Standard</span>')
    distributor_badge.short_description = _("Distributor")

    def formatted_balance(self, obj):
        bal = obj.current_balance or Decimal('0.00')
        if bal > Decimal('0.00'):
            return format_html(
                '<span style="color: #dc2626; font-weight: 700;">Rs. {:,.2f} (Due)</span>', bal
            )
        elif bal < Decimal('0.00'):
            return format_html(
                '<span style="color: #059669; font-weight: 700;">Rs. {:,.2f} (Adv)</span>', abs(bal)
            )
        return format_html('<span style="color: #6b7280;">Rs. 0.00</span>')
    formatted_balance.short_description = _("Net Balance")
    formatted_balance.admin_order_field = 'current_balance'

    def status_badge(self, obj):
        colors = {'ACTIVE': '#10b981', 'INACTIVE': '#64748b', 'BLOCKED': '#ef4444'}
        color = colors.get(obj.status, '#64748b')
        return format_html(
            '<span style="color: white; background-color: {}; padding: 2px 8px; border-radius: 999px; '
            'font-weight: 700; font-size: 10px;">{}</span>',
            color, obj.get_status_display()
        )
    status_badge.short_description = _("Status")

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
    search_fields = ['po_number', 'supplier__company_name', 'supplier__phone_number', 'fiscal_year', 'order_date_bs', 'notes']
    readonly_fields = ['created_at', 'updated_at']
    autocomplete_fields = ['supplier', 'branch', 'created_by']
    inlines = [PurchaseOrderItemInline]

    fieldsets = (
        (_("1. Purchase Order Identification & Historical Dates"), {
            'fields': (
                ('po_number', 'status'),
                ('supplier', 'branch'),
                ('order_date', 'order_date_bs', 'fiscal_year'),
                ('expected_delivery_date', 'created_by')
            )
        }),
        (_("2. Financial Totals"), {
            'fields': (
                ('subtotal', 'tax_amount', 'total_amount'),
            )
        }),
        (_("3. Instructions & Notes"), {
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
            '<span style="color: white; background-color: {}; padding: 2px 8px; border-radius: 999px; '
            'font-weight: 700; font-size: 10px;">{}</span>',
            color, obj.get_status_display()
        )
    status_badge.short_description = _("PO Status")

@admin.register(PurchaseOrderItem)
class PurchaseOrderItemAdmin(admin.ModelAdmin):
    list_display = [
        'purchase_order', 'product', 'ordered_quantity', 'received_quantity',
        'unit_cost_price', 'line_total'
    ]
    list_filter = ['purchase_order__branch', 'purchase_order__status']
    search_fields = ['purchase_order__po_number', 'product__name', 'product__sku']
    autocomplete_fields = ['purchase_order', 'product', 'unit']
    readonly_fields = ['line_total', 'created_at', 'updated_at']

# =============================================================================
# GOODS RECEIVED NOTE (GRN) ADMIN
# =============================================================================
@admin.register(GoodsReceivedNote)
class GoodsReceivedNoteAdmin(admin.ModelAdmin):
    list_display = [
        'grn_number', 'supplier', 'branch', 'supplier_bill_no',
        'bill_date', 'bill_date_bs', 'fiscal_year',
        'status_badge', 'mdms_badge', 'is_vat_badge',
        'formatted_gross_amount', 'formatted_taxable_amount',
        'formatted_vat_amount', 'formatted_landed_cost',
        'formatted_net_total', 'formatted_due'
    ]
    list_filter = [
        'status', 'is_vat_bill', 'distributor_mdms_certified',
        'fiscal_year', 'branch', 'bill_date'
    ]
    search_fields = [
        'grn_number', 'supplier_bill_no', 'supplier_product_code',
        'mdms_tax_invoice_ref', 'supplier__company_name', 'fiscal_year', 'bill_date_bs'
    ]
    readonly_fields = [
        'gross_amount', 'total_line_discount', 'bill_discount_amount',
        'discount_amount', 'taxable_amount', 'vat_amount', 'total_landed_cost',
        'net_total_amount', 'due_amount', 'financial_summary_card',
        'created_at', 'updated_at'
    ]
    autocomplete_fields = ['supplier', 'branch', 'purchase_order', 'received_by']
    inlines = [GRNItemInline]
    actions = ['recalculate_all_grn_totals']

    fieldsets = (
        (_("1. Supplier & Inward Metadata (Historical Dates Supported)"), {
            'description': _("Permits setting historical supplier purchase dates starting from 2080 B.S."),
            'fields': (
                ('grn_number', 'status'),
                ('supplier', 'branch', 'purchase_order'),
                ('supplier_bill_no', 'supplier_product_code'),
                ('bill_date', 'bill_date_bs', 'fiscal_year')
            )
        }),
        (_("2. NTA MDMS & Customs Clearance Compliance"), {
            'description': _("Verify distributor's official Nepal Telecommunications Authority MDMS entry certification."),
            'fields': (
                ('distributor_mdms_certified', 'mdms_tax_invoice_ref'),
            )
        }),
        (_("3. Procurement Financial Valuation & Landed COGS Summary"), {
            'description': _("Live breakdown of Pre-VAT Gross, Line & Bill Discounts, 13% VAT, Overheads, and Net Payable."),
            'fields': (
                'financial_summary_card',
                ('gross_amount', 'total_line_discount'),
                ('bill_discount_type', 'bill_discount_input_value', 'bill_discount_amount'),
                ('discount_amount', 'taxable_amount'),
                ('is_vat_bill', 'vat_rate', 'vat_amount'),
                ('extra_freight_charge', 'customs_import_charge', 'other_handling_charge'),
                ('total_landed_cost', 'net_total_amount'),
                ('paid_amount', 'due_amount')
            )
        }),
        (_("4. Supplier Warranty & Support Center"), {
            'fields': (
                ('warranty_provider', 'warranty_months'),
                'authorized_service_center',
                'remarks'
            )
        }),
        (_("5. Audit & Approval"), {
            'fields': (
                ('received_by', 'estimate_disclaimer_noted'),
                ('created_at', 'updated_at')
            )
        })
    )

    # --- Visual 5-Card Financial Summary Component ---
    def financial_summary_card(self, obj):
        if not obj.pk:
            return format_html(
                '<div style="color: #64748b; font-style: italic; padding: 10px; background: #f8fafc; border: 1px dashed #cbd5e1; border-radius: 6px;">'
                'Save GRN and add items to generate the live 5-section financial valuation summary.'
                '</div>'
            )

        gross = obj.gross_amount or Decimal('0.00')
        vat = obj.vat_amount or Decimal('0.00')
        overheads = obj.overhead_total or Decimal('0.00')
        landed = obj.total_landed_cost or Decimal('0.00')
        due = obj.due_amount or Decimal('0.00')

        vat_status = "13% VAT Active" if obj.is_vat_bill else "Non-VAT (0.00)"
        vat_color = "#059669" if obj.is_vat_bill else "#64748b"

        return format_html(
            '''
            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; margin-bottom: 18px;">
                <!-- 1. Gross Merchandise Value -->
                <div style="background: #ffffff; border: 1px solid #e2e8f0; border-left: 5px solid #2563eb; border-radius: 8px; padding: 12px 14px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
                    <div style="font-size: 11px; text-transform: uppercase; color: #64748b; font-weight: 700; letter-spacing: 0.5px;">1. Gross Merchandise</div>
                    <div style="font-size: 18px; font-weight: 800; color: #1e293b; margin-top: 4px;">Rs. {:,.2f}</div>
                    <div style="font-size: 11px; color: #64748b; margin-top: 2px;">Pre-VAT Base (Before Disc)</div>
                </div>

                <!-- 2. Dedicated 13% VAT Amount -->
                <div style="background: #ffffff; border: 1px solid #e2e8f0; border-left: 5px solid {}; border-radius: 8px; padding: 12px 14px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
                    <div style="font-size: 11px; text-transform: uppercase; color: #64748b; font-weight: 700; letter-spacing: 0.5px;">2. Dedicated 13% VAT</div>
                    <div style="font-size: 18px; font-weight: 800; color: {}; margin-top: 4px;">Rs. {:,.2f}</div>
                    <div style="font-size: 11px; color: {}; font-weight: 600; margin-top: 2px;">{}</div>
                </div>

                <!-- 3. Total Overheads -->
                <div style="background: #ffffff; border: 1px solid #e2e8f0; border-left: 5px solid #d97706; border-radius: 8px; padding: 12px 14px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
                    <div style="font-size: 11px; text-transform: uppercase; color: #64748b; font-weight: 700; letter-spacing: 0.5px;">3. Total Overheads</div>
                    <div style="font-size: 18px; font-weight: 800; color: #b45309; margin-top: 4px;">Rs. {:,.2f}</div>
                    <div style="font-size: 11px; color: #64748b; margin-top: 2px;">Freight + Customs + Unloading</div>
                </div>

                <!-- 4. Total Landed Valuation -->
                <div style="background: #ffffff; border: 1px solid #e2e8f0; border-left: 5px solid #7c3aed; border-radius: 8px; padding: 12px 14px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
                    <div style="font-size: 11px; text-transform: uppercase; color: #64748b; font-weight: 700; letter-spacing: 0.5px;">4. Total Landed (COGS)</div>
                    <div style="font-size: 18px; font-weight: 800; color: #6d28d9; margin-top: 4px;">Rs. {:,.2f}</div>
                    <div style="font-size: 11px; color: #64748b; margin-top: 2px;">Pre-VAT Taxable + Overheads</div>
                </div>

                <!-- 5. Net Due / Supplier Debt -->
                <div style="background: #fef2f2; border: 1px solid #fecaca; border-left: 5px solid #dc2626; border-radius: 8px; padding: 12px 14px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
                    <div style="font-size: 11px; text-transform: uppercase; color: #991b1b; font-weight: 700; letter-spacing: 0.5px;">5. Net Due (Supplier Debt)</div>
                    <div style="font-size: 18px; font-weight: 800; color: #b91c1c; margin-top: 4px;">Rs. {:,.2f}</div>
                    <div style="font-size: 11px; color: #991b1b; margin-top: 2px;">Invoice Total - Paid Amount</div>
                </div>
            </div>
            ''',
            gross,
            vat_color,
            vat_color,
            vat,
            vat_color,
            vat_status,
            overheads,
            landed,
            due
        )
    financial_summary_card.short_description = _("Section 3: Executive Financial Summary")

    # --- Badges & Formatted Table Columns ---
    def is_vat_badge(self, obj):
        if obj.is_vat_bill:
            return format_html(
                '<span style="color: #065f46; background-color: #ecfdf5; border: 1px solid #a7f3d0; '
                'padding: 2px 7px; border-radius: 999px; font-weight: 700; font-size: 10px;">13% VAT</span>'
            )
        return format_html('<span style="color: #64748b; font-size: 10px; font-weight: 600;">PAN (0%)</span>')
    is_vat_badge.short_description = _("VAT")

    def mdms_badge(self, obj):
        if obj.distributor_mdms_certified:
            return format_html(
                '<span style="color: #0369a1; background-color: #f0f9ff; border: 1px solid #bae6fd; '
                'padding: 2px 7px; border-radius: 999px; font-weight: 700; font-size: 10px;">MDMS</span>'
            )
        return format_html(
            '<span style="color: #b91c1c; background-color: #fef2f2; border: 1px solid #fecaca; '
            'padding: 2px 7px; border-radius: 999px; font-weight: 700; font-size: 10px;">NON-MDMS</span>'
        )
    mdms_badge.short_description = _("MDMS")

    def status_badge(self, obj):
        colors = {
            'DRAFT': '#94a3b8',
            'RECEIVED': '#10b981',
            'CANCELLED': '#ef4444',
        }
        color = colors.get(obj.status, '#64748b')
        return format_html(
            '<span style="color: white; background-color: {}; padding: 2px 8px; border-radius: 999px; '
            'font-weight: 700; font-size: 10px;">{}</span>',
            color, obj.get_status_display()
        )
    status_badge.short_description = _("Status")

    def formatted_gross_amount(self, obj):
        return format_html('Rs. {:,.2f}', obj.gross_amount or Decimal('0.00'))
    formatted_gross_amount.short_description = _("Gross (Pre-VAT)")
    formatted_gross_amount.admin_order_field = 'gross_amount'

    def formatted_taxable_amount(self, obj):
        return format_html('Rs. {:,.2f}', obj.taxable_amount or Decimal('0.00'))
    formatted_taxable_amount.short_description = _("Taxable Base")
    formatted_taxable_amount.admin_order_field = 'taxable_amount'

    def formatted_vat_amount(self, obj):
        val = obj.vat_amount or Decimal('0.00')
        color = '#059669' if val > Decimal('0.00') else '#64748b'
        return format_html('<span style="color: {}; font-weight: 600;">Rs. {:,.2f}</span>', color, val)
    formatted_vat_amount.short_description = _("13% VAT")
    formatted_vat_amount.admin_order_field = 'vat_amount'

    def formatted_landed_cost(self, obj):
        return format_html('<span style="color: #7c3aed; font-weight: 700;">Rs. {:,.2f}</span>', obj.total_landed_cost or Decimal('0.00'))
    formatted_landed_cost.short_description = _("Landed (COGS)")
    formatted_landed_cost.admin_order_field = 'total_landed_cost'

    def formatted_net_total(self, obj):
        return format_html('<strong>Rs. {:,.2f}</strong>', obj.net_total_amount or Decimal('0.00'))
    formatted_net_total.short_description = _("Bill Total")
    formatted_net_total.admin_order_field = 'net_total_amount'

    def formatted_due(self, obj):
        val = obj.due_amount or Decimal('0.00')
        if val > Decimal('0.00'):
            return format_html('<span style="color: #dc2626; font-weight: 700;">Rs. {:,.2f}</span>', val)
        return format_html('<span style="color: #059669; font-weight: 600;">Rs. 0.00</span>')
    formatted_due.short_description = _("Net Due")
    formatted_due.admin_order_field = 'due_amount'

    def save_related(self, request, form, formsets, change):
        """
        Executes after inline GRN items are saved to database.
        Recalculates all line discounts, gross amounts, bill discounts, VAT,
        and accurately distributes overheads proportionally to calculate line unit landed costs.
        """
        super().save_related(request, form, formsets, change)
        form.instance.recalculate_financials(save=True)

    @admin.action(description=_("Recalculate Pre-VAT, 13% VAT, Overheads & Landed Costs for selected GRNs"))
    def recalculate_all_grn_totals(self, request, queryset):
        count = 0
        for grn in queryset:
            grn.recalculate_financials(save=True)
            count += 1
        self.message_user(
            request,
            f"Successfully recalculated Pre-VAT bases, 13% VAT, and landed costs for {count} GRN voucher(s)."
        )

@admin.register(GRNItem)
class GRNItemAdmin(admin.ModelAdmin):
    list_display = [
        'grn', 'product', 'purchased_quantity', 'base_unit_quantity',
        'purchase_rate', 'gross_amount', 'discount_type', 'item_discount_amount',
        'line_total', 'unit_landed_cost', 'default_mdms_status'
    ]
    list_filter = ['discount_type', 'is_vat_applicable', 'default_mdms_status', 'grn__branch']
    search_fields = [
        'grn__grn_number', 'grn__supplier_bill_no', 'product__name',
        'supplier_item_code', 'scanned_imei_list'
    ]
    autocomplete_fields = ['grn', 'product', 'unit_conversion']
    readonly_fields = [
        'gross_amount', 'base_unit_quantity', 'item_discount_amount',
        'discount_percent', 'line_total', 'unit_landed_cost', 'created_at', 'updated_at'
    ]

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
    search_fields = [
        'return_number', 'original_bill_reference', 'supplier__company_name',
        'fiscal_year', 'return_date_bs', 'remarks'
    ]
    readonly_fields = [
        'return_number', 'total_return_amount', 'tax_amount',
        'net_refund_amount', 'created_at', 'updated_at'
    ]
    autocomplete_fields = ['supplier', 'branch', 'original_grn', 'processed_by']
    inlines = [PurchaseReturnItemInline]

    fieldsets = (
        (_("1. Voucher Header & Routing (Historical Dates Supported)"), {
            'fields': (
                ('return_number', 'status'),
                ('supplier', 'branch'),
                ('return_date', 'return_date_bs', 'fiscal_year'),
                ('original_grn', 'original_bill_reference'),
                'refund_mode'
            )
        }),
        (_("2. Financial Settlement (Debit Note Amount)"), {
            'fields': (
                ('total_return_amount', 'tax_amount'),
                'net_refund_amount'
            )
        }),
        (_("3. Remarks & Personnel"), {
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
            '<span style="color: white; background-color: {}; padding: 2px 8px; border-radius: 999px; '
            'font-weight: 600; font-size: 10px;">{}</span>',
            color, obj.get_refund_mode_display()
        )
    refund_mode_badge.short_description = _("Refund Mode")

    def status_badge(self, obj):
        colors = {
            'DRAFT': '#94a3b8',
            'CONFIRMED': '#10b981',
            'CANCELLED': '#ef4444',
        }
        color = colors.get(obj.status, '#64748b')
        return format_html(
            '<span style="color: white; background-color: {}; padding: 2px 8px; border-radius: 999px; '
            'font-weight: 700; font-size: 10px;">{}</span>',
            color, obj.get_status_display()
        )
    status_badge.short_description = _("Status")

@admin.register(PurchaseReturnItem)
class PurchaseReturnItemAdmin(admin.ModelAdmin):
    list_display = [
        'purchase_return', 'product', 'returned_quantity',
        'purchase_rate', 'tax_rate', 'line_total', 'returned_imei_list', 'return_reason'
    ]
    list_filter = ['purchase_return__branch', 'purchase_return__supplier', 'purchase_return__return_date']
    search_fields = [
        'purchase_return__return_number', 'product__name',
        'returned_imei_list', 'return_reason'
    ]
    autocomplete_fields = ['purchase_return', 'product', 'unit_conversion', 'item_instance']
    readonly_fields = ['base_unit_quantity', 'tax_amount', 'line_total', 'created_at', 'updated_at']

# =============================================================================
# SUPPLIER UDHAARI (LEDGER) ADMIN
# =============================================================================
@admin.register(SupplierUdhaariLedger)
class SupplierUdhaariLedgerAdmin(admin.ModelAdmin):
    list_display = [
        'supplier', 'branch', 'transaction_type_badge', 'amount',
        'previous_balance', 'resulting_balance', 'payment_mode',
        'reference_number', 'cheque_date', 'cheque_cleared', 'created_at'
    ]
    list_filter = ['transaction_type', 'payment_mode', 'cheque_cleared', 'created_at', 'branch']
    search_fields = ['supplier__company_name', 'supplier__phone_number', 'reference_number', 'remarks']
    autocomplete_fields = ['supplier', 'branch', 'recorded_by']
    readonly_fields = [f.name for f in SupplierUdhaariLedger._meta.fields]

    def transaction_type_badge(self, obj):
        colors = {
            'OPENING_BALANCE': '#6366f1',
            'PURCHASE_BILL': '#dc2626',
            'PAYMENT': '#10b981',
            'PURCHASE_RETURN': '#2563eb',
            'ADJUSTMENT': '#f59e0b',
        }
        color = colors.get(obj.transaction_type, '#64748b')
        return format_html(
            '<span style="color: white; background-color: {}; padding: 2px 8px; border-radius: 999px; '
            'font-weight: 700; font-size: 10px;">{}</span>',
            color, obj.get_transaction_type_display()
        )
    transaction_type_badge.short_description = _("Tx Type")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False