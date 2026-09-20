"""
Accounting Event Listeners & Auto-Posting Signal Handlers.
File Path: apps/accounting/signals.py

Core Architectural Design:
1. Strict Fail-Closed Integrity:
   - Exception swallowing (try...except logging without raising) has been removed.
   - If General Ledger voucher creation fails (e.g., locked fiscal period, missing
     control account, or imbalance), the exception bubbles up, guaranteeing that the
     enclosing database transaction rolls back.
2. Standardized Explicit Service Coordination:
   - Redundant post_save signal handlers for POS SalesEstimates and GoodsReceivedNotes (GRN)
     have been removed. These operations execute GL auto-posting directly within their
     respective atomic services (SalesService and PurchaseService). Removing them from signals
     eliminates race conditions, lock contention, and duplicate posting vouchers.
3. Event-Driven Asynchronous Operations:
   - Customer Udhaari repayments, Supplier Udhaari payouts, Sales Returns, and manual inventory
     write-offs/shrinkage continue to trigger GL vouchers here fail-closed.
"""

from decimal import Decimal
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.sales.models import SalesReturn
from apps.purchases.models import SupplierUdhaariLedger
from apps.customers.models import CustomerUdhaariLedger
from apps.inventory.models import StockMovementLog
from apps.accounting.services.auto_posting import AutoPostingService


# =============================================================================
# EXPLICIT SERVICE CALL NOTICE:
# SalesEstimate and GoodsReceivedNote auto-posting handlers have been removed from
# signals.py to prevent race conditions and duplicate journal creation.
# They are invoked directly inside:
# - SalesService (at POS checkout / bill finalization)
# - PurchaseService.process_grn_approval_and_stock_in (at GRN verification)
# =============================================================================


@receiver(post_save, sender=CustomerUdhaariLedger)
def handle_customer_payment_auto_post(sender, instance, created, **kwargs):
    """
    Triggers double-entry receipt voucher when a customer repays Udhaari debt.
    Exceptions bubble up to roll back the credit repayment transaction if GL posting fails.
    """
    if created and instance.entry_type == 'CREDIT' and instance.amount > Decimal('0.00'):
        AutoPostingService.post_customer_payment(ledger_entry=instance)


@receiver(post_save, sender=SupplierUdhaariLedger)
def handle_supplier_payment_auto_post(sender, instance, created, **kwargs):
    """
    Triggers double-entry payment voucher when a payout is issued to a vendor/distributor.
    Exceptions bubble up to roll back the payment transaction if GL posting fails.
    """
    if created and instance.transaction_type == 'PAYMENT' and instance.amount > Decimal('0.00'):
        AutoPostingService.post_supplier_payment(ledger_entry=instance)


@receiver(post_save, sender=SalesReturn)
def handle_sales_return_auto_post(sender, instance, created, **kwargs):
    """
    Triggers credit note and inventory re-docking journal when a customer sales return is finalized.
    Exceptions bubble up to roll back the return transaction if GL posting fails.
    """
    if instance.total_refund_amount > Decimal('0.00'):
        AutoPostingService.post_sales_return(sales_return=instance)


@receiver(post_save, sender=StockMovementLog)
def handle_stock_shrinkage_auto_post(sender, instance, created, **kwargs):
    """
    Triggers write-off expense voucher when manual damaged/lost stock subtraction occurs.
    Exceptions bubble up to roll back the inventory adjustment if GL posting fails.
    """
    if created and instance.movement_type == 'ADJUSTMENT_SUB':
        qty_lost = abs(instance.quantity_delta)
        unit_cost = instance.product.purchase_price
        AutoPostingService.post_inventory_shrinkage(
            branch=instance.branch,
            product=instance.product,
            quantity=qty_lost,
            unit_cost=unit_cost,
            reason=instance.remarks or "Physical inventory damage / loss write-off",
            user=instance.user
        )