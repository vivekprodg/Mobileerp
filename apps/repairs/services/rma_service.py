import uuid
from decimal import Decimal
from datetime import date
from typing import List
from django.db import transaction
from django.core.exceptions import ValidationError
from apps.inventory.models import VendorRMAClaim, VendorRMAClaimItem, BranchStock, StockMovementLog
from apps.repairs.models import RepairReplacedPart
from apps.purchases.models import Supplier, SupplierUdhaariLedger
from apps.branches.models import Branch

class VendorRMAService:
    """
    Batches quarantined defective parts into vendor delivery challans and reconciles
    distributor replacements or credit note reimbursements.
    """

    @classmethod
    @transaction.atomic
    def dispatch_defective_parts_rma(
        cls,
        supplier: Supplier,
        branch: Branch,
        quarantined_parts: List[RepairReplacedPart],
        tracking_ref: str = "",
        service_center_lab: str = "Authorized National Service Center",
        user=None
    ) -> VendorRMAClaim:
        if not quarantined_parts:
            raise ValidationError("No defective parts selected for RMA batching.")

        rma_number = f"RMA-{branch.code}-{uuid.uuid4().hex[:6].upper()}"

        claim = VendorRMAClaim.objects.create(
            rma_number=rma_number,
            supplier=supplier,
            branch=branch,
            status='DISPATCHED_TO_VENDOR',
            dispatch_date=date.today(),
            distributor_tracking_ref=tracking_ref,
            distributor_service_center=service_center_lab,
            total_claimed_parts_count=len(quarantined_parts),
            dispatched_by=user
        )

        for part in quarantined_parts:
            verdict_text = "Genuine Factory Defect under Warranty"
            if hasattr(part.ticket, 'defect_verdict'):
                verdict_text = part.ticket.defect_verdict.technical_justification

            VendorRMAClaimItem.objects.create(
                rma_claim=claim,
                product=part.spare_part_product,
                defective_serial_or_imei=part.old_part_serial_or_batch or part.ticket.imei_or_serial,
                defect_description=f"Ticket {part.ticket.ticket_number}: {verdict_text}",
                resolution='PENDING'
            )

            # Deduct from branch defective quarantine counter
            bs = BranchStock.objects.filter(branch=branch, product=part.spare_part_product).first()
            if bs and bs.quarantined_defective_quantity >= part.quantity:
                bs.quarantined_defective_quantity -= part.quantity
                bs.save(update_fields=['quarantined_defective_quantity', 'updated_at'])

        return claim

    @classmethod
    @transaction.atomic
    def settle_rma_item_settlement(
        cls,
        rma_item: VendorRMAClaimItem,
        resolution: str,
        replacement_serial: str = "",
        credit_amount: Decimal = Decimal('0.00'),
        user=None
    ) -> VendorRMAClaimItem:
        rma_item.resolution = resolution
        rma_item.replacement_batch_or_serial = replacement_serial
        rma_item.credit_amount = credit_amount
        rma_item.save()

        rma_claim = rma_item.rma_claim

        if resolution == 'REPLACED_WITH_NEW_PART':
            bs, _ = BranchStock.objects.get_or_create(
                branch=rma_claim.branch,
                product=rma_item.product,
                defaults={'quantity': Decimal('0.000')}
            )
            prev_qty = bs.quantity
            bs.quantity += Decimal('1.000')
            bs.save(update_fields=['quantity', 'updated_at'])

            StockMovementLog.objects.create(
                product=rma_item.product,
                branch=rma_claim.branch,
                movement_type='RMA_VENDOR_REPLACEMENT_IN',
                quantity_delta=Decimal('1.000'),
                previous_quantity=prev_qty,
                new_quantity=bs.quantity,
                reference_document=rma_claim.rma_number,
                imei_or_serial_number=replacement_serial,
                remarks=f"RMA New Replacement Received from {rma_claim.supplier.company_name}",
                user=user
            )

        elif resolution == 'CREDIT_NOTE_ISSUED':
            supplier = rma_claim.supplier
            prev_bal = supplier.current_balance
            new_bal = prev_bal - credit_amount
            supplier.current_balance = new_bal
            supplier.save(update_fields=['current_balance', 'updated_at'])

            SupplierUdhaariLedger.objects.create(
                supplier=supplier,
                branch=rma_claim.branch,
                transaction_type='ADJUSTMENT',
                amount=credit_amount,
                previous_balance=prev_bal,
                resulting_balance=new_bal,
                payment_mode='OTHER',
                reference_number=rma_claim.rma_number,
                remarks=f"Distributor warranty credit note for {rma_item.product.name}",
                recorded_by=user
            )

        # Check if all items in claim are closed
        if not rma_claim.claimed_items.filter(resolution='PENDING').exists():
            rma_claim.status = 'COMPLETED'
            rma_claim.resolution_date = date.today()
            rma_claim.resolved_by = user
            rma_claim.total_credit_amount = sum((it.credit_amount for it in rma_claim.claimed_items.all()), Decimal('0.00'))
            rma_claim.save()

        return rma_item