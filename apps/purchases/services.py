import re
import uuid
from decimal import Decimal, ROUND_HALF_UP
from datetime import date, timedelta
from typing import List, Tuple, Dict, Any, Optional
from django.db import transaction
from django.db.models import Q
from django.core.exceptions import ValidationError

from apps.purchases.models import GoodsReceivedNote, GRNItem, Supplier, SupplierUdhaariLedger
from apps.inventory.models import Product, ItemInstance, ProductBatch, BranchStock
from apps.inventory.services import InventoryService
from apps.reports.models import ProductCostHistory
from apps.core.models import AuditLog


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