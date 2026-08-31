"""
Mathematical Diagnostic Valuation Engine & Police Ownership Undertaking Controller.
File Path: apps/sales/services/trade_in_engine.py

Core Capabilities:
1. 10-Point Technical Diagnostic Valuation:
   - Evaluates 10 hardware and cosmetic diagnostic criteria.
   - Calculates penalty deductions against pristine benchmark market value.
   - Safeguards store profit margins via configurable buffer deduction (default 15.00%).
   - Calculates diagnostic score percentage and assigns standard inventory condition grades (Grade A, B, C).
2. Excess Trade-In Value Settlement:
   - For registered customer accounts: Deposits excess surplus into customer ledger as store credit.
   - For walk-in anonymous customers: Payouts difference as physical cash change.
3. Inventory Restocking & Historical Serial Archiving (DAT-01 Compliance):
   - Archives previous customer ownership records (marking historical ItemInstances as 'ARCHIVED')
     to prevent unique constraint collisions on active 'IN_STOCK' units.
   - Creates new active pre-owned ItemInstance in 'IN_STOCK' status with landed cost equal to buy-back payout.
   - Stamps NTA MDMS compliance status and updates BranchStock counter.
4. Comprehensive Forensic Audit Trail:
   - Generates immutable audit logs for valuation, buy-back acquisitions, and debt settlements.
"""

import uuid
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from datetime import date
from typing import Dict, Any, Optional, Tuple, List

from django.db import transaction
from django.db.models import Q
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.sales.models import (
    PhoneExchangeTradeIn, TradeInInspectionChecklist, TradeInLegalUndertaking, SalesEstimate
)
from apps.inventory.models import (
    Product, ProductCategory, Brand, UnitOfMeasurement, BranchStock, ItemInstance, StockMovementLog
)
from apps.inventory.services import InventoryService
from apps.branches.models import Branch
from apps.core.models import SystemConfiguration, AuditLog
from apps.customers.models import Customer, CustomerUdhaariLedger


class TradeInValuationEngine:
    """
    Mathematical Diagnostic Valuation Engine for Pre-Owned / Old Traded-In Smartphones.
    """

    # Exact percentage penalty deductions corresponding to 10-point inspection criteria
    PENALTY_RATES = {
        'touch_and_display': {
            'PASS_FLAWLESS': Decimal('0.00'),
            'MINOR_SCRATCHES': Decimal('0.05'),
            'GREEN_LINE_BLEED': Decimal('0.35'),
            'CRACKED_GLASS': Decimal('0.40'),
            'DEAD_TOUCH': Decimal('0.60'),
        },
        'front_and_back_cameras': {
            'BOTH_WORKING': Decimal('0.00'),
            'FRONT_DEFECTIVE': Decimal('0.10'),
            'REAR_DEFECTIVE': Decimal('0.15'),
            'BOTH_DEFECTIVE': Decimal('0.25'),
        },
        'charging_and_battery': {
            'HEALTH_GOOD_85_PLUS': Decimal('0.00'),
            'HEALTH_FAIR_70_85': Decimal('0.08'),
            'POOR_DRAINING_FAST': Decimal('0.15'),
            'PORT_FAULTY': Decimal('0.10'),
        },
        'wifi_bluetooth_gps': {
            'ALL_WORKING': Decimal('0.00'),
            'WIFI_FAULTY': Decimal('0.20'),
            'BT_FAULTY': Decimal('0.10'),
            'NO_SIGNAL': Decimal('0.40'),
        },
        'cellular_calling_mic_speaker': {
            'ALL_CLEAR': Decimal('0.00'),
            'MIC_FAULTY': Decimal('0.10'),
            'EARPIECE_FAULTY': Decimal('0.08'),
            'LOUDSPEAKER_CRACKLE': Decimal('0.08'),
        },
        'biometrics_security': {
            'FINGERPRINT_FACEID_OK': Decimal('0.00'),
            'FINGERPRINT_DEAD': Decimal('0.10'),
            'FACEID_FAIL': Decimal('0.18'),
            'NOT_SUPPORTED': Decimal('0.00'),
        },
        'body_frame_condition': {
            'PRISTINE_GRADE_A': Decimal('0.00'),
            'LIGHT_SCUFFS_GRADE_B': Decimal('0.06'),
            'CORNER_DENTS_GRADE_C': Decimal('0.12'),
            'BENT_FRAME_GRADE_D': Decimal('0.25'),
        },
        'liquid_ingress_ldi': {
            'WHITE_NO_LIQUID': Decimal('0.00'),
            'PINK_RED_TRIGGERED': Decimal('0.30'),
        },
        'original_accessories_available': {
            'BOX_AND_ORIGINAL_CHARGER': Decimal('0.00'),
            'CHARGER_ONLY': Decimal('0.04'),
            'BOX_ONLY': Decimal('0.04'),
            'HANDSET_ONLY': Decimal('0.08'),
        },
        'account_lock_factory_reset': {
            'ICLOUD_MI_ACCOUNT_REMOVED': Decimal('0.00'),
            'FRP_LOCKED_CANNOT_RESET': Decimal('1.00'),  # 100% deduction - Prohibited buyback
        }
    }

    @classmethod
    def calculate_valuation(
        cls,
        base_market_value: Decimal,
        checklist_data: Dict[str, str],
        shop_margin_pct: Optional[Decimal] = None
    ) -> Dict[str, Any]:
        """
        Computes itemized deductions, shop margin buffer, condition grade, and final buy-back offer.
        """
        if not isinstance(base_market_value, Decimal):
            try:
                base_market_value = Decimal(str(base_market_value))
            except (InvalidOperation, ValueError, TypeError):
                base_market_value = Decimal('0.00')

        base_market_value = base_market_value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        if base_market_value <= Decimal('0.00'):
            return {
                'final_offer': Decimal('0.00'),
                'total_deductions': Decimal('0.00'),
                'margin_deduction': Decimal('0.00'),
                'score_percent': Decimal('0.00'),
                'condition_grade': 'USED_GRADE_C',
                'deduction_breakdown': []
            }

        config = SystemConfiguration.get_solo()
        margin_pct = shop_margin_pct if shop_margin_pct is not None else config.default_trade_in_margin_percent
        if not isinstance(margin_pct, Decimal):
            margin_pct = Decimal(str(margin_pct))

        total_penalty_pct = Decimal('0.00')
        deduction_lines = []

        is_frp_locked = False

        for category, selected_choice in checklist_data.items():
            rates = cls.PENALTY_RATES.get(category, {})
            penalty = rates.get(selected_choice, Decimal('0.00'))

            if category == 'account_lock_factory_reset' and selected_choice == 'FRP_LOCKED_CANNOT_RESET':
                is_frp_locked = True

            if penalty > Decimal('0.00'):
                penalty_amt = (base_market_value * penalty).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                deduction_lines.append({
                    'category': category,
                    'choice': selected_choice,
                    'penalty_percent': f"{penalty * Decimal('100.00'):.0f}%",
                    'deduction_amount': penalty_amt
                })
                total_penalty_pct += penalty

        # If FRP / iCloud is locked, buy-back is completely prohibited (zero offer)
        if is_frp_locked:
            return {
                'final_offer': Decimal('0.00'),
                'total_deductions': base_market_value,
                'margin_deduction': Decimal('0.00'),
                'score_percent': Decimal('0.00'),
                'condition_grade': 'USED_GRADE_C',
                'deduction_breakdown': [{
                    'category': 'account_lock_factory_reset',
                    'choice': 'FRP_LOCKED_CANNOT_RESET',
                    'penalty_percent': '100%',
                    'deduction_amount': base_market_value
                }]
            }

        # Cap total penalty at 100%
        total_penalty_pct = min(Decimal('1.00'), total_penalty_pct)
        total_deduction_amount = (base_market_value * total_penalty_pct).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        value_after_defects = max(Decimal('0.00'), base_market_value - total_deduction_amount)

        # Apply Shop Profit Buffer Deduction
        margin_deduction_amt = (value_after_defects * (margin_pct / Decimal('100.00'))).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP
        )
        final_offer = max(Decimal('0.00'), value_after_defects - margin_deduction_amt).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP
        )
        score_percent = max(
            Decimal('0.00'),
            (Decimal('100.00') - (total_penalty_pct * Decimal('100.00')))
        ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        # Determine Standard Inventory Condition Grade
        if score_percent >= Decimal('88.00'):
            condition_grade = 'USED_GRADE_A'
        elif score_percent >= Decimal('65.00'):
            condition_grade = 'USED_GRADE_B'
        else:
            condition_grade = 'USED_GRADE_C'

        return {
            'final_offer': final_offer,
            'total_deductions': total_deduction_amount,
            'margin_deduction': margin_deduction_amt,
            'score_percent': score_percent,
            'condition_grade': condition_grade,
            'deduction_breakdown': deduction_lines
        }

    @classmethod
    @transaction.atomic
    def restock_traded_in_phone(
        cls,
        trade_in_voucher: PhoneExchangeTradeIn,
        branch: Branch,
        user=None
    ) -> ItemInstance:
        """
        Restocks a traded-in smartphone into inventory:
        1. Archives any existing historical ItemInstance matching IMEI 1, IMEI 2, or Serial Number
           (DAT-01 fix) to guarantee unique active IN_STOCK constraints are cleanly met.
        2. Matches or registers a catalog Product for pre-owned handsets.
        3. Increments BranchStock counter.
        4. Creates new active ItemInstance with condition grade, landed cost = buy-back offer,
           and NTA MDMS status.
        """
        clean_name = f"{trade_in_voucher.brand_name} {trade_in_voucher.model_name} (Pre-Owned / Trade-In)"

        # 1. Match or Create Catalog Product
        category = ProductCategory.objects.filter(
            Q(name__icontains="Smartphones") | Q(name__icontains="Mobile")
        ).first() or ProductCategory.objects.first()

        brand = Brand.objects.filter(name__iexact=trade_in_voucher.brand_name).first()
        if not brand and trade_in_voucher.brand_name:
            brand = Brand.objects.create(name=trade_in_voucher.brand_name, origin_country="Nepal")

        base_unit = UnitOfMeasurement.objects.filter(code='PCS').first()
        if not base_unit:
            base_unit = UnitOfMeasurement.objects.create(name="Piece", name_np="पिस", code="PCS", allow_decimal=False)

        product = Product.objects.filter(
            name__iexact=clean_name,
            ram__iexact=trade_in_voucher.ram_capacity or '',
            internal_storage__iexact=trade_in_voucher.storage_capacity
        ).first()

        if not product:
            sku_code = f"USED-{trade_in_voucher.brand_name[:3].upper()}-{trade_in_voucher.model_name[:4].upper()}-{uuid.uuid4().hex[:4].upper()}"
            barcode_num = f"890{uuid.uuid4().int % 1000000000:09d}"

            # Suggested retail resale price = buy-back payout + target markup (25%)
            suggested_resale = (trade_in_voucher.final_trade_in_value * Decimal('1.25')).quantize(
                Decimal('100.00'), rounding=ROUND_HALF_UP
            )

            product = Product.objects.create(
                name=clean_name,
                model_name=trade_in_voucher.model_name,
                sku=sku_code,
                barcode=barcode_num,
                category=category,
                brand=brand,
                variant_name=f"{trade_in_voucher.storage_capacity} {trade_in_voucher.color_variant or ''} (Used)".strip(),
                ram=trade_in_voucher.ram_capacity or None,
                internal_storage=trade_in_voucher.storage_capacity,
                color_variant=trade_in_voucher.color_variant or None,
                base_unit=base_unit,
                purchase_price=trade_in_voucher.final_trade_in_value,
                selling_price=suggested_resale,
                requires_imei_tracking=True,
                default_mdms_status=trade_in_voucher.mdms_status,
                warranty_months=1,
                is_vat_applicable=False,
                tax_pricing_type='EXEMPT'
            )

        # 2. Archive any older historical record with this IMEI 1, IMEI 2, or Serial Number
        clean_imei_1 = trade_in_voucher.imei_1.strip() if trade_in_voucher.imei_1 else None
        clean_imei_2 = trade_in_voucher.imei_2.strip() if trade_in_voucher.imei_2 else None
        clean_sn = trade_in_voucher.serial_number.strip() if trade_in_voucher.serial_number else None

        if clean_imei_1:
            ItemInstance.objects.filter(imei_1=clean_imei_1).exclude(status='ARCHIVED').update(
                status='ARCHIVED',
                updated_at=timezone.now()
            )

        if clean_imei_2:
            ItemInstance.objects.filter(imei_2=clean_imei_2).exclude(status='ARCHIVED').update(
                status='ARCHIVED',
                updated_at=timezone.now()
            )

        if clean_sn:
            ItemInstance.objects.filter(serial_number=clean_sn).exclude(status='ARCHIVED').update(
                status='ARCHIVED',
                updated_at=timezone.now()
            )

        # 3. Increment Branch Stock Balance
        InventoryService.adjust_stock(
            product=product,
            branch=branch,
            quantity_delta=Decimal('1.000'),
            movement_type='TRADE_IN_ACQUISITION',
            reference_doc=trade_in_voucher.voucher_number,
            imei_or_serial=clean_imei_1 or clean_sn or "",
            remarks=f"Old Phone Buy-Back from {trade_in_voucher.customer_name_manual}",
            user=user,
            allow_negative=True
        )

        # 4. Create New Active Serialized ItemInstance in IN_STOCK status
        item_instance = ItemInstance.objects.create(
            product=product,
            branch=branch,
            device_uid=f"DEV-{uuid.uuid4().hex[:12].upper()}",
            imei_1=clean_imei_1,
            imei_2=clean_imei_2,
            serial_number=clean_sn,
            status='IN_STOCK',
            condition=trade_in_voucher.recommended_condition_grade,
            activation_status='ACTIVATED',
            source_type='CUSTOMER_EXCHANGE_TRADE_IN',
            trade_in_voucher_reference=trade_in_voucher.voucher_number,
            mdms_status=trade_in_voucher.mdms_status,
            mdms_verification_date=date.today(),
            mdms_remarks=f"Trade-in exchange from {trade_in_voucher.customer_name_manual}",
            purchase_reference=trade_in_voucher.voucher_number,
            supplier_name="Customer Trade-In Buy-Back",
            landed_cost=trade_in_voucher.final_trade_in_value,
            purchase_date=date.today()
        )

        trade_in_voucher.restocked_product = product
        trade_in_voucher.restocked_item_instance = item_instance
        trade_in_voucher.status = 'RESTOCKED'
        trade_in_voucher.save(update_fields=['restocked_product', 'restocked_item_instance', 'status', 'updated_at'])

        AuditLog.objects.create(
            user=user,
            branch=branch,
            action_type='TRADE_IN_PURCHASE',
            module='TradeInBuyBack',
            object_repr=trade_in_voucher.voucher_number,
            details={
                'model': trade_in_voucher.model_name,
                'imei': trade_in_voucher.imei_1,
                'payout': str(trade_in_voucher.final_trade_in_value),
                'grade': trade_in_voucher.recommended_condition_grade
            }
        )

        return item_instance

    @classmethod
    @transaction.atomic
    def settle_excess_trade_in_credit(
        cls,
        estimate: SalesEstimate,
        trade_in_voucher: PhoneExchangeTradeIn,
        excess_amount: Decimal,
        user=None
    ) -> Dict[str, Any]:
        """
        Settles surplus trade-in buy-back valuation when the old phone value exceeds bill total:
        - If customer is a registered profile: Deposits remaining amount as store credit in ledger.
        - If walk-in anonymous customer: Payouts difference as cash change returned on bill.
        """
        if excess_amount <= Decimal('0.00'):
            return {'settled': False, 'mode': 'NONE', 'amount': Decimal('0.00')}

        excess_amount = excess_amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        if estimate.customer_id:
            customer = Customer.objects.select_for_update().get(id=estimate.customer_id)
            prev_bal = customer.current_credit_balance
            new_bal = prev_bal - excess_amount
            customer.current_credit_balance = new_bal
            customer.save(update_fields=['current_credit_balance', 'updated_at'])

            CustomerUdhaariLedger.objects.create(
                customer=customer,
                branch=estimate.branch,
                entry_type='CREDIT',
                amount=excess_amount,
                previous_balance=prev_bal,
                resulting_balance=new_bal,
                reference_invoice=estimate.estimate_number,
                payment_mode='OTHER',
                remarks=f"Surplus trade-in credit from buy-back voucher {trade_in_voucher.voucher_number} on bill {estimate.estimate_number}",
                recorded_by=user
            )

            AuditLog.objects.create(
                user=user,
                branch=estimate.branch,
                action_type='UPDATE',
                module='TradeInExcessCredit',
                object_repr=f"Customer Store Credit: {customer.name}",
                details={
                    'voucher': trade_in_voucher.voucher_number,
                    'estimate': estimate.estimate_number,
                    'excess_credited': str(excess_amount),
                    'new_balance': str(new_bal)
                }
            )

            return {'settled': True, 'mode': 'STORE_CREDIT', 'amount': excess_amount, 'customer': customer.name}
        else:
            estimate.change_returned += excess_amount
            estimate.save(update_fields=['change_returned', 'updated_at'])

            AuditLog.objects.create(
                user=user,
                branch=estimate.branch,
                action_type='UPDATE',
                module='TradeInExcessCash',
                object_repr=f"Walk-In Cash Refund: {estimate.estimate_number}",
                details={
                    'voucher': trade_in_voucher.voucher_number,
                    'estimate': estimate.estimate_number,
                    'excess_cash_returned': str(excess_amount)
                }
            )

            return {'settled': True, 'mode': 'CASH_CHANGE', 'amount': excess_amount}