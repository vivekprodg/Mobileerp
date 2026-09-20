import uuid
import logging
from decimal import Decimal, ROUND_HALF_UP
from datetime import date, timedelta
from typing import Optional, List
from django.db import transaction
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.inventory.models import (
    Product, BranchStock, ItemInstance,
    DeviceComponentWarranty, StockMovementLog
)
from apps.branches.models import Branch

logger = logging.getLogger(__name__)


class InventoryService:
    """
    Business logic service handling atomic stock updates,
    IMEI handset lifecycles, component-level warranty calculations,
    and automatic Double-Entry General Ledger write-down / surplus postings.
    """

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
        General Ledger postings for manual inventory adjustments:
        - ADJUSTMENT_SUB (Damaged/Lost): Dr. Inventory Shrinkage Expense / Cr. Merchandise Inventory Asset
        - ADJUSTMENT_ADD (Found/Surplus): Dr. Merchandise Inventory Asset / Cr. Inventory Audit Surplus Gain

        If GL posting fails, the entire transaction rolls back cleanly.
        """
        branch_stock, _ = BranchStock.objects.select_for_update().get_or_create(
            branch=branch,
            product=product,
            defaults={'quantity': Decimal('0.000'), 'reserved_quantity': Decimal('0.000')}
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

        # ---------------------------------------------------------------------
        # GENERAL LEDGER AUTOMATIC DOUBLE-ENTRY POSTING FOR STOCK DISCREPANCIES
        # ---------------------------------------------------------------------
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
        Posts balanced double-entry vouchers to the General Ledger for inventory adjustments:
        - ADJUSTMENT_SUB: Dr. Inventory Shrinkage Expense / Cr. Merchandise Inventory Asset
        - ADJUSTMENT_ADD: Dr. Merchandise Inventory Asset / Cr. Inventory Audit Surplus Gain

        Strictly enforces GL synchronization without swallowing errors. If account resolution
        or voucher posting fails, raises ValidationError to abort the outer transaction.
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

            inv_asset_acc = AutoPostingService.get_or_create_control_account(
                branch, 'INVENTORY_ASSET', '1040', 'Merchandise Inventory Asset', 'ASSET', 'DEBIT'
            )

            lines = []
            if movement_type == 'ADJUSTMENT_SUB':
                # Write-down: Debit Shrinkage Expense, Credit Inventory Asset
                shrinkage_acc = AutoPostingService.get_or_create_control_account(
                    branch, 'INVENTORY_SHRINKAGE', '5040', 'Inventory Shrinkage & Damage Expense', 'INDIRECT_EXPENSE', 'DEBIT'
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
                # Audit Surplus: Debit Inventory Asset, Credit Stock Surplus Gain
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
        purchase_date: date = None
    ) -> ItemInstance:
        """
        Registers a physical handset instance during inward purchase or GRN receiving.
        Normalizes IMEI 2 to None if not provided or empty to prevent unique constraint crashes.
        """
        clean_imei_1 = imei_1.strip() if imei_1 and imei_1.strip() else None
        clean_imei_2 = imei_2.strip() if imei_2 and imei_2.strip() else None
        clean_sn = serial_number.strip() if serial_number and serial_number.strip() else None

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
            imei_1=clean_imei_1,
            imei_2=clean_imei_2,
            imei_2_pending_scan=pending_scan,
            serial_number=clean_sn,
            condition=condition,
            activation_status=activation_status,
            status='IN_STOCK',
            purchase_reference=purchase_ref,
            batch_reference=batch_ref,
            supplier_name=supplier_name,
            landed_cost=landed_cost,
            purchase_date=purchase_date or date.today(),
            device_barcode=clean_imei_1 or clean_sn or product.barcode
        )
        return instance

    @classmethod
    @transaction.atomic
    def initialize_device_component_warranties(
        cls,
        item_instance: ItemInstance,
        sale_date: date,
        custom_warranty_months: Optional[int] = None
    ) -> List[DeviceComponentWarranty]:
        """
        Creates individual component warranty ledger records upon POS phone sale:
        e.g., Device Body = 12M, Screen = 6M, Battery = 6M, Charger = 6M.
        """
        product = item_instance.product
        created_records = []
        rules = product.component_warranty_rules.all()

        if rules.exists():
            for rule in rules:
                end_date = sale_date + timedelta(days=rule.warranty_months * 30)
                cw = DeviceComponentWarranty.objects.create(
                    item_instance=item_instance,
                    component_type=rule.component_type,
                    component_name=rule.component_name,
                    warranty_months=rule.warranty_months,
                    warranty_start_date=sale_date,
                    warranty_expiry_date=end_date,
                    status='ACTIVE',
                    remarks=rule.coverage_conditions or ""
                )
                created_records.append(cw)
        else:
            overall_months = custom_warranty_months or product.warranty_months or 12
            end_date = sale_date + timedelta(days=overall_months * 30)
            cw = DeviceComponentWarranty.objects.create(
                item_instance=item_instance,
                component_type='DEVICE',
                component_name="Main Handset & Motherboard",
                warranty_months=overall_months,
                warranty_start_date=sale_date,
                warranty_expiry_date=end_date,
                status='ACTIVE',
                remarks="Full handset official manufacturer warranty"
            )
            created_records.append(cw)

        return created_records