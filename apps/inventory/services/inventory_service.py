"""
Inventory Management & Business Logic Service.

Key Capabilities:
1. Atomic Stock Adjustments & Movement Logs:
   - Row-level database locking (select_for_update) on BranchStock.
   - Comprehensive audit logging via StockMovementLog.
   - Automatic Double-Entry General Ledger postings for physical audit variances:
     * ADJUSTMENT_SUB: Dr. 6160 Inventory Shrinkage / Cr. 1310 Merchandise Inventory Asset.
     * ADJUSTMENT_ADD: Dr. 1310 Merchandise Inventory Asset / Cr. 4030 Inventory Audit Surplus Gain.
2. Inward FIFO Batch & Depletion Engine:
   - Prioritizes user-entered vendor batch numbers from GRN line items.
   - Depletes oldest purchase batches with row-level locks on sales or transfers.
3. Inward IMEI Registration & Dual-SIM Compliance:
   - Registers physical device instances, tags MDMS status, and tracks dual-SIM pending scans.
4. Component-Level Customer Warranty Schedules:
   - Sets exact calendar expiration dates for Main Body (12M), Battery (6M), and Screen (3M).
5. Enterprise Dashboard Multi-Warehouse Aggregations:
   - High-level multi-warehouse stock rollups (SKU counts, total units, asset valuation per warehouse).
6. Stock Movement Velocity & Timeline Series:
   - Aggregates Stock In, Stock Out, Transfers, and Adjustments across dynamic time horizons (7D, 30D, 3M)
     with timeline coordinate series for direct SVG chart rendering.
7. Real-Time Inventory Alerts Engine:
   - Dynamic threshold computation for Low Stock, Out of Stock, Reorder Required,
     Audit Discrepancies, and Expiring FIFO Batches.
8. Direct Inter-Warehouse Stock Transfer Engine:
   - Executes atomic multi-location transfers with instant stock balance updates,
     dual StockMovementLog auditing, and physical serialized handset re-allocation.
"""

import uuid
import logging
from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime, timedelta
from typing import Optional, List, Tuple, Dict, Any

from django.db import transaction
from django.db.models import (
    F, Q, Sum, Count, DecimalField, ExpressionWrapper, Value
)
from django.db.models.functions import Coalesce, TruncDate
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
    Authoritative service handling atomic stock adjustments, multi-warehouse rollups,
    period-based movement velocity calculations, inventory alerts, FIFO batches,
    IMEI handset lifecycles, and synchronized Double-Entry General Ledger postings.
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
            base_unit_label = getattr(product.base_unit, 'code', 'Units') if product.base_unit else 'Units'
            raise ValidationError(
                f"Insufficient stock for '{product.name}' at {branch.name}. "
                f"Available: {previous_qty} {base_unit_label}, Requested reduction: {abs(quantity_delta)}"
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

        clean_batch_no = str(batch_number or '').strip()
        if not clean_batch_no:
            date_str = (purchase_date or timezone.now().date()).strftime('%y%m%d')
            clean_batch_no = f"BATCH-{date_str}-{product.id}-{uuid.uuid4().hex[:4].upper()}"
        else:
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

        Stamps statutory exclusion note into customer permanent warranty record.
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

    # =========================================================================
    # 6. WAREHOUSE SUMMARY AGGREGATION METHOD
    # =========================================================================
    @classmethod
    def get_warehouse_summary(cls) -> List[Dict[str, Any]]:
        """
        Aggregates product catalog counts, physical unit quantities, and total
        monetary asset valuations across all registered warehouse branches.
        Returns a structured summary list matching the Warehouse Overview dashboard table.
        """
        branches = Branch.objects.all().order_by('-is_main_branch', 'name')
        summary = []

        cost_val_expr = ExpressionWrapper(
            F('quantity') * F('product__purchase_price'),
            output_field=DecimalField(max_digits=18, decimal_places=2)
        )

        for branch in branches:
            branch_stocks = BranchStock.objects.filter(branch=branch)

            agg = branch_stocks.aggregate(
                distinct_products=Count('product_id', distinct=True),
                total_units=Coalesce(
                    Sum('quantity'),
                    Value(Decimal('0.000'), output_field=DecimalField(max_digits=18, decimal_places=3))
                ),
                total_val=Coalesce(
                    Sum(cost_val_expr),
                    Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
                )
            )

            summary.append({
                'id': branch.id,
                'name': branch.name,
                'code': branch.code,
                'is_main_branch': branch.is_main_branch,
                'products_count': agg['distinct_products'] or 0,
                'stock_quantity': int(agg['total_units'] or 0),
                'inventory_value': agg['total_val'] or Decimal('0.00'),
                'status': 'Active' if branch.is_active else 'Inactive',
                'is_active': branch.is_active
            })

        return summary

    # =========================================================================
    # 7. STOCK MOVEMENT VELOCITY & TIMELINE SERIES
    # =========================================================================
    @classmethod
    def get_stock_movement_metrics(
        cls,
        period: str = '7d',
        branch: Optional[Branch] = None
    ) -> Dict[str, Any]:
        """
        Computes stock movement velocities over specified time horizons ('today', '7d', '30d', '3m').
        Aggregates four movement buckets:
          - Stock In: Purchases, Transfers In, Positive Adjustments, Trade-In Acquisitions, RMA Replacements
          - Stock Out: Counter Sales, Transfers Out, Damage/Loss Adjustments, Trade-In Sales
          - Transfers: All Inter-Branch Movements
          - Adjustments: Physical Count Discrepancies (+ and -)
        Also compiles parallel timeline coordinate arrays (labels, stock_in, stock_out)
        for direct rendering by the frontend SVG chart engine.
        """
        today = timezone.now().date()

        if period == 'today':
            start_date = today
            days_count = 1
        elif period == '30d':
            start_date = today - timedelta(days=29)
            days_count = 30
        elif period == '3m':
            start_date = today - timedelta(days=89)
            days_count = 90
        else:  # Default '7d'
            period = '7d'
            start_date = today - timedelta(days=6)
            days_count = 7

        base_qs = StockMovementLog.objects.filter(
            created_at__date__gte=start_date,
            created_at__date__lte=today
        )
        if branch:
            base_qs = base_qs.filter(branch=branch)

        IN_TYPES = [
            'PURCHASE', 'TRANSFER_IN', 'ADJUSTMENT_ADD',
            'TRADE_IN_ACQUISITION', 'RMA_VENDOR_REPLACEMENT_IN', 'SALE_RETURN'
        ]
        OUT_TYPES = [
            'SALE', 'TRANSFER_OUT', 'ADJUSTMENT_SUB',
            'TRADE_IN_SALE', 'SERVICE_REPLACED_PART_DEDUCT', 'RMA_VENDOR_DISPATCH'
        ]
        TRANSFER_TYPES = ['TRANSFER_IN', 'TRANSFER_OUT']
        ADJUSTMENT_TYPES = ['ADJUSTMENT_ADD', 'ADJUSTMENT_SUB']

        # Aggregate total metric sums
        in_sum = base_qs.filter(movement_type__in=IN_TYPES).aggregate(
            total=Coalesce(Sum('quantity_delta'), Value(Decimal('0.000'), output_field=DecimalField(max_digits=14, decimal_places=3)))
        )['total']

        out_sum = base_qs.filter(movement_type__in=OUT_TYPES).aggregate(
            total=Coalesce(Sum('quantity_delta'), Value(Decimal('0.000'), output_field=DecimalField(max_digits=14, decimal_places=3)))
        )['total']

        tr_sum = base_qs.filter(movement_type__in=TRANSFER_TYPES).aggregate(
            total=Coalesce(Sum('quantity_delta'), Value(Decimal('0.000'), output_field=DecimalField(max_digits=14, decimal_places=3)))
        )['total']

        adj_sum = base_qs.filter(movement_type__in=ADJUSTMENT_TYPES).aggregate(
            total=Coalesce(Sum('quantity_delta'), Value(Decimal('0.000'), output_field=DecimalField(max_digits=14, decimal_places=3)))
        )['total']

        # Format KPI strings
        in_val = int(abs(in_sum))
        out_val = int(abs(out_sum))
        tr_val = int(abs(tr_sum))
        adj_net = int(adj_sum)

        in_str = f"+{in_val:,}"
        out_str = f"-{out_val:,}"
        tr_str = f"{tr_val:,}"
        adj_str = f"{adj_net:+,}" if adj_net != 0 else "0"

        # Generate timeline buckets for SVG chart
        timeline_labels = []
        timeline_in = []
        timeline_out = []

        if period in ['today', '7d']:
            # Day-by-day intervals
            curr = start_date
            while curr <= today:
                day_label = curr.strftime('%a') if period == '7d' else curr.strftime('%H:00')
                timeline_labels.append(day_label)

                day_logs = base_qs.filter(created_at__date=curr)
                day_in = day_logs.filter(movement_type__in=IN_TYPES).aggregate(
                    t=Coalesce(Sum('quantity_delta'), Value(Decimal('0.000'), output_field=DecimalField(max_digits=14, decimal_places=3)))
                )['t']
                day_out = day_logs.filter(movement_type__in=OUT_TYPES).aggregate(
                    t=Coalesce(Sum('quantity_delta'), Value(Decimal('0.000'), output_field=DecimalField(max_digits=14, decimal_places=3)))
                )['t']

                timeline_in.append(int(abs(day_in)))
                timeline_out.append(int(abs(day_out)))
                curr += timedelta(days=1)

        elif period == '30d':
            # 4-Week intervals
            curr = start_date
            week_idx = 1
            while curr <= today:
                week_end = min(curr + timedelta(days=6), today)
                timeline_labels.append(f"W{week_idx}")

                week_logs = base_qs.filter(created_at__date__gte=curr, created_at__date__lte=week_end)
                w_in = week_logs.filter(movement_type__in=IN_TYPES).aggregate(
                    t=Coalesce(Sum('quantity_delta'), Value(Decimal('0.000'), output_field=DecimalField(max_digits=14, decimal_places=3)))
                )['t']
                w_out = week_logs.filter(movement_type__in=OUT_TYPES).aggregate(
                    t=Coalesce(Sum('quantity_delta'), Value(Decimal('0.000'), output_field=DecimalField(max_digits=14, decimal_places=3)))
                )['t']

                timeline_in.append(int(abs(w_in)))
                timeline_out.append(int(abs(w_out)))
                curr = week_end + timedelta(days=1)
                week_idx += 1

        else:  # '3m'
            # 3 Monthly intervals
            for m_offset in range(2, -1, -1):
                m_target_date = today - timedelta(days=m_offset * 30)
                m_start = m_target_date - timedelta(days=14)
                m_end = min(m_target_date + timedelta(days=15), today)

                month_name = m_target_date.strftime('%b')
                timeline_labels.append(month_name)

                m_logs = base_qs.filter(created_at__date__gte=m_start, created_at__date__lte=m_end)
                m_in = m_logs.filter(movement_type__in=IN_TYPES).aggregate(
                    t=Coalesce(Sum('quantity_delta'), Value(Decimal('0.000'), output_field=DecimalField(max_digits=14, decimal_places=3)))
                )['t']
                m_out = m_logs.filter(movement_type__in=OUT_TYPES).aggregate(
                    t=Coalesce(Sum('quantity_delta'), Value(Decimal('0.000'), output_field=DecimalField(max_digits=14, decimal_places=3)))
                )['t']

                timeline_in.append(int(abs(m_in)))
                timeline_out.append(int(abs(m_out)))

        return {
            'period': period,
            'inVal': in_str,
            'outVal': out_str,
            'trVal': tr_str,
            'adjVal': adj_str,
            'raw_in': in_val,
            'raw_out': out_val,
            'raw_transfers': tr_val,
            'raw_adjustments': adj_net,
            'days': timeline_labels,
            'stockIn': timeline_in,
            'stockOut': timeline_out
        }

    # =========================================================================
    # 8. INVENTORY ALERTS CALCULATION METHOD
    # =========================================================================
    @classmethod
    def get_inventory_alerts(cls, branch: Optional[Branch] = None) -> Dict[str, int]:
        """
        Dynamically computes the five core operational inventory alert counts:
        1. Low Stock: Stock quantity > 0 but <= low_stock_threshold.
        2. Out of Stock: Stock quantity <= 0.
        3. Reorder Required: Available stock (quantity - reserved) <= low_stock_threshold.
        4. Stock Discrepancy: Adjustment records logged within the last 30 days.
        5. Expiring Soon: FIFO batches expiring within the next 60 days.
        """
        today = timezone.now().date()
        thirty_days_ago = today - timedelta(days=30)
        sixty_days_future = today + timedelta(days=60)

        stock_qs = BranchStock.objects.all()
        if branch:
            stock_qs = stock_qs.filter(branch=branch)

        # 1. Low Stock
        low_stock_count = stock_qs.filter(
            quantity__gt=Decimal('0.000'),
            quantity__lte=F('low_stock_threshold')
        ).count()

        # 2. Out of Stock
        out_of_stock_count = stock_qs.filter(
            quantity__lte=Decimal('0.000')
        ).count()

        # 3. Reorder Required (considers reserved customer orders)
        available_expr = ExpressionWrapper(
            F('quantity') - F('reserved_quantity'),
            output_field=DecimalField(max_digits=12, decimal_places=3)
        )
        reorder_count = stock_qs.annotate(calc_available=available_expr).filter(
            calc_available__lte=F('low_stock_threshold')
        ).count()

        # 4. Stock Discrepancy (audit corrections)
        discrepancy_qs = StockMovementLog.objects.filter(
            created_at__date__gte=thirty_days_ago,
            movement_type__in=['ADJUSTMENT_ADD', 'ADJUSTMENT_SUB']
        )
        if branch:
            discrepancy_qs = discrepancy_qs.filter(branch=branch)
        discrepancy_count = discrepancy_qs.count()

        # 5. Expiring Soon (FIFO Batches)
        batch_qs = ProductBatch.objects.filter(
            is_depleted=False,
            expiry_date__isnull=False,
            expiry_date__gte=today,
            expiry_date__lte=sixty_days_future
        )
        if branch:
            batch_qs = batch_qs.filter(branch=branch)
        expiring_count = batch_qs.count()

        return {
            'low_stock': low_stock_count,
            'out_of_stock': out_of_stock_count,
            'reorder_required': reorder_count,
            'stock_discrepancy': discrepancy_count,
            'expiring_soon': expiring_count
        }

    # =========================================================================
    # 9. DIRECT INTER-WAREHOUSE STOCK TRANSFER ENGINE
    # =========================================================================
    @classmethod
    @transaction.atomic
    def execute_direct_warehouse_transfer(
        cls,
        product: Product,
        source_branch: Branch,
        destination_branch: Branch,
        quantity: Decimal,
        scanned_imei_or_serial: str = "",
        transfer_date: Optional[date] = None,
        remarks: str = "",
        user=None
    ) -> Dict[str, Any]:
        """
        Executes an instant atomic inter-warehouse stock transfer:
        1. Validates source and destination branches are distinct.
        2. Validates positive quantity and sufficient available balance at source with select_for_update.
        3. Deducts quantity from source BranchStock (TRANSFER_OUT).
        4. Increments quantity at destination BranchStock (TRANSFER_IN).
        5. Logs dual audit trails in StockMovementLog.
        6. Reassigns physical ItemInstance handset records if moving serialized items.
        """
        if source_branch.pk == destination_branch.pk:
            raise ValidationError("Source and destination warehouses must be different.")

        qty_to_transfer = Decimal(str(quantity))
        if qty_to_transfer <= Decimal('0.000'):
            raise ValidationError("Transfer quantity must be strictly greater than zero.")

        # Lock source stock record
        source_stock, _ = BranchStock.objects.select_for_update().get_or_create(
            branch=source_branch,
            product=product,
            defaults={
                'quantity': Decimal('0.000'),
                'reserved_quantity': Decimal('0.000'),
                'quarantined_defective_quantity': Decimal('0.000'),
                'low_stock_threshold': product.reorder_level or Decimal('5.00')
            }
        )

        if source_stock.available_quantity < qty_to_transfer:
            base_code = getattr(product.base_unit, 'code', 'Units') if product.base_unit else 'Units'
            raise ValidationError(
                f"Insufficient stock for '{product.name}' at {source_branch.name}. "
                f"Available: {source_stock.available_quantity} {base_code}, Requested: {qty_to_transfer}"
            )

        clean_date = transfer_date or timezone.now().date()
        date_str = clean_date.strftime('%y%m%d')
        transfer_doc = f"TRF-{date_str}-{product.id}-{uuid.uuid4().hex[:4].upper()}"

        clean_imei = str(scanned_imei_or_serial or '').strip()
        matched_instance = None

        # Handle serialized handset movement
        if product.requires_imei_tracking:
            if clean_imei:
                matched_instance = ItemInstance.objects.select_for_update().filter(
                    Q(imei_1=clean_imei) | Q(imei_2=clean_imei) | Q(serial_number=clean_imei),
                    product=product,
                    branch=source_branch,
                    status='IN_STOCK'
                ).first()

                if not matched_instance:
                    raise ValidationError(
                        f"Device with IMEI/Serial '{clean_imei}' was not found in available stock at {source_branch.name}."
                    )
            else:
                # If non-explicit IMEI was provided for serialized item, find available unit
                matched_instance = ItemInstance.objects.select_for_update().filter(
                    product=product,
                    branch=source_branch,
                    status='IN_STOCK'
                ).first()

        # Deduct from source branch
        source_prev = source_stock.quantity
        source_stock.quantity -= qty_to_transfer
        source_stock.save(update_fields=['quantity', 'updated_at'])

        StockMovementLog.objects.create(
            product=product,
            branch=source_branch,
            movement_type='TRANSFER_OUT',
            quantity_delta=-qty_to_transfer,
            previous_quantity=source_prev,
            new_quantity=source_stock.quantity,
            reference_document=transfer_doc,
            imei_or_serial_number=clean_imei,
            remarks=f"Direct Transfer to {destination_branch.name}. {remarks}".strip(),
            user=user
        )

        # Increment at destination branch
        dest_stock, _ = BranchStock.objects.select_for_update().get_or_create(
            branch=destination_branch,
            product=product,
            defaults={
                'quantity': Decimal('0.000'),
                'reserved_quantity': Decimal('0.000'),
                'quarantined_defective_quantity': Decimal('0.000'),
                'low_stock_threshold': product.reorder_level or Decimal('5.00')
            }
        )

        dest_prev = dest_stock.quantity
        dest_stock.quantity += qty_to_transfer
        dest_stock.save(update_fields=['quantity', 'updated_at'])

        StockMovementLog.objects.create(
            product=product,
            branch=destination_branch,
            movement_type='TRANSFER_IN',
            quantity_delta=qty_to_transfer,
            previous_quantity=dest_prev,
            new_quantity=dest_stock.quantity,
            reference_document=transfer_doc,
            imei_or_serial_number=clean_imei,
            remarks=f"Direct Transfer from {source_branch.name}. {remarks}".strip(),
            user=user
        )

        # Update handset branch location
        if matched_instance:
            matched_instance.branch = destination_branch
            matched_instance.save(update_fields=['branch', 'updated_at'])

        logger.info(
            f"[Direct Stock Transfer] {qty_to_transfer} units of '{product.name}' transferred "
            f"from {source_branch.name} to {destination_branch.name} (Ref: {transfer_doc})."
        )

        return {
            'success': True,
            'transfer_reference': transfer_doc,
            'product_name': product.name,
            'sku': product.sku,
            'transferred_quantity': qty_to_transfer,
            'source_branch': source_branch.name,
            'source_new_stock': source_stock.quantity,
            'destination_branch': destination_branch.name,
            'destination_new_stock': dest_stock.quantity,
            'imei': clean_imei
        }