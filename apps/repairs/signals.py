from django.db.models.signals import post_save
from django.dispatch import receiver
from apps.repairs.models import RepairTicket, RepairReplacedPart
from apps.inventory.models import BranchStock, StockMovementLog
from apps.core.models import AuditLog

@receiver(post_save, sender=RepairReplacedPart)
def handle_defective_part_quarantine_stock(sender, instance, created, **kwargs):
    """
    When a defective part is replaced under warranty, increment the branch's
    Quarantined Defective Stock counter for vendor RMA batching.
    """
    if created and instance.defective_part_status == 'QUARANTINED_FOR_RMA':
        branch_stock, _ = BranchStock.objects.get_or_create(
            branch=instance.branch,
            product=instance.spare_part_product,
            defaults={'quantity': 0.0, 'quarantined_defective_quantity': 0.0}
        )
        prev_quarantine = branch_stock.quarantined_defective_quantity
        branch_stock.quarantined_defective_quantity += instance.quantity
        branch_stock.save(update_fields=['quarantined_defective_quantity', 'updated_at'])

        StockMovementLog.objects.create(
            product=instance.spare_part_product,
            branch=instance.branch,
            movement_type='SERVICE_DEFECTIVE_QUARANTINE',
            quantity_delta=instance.quantity,
            previous_quantity=prev_quarantine,
            new_quantity=branch_stock.quarantined_defective_quantity,
            reference_document=instance.ticket.ticket_number,
            imei_or_serial_number=instance.old_part_serial_or_batch or instance.ticket.imei_or_serial,
            remarks=f"Defective part quarantined from ticket {instance.ticket.ticket_number}",
            user=instance.ticket.technician
        )

@receiver(post_save, sender=RepairTicket)
def audit_repair_status_transitions(sender, instance, created, **kwargs):
    """Log status modifications for forensic audit tracking."""
    action = 'CREATE' if created else 'UPDATE'
    AuditLog.objects.create(
        user=instance.technician,
        branch=instance.branch,
        action_type=action,
        module='RepairService',
        object_repr=instance.ticket_number,
        details={
            'status': instance.service_status,
            'claim_type': instance.claim_type,
            'total_amount': str(instance.final_total_amount)
        }
    )