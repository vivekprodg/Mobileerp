from django.contrib import admin
from django.utils.html import format_html
from apps.repairs.models import (
    RepairTicket, DeviceIntakeChecklist, DefectClassificationVerdict,
    RepairDiagnosticEvidence, RepairReplacedPart, CustomerQuotation,
    OpticalServiceTicket, TechnicianCommissionLog
)

class DeviceIntakeChecklistInline(admin.StackedInline):
    model = DeviceIntakeChecklist
    extra = 0
    can_delete = False

class DefectClassificationVerdictInline(admin.StackedInline):
    model = DefectClassificationVerdict
    extra = 0
    can_delete = False

class RepairDiagnosticEvidenceInline(admin.TabularInline):
    model = RepairDiagnosticEvidence
    extra = 0
    fields = ['evidence_type', 'image', 'caption', 'uploaded_by', 'created_at']
    readonly_fields = ['created_at']

class RepairReplacedPartInline(admin.TabularInline):
    model = RepairReplacedPart
    extra = 0
    fields = [
        'spare_part_product', 'quantity', 'cost_price', 'customer_charge',
        'old_part_serial_or_batch', 'new_part_serial_or_batch',
        'replacement_warranty_months', 'warranty_end_date', 'defective_part_status'
    ]

class OpticalServiceTicketInline(admin.StackedInline):
    model = OpticalServiceTicket
    extra = 0

@admin.register(RepairTicket)
class RepairTicketAdmin(admin.ModelAdmin):
    list_display = [
        'ticket_number', 'product', 'imei_or_serial', 'customer_name_manual',
        'customer_phone_manual', 'claim_type_badge', 'service_status_badge',
        'final_total_amount', 'technician', 'created_at'
    ]
    list_filter = ['service_status', 'claim_type', 'branch', 'technician', 'created_at']
    search_fields = ['ticket_number', 'imei_or_serial', 'customer_name_manual', 'customer_phone_manual', 'reported_fault']
    readonly_fields = ['ticket_number', 'created_at', 'updated_at']
    inlines = [
        DeviceIntakeChecklistInline,
        DefectClassificationVerdictInline,
        RepairDiagnosticEvidenceInline,
        RepairReplacedPartInline,
        OpticalServiceTicketInline
    ]

    fieldsets = (
        ("1. Ticket & Customer Information", {
            'fields': (
                ('ticket_number', 'branch'),
                ('customer', 'customer_name_manual', 'customer_phone_manual'),
                ('product', 'imei_or_serial', 'item_instance')
            )
        }),
        ("2. Security & Reported Fault", {
            'fields': (
                ('device_color', 'security_pin_code', 'pattern_lock_sequence'),
                'reported_fault', 'intake_accessories_received'
            )
        }),
        ("3. Diagnostics, Technician & Pipeline Status", {
            'fields': (
                ('claim_type', 'service_status'),
                'technician',
                'technician_diagnostic_findings',
                'qc_passed_notes'
            )
        }),
        ("4. Financials & Delivery", {
            'fields': (
                ('labor_charge', 'parts_cost', 'discount_amount'),
                ('final_total_amount', 'paid_amount'),
                ('expected_delivery_date', 'delivered_date', 'pos_invoice_reference')
            )
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
    claim_type_badge.short_description = "Warranty Type"

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
    service_status_badge.short_description = "Service Status"

@admin.register(DefectClassificationVerdict)
class DefectClassificationVerdictAdmin(admin.ModelAdmin):
    list_display = ['ticket', 'verdict', 'diagnosed_by', 'inspection_date', 'rma_eligibility_certified']
    list_filter = ['verdict', 'rma_eligibility_certified', 'inspection_date']
    search_fields = ['ticket__ticket_number', 'technical_justification']

@admin.register(CustomerQuotation)
class CustomerQuotationAdmin(admin.ModelAdmin):
    list_display = ['ticket', 'total_quoted_amount', 'approval_status', 'customer_response_timestamp', 'created_at']
    list_filter = ['approval_status', 'created_at']
    search_fields = ['ticket__ticket_number', 'quote_token']

@admin.register(TechnicianCommissionLog)
class TechnicianCommissionLogAdmin(admin.ModelAdmin):
    list_display = ['ticket', 'technician', 'branch', 'labor_amount_collected', 'commission_percentage', 'commission_earned', 'is_settled_in_payroll']
    list_filter = ['is_settled_in_payroll', 'technician', 'branch']