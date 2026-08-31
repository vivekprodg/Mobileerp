import uuid
from decimal import Decimal
from datetime import date
from django.db import transaction
from django.core.exceptions import ValidationError
from apps.repairs.models import (
    RepairTicket, DeviceIntakeChecklist, DefectClassificationVerdict, CustomerQuotation
)
from apps.branches.models import Branch, BranchDocumentSequence
from apps.inventory.models import Product
from apps.repairs.services.warranty_engine import WarrantyEvaluationEngine


class RepairTicketService:
    """
    Lifecycle coordinator managing ticket generation, technician assignment,
    diagnostic decision transitions, and QC sign-offs.
    """

    @staticmethod
    def generate_ticket_number(branch: Branch) -> str:
        """
        Atomically allocates a strictly unique sequential repair ticket number
        using database row-level locking (CON-01 Fix).
        """
        prefix = f"SRV-{branch.code}"
        return BranchDocumentSequence.get_next_sequence_number(
            branch=branch,
            document_type='REPAIR_TICKET',
            prefix_override=prefix,
            padding=6
        )

    @classmethod
    @transaction.atomic
    def create_intake_ticket(
        cls,
        branch: Branch,
        product: Product,
        imei_or_serial: str,
        customer_name: str,
        customer_phone: str,
        reported_fault: str,
        claimed_component: str = 'DEVICE',
        pin_code: str = "",
        pattern_lock: str = "",
        intake_accessories: str = "Handset Only",
        checklist_data: dict = None,
        technician=None,
        created_by=None
    ) -> RepairTicket:
        """
        Creates a new repair ticket at intake, audits pre-existing condition,
        evaluates automatic warranty eligibility, and records the initial diagnostic verdict.
        Guarantees that diagnosed_by is never None to prevent database IntegrityError,
        and allocates ticket numbers atomically via BranchDocumentSequence.
        """
        clean_imei = imei_or_serial.strip()
        ticket_no = cls.generate_ticket_number(branch)

        ticket = RepairTicket.objects.create(
            ticket_number=ticket_no,
            branch=branch,
            product=product,
            imei_or_serial=clean_imei,
            customer_name_manual=customer_name,
            customer_phone_manual=customer_phone,
            reported_fault=reported_fault,
            claimed_component=claimed_component,
            security_pin_code=pin_code,
            pattern_lock_sequence=pattern_lock,
            intake_accessories_received=intake_accessories,
            technician=technician,
            service_status='RECEIVED'
        )

        checklist_obj = None
        if checklist_data:
            checklist_obj = DeviceIntakeChecklist.objects.create(ticket=ticket, **checklist_data)

        # Run Automatic Defect & Warranty Engine
        assessment = WarrantyEvaluationEngine.evaluate_warranty_eligibility(
            imei_or_serial=clean_imei,
            claimed_component=claimed_component,
            checklist=checklist_obj
        )

        if assessment['is_found']:
            ticket.item_instance = assessment.get('item_instance')
            ticket.claim_type = 'FREE_WARRANTY' if assessment['is_eligible_free'] else 'PAID_OUT_OF_WARRANTY'
            if assessment['verdict'] == 'BRAND_SPECIAL_RECALL':
                ticket.claim_type = 'BRAND_SPECIAL_POLICY'
            ticket.save(update_fields=['item_instance', 'claim_type'])

            # Self-healing fallback chain for diagnosed_by
            diagnosing_user = technician or created_by
            if not diagnosing_user and branch.manager:
                diagnosing_user = branch.manager
            if not diagnosing_user and hasattr(branch, 'staff_members'):
                diagnosing_user = branch.staff_members.filter(is_active=True).first()
            if not diagnosing_user:
                from apps.users.models import User
                diagnosing_user = User.objects.filter(is_superuser=True, is_active=True).first() or User.objects.filter(is_active=True).first()

            DefectClassificationVerdict.objects.create(
                ticket=ticket,
                verdict=assessment['verdict'],
                diagnosed_by=diagnosing_user,
                technical_justification=assessment['message'],
                rma_eligibility_certified=assessment['is_eligible_free']
            )

        return ticket

    @classmethod
    @transaction.atomic
    def update_diagnostic_findings(
        cls,
        ticket: RepairTicket,
        verdict: str,
        technical_justification: str,
        estimated_labor: Decimal,
        estimated_parts: Decimal,
        diagnosed_by
    ) -> RepairTicket:
        verdict_obj, _ = DefectClassificationVerdict.objects.get_or_create(
            ticket=ticket,
            defaults={'diagnosed_by': diagnosed_by, 'technical_justification': technical_justification}
        )
        verdict_obj.verdict = verdict
        verdict_obj.technical_justification = technical_justification
        verdict_obj.rma_eligibility_certified = verdict in ['GENUINE_FACTORY_DEFECT', 'BRAND_SPECIAL_RECALL']
        verdict_obj.diagnosed_by = diagnosed_by
        verdict_obj.save()

        ticket.technician_diagnostic_findings = technical_justification
        ticket.labor_charge = estimated_labor
        ticket.parts_cost = estimated_parts

        if verdict in ['GENUINE_FACTORY_DEFECT', 'BRAND_SPECIAL_RECALL']:
            ticket.claim_type = 'FREE_WARRANTY'
            ticket.service_status = 'APPROVED'
            ticket.labor_charge = Decimal('0.00')
            ticket.parts_cost = Decimal('0.00')
        else:
            ticket.claim_type = 'PAID_OUT_OF_WARRANTY'
            ticket.service_status = 'QUOTATION_PENDING'

            CustomerQuotation.objects.update_or_create(
                ticket=ticket,
                defaults={
                    'estimated_labor_total': estimated_labor,
                    'estimated_parts_total': estimated_parts,
                    'approval_status': 'PENDING'
                }
            )

        ticket.save()
        return ticket