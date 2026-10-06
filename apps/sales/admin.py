"""
Django Admin Configuration for Sales, Invoices, POS Checkouts,
Trade-In Exchanges, and Itemized Sales Returns.

Aligned with `apps/sales/models.py`:
- Standard Retail Turnover Accounting: Grand Total reflects merchandise gross sales + tax;
  Trade-In buy-back allowances operate strictly as tender settlement offsets (barter payment).
- Bidirectional Bikram Sambat (BS) and Gregorian (AD) Date Synchronization with auto-propagation.
- Police-Compliant Customer Ownership Undertaking Administration with image evidence previews.
- Safe Counter and Backend Override Capabilities:
  1. Superusers and Store Managers can safely modify bill dates, customer details,
     and remarks without financial total corruption.
  2. Status transitions to CANCELLED in the admin panel are intercepted to execute
     atomic inventory restock, phone IMEI release, customer debt reversal, and GL balancing.
- High-Performance Database Scoping using raw_id_fields for scalable customer and product lookups.
"""

from decimal import Decimal
from django.contrib import admin, messages
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from apps.sales.models import (
    SalesEstimate,
    SalesEstimateItem,
    SalesPaymentTransaction,
    SalesReturn,
    SalesReturnItem,
    PhoneExchangeTradeIn,
    TradeInInspectionChecklist,
    TradeInLegalUndertaking,
)
from apps.sales.services import SalesPOSService
from apps.core.models import AuditLog

# =============================================================================
# 1. SALES ESTIMATE LINE ITEMS & PAYMENT INLINES
# =============================================================================
class SalesEstimateItemInline(admin.TabularInline):
    model = SalesEstimateItem
    extra = 0
    raw_id_fields = ['product', 'unit_conversion', 'item_instance']
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
        return format_html('<span style="color: #94a3b8; font-style: italic; font-size: 10px;">(Non-Serialized / Accessory)</span>')
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
# 2. SALES ESTIMATE / INVOICE ADMIN (TURNOVER ACCOUNTING & OVERRIDES)
# =============================================================================
@admin.register(SalesEstimate)
class SalesEstimateAdmin(admin.ModelAdmin):
    list_display = [
        'estimate_number', 'bill_type_badge', 'branch', 'recipient_display_name',
        'customer_phone_display', 'customer_pan_display',
        'bill_date_ad', 'bill_date_bs', 'fiscal_year',
        'taxable_amount_display', 'vat_amount_display', 'grand_total_display',
        'merchandise_discount_display', 'trade_in_tender_display',
        'net_payable_display', 'paid_amount', 'due_amount', 'status_badge', 'payment_status_badge'
    ]
    list_filter = [
        'bill_type', 'status', 'payment_status', 'fiscal_year', 'bill_discount_type',
        'has_trade_in_exchange', 'is_vat_applicable', 'branch',
        'salesperson', 'cashier', 'bill_date_ad'
    ]
    search_fields = [
        'estimate_number', 'customer__name', 'customer__phone_number',
        'customer_phone_manual', 'customer_name_manual', 'customer_pan',
        'bill_date_bs', 'fiscal_year', 'trade_in_voucher_reference',
        'items__imei_number', 'items__secondary_imei', 'items__serial_number', 'discount_reason'
    ]
    raw_id_fields = ['branch', 'customer', 'cashier', 'salesperson', 'manager_override_by']
    inlines = [SalesEstimateItemInline, SalesPaymentTransactionInline]

    fieldsets = (
        (_("1. Transaction & Historical Date Information"), {
            'description': _(
                "Updating either Bill Date (BS) or Bill Date (AD) will bidirectionally "
                "recalculate the corresponding date, Nepali Fiscal Year, and synchronize downstream "
                "General Ledger entries, sold phone warranty dates, and debt ledgers automatically upon saving."
            ),
            'fields': (
                ('estimate_number', 'branch', 'bill_type'),
                ('bill_date_ad', 'bill_date_bs', 'fiscal_year'),
                ('cashier', 'salesperson')
            )
        }),
        (_("2. Customer & B2B Tax Identification"), {
            'fields': (
                'customer',
                ('customer_name_manual', 'customer_phone_manual'),
                'customer_pan'
            )
        }),
        (_("3. Merchandise Turnover & 13% Tax Breakdown"), {
            'description': _(
                "Turnover Accounting: Grand Total strictly represents the gross merchandise sales value + applicable taxes. "
                "Trade-In allowances act exclusively as a tender settlement offset (barter payment)."
            ),
            'fields': (
                ('subtotal', 'item_discount_total'),
                ('bill_discount_type', 'bill_discount_input_value'),
                ('bill_discount_percent', 'bill_discount_amount'),
                ('is_vat_applicable', 'taxable_amount', 'non_taxable_amount', 'vat_amount'),
                ('round_off', 'grand_total'),
            )
        }),
        (_("4. Trade-In Barter Tender & Settlement Offsets"), {
            'fields': (
                ('has_trade_in_exchange', 'trade_in_discount_amount', 'trade_in_voucher_reference'),
                ('effective_trade_in_tender_display', 'excess_trade_in_credit_display', 'net_customer_payable_display'),
            )
        }),
        (_("5. Acquisition Cost (COGS) & Margins"), {
            'fields': (
                ('total_cost_amount', 'total_gross_profit'),
            )
        }),
        (_("6. Payments, Collections & Udhaari (Debt)"), {
            'fields': (
                ('paid_amount', 'due_amount', 'change_returned'),
                ('status', 'payment_status')
            )
        }),
        (_("7. Commercial Approvals & Justifications"), {
            'fields': (
                ('manager_override_by', 'discount_approved_at'),
                'discount_reason',
                'cancellation_reason',
                'notes'
            )
        }),
        (_("8. Audit Metadata & System Timestamps"), {
            'classes': ('collapse',),
            'fields': (
                ('created_at', 'updated_at'),
            )
        }),
    )

    def get_readonly_fields(self, request, obj=None):
        """
        Locks accounting calculation totals to prevent accidental financial corruption,
        while empowering Superusers and Store Owners to modify historical dates, customer metadata,
        notes, or status even on cancelled bills.
        """
        permanent_readonly = [
            'estimate_number', 'branch', 'fiscal_year',
            'subtotal', 'item_discount_total', 'bill_discount_amount',
            'taxable_amount', 'non_taxable_amount', 'vat_amount',
            'round_off', 'grand_total', 'total_cost_amount', 'total_gross_profit',
            'paid_amount', 'due_amount', 'change_returned',
            'cashier', 'salesperson', 'manager_override_by', 'discount_approved_at',
            'has_trade_in_exchange', 'trade_in_discount_amount', 'trade_in_voucher_reference',
            'net_customer_payable_display', 'effective_trade_in_tender_display',
            'excess_trade_in_credit_display', 'created_at', 'updated_at'
        ]

        is_privileged = bool(
            request.user.is_superuser or
            getattr(request.user, 'role', '') in ['OWNER', 'MANAGER']
        )

        if is_privileged:
            # Privileged staff can edit: bill_type, dates (AD/BS), customer fields, status, reasons, and notes
            return permanent_readonly

        # Non-privileged staff have full read-only view
        return [f.name for f in SalesEstimate._meta.fields] + [
            'net_customer_payable_display', 'effective_trade_in_tender_display', 'excess_trade_in_credit_display'
        ]

    def save_model(self, request, obj, form, change):
        """
        Backend Save Interceptor:
        1. If an admin changes a bill's status to 'CANCELLED', intercept and route through
           SalesPOSService.cancel_sales_estimate() to ensure inventory stock, phone IMEIs,
           customer debt, and General Ledger double-entry journals are fully reversed.
        2. If updating historical dates or metadata, obj.save() bidirectionally updates BS/AD
           dates and auto-propagates them down to ItemInstances, component warranties, and journals.
        """
        old_status = form.initial.get('status') if change else None
        new_status = obj.status

        # Case 1: Status changed to CANCELLED via Admin Panel
        if change and old_status != 'CANCELLED' and new_status == 'CANCELLED':
            reason = (
                form.cleaned_data.get('cancellation_reason') or
                obj.cancellation_reason or
                f"Cancelled via Django Admin Panel by {request.user.username}"
            ).strip()

            try:
                SalesPOSService.cancel_sales_estimate(
                    estimate=obj,
                    reason=reason,
                    user=request.user
                )
                self.message_user(
                    request,
                    f"Bill '{obj.estimate_number}' was successfully cancelled. "
                    f"Sold merchandise stock, phone IMEIs, customer debt, and accounting journals have been safely reversed.",
                    level=messages.SUCCESS
                )
                return
            except Exception as err:
                self.message_user(
                    request,
                    f"Cancellation failed for bill '{obj.estimate_number}': {str(err)}",
                    level=messages.ERROR
                )
                obj.status = old_status
                return

        # Case 2: Standard Admin Save (Metadata or Historical Date Correction)
        super().save_model(request, obj, form, change)

        # Forensic Audit Trail for Admin Panel Changes
        if change and form.changed_data:
            AuditLog.objects.create(
                user=request.user,
                branch=obj.branch,
                action_type='UPDATE',
                module='Admin_SalesEstimate_Override',
                object_repr=obj.estimate_number,
                details={
                    'changed_fields': list(form.changed_data),
                    'status': obj.status,
                    'bill_date_ad': str(obj.bill_date_ad),
                    'bill_date_bs': obj.bill_date_bs,
                    'fiscal_year': obj.fiscal_year,
                    'notes': obj.notes,
                }
            )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return bool(
            request.user.is_superuser or
            getattr(request.user, 'role', '') in ['OWNER', 'MANAGER']
        )

    # --- Formatted Column Helpers ---
    def bill_type_badge(self, obj):
        if obj.is_official_vat_bill:
            return format_html(
                '<span style="color: #1e40af; background-color: #dbeafe; border: 1px solid #bfdbfe; padding: 2px 7px; border-radius: 999px; font-weight: 700; font-size: 10px;">VAT BILL</span>'
            )
        return format_html(
            '<span style="color: #6b21a8; background-color: #f3e8ff; border: 1px solid #e9d5ff; padding: 2px 7px; border-radius: 999px; font-weight: 700; font-size: 10px;">ESTIMATE</span>'
        )
    bill_type_badge.short_description = _("Doc Type")

    def customer_phone_display(self, obj):
        phone = obj.customer_phone_manual or (obj.customer.phone_number if obj.customer else None)
        if phone:
            return format_html('<span class="font-monospace">{}</span>', phone)
        return format_html('<span style="color: #94a3b8;">-</span>')
    customer_phone_display.short_description = _("Customer Phone")

    def customer_pan_display(self, obj):
        if obj.customer_pan:
            return format_html('<span class="font-monospace fw-bold text-primary">{}</span>', obj.customer_pan)
        elif obj.customer and obj.customer.pan_number:
            return format_html('<span class="font-monospace text-muted">{}</span>', obj.customer.pan_number)
        return format_html('<span style="color: #94a3b8;">-</span>')
    customer_pan_display.short_description = _("Customer PAN")

    def taxable_amount_display(self, obj):
        return format_html('Rs. {:,.2f}', obj.taxable_amount)
    taxable_amount_display.short_description = _("Taxable Base")

    def vat_amount_display(self, obj):
        if obj.vat_amount > Decimal('0.00'):
            return format_html('<span style="color: #1e40af; font-weight: 700;">Rs. {:,.2f}</span>', obj.vat_amount)
        return format_html('<span style="color: #94a3b8;">Rs. 0.00</span>')
    vat_amount_display.short_description = _("13% VAT")

    def grand_total_display(self, obj):
        return format_html('<span class="font-monospace fw-bold text-primary">Rs. {:,.2f}</span>', obj.grand_total)
    grand_total_display.short_description = _("Grand Total (Turnover)")

    def merchandise_discount_display(self, obj):
        tot_disc = obj.total_sales_discount
        if tot_disc > Decimal('0.00'):
            type_tag = f" ({obj.get_bill_discount_type_display()})" if obj.bill_discount_amount > Decimal('0.00') else ""
            return format_html(
                '<span style="color: #dc2626; font-weight: 700; font-family: monospace;">-Rs. {:,.2f}{}</span>',
                tot_disc, type_tag
            )
        return format_html('<span style="color: #94a3b8;">-</span>')
    merchandise_discount_display.short_description = _("Sales Discount")

    def trade_in_tender_display(self, obj):
        if obj.has_trade_in_exchange and obj.trade_in_discount_amount > Decimal('0.00'):
            return format_html(
                '<span style="color: #92400e; font-weight: 700; font-family: monospace;">-Rs. {:,.2f}</span>',
                obj.trade_in_discount_amount
            )
        return format_html('<span style="color: #94a3b8;">-</span>')
    trade_in_tender_display.short_description = _("Trade-In Tender")

    def net_payable_display(self, obj):
        return format_html('<span class="font-monospace fw-bold text-dark">Rs. {:,.2f}</span>', obj.net_customer_payable)
    net_payable_display.short_description = _("Net Payable")

    def effective_trade_in_tender_display(self, obj):
        return format_html('Rs. {:,.2f}', obj.effective_trade_in_tender)
    effective_trade_in_tender_display.short_description = _("Effective Tender Consumed")

    def excess_trade_in_credit_display(self, obj):
        return format_html('Rs. {:,.2f}', obj.excess_trade_in_credit)
    excess_trade_in_credit_display.short_description = _("Surplus Trade-In Refunded/Credited")

    def net_customer_payable_display(self, obj):
        return format_html('<strong class="font-monospace text-primary">Rs. {:,.2f}</strong>', obj.net_customer_payable)
    net_customer_payable_display.short_description = _("Net Balance Payable")

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
    raw_id_fields = ['verified_by']
    fields = [
        'customer_full_name', 'customer_father_or_spouse_name',
        ('id_type', 'id_number'),
        ('id_issued_district', 'id_issued_date_bs'),
        'permanent_address', 'current_address',
        'preview_customer_photo', 'preview_id_front', 'preview_id_back',
        'declaration_text', 'declaration_accepted', 'verified_by'
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
        'status_badge', 'fiscal_year', 'branch', 'intake_date_ad', 'intake_date_bs'
    ]
    list_filter = [
        'status', 'fiscal_year', 'recommended_condition_grade',
        'mdms_status', 'branch', 'intake_date_ad', 'created_at'
    ]
    search_fields = [
        'voucher_number', 'brand_name', 'model_name',
        'imei_1', 'imei_2', 'intake_date_bs', 'fiscal_year',
        'customer_name_manual', 'customer_phone_manual'
    ]
    readonly_fields = ['voucher_number', 'fiscal_year', 'created_at', 'updated_at']
    raw_id_fields = [
        'branch', 'customer', 'cashier', 'inspector_technician',
        'pos_estimate', 'restocked_product', 'restocked_item_instance'
    ]
    inlines = [TradeInInspectionChecklistInline, TradeInLegalUndertakingInline]

    fieldsets = (
        (_("1. Voucher & Origin Store (Historical Dates Supported)"), {
            'fields': (
                ('voucher_number', 'branch', 'status'),
                ('intake_date_ad', 'intake_date_bs', 'fiscal_year'),
                ('cashier', 'inspector_technician'),
                ('customer', 'customer_name_manual', 'customer_phone_manual'),
                'pos_estimate'
            )
        }),
        (_("2. Old Traded-In Device Identification"), {
            'fields': (
                ('brand_name', 'model_name'),
                ('ram_capacity', 'storage_capacity', 'color_variant'),
                ('imei_1', 'imei_2', 'serial_number'),
                'mdms_status'
            )
        }),
        (_("3. Valuation Calculation & Pre-Owned Grade"), {
            'fields': (
                ('market_base_value', 'total_deductions'),
                ('shop_margin_deduction', 'final_trade_in_value'),
                'recommended_condition_grade',
                ('restocked_product', 'restocked_item_instance'),
                'evaluation_notes'
            )
        }),
        (_("4. Timestamps & Audit Logs"), {
            'classes': ('collapse',),
            'fields': (
                ('created_at', 'updated_at'),
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

@admin.register(TradeInLegalUndertaking)
class TradeInLegalUndertakingAdmin(admin.ModelAdmin):
    list_display = [
        'customer_full_name', 'id_type', 'id_number', 'id_issued_district',
        'trade_in_voucher', 'declaration_accepted', 'verified_by', 'created_at'
    ]
    list_filter = ['id_type', 'declaration_accepted', 'id_issued_district', 'created_at']
    search_fields = [
        'customer_full_name', 'id_number', 'trade_in_voucher__voucher_number',
        'permanent_address', 'customer_father_or_spouse_name'
    ]
    raw_id_fields = ['trade_in_voucher', 'verified_by']
    readonly_fields = ['preview_customer_photo', 'preview_id_front', 'preview_id_back', 'created_at', 'updated_at']

    fieldsets = (
        (_("Customer Identity & Police KYC"), {
            'fields': (
                'trade_in_voucher',
                ('customer_full_name', 'customer_father_or_spouse_name'),
                ('id_type', 'id_number'),
                ('id_issued_district', 'id_issued_date_bs'),
                ('permanent_address', 'current_address')
            )
        }),
        (_("Identity Evidence & Snapshots"), {
            'fields': (
                ('id_front_image', 'preview_id_front'),
                ('id_back_image', 'preview_id_back'),
                ('customer_live_photo', 'preview_customer_photo'),
                'customer_digital_signature'
            )
        }),
        (_("Legal Undertaking & Verification"), {
            'fields': (
                'declaration_text',
                'declaration_accepted',
                'verified_by',
                ('created_at', 'updated_at')
            )
        }),
    )

    def preview_customer_photo(self, obj):
        if obj.customer_live_photo:
            return format_html(
                '<img src="{}" style="height: 120px; border-radius: 8px; border: 1px solid #cbd5e1; object-fit: cover;" />',
                obj.customer_live_photo.url
            )
        return "(No live photo)"
    preview_customer_photo.short_description = _("Customer Live Snapshot")

    def preview_id_front(self, obj):
        if obj.id_front_image:
            return format_html(
                '<img src="{}" style="height: 120px; border-radius: 8px; border: 1px solid #cbd5e1; object-fit: cover;" />',
                obj.id_front_image.url
            )
        return "(No ID front)"
    preview_id_front.short_description = _("ID Front Photo")

    def preview_id_back(self, obj):
        if obj.id_back_image:
            return format_html(
                '<img src="{}" style="height: 120px; border-radius: 8px; border: 1px solid #cbd5e1; object-fit: cover;" />',
                obj.id_back_image.url
            )
        return "(No ID back)"
    preview_id_back.short_description = _("ID Back Photo")

# =============================================================================
# 4. SALES RETURN & DEFECTIVE ITEMS ADMIN
# =============================================================================
class SalesReturnItemInline(admin.TabularInline):
    model = SalesReturnItem
    extra = 0
    raw_id_fields = ['estimate_item', 'product']
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
        'customer__name', 'customer__phone_number', 'return_date_bs', 'fiscal_year',
        'reason', 'items__returned_imei'
    ]
    raw_id_fields = ['original_estimate', 'branch', 'customer', 'processed_by']
    inlines = [SalesReturnItemInline]
    readonly_fields = ['return_number', 'fiscal_year', 'total_refund_amount', 'created_at', 'updated_at']

    fieldsets = (
        (_("Voucher Header & Routing (Historical Dates Supported)"), {
            'fields': (
                ('return_number', 'branch'),
                ('original_estimate', 'customer'),
                ('return_date_ad', 'return_date_bs', 'fiscal_year'),
                ('refund_mode', 'processed_by')
            )
        }),
        (_("Financials & Reasons"), {
            'fields': (
                'total_refund_amount',
                'reason',
                'technician_notes'
            )
        }),
        (_("Audit Metadata"), {
            'classes': ('collapse',),
            'fields': (
                ('created_at', 'updated_at'),
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