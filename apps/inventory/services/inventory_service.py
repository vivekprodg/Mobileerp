from decimal import Decimal
from datetime import date, timedelta
from typing import Optional, List
from django.db import transaction
from django.core.exceptions import ValidationError

from apps.inventory.models import (
    Product, BranchStock, ItemInstance,
    DeviceComponentWarranty, StockMovementLog
)
from apps.branches.models import Branch


class InventoryService:
    """
    Business logic service handling atomic stock updates,
    IMEI handset lifecycles, and component-level warranty calculations.
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
        """Atomically modifies branch stock balance with row locking and records an immutable log."""
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

        StockMovementLog.objects.create(
            product=product,
            branch=branch,
            movement_type=movement_type,
            quantity_delta=quantity_delta,
            previous_quantity=previous_qty,
            new_quantity=new_qty,
            reference_document=reference_doc,
            imei_or_serial_number=imei_or_serial,
            remarks=remarks,
            user=user
        )

        return branch_stock

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
            existing = ItemInstance.objects.filter(imei_1=clean_imei_1).first()
            if existing:
                raise ValidationError(f"IMEI 1 '{clean_imei_1}' is already registered in the system (Status: {existing.status}).")

        if clean_imei_2:
            existing_2 = ItemInstance.objects.filter(imei_2=clean_imei_2).first()
            if existing_2:
                raise ValidationError(f"IMEI 2 '{clean_imei_2}' is already registered in the system (Status: {existing_2.status}).")

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