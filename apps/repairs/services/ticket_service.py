"""
Repair Ticket Coordination & Workshop Operations Engine.
File Path: apps/repairs/services/ticket_service.py

Lifecycle coordinator managing ticket generation, technician assignment,
diagnostic decision transitions, QC sign-offs, device deliveries,
and strictly atomic Double-Entry General Ledger Service Revenue postings.
"""

import uuid
import logging
from decimal import Decimal, ROUND_HALF_UP
from datetime import date
from typing import Optional
from django.db import transaction
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.repairs.models import (
    RepairTicket, DeviceIntakeChecklist, DefectClassificationVerdict, CustomerQuotation
)
from apps.branches.models import Branch, BranchDocumentSequence
from apps.inventory.models import Product
from apps.repairs.services.warranty_engine import WarrantyEvaluationEngine
from apps.core.models import AuditLog

logger = logging.getLogger(__name__)


class RepairTicketService:
    """
    Lifecycle coordinator managing ticket generation, technician assignment,
    diagnostic decision transitions, QC sign-offs, device deliveries,
    and automatic Double-Entry General Ledger Service Revenue postings.
    """

    @staticmethod
    def generate_ticket_number(branch: Branch) -> str:
        """
        Atomically allocates a strictly unique sequential repair ticket number
        using database row-level locking.
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
        """Updates diagnostic classification, estimates, and customer quotation."""
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

    @classmethod
    @transaction.atomic
    def deliver_ticket(
        cls,
        ticket: RepairTicket,
        payment_mode: str = 'CASH',
        paid_amount: Optional[Decimal] = None,
        pos_invoice_reference: Optional[str] = None,
        delivered_by=None
    ) -> RepairTicket:
        """
        Delivers the serviced handset to the customer:
        1. Validates delivery readiness.
        2. Sets service_status='DELIVERED', delivered_date, paid_amount, and POS invoice reference.
        3. Strictly posts Double-Entry General Ledger service revenue vouchers:
           - Debit: Cash in Hand / Bank (or Accounts Receivable if due)
           - Debit: Sales Discount Expense (if discount conceded)
           - Credit: Repair Service Labor Revenue
           - Credit: Spare Parts Revenue (if paid out-of-warranty)
           If GL voucher creation fails, the entire transaction rolls back fail-closed.
        4. Writes forensic audit logs.
        """
        if ticket.service_status == 'DELIVERED':
            raise ValidationError(f"Repair ticket {ticket.ticket_number} is already marked as DELIVERED.")
        if ticket.service_status == 'CANCELLED':
            raise ValidationError(f"Cannot deliver cancelled repair ticket {ticket.ticket_number}.")

        ticket.recalculate_totals()
        final_total = ticket.final_total_amount

        actual_paid = paid_amount if paid_amount is not None else final_total
        actual_paid = Decimal(str(actual_paid)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        ticket.paid_amount = actual_paid
        ticket.service_status = 'DELIVERED'
        ticket.delivered_date = timezone.now()

        if pos_invoice_reference:
            ticket.pos_invoice_reference = pos_invoice_reference.strip()

        ticket.save(update_fields=[
            'service_status', 'delivered_date', 'paid_amount',
            'pos_invoice_reference', 'final_total_amount', 'updated_at'
        ])

        # ---------------------------------------------------------------------
        # GENERAL LEDGER AUTOMATIC DOUBLE-ENTRY POSTING FOR WORKSHOP REVENUE
        # ---------------------------------------------------------------------
        cls.post_repair_revenue_gl(
            ticket=ticket,
            payment_mode=payment_mode,
            user=delivered_by or ticket.technician
        )

        AuditLog.objects.create(
            user=delivered_by,
            branch=ticket.branch,
            action_type='UPDATE',
            module='RepairService',
            object_repr=ticket.ticket_number,
            details={
                'action': 'DELIVERY_AND_CLOSURE',
                'labor_charge': str(ticket.labor_charge),
                'parts_cost': str(ticket.parts_cost),
                'discount_amount': str(ticket.discount_amount),
                'final_total': str(ticket.final_total_amount),
                'paid_amount': str(ticket.paid_amount),
                'payment_mode': payment_mode,
                'pos_invoice_reference': ticket.pos_invoice_reference or ""
            }
        )

        return ticket

    @classmethod
    def post_repair_revenue_gl(
        cls,
        ticket: RepairTicket,
        payment_mode: str = 'CASH',
        user=None
    ) -> None:
        """
        Posts balanced double-entry vouchers to the General Ledger upon repair delivery:
        - Debit: Cash in Hand (if CASH) or Bank & Digital Wallets (if QR/Card/IPS)
        - Debit: Accounts Receivable (Customer Debtors) if uncollected balance remains
        - Debit: Sales Discount Expense (if goodwill discount applied)
        - Credit: Repair Service Labor Revenue
        - Credit: Spare Parts Revenue (if paid out-of-warranty)

        Fail-closed: No exceptions are swallowed. Any ledger posting failure raises
        and guarantees the rollback of ticket delivery and payment records.
        """
        from apps.accounting.models import JournalEntry
        from apps.accounting.services.auto_posting import JournalEngine, AutoPostingService

        ref_doc = ticket.ticket_number

        # Prevent duplicate voucher posting
        if JournalEntry.objects.filter(
            voucher_type='RECEIPT',
            reference_document=ref_doc,
            status='POSTED'
        ).exists():
            return

        ticket.recalculate_totals()
        final_total = ticket.final_total_amount
        labor_revenue = ticket.labor_charge or Decimal('0.00')
        parts_revenue = ticket.parts_cost or Decimal('0.00')
        discount = ticket.discount_amount or Decimal('0.00')
        paid = ticket.paid_amount or Decimal('0.00')

        # If completely zero-value (e.g. Free Warranty), no financial revenue is realized
        if (labor_revenue + parts_revenue) <= Decimal('0.00'):
            return

        branch = ticket.branch
        lines = []

        # 1. DEBIT: Payment Collection (Cash vs Bank/QR)
        cash_acc = AutoPostingService.get_or_create_control_account(
            branch, 'CASH', '1010', 'Cash in Hand', 'ASSET', 'DEBIT'
        )
        bank_acc = AutoPostingService.get_or_create_control_account(
            branch, 'BANK', '1020', 'Bank & Digital Wallets', 'ASSET', 'DEBIT'
        )
        ar_acc = AutoPostingService.get_or_create_control_account(
            branch, 'ACCOUNTS_RECEIVABLE', '1030', 'Accounts Receivable (Debtors)', 'ASSET', 'DEBIT'
        )

        collection_acc = cash_acc if str(payment_mode).upper() == 'CASH' else bank_acc

        if paid > Decimal('0.00'):
            lines.append({
                'account': collection_acc,
                'debit': paid,
                'credit': Decimal('0.00'),
                'customer': ticket.customer,
                'narration': f"Workshop repair collection ({payment_mode}) for Ticket {ticket.ticket_number}"
            })

        # 2. DEBIT: Unpaid Debt / Due Balance (Accounts Receivable)
        due_amount = max(Decimal('0.00'), final_total - paid)
        if due_amount > Decimal('0.00'):
            lines.append({
                'account': ar_acc,
                'debit': due_amount,
                'credit': Decimal('0.00'),
                'customer': ticket.customer,
                'narration': f"Unpaid repair balance on Ticket {ticket.ticket_number}"
            })

        # 3. DEBIT: Discount Allowed
        if discount > Decimal('0.00'):
            disc_acc = AutoPostingService.get_or_create_control_account(
                branch, 'SALES_DISCOUNT', '5030', 'Sales Discounts Allowed', 'DIRECT_EXPENSE', 'DEBIT'
            )
            lines.append({
                'account': disc_acc,
                'debit': discount,
                'credit': Decimal('0.00'),
                'narration': f"Commercial discount on repair ticket {ticket.ticket_number}"
            })

        # 4. CREDIT: Repair Service Labor Revenue
        if labor_revenue > Decimal('0.00'):
            labor_acc = AutoPostingService.get_or_create_control_account(
                branch, 'REPAIR_SERVICE_INCOME', '4050', 'Repair Service Labor Revenue', 'REVENUE', 'CREDIT'
            )
            lines.append({
                'account': labor_acc,
                'debit': Decimal('0.00'),
                'credit': labor_revenue,
                'narration': f"Technician labor revenue for ticket {ticket.ticket_number} ({ticket.product.name})"
            })

        # 5. CREDIT: Spare Parts Revenue
        if parts_revenue > Decimal('0.00'):
            parts_acc = AutoPostingService.get_or_create_control_account(
                branch, 'REPAIR_PARTS_REVENUE', '4060', 'Repair Spare Parts Revenue', 'REVENUE', 'CREDIT'
            )
            lines.append({
                'account': parts_acc,
                'debit': Decimal('0.00'),
                'credit': parts_revenue,
                'narration': f"Spare parts billed on ticket {ticket.ticket_number} ({ticket.product.name})"
            })

        narration = (
            f"Repair Service Delivery & Revenue: Ticket {ticket.ticket_number} "
            f"({ticket.customer_name_manual} - {ticket.product.name})"
        )

        JournalEngine.create_balanced_entry(
            voucher_type='RECEIPT',
            date_ad=timezone.now().date(),
            branch=branch,
            lines=lines,
            narration=narration,
            reference_doc=ref_doc,
            user=user,
            auto_post=True
        )