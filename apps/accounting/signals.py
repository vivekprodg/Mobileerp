"""
Accounting Event Listeners & Auto-Posting Signal Handlers.

Core Architectural Design:
1. Strict Fail-Closed Integrity:
   - If General Ledger voucher creation fails (e.g., locked fiscal period, missing
     control account, or imbalance), exceptions bubble up to guarantee that the
     enclosing database transaction rolls back.
2. Non-Monetary Sub-Ledger Protection (Anti-Phantom Cash Shield):
   - In `handle_customer_payment_auto_post`, filters out non-cash store credits
     (such as surplus trade-in buy-back allowances, sales return store credit adjustments,
     or opening balance equity offsets).
   - Guarantees that only genuine physical or digital monetary receipts entering the store
     trigger Cash/Bank receipt vouchers.
3. Explicit Service Coordination:
   - POS SalesEstimates and GoodsReceivedNotes (GRN) auto-posting are dispatched
     directly inside their respective atomic services (SalesPOSService and PurchaseService)
     to prevent race conditions, lock contention, and duplicate posting vouchers.
"""

from decimal import Decimal
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.sales.models import SalesReturn
from apps.purchases.models import SupplierUdhaariLedger
from apps.customers.models import CustomerUdhaariLedger
from apps.inventory.models import StockMovementLog
from apps.accounting.services.auto_posting import AutoPostingService

# Recognized genuine monetary payment modes where liquid funds entered the store
GENUINE_MONETARY_PAYMENT_MODES = {
    'CASH', 'BANK_TRANSFER', 'BANK', 'CHEQUE', 'CONNECT_IPS',
    'FONEPAY', 'QR', 'DYNAMIC_QR', 'ESEWA', 'KHALTI', 'CARD', 'POS', 'POS_CARD'
}

@receiver(post_save, sender=CustomerUdhaariLedger)
def handle_customer_payment_auto_post(sender, instance, created, **kwargs):
    """
    Triggers double-entry receipt voucher when a customer repays Udhaari debt with genuine money.
    
    Protective Filters:
    - Ignores non-monetary store credit deposits (e.g. Trade-In surplus credit from old phone buy-backs).
    - Ignores return adjustments and credit notes already posted during sales return finalization.
    - Exceptions bubble up to roll back the credit repayment transaction if GL posting fails.
    """
    if not (created and instance.entry_type == 'CREDIT' and instance.amount > Decimal('0.00')):
        return

    mode = str(instance.payment_mode or '').upper().strip()

    # 1. Reject non-monetary / internal adjustment modes
    if mode in ['OTHER', 'ADJUSTMENT', 'STORE_CREDIT', 'TRADE_IN', 'NON_MONETARY', '']:
        return

    if mode not in GENUINE_MONETARY_PAYMENT_MODES:
        return

    # 2. Inspect remarks for trade-in / return / non-cash context
    remarks_str = str(getattr(instance, 'remarks', '') or '').upper()
    if any(keyword in remarks_str for keyword in [
        'SURPLUS TRADE-IN', 'TRADE-IN BUY-BACK', 'TRADE-IN', 'STORE CREDIT',
        'RETURN VOUCHER', 'OPENING BALANCE', 'BARTER'
    ]):
        return

    # 3. Genuine monetary payment received: dispatch double-entry receipt voucher
    AutoPostingService.post_customer_payment(ledger_entry=instance)

@receiver(post_save, sender=SupplierUdhaariLedger)
def handle_supplier_payment_auto_post(sender, instance, created, **kwargs):
    """
    Triggers double-entry payment voucher when a payout is issued to a vendor/distributor.
    Filters out non-monetary adjustments (e.g. rate revisions, debit note reversals).
    Exceptions bubble up to roll back the payment transaction if GL posting fails.
    """
    if not (created and instance.transaction_type == 'PAYMENT' and instance.amount > Decimal('0.00')):
        return

    mode = str(instance.payment_mode or '').upper().strip()
    if mode in ['OTHER', 'ADJUSTMENT', 'NON_MONETARY', '']:
        return

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