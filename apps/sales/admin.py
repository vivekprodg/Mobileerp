"""
Django Admin Configuration for Sales, Invoices, POS Checkouts,
Trade-In Exchanges, and Itemized Sales Returns.
"""

from django.contrib import admin
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from apps.sales.models import (
    SalesEstimate, SalesEstimateItem,
    SalesPaymentTransaction, SalesReturn, SalesReturnItem,
    PhoneExchangeTradeIn, TradeInInspectionChecklist, TradeInLegalUndertaking
)

# =============================================================================
# 1. SALES ESTIMATE LINE ITEMS & PAYMENT INLINES
# =============================================================================
class SalesEstimateItemInline(admin.TabularInline):
    model = SalesEstimateItem
    extra = 0
    fields = [
        'product', 'quantity', 'official_unit_price', 'unit_price',
        'price_override_amount', 'discount_type_badge', 'discount_input_value',
        'item_discount_amount', 'effective_discount_percent',
        'allocated_bill_discount_amount', 'discount_amount',
        'tax_pricing_type', 'vat_rate', 'cost_price',
        'base_taxable_amount', 'tax_amount', 'line_total',
        'imei_display', 'batch_reference', 'warranty_expiry_date'
    ]
    readonly_fields = [
        'product', 'quantity', 'official_unit_price', 'unit_price',
        'price_override_amount', 'discount_type_badge', 'discount_input_value',
        'item_discount_amount', 'effective_discount_percent',
        'allocated_bill_discount_amount', 'discount_amount',
        'tax_pricing_type', 'vat_rate', 'cost_price',
        'base_taxable_amount', 'tax_amount', 'line_total',
        'imei_display', 'batch_reference', 'warranty_expiry_date'
    ]
    can_delete = False

    def discount_type_badge(self, obj):
        if obj.discount_type in ['AMOUNT', 'FIXED']:
            return format_html(
                '<span style="color: #1e40af; background-color: #dbeafe; font-weight: 700; padding: 2px 6px; border-radius: 4px; font-size: 10px;">AMOUNT</span>'
            )
        elif obj.discount_type == 'PERCENTAGE':
            return format_html(
                '<span style="color: #92400e; background-color: #fef3c7; font-weight: 700; padding: 2px 6px; border-radius: 4px; font-size: 10px;">PERCENT</span>'
            )
        return format_html('<span style="color: #64748b; font-size: 10px;">NONE</span>')
    discount_type_badge.short_description = _("Disc Type")

    def imei_display(self, obj):
        if obj.imei_number:
            sec_tag = f"<br><small style='color:#64748b;'>SIM 2: {obj.secondary_imei}</small>" if obj.secondary_imei else ""
            return format_html(
                '<span class="font-monospace" style="color: #0f172a; font-weight: 700;">{}</span>{}',
                obj.imei_number, format_html(sec_tag)
            )
        return format_html('<span style="color: #94a3b8; font-style: italic; font-size: 10px;">(Non-Serialized / Historical)</span>')
    imei_display.short_description = _("IMEI / Serial")

class SalesPaymentTransactionInline(admin.TabularInline):
    model = SalesPaymentTransaction
    extra = 0
    fields = ['payment_mode_badge', 'amount', 'transaction_ref', 'notes', 'created_at']
    readonly_fields = ['payment_mode_badge', 'amount', 'transaction_ref', 'notes', 'created_at']
    can_delete = False

    def payment_mode_badge(self, obj):
        return format_html(
            '<span class="badge bg-light text-dark border font-monospace fs-2xs">{}</span>',
            obj.get_payment_mode_display()
        )
    payment_mode_badge.short_description = _("Payment Mode")

# =============================================================================
# 2. SALES ESTIMATE / INVOICE ADMIN (HISTORICAL AUDIT PROTECTED)
# =============================================================================
@admin.register(SalesEstimate)
class SalesEstimateAdmin(admin.ModelAdmin):
    list_display = [
        'estimate_number', 'branch', 'recipient_display_name',
        'customer_phone_display', 'customer_pan_display',
        'bill_date_ad', 'bill_date_bs', 'fiscal_year',
        'taxable_amount_display', 'vat_amount_display', 'grand_total',
        'merchandise_discount_display', 'trade_in_credit_display',
        'paid_amount', 'due_amount', 'status_badge', 'payment_status_badge'
    ]
    list_filter = [
        'status', 'payment_status', 'fiscal_year', 'bill_discount_type',
        'has_trade_in_exchange', 'is_vat_applicable', 'branch',
        'salesperson', 'cashier', 'bill_date_ad'
    ]
    search_fields = [
        'estimate_number', 'customer__name', 'customer__phone_number',
        'customer_phone_manual', 'customer_name_manual', 'customer_pan',
        'fiscal_year', 'trade_in_voucher_reference', 'items__imei_number',
        'items__secondary_imei', 'items__serial_number', 'discount_reason'
    ]
    inlines = [SalesEstimateItemInline, SalesPaymentTransactionInline]
    readonly_fields = [f.name for f in SalesEstimate._meta.fields]

    fieldsets = (
        ("1. Transaction & Historical Date Information", {
            'fields': (
                ('estimate_number', 'branch'),
                ('bill_date_ad', 'bill_date_bs', 'fiscal_year'),
                ('cashier', 'salesperson')
            )
        }),
        ("2. Customer & B2B Tax Identification", {
            'fields': (
                'customer',
                ('customer_name_manual', 'customer_phone_manual'),
                'customer_pan'
            )
        }),
        ("3. Financial Breakdown & 13% Tax Breakdown (Annex-5 Aligned)", {
            'description': "Three-way tax split separating Taxable Base, 13% Output VAT, and Gross Total.",
            'fields': (
                ('subtotal', 'item_discount_total'),
                ('bill_discount_type', 'bill_discount_input_value'),
                ('bill_discount_percent', 'bill_discount_amount'),
                ('has_trade_in_exchange', 'trade_in_discount_amount', 'trade_in_voucher_reference'),
                ('is_vat_applicable', 'taxable_amount', 'non_taxable_amount', 'vat_amount'),
                ('round_off', 'grand_total')
            )
        }),
        ("4. Acquisition Cost (COGS) & Margins", {
            'fields': (
                ('total_cost_amount', 'total_gross_profit'),
            )
        }),
        ("5. Payments, Collections & Udhaari (Debt)", {
            'fields': (
                ('paid_amount', 'due_amount', 'change_returned'),
                ('status', 'payment_status')
            )
        }),
        ("6. Commercial Approvals & Justifications", {
            'fields': (
                ('manager_override_by', 'discount_approved_at'),
                'discount_reason',
                'cancellation_reason',
                'notes'
            )
        }),
    )

    def customer_phone_display(self, obj):
        phone = obj.customer_phone_manual or (obj.customer.phone_number if obj.customer else None)
        if phone:
            return format_html('<span class="font-monospace">{}</span>', phone)
        return format_html('<span style="color: #94a3b8;">-</span>')
    customer_phone_display.short_description = _("Customer Phone")

    def customer_pan_display(self, obj):
        if obj.customer_pan:
            return format_html('<span class="font-monospace fw-bold text-primary">{}</span>', obj.customer_pan)
        return format_html('<span style="color: #94a3b8;">-</span>')
    customer_pan_display.short_description = _("Customer PAN")

    def taxable_amount_display(self, obj):
        return format_html('Rs. {:,.2f}', obj.taxable_amount)
    taxable_amount_display.short_description = _("Taxable Base")

    def vat_amount_display(self, obj):
        if obj.vat_amount > 0:
            return format_html('<span style="color: #1e40af; font-weight: 700;">Rs. {:,.2f}</span>', obj.vat_amount)
        return format_html('<span style="color: #94a3b8;">Rs. 0.00</span>')
    vat_amount_display.short_description = _("13% VAT")

    def merchandise_discount_display(self, obj):
        tot_disc = obj.total_sales_discount
        if tot_disc > 0:
            type_tag = f" ({obj.get_bill_discount_type_display()})" if obj.bill_discount_amount > 0 else ""
            return format_html(
                '<span style="color: #dc2626; font-weight: 700; font-family: monospace;">-Rs. {:,.2f}{}</span>',
                tot_disc, type_tag
            )
        return format_html('<span style="color: #94a3b8;">-</span>')
    merchandise_discount_display.short_description = _("Sales Discount")

    def trade_in_credit_display(self, obj):
        if obj.has_trade_in_exchange and obj.trade_in_discount_amount > 0:
            return format_html(
                '<span style="color: #92400e; font-weight: 700; font-family: monospace;">-Rs. {:,.2f}</span>',
                obj.trade_in_discount_amount
            )
        return format_html('<span style="color: #94a3b8;">-</span>')
    trade_in_credit_display.short_description = _("Trade-In Credit")

    def status_badge(self, obj):
        colors = {
            'COMPLETED': '#10b981',
            'CANCELLED': '#ef4444',
            'RETURNED': '#8b5cf6',
            'PARTIALLY_RETURNED': '#f59e0b',
            'DRAFT': '#64748b',
        }
        color = colors.get(obj.status, '#64748b')
        return format_html(
            '<span style="color: white; background-color: {}; padding: 3px 8px; border-radius: 999px; font-weight: 600; font-size: 11px;">{}</span>',
            color, obj.get_status_display()
        )
    status_badge.short_description = _("Bill Status")

    def payment_status_badge(self, obj):
        colors = {
            'PAID': '#10b981',
            'PARTIAL': '#f59e0b',
            'DUE': '#ef4444',
        }
        color = colors.get(obj.payment_status, '#64748b')
        return format_html(
            '<span style="color: white; background-color: {}; padding: 3px 8px; border-radius: 999px; font-weight: 600; font-size: 11px;">{}</span>',
            color, obj.get_payment_status_display()
        )
    payment_status_badge.short_description = _("Payment Status")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

# =============================================================================
# 3. TRADE-IN & BUY-BACK ADMIN
# =============================================================================
class TradeInInspectionChecklistInline(admin.StackedInline):
    model = TradeInInspectionChecklist
    extra = 0
    can_delete = False

class TradeInLegalUndertakingInline(admin.StackedInline):
    model = TradeInLegalUndertaking
    extra = 0
    can_delete = False
    fields = [
        'customer_full_name', 'customer_father_or_spouse_name',
        ('id_type', 'id_number'),
        ('id_issued_district', 'id_issued_date_bs'),
        'permanent_address', 'current_address',
        'preview_customer_photo', 'preview_id_front', 'preview_id_back',
        'declaration_accepted', 'verified_by'
    ]
    readonly_fields = ['preview_customer_photo', 'preview_id_front', 'preview_id_back']

    def preview_customer_photo(self, obj):
        if obj.customer_live_photo:
            return format_html(
                '<img src="{}" style="height: 100px; border-radius: 8px; border: 1px solid #cbd5e1; object-fit: cover;" />',
                obj.customer_live_photo.url
            )
        return "(No live photo)"
    preview_customer_photo.short_description = _("Customer Live Snapshot")

    def preview_id_front(self, obj):
        if obj.id_front_image:
            return format_html(
                '<img src="{}" style="height: 100px; border-radius: 8px; border: 1px solid #cbd5e1; object-fit: cover;" />',
                obj.id_front_image.url
            )
        return "(No ID front)"
    preview_id_front.short_description = _("ID Front Photo")

    def preview_id_back(self, obj):
        if obj.id_back_image:
            return format_html(
                '<img src="{}" style="height: 100px; border-radius: 8px; border: 1px solid #cbd5e1; object-fit: cover;" />',
                obj.id_back_image.url
            )
        return "(No ID back)"
    preview_id_back.short_description = _("ID Back Photo")

@admin.register(PhoneExchangeTradeIn)
class PhoneExchangeTradeInAdmin(admin.ModelAdmin):
    list_display = [
        'voucher_number', 'brand_model_display', 'imei_1',
        'customer_name_manual', 'customer_phone_manual',
        'final_trade_in_value', 'grade_badge', 'mdms_badge',
        'status_badge', 'fiscal_year', 'branch', 'created_at'
    ]
    list_filter = ['status', 'fiscal_year', 'recommended_condition_grade', 'mdms_status', 'branch', 'intake_date_ad', 'created_at']
    search_fields = [
        'voucher_number', 'brand_name', 'model_name',
        'imei_1', 'imei_2', 'fiscal_year', 'customer_name_manual', 'customer_phone_manual'
    ]
    readonly_fields = ['voucher_number', 'created_at', 'updated_at']
    inlines = [TradeInInspectionChecklistInline, TradeInLegalUndertakingInline]

    fieldsets = (
        ("1. Voucher & Origin Store", {
            'fields': (
                ('voucher_number', 'branch', 'status'),
                ('intake_date_ad', 'intake_date_bs', 'fiscal_year'),
                ('cashier', 'inspector_technician'),
                ('customer', 'customer_name_manual', 'customer_phone_manual'),
                'pos_estimate'
            )
        }),
        ("2. Old Traded-In Device Identification", {
            'fields': (
                ('brand_name', 'model_name'),
                ('ram_capacity', 'storage_capacity', 'color_variant'),
                ('imei_1', 'imei_2', 'serial_number'),
                'mdms_status'
            )
        }),
        ("3. Valuation Calculation & Pre-Owned Grade", {
            'fields': (
                ('market_base_value', 'total_deductions'),
                ('shop_margin_deduction', 'final_trade_in_value'),
                'recommended_condition_grade',
                ('restocked_product', 'restocked_item_instance'),
                'evaluation_notes'
            )
        }),
    )

    def brand_model_display(self, obj):
        return f"{obj.brand_name} {obj.model_name} ({obj.storage_capacity})"
    brand_model_display.short_description = _("Device Traded-In")

    def grade_badge(self, obj):
        colors = {
            'USED_GRADE_A': '#0ea5e9',
            'USED_GRADE_B': '#f59e0b',
            'USED_GRADE_C': '#ef4444',
        }
        color = colors.get(obj.recommended_condition_grade, '#64748b')
        return format_html(
            '<span style="color: white; background-color: {}; padding: 2px 7px; border-radius: 999px; font-weight: bold; font-size: 10px;">{}</span>',
            color, obj.get_recommended_condition_grade_display()
        )
    grade_badge.short_description = _("Condition Grade")

    def mdms_badge(self, obj):
        if obj.mdms_status == 'REGISTERED_OFFICIAL':
            return format_html(
                '<span style="color: #065f46; background-color: #ecfdf5; border: 1px solid #a7f3d0; padding: 2px 7px; border-radius: 999px; font-weight: bold; font-size: 10px;">MDMS OK</span>'
            )
        return format_html(
            '<span style="color: #991b1b; background-color: #fef2f2; border: 1px solid #fecaca; padding: 2px 7px; border-radius: 999px; font-weight: bold; font-size: 10px;">UNREGISTERED</span>'
        )
    mdms_badge.short_description = _("NTA MDMS")

    def status_badge(self, obj):
        colors = {
            'DRAFT': '#94a3b8',
            'VALUATED': '#3b82f6',
            'ATTACHED_TO_BILL': '#f59e0b',
            'RESTOCKED': '#10b981',
            'CANCELLED': '#ef4444',
        }
        color = colors.get(obj.status, '#64748b')
        return format_html(
            '<span style="color: white; background-color: {}; padding: 3px 8px; border-radius: 999px; font-weight: 600; font-size: 11px;">{}</span>',
            color, obj.get_status_display()
        )
    status_badge.short_description = _("Voucher Status")

# =============================================================================
# 4. SALES RETURN & DEFECTIVE ITEMS ADMIN
# =============================================================================
class SalesReturnItemInline(admin.TabularInline):
    model = SalesReturnItem
    extra = 0
    fields = [
        'product', 'return_quantity', 'base_unit_quantity', 'refund_amount',
        'discount_type', 'discount_input_value', 'item_discount_amount',
        'effective_discount_percent', 'returned_imei', 'restock_to_inventory',
        'is_defective', 'defect_reason'
    ]
    readonly_fields = [
        'product', 'return_quantity', 'base_unit_quantity', 'refund_amount',
        'discount_type', 'discount_input_value', 'item_discount_amount',
        'effective_discount_percent', 'returned_imei', 'restock_to_inventory',
        'is_defective', 'defect_reason'
    ]
    can_delete = False

@admin.register(SalesReturn)
class SalesReturnAdmin(admin.ModelAdmin):
    list_display = [
        'return_number', 'original_estimate', 'branch',
        'return_date_ad', 'return_date_bs', 'fiscal_year',
        'total_refund_amount', 'refund_mode_badge', 'processed_by', 'created_at'
    ]
    list_filter = ['refund_mode', 'fiscal_year', 'branch', 'return_date_ad', 'created_at']
    search_fields = [
        'return_number', 'original_estimate__estimate_number',
        'customer__name', 'customer__phone_number', 'fiscal_year',
        'reason', 'items__returned_imei'
    ]
    inlines = [SalesReturnItemInline]
    readonly_fields = [f.name for f in SalesReturn._meta.fields]

    fieldsets = (
        ("Voucher Header & Routing", {
            'fields': (
                ('return_number', 'branch'),
                ('original_estimate', 'customer'),
                ('return_date_ad', 'return_date_bs', 'fiscal_year'),
                ('refund_mode', 'processed_by')
            )
        }),
        ("Financials & Reasons", {
            'fields': (
                'total_refund_amount',
                'reason',
                'technician_notes'
            )
        }),
    )

    def refund_mode_badge(self, obj):
        colors = {
            'CASH': '#10b981',
            'STORE_CREDIT': '#3b82f6',
            'EXCHANGE_ADJUST': '#8b5cf6',
        }
        color = colors.get(obj.refund_mode, '#64748b')
        return format_html(
            '<span style="color: white; background-color: {}; padding: 2px 7px; border-radius: 999px; font-weight: 600; font-size: 10px;">{}</span>',
            color, obj.get_refund_mode_display()
        )
    refund_mode_badge.short_description = _("Refund Mode")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False