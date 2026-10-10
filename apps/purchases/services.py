"""
Purchase GRN & Commercial Purchase Return (Debit Note) Services.

Key Architectural Improvements & Mathematical Integrity:
1. Hardened Line-to-Header VAT Snapshot Chain:
   For every GRN:
       GRNItem.tax_amount (Item-level VAT)
             ↓
       GoodsReceivedNote.vat_amount (Header VAT = Exact sum of line item VATs)
             ↓
       Input VAT Report (Annex 7 Purchase Register / Day-Wise VAT Ledger)
             ↓
       Input VAT GL (Account 1410 Inward Tax Claim)
   
   For every Purchase Return:
       PurchaseReturnItem.tax_amount (Return Item-level VAT)
             ↓
       PurchaseReturn.tax_amount (Return Header VAT = Exact sum of line tax amounts)
             ↓
       Input VAT Reversal (Annex 7 deduction from claimable input tax)
             ↓
       VAT Report Reconciliation
             ↓
       GL Reversal (Account 1410 Credited)

2. Spot-Payment Udhaari Ledger Continuity (Fixes Spot-Payment Debt Inflation):
   - When a GRN is inwarded with partial or full cash/bank payment on delivery (`paid_amount > 0`),
     `_post_supplier_ledger` generates two linked, sequential entries in `SupplierUdhaariLedger`:
     * Entry 1 (`PURCHASE_BILL`): Records the full net invoice amount (`net_total_amount`),
       advancing the intermediate balance.
     * Entry 2 (`PAYMENT`): Records the immediate spot disbursement (`paid_amount`),
       reducing the supplier's balance down to the true net due debt (`due_amount`).
   - Guarantees that automated sub-ledger audits and `Supplier.recalculate_balance_from_ledger()`
     preserve cash disbursements permanently without resetting debt to the gross invoice total.

3. Accurate Dual-Pot & Item-Level VAT Application:
   - Segregates line items into two distinct pots:
     * Taxable Pot: Items with locked 13.00% VAT.
     * Non-Taxable Pot: Items with locked 0.00% VAT (Exempt / PAN bills).
   - Shields 0% tax-exempt or zero-rated items from tax additions, even within mixed consignments.
   - Reconciles total consignment VAT as the exact sum of line-level VAT amounts.

4. Coherent VAT-Inclusive Line Pre-Tax Extraction:
   - When `vat_handling_mode == 'INCLUSIVE'` and the item is 13% taxable, extracts the pre-tax base
     using the exact statutory divisor `1.13` (`Rate ÷ 1.13` and `Discount ÷ 1.13`).
   - Items with 0% VAT remain untouched (divisor 1.00).
   - Reconstitutes `gross_amount` on an identical pre-tax basis (`taxable_extracted + pre_tax_discount`),
     eliminating mathematical distortion of pre-tax gross values and effective discount percentages.

5. 5-Tier Proportional Landed Cost Overhead Allocation:
   - Distributes all 5 overhead categories (Freight, Customs Duty, Handling & Unloading,
     Transit Insurance, and Other Overheads) proportionally across lines based on net pre-tax merchandise value.
   - Accurately establishes the unit landed cost for inventory COGS asset valuation, insulating
     recoverable 13% input VAT from physical stock valuation.

6. Custom Physical Batch Number Persistence:
   - Preserves user-entered batch identifiers (`item.batch_number`, e.g. BT-2026-A1) on `ProductBatch`
     and links them to individual `ItemInstance` records instead of falling back to computer-generated hashes.

7. Save Draft vs. Final Verify Split:
   - `save_grn_draft()`: Calculates line financials, taxes, and landed costs without adjusting stock,
     without creating serial instances, and without touching accounting ledgers.
   - `process_grn_approval_and_stock_in()`: Performs strict validation, updates physical warehouse stock,
     registers IMEI instances, creates FIFO batches, updates supplier debt, and posts double-entry GL journals.

8. Master-Switch Sensitive Serialized & Dual-IMEI Enforcement:
   - In Strict Mode (`enforce_imei_tracking=True`): Mandates exact 1-to-1 match between handset quantities and scanned IMEIs on approval.
   - In Backlog Mode (`enforce_imei_tracking=False`): Allows phone inward entry without IMEIs, creating FIFO ProductBatches.
   - Non-serialized accessories always bypass serial checks.

9. Dedicated Post-Receipt IMEI Linker (`attach_imeis_to_received_grn`):
   - Safely links physical 15-digit IMEIs to already-received GRNs.
   - Strictly enforces zero stock inflation (does not touch BranchStock quantity).
   - Strictly preserves accounting journals and supplier balances.
   - Enforces system-wide IMEI uniqueness and captures Dual-SIM statuses.
"""

import re
import uuid
import logging
from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime, timedelta
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
from apps.core.models import SystemConfiguration, AuditLog
from apps.core.nepali_calendar import NepaliCalendar
from apps.core.utils.nepali_date_converter import parse_bs_date_components

logger = logging.getLogger(__name__)

# =============================================================================
# GENERAL LEDGER DISPATCHER BRIDGES (STRICT TRANSACTIONAL INTEGRITY)
# =============================================================================
def _post_purchase_return_direct(purchase_return: PurchaseReturn, user=None):
    """
    Direct General Ledger poster for commercial purchase returns (Debit Notes).
    - Debit: Accounts Payable (Supplier Ledger) OR Cash in Hand (if cash refund)
    - Credit: Merchandise Inventory Asset (at purchase return value)
    - Credit: Input VAT 13% (reversing input tax if tax invoice by purchase_return.tax_amount)
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
        branch, 'ACCOUNTS_PAYABLE', '2110', 'Accounts Payable (Trade Creditors)', 'LIABILITY', 'CREDIT'
    )
    cash_acc = AutoPostingService.get_or_create_control_account(
        branch, 'CASH', '1110', 'Cash in Hand (Main Drawer)', 'ASSET', 'DEBIT'
    )
    inv_asset_acc = AutoPostingService.get_or_create_control_account(
        branch, 'INVENTORY_ASSET', '1310', 'Merchandise Inventory Asset', 'ASSET', 'DEBIT'
    )
    input_vat_acc = AutoPostingService.get_or_create_control_account(
        branch, 'INPUT_VAT', '1410', 'Input VAT 13%', 'ASSET', 'DEBIT'
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

    # 2. Credit: Merchandise Inventory Asset (at purchase return value)
    lines.append({
        'account': inv_asset_acc,
        'debit': Decimal('0.00'),
        'credit': purchase_return.total_return_amount,
        'supplier': purchase_return.supplier,
        'narration': f"Merchandise inventory returned to vendor {purchase_return.supplier.company_name}"
    })

    # 3. Credit: Input VAT 13% (reversing exact input tax claimed on return items)
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

# Ensure dynamic hooks exist on auto_posting module
try:
    import apps.accounting.services.auto_posting as _ap_mod
    if not hasattr(_ap_mod, 'post_grn_journal'):
        if hasattr(_ap_mod, 'AutoPostingService') and hasattr(_ap_mod.AutoPostingService, 'post_grn_receipt'):
            _ap_mod.post_grn_journal = _ap_mod.AutoPostingService.post_grn_receipt
    if not hasattr(_ap_mod, 'post_purchase_return_journal'):
        _ap_mod.post_purchase_return_journal = _post_purchase_return_direct
except Exception:
    pass

# =============================================================================
# PURCHASE & GRN SERVICE ENGINE
# =============================================================================
class PurchaseService:
    """
    Core Procurement & Goods Received Notes (GRN) Processing Engine.
    Executes mathematically strict, Nepal tax-compliant procurement workflows.
    """

    # =========================================================================
    # PATH 1: SAVE DRAFT (NO STOCK MODIFICATION, NO GL POSTING)
    # =========================================================================
    @classmethod
    @transaction.atomic
    def save_grn_draft(
        cls,
        grn: GoodsReceivedNote,
        items: Optional[List[GRNItem]] = None,
        user=None
    ) -> GoodsReceivedNote:
        """
        Saves an in-progress GRN voucher as a draft.
        - Synchronizes dates and derives the Nepali Fiscal Year.
        - Executes financial valuation and prorates 5-tier overheads on line items.
        - Saves line tax_amount snapshots.
        - Leaves stock counters untouched, skips serial creation, and skips GL journals.
        """
        if grn.status == 'RECEIVED':
            raise ValidationError("Cannot revert an already received and approved GRN to draft.")

        cls._harmonize_grn_dates(grn)

        if items is None:
            items = list(grn.items.select_related('product', 'product__base_unit', 'unit_conversion').all())

        # Mathematical Valuation & Landed Proration on in-memory line items
        cls._calculate_financials_only(grn=grn, items=items)

        grn.status = 'DRAFT'
        grn.save()

        # Save line item projections with tax_amount persisted
        for item in items:
            item.grn = grn
            item.save()

        AuditLog.objects.create(
            user=user,
            branch=grn.branch,
            action_type='UPDATE' if grn.pk else 'CREATE',
            module='PurchaseGRN',
            object_repr=grn.grn_number,
            details={
                'action': 'SAVE_DRAFT',
                'supplier': grn.supplier.company_name if grn.supplier else 'None',
                'bill_no': grn.supplier_bill_no,
                'challan_no': grn.challan_no or '',
                'gross_amount': str(grn.gross_amount),
                'taxable_amount': str(grn.taxable_amount),
                'vat_amount': str(grn.vat_amount),
                'total_landed_cost': str(grn.total_landed_cost),
                'net_total_amount': str(grn.net_total_amount),
                'status': 'DRAFT'
            }
        )

        return grn

    # =========================================================================
    # PATH 2: PROCESS APPROVAL & STOCK INWARD
    # =========================================================================
    @classmethod
    @transaction.atomic
    def process_grn_approval_and_stock_in(
        cls,
        grn: GoodsReceivedNote,
        user=None
    ) -> GoodsReceivedNote:
        """
        Main transactional entry point coordinating complete GRN verification:
        1. Dates & Fiscal Year check.
        2. Serialized / Dual-IMEI validation.
        3. Item-by-item VAT snapshot & header VAT summation invariant:
           GRNItem.tax_amount -> GRN.vat_amount.
        4. Inward stock counters and FIFO batch creation.
        5. Supplier debt ledger reconciliation with spot-payment continuity.
        6. General Ledger Input VAT (1410) journal posting.
        """
        if grn.status == 'RECEIVED':
            raise ValidationError("This GRN voucher has already been verified and received.")

        items = list(grn.items.select_related('product', 'product__base_unit', 'unit_conversion').all())
        if not items:
            raise ValidationError("Cannot approve a GRN without line items. Please add at least one product.")

        # Step 0: Ensure Historical Date and Fiscal Year Integrity
        cls._harmonize_grn_dates(grn)

        # Step 1: Pre-Validation of Serialized / Dual-IMEI Quantities & Master Setting Sensitivity
        cls._validate_grn_lines(grn, items)

        # Step 2: Full Mathematical Valuation, Line VAT Snapshotting, Landed Proration & Stock Inward
        cls._calculate_and_apply_financials_and_stock(
            grn=grn,
            items=items,
            user=user
        )

        # Step 3: Post to Supplier Ledger with Row-Level Locking & Historical Dates (Includes Spot Payment Entry)
        cls._post_supplier_ledger(grn=grn, user=user)

        # Step 4: Post General Ledger Double-Entry Journal (Debits Input VAT 1410 by grn.vat_amount)
        cls._post_gl_journal(grn=grn, user=user)

        return grn

    # =========================================================================
    # STEP 0: DATE & FISCAL YEAR HARMONIZATION
    # =========================================================================
    @staticmethod
    def _harmonize_grn_dates(grn: GoodsReceivedNote) -> None:
        """
        Ensures that grn.bill_date (AD), grn.bill_date_bs (BS), grn.fiscal_year,
        and challan dates are accurately synchronized before inventory records are created.
        """
        # Bill Date Synchronization
        if grn.bill_date_bs and str(grn.bill_date_bs).strip():
            try:
                bs_y, bs_m, bs_d = parse_bs_date_components(str(grn.bill_date_bs).strip())
                target_ad = NepaliCalendar.bs_to_ad(bs_y, bs_m, bs_d)
                grn.bill_date = target_ad
                grn.bill_date_bs = f"{bs_y:04d}-{bs_m:02d}-{bs_d:02d}"
                if not grn.fiscal_year:
                    grn.fiscal_year = NepaliCalendar.get_fiscal_year(bs_y, bs_m)
            except Exception as e:
                logger.warning(f"[PurchaseService] Could not parse grn.bill_date_bs '{grn.bill_date_bs}': {e}")
        elif grn.bill_date:
            ad_date = grn.bill_date.date() if isinstance(grn.bill_date, datetime) else grn.bill_date
            grn.bill_date = ad_date
            try:
                bs_y, bs_m, bs_d = NepaliCalendar.ad_to_bs(ad_date)
                if not grn.bill_date_bs:
                    grn.bill_date_bs = NepaliCalendar.format_bs(bs_y, bs_m, bs_d, lang='en')
                if not grn.fiscal_year:
                    grn.fiscal_year = NepaliCalendar.get_fiscal_year(bs_y, bs_m)
            except Exception as e:
                logger.warning(f"[PurchaseService] Could not convert grn.bill_date to BS: {e}")

        # Challan Date Synchronization
        if grn.challan_date_bs and str(grn.challan_date_bs).strip():
            try:
                c_y, c_m, c_d = parse_bs_date_components(str(grn.challan_date_bs).strip())
                grn.challan_date = NepaliCalendar.bs_to_ad(c_y, c_m, c_d)
                grn.challan_date_bs = f"{c_y:04d}-{c_m:02d}-{c_d:02d}"
            except Exception as e:
                logger.warning(f"[PurchaseService] Could not parse grn.challan_date_bs '{grn.challan_date_bs}': {e}")
        elif grn.challan_date:
            c_ad = grn.challan_date.date() if isinstance(grn.challan_date, datetime) else grn.challan_date
            grn.challan_date = c_ad
            try:
                c_y, c_m, c_d = NepaliCalendar.ad_to_bs(c_ad)
                if not grn.challan_date_bs:
                    grn.challan_date_bs = NepaliCalendar.format_bs(c_y, c_m, c_d, lang='en')
            except Exception:
                pass

    # =========================================================================
    # STEP 1: SERIALIZED QUANTITIES & MASTER SETTING VALIDATION
    # =========================================================================
    @classmethod
    def _validate_grn_lines(cls, grn: GoodsReceivedNote, items: List[GRNItem]) -> None:
        """
        Validates serialized handset lines consulting the master configuration switch:
        - When enforce_imei_tracking is ON: Strictly mandates an exact 1-to-1 match
          between purchased whole integer units and scanned IMEIs.
        - When enforce_imei_tracking is OFF (Backlog Mode): If no IMEIs are supplied,
          cleanly bypasses the check, permitting historical quantity-only purchase entry.
        - If IMEIs are supplied in either mode: Validates format, internal duplication,
          and rejects collisions against already active IN_STOCK items.
        - Non-phone accessories always bypass serial checks.
        """
        sys_config = SystemConfiguration.get_solo()
        enforce_imei = getattr(sys_config, 'enforce_imei_tracking', True)

        seen_imeis = set()

        for item in items:
            product = item.product
            factor = item.conversion_factor if (item.conversion_factor and item.conversion_factor > Decimal('0.000')) else Decimal('1.000')
            qty = item.purchased_quantity if (item.purchased_quantity and item.purchased_quantity > Decimal('0.000')) else Decimal('1.000')
            base_qty = (qty * factor).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)

            if base_qty <= Decimal('0.000'):
                raise ValidationError(f"Quantity for line item '{product.name}' must be greater than zero.")

            if product.requires_imei_tracking or product.requires_serial_tracking:
                if base_qty % 1 != 0:
                    raise ValidationError(
                        f"Fractional base quantity ({base_qty}) is not permitted for serialized item '{product.name}'. "
                        f"Handsets and serialized devices must be received in whole integer units."
                    )

                expected_units = int(base_qty)
                scanned_raw = (item.scanned_imei_list or '').strip()
                has_imeis = bool(scanned_raw)

                # BACKLOG MODE: If IMEI enforcement is OFF and staff provided no IMEIs, bypass
                if not enforce_imei and not has_imeis:
                    continue

                if not has_imeis:
                    raise ValidationError(
                        f"IMEI / Serial numbers are strictly required for '{product.name}'. "
                        f"Expected exactly {expected_units} unit identifier(s), but the scanned list is completely empty. "
                        f"(You can disable 'Enforce Mandatory Handset IMEI Tracking' in Settings to enter backlog purchase bills without IMEIs)."
                    )

                imei_tokens = [t.strip() for t in re.split(r'[\n,;]+', scanned_raw) if t.strip()]
                scanned_count = len(imei_tokens)

                if scanned_count != expected_units:
                    raise ValidationError(
                        f"IMEI count mismatch for '{product.name}': Purchased quantity is {expected_units} unit(s), "
                        f"but received {scanned_count} IMEI entry/pair(s). "
                        f"Please scan exactly {expected_units} IMEI pair(s), or leave completely blank in Backlog Mode."
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
    # STEP 2: MATHEMATICAL VALUATION, DUAL-POT OVERHEAD & HARDENED VAT INVARIANT
    # =========================================================================
    @classmethod
    def _calculate_financials_only(
        cls,
        grn: GoodsReceivedNote,
        items: List[GRNItem]
    ) -> Decimal:
        """
        Executes unified mathematical valuation across line items and whole-bill overheads:
        - Segregates items into Taxable Pot (13% VAT) vs. Non-Taxable Pot (0% VAT).
        - In INCLUSIVE mode: Extracts pre-tax base using `Rate ÷ 1.13` strictly on 13% taxable items.
          Items with 0% VAT remain untouched.
        - Prorates whole-bill discount proportionally across pre-tax merchandise bases.
        - Calculates line VAT strictly per item: `item.tax_amount = line_vat`.
        - Sums all line-level `tax_amount` values into `grn.vat_amount`:
          GRN VAT = Item 1 VAT + Item 2 VAT + Item 3 VAT + ...
        - Total Landed Cost = Net Taxable Base + Non-Taxable Base + 5 Overheads.
        - Net Total Payable = Net Taxable Base + Non-Taxable Base + 13% VAT Amount.
        - Distributes bill discounts and overhead expenses across items to compute exact unit landed cost.
        """
        total_line_gross = Decimal('0.00')
        total_line_discount = Decimal('0.00')
        line_meta = []

        # Phase 1: Line Item Gross, Line Discounts & Initial Pre-Tax Extraction
        for item in items:
            qty = item.purchased_quantity if (item.purchased_quantity and item.purchased_quantity > Decimal('0.000')) else Decimal('1.000')
            raw_rate = item.purchase_rate or Decimal('0.00')
            raw_gross = (qty * raw_rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            disc_type = item.discount_type or 'NONE'
            disc_input = item.discount_input_value or Decimal('0.00')

            # Calculate raw line discount on entered gross
            if disc_type == 'PERCENTAGE':
                pct = min(Decimal('100.00'), max(Decimal('0.00'), disc_input))
                raw_rupee_disc = (raw_gross * (pct / Decimal('100.00'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                raw_item_disc = min(raw_rupee_disc, raw_gross)
                eff_disc_pct = pct.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            elif disc_type == 'AMOUNT':
                amt = max(Decimal('0.00'), disc_input)
                raw_item_disc = min(amt, raw_gross)
                if raw_gross > Decimal('0.00'):
                    eff_disc_pct = ((raw_item_disc / raw_gross) * Decimal('100.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                else:
                    eff_disc_pct = Decimal('0.00')
            else:
                disc_type = 'NONE'
                disc_input = Decimal('0.00')
                raw_item_disc = Decimal('0.00')
                eff_disc_pct = Decimal('0.00')

            net_line_base = max(Decimal('0.00'), raw_gross - raw_item_disc)

            # Strict Tax Rate Normalization: 13.00 or 0.00
            current_rate = item.vat_rate if (item.vat_rate is not None and item.vat_rate > Decimal('0.00')) else Decimal('0.00')
            is_line_taxable = bool(current_rate > Decimal('0.00'))
            item_vat_rate = Decimal('13.00') if is_line_taxable else Decimal('0.00')
            item.vat_rate = item_vat_rate
            item.is_vat_applicable = is_line_taxable

            # VAT Handling Mode: If INCLUSIVE and 13% taxable, extract pre-VAT base and discount via 1.13
            vat_mode = getattr(grn, 'vat_handling_mode', 'EXCLUSIVE') or 'EXCLUSIVE'
            if vat_mode == 'INCLUSIVE' and is_line_taxable:
                tax_divisor = Decimal('1.13')
                taxable_extracted = (net_line_base / tax_divisor).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                pre_tax_discount = (raw_item_disc / tax_divisor).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                pre_tax_gross = (taxable_extracted + pre_tax_discount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

                item.gross_amount = pre_tax_gross
                item.item_discount_amount = pre_tax_discount
                item.line_total = taxable_extracted
            else:
                # Mode EXCLUSIVE or 0% Exempt Rate: rate is treated as pure pre-tax cost
                item.gross_amount = raw_gross
                item.item_discount_amount = raw_item_disc
                item.line_total = net_line_base

            item.discount_type = disc_type
            item.discount_input_value = disc_input
            item.discount_percent = eff_disc_pct

            total_line_gross += item.gross_amount
            total_line_discount += item.item_discount_amount

            line_meta.append({
                'item': item,
                'is_line_taxable': is_line_taxable,
                'vat_rate': item_vat_rate,
                'pre_tax_net': item.line_total
            })

        net_merchandise_subtotal = max(Decimal('0.00'), total_line_gross - total_line_discount)

        # Phase 2: Whole-Bill Discount Calculation
        bill_disc_type = grn.bill_discount_type or 'NONE'
        bill_disc_input = grn.bill_discount_input_value or Decimal('0.00')

        if bill_disc_type == 'PERCENTAGE':
            pct = min(Decimal('100.00'), max(Decimal('0.00'), bill_disc_input))
            b_disc = (net_merchandise_subtotal * (pct / Decimal('100.00'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            grn.bill_discount_amount = min(b_disc, net_merchandise_subtotal)
        elif bill_disc_type == 'AMOUNT':
            amt = max(Decimal('0.00'), bill_disc_input)
            grn.bill_discount_amount = min(amt, net_merchandise_subtotal)
        else:
            grn.bill_discount_type = 'NONE'
            grn.bill_discount_input_value = Decimal('0.00')
            grn.bill_discount_amount = Decimal('0.00')

        # Phase 3 & 4: Proportional Bill Discount Allocation, Landed Costs & Strict Line-by-Line VAT Summation
        overheads = grn.overhead_total
        item_count = len(items)

        total_net_taxable_base = Decimal('0.00')
        total_net_non_taxable_base = Decimal('0.00')
        total_vat_amount = Decimal('0.00')

        for entry in line_meta:
            item = entry['item']
            is_taxable = entry['is_line_taxable']
            rate = entry['vat_rate']
            pre_tax_net = entry['pre_tax_net']

            if net_merchandise_subtotal > Decimal('0.00'):
                weight = pre_tax_net / net_merchandise_subtotal
            else:
                weight = Decimal('1.00') / Decimal(item_count) if item_count > 0 else Decimal('0.00')

            line_bill_disc = (grn.bill_discount_amount * weight).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            effective_net_base = max(Decimal('0.00'), pre_tax_net - line_bill_disc)

            line_overhead = (overheads * weight).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            line_landed_total = effective_net_base + line_overhead

            factor = item.conversion_factor if (item.conversion_factor and item.conversion_factor > Decimal('0.000')) else Decimal('1.000')
            base_qty = (item.purchased_quantity * factor).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)
            item.base_unit_quantity = base_qty

            if base_qty > Decimal('0.000'):
                item.unit_landed_cost = (line_landed_total / base_qty).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            else:
                item.unit_landed_cost = Decimal('0.00')

            # Calculate VAT strictly per line item using 13% for taxable or 0% for exempt
            if is_taxable and rate == Decimal('13.00'):
                line_vat = (effective_net_base * Decimal('0.13')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                total_net_taxable_base += effective_net_base
            else:
                line_vat = Decimal('0.00')
                total_net_non_taxable_base += effective_net_base

            # Persist exact calculated line VAT snapshot onto the line item
            item.tax_amount = line_vat
            total_vat_amount += line_vat

        # Phase 5: Reconciled Consignment Financials (Hardened Invariant: Header VAT == Sum of Line VATs)
        total_merchandise_net = total_net_taxable_base + total_net_non_taxable_base
        total_landed_valuation = (total_merchandise_net + overheads).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        net_invoice_total = (total_merchandise_net + total_vat_amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        paid = grn.paid_amount or Decimal('0.00')
        net_due = max(Decimal('0.00'), net_invoice_total - paid)

        # Update GRN Header In-Memory Values
        grn.gross_amount = total_line_gross
        grn.total_line_discount = total_line_discount
        grn.discount_amount = total_line_discount + grn.bill_discount_amount
        grn.taxable_amount = total_net_taxable_base
        # HARDENED INVARIANT: Exactly equals the sum of all child line items' tax_amount
        grn.vat_amount = total_vat_amount
        grn.total_landed_cost = total_landed_valuation
        grn.net_total_amount = net_invoice_total
        grn.due_amount = net_due
        grn.is_vat_bill = bool(total_vat_amount > Decimal('0.00') or total_net_taxable_base > Decimal('0.00'))
        grn.vat_rate = Decimal('13.00') if grn.is_vat_bill else Decimal('0.00')

        return overheads

    # =========================================================================
    # STEP 2 (FULL): CALCULATE FINANCIALS AND APPLY PHYSICAL STOCK INWARD
    # =========================================================================
    @classmethod
    def _calculate_and_apply_financials_and_stock(
        cls,
        grn: GoodsReceivedNote,
        items: List[GRNItem],
        user=None
    ) -> None:
        """
        Executes complete mathematical valuation and commits physical stock counters,
        FIFO batches, and ItemInstance records.
        Saves line item tax_amount snapshots.
        Ensures inventory records always receive true Pre-Tax Landed Cost.
        """
        # Execute unified valuation across all items (populates item.tax_amount and grn.vat_amount)
        cls._calculate_financials_only(grn=grn, items=items)

        grn.status = 'RECEIVED'
        grn.received_by = user
        grn.save()

        for item in items:
            product = item.product
            base_qty = item.base_unit_quantity or item.purchased_quantity
            item.save()

            # 1. Update Physical Branch Inventory Counters
            date_label = grn.bill_date_bs or str(grn.bill_date)
            InventoryService.adjust_stock(
                product=product,
                branch=grn.branch,
                quantity_delta=base_qty,
                movement_type='PURCHASE',
                reference_doc=grn.grn_number,
                remarks=f"GRN Inward: {grn.supplier_bill_no} from {grn.supplier.company_name} on {date_label}",
                user=user,
                allow_negative=True
            )

            # 2. Update Master Selling Price & Create FIFO Batch (Preserving True Pre-Tax Landed Cost)
            batch_id = cls._update_product_master_and_batches(
                grn=grn,
                item=item,
                product=product,
                base_qty=base_qty,
                user=user
            )

            # 3. Register Physical ItemInstance Records (Dual-IMEI & MDMS)
            cls._register_imei_instances(
                grn=grn,
                item=item,
                product=product,
                batch_id=batch_id
            )

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
        logs historical price transitions, and creates date-specific FIFO batches.
        Always capitalizes pre-tax landed cost into inventory (excluding recoverable VAT).
        """
        old_cost = product.purchase_price
        old_sell = product.selling_price
        new_sell = item.new_selling_price or product.selling_price

        if item.unit_landed_cost != old_cost or (item.new_selling_price and new_sell != old_sell):
            ProductCostHistory.objects.create(
                product=product,
                date_effective=grn.bill_date,  # Strict historical date
                old_cost_price=old_cost,
                new_cost_price=item.unit_landed_cost,
                old_selling_price=old_sell,
                new_selling_price=new_sell,
                source_reference=grn.grn_number,
                changed_by=user,
                remarks=f"Inward GRN price update from {grn.supplier.company_name} on {grn.bill_date_bs or grn.bill_date}"
            )

        product.purchase_price = item.unit_landed_cost
        if item.new_selling_price and item.new_selling_price > Decimal('0.00'):
            product.selling_price = item.new_selling_price
        product.save(update_fields=['purchase_price', 'selling_price', 'updated_at'])

        # Priority: Use user-entered physical batch code if provided; else fallback to auto-generated string
        user_batch = (item.batch_number or '').strip()
        if user_batch:
            batch_id = user_batch
        else:
            batch_id = f"BATCH-{grn.grn_number}-{product.id}"
            item.batch_number = batch_id
            item.save(update_fields=['batch_number'])

        has_imeis = bool(item.scanned_imei_list and item.scanned_imei_list.strip())
        is_serialized = product.requires_imei_tracking or product.requires_serial_tracking

        # CREATE FIFO BATCH IF:
        # 1. Product is an accessory (not serialized), OR
        # 2. Product is a phone received WITHOUT IMEIs (Backlog Mode) so its cost is tracked!
        if not is_serialized or not has_imeis:
            ProductBatch.objects.create(
                batch_number=batch_id,
                product=product,
                branch=grn.branch,
                purchase_date=grn.bill_date,  # Strict historical date
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
        NTA MDMS certification, individual warranty end dates, and pre-tax unit landed costs.
        """
        if not (product.requires_imei_tracking or product.requires_serial_tracking):
            return

        scanned_raw = (item.scanned_imei_list or '').strip()
        if not scanned_raw:
            return

        imei_tokens = [t.strip() for t in re.split(r'[\n,;]+', scanned_raw) if t.strip()]
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
            w_start = grn.bill_date  # Strict historical date
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
                    mdms_verification_date=grn.bill_date,  # Strict historical date
                    mdms_remarks=f"GRN Inward: {grn.grn_number} | Invoice: {grn.supplier_bill_no}",
                    purchase_reference=grn.grn_number,
                    batch_reference=batch_id,
                    supplier_name=grn.supplier.company_name,
                    landed_cost=item.unit_landed_cost,
                    purchase_date=grn.bill_date,  # Strict historical date
                    warranty_start_date=w_start,
                    warranty_end_date=w_end,
                    warranty_remarks=f"Supplier Warranty: {item.warranty_provider or grn.warranty_provider or 'Distributor'}"
                )

    # =========================================================================
    # STEP 3: AUTOMATIC SUPPLIER LEDGER & BALANCE UPDATE (SPOT PAYMENT CONTINUITY)
    # =========================================================================
    @staticmethod
    def _post_supplier_ledger(
        grn: GoodsReceivedNote,
        user=None
    ) -> None:
        """
        Atomically updates the supplier's outstanding ledger balance using database
        row-level locking (select_for_update), posts the invoice bill transaction,
        and generates an explicit linked PAYMENT entry if spot cash/bank payment occurred.
        Guarantees that `Supplier.recalculate_balance_from_ledger()` preserves payments permanently.
        """
        supplier = Supplier.objects.select_for_update().get(pk=grn.supplier_id)
        prev_bal = supplier.current_balance or Decimal('0.00')

        paid = grn.paid_amount or Decimal('0.00')
        net_invoice_total = grn.net_total_amount or Decimal('0.00')
        due = grn.due_amount or Decimal('0.00')

        # Step A: Post Purchase Bill Entry in SupplierUdhaariLedger
        intermediate_bal = (prev_bal + net_invoice_total).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        bill_entry_resulting_bal = intermediate_bal if paid > Decimal('0.00') else (prev_bal + due).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        SupplierUdhaariLedger.objects.create(
            supplier=supplier,
            branch=grn.branch,
            transaction_type='PURCHASE_BILL',
            amount=net_invoice_total,
            previous_balance=prev_bal,
            resulting_balance=bill_entry_resulting_bal,
            payment_mode='OTHER',
            reference_number=grn.grn_number,
            entry_date=grn.bill_date,          # Explicit historical entry date (AD)
            entry_date_bs=grn.bill_date_bs,    # Explicit historical entry date (BS)
            recorded_by=user,
            remarks=(
                f"Inward Consignment ({grn.bill_date_bs or grn.bill_date}). Bill No: {grn.supplier_bill_no} "
                f"(Gross: Rs. {grn.gross_amount:,.2f}, Taxable: Rs. {grn.taxable_amount:,.2f}, "
                f"VAT: Rs. {grn.vat_amount:,.2f}, Total: Rs. {net_invoice_total:,.2f})"
            )
        )

        # Step B & C: Generate Linked Spot PAYMENT Entry if Paid on Delivery
        final_balance = (prev_bal + due).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        if paid > Decimal('0.00'):
            payment_mode = getattr(grn, 'preferred_payment_method', None) or 'CASH'
            SupplierUdhaariLedger.objects.create(
                supplier=supplier,
                branch=grn.branch,
                transaction_type='PAYMENT',
                amount=paid,
                previous_balance=intermediate_bal,
                resulting_balance=final_balance,
                payment_mode=payment_mode,
                reference_number=f"PMT-{grn.grn_number}",
                entry_date=grn.bill_date,          # Explicit historical entry date (AD)
                entry_date_bs=grn.bill_date_bs,    # Explicit historical entry date (BS)
                recorded_by=user,
                remarks=(
                    f"Spot payment disbursed on receipt of GRN {grn.grn_number} "
                    f"(Supplier Bill: {grn.supplier_bill_no}) via {payment_mode}."
                )
            )
            supplier.last_payment_date = grn.bill_date

        supplier.current_balance = final_balance
        supplier.last_purchase_date = grn.bill_date
        supplier.save(update_fields=['current_balance', 'last_purchase_date', 'last_payment_date', 'updated_at'])

        AuditLog.objects.create(
            user=user,
            branch=grn.branch,
            action_type='CREATE',
            module='PurchaseGRN',
            object_repr=grn.grn_number,
            details={
                'supplier': supplier.company_name,
                'bill_date_ad': str(grn.bill_date),
                'bill_date_bs': grn.bill_date_bs or '',
                'fiscal_year': grn.fiscal_year or '',
                'gross_amount': str(grn.gross_amount),
                'taxable_amount': str(grn.taxable_amount),
                'vat_amount': str(grn.vat_amount),
                'net_amount': str(net_invoice_total),
                'paid_amount': str(paid),
                'due_amount': str(due),
                'landed_cost': str(grn.total_landed_cost),
                'overheads': str(grn.overhead_total),
                'mdms_certified': grn.distributor_mdms_certified,
                'items_count': grn.items.count(),
                'prev_supplier_balance': str(prev_bal),
                'new_supplier_balance': str(final_balance)
            }
        )

    # =========================================================================
    # STEP 4: GENERAL LEDGER POSTING BRIDGE
    # =========================================================================
    @staticmethod
    def _post_gl_journal(grn: GoodsReceivedNote, user=None) -> None:
        """
        Dispatches double-entry voucher to General Ledger fail-closed.
        Debits Account 1410 (Input VAT 13%) strictly by `grn.vat_amount`.
        """
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

    # =========================================================================
    # SPECIALIZED ACTION: ATTACH / LINK PHYSICAL IMEIS TO ALREADY-RECEIVED GRN
    # =========================================================================
    @classmethod
    @transaction.atomic
    def attach_imeis_to_received_grn(
        cls,
        grn: GoodsReceivedNote,
        items_payload: Dict[str, Any],
        user=None
    ) -> int:
        """
        Attaches / links physical 15-digit IMEI serial numbers to an already-received GRN:
        1. Row-level locking on GRN and phone items.
        2. Validates that GRN is not CANCELLED.
        3. Strict Quantity Check: Count of IMEIs per line cannot exceed base_unit_quantity.
        4. CRITICAL RULE - ZERO STOCK INFLATION: Does NOT call adjust_stock(). Does NOT touch BranchStock.quantity.
        5. CRITICAL RULE - ZERO ACCOUNTING IMPACT: Does NOT alter purchase financials, supplier debt, or GL entries.
        6. System-Wide IMEI Uniqueness: Verifies no active duplicate IMEIs exist across the company.
        7. Creates active ItemInstance records in 'IN_STOCK' status linked to this GRN.
        8. Handles Dual-SIM (imei_2_pending_scan flag).
        9. Rebuilds and updates GRNItem.scanned_imei_list for invoice printing.
        10. Writes an immutable record to AuditLog.
        """
        locked_grn = GoodsReceivedNote.objects.select_for_update().get(pk=grn.pk)
        if locked_grn.status == 'CANCELLED':
            raise ValidationError(f"Cannot link IMEIs: Goods Received Note {locked_grn.grn_number} is CANCELLED.")

        seen_imeis_in_batch = set()
        total_created = 0

        for item_id_str, item_info in items_payload.items():
            try:
                item_id = int(item_id_str)
            except (ValueError, TypeError):
                continue

            grn_item = locked_grn.items.select_for_update().select_related('product').filter(id=item_id).first()
            if not grn_item:
                continue

            product = grn_item.product
            if not product.requires_imei_tracking:
                continue

            base_qty = int(grn_item.base_unit_quantity or grn_item.purchased_quantity or 1)
            raw_imeis = item_info.get('imeis', [])

            if len(raw_imeis) > base_qty:
                raise ValidationError(
                    f"Quantity Exceeded on '{product.name}': You submitted {len(raw_imeis)} IMEIs, "
                    f"but only {base_qty} units were purchased on this bill line item."
                )

            # Query existing instances already linked to this GRN and product
            existing_instances_qs = ItemInstance.objects.select_for_update().filter(
                purchase_reference=locked_grn.grn_number,
                product=product,
                branch=locked_grn.branch
            )
            existing_im1_map = {inst.imei_1: inst for inst in existing_instances_qs if inst.imei_1}
            existing_im2_map = {inst.imei_2: inst for inst in existing_instances_qs if inst.imei_2}

            is_dual_sim = getattr(product, 'sim_configuration', 'DUAL_SIM') in ['DUAL_SIM', 'ESIM_DUAL']
            batch_id = grn_item.batch_number or f"BATCH-{locked_grn.grn_number}-{product.id}"
            warranty_m = grn_item.warranty_months or locked_grn.warranty_months or product.warranty_months or 12
            w_start = locked_grn.bill_date
            w_end = w_start + timedelta(days=warranty_m * 30) if warranty_m > 0 else None
            line_mdms = grn_item.default_mdms_status if not locked_grn.distributor_mdms_certified else 'REGISTERED_OFFICIAL'

            formatted_imei_lines = []

            for entry in raw_imeis:
                if isinstance(entry, dict):
                    im1 = str(entry.get('imei_1') or '').strip()
                    im2 = str(entry.get('imei_2') or '').strip()
                elif isinstance(entry, str):
                    parts = entry.split('|')
                    im1 = parts[0].strip() if len(parts) > 0 else ''
                    im2 = parts[1].strip() if len(parts) > 1 else ''
                else:
                    continue

                im1 = im1 if im1 else None
                im2 = im2 if im2 else None

                if not im1:
                    continue

                # Basic Digits and Length Validation
                if not im1.isdigit():
                    raise ValidationError(f"Invalid IMEI 1 '{im1}' on '{product.name}'. IMEIs must be numeric digits.")
                if len(im1) < 14:
                    raise ValidationError(f"IMEI 1 '{im1}' on '{product.name}' is too short (must be at least 14-15 digits).")

                if im2:
                    if not im2.isdigit():
                        raise ValidationError(f"Invalid IMEI 2 '{im2}' on '{product.name}'. IMEIs must be numeric digits.")
                    if len(im2) < 14:
                        raise ValidationError(f"IMEI 2 '{im2}' on '{product.name}' is too short (must be at least 14-15 digits).")
                    if im1 == im2:
                        raise ValidationError(f"Duplicate dual-SIM pair: IMEI 1 and IMEI 2 cannot be identical ('{im1}') on '{product.name}'.")

                # Check Internal Duplicates within the same submitted batch
                if im1 in seen_imeis_in_batch:
                    raise ValidationError(f"Duplicate IMEI 1 '{im1}' entered multiple times in this batch for '{product.name}'.")
                seen_imeis_in_batch.add(im1)

                if im2:
                    if im2 in seen_imeis_in_batch:
                        raise ValidationError(f"Duplicate IMEI 2 '{im2}' entered multiple times in this batch for '{product.name}'.")
                    seen_imeis_in_batch.add(im2)

                # Check if this physical unit was already registered under this GRN
                existing_instance = existing_im1_map.get(im1) or (existing_im2_map.get(im2) if im2 else None)

                if existing_instance:
                    # Already registered for this bill: update missing secondary IMEI if now provided
                    if im2 and not existing_instance.imei_2:
                        existing_instance.imei_2 = im2
                        existing_instance.imei_2_pending_scan = False
                        existing_instance.save(update_fields=['imei_2', 'imei_2_pending_scan', 'updated_at'])
                else:
                    # Check System-Wide Uniqueness across active stock in other branches
                    collision_q = Q(imei_1=im1) | Q(imei_2=im1)
                    if im2:
                        collision_q |= Q(imei_1=im2) | Q(imei_2=im2)

                    existing_in_other = ItemInstance.objects.filter(
                        collision_q,
                        status='IN_STOCK'
                    ).select_related('product', 'branch').first()

                    if existing_in_other:
                        raise ValidationError(
                            f"IMEI '{im1}' on '{product.name}' is ALREADY registered as active stock in warehouse "
                            f"'{existing_in_other.branch.name}' (Product: {existing_in_other.product.name}). "
                            f"Duplicate active inventory is strictly prohibited."
                        )

                    # Create New Active Physical ItemInstance (Strictly IN_STOCK)
                    pending_scan = is_dual_sim and (im2 is None)
                    ItemInstance.objects.create(
                        product=product,
                        branch=locked_grn.branch,
                        device_uid=f"DEV-{uuid.uuid4().hex[:12].upper()}",
                        imei_1=im1,
                        imei_2=im2,
                        imei_2_pending_scan=pending_scan,
                        serial_number=None,
                        device_barcode=im1 or product.barcode,
                        status='IN_STOCK',
                        condition='BRAND_NEW',
                        activation_status='SEALED_INACTIVE',
                        source_type='NEW_PURCHASE_GRN',
                        mdms_status=line_mdms,
                        mdms_verification_date=locked_grn.bill_date,
                        mdms_remarks=f"Post-Inward IMEI scan: {locked_grn.grn_number} | Invoice: {locked_grn.supplier_bill_no}",
                        purchase_reference=locked_grn.grn_number,
                        batch_reference=batch_id,
                        supplier_name=locked_grn.supplier.company_name,
                        landed_cost=grn_item.unit_landed_cost,
                        purchase_date=locked_grn.bill_date,
                        warranty_start_date=w_start,
                        warranty_end_date=w_end,
                        warranty_remarks=f"Supplier Warranty: {grn_item.warranty_provider or locked_grn.warranty_provider or 'Distributor'}"
                    )
                    total_created += 1

                # Append to permanent text representation
                if im2:
                    formatted_imei_lines.append(f"{im1}|{im2}")
                else:
                    formatted_imei_lines.append(f"{im1}")

            # Update permanent text log on GRNItem
            grn_item.scanned_imei_list = "\n".join(formatted_imei_lines)
            grn_item.save(update_fields=['scanned_imei_list', 'updated_at'])

        # Record System-Wide Forensic Audit Trail
        AuditLog.objects.create(
            user=user,
            branch=locked_grn.branch,
            action_type='UPDATE',
            module='PurchaseGRN_IMEIScan',
            object_repr=locked_grn.grn_number,
            details={
                'grn_number': locked_grn.grn_number,
                'bill_no': locked_grn.supplier_bill_no,
                'supplier': locked_grn.supplier.company_name,
                'new_imeis_created': total_created,
                'total_imeis_linked': len(seen_imeis_in_batch),
                'message': f"Linked {total_created} new physical IMEI(s) to existing warehouse stock without altering quantities or accounting."
            }
        )

        return total_created

# =============================================================================
# COMMERCIAL PURCHASE RETURN / DEBIT NOTE SERVICE
# =============================================================================
class PurchaseReturnService:
    """
    Commercial Purchase Return / Debit Note Processing Engine.
    Executes atomic merchandise return to suppliers:
    1. Harmonizes historical return dates.
    2. Pre-validates stock and serialized IMEIs in IN_STOCK status (supporting Backlog Mode).
    3. Deducts physical inventory via InventoryService.adjust_stock.
    4. Locks serialized ItemInstances as 'RETURNED_TO_SUPPLIER' if present.
    5. Deducts from FIFO batches for non-serialized items or backlog units.
    6. Reconciles supplier debt balance with explicit historical dates.
    7. Automatically posts double-entry General Ledger reversal vouchers fail-closed
       (Credits Input VAT 1410 by purchase_return.tax_amount).
    """

    @staticmethod
    def generate_return_number(branch: Branch) -> str:
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
        Coordinates full purchase return lifecycle:
        Item VAT calculated -> Return Header VAT updated -> Supplier ledger adjusted -> GL reversal posted.
        """
        items = list(purchase_return.items.select_related('product', 'product__base_unit', 'unit_conversion').all())
        if not items:
            raise ValidationError("Cannot process a purchase return without line items. Please add at least one product.")

        # Harmonize return dates (AD, BS, Fiscal Year)
        cls._harmonize_return_dates(purchase_return)

        # Step 1: Pre-validation of Stock Availability & Serialized IMEI Ownership
        cls._validate_return_items(purchase_return, items)

        # Step 2: Deduct Stock, Batches & Update Serialized IMEI Units
        # (Returns total merchandise gross and exact sum of line tax_amount values)
        total_return_val, total_tax_val = cls._deduct_stock_and_update_imeis(
            purchase_return=purchase_return,
            items=items,
            user=user
        )

        net_refund_val = (total_return_val + total_tax_val).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        purchase_return.total_return_amount = total_return_val
        # Hardened Invariant: Exactly equals the sum of all return line item tax_amount values
        purchase_return.tax_amount = total_tax_val
        purchase_return.net_refund_amount = net_refund_val
        purchase_return.status = 'CONFIRMED'
        purchase_return.processed_by = user
        purchase_return.save(update_fields=[
            'total_return_amount', 'tax_amount', 'net_refund_amount',
            'status', 'processed_by', 'return_date', 'return_date_bs',
            'fiscal_year', 'updated_at'
        ])

        # Step 3: Settle Supplier Debt Ledger & Post Financial Adjustment with Historical Dates
        cls._reconcile_supplier_ledger(purchase_return, net_refund_val, user)

        # Step 4: Post Double-Entry Journal to General Ledger
        # (Reverses Input VAT Account 1410 strictly by purchase_return.tax_amount)
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
                'return_date_ad': str(purchase_return.return_date),
                'return_date_bs': purchase_return.return_date_bs or '',
                'fiscal_year': purchase_return.fiscal_year or '',
                'total_return_amount': str(total_return_val),
                'tax_amount_reversed': str(total_tax_val),
                'net_refund_amount': str(net_refund_val),
                'refund_mode': purchase_return.refund_mode,
                'items_count': len(items),
                'original_bill_reference': purchase_return.original_bill_reference or "",
            }
        )

        return purchase_return

    @staticmethod
    def _harmonize_return_dates(purchase_return: PurchaseReturn) -> None:
        """
        Harmonizes return_date (AD), return_date_bs (BS), and fiscal_year before processing.
        """
        if purchase_return.return_date_bs and str(purchase_return.return_date_bs).strip():
            try:
                bs_y, bs_m, bs_d = parse_bs_date_components(str(purchase_return.return_date_bs).strip())
                target_ad = NepaliCalendar.bs_to_ad(bs_y, bs_m, bs_d)
                purchase_return.return_date = target_ad
                purchase_return.return_date_bs = f"{bs_y:04d}-{bs_m:02d}-{bs_d:02d}"
                if not purchase_return.fiscal_year:
                    purchase_return.fiscal_year = NepaliCalendar.get_fiscal_year(bs_y, bs_m)
            except Exception as e:
                logger.warning(f"[PurchaseReturnService] Could not parse return_date_bs '{purchase_return.return_date_bs}': {e}")
        elif purchase_return.return_date:
            ad_date = purchase_return.return_date.date() if isinstance(purchase_return.return_date, datetime) else purchase_return.return_date
            purchase_return.return_date = ad_date
            try:
                bs_y, bs_m, bs_d = NepaliCalendar.ad_to_bs(ad_date)
                if not purchase_return.return_date_bs:
                    purchase_return.return_date_bs = NepaliCalendar.format_bs(bs_y, bs_m, bs_d, lang='en')
                if not purchase_return.fiscal_year:
                    purchase_return.fiscal_year = NepaliCalendar.get_fiscal_year(bs_y, bs_m)
            except Exception as e:
                logger.warning(f"[PurchaseReturnService] Could not convert return_date to BS: {e}")

    @classmethod
    def _validate_return_items(cls, purchase_return: PurchaseReturn, items: List[PurchaseReturnItem]) -> None:
        sys_config = SystemConfiguration.get_solo()
        enforce_imei = getattr(sys_config, 'enforce_imei_tracking', True)

        for item in items:
            product = item.product
            factor = item.conversion_factor if (item.conversion_factor and item.conversion_factor > Decimal('0.000')) else Decimal('1.000')
            qty = item.returned_quantity if (item.returned_quantity and item.returned_quantity > Decimal('0.000')) else Decimal('1.000')
            base_qty = (qty * factor).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)

            if base_qty <= Decimal('0.000'):
                raise ValidationError(f"Return quantity for item '{product.name}' must be greater than zero.")

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

            if product.requires_imei_tracking or product.requires_serial_tracking:
                expected_units = int(base_qty)
                raw_imei = (item.returned_imei_list or '').strip()
                tokens = [t.strip() for t in re.split(r'[\n,;]+', raw_imei) if t.strip()]

                # If Backlog Mode is active and no IMEIs were entered for return, allow skipping
                if not enforce_imei and len(tokens) == 0:
                    continue

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
        Calculates item-by-item VAT on returned items, persists `tax_amount` on each line,
        deducts warehouse inventory, and updates serialized item instances.
        """
        total_return_val = Decimal('0.00')
        total_tax_val = Decimal('0.00')

        for item in items:
            product = item.product
            factor = item.conversion_factor if (item.conversion_factor and item.conversion_factor > Decimal('0.000')) else Decimal('1.000')
            qty = item.returned_quantity if (item.returned_quantity and item.returned_quantity > Decimal('0.000')) else Decimal('1.000')
            base_qty = (qty * factor).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)
            item.base_unit_quantity = base_qty

            gross = (qty * (item.purchase_rate or Decimal('0.00'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            tax_rate = item.tax_rate or Decimal('0.00')
            tax = (gross * (tax_rate / Decimal('100.00'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) if tax_rate > Decimal('0.00') else Decimal('0.00')
            line_tot = gross + tax

            # Snapshot exact tax amount and line total onto the return item
            item.tax_amount = tax
            item.line_total = line_tot
            item.save(update_fields=['base_unit_quantity', 'tax_amount', 'line_total', 'updated_at'])

            total_return_val += gross
            total_tax_val += tax

            # 1. Deduct sellable stock from BranchStock
            date_label = purchase_return.return_date_bs or str(purchase_return.return_date)
            InventoryService.adjust_stock(
                product=product,
                branch=purchase_return.branch,
                quantity_delta=-base_qty,
                movement_type='RMA_VENDOR_DISPATCH',
                reference_doc=purchase_return.return_number,
                imei_or_serial=item.returned_imei_list or "",
                remarks=(
                    f"Commercial Purchase Return (Debit Note: {purchase_return.return_number}) "
                    f"to {purchase_return.supplier.company_name} on {date_label}. Reason: {item.return_reason or 'Stock Return'}"
                ),
                user=user,
                allow_negative=False
            )

            # 2. Update Serialized Phone ItemInstances to RETURNED_TO_SUPPLIER if IMEIs were tracked
            raw_imei = (item.returned_imei_list or '').strip()
            is_serialized = product.requires_imei_tracking or product.requires_serial_tracking

            if is_serialized and raw_imei:
                tokens = [t.strip() for t in re.split(r'[\n,;]+', raw_imei) if t.strip()]
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

            # 3. Deduct from active FIFO batches (for non-serialized items OR backlog units returned without IMEIs)
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
        Updates supplier credit balance and posts historical sub-ledger records
        with explicit entry_date and entry_date_bs.
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
                entry_date=purchase_return.return_date,          # Explicit historical entry date (AD)
                entry_date_bs=purchase_return.return_date_bs,    # Explicit historical entry date (BS)
                recorded_by=user,
                remarks=(
                    f"Debit Note {purchase_return.return_number} ({purchase_return.return_date_bs or purchase_return.return_date}): "
                    f"Stock returned to supplier {supplier.company_name}. Balance deducted by Rs. {net_refund_amount:.2f}. "
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
                entry_date=purchase_return.return_date,          # Explicit historical entry date (AD)
                entry_date_bs=purchase_return.return_date_bs,    # Explicit historical entry date (BS)
                recorded_by=user,
                remarks=(
                    f"Debit Note {purchase_return.return_number} ({purchase_return.return_date_bs or purchase_return.return_date}): "
                    f"Cash refund received of Rs. {net_refund_amount:.2f} from {supplier.company_name}."
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
                entry_date=purchase_return.return_date,          # Explicit historical entry date (AD)
                entry_date_bs=purchase_return.return_date_bs,    # Explicit historical entry date (BS)
                recorded_by=user,
                remarks=(
                    f"Debit Note {purchase_return.return_number} ({purchase_return.return_date_bs or purchase_return.return_date}): "
                    f"Stock returned awaiting replacement consignment from {supplier.company_name} (Value: Rs. {net_refund_amount:.2f})."
                )
            )