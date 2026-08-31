from decimal import Decimal
from django.db.models.signals import post_save
from django.dispatch import receiver
from apps.inventory.models import Product, BranchStock
from apps.branches.models import Branch


@receiver(post_save, sender=Product)
def initialize_branch_stocks_for_new_product(sender, instance, created, **kwargs):
    """
    Auto-creates zero-quantity stock entries across all active branches when a product
    is registered. Optimized using bulk_create to eliminate individual query loops.
    Bypasses execution when `_skip_signal_branch_stock` flag is set on the instance (e.g. bulk Excel imports).
    Uses explicit Decimal zero representations for numerical precision.
    """
    if created and not getattr(instance, '_skip_signal_branch_stock', False):
        active_branches = Branch.objects.filter(is_active=True)
        existing_branch_ids = set(
            BranchStock.objects.filter(product=instance, branch__in=active_branches)
            .values_list('branch_id', flat=True)
        )

        reorder_threshold = instance.reorder_level if instance.reorder_level is not None else Decimal('5.00')
        
        new_stocks = [
            BranchStock(
                branch=branch,
                product=instance,
                quantity=Decimal('0.000'),
                reserved_quantity=Decimal('0.000'),
                quarantined_defective_quantity=Decimal('0.000'),
                low_stock_threshold=reorder_threshold
            )
            for branch in active_branches
            if branch.id not in existing_branch_ids
        ]
        
        if new_stocks:
            BranchStock.objects.bulk_create(new_stocks, ignore_conflicts=True)