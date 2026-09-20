"""
Purchase GRN & Commercial Purchase Return (Debit Note) Services.
File Path: apps/purchases/services.py

Core Capabilities:
1. Value-Based Overhead Distribution:
   - Shipping, freight, customs duty, and handling fees are allocated proportionally
     based on each item's monetary value rather than raw unit box counts.
2. Strict Serialized & Dual-IMEI Enforcement:
   - Mandates that if N units of a smartphone are received, exactly N valid IMEI pairs
     (IMEI 1 & optional IMEI 2) must be scanned and verified against active stock before
     inventory is approved.
3. Thread-Safe Supplier Ledger Reconciliation:
   - Atomically updates supplier outstanding balances with row-level locking (select_for_update)
     and logs double-entry ledger transactions.
4. Product Master & Multi-Branch FIFO Updates:
   - Adjusts landed cost, updates counter MRPs, records historical price fluctuations,
     and initializes serialized ItemInstances with NTA MDMS certification.
5. Strict Atomic General Ledger Double-Entry Accounting Integration:
   - Inward GRN verification automatically debits Merchandise Inventory Asset & Input VAT
     and credits Accounts Payable / Cash via post_grn_journal / post_grn_receipt.
   - Purchase Return (Debit Note) verification automatically debits Accounts Payable / Cash
     and credits Merchandise Inventory Asset & Input VAT via post_purchase_return_journal.
   - All GL postings are strictly bound to the database transaction. If GL voucher creation
     fails, the entire procurement/return transaction rolls back cleanly.
"""

import re
import uuid
import logging
from decimal import Decimal, ROUND_HALF_UP
from datetime import date, timedelta
from typing import List, Tuple, Dict, Any, Optional

from django.db import transaction
from django.db.models import Q
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.purchases.models import (
    GoodsReceivedNote, GRNItem, Supplier, SupplierUdhaariLedger,
    PurchaseReturn, PurchaseReturnItem
)
from apps.inventory.models import Product, ItemInstance, ProductBatch, BranchStock
from apps.inventory.services import InventoryService
from apps.branches.models import Branch, BranchDocumentSequence
from apps.reports.models import ProductCostHistory
from apps.core.models import AuditLog

logger = logging.getLogger(__name__)


# =============================================================================
# GENERAL LEDGER DISPATCHER BRIDGES (STRICT TRANSACTIONAL INTEGRITY)
# =============================================================================

def _post_purchase_return_direct(purchase_return: PurchaseReturn, user=None):
    """
    Direct General Ledger poster for commercial purchase returns (Debit Notes).
    - Debit: Accounts Payable (Supplier Ledger) OR Cash in Hand (if cash refund)
    - Credit: Merchandise Inventory Asset (at total return value)
    - Credit: Input VAT 13% (reversing input tax if tax invoice)

    Errors bubble up to enforce atomic rollback across stock and ledger.
    """
    from apps.accounting.models import JournalEntry
    from apps.accounting.services.auto_posting import JournalEngine, AutoPostingService

    existing = JournalEntry.objects.filter(
        voucher_type='DEBIT_NOTE',
        reference_document=purchase_return.return_number,
        status='POSTED'
    ).first()
    if existing:
        return existing

    branch = purchase_return.branch
    date_ad = purchase_return.return_date

    ap_acc = AutoPostingService.get_or_create_control_account(
        branch, 'ACCOUNTS_PAYABLE', '2010', 'Accounts Payable (Creditors)', 'LIABILITY', 'CREDIT'
    )
    cash_acc = AutoPostingService.get_or_create_control_account(
        branch, 'CASH', '1010', 'Cash in Hand', 'ASSET', 'DEBIT'
    )
    inv_asset_acc = AutoPostingService.get_or_create_control_account(
        branch, 'INVENTORY_ASSET', '1040', 'Merchandise Inventory Asset', 'ASSET', 'DEBIT'
    )
    input_vat_acc = AutoPostingService.get_or_create_control_account(
        branch, 'INPUT_VAT', '1050', 'Input VAT 13%', 'ASSET', 'DEBIT'
    )

    lines: List[Dict[str, Any]] = []

    # 1. Debit: Accounts Payable (Deduct from Supplier Udhaari) OR Cash in Hand
    if purchase_return.refund_mode == 'CASH_REFUND':
        lines.append({
            'account': cash_acc,
            'debit': purchase_return.net_refund_amount,
            'credit': Decimal('0.00'),
            'supplier': purchase_return.supplier,
            'narration': f"Cash refund received on Debit Note {purchase_return.return_number}"
        })
    else:
        lines.append({
            'account': ap_acc,
            'debit': purchase_return.net_refund_amount,
            'credit': Decimal('0.00'),
            'supplier': purchase_return.supplier,
            'narration': f"Accounts Payable reduced on Debit Note {purchase_return.return_number}"
        })

    # 2. Credit: Merchandise Inventory Asset (at purchase value)
    lines.append({
        'account': inv_asset_acc,
        'debit': Decimal('0.00'),
        'credit': purchase_return.total_return_amount,
        'supplier': purchase_return.supplier,
        'narration': f"Merchandise inventory returned to vendor {purchase_return.supplier.company_name}"
    })

    # 3. Credit: Input VAT 13% (reversing input tax if applicable)
    if purchase_return.tax_amount > Decimal('0.00'):
        lines.append({
            'account': input_vat_acc,
            'debit': Decimal('0.00'),
            'credit': purchase_return.tax_amount,
            'supplier': purchase_return.supplier,
            'narration': f"Input VAT claimed reversal on Debit Note {purchase_return.return_number}"
        })

    narration = f"Purchase Return / Debit Note {purchase_return.return_number} to {purchase_return.supplier.company_name}"
    return JournalEngine.create_balanced_entry(
        voucher_type='DEBIT_NOTE',
        date_ad=date_ad,
        branch=branch,
        lines=lines,
        narration=narration,
        reference_doc=purchase_return.return_number,
        user=user or purchase_return.processed_by,
        auto_post=True
    )


# Ensure post_grn_journal and post_purchase_return_journal exist on auto_posting module
try:
    import apps.accounting.services.auto_posting as _ap_mod
    if not hasattr(_ap_mod, 'post_grn_journal'):
        if hasattr(_ap_mod, 'AutoPostingService') and hasattr(_ap_mod.AutoPostingService, 'post_grn_receipt'):
            _ap_mod.post_grn_journal = _ap_mod.AutoPostingService.post_grn_receipt
    if not hasattr(_ap_mod, 'post_purchase_return_journal'):
        _ap_mod.post_purchase_return_journal = _post_purchase_return_direct
except Exception:
    pass


class PurchaseService:
    """
    Core procurement engine managing Goods Received Notes (GRN):
    1. Enforces Value-Based Overhead Distribution: Shipping, freight, customs duties,
       and handling fees are allocated proportionally based on each item's monetary value
       rather than raw box count.
    2. Strict Serialized & Dual-IMEI Enforcement: Mandates that if N units of a smartphone
       are received, exactly N valid IMEI pairs (IMEI 1 & optional IMEI 2) must be scanned
       and verified against existing stock before inventory is updated.
    3. Thread-Safe Supplier Ledger Reconciliation: Atomically updates the supplier's
       outstanding balance with row-level locking (select_for_update) and logs ledger transactions.
    4. Product Master & Multi-Branch FIFO Updates: Adjusts landed cost, updates counter MRPs,
       records historical price fluctuations, and initializes serialized ItemInstances.
    5. Fail-Closed General Ledger Auto-Posting: Dispatches balanced vouchers upon GRN verification.
       If voucher creation fails, the transaction is rolled back completely.
    """

    @classmethod
    @transaction.atomic
    def process_grn_approval_and_stock_in(
        cls,
        grn: GoodsReceivedNote,
        user=None
    ) -> GoodsReceivedNote:
        """
        Main transactional entry point coordinating complete GRN verification and stock inward.
        """
        if grn.status == 'RECEIVED':
            raise ValidationError("This GRN voucher has already been verified and received.")

        items = list(grn.items.select_related('product', 'product__base_unit', 'unit_conversion').all())
        if not items:
            raise ValidationError("Cannot approve a GRN without line items. Please add at least one product.")

        # Step 1: Strict Pre-Validation of Serialized / Dual-IMEI Quantities & Collisions
        cls._validate_grn_lines(grn, items)

        # Step 2: Proportional Value-Based Overhead Allocation
        landed_overhead_rate, extra_charges = cls._allocate_landed_overhead(grn, items)

        # Step 3: Calculate Line Figures, Update Branch Stock, FIFO Batches & Master Rates
        gross_total, total_discount, total_tax = cls._calculate_and_apply_line_items(
            grn=grn,
            items=items,
            landed_overhead_rate=landed_overhead_rate,
            user=user
        )

        # Step 4: Finalize GRN Header Summaries & Due Debt
        net_total = cls._finalize_grn_header(
            grn=grn,
            gross_total=gross_total,
            total_discount=total_discount,
            total_tax=total_tax,
            extra_charges=extra_charges,
            user=user
        )

        # Step 5: Post to Supplier Ledger with Row-Level Locking & Audit Trail
        cls._post_supplier_ledger(grn=grn, net_total=net_total, user=user)

        # Step 6: Post General Ledger Double-Entry Journal (Debit Inventory Asset, Credit Accounts Payable)
        # Execution is strict; errors bubble up to enforce atomic rollback across stock and ledger.
        import apps.accounting.services.auto_posting as auto_posting_module
        if hasattr(auto_posting_module, 'post_grn_journal'):
            auto_posting_module.post_grn_journal(grn, user=user)
        elif hasattr(auto_posting_module, 'AutoPostingService'):
            service = auto_posting_module.AutoPostingService
            if hasattr(service, 'post_grn_journal'):
                service.post_grn_journal(grn, user=user)
            elif hasattr(service, 'post_grn_receipt'):
                service.post_grn_receipt(grn=grn, user=user)
            else:
                raise ValidationError("AutoPostingService has no post_grn_journal or post_grn_receipt implementation.")
        else:
            raise ValidationError("Accounting auto_posting module is unavailable for GRN journal posting.")

        return grn

    # =========================================================================
    # STEP 1: STRICT PRE-VALIDATION OF SERIALIZED QUANTITIES & IMEIS
    # =========================================================================

    @classmethod
    def _validate_grn_lines(cls, grn: GoodsReceivedNote, items: List[GRNItem]) -> None:
        """
        Strictly validates that every line item with IMEI/Serial tracking has an exact
        1-to-1 match between the required base unit quantity and the count of scanned IMEIs.
        Validates both IMEI 1 and IMEI 2:
        - Rejects internal duplicate IMEIs within the consignment.
        - Rejects devices where IMEI 1 equals IMEI 2.
        - Rejects collisions where either IMEI matches an already active IN_STOCK unit.
        """
        seen_imeis = set()

        for item in items:
            product = item.product
            factor = item.conversion_factor if item.conversion_factor > Decimal('0.000') else Decimal('1.000')
            base_qty = (item.purchased_quantity * factor).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)

            if base_qty <= Decimal('0.000'):
                raise ValidationError(f"Quantity for line item '{product.name}' must be greater than zero.")

            if product.requires_imei_tracking or product.requires_serial_tracking:
                if base_qty % 1 != 0:
                    raise ValidationError(
                        f"Fractional base quantity ({base_qty}) is not permitted for serialized item '{product.name}'. "
                        f"Handsets and serialized devices must be received in whole integer units."
                    )

                expected_units = int(base_qty)

                if not item.scanned_imei_list or not item.scanned_imei_list.strip():
                    raise ValidationError(
                        f"IMEI / Serial numbers are strictly required for '{product.name}'. "
                        f"Expected exactly {expected_units} unit identifier(s), but the scanned list is completely empty."
                    )

                imei_tokens = [t.strip() for t in re.split(r'[\n,;]+', item.scanned_imei_list) if t.strip()]
                scanned_count = len(imei_tokens)

                if scanned_count != expected_units:
                    raise ValidationError(
                        f"IMEI count mismatch for '{product.name}': Purchased quantity is {expected_units} unit(s), "
                        f"but received {scanned_count} IMEI entry/pair(s). "
                        f"Please scan exactly {expected_units} IMEI pair(s) before verifying inward stock."
                    )

                for token in imei_tokens:
                    parts = token.split('|')
                    im1 = parts[0].strip() if len(parts) > 0 and parts[0].strip() else None
                    im2 = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None

                    # Validate Primary IMEI 1
                    if im1:
                        if not im1.isdigit() and len(im1) >= 14:
                            raise ValidationError(f"Invalid IMEI 1 '{im1}' for '{product.name}'. IMEIs must be numeric digits.")

                        if im1 in seen_imeis:
                            raise ValidationError(
                                f"Duplicate IMEI 1 '{im1}' entered multiple times in the consignment for '{product.name}'."
                            )
                        seen_imeis.add(im1)

                        existing_im1 = ItemInstance.objects.filter(
                            Q(imei_1=im1) | Q(imei_2=im1),
                            status='IN_STOCK'
                        ).select_related('branch', 'product').first()

                        if existing_im1:
                            raise ValidationError(
                                f"Primary IMEI '{im1}' for '{product.name}' is already registered as IN_STOCK at branch "
                                f"'{existing_im1.branch.name}' (Product: {existing_im1.product.name}). "
                                f"Cannot inward duplicate active inventory."
                            )

                    # Validate Secondary IMEI 2
                    if im2:
                        if not im2.isdigit() and len(im2) >= 14:
                            raise ValidationError(f"Invalid IMEI 2 '{im2}' for '{product.name}'. IMEIs must be numeric digits.")

                        if im1 and im1 == im2:
                            raise ValidationError(
                                f"Invalid dual-IMEI pair on '{product.name}': IMEI 1 and IMEI 2 cannot be identical ('{im1}')."
                            )

                        if im2 in seen_imeis:
                            raise ValidationError(
                                f"Duplicate IMEI 2 '{im2}' entered multiple times in the consignment for '{product.name}'."
                            )
                        seen_imeis.add(im2)

                        existing_im2 = ItemInstance.objects.filter(
                            Q(imei_1=im2) | Q(imei_2=im2),
                            status='IN_STOCK'
                        ).select_related('branch', 'product').first()

                        if existing_im2:
                            raise ValidationError(
                                f"Secondary IMEI 2 '{im2}' for '{product.name}' is already registered as IN_STOCK at branch "
                                f"'{existing_im2.branch.name}' (Product: {existing_im2.product.name}). "
                                f"Cannot inward duplicate active inventory."
                            )

    # =========================================================================
    # STEP 2: PROPORTIONAL VALUE-BASED OVERHEAD ALLOCATION
    # =========================================================================

    @staticmethod
    def _allocate_landed_overhead(grn: GoodsReceivedNote, items: List[GRNItem]) -> Tuple[Decimal, Decimal]:
        """
        Calculates proportional value-weighted overhead distribution rate for freight,
        customs, and handling charges based on total merchandise value rather than raw unit counts.
        Formula:
            Item Line Value = Purchased Qty * Purchase Rate * (1 - Discount%)
            Total Merchandise Value = Sum(Item Line Values)
            Overhead Rate = Extra Charges / Total Merchandise Value
        """
        extra_charges = (
            (grn.extra_freight_charge or Decimal('0.00')) +
            (grn.customs_import_charge or Decimal('0.00')) +
            (grn.other_handling_charge or Decimal('0.00'))
        ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        total_merchandise_value = Decimal('0.00')
        for item in items:
            qty = item.purchased_quantity if item.purchased_quantity > Decimal('0.000') else Decimal('1.000')
            rate = item.purchase_rate or Decimal('0.00')
            disc_pct = item.discount_percent or Decimal('0.00')

            line_gross = (qty * rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            discount = (line_gross * (disc_pct / Decimal('100.00'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            taxable_line = line_gross - discount
            total_merchandise_value += taxable_line

        landed_overhead_rate = (
            (extra_charges / total_merchandise_value) if total_merchandise_value > Decimal('0.00') else Decimal('0.00')
        )
        return landed_overhead_rate, extra_charges

    # =========================================================================
    # STEP 3: CALCULATE LINE ITEMS, LANDED COSTS & UPDATE STOCK
    # =========================================================================

    @classmethod
    def _calculate_and_apply_line_items(
        cls,
        grn: GoodsReceivedNote,
        items: List[GRNItem],
        landed_overhead_rate: Decimal,
        user=None
    ) -> Tuple[Decimal, Decimal, Decimal]:
        """
        Computes line financials with value-weighted overhead allocation, updates live
        branch inventory counters, writes FIFO batches, and creates serialized ItemInstances.
        """
        gross_total = Decimal('0.00')
        total_discount = Decimal('0.00')
        total_tax = Decimal('0.00')

        for item in items:
            product = item.product
            factor = item.conversion_factor if item.conversion_factor > Decimal('0.000') else Decimal('1.000')
            base_qty = (item.purchased_quantity * factor).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)
            item.base_unit_quantity = base_qty

            line_gross = (item.purchased_quantity * item.purchase_rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            discount = (line_gross * ((item.discount_percent or Decimal('0.00')) / Decimal('100.00'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            taxable_line = line_gross - discount

            # Inward Tax Calculation
            line_tax = Decimal('0.00')
            if grn.is_vat_bill and item.is_vat_applicable and (item.vat_rate or Decimal('0.00')) > Decimal('0.00'):
                line_tax = (taxable_line * (item.vat_rate / Decimal('100.00'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            item.line_total = taxable_line + line_tax

            # Proportional Value-Weighted Landed Overhead Calculation
            line_landed_overhead = (taxable_line * landed_overhead_rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            total_line_landed_cost = taxable_line + line_landed_overhead
            item.unit_landed_cost = (total_line_landed_cost / base_qty).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) if base_qty > Decimal('0.000') else Decimal('0.00')
            item.save()

            gross_total += line_gross
            total_discount += discount
            total_tax += line_tax

            # A. Update Physical Branch Stock Level
            InventoryService.adjust_stock(
                product=product,
                branch=grn.branch,
                quantity_delta=base_qty,
                movement_type='PURCHASE',
                reference_doc=grn.grn_number,
                remarks=f"GRN Inward: {grn.supplier_bill_no} from {grn.supplier.company_name} on {grn.bill_date}",
                user=user,
                allow_negative=True
            )

            # B. Price Fluctuation Audit & Update Master Counter Selling Price
            batch_id = cls._update_product_master_and_batches(
                grn=grn,
                item=item,
                product=product,
                base_qty=base_qty,
                user=user
            )

            # C. Register Physical ItemInstance Records (Dual-IMEI & MDMS)
            cls._register_imei_instances(
                grn=grn,
                item=item,
                product=product,
                batch_id=batch_id
            )

        return gross_total, total_discount, total_tax

    @staticmethod
    def _update_product_master_and_batches(
        grn: GoodsReceivedNote,
        item: GRNItem,
        product: Product,
        base_qty: Decimal,
        user=None
    ) -> str:
        """
        Updates product master purchase price to the latest landed cost, adjusts MRP if provided,
        logs historical price transitions, and creates date-specific FIFO batches for non-serialized items.
        """
        old_cost = product.purchase_price
        old_sell = product.selling_price
        new_sell = item.new_selling_price or product.selling_price

        if item.unit_landed_cost != old_cost or new_sell != old_sell:
            ProductCostHistory.objects.create(
                product=product,
                date_effective=grn.bill_date,
                old_cost_price=old_cost,
                new_cost_price=item.unit_landed_cost,
                old_selling_price=old_sell,
                new_selling_price=new_sell,
                source_reference=grn.grn_number,
                changed_by=user,
                remarks=f"Inward GRN price update from {grn.supplier.company_name}"
            )

        product.purchase_price = item.unit_landed_cost
        if item.new_selling_price and item.new_selling_price > Decimal('0.00'):
            product.selling_price = item.new_selling_price
        product.save(update_fields=['purchase_price', 'selling_price', 'updated_at'])

        batch_id = f"BATCH-{grn.grn_number}-{product.id}"
        if not product.requires_imei_tracking and not product.requires_serial_tracking:
            ProductBatch.objects.create(
                batch_number=batch_id,
                product=product,
                branch=grn.branch,
                purchase_date=grn.bill_date,
                cost_price=item.unit_landed_cost,
                selling_price=item.new_selling_price or product.selling_price,
                quantity_received=base_qty,
                quantity_remaining=base_qty,
                supplier_name=grn.supplier.company_name,
                grn_reference=grn.grn_number
            )

        return batch_id

    @staticmethod
    def _register_imei_instances(
        grn: GoodsReceivedNote,
        item: GRNItem,
        product: Product,
        batch_id: str
    ) -> None:
        """
        Parses scanned IMEI tokens and creates physical ItemInstance records with
        NTA MDMS certification and individual warranty end dates.
        """
        if not (product.requires_imei_tracking or product.requires_serial_tracking) or not item.scanned_imei_list:
            return

        imei_tokens = [t.strip() for t in re.split(r'[\n,;]+', item.scanned_imei_list) if t.strip()]
        line_mdms = item.default_mdms_status if not grn.distributor_mdms_certified else 'REGISTERED_OFFICIAL'

        for token in imei_tokens:
            parts = token.split('|')
            im1 = parts[0].strip() if len(parts) > 0 and parts[0].strip() else None
            im2 = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
            sn = parts[2].strip() if len(parts) > 2 and parts[2].strip() else (
                token if product.requires_serial_tracking and not product.requires_imei_tracking else None
            )

            if im1 == '':
                im1 = None
            if im2 == '':
                im2 = None
            if sn == '':
                sn = None

            warranty_m = item.warranty_months or grn.warranty_months or product.warranty_months or 12
            w_start = grn.bill_date
            w_end = w_start + timedelta(days=warranty_m * 30) if warranty_m > 0 else None

            existing = ItemInstance.objects.filter(imei_1=im1).first() if im1 else None
            if not existing:
                is_dual_sim = getattr(product, 'sim_configuration', 'DUAL_SIM') in ['DUAL_SIM', 'ESIM_DUAL']
                pending_scan = is_dual_sim and (im2 is None)

                ItemInstance.objects.create(
                    product=product,
                    branch=grn.branch,
                    device_uid=f"DEV-{uuid.uuid4().hex[:12].upper()}",
                    imei_1=im1,
                    imei_2=im2,
                    imei_2_pending_scan=pending_scan,
                    serial_number=sn,
                    device_barcode=im1 or sn or product.barcode,
                    status='IN_STOCK',
                    condition='BRAND_NEW',
                    activation_status='SEALED_INACTIVE',
                    source_type='NEW_PURCHASE_GRN',
                    mdms_status=line_mdms,
                    mdms_verification_date=grn.bill_date,
                    mdms_remarks=f"GRN Inward: {grn.grn_number} | Invoice: {grn.supplier_bill_no}",
                    purchase_reference=grn.grn_number,
                    batch_reference=batch_id,
                    supplier_name=grn.supplier.company_name,
                    landed_cost=item.unit_landed_cost,
                    purchase_date=grn.bill_date,
                    warranty_start_date=w_start,
                    warranty_end_date=w_end,
                    warranty_remarks=f"Supplier Warranty: {item.warranty_provider or grn.warranty_provider or 'Distributor'}"
                )

    # =========================================================================
    # STEP 4: FINALIZE GRN HEADER TOTALS
    # =========================================================================

    @staticmethod
    def _finalize_grn_header(
        grn: GoodsReceivedNote,
        gross_total: Decimal,
        total_discount: Decimal,
        total_tax: Decimal,
        extra_charges: Decimal,
        user=None
    ) -> Decimal:
        """Computes and locks the final voucher totals."""
        net_total = (gross_total - total_discount + total_tax + extra_charges).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        total_landed = (gross_total - total_discount + extra_charges).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        grn.gross_amount = gross_total
        grn.discount_amount = total_discount
        grn.vat_amount = total_tax
        grn.total_landed_cost = total_landed
        grn.net_total_amount = net_total

        paid = grn.paid_amount or Decimal('0.00')
        grn.due_amount = max(Decimal('0.00'), net_total - paid)
        grn.status = 'RECEIVED'
        grn.received_by = user
        grn.save()

        return net_total

    # =========================================================================
    # STEP 5: AUTOMATIC SUPPLIER LEDGER & BALANCE UPDATE
    # =========================================================================

    @staticmethod
    def _post_supplier_ledger(
        grn: GoodsReceivedNote,
        net_total: Decimal,
        user=None
    ) -> None:
        """
        Atomically updates the supplier's outstanding ledger balance using database
        row-level locking (select_for_update), posts the ledger transaction, and generates an audit log.
        """
        supplier = Supplier.objects.select_for_update().get(pk=grn.supplier_id)
        prev_bal = supplier.current_balance or Decimal('0.00')
        new_bal = prev_bal + grn.due_amount

        supplier.current_balance = new_bal
        supplier.last_purchase_date = grn.bill_date
        supplier.save(update_fields=['current_balance', 'last_purchase_date', 'updated_at'])

        # Post Supplier Udhaari Ledger Entry
        SupplierUdhaariLedger.objects.create(
            supplier=supplier,
            branch=grn.branch,
            transaction_type='PURCHASE_BILL',
            amount=net_total,
            previous_balance=prev_bal,
            resulting_balance=new_bal,
            payment_mode='CASH' if (grn.paid_amount or Decimal('0.00')) > Decimal('0.00') else 'OTHER',
            reference_number=grn.grn_number,
            recorded_by=user,
            remarks=(
                f"GRN Received. Bill No: {grn.supplier_bill_no} "
                f"(Total: Rs. {net_total:.2f}, Paid: Rs. {grn.paid_amount:.2f}, Due: Rs. {grn.due_amount:.2f})"
            )
        )

        AuditLog.objects.create(
            user=user,
            branch=grn.branch,
            action_type='CREATE',
            module='PurchaseGRN',
            object_repr=grn.grn_number,
            details={
                'supplier': supplier.company_name,
                'net_amount': str(net_total),
                'landed_cost': str(grn.total_landed_cost),
                'freight_and_customs': str(grn.extra_freight_charge + grn.customs_import_charge + grn.other_handling_charge),
                'mdms_certified': grn.distributor_mdms_certified,
                'items_count': grn.items.count(),
                'prev_supplier_balance': str(prev_bal),
                'new_supplier_balance': str(new_bal)
            }
        )


class PurchaseReturnService:
    """
    Commercial Purchase Return / Debit Note Processing Engine.
    Executes atomic merchandise return to suppliers:
    1. Pre-validates that stock and serialized IMEIs are currently available in IN_STOCK status at the branch.
    2. Deducts physical inventory from BranchStock via InventoryService.adjust_stock.
    3. For serialized smartphones (IMEI), marks ItemInstance status as 'RETURNED_TO_SUPPLIER'
       so it is permanently removed from active sellable inventory.
    4. Deducts quantity from active non-serialized inventory FIFO batches.
    5. If refund_mode is 'DEDUCT_FROM_BALANCE', atomically reduces the supplier's
       outstanding debt balance in Supplier and records a 'PURCHASE_RETURN' transaction in SupplierUdhaariLedger.
    6. If refund_mode is 'CASH_REFUND', logs the cash inflow in the supplier ledger.
    7. Records detailed movement logs in StockMovementLog and forensics in AuditLog.
    8. Automatically posts double-entry General Ledger reversal vouchers fail-closed.
    """

    @staticmethod
    def generate_return_number(branch: Branch) -> str:
        """
        Atomically allocates a strictly unique sequential debit note number
        using database row-level locking.
        """
        try:
            return BranchDocumentSequence.get_next_sequence_number(
                branch=branch,
                document_type='PURCHASE_RETURN',
                prefix_override=f"DN-{branch.code}",
                padding=6
            )
        except Exception:
            return f"DN-{branch.code}-{uuid.uuid4().hex[:6].upper()}"

    @classmethod
    @transaction.atomic
    def process_purchase_return(
        cls,
        purchase_return: PurchaseReturn,
        user=None
    ) -> PurchaseReturn:
        """
        Main transactional entry point coordinating purchase return approval,
        inventory deduction, IMEI locking, supplier udhaari ledger reconciliation,
        and double-entry General Ledger voucher posting.
        """
        items = list(purchase_return.items.select_related('product', 'product__base_unit', 'unit_conversion').all())
        if not items:
            raise ValidationError("Cannot process a purchase return without line items. Please add at least one product.")

        # Step 1: Pre-validation of Stock Availability & Serialized IMEI Ownership
        cls._validate_return_items(purchase_return, items)

        # Step 2: Deduct Stock, Batches & Update Serialized IMEI Units
        total_return_val, total_tax_val = cls._deduct_stock_and_update_imeis(
            purchase_return=purchase_return,
            items=items,
            user=user
        )

        net_refund_val = (total_return_val + total_tax_val).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        purchase_return.total_return_amount = total_return_val
        purchase_return.tax_amount = total_tax_val
        purchase_return.net_refund_amount = net_refund_val
        purchase_return.status = 'CONFIRMED'
        purchase_return.processed_by = user
        purchase_return.save(update_fields=['total_return_amount', 'tax_amount', 'net_refund_amount', 'status', 'processed_by', 'updated_at'])

        # Step 3: Settle Supplier Debt Ledger & Post Financial Adjustment
        cls._reconcile_supplier_ledger(purchase_return, net_refund_val, user)

        # Step 4: Post Double-Entry Journal to General Ledger (Debit AP/Cash, Credit Inventory Asset & Input VAT)
        # Execution is strict; errors bubble up to enforce atomic rollback across stock and ledger.
        import apps.accounting.services.auto_posting as auto_posting_module
        if hasattr(auto_posting_module, 'post_purchase_return_journal'):
            auto_posting_module.post_purchase_return_journal(purchase_return, user=user)
        elif hasattr(auto_posting_module, 'AutoPostingService'):
            service = auto_posting_module.AutoPostingService
            if hasattr(service, 'post_purchase_return_journal'):
                service.post_purchase_return_journal(purchase_return, user=user)
            elif hasattr(service, 'post_purchase_return'):
                service.post_purchase_return(purchase_return, user=user)
            else:
                _post_purchase_return_direct(purchase_return, user=user)
        else:
            _post_purchase_return_direct(purchase_return, user=user)

        # Step 5: Audit Trail
        AuditLog.objects.create(
            user=user,
            branch=purchase_return.branch,
            action_type='UPDATE',
            module='PurchaseReturn',
            object_repr=purchase_return.return_number,
            details={
                'supplier': purchase_return.supplier.company_name,
                'net_refund_amount': str(net_refund_val),
                'refund_mode': purchase_return.refund_mode,
                'items_count': len(items),
                'original_bill_reference': purchase_return.original_bill_reference or "",
            }
        )

        return purchase_return

    @classmethod
    def _validate_return_items(cls, purchase_return: PurchaseReturn, items: List[PurchaseReturnItem]) -> None:
        """
        Strictly verifies that:
        - Quantities are strictly positive.
        - Branch has sufficient sellable stock to cover the return without going negative.
        - For serialized items, the specified IMEIs currently exist in available IN_STOCK status at this branch.
        """
        for item in items:
            product = item.product
            factor = item.conversion_factor if item.conversion_factor > Decimal('0.000') else Decimal('1.000')
            base_qty = (item.returned_quantity * factor).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)

            if base_qty <= Decimal('0.000'):
                raise ValidationError(f"Return quantity for item '{product.name}' must be greater than zero.")

            # Check branch stock level
            branch_stock = BranchStock.objects.filter(
                branch=purchase_return.branch,
                product=product
            ).first()

            available_stock = branch_stock.quantity if branch_stock else Decimal('0.000')
            if available_stock < base_qty:
                raise ValidationError(
                    f"Insufficient stock for '{product.name}' at {purchase_return.branch.name}. "
                    f"Available in warehouse: {available_stock} {product.base_unit.code}, Requested return: {base_qty}."
                )

            # Validate serialized IMEIs
            if product.requires_imei_tracking or product.requires_serial_tracking:
                expected_units = int(base_qty)
                raw_imei = item.returned_imei_list or ''
                tokens = [t.strip() for t in re.split(r'[\n,;]+', raw_imei) if t.strip()]

                if len(tokens) != expected_units:
                    raise ValidationError(
                        f"IMEI Count Mismatch on '{product.name}': Returning {expected_units} unit(s), "
                        f"but received {len(tokens)} IMEI(s). Exactly {expected_units} IMEI(s) are required."
                    )

                for token in tokens:
                    clean_imei = token.split('|')[0].strip()
                    instance = ItemInstance.objects.filter(
                        Q(imei_1=clean_imei) | Q(imei_2=clean_imei) | Q(serial_number=clean_imei),
                        branch=purchase_return.branch,
                        status='IN_STOCK'
                    ).first()

                    if not instance:
                        raise ValidationError(
                            f"Device with IMEI / Serial '{clean_imei}' for product '{product.name}' "
                            f"was not found in available active stock at {purchase_return.branch.name}."
                        )

    @classmethod
    def _deduct_stock_and_update_imeis(
        cls,
        purchase_return: PurchaseReturn,
        items: List[PurchaseReturnItem],
        user=None
    ) -> Tuple[Decimal, Decimal]:
        """
        Deducts physical branch stock, deducts non-serialized FIFO batches,
        locks serialized phone instances as 'RETURNED_TO_SUPPLIER', and computes financial totals.
        """
        total_return_val = Decimal('0.00')
        total_tax_val = Decimal('0.00')

        for item in items:
            product = item.product
            factor = item.conversion_factor if item.conversion_factor > Decimal('0.000') else Decimal('1.000')
            base_qty = (item.returned_quantity * factor).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)
            item.base_unit_quantity = base_qty

            gross = (item.returned_quantity * (item.purchase_rate or Decimal('0.00'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            tax_rate = item.tax_rate or Decimal('0.00')
            tax = (gross * (tax_rate / Decimal('100.00'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) if tax_rate > Decimal('0.00') else Decimal('0.00')
            line_tot = gross + tax

            item.tax_amount = tax
            item.line_total = line_tot
            item.save(update_fields=['base_unit_quantity', 'tax_amount', 'line_total', 'updated_at'])

            total_return_val += gross
            total_tax_val += tax

            # 1. Deduct sellable stock from BranchStock via InventoryService
            InventoryService.adjust_stock(
                product=product,
                branch=purchase_return.branch,
                quantity_delta=-base_qty,
                movement_type='RMA_VENDOR_DISPATCH',
                reference_doc=purchase_return.return_number,
                imei_or_serial=item.returned_imei_list or "",
                remarks=(
                    f"Commercial Purchase Return (Debit Note: {purchase_return.return_number}) "
                    f"to {purchase_return.supplier.company_name}. Reason: {item.return_reason or 'Stock Return'}"
                ),
                user=user,
                allow_negative=False
            )

            # 2. Update Serialized Phone ItemInstances to RETURNED_TO_SUPPLIER
            if product.requires_imei_tracking or product.requires_serial_tracking:
                if item.returned_imei_list:
                    tokens = [t.strip() for t in re.split(r'[\n,;]+', item.returned_imei_list) if t.strip()]
                    for token in tokens:
                        clean_imei = token.split('|')[0].strip()
                        instance = ItemInstance.objects.select_for_update().filter(
                            Q(imei_1=clean_imei) | Q(imei_2=clean_imei) | Q(serial_number=clean_imei),
                            branch=purchase_return.branch,
                            status='IN_STOCK'
                        ).first()

                        if instance:
                            instance.status = 'RETURNED_TO_SUPPLIER'
                            instance.save(update_fields=['status', 'updated_at'])
                            if not item.item_instance:
                                item.item_instance = instance
                                item.save(update_fields=['item_instance', 'updated_at'])

            # 3. Deduct from active non-serialized inventory FIFO batches
            else:
                batches = ProductBatch.objects.select_for_update().filter(
                    product=product,
                    branch=purchase_return.branch,
                    is_depleted=False
                ).order_by('-purchase_date', '-created_at')

                qty_needed = base_qty
                for batch in batches:
                    if batch.quantity_remaining >= qty_needed:
                        batch.quantity_remaining -= qty_needed
                        batch.is_depleted = (batch.quantity_remaining <= Decimal('0.000'))
                        batch.save(update_fields=['quantity_remaining', 'is_depleted', 'updated_at'])
                        qty_needed = Decimal('0.000')
                        break
                    else:
                        qty_needed -= batch.quantity_remaining
                        batch.quantity_remaining = Decimal('0.000')
                        batch.is_depleted = True
                        batch.save(update_fields=['quantity_remaining', 'is_depleted', 'updated_at'])

        return total_return_val, total_tax_val

    @classmethod
    def _reconcile_supplier_ledger(
        cls,
        purchase_return: PurchaseReturn,
        net_refund_amount: Decimal,
        user=None
    ) -> None:
        """
        Adjusts supplier debt ledger according to selected refund mode:
        - DEDUCT_FROM_BALANCE: Reduces supplier current balance (owing less to supplier).
        - CASH_REFUND: Logs cash inflow from supplier without altering debt balance.
        - REPLACEMENT: Logs return awaiting replacement consignment.
        """
        supplier = Supplier.objects.select_for_update().get(pk=purchase_return.supplier_id)
        prev_bal = supplier.current_balance or Decimal('0.00')

        if purchase_return.refund_mode == 'DEDUCT_FROM_BALANCE':
            new_bal = prev_bal - net_refund_amount
            supplier.current_balance = new_bal
            supplier.save(update_fields=['current_balance', 'updated_at'])

            SupplierUdhaariLedger.objects.create(
                supplier=supplier,
                branch=purchase_return.branch,
                transaction_type='PURCHASE_RETURN',
                amount=net_refund_amount,
                previous_balance=prev_bal,
                resulting_balance=new_bal,
                payment_mode='OTHER',
                reference_number=purchase_return.return_number,
                recorded_by=user,
                remarks=(
                    f"Debit Note {purchase_return.return_number}: Stock returned to supplier "
                    f"{supplier.company_name}. Balance deducted by Rs. {net_refund_amount:.2f}. "
                    f"Original Ref: {purchase_return.original_bill_reference or '-'}"
                )
            )

        elif purchase_return.refund_mode == 'CASH_REFUND':
            SupplierUdhaariLedger.objects.create(
                supplier=supplier,
                branch=purchase_return.branch,
                transaction_type='PURCHASE_RETURN',
                amount=net_refund_amount,
                previous_balance=prev_bal,
                resulting_balance=prev_bal,
                payment_mode='CASH',
                reference_number=purchase_return.return_number,
                recorded_by=user,
                remarks=(
                    f"Debit Note {purchase_return.return_number}: Cash refund received of "
                    f"Rs. {net_refund_amount:.2f} from {supplier.company_name}."
                )
            )

        elif purchase_return.refund_mode == 'REPLACEMENT':
            SupplierUdhaariLedger.objects.create(
                supplier=supplier,
                branch=purchase_return.branch,
                transaction_type='PURCHASE_RETURN',
                amount=net_refund_amount,
                previous_balance=prev_bal,
                resulting_balance=prev_bal,
                payment_mode='OTHER',
                reference_number=purchase_return.return_number,
                recorded_by=user,
                remarks=(
                    f"Debit Note {purchase_return.return_number}: Stock returned awaiting replacement "
                    f"consignment from {supplier.company_name} (Value: Rs. {net_refund_amount:.2f})."
                )
            )