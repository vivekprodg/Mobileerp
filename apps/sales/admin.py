from django.contrib import admin
from django.utils.html import format_html
from apps.sales.models import (
    SalesEstimate, SalesEstimateItem,
    SalesPaymentTransaction, SalesReturn, SalesReturnItem,
    PhoneExchangeTradeIn, TradeInInspectionChecklist, TradeInLegalUndertaking
)


class SalesEstimateItemInline(admin.TabularInline):
    model = SalesEstimateItem
    extra = 0
    fields = [
        'product', 'quantity', 'unit_price', 'tax_pricing_type', 'vat_rate', 'cost_price',
        'discount_amount', 'base_taxable_amount', 'tax_amount', 'line_total',
        'imei_number', 'batch_reference', 'warranty_expiry_date'
    ]
    readonly_fields = [
        'product', 'quantity', 'unit_price', 'tax_pricing_type', 'vat_rate', 'cost_price',
        'discount_amount', 'base_taxable_amount', 'tax_amount', 'line_total',
        'imei_number', 'batch_reference', 'warranty_expiry_date'
    ]
    can_delete = False


class SalesPaymentTransactionInline(admin.TabularInline):
    model = SalesPaymentTransaction
    extra = 0
    readonly_fields = ['payment_mode', 'amount', 'transaction_ref', 'created_at']
    can_delete = False


@admin.register(SalesEstimate)
class SalesEstimateAdmin(admin.ModelAdmin):
    list_display = [
        'estimate_number', 'branch', 'recipient_display_name',
        'customer_phone_manual', 'salesperson', 'cashier',
        'bill_date_ad', 'grand_total', 'trade_in_credit_display',
        'total_cost_amount', 'total_gross_profit',
        'paid_amount', 'due_amount', 'status_badge', 'payment_status_badge'
    ]
    list_filter = [
        'status', 'payment_status', 'has_trade_in_exchange',
        'is_vat_applicable', 'branch', 'salesperson', 'cashier', 'bill_date_ad'
    ]
    search_fields = [
        'estimate_number', 'customer__name', 'customer__phone_number',
        'customer_phone_manual', 'customer_name_manual', 'customer_pan',
        'trade_in_voucher_reference', 'items__imei_number', 'items__serial_number'
    ]
    inlines = [SalesEstimateItemInline, SalesPaymentTransactionInline]
    readonly_fields = [f.name for f in SalesEstimate._meta.fields]

    fieldsets = (
        ("1. Transaction & Terminal Information", {
            'fields': (
                ('estimate_number', 'branch'),
                ('bill_date_ad', 'bill_date_bs'),
                ('cashier', 'salesperson')
            )
        }),
        ("2. Customer & Billing Details", {
            'fields': (
                'customer',
                ('customer_name_manual', 'customer_phone_manual'),
                'customer_pan'
            )
        }),
        ("3. Financial Breakdown & Calculations", {
            'fields': (
                ('subtotal', 'item_discount_total'),
                ('bill_discount_percent', 'bill_discount_amount'),
                ('has_trade_in_exchange', 'trade_in_discount_amount', 'trade_in_voucher_reference'),
                ('is_vat_applicable', 'taxable_amount', 'non_taxable_amount', 'vat_amount'),
                ('round_off', 'grand_total')
            )
        }),
        ("4. COGS & Profit Margin Realization", {
            'fields': (
                ('total_cost_amount', 'total_gross_profit'),
            )
        }),
        ("5. Payment Breakdown & Debt", {
            'fields': (
                ('paid_amount', 'due_amount', 'change_returned'),
                ('status', 'payment_status')
            )
        }),
        ("6. Security & Discount Approvals", {
            'fields': (
                'manager_override_by',
                'cancellation_reason',
                'notes'
            )
        }),
    )

    def trade_in_credit_display(self, obj):
        if obj.has_trade_in_exchange and obj.trade_in_discount_amount > 0:
            return format_html('<span style="color: #92400e; font-weight: bold;">-Rs. {}</span>', obj.trade_in_discount_amount)
        return "-"
    trade_in_credit_display.short_description = "Trade-In Credit"

    def status_badge(self, obj):
        colors = {
            'COMPLETED': '#10b981',
            'CANCELLED': '#ef4444',
            'RETURNED': '#8b5cf6',
            'PARTIALLY_RETURNED': '#f59e0b',
        }
        color = colors.get(obj.status, '#64748b')
        return format_html('<span style="color: white; background-color: {}; padding: 3px 8px; border-radius: 999px; font-weight: 600; font-size: 11px;">{}</span>', color, obj.get_status_display())
    status_badge.short_description = "Bill Status"

    def payment_status_badge(self, obj):
        colors = {
            'PAID': '#10b981',
            'PARTIAL': '#f59e0b',
            'DUE': '#ef4444',
        }
        color = colors.get(obj.payment_status, '#64748b')
        return format_html('<span style="color: white; background-color: {}; padding: 3px 8px; border-radius: 999px; font-weight: 600; font-size: 11px;">{}</span>', color, obj.get_payment_status_display())
    payment_status_badge.short_description = "Payment Status"

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


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
            return format_html('<img src="{}" style="height: 100px; border-radius: 8px; border: 1px solid #cbd5e1;" />', obj.customer_live_photo.url)
        return "(No live photo)"
    preview_customer_photo.short_description = "Customer Live Snapshot"

    def preview_id_front(self, obj):
        if obj.id_front_image:
            return format_html('<img src="{}" style="height: 100px; border-radius: 8px; border: 1px solid #cbd5e1;" />', obj.id_front_image.url)
        return "(No ID front)"
    preview_id_front.short_description = "ID Front Photo"

    def preview_id_back(self, obj):
        if obj.id_back_image:
            return format_html('<img src="{}" style="height: 100px; border-radius: 8px; border: 1px solid #cbd5e1;" />', obj.id_back_image.url)
        return "(No ID back)"
    preview_id_back.short_description = "ID Back Photo"


@admin.register(PhoneExchangeTradeIn)
class PhoneExchangeTradeInAdmin(admin.ModelAdmin):
    list_display = [
        'voucher_number', 'brand_model_display', 'imei_1',
        'customer_name_manual', 'customer_phone_manual',
        'final_trade_in_value', 'grade_badge', 'mdms_badge',
        'status_badge', 'branch', 'created_at'
    ]
    list_filter = ['status', 'recommended_condition_grade', 'mdms_status', 'branch', 'created_at']
    search_fields = [
        'voucher_number', 'brand_name', 'model_name',
        'imei_1', 'imei_2', 'customer_name_manual', 'customer_phone_manual'
    ]
    readonly_fields = ['voucher_number', 'created_at', 'updated_at']
    inlines = [TradeInInspectionChecklistInline, TradeInLegalUndertakingInline]

    fieldsets = (
        ("1. Voucher & Origin Store", {
            'fields': (
                ('voucher_number', 'branch', 'status'),
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
    brand_model_display.short_description = "Device Traded-In"

    def grade_badge(self, obj):
        colors = {
            'USED_GRADE_A': '#0ea5e9',
            'USED_GRADE_B': '#f59e0b',
            'USED_GRADE_C': '#ef4444',
        }
        color = colors.get(obj.recommended_condition_grade, '#64748b')
        return format_html('<span style="color: white; background-color: {}; padding: 2px 7px; border-radius: 999px; font-weight: bold; font-size: 10px;">{}</span>', color, obj.get_recommended_condition_grade_display())
    grade_badge.short_description = "Condition Grade"

    def mdms_badge(self, obj):
        if obj.mdms_status == 'REGISTERED_OFFICIAL':
            return format_html('<span style="color: #065f46; background-color: #ecfdf5; border: 1px solid #a7f3d0; padding: 2px 7px; border-radius: 999px; font-weight: bold; font-size: 10px;">MDMS OK</span>')
        return format_html('<span style="color: #991b1b; background-color: #fef2f2; border: 1px solid #fecaca; padding: 2px 7px; border-radius: 999px; font-weight: bold; font-size: 10px;">UNREGISTERED</span>')
    mdms_badge.short_description = "NTA MDMS"

    def status_badge(self, obj):
        colors = {
            'DRAFT': '#94a3b8',
            'VALUATED': '#3b82f6',
            'ATTACHED_TO_BILL': '#f59e0b',
            'RESTOCKED': '#10b981',
            'CANCELLED': '#ef4444',
        }
        color = colors.get(obj.status, '#64748b')
        return format_html('<span style="color: white; background-color: {}; padding: 3px 8px; border-radius: 999px; font-weight: 600; font-size: 11px;">{}</span>', color, obj.get_status_display())
    status_badge.short_description = "Voucher Status"


class SalesReturnItemInline(admin.TabularInline):
    model = SalesReturnItem
    extra = 0
    fields = [
        'product', 'return_quantity', 'refund_amount',
        'returned_imei', 'restock_to_inventory', 'is_defective', 'defect_reason'
    ]
    readonly_fields = [
        'product', 'return_quantity', 'refund_amount',
        'returned_imei', 'restock_to_inventory', 'is_defective', 'defect_reason'
    ]
    can_delete = False


@admin.register(SalesReturn)
class SalesReturnAdmin(admin.ModelAdmin):
    list_display = [
        'return_number', 'original_estimate', 'branch',
        'total_refund_amount', 'refund_mode', 'processed_by', 'created_at'
    ]
    list_filter = ['refund_mode', 'branch', 'created_at']
    search_fields = ['return_number', 'original_estimate__estimate_number', 'reason', 'items__returned_imei']
    inlines = [SalesReturnItemInline]
    readonly_fields = [f.name for f in SalesReturn._meta.fields]

    def has_add_permission(self, request):
        return False