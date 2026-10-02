"""
Inventory Management & Business Logic Service.

Key Capabilities:
1. Atomic Stock Adjustments & Movement Logs:
   - Row-level database locking (`select_for_update`) on BranchStock.
   - Comprehensive audit logging via StockMovementLog.
   - Automatic Double-Entry General Ledger postings for physical audit variances:
     * ADJUSTMENT_SUB: Dr. 6160 Inventory Shrinkage / Cr. 1310 Merchandise Inventory Asset.
     * ADJUSTMENT_ADD: Dr. 1310 Merchandise Inventory Asset / Cr. 4030 Inventory Audit Surplus Gain.
2. User-Entered Batch Prioritization & FIFO Tracking (`record_inward_stock_batch`):
   - Prioritizes custom vendor batch numbers entered in the GRN line item table (up to 100 chars).
   - Generates clean sequential fallback batch strings (`BATCH-{YYMMDD}-{PRODUCT_ID}-{UUID}`)
     only if the batch number field was left blank by the user.
   - Tracks FIFO batches for non-serialized accessories and backlog phones received without IMEIs.
3. Inward IMEI Registration & Dual-SIM Compliance (`register_inward_imei_unit`):
   - Registers physical device instances, associates them with the user's custom batch number,
     and tags NTA MDMS compliance status.
4. Component-Level Customer Warranty Schedules (`initialize_device_component_warranties`):
   - Generates exact calendar expiration dates for Main Body (12M/365D), Battery (6M/180D),
     and Screen (3M/90D) while stamping statutory damage exclusion terms.
"""

import uuid
import logging
from decimal import Decimal, ROUND_HALF_UP
from datetime import date, timedelta
from typing import Optional, List, Tuple
from django.db import transaction
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.inventory.models import (
    Product, BranchStock, ProductBatch, ItemInstance,
    DeviceComponentWarranty, StockMovementLog
)
from apps.branches.models import Branch

logger = logging.getLogger(__name__)

class InventoryService:
    """
    Authoritative service handling atomic stock adjustments, user-defined FIFO batch
    registrations, IMEI handset lifecycles, and synchronized Double-Entry General Ledger postings.
    """

    # =========================================================================
    # 1. ATOMIC STOCK ADJUSTMENTS & AUDIT LEDGERS
    # =========================================================================
    @classmethod
    @transaction.atomic
    def adjust_stock(
        cls,
        product: Product,
        branch: Branch,
        quantity_delta: Decimal,
        movement_type: str,
        reference_doc: str = "",
        imei_or_serial: str = "",
        remarks: str = "",
        user=None,
        allow_negative: bool = False
    ) -> BranchStock:
        """
        Atomically modifies branch stock balance with database row-level locking,
        records an immutable stock movement log, and dispatches Double-Entry
        General Ledger postings for physical inventory discrepancies.
        """
        branch_stock, _ = BranchStock.objects.select_for_update().get_or_create(
            branch=branch,
            product=product,
            defaults={
                'quantity': Decimal('0.000'),
                'reserved_quantity': Decimal('0.000'),
                'quarantined_defective_quantity': Decimal('0.000'),
                'low_stock_threshold': product.reorder_level or Decimal('5.00')
            }
        )

        previous_qty = branch_stock.quantity
        new_qty = previous_qty + quantity_delta

        if new_qty < Decimal('0.000') and not allow_negative:
            raise ValidationError(
                f"Insufficient stock for '{product.name}' at {branch.name}. "
                f"Available: {previous_qty} {product.base_unit.code}, Requested reduction: {abs(quantity_delta)}"
            )

        branch_stock.quantity = new_qty
        branch_stock.save(update_fields=['quantity', 'updated_at'])

        clean_ref_doc = reference_doc or f"ADJ-{movement_type[:3]}-{product.id}-{uuid.uuid4().hex[:6].upper()}"

        StockMovementLog.objects.create(
            product=product,
            branch=branch,
            movement_type=movement_type,
            quantity_delta=quantity_delta,
            previous_quantity=previous_qty,
            new_quantity=new_qty,
            reference_document=clean_ref_doc,
            imei_or_serial_number=imei_or_serial,
            remarks=remarks,
            user=user
        )

        # Dispatch Double-Entry GL Vouchers for Physical Discrepancies
        if movement_type in ['ADJUSTMENT_SUB', 'ADJUSTMENT_ADD']:
            cls._post_adjustment_to_gl(
                product=product,
                branch=branch,
                quantity_delta=quantity_delta,
                movement_type=movement_type,
                reference_doc=clean_ref_doc,
                remarks=remarks,
                user=user
            )

        return branch_stock

    @classmethod
    def _post_adjustment_to_gl(
        cls,
        product: Product,
        branch: Branch,
        quantity_delta: Decimal,
        movement_type: str,
        reference_doc: str,
        remarks: str = "",
        user=None
    ) -> None:
        """
        Posts balanced double-entry vouchers to the General Ledger:
        - ADJUSTMENT_SUB: Dr. 6160 Inventory Shrinkage / Cr. 1310 Merchandise Inventory Asset.
        - ADJUSTMENT_ADD: Dr. 1310 Merchandise Inventory Asset / Cr. 4030 Inventory Audit Surplus Gain.
        """
        try:
            from apps.accounting.models import JournalEntry
            from apps.accounting.services.auto_posting import JournalEngine, AutoPostingService

            # Prevent duplicate voucher postings
            if JournalEntry.objects.filter(
                voucher_type='JOURNAL',
                reference_document=reference_doc,
                status='POSTED'
            ).exists():
                return

            qty_abs = abs(Decimal(str(quantity_delta)))
            unit_cost = product.purchase_price if product.purchase_price is not None else Decimal('0.00')
            total_valuation = (qty_abs * unit_cost).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            if total_valuation <= Decimal('0.00'):
                return

            # Aligned with Nepal Standard Chart of Accounts (COA 1310)
            inv_asset_acc = AutoPostingService.get_or_create_control_account(
                branch, 'INVENTORY_ASSET', '1310', 'Merchandise Inventory Asset', 'ASSET', 'DEBIT'
            )

            lines = []
            if movement_type == 'ADJUSTMENT_SUB':
                # Write-down: Debit 6160 Shrinkage Expense, Credit 1310 Inventory Asset
                shrinkage_acc = AutoPostingService.get_or_create_control_account(
                    branch, 'INVENTORY_SHRINKAGE', '6160', 'Inventory Shrinkage, Breakage & Loss', 'INDIRECT_EXPENSE', 'DEBIT'
                )
                line_narr = f"Inventory write-down: {product.name} x {qty_abs} ({remarks or 'Stock Reduction'})"
                lines.append({
                    'account': shrinkage_acc,
                    'debit': total_valuation,
                    'credit': Decimal('0.00'),
                    'narration': line_narr
                })
                lines.append({
                    'account': inv_asset_acc,
                    'debit': Decimal('0.00'),
                    'credit': total_valuation,
                    'narration': f"Relieve inventory asset for {product.name} (Loss: Rs. {total_valuation:.2f})"
                })
                entry_narration = f"Inventory Shrinkage & Damage Write-off: {product.name} x {qty_abs} (Loss: Rs. {total_valuation:.2f})"

            else:  # ADJUSTMENT_ADD
                # Audit Surplus: Debit 1310 Inventory Asset, Credit 4030 Stock Surplus Gain
                surplus_acc = AutoPostingService.get_or_create_control_account(
                    branch, 'INVENTORY_SURPLUS', '4030', 'Inventory Audit Surplus & Stock Gain', 'REVENUE', 'CREDIT'
                )
                line_narr = f"Inventory audit gain: {product.name} x {qty_abs} ({remarks or 'Stock Count Surplus'})"
                lines.append({
                    'account': inv_asset_acc,
                    'debit': total_valuation,
                    'credit': Decimal('0.00'),
                    'narration': f"Increase inventory asset for found stock: {product.name}"
                })
                lines.append({
                    'account': surplus_acc,
                    'debit': Decimal('0.00'),
                    'credit': total_valuation,
                    'narration': line_narr
                })
                entry_narration = f"Inventory Audit Surplus & Stock Gain: {product.name} x {qty_abs} (Value: Rs. {total_valuation:.2f})"

            JournalEngine.create_balanced_entry(
                voucher_type='JOURNAL',
                date_ad=timezone.now().date(),
                branch=branch,
                lines=lines,
                narration=entry_narration,
                reference_doc=reference_doc,
                user=user,
                auto_post=True
            )
        except ValidationError:
            raise
        except Exception as err:
            logger.error(f"[Inventory GL Adjustment Error] Product '{product.name}' ({movement_type}): {err}", exc_info=True)
            raise ValidationError(
                f"Failed to post General Ledger voucher for inventory adjustment on '{product.name}': {err}"
            ) from err

    # =========================================================================
    # 2. INWARD FIFO BATCH RECORDING (USER-DEFINED BATCH PRIORITIZATION)
    # =========================================================================
    @classmethod
    @transaction.atomic
    def record_inward_stock_batch(
        cls,
        product: Product,
        branch: Branch,
        quantity: Decimal,
        cost_price: Decimal,
        selling_price: Optional[Decimal] = None,
        purchase_date: Optional[date] = None,
        batch_number: Optional[str] = None,
        supplier_name: str = "",
        grn_reference: str = "",
        expiry_date: Optional[date] = None
    ) -> ProductBatch:
        """
        Registers an inward FIFO inventory batch:
        - Strictly prioritizes the user-entered batch number from the GRN row (e.g. BT-2026-A1).
        - If left blank or empty, generates an internal fallback code: BATCH-{YYMMDD}-{PRODUCT_ID}-{UUID}.
        - Correctly initializes `quantity_received` and `quantity_remaining`.
        - Tracks FIFO inventory for non-serialized accessories and backlog phones received without IMEIs.
        """
        if quantity <= Decimal('0.000'):
            raise ValidationError(f"Batch quantity for '{product.name}' must be greater than zero.")

        # 1. Prioritize user-entered physical batch code
        clean_batch_no = str(batch_number or '').strip()
        if not clean_batch_no:
            date_str = (purchase_date or timezone.now().date()).strftime('%y%m%d')
            clean_batch_no = f"BATCH-{date_str}-{product.id}-{uuid.uuid4().hex[:4].upper()}"
        else:
            # Clean and truncate to schema limit (max 100 characters)
            clean_batch_no = clean_batch_no[:100]

        target_purchase_date = purchase_date or timezone.now().date()
        target_selling_price = selling_price if (selling_price is not None and selling_price > Decimal('0.00')) else product.selling_price

        batch = ProductBatch.objects.create(
            batch_number=clean_batch_no,
            product=product,
            branch=branch,
            purchase_date=target_purchase_date,
            cost_price=cost_price,
            selling_price=target_selling_price,
            quantity_received=quantity,
            quantity_remaining=quantity,
            is_depleted=(quantity <= Decimal('0.000')),
            supplier_name=supplier_name or "",
            grn_reference=grn_reference or "",
            expiry_date=expiry_date
        )

        return batch

    # Convenient method alias
    record_inward_batch = record_inward_stock_batch

    # =========================================================================
    # 3. FIFO BATCH DEPLETION ENGINE (FOR SALES & RETURNS)
    # =========================================================================
    @classmethod
    @transaction.atomic
    def deplete_fifo_batches(
        cls,
        product: Product,
        branch: Branch,
        quantity: Decimal
    ) -> Tuple[Decimal, Optional[str]]:
        """
        Depletes active FIFO batches for non-serialized items or backlog handsets:
        - Orders batches by oldest purchase_date first.
        - Atomically decrements quantity_remaining with row-level locks.
        - Sets is_depleted=True when quantity_remaining hits 0.000.
        - Returns: (weighted_average_cost_price, last_depleted_batch_reference)
        """
        available_batches = ProductBatch.objects.select_for_update().filter(
            product=product,
            branch=branch,
            is_depleted=False
        ).order_by('purchase_date', 'created_at', 'id')

        remaining_needed = quantity
        weighted_cost_sum = Decimal('0.00')
        total_depleted = Decimal('0.000')
        last_batch_ref = None

        for batch in available_batches:
            if remaining_needed <= Decimal('0.000'):
                break

            last_batch_ref = batch.batch_number

            if batch.quantity_remaining >= remaining_needed:
                weighted_cost_sum += (remaining_needed * batch.cost_price)
                total_depleted += remaining_needed
                batch.quantity_remaining -= remaining_needed
                batch.is_depleted = (batch.quantity_remaining <= Decimal('0.000'))
                batch.save(update_fields=['quantity_remaining', 'is_depleted', 'updated_at'])
                remaining_needed = Decimal('0.000')
                break
            else:
                weighted_cost_sum += (batch.quantity_remaining * batch.cost_price)
                total_depleted += batch.quantity_remaining
                remaining_needed -= batch.quantity_remaining
                batch.quantity_remaining = Decimal('0.000')
                batch.is_depleted = True
                batch.save(update_fields=['quantity_remaining', 'is_depleted', 'updated_at'])

        if total_depleted > Decimal('0.000'):
            actual_unit_cost = (weighted_cost_sum / total_depleted).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        else:
            actual_unit_cost = product.purchase_price or Decimal('0.00')

        return actual_unit_cost, last_batch_ref

    # =========================================================================
    # 4. INWARD IMEI SERIAL REGISTRATION
    # =========================================================================
    @classmethod
    @transaction.atomic
    def register_inward_imei_unit(
        cls,
        product: Product,
        branch: Branch,
        imei_1: str,
        imei_2: Optional[str] = None,
        serial_number: str = "",
        condition: str = 'BRAND_NEW',
        activation_status: str = 'SEALED_INACTIVE',
        purchase_ref: str = "",
        batch_ref: str = "",
        supplier_name: str = "",
        landed_cost: Decimal = Decimal('0.00'),
        purchase_date: Optional[date] = None,
        mdms_status: str = 'REGISTERED_OFFICIAL'
    ) -> ItemInstance:
        """
        Registers a physical handset instance during inward purchase or GRN receiving.
        - Links the user-defined custom `batch_ref` directly to `ItemInstance.batch_reference`.
        - Normalizes empty strings to None to prevent database unique constraint collisions.
        - Automatically detects Dual-SIM configuration and sets `imei_2_pending_scan` flag.
        """
        clean_imei_1 = str(imei_1).strip() if imei_1 and str(imei_1).strip() else None
        clean_imei_2 = str(imei_2).strip() if imei_2 and str(imei_2).strip() else None
        clean_sn = str(serial_number).strip() if serial_number and str(serial_number).strip() else None
        clean_batch_ref = str(batch_ref).strip()[:100] if batch_ref and str(batch_ref).strip() else None

        if clean_imei_1:
            existing = ItemInstance.objects.filter(imei_1=clean_imei_1, status='IN_STOCK').first()
            if existing:
                raise ValidationError(f"IMEI 1 '{clean_imei_1}' is already registered as active stock (Branch: {existing.branch.name}).")

        if clean_imei_2:
            existing_2 = ItemInstance.objects.filter(imei_2=clean_imei_2, status='IN_STOCK').first()
            if existing_2:
                raise ValidationError(f"IMEI 2 '{clean_imei_2}' is already registered as active stock (Branch: {existing_2.branch.name}).")

        is_dual_sim = getattr(product, 'sim_configuration', 'DUAL_SIM') in ['DUAL_SIM', 'ESIM_DUAL']
        pending_scan = is_dual_sim and (clean_imei_2 is None)

        instance = ItemInstance.objects.create(
            product=product,
            branch=branch,
            device_uid=f"DEV-{uuid.uuid4().hex[:12].upper()}",
            imei_1=clean_imei_1,
            imei_2=clean_imei_2,
            imei_2_pending_scan=pending_scan,
            serial_number=clean_sn,
            condition=condition,
            activation_status=activation_status,
            status='IN_STOCK',
            purchase_reference=purchase_ref,
            batch_reference=clean_batch_ref,
            supplier_name=supplier_name,
            landed_cost=landed_cost,
            purchase_date=purchase_date or timezone.now().date(),
            mdms_status=mdms_status,
            device_barcode=clean_imei_1 or clean_sn or product.barcode
        )
        return instance

    # =========================================================================
    # 5. DEVICE COMPONENT WARRANTY SCHEDULES
    # =========================================================================
    @classmethod
    @transaction.atomic
    def initialize_device_component_warranties(
        cls,
        item_instance: ItemInstance,
        sale_date: date,
        custom_warranty_months: Optional[int] = None
    ) -> List[DeviceComponentWarranty]:
        """
        Creates individual component warranty ledger records upon POS phone checkout:
        - Main Body (12M): Exactly 365 days from sale date.
        - Battery (6M): Exactly 180 days from sale date.
        - Screen (3M): Exactly 90 days from sale date.

        Stamps the statutory exclusion note:
        "Covers manufacturing defects only. Void if physical drop cracks or liquid damage found."
        into the customer's permanent warranty certificate.
        """
        product = item_instance.product
        created_records = []
        rules = product.component_warranty_rules.all()

        standard_exclusion = (
            "Covers genuine manufacturing defects only. "
            "Void if physical drop cracks or liquid damage found."
        )

        if rules.exists():
            for rule in rules:
                comp_type = (rule.component_type or '').upper()
                months = rule.warranty_months if rule.warranty_months is not None else 0

                # Compute exact calendar durations
                if months == 0:
                    duration_days = 0
                elif comp_type == 'SCREEN' and months == 3:
                    duration_days = 90
                elif comp_type == 'BATTERY' and months == 6:
                    duration_days = 180
                elif comp_type == 'DEVICE' and months == 12:
                    duration_days = 365
                elif months == 3:
                    duration_days = 90
                elif months == 6:
                    duration_days = 180
                elif months == 12:
                    duration_days = 365
                elif months > 0:
                    duration_days = months * 30
                else:
                    duration_days = 0

                end_date = sale_date + timedelta(days=duration_days)
                rule_conditions = (rule.coverage_conditions or "").strip()
                remarks = rule_conditions or standard_exclusion

                cw = DeviceComponentWarranty.objects.create(
                    item_instance=item_instance,
                    component_type=rule.component_type,
                    component_name=rule.component_name,
                    warranty_months=rule.warranty_months,
                    warranty_start_date=sale_date,
                    warranty_expiry_date=end_date,
                    status='ACTIVE',
                    remarks=remarks
                )
                created_records.append(cw)
        else:
            # Fallback when no component rules exist on the product
            if product.requires_imei_tracking:
                default_rules = [
                    ('DEVICE', 'Main Handset Body & Motherboard', custom_warranty_months or product.warranty_months or 12, 365),
                    ('BATTERY', 'Internal Battery', 6, 180),
                    ('SCREEN', 'Screen / Display Panel', 3, 90),
                ]
                for c_type, c_name, c_months, c_days in default_rules:
                    days = 365 if c_months == 12 else (180 if c_months == 6 else (90 if c_months == 3 else c_months * 30))
                    end_date = sale_date + timedelta(days=days)
                    cw = DeviceComponentWarranty.objects.create(
                        item_instance=item_instance,
                        component_type=c_type,
                        component_name=c_name,
                        warranty_months=c_months,
                        warranty_start_date=sale_date,
                        warranty_expiry_date=end_date,
                        status='ACTIVE',
                        remarks=standard_exclusion
                    )
                    created_records.append(cw)
            else:
                overall_months = custom_warranty_months or product.warranty_months or 12
                days = 365 if overall_months == 12 else overall_months * 30
                end_date = sale_date + timedelta(days=days)
                cw = DeviceComponentWarranty.objects.create(
                    item_instance=item_instance,
                    component_type='DEVICE',
                    component_name="Main Product Warranty",
                    warranty_months=overall_months,
                    warranty_start_date=sale_date,
                    warranty_expiry_date=end_date,
                    status='ACTIVE',
                    remarks=standard_exclusion
                )
                created_records.append(cw)

        return created_records