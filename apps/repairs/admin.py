from django.contrib import admin
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from apps.repairs.models import (
    RepairTicket,
    DeviceIntakeChecklist,
    DefectClassificationVerdict,
    RepairDiagnosticEvidence,
    RepairReplacedPart,
    CustomerQuotation,
    OpticalServiceTicket,
    TechnicianCommissionLog
)

# ==============================================================================
# INLINE ADMIN DEFINITIONS
# ==============================================================================
class DeviceIntakeChecklistInline(admin.StackedInline):
    model = DeviceIntakeChecklist
    extra = 0
    can_delete = False
    classes = ['collapse']

class DefectClassificationVerdictInline(admin.StackedInline):
    model = DefectClassificationVerdict
    extra = 0
    can_delete = False
    classes = ['collapse']

class CustomerQuotationInline(admin.StackedInline):
    model = CustomerQuotation
    extra = 0
    can_delete = False
    readonly_fields = ['quote_token', 'total_quoted_amount', 'customer_response_timestamp']
    classes = ['collapse']

class RepairDiagnosticEvidenceInline(admin.TabularInline):
    model = RepairDiagnosticEvidence
    extra = 0
    fields = ['evidence_type', 'image', 'caption', 'uploaded_by', 'image_thumbnail', 'created_at']
    readonly_fields = ['image_thumbnail', 'created_at']
    classes = ['collapse']

    def image_thumbnail(self, obj):
        if obj.image:
            return format_html(
                '<a href="{0}" target="_blank"><img src="{0}" style="max-height: 45px; max-width: 70px; border-radius: 4px; border: 1px solid #ccc;" /></a>',
                obj.image.url
            )
        return "-"
    image_thumbnail.short_description = _("Thumbnail")

class RepairReplacedPartInline(admin.TabularInline):
    model = RepairReplacedPart
    extra = 0
    fields = [
        'spare_part_product', 'branch', 'quantity', 'cost_price', 'customer_charge',
        'old_part_serial_or_batch', 'new_part_serial_or_batch',
        'replacement_warranty_months', 'warranty_start_date', 'warranty_end_date',
        'defective_part_status'
    ]
    classes = ['collapse']

class OpticalServiceTicketInline(admin.StackedInline):
    model = OpticalServiceTicket
    extra = 0
    classes = ['collapse']

class TechnicianCommissionLogInline(admin.TabularInline):
    model = TechnicianCommissionLog
    extra = 0
    fields = [
        'technician', 'labor_amount_collected', 'commission_percentage',
        'commission_earned', 'turnaround_time_hours', 'is_settled_in_payroll'
    ]
    readonly_fields = ['commission_earned']
    classes = ['collapse']

# ==============================================================================
# MODEL ADMIN DEFINITIONS
# ==============================================================================
@admin.register(RepairTicket)
class RepairTicketAdmin(admin.ModelAdmin):
    list_display = [
        'ticket_number', 'product', 'imei_or_serial', 'customer_name_manual',
        'customer_phone_manual', 'claim_type_badge', 'service_status_badge',
        'final_total_amount', 'paid_amount', 'balance_amount_display',
        'technician', 'created_at'
    ]
    list_filter = ['service_status', 'claim_type', 'branch', 'technician', 'created_at']
    search_fields = [
        'ticket_number', 'imei_or_serial', 'customer_name_manual',
        'customer_phone_manual', 'reported_fault', 'product__name'
    ]
    readonly_fields = ['ticket_number', 'final_total_amount', 'created_at', 'updated_at']
    inlines = [
        DeviceIntakeChecklistInline,
        DefectClassificationVerdictInline,
        CustomerQuotationInline,
        RepairDiagnosticEvidenceInline,
        RepairReplacedPartInline,
        OpticalServiceTicketInline,
        TechnicianCommissionLogInline
    ]

    fieldsets = (
        (_("1. Ticket & Customer Identification"), {
            'fields': (
                ('ticket_number', 'branch'),
                ('customer', 'customer_name_manual', 'customer_phone_manual'),
                ('product', 'imei_or_serial', 'item_instance'),
                'component_warranty_record',
            )
        }),
        (_("2. Physical Intake & Security Access"), {
            'fields': (
                ('device_color', 'security_pin_code', 'pattern_lock_sequence'),
                ('backup_warning_acknowledged', 'intake_accessories_received'),
                'reported_fault',
            )
        }),
        (_("3. Diagnostics, Technician & Service Pipeline"), {
            'fields': (
                ('claim_type', 'claimed_component'),
                ('service_status', 'technician'),
                'technician_diagnostic_findings',
                'qc_passed_notes',
            )
        }),
        (_("4. Billing, Financials & Handover"), {
            'fields': (
                ('estimated_cost', 'labor_charge', 'parts_cost'),
                ('discount_amount', 'final_total_amount', 'paid_amount'),
                ('expected_delivery_date', 'delivered_date', 'pos_invoice_reference')
            )
        }),
        (_("5. System Audit Timestamps"), {
            'fields': (('created_at', 'updated_at'),),
            'classes': ('collapse',)
        }),
    )

    def claim_type_badge(self, obj):
        colors = {
            'FREE_WARRANTY': '#10b981',
            'PAID_OUT_OF_WARRANTY': '#ef4444',
            'BRAND_SPECIAL_POLICY': '#3b82f6',
            'GOODWILL_DISCOUNT': '#f59e0b',
        }
        color = colors.get(obj.claim_type, '#64748b')
        return format_html(
            '<span style="color: white; background-color: {}; padding: 3px 8px; border-radius: 999px; font-weight: 600; font-size: 11px;">{}</span>',
            color, obj.get_claim_type_display()
        )
    claim_type_badge.short_description = _("Warranty Type")

    def service_status_badge(self, obj):
        colors = {
            'RECEIVED': '#94a3b8',
            'DIAGNOSING': '#f59e0b',
            'QUOTATION_PENDING': '#ec4899',
            'APPROVED': '#06b6d4',
            'IN_REPAIR': '#3b82f6',
            'WAITING_PARTS': '#eab308',
            'QC_TESTING': '#8b5cf6',
            'READY_FOR_PICKUP': '#10b981',
            'DELIVERED': '#059669',
            'CANCELLED': '#ef4444',
        }
        color = colors.get(obj.service_status, '#334155')
        return format_html(
            '<span style="color: white; background-color: {}; padding: 3px 8px; border-radius: 999px; font-weight: 600; font-size: 11px;">{}</span>',
            color, obj.get_service_status_display()
        )
    service_status_badge.short_description = _("Service Status")

    def balance_amount_display(self, obj):
        balance = obj.balance_amount
        color = '#ef4444' if balance > 0 else '#10b981'
        return format_html(
            '<span style="color: {}; font-weight: 700;">NPR {:,.2f}</span>',
            color, balance
        )
    balance_amount_display.short_description = _("Balance Due")

@admin.register(DeviceIntakeChecklist)
class DeviceIntakeChecklistAdmin(admin.ModelAdmin):
    list_display = [
        'ticket', 'power_status', 'ldi_indicator_status', 'body_physical_grade',
        'is_front_glass_cracked', 'has_display_lines_or_bleed', 'created_at'
    ]
    list_filter = ['power_status', 'ldi_indicator_status', 'body_physical_grade', 'is_front_glass_cracked']
    search_fields = ['ticket__ticket_number', 'ticket__customer_name_manual', 'intake_notes']

@admin.register(DefectClassificationVerdict)
class DefectClassificationVerdictAdmin(admin.ModelAdmin):
    list_display = ['ticket', 'verdict', 'diagnosed_by', 'inspection_date', 'rma_eligibility_certified']
    list_filter = ['verdict', 'rma_eligibility_certified', 'inspection_date']
    search_fields = ['ticket__ticket_number', 'technical_justification', 'diagnosed_by__username']

@admin.register(RepairDiagnosticEvidence)
class RepairDiagnosticEvidenceAdmin(admin.ModelAdmin):
    list_display = ['ticket', 'evidence_type', 'caption', 'uploaded_by', 'image_preview', 'created_at']
    list_filter = ['evidence_type', 'created_at']
    search_fields = ['ticket__ticket_number', 'caption', 'uploaded_by__username']

    def image_preview(self, obj):
        if obj.image:
            return format_html(
                '<a href="{0}" target="_blank"><img src="{0}" style="max-height: 50px; border-radius: 4px;" /></a>',
                obj.image.url
            )
        return "-"
    image_preview.short_description = _("Preview")

@admin.register(RepairReplacedPart)
class RepairReplacedPartAdmin(admin.ModelAdmin):
    list_display = [
        'ticket', 'spare_part_product', 'branch', 'quantity', 'cost_price',
        'customer_charge', 'defective_part_status', 'replacement_warranty_months',
        'warranty_end_date'
    ]
    list_filter = ['defective_part_status', 'branch', 'replacement_warranty_months']
    search_fields = [
        'ticket__ticket_number', 'spare_part_product__name',
        'old_part_serial_or_batch', 'new_part_serial_or_batch'
    ]

@admin.register(CustomerQuotation)
class CustomerQuotationAdmin(admin.ModelAdmin):
    list_display = [
        'ticket', 'quote_token', 'total_quoted_amount', 'approval_status',
        'customer_response_timestamp', 'created_at'
    ]
    list_filter = ['approval_status', 'created_at']
    search_fields = ['ticket__ticket_number', 'quote_token', 'rejection_reason']
    readonly_fields = ['quote_token', 'total_quoted_amount', 'created_at', 'updated_at']

@admin.register(OpticalServiceTicket)
class OpticalServiceTicketAdmin(admin.ModelAdmin):
    list_display = [
        'ticket', 'service_type', 'frame_brand_and_model',
        'right_eye_sph', 'left_eye_sph', 'pupillary_distance_pd', 'created_at'
    ]
    list_filter = ['service_type', 'created_at']
    search_fields = ['ticket__ticket_number', 'frame_brand_and_model', 'lens_type_description']

@admin.register(TechnicianCommissionLog)
class TechnicianCommissionLogAdmin(admin.ModelAdmin):
    list_display = [
        'ticket', 'technician', 'branch', 'labor_amount_collected',
        'commission_percentage', 'commission_earned', 'turnaround_time_hours',
        'is_settled_in_payroll', 'created_at'
    ]
    list_filter = ['is_settled_in_payroll', 'technician', 'branch', 'created_at']
    search_fields = ['ticket__ticket_number', 'technician__username', 'technician__first_name', 'technician__last_name']
    readonly_fields = ['commission_earned', 'created_at', 'updated_at']