from decimal import Decimal
from datetime import date
from django.db import transaction
from django.core.exceptions import ValidationError
from apps.repairs.models import RepairTicket, RepairReplacedPart
from apps.inventory.models import Product, BranchStock, StockMovementLog

class RepairPartsService:
    """
    Executes spare part stock allocation, live inventory deduction,
    defective quarantine placement, and sub-warranty ledger creation.
    """

    @classmethod
    @transaction.atomic
    def install_replacement_part(
        cls,
        ticket: RepairTicket,
        spare_part_product: Product,
        quantity: Decimal = Decimal('1.000'),
        old_part_serial: str = "",
        new_part_serial: str = "",
        customer_charge: Decimal = Decimal('0.00'),
        sub_warranty_months: int = 6,
        defective_destination: str = 'QUARANTINED_FOR_RMA',
        user=None
    ) -> RepairReplacedPart:
        branch = ticket.branch

        # Check Available Branch Stock
        branch_stock, _ = BranchStock.objects.select_for_update().get_or_create(
            branch=branch,
            product=spare_part_product,
            defaults={'quantity': Decimal('0.000')}
        )

        if branch_stock.quantity < quantity:
            raise ValidationError(
                f"Insufficient stock for spare part '{spare_part_product.name}' at {branch.name}. "
                f"Available: {branch_stock.quantity}, Needed: {quantity}"
            )

        # Deduct sellable stock
        prev_qty = branch_stock.quantity
        branch_stock.quantity -= quantity
        branch_stock.save(update_fields=['quantity', 'updated_at'])

        StockMovementLog.objects.create(
            product=spare_part_product,
            branch=branch,
            movement_type='SERVICE_REPLACED_PART_DEDUCT',
            quantity_delta=-quantity,
            previous_quantity=prev_qty,
            new_quantity=branch_stock.quantity,
            reference_document=ticket.ticket_number,
            imei_or_serial_number=new_part_serial or ticket.imei_or_serial,
            remarks=f"Installed under ticket {ticket.ticket_number}",
            user=user
        )

        actual_cost = spare_part_product.purchase_price
        billed_to_customer = Decimal('0.00') if ticket.claim_type == 'FREE_WARRANTY' else customer_charge

        replaced_part = RepairReplacedPart.objects.create(
            ticket=ticket,
            spare_part_product=spare_part_product,
            branch=branch,
            quantity=quantity,
            cost_price=actual_cost,
            customer_charge=billed_to_customer,
            old_part_serial_or_batch=old_part_serial or None,
            new_part_serial_or_batch=new_part_serial or None,
            replacement_warranty_months=sub_warranty_months,
            defective_part_status=defective_destination
        )

        ticket.parts_cost += billed_to_customer
        ticket.service_status = 'QC_TESTING'
        ticket.save()

        return replaced_part