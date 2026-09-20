"""
Inventory Signals & Event Handlers.

Core Architectural Design:
1. Signal Muting Capability:
   - Provides thread-safe suppression utilities (suppress_inventory_signals) so that bulk imports,
     migrations, and background synchronization routines can safely bypass signals.
2. Auto-Branch Stock Initialization:
   - Auto-creates zero-quantity BranchStock records across all active branches when a new Product
     is registered, skipping execution if _skip_signal_branch_stock is set.
3. Explicit Absence of Sales Deduction Signals:
   - POS stock deduction lives deliberately inside SalesPOSService.process_checkout().
   - No post_save signal exists on SalesEstimate, guaranteeing that saving historical 2080 bills
     will NEVER trigger background inventory stock decrements.
"""

import threading
from decimal import Decimal
from contextlib import contextmanager
from django.db.models.signals import post_save
from django.dispatch import receiver
from apps.inventory.models import Product, BranchStock
from apps.branches.models import Branch

# Thread-local storage for managing runtime signal suppression
_thread_locals = threading.local()


def is_inventory_signals_muted() -> bool:
    """Returns True if inventory signals are currently muted on this thread."""
    return getattr(_thread_locals, 'mute_inventory_signals', False)


def mute_inventory_signals():
    """Mutes inventory signals on the current thread."""
    _thread_locals.mute_inventory_signals = True


def unmute_inventory_signals():
    """Unmutes inventory signals on the current thread."""
    _thread_locals.mute_inventory_signals = False


@contextmanager
def suppress_inventory_signals():
    """
    Context manager to safely suppress inventory signals during migrations and bulk operations.
    Usage:
        with suppress_inventory_signals():
            # Bulk operations run here with signals completely bypassed
            Product.objects.bulk_create(...)
    """
    mute_inventory_signals()
    try:
        yield
    finally:
        unmute_inventory_signals()


@receiver(post_save, sender=Product)
def initialize_branch_stocks_for_new_product(sender, instance, created, **kwargs):
    """
    Auto-creates zero-quantity stock entries across all active branches when a product
    is registered. Optimized using bulk_create to eliminate individual query loops.
    Bypasses execution when `_skip_signal_branch_stock` flag is set or when signals are muted.
    """
    if is_inventory_signals_muted():
        return

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