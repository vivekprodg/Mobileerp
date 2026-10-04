"""
Purchase GRN & Commercial Purchase Return (Debit Note) Services.

Core Capabilities:
1. Dual VAT Mode Calculations & Dual-Pot Separation:
   - Evaluates line items into two distinct pots: Taxable vs. Non-Taxable.
   - When VAT Excluded (EXCLUSIVE): Purchase rate is treated directly as pre-VAT base.
   - When VAT Included (INCLUSIVE): Pre-VAT base is extracted on taxable lines:
     Pre-VAT Base = (Gross - Line Discount) / (1 + (Tax Rate / 100))
     Gross Pre-VAT = Pre-VAT Base + Line Discount.
   - VAT (13%) is calculated strictly on the net Taxable pot after whole-bill discount.
   - Non-taxable items are completely insulated from VAT calculations.
   - Net Invoice Total = Net Taxable Base + Non-Taxable Base + VAT Amount.
2. 5-Tier Proportional Landed Cost Overhead Allocation:
   - Distributes all 5 overhead categories (Freight, Customs Duty, Handling & Unloading,
     Transit Insurance, and Other Overheads) proportionally across line items based on net merchandise value.
   - Accurately establishes the unit landed cost for inventory COGS asset valuation.
3. Custom Physical Batch Number Persistence:
   - Preserves user-entered batch identifiers (`item.batch_number`, e.g. BT-2026-A1) on `ProductBatch`
     and links them to individual `ItemInstance` records instead of falling back to computer-generated hashes.
4. Save Draft vs. Final Verify Split:
   - `save_grn_draft()`: Calculates line financials, taxes, and landed costs without adjusting stock,
     without creating serial instances, and without touching accounting ledgers.
   - `process_grn_approval_and_stock_in()`: Performs strict validation, updates physical warehouse stock,
     registers IMEI instances, creates FIFO batches, updates supplier debt, and posts double-entry GL journals.
5. Master-Switch Sensitive Serialized & Dual-IMEI Enforcement:
   - In Strict Mode (`enforce_imei_tracking=True`): Mandates exact 1-to-1 match between handset quantities and scanned IMEIs on approval.
   - In Backlog Mode (`enforce_imei_tracking=False`): Allows phone inward entry without IMEIs, creating FIFO ProductBatches.
   - Non-serialized accessories always bypass serial checks.
6. Thread-Safe Supplier Ledger Reconciliation & Fail-Closed General Ledger Posting:
   - Row-level locking (`select_for_update`) on supplier balances and synchronized double-entry GL vouchers.
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
    - Credit: Input VAT 13% (reversing input tax if tax invoice)
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

        # Save line item projections
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
        Main transactional entry point coordinating complete GRN verification,
        dual VAT handling (exclusive vs inclusive), 5-tier proportional overhead distribution,
        custom batch numbers, stock inward, historical dates, supplier debt updates,
        and fail-closed General Ledger posting.
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

        # Step 2: Full Mathematical Valuation, Proportional Overhead Allocation & Stock Inward
        cls._calculate_and_apply_financials_and_stock(
            grn=grn,
            items=items,
            user=user
        )

        # Step 3: Post to Supplier Ledger with Row-Level Locking & Historical Dates
        cls._post_supplier_ledger(grn=grn, user=user)

        # Step 4: Post General Ledger Double-Entry Journal
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
    # STEP 2: MATHEMATICAL VALUATION & DUAL-POT OVERHEAD ALLOCATION (FINANCIALS ONLY)
    # =========================================================================
    @classmethod
    def _calculate_financials_only(
        cls,
        grn: GoodsReceivedNote,
        items: List[GRNItem]
    ) -> Decimal:
        """
        Executes unified mathematical valuation across line items and whole-bill overheads:
        - Resolves VAT Exclusive vs. Inclusive modes per line item.
        - Segregates items into Taxable vs. Non-Taxable merchandise pots.
        - Calculates line discounts (AMOUNT or PERCENTAGE).
        - Prorates whole-bill discount proportionally across merchandise pots.
        - Calculates 13% VAT strictly on the net Taxable Base after discounts.
        - Non-taxable merchandise is completely insulated from VAT additions.
        - Net Total Payable = Net Taxable Base + Non-Taxable Base + VAT Amount.
        - Total Landed Cost = Net Taxable Base + Non-Taxable Base + 5 Overheads.
        - Distributes bill discounts and overhead expenses across items to compute exact unit landed cost.
        """
        total_line_gross = Decimal('0.00')
        total_line_discount = Decimal('0.00')
        gross_taxable_lines = Decimal('0.00')
        gross_non_taxable_lines = Decimal('0.00')

        # Phase 1: Line Item Gross, Line Discounts & Initial Pot Separation
        for item in items:
            qty = item.purchased_quantity if (item.purchased_quantity and item.purchased_quantity > Decimal('0.000')) else Decimal('1.000')
            raw_rate = item.purchase_rate or Decimal('0.00')
            raw_gross = (qty * raw_rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            disc_type = item.discount_type or 'NONE'
            disc_input = item.discount_input_value or Decimal('0.00')

            if disc_type == 'PERCENTAGE':
                pct = min(Decimal('100.00'), max(Decimal('0.00'), disc_input))
                rupee_disc = (raw_gross * (pct / Decimal('100.00'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                item.item_discount_amount = min(rupee_disc, raw_gross)
                item.discount_percent = pct.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            elif disc_type == 'AMOUNT':
                amt = max(Decimal('0.00'), disc_input)
                item.item_discount_amount = min(amt, raw_gross)
                if raw_gross > Decimal('0.00'):
                    item.discount_percent = ((item.item_discount_amount / raw_gross) * Decimal('100.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                else:
                    item.discount_percent = Decimal('0.00')
            else:
                item.discount_type = 'NONE'
                item.discount_input_value = Decimal('0.00')
                item.item_discount_amount = Decimal('0.00')
                item.discount_percent = Decimal('0.00')

            net_line_base = max(Decimal('0.00'), raw_gross - item.item_discount_amount)

            # Determine whether this specific line item is taxable under Nepal VAT rules
            line_has_vat = (item.vat_rate and item.vat_rate > Decimal('0.00'))
            is_line_taxable = bool(grn.is_vat_bill and item.is_vat_applicable and line_has_vat)

            item_vat_rate = item.vat_rate if line_has_vat else Decimal('13.00')

            # VAT Handling Mode: If INCLUSIVE and line is taxable, extract pre-VAT base
            if getattr(grn, 'vat_handling_mode', 'EXCLUSIVE') == 'INCLUSIVE' and is_line_taxable:
                tax_divisor = Decimal('1.00') + (item_vat_rate / Decimal('100.00'))
                taxable_extracted = (net_line_base / tax_divisor).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                item.gross_amount = (taxable_extracted + item.item_discount_amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                item.line_total = taxable_extracted
            else:
                item.gross_amount = raw_gross
                item.line_total = net_line_base

            total_line_gross += item.gross_amount
            total_line_discount += item.item_discount_amount

            # Accumulate into distinct merchandise pots
            if is_line_taxable:
                gross_taxable_lines += item.line_total
            else:
                gross_non_taxable_lines += item.line_total

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

        # Phase 3: Proportional Allocation of Bill Discount to Taxable & Non-Taxable Pots
        if net_merchandise_subtotal > Decimal('0.00') and grn.bill_discount_amount > Decimal('0.00'):
            taxable_share_disc = (grn.bill_discount_amount * (gross_taxable_lines / net_merchandise_subtotal)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            non_taxable_share_disc = grn.bill_discount_amount - taxable_share_disc
        else:
            taxable_share_disc = Decimal('0.00')
            non_taxable_share_disc = Decimal('0.00')

        net_taxable_base = max(Decimal('0.00'), gross_taxable_lines - taxable_share_disc)
        net_non_taxable_base = max(Decimal('0.00'), gross_non_taxable_lines - non_taxable_share_disc)

        # Phase 4: Dedicated 13% VAT Calculation (Strictly on Pre-VAT Taxable Base)
        if grn.is_vat_bill and net_taxable_base > Decimal('0.00'):
            rate = grn.vat_rate if (grn.vat_rate and grn.vat_rate > Decimal('0.00')) else Decimal('13.00')
            grn.vat_rate = rate
            vat_amount = (net_taxable_base * (rate / Decimal('100.00'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        else:
            vat_amount = Decimal('0.00')

        # Phase 5: Overheads (Aggregating all 5 Overhead Categories)
        overheads = grn.overhead_total

        # Phase 6: Landed Cost Valuation & Final Reconciled Bill Total (Zero Double-Counting)
        total_merchandise_net = net_taxable_base + net_non_taxable_base
        total_landed_valuation = (total_merchandise_net + overheads).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        net_invoice_total = (total_merchandise_net + vat_amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        paid = grn.paid_amount or Decimal('0.00')
        net_due = max(Decimal('0.00'), net_invoice_total - paid)

        # Update GRN Header In-Memory Values
        grn.gross_amount = total_line_gross
        grn.total_line_discount = total_line_discount
        grn.discount_amount = total_line_discount + grn.bill_discount_amount
        grn.taxable_amount = net_taxable_base
        grn.vat_amount = vat_amount
        grn.total_landed_cost = total_landed_valuation
        grn.net_total_amount = net_invoice_total
        grn.due_amount = net_due

        # Phase 7: Value-Based Overhead & Bill-Discount Allocation to Line Items
        item_count = len(items)
        for item in items:
            factor = item.conversion_factor if (item.conversion_factor and item.conversion_factor > Decimal('0.000')) else Decimal('1.000')
            base_qty = (item.purchased_quantity * factor).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)
            item.base_unit_quantity = base_qty

            if net_merchandise_subtotal > Decimal('0.00'):
                weight = item.line_total / net_merchandise_subtotal
            else:
                weight = Decimal('1.00') / Decimal(item_count) if item_count > 0 else Decimal('0.00')

            line_bill_disc = (grn.bill_discount_amount * weight).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            line_overhead = (overheads * weight).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            # Net landed cost for this line item (Merchandise Net - Bill Disc Share + Overhead Share)
            line_landed_total = item.line_total - line_bill_disc + line_overhead

            if base_qty > Decimal('0.000'):
                item.unit_landed_cost = (line_landed_total / base_qty).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            else:
                item.unit_landed_cost = Decimal('0.00')

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
        """
        # Execute unified valuation across all items
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

            # 2. Update Master Selling Price & Create FIFO Batch (Preserving Custom Batch Numbers)
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
        Preserves custom physical batch numbers (`item.batch_number`) entered by user.
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
        NTA MDMS certification and individual warranty end dates, strictly stamped
        with the verified historical `grn.bill_date`.
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
    # STEP 3: AUTOMATIC SUPPLIER LEDGER & BALANCE UPDATE (HISTORICAL DATES)
    # =========================================================================
    @staticmethod
    def _post_supplier_ledger(
        grn: GoodsReceivedNote,
        user=None
    ) -> None:
        """
        Atomically updates the supplier's outstanding ledger balance using database
        row-level locking (select_for_update), posts the ledger transaction with
        verified historical entry dates (AD and BS), and generates an audit log.
        """
        supplier = Supplier.objects.select_for_update().get(pk=grn.supplier_id)
        prev_bal = supplier.current_balance or Decimal('0.00')
        new_bal = prev_bal + grn.due_amount

        supplier.current_balance = new_bal
        supplier.last_purchase_date = grn.bill_date  # Strict historical date
        supplier.save(update_fields=['current_balance', 'last_purchase_date', 'updated_at'])

        # Post Supplier Udhaari Ledger Entry with verified historical dates
        SupplierUdhaariLedger.objects.create(
            supplier=supplier,
            branch=grn.branch,
            transaction_type='PURCHASE_BILL',
            amount=grn.net_total_amount,
            previous_balance=prev_bal,
            resulting_balance=new_bal,
            payment_mode='CASH' if (grn.paid_amount or Decimal('0.00')) > Decimal('0.00') else 'OTHER',
            reference_number=grn.grn_number,
            entry_date=grn.bill_date,  # Explicit historical entry date (AD)
            entry_date_bs=grn.bill_date_bs,  # Explicit historical entry date (BS)
            recorded_by=user,
            remarks=(
                f"GRN Received ({grn.bill_date_bs or grn.bill_date}). Bill No: {grn.supplier_bill_no} "
                f"(Gross: Rs. {grn.gross_amount:.2f}, Taxable: Rs. {grn.taxable_amount:.2f}, "
                f"VAT: Rs. {grn.vat_amount:.2f}, Total: Rs. {grn.net_total_amount:.2f}, "
                f"Paid: Rs. {grn.paid_amount:.2f}, Due: Rs. {grn.due_amount:.2f})"
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
                'bill_date_ad': str(grn.bill_date),
                'bill_date_bs': grn.bill_date_bs or '',
                'fiscal_year': grn.fiscal_year or '',
                'gross_amount': str(grn.gross_amount),
                'taxable_amount': str(grn.taxable_amount),
                'vat_amount': str(grn.vat_amount),
                'net_amount': str(grn.net_total_amount),
                'landed_cost': str(grn.total_landed_cost),
                'overheads': str(grn.overhead_total),
                'mdms_certified': grn.distributor_mdms_certified,
                'items_count': grn.items.count(),
                'prev_supplier_balance': str(prev_bal),
                'new_supplier_balance': str(new_bal)
            }
        )

    # =========================================================================
    # STEP 4: GENERAL LEDGER POSTING BRIDGE
    # =========================================================================
    @staticmethod
    def _post_gl_journal(grn: GoodsReceivedNote, user=None) -> None:
        """
        Dispatches double-entry voucher to General Ledger fail-closed.
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
    7. Automatically posts double-entry General Ledger reversal vouchers fail-closed.
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
        items = list(purchase_return.items.select_related('product', 'product__base_unit', 'unit_conversion').all())
        if not items:
            raise ValidationError("Cannot process a purchase return without line items. Please add at least one product.")

        # Harmonize return dates (AD, BS, Fiscal Year)
        cls._harmonize_return_dates(purchase_return)

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
        purchase_return.save(update_fields=[
            'total_return_amount', 'tax_amount', 'net_refund_amount',
            'status', 'processed_by', 'return_date', 'return_date_bs',
            'fiscal_year', 'updated_at'
        ])

        # Step 3: Settle Supplier Debt Ledger & Post Financial Adjustment with Historical Dates
        cls._reconcile_supplier_ledger(purchase_return, net_refund_val, user)

        # Step 4: Post Double-Entry Journal to General Ledger
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