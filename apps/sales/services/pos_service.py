"""
POS Counter Terminal, Sales Estimation & Parked Bill Recovery Service.

Core Capabilities:
1. Historical Migration Safeguard (Zero Shelf-Stock Deduction):
   - When is_historical_import=True, saves the sales invoice and items for audit and tax
     reporting, but completely bypasses physical stock deduction (InventoryService.adjust_stock),
     batch depletion (ProductBatch), handset serial mutation (ItemInstance), and customer warranties.
   - Sets total_cost_amount to 0.00 for historical imports, protecting Account 1310 (Inventory Asset).
2. Strict Live Counter Validation (15-Digit Mandatory IMEI):
   - When is_historical_import=False (standard live POS operations), strictly mandates that any
     smartphone (product.requires_imei_tracking) must have a valid scanned 15-digit IMEI verified
     in IN_STOCK status before allowing checkout.
3. Direct Amount Usage & Zero Rounding Leakage:
   - When Discount Type = AMOUNT (or legacy FIXED), the entered monetary value is
     applied directly as the line deduction without prior conversion to percentage.
4. Absolute Field Separation:
   - Item discount is saved exclusively into item_discount_amount.
   - Proportional bill-level discount is saved exclusively into allocated_bill_discount_amount.
5. Proportional Bill Discount Allocation with Residual Penny Reconciliation:
   - Distributes bill-level discounts only across eligible discountable lines.
   - Reconciles 1-paisa rounding variances to the highest-value line item.
6. Post-Discount Net Tax Base:
   - TaxCalculator computes VAT strictly on the net post-discount payable base.
7. Strict General Ledger Integration & Bank Reconciliation:
   - Normalizes and captures digital transaction reference numbers (FonePay Trace IDs,
     eSewa IDs, Card Approval Codes, Cheque Numbers).
   - Enriches General Ledger double-entry voucher line narrations with exact transaction
     reference codes so accountants can reconcile bank statement deposits effortlessly.
8. Itemized Sales Return & Defective Routing:
   - Refunds strictly the net amount the customer paid per returned unit.
   - Restocks working items to sellable stock and routes defective items to RMA quarantine.
9. Atomic Bill Cancellation:
   - Reverses physical stock, voids warranties, reverses Udhaari, and creates inverse journal vouchers.
"""

import re
import uuid
import inspect
import logging
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from datetime import date, datetime, timedelta
from typing import List, Dict, Any, Optional, Tuple

from django.db import transaction
from django.db.models import Q, F, Sum
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.sales.models import (
    SalesEstimate, SalesEstimateItem, SalesPaymentTransaction,
    SalesReturn, SalesReturnItem, PhoneExchangeTradeIn
)
from apps.inventory.models import (
    Product, UnitConversion, ItemInstance, ProductBatch, DeviceComponentWarranty,
    BranchStock, StockMovementLog
)
from apps.inventory.services import InventoryService
from apps.products.services import ProductCatalogService
from apps.sales.services.trade_in_engine import TradeInValuationEngine
from apps.customers.models import Customer, CustomerUdhaariLedger
from apps.branches.models import Branch, BranchDocumentSequence
from apps.core.models import SystemConfiguration, AuditLog
from apps.core.nepali_calendar import NepaliCalendar
from apps.core.utils.nepali_date_converter import ad_to_bs_string

logger = logging.getLogger(__name__)


class TaxCalculator:
    """
    Dedicated tax computation engine supporting Exclusive, Inclusive, and Exempt tax regimes.
    Calculates tax strictly on the net post-discount merchandise base.
    """

    @staticmethod
    def compute_line_tax(
        net_line_payable: Decimal,
        is_vat_registered: bool,
        is_product_taxable: bool,
        tax_mode: str,
        effective_vat_rate: Decimal
    ) -> Tuple[Decimal, Decimal, Decimal]:
        """
        Computes line tax figures with exact Decimal rounding.
        Returns:
            Tuple[Decimal, Decimal, Decimal]: (base_taxable_amount, line_vat, final_line_total)
        """
        if not is_vat_registered or not is_product_taxable or effective_vat_rate <= Decimal('0.00'):
            return Decimal('0.00'), Decimal('0.00'), net_line_payable

        if tax_mode == 'EXCLUSIVE':
            base_taxable = net_line_payable
            line_vat = (base_taxable * (effective_vat_rate / Decimal('100.00'))).quantize(
                Decimal('0.01'), rounding=ROUND_HALF_UP
            )
            final_line_total = base_taxable + line_vat
            return base_taxable, line_vat, final_line_total

        elif tax_mode == 'INCLUSIVE':
            final_line_total = net_line_payable
            multiplier = Decimal('1.00') + (effective_vat_rate / Decimal('100.00'))
            base_taxable = (final_line_total / multiplier).quantize(
                Decimal('0.01'), rounding=ROUND_HALF_UP
            )
            line_vat = final_line_total - base_taxable
            return base_taxable, line_vat, final_line_total

        return Decimal('0.00'), Decimal('0.00'), net_line_payable


class SalesPOSService:
    """
    Modular POS Engine executing instant billing, sales scoping, dual-IMEI tagging,
    price validation, customer credit checks, live inventory movements, itemized sales returns,
    and automated General Ledger double-entry synchronization.
    """

    DIGITAL_PAYMENT_MODES = {
        'FONEPAY', 'ESEWA', 'KHALTI', 'CARD', 'BANK_TRANSFER', 'CONNECT_IPS', 'CHEQUE'
    }

    @staticmethod
    def generate_estimate_number(branch: Branch) -> str:
        """
        Atomically allocates a unique sequential estimate slip number using row-level locking.
        """
        prefix = branch.invoice_prefix or "EST"
        return BranchDocumentSequence.get_next_sequence_number(
            branch=branch,
            document_type='SALES_ESTIMATE',
            prefix_override=prefix,
            padding=6
        )

    @classmethod
    @transaction.atomic
    def process_checkout(
        cls,
        branch: Branch,
        cashier,
        cart_items: list,
        payments: list,
        salesperson=None,
        customer_id: Optional[int] = None,
        customer_name: str = "",
        customer_phone: str = "",
        customer_pan: str = "",
        bill_discount_type: str = 'PERCENTAGE',
        bill_discount_input_value: Optional[Decimal] = None,
        bill_discount_percent: Optional[Decimal] = None,
        discount_reason: str = "",
        trade_in_voucher_id: Optional[int] = None,
        manager_override_user=None,
        notes: str = "",
        is_historical_import: bool = False,
        bill_date_ad: Optional[date] = None,
        bill_date_bs: Optional[str] = None,
        fiscal_year: Optional[str] = None,
        estimate_number_override: Optional[str] = None,
        **kwargs
    ) -> SalesEstimate:
        """
        Main transactional checkout coordinator.
        
        SAFEGUARDS:
        - When is_historical_import=True:
            * Bypasses physical stock deduction and batch depletion.
            * Bypasses strict counter IMEI mandates (historical rows can use placeholders).
            * Protects Account 1310 (Inventory Asset) by setting total_cost_amount = 0.00.
        - When is_historical_import=False (Live Daily POS Sales):
            * 100% strictly mandates 15-digit IMEIs for real smartphones.
            * Decrements live warehouse shelf inventory and manages serialized warranties.
        """
        cls._validate_cart_items(cart_items)

        config = SystemConfiguration.get_solo()
        is_shop_vat_registered = (config.tax_system_mode == 'VAT')

        # 1. Date & Fiscal Period Resolution
        if bill_date_ad:
            target_date_ad = bill_date_ad
            if isinstance(target_date_ad, datetime):
                target_date_ad = target_date_ad.date()
        else:
            target_date_ad = timezone.now().date()

        if bill_date_bs and fiscal_year:
            target_date_bs = bill_date_bs
            target_fiscal_year = fiscal_year
        else:
            bs_y, bs_m, bs_d = NepaliCalendar.ad_to_bs(target_date_ad)
            target_date_bs = NepaliCalendar.format_bs(bs_y, bs_m, bs_d, lang='en')
            target_fiscal_year = NepaliCalendar.get_fiscal_year(bs_y, bs_m)

        # 2. Resolve Customer Profile & Tier
        customer_type = 'RETAIL'
        if customer_id:
            customer_record = Customer.objects.filter(id=customer_id, is_active=True).first()
            if customer_record:
                customer_type = customer_record.customer_type

        # 3. Validate Trade-In Voucher (if attached)
        trade_in_voucher, trade_in_credit_amt = cls._validate_trade_in_voucher(branch, trade_in_voucher_id)

        # 4. Parse Cart Lines (Dual-Mode Item Discounts & Price Override Audit)
        processed_lines, subtotal, item_discount_sum = cls._parse_cart_lines(
            cart_items=cart_items,
            is_shop_vat_registered=is_shop_vat_registered,
            customer_type=customer_type,
            manager_override_user=manager_override_user,
            config=config,
            cashier=cashier,
            branch=branch,
            is_historical=is_historical_import
        )

        # 5. Dual-Mode Bill-Level Discount Evaluation & Authorization Check
        discountable_net_base = sum(
            line['line_after_item_disc'] for line in processed_lines if line['is_discountable']
        )

        raw_bill_type = str(bill_discount_type or 'PERCENTAGE').upper().strip()
        if raw_bill_type in ['AMOUNT', 'FIXED', 'FLAT', 'CASH', 'NPR', 'RS']:
            raw_bill_type = 'AMOUNT'
        elif raw_bill_type in ['PERCENTAGE', '%', 'PERCENT']:
            raw_bill_type = 'PERCENTAGE'
        elif raw_bill_type == 'NONE':
            raw_bill_type = 'NONE'
        else:
            raw_bill_type = 'PERCENTAGE'

        if bill_discount_input_value is not None and str(bill_discount_input_value).strip() != '':
            try:
                raw_bill_input = Decimal(str(bill_discount_input_value))
            except (InvalidOperation, ValueError, TypeError):
                raise ValidationError("Invalid bill discount input format.")
        elif bill_discount_percent is not None and str(bill_discount_percent).strip() != '':
            try:
                raw_bill_input = Decimal(str(bill_discount_percent))
                raw_bill_type = 'PERCENTAGE'
            except (InvalidOperation, ValueError, TypeError):
                raise ValidationError("Invalid bill discount percentage format.")
        else:
            raw_bill_input = Decimal('0.00')

        raw_bill_input = raw_bill_input.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        if raw_bill_input < Decimal('0.00'):
            raise ValidationError("Bill discount cannot be negative.")

        if raw_bill_input > Decimal('0.00') and discountable_net_base <= Decimal('0.00'):
            raise ValidationError("Bill discount cannot be applied because there are no discountable items in the cart.")

        # Calculate bill discount deduction amount and secondary control percentage
        if raw_bill_type == 'AMOUNT':
            if raw_bill_input > discountable_net_base and not is_historical_import:
                raise ValidationError(
                    f"Bill discount amount of Rs. {raw_bill_input:.2f} cannot exceed "
                    f"the discountable merchandise subtotal of Rs. {discountable_net_base:.2f}."
                )
            bill_discount_amt = raw_bill_input
            effective_bill_discount_pct = (
                ((bill_discount_amt / discountable_net_base) * Decimal('100.00')).quantize(
                    Decimal('0.01'), rounding=ROUND_HALF_UP
                )
                if discountable_net_base > Decimal('0.00')
                else Decimal('0.00')
            )
        elif raw_bill_type == 'PERCENTAGE':
            if raw_bill_input > Decimal('100.00'):
                raise ValidationError(f"Bill discount percentage ({raw_bill_input:.2f}%) cannot exceed 100.00%.")
            effective_bill_discount_pct = raw_bill_input
            bill_discount_amt = (
                discountable_net_base * (effective_bill_discount_pct / Decimal('100.00'))
            ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        else:
            raw_bill_type = 'NONE'
            raw_bill_input = Decimal('0.00')
            effective_bill_discount_pct = Decimal('0.00')
            bill_discount_amt = Decimal('0.00')

        # Check global manager approval threshold (Bypassed on historical migration)
        threshold = config.require_manager_approval_discount or Decimal('10.00')
        is_cashier_privileged = bool(
            manager_override_user or
            cashier.is_superuser or
            getattr(cashier, 'role', '') in ['OWNER', 'MANAGER'] or
            is_historical_import
        )

        if effective_bill_discount_pct > threshold and not is_cashier_privileged:
            raise ValidationError(
                f"Bill discount of {effective_bill_discount_pct:.2f}% (Rs. {bill_discount_amt:.2f}) "
                f"exceeds the supervisor authorization threshold of {threshold:.2f}%. "
                f"Manager PIN approval is required."
            )

        # 6. Proportional Bill Discount Allocation with Residual Penny Reconciliation
        cls._allocate_bill_discount_with_residual_reconciliation(
            processed_lines=processed_lines,
            discountable_net_base=discountable_net_base,
            bill_discount_amt=bill_discount_amt
        )

        # 7. Initialize Sales Estimate Invoice Model
        estimate_number = estimate_number_override or cls.generate_estimate_number(branch)
        discount_approved_at = timezone.now() if (
            manager_override_user or (
                is_cashier_privileged and (
                    bill_discount_amt > Decimal('0.00') or
                    any(l['price_override_amount'] > Decimal('0.00') or l['line_disc'] > Decimal('0.00') for l in processed_lines)
                )
            )
        ) else None

        estimate = SalesEstimate(
            estimate_number=estimate_number,
            branch=branch,
            cashier=cashier,
            salesperson=salesperson or cashier,
            bill_date_ad=target_date_ad,
            bill_date_bs=target_date_bs,
            fiscal_year=target_fiscal_year,
            customer_id=customer_id,
            customer_name_manual=customer_name,
            customer_phone_manual=customer_phone,
            customer_pan=customer_pan,
            bill_discount_type=raw_bill_type,
            bill_discount_input_value=raw_bill_input,
            bill_discount_percent=effective_bill_discount_pct,
            bill_discount_amount=bill_discount_amt,
            discount_reason=discount_reason.strip() if discount_reason else None,
            discount_approved_at=discount_approved_at,
            has_trade_in_exchange=bool(trade_in_voucher),
            trade_in_discount_amount=trade_in_credit_amt,
            trade_in_voucher_reference=trade_in_voucher.voucher_number if trade_in_voucher else None,
            manager_override_by=manager_override_user,
            notes=notes,
            status='COMPLETED',
            is_vat_applicable=is_shop_vat_registered
        )

        # 8. Process Line Items, Taxes, COGS & Inventory (Safeguard Applied)
        calc_result = cls._process_lines_and_inventory(
            estimate=estimate,
            branch=branch,
            cashier=cashier,
            processed_lines=processed_lines,
            is_shop_vat_registered=is_shop_vat_registered,
            default_vat_rate=config.default_vat_rate,
            allow_negative=config.allow_negative_stock,
            today_ad=target_date_ad,
            customer_name=customer_name,
            customer_phone=customer_phone,
            is_historical=is_historical_import
        )

        # 9. Finalize Totals, Net Revenue & Margin Realization
        excess_trade_in_credit = cls._finalize_estimate_totals(
            estimate=estimate,
            subtotal=subtotal,
            item_discount_sum=item_discount_sum,
            bill_discount_amt=bill_discount_amt,
            trade_in_credit_amt=trade_in_credit_amt,
            calc_result=calc_result,
            is_historical=is_historical_import
        )

        # Apply Trade-In Restocking if present (Only for live sales)
        if trade_in_voucher and not is_historical_import:
            cls._apply_trade_in_restock(trade_in_voucher, estimate, branch, cashier)

        # 10. Split Payments & Customer Debt (Udhaari) Settlement
        payment_transactions = cls._process_payments_and_udhaari(
            estimate=estimate,
            branch=branch,
            cashier=cashier,
            payments=payments,
            customer_id=customer_id,
            excess_trade_in_credit=excess_trade_in_credit,
            manager_override_user=manager_override_user,
            is_historical=is_historical_import,
            **kwargs
        )

        # 11. Automatic General Ledger Double-Entry Posting
        cls._post_gl_sales_estimate(
            estimate=estimate,
            cashier=cashier,
            payment_transactions=payment_transactions
        )

        # 12. Forensic Audit Log
        AuditLog.objects.create(
            user=cashier,
            branch=branch,
            action_type='CREATE',
            module='POS_Sales',
            object_repr=estimate.estimate_number,
            details={
                'tax_mode': config.tax_system_mode,
                'subtotal': str(estimate.subtotal),
                'item_discount_total': str(estimate.item_discount_total),
                'bill_discount_type': estimate.bill_discount_type,
                'bill_discount_input': str(estimate.bill_discount_input_value),
                'bill_discount_amount': str(estimate.bill_discount_amount),
                'bill_discount_pct': str(estimate.bill_discount_percent),
                'discount_reason': estimate.discount_reason or "",
                'trade_in_credit': str(trade_in_credit_amt),
                'excess_trade_in_credit': str(excess_trade_in_credit),
                'grand_total': str(estimate.grand_total),
                'total_cogs': str(estimate.total_cost_amount),
                'total_gross_profit': str(estimate.total_gross_profit),
                'paid': str(estimate.paid_amount),
                'due': str(estimate.due_amount),
                'payment_status': estimate.payment_status,
                'is_historical_import': is_historical_import,
                'payments': [
                    {
                        'mode': p.payment_mode,
                        'amount': str(p.amount),
                        'transaction_ref': p.transaction_ref or ""
                    }
                    for p in payment_transactions
                ],
                'salesperson': estimate.salesperson.username if estimate.salesperson else cashier.username,
                'manager_override': manager_override_user.username if manager_override_user else None,
                'discount_approved_at': estimate.discount_approved_at.isoformat() if estimate.discount_approved_at else None
            }
        )

        return estimate

    @staticmethod
    def _validate_cart_items(cart_items: list):
        if not cart_items:
            raise ValidationError("Cart is empty. Cannot process sale.")

    @staticmethod
    def _validate_trade_in_voucher(branch: Branch, voucher_id: Optional[int]) -> Tuple[Optional[PhoneExchangeTradeIn], Decimal]:
        if not voucher_id:
            return None, Decimal('0.00')

        voucher = PhoneExchangeTradeIn.objects.select_for_update().filter(
            id=voucher_id, branch=branch, status__in=['DRAFT', 'VALUATED']
        ).first()

        if not voucher:
            raise ValidationError("Specified Trade-In Buy-Back voucher is invalid, already attached, or expired.")

        return voucher, voucher.final_trade_in_value

    @classmethod
    def _parse_cart_lines(
        cls,
        cart_items: list,
        is_shop_vat_registered: bool,
        customer_type: str = 'RETAIL',
        manager_override_user=None,
        config: SystemConfiguration = None,
        cashier=None,
        branch: Branch = None,
        is_historical: bool = False
    ) -> Tuple[list, Decimal, Decimal]:
        """
        Parses all cart lines.
        Supports direct AMOUNT and PERCENTAGE line-level discounts without rounding leakage.
        """
        processed = []
        subtotal = Decimal('0.00')
        item_discount_sum = Decimal('0.00')

        threshold = config.require_manager_approval_discount if config else Decimal('10.00')
        is_cashier_privileged = bool(
            manager_override_user or
            is_historical or
            (cashier and (cashier.is_superuser or getattr(cashier, 'role', '') in ['OWNER', 'MANAGER']))
        )

        for item_data in cart_items:
            product_id = item_data.get('product_id')
            if not product_id:
                raise ValidationError("Invalid cart item: missing product identifier.")

            product = Product.objects.select_for_update().get(id=product_id)

            raw_qty = item_data.get('quantity', 1)
            try:
                qty = Decimal(str(raw_qty if raw_qty is not None else 1)).quantize(
                    Decimal('0.001'), rounding=ROUND_HALF_UP
                )
            except (InvalidOperation, ValueError, TypeError):
                qty = Decimal('1.000')

            if qty <= Decimal('0.000'):
                raise ValidationError(f"Quantity for item '{product.name}' must be greater than zero.")

            # Packaging Unit Conversion
            pkg_conversion_id = item_data.get('unit_conversion_id') or item_data.get('conversion_id')
            factor = Decimal('1.000')
            unit_conv = None
            if pkg_conversion_id:
                unit_conv = UnitConversion.objects.filter(id=pkg_conversion_id, product=product).first()
                if unit_conv:
                    factor = unit_conv.conversion_factor

            # Official Catalog Price
            base_official_price = ProductCatalogService.get_applicable_price(product, qty * factor, customer_type)
            if unit_conv and unit_conv.selling_price_per_unit:
                official_unit_price = unit_conv.selling_price_per_unit
            else:
                official_unit_price = base_official_price * factor
            official_unit_price = official_unit_price.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            # Submitted Selling Price
            raw_price = item_data.get('unit_price')
            if raw_price is None or str(raw_price).strip() == '':
                raw_price = item_data.get('price')
            if raw_price is None or str(raw_price).strip() == '':
                raw_price = item_data.get('selling_price')

            if raw_price is not None and str(raw_price).strip() != '':
                try:
                    submitted_unit_price = Decimal(str(raw_price)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                except (InvalidOperation, ValueError, TypeError):
                    submitted_unit_price = official_unit_price
            else:
                submitted_unit_price = official_unit_price

            if submitted_unit_price < Decimal('0.00'):
                raise ValidationError(f"Unit price for '{product.name}' cannot be negative.")

            # Gross Line Totals
            line_official_gross = (qty * official_unit_price).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            line_submitted_gross = (qty * submitted_unit_price).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            # Dual-Mode Item Discount Parsing
            raw_disc_type = str(item_data.get('discount_type', '')).upper().strip()
            if raw_disc_type in ['AMOUNT', 'FIXED', 'FLAT', 'CASH', 'NPR', 'RS']:
                raw_disc_type = 'AMOUNT'
            elif raw_disc_type in ['PERCENTAGE', '%', 'PERCENT']:
                raw_disc_type = 'PERCENTAGE'
            elif raw_disc_type == 'NONE':
                raw_disc_type = 'NONE'
            else:
                raw_disc_type = 'PERCENTAGE'

            raw_disc_val = item_data.get('discount_input_value')
            if raw_disc_val is None or str(raw_disc_val).strip() == '':
                raw_disc_val = item_data.get('discount_value')
            if raw_disc_val is None or str(raw_disc_val).strip() == '':
                raw_disc_val = item_data.get('discount_percent', Decimal('0.00'))

            try:
                disc_input_val = Decimal(str(raw_disc_val if raw_disc_val is not None else 0)).quantize(
                    Decimal('0.01'), rounding=ROUND_HALF_UP
                )
            except (InvalidOperation, ValueError, TypeError):
                raise ValidationError(f"Invalid line discount input for '{product.name}'.")

            if disc_input_val < Decimal('0.00'):
                raise ValidationError(f"Discount for '{product.name}' cannot be negative.")

            is_item_discountable = getattr(product, 'is_discountable', True)
            if not is_item_discountable and disc_input_val > Decimal('0.00') and not is_historical:
                raise ValidationError(
                    f"Product '{product.name}' is marked as non-discountable. Line-level discounts cannot be applied."
                )

            if not is_item_discountable or raw_disc_type == 'NONE' or disc_input_val == Decimal('0.00'):
                raw_disc_type = 'NONE'
                disc_input_val = Decimal('0.00')
                line_submitted_disc = Decimal('0.00')
                effective_item_disc_pct = Decimal('0.00')
            elif raw_disc_type == 'AMOUNT':
                if disc_input_val > line_submitted_gross and not is_historical:
                    raise ValidationError(
                        f"Discount amount of Rs. {disc_input_val:.2f} on '{product.name}' cannot exceed "
                        f"the line gross total of Rs. {line_submitted_gross:.2f}."
                    )
                line_submitted_disc = disc_input_val
                effective_item_disc_pct = (
                    ((line_submitted_disc / line_submitted_gross) * Decimal('100.00')).quantize(
                        Decimal('0.01'), rounding=ROUND_HALF_UP
                    )
                    if line_submitted_gross > Decimal('0.00')
                    else Decimal('0.00')
                )
            elif raw_disc_type == 'PERCENTAGE':
                if disc_input_val > Decimal('100.00'):
                    raise ValidationError(
                        f"Discount percentage for '{product.name}' cannot exceed 100.00%. "
                        f"Received: {disc_input_val:.2f}%."
                    )
                effective_item_disc_pct = disc_input_val
                line_submitted_disc = (
                    line_submitted_gross * (effective_item_disc_pct / Decimal('100.00'))
                ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            line_submitted_net = max(Decimal('0.00'), line_submitted_gross - line_submitted_disc)

            price_override_amount = Decimal('0.00')
            if official_unit_price > submitted_unit_price:
                price_override_amount = ((official_unit_price - submitted_unit_price) * qty).quantize(
                    Decimal('0.01'), rounding=ROUND_HALF_UP
                )

            total_price_concession = max(Decimal('0.00'), line_official_gross - line_submitted_net)
            effective_commercial_pct = Decimal('0.00')
            if line_official_gross > Decimal('0.00'):
                effective_commercial_pct = (
                    (total_price_concession / line_official_gross) * Decimal('100.00')
                ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            prod_max_discount = getattr(product, 'max_discount_percent', Decimal('10.00'))
            allowed_threshold = min(threshold, prod_max_discount)

            has_price_reduction = (submitted_unit_price < official_unit_price)
            has_excessive_item_discount = (effective_item_disc_pct > prod_max_discount)
            has_excessive_concession = (effective_commercial_pct > allowed_threshold)

            if (has_price_reduction or has_excessive_item_discount or has_excessive_concession) and not is_cashier_privileged:
                raise ValidationError(
                    f"Commercial discount ceiling exceeded on '{product.name}'. "
                    f"Official Catalog Price: Rs. {official_unit_price:.2f}, Submitted Unit Rate: Rs. {submitted_unit_price:.2f} "
                    f"(Item Discount: {effective_item_disc_pct:.2f}% > Product Max: {prod_max_discount:.2f}%, "
                    f"Total Concession: Rs. {total_price_concession:.2f} / {effective_commercial_pct:.2f}% > Allowed: {allowed_threshold:.2f}%). "
                    f"Manager PIN authorization is required."
                )

            if has_price_reduction and is_cashier_privileged and not is_historical:
                AuditLog.objects.create(
                    user=manager_override_user or cashier,
                    branch=branch,
                    action_type='PRICE_OVERRIDE',
                    module='POS_Checkout',
                    object_repr=f"{product.name} (SKU: {product.sku})",
                    details={
                        'official_unit_price': str(official_unit_price),
                        'override_unit_price': str(submitted_unit_price),
                        'quantity': str(qty),
                        'concession_amount': str(price_override_amount),
                        'effective_commercial_pct': str(effective_commercial_pct),
                        'authorized_by': (manager_override_user.username if manager_override_user else cashier.username)
                    }
                )

            base_units = (qty * factor).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)
            subtotal += line_submitted_gross
            item_discount_sum += line_submitted_disc

            imei_num = str(item_data.get('imei_number', '') or item_data.get('imei_1', '')).strip()
            secondary_imei = str(item_data.get('secondary_imei', '') or item_data.get('imei_2', '')).strip()

            tax_mode = 'EXEMPT' if not is_shop_vat_registered else (
                item_data.get('tax_pricing_type') or getattr(product, 'tax_pricing_type', 'INCLUSIVE')
            )

            processed.append({
                'product': product,
                'qty': qty,
                'unit_price': submitted_unit_price,
                'official_unit_price': official_unit_price,
                'price_override_amount': price_override_amount,
                'discount_type': raw_disc_type,
                'discount_input_value': disc_input_val,
                'discount_percent': effective_item_disc_pct,
                'line_gross': line_submitted_gross,
                'line_disc': line_submitted_disc,
                'line_after_item_disc': line_submitted_net,
                'is_discountable': is_item_discountable,
                'factor': factor,
                'unit_conv': unit_conv,
                'base_units': base_units,
                'imei_num': imei_num,
                'secondary_imei': secondary_imei,
                'tax_mode': tax_mode,
                'allocated_bill_discount': Decimal('0.00')
            })

        return processed, subtotal, item_discount_sum

    @classmethod
    def _allocate_bill_discount_with_residual_reconciliation(
        cls,
        processed_lines: list,
        discountable_net_base: Decimal,
        bill_discount_amt: Decimal
    ) -> None:
        if discountable_net_base <= Decimal('0.00') or bill_discount_amt <= Decimal('0.00'):
            for line in processed_lines:
                line['allocated_bill_discount'] = Decimal('0.00')
            return

        allocated_discounts = []
        sum_allocated = Decimal('0.00')
        largest_idx = -1
        largest_net = Decimal('-1.00')

        for idx, line in enumerate(processed_lines):
            line_net = line['line_after_item_disc']
            is_discountable = line['is_discountable']

            if not is_discountable or line_net <= Decimal('0.00'):
                raw_alloc = Decimal('0.00')
            else:
                if line_net > largest_net:
                    largest_net = line_net
                    largest_idx = idx

                raw_alloc = (bill_discount_amt * (line_net / discountable_net_base)).quantize(
                    Decimal('0.01'), rounding=ROUND_HALF_UP
                )

            allocated_discounts.append(raw_alloc)
            sum_allocated += raw_alloc

        residual = bill_discount_amt - sum_allocated
        if residual != Decimal('0.00') and largest_idx >= 0:
            allocated_discounts[largest_idx] += residual

        for idx, line in enumerate(processed_lines):
            line['allocated_bill_discount'] = allocated_discounts[idx]

    @classmethod
    def _process_lines_and_inventory(
        cls,
        estimate: SalesEstimate,
        branch: Branch,
        cashier,
        processed_lines: list,
        is_shop_vat_registered: bool,
        default_vat_rate: Decimal,
        allow_negative: bool,
        today_ad: date,
        customer_name: str,
        customer_phone: str,
        is_historical: bool = False
    ) -> Dict[str, Any]:
        saved_items = []
        taxable_total = Decimal('0.00')
        non_taxable_total = Decimal('0.00')
        vat_sum = Decimal('0.00')
        exclusive_vat_to_add = Decimal('0.00')
        total_cogs = Decimal('0.00')

        for line in processed_lines:
            product = line['product']
            qty = line['qty']
            unit_price = line['unit_price']
            factor = line['factor']
            unit_conv = line['unit_conv']
            base_units = line['base_units']
            imei_num = line['imei_num']
            secondary_imei = line['secondary_imei']
            tax_mode = line['tax_mode']
            pure_item_disc = line['line_disc']
            allocated_bill_disc = line['allocated_bill_discount']
            line_after_item_disc = line['line_after_item_disc']

            net_line_payable = max(Decimal('0.00'), line_after_item_disc - allocated_bill_disc)

            effective_vat_rate = (
                product.vat_rate if (product.is_vat_applicable and product.vat_rate > Decimal('0.00')) else default_vat_rate
            ) if is_shop_vat_registered else Decimal('0.00')

            base_taxable, line_vat, final_line_total = TaxCalculator.compute_line_tax(
                net_line_payable=net_line_payable,
                is_vat_registered=is_shop_vat_registered,
                is_product_taxable=product.is_vat_applicable,
                tax_mode=tax_mode,
                effective_vat_rate=effective_vat_rate
            )

            if is_shop_vat_registered and effective_vat_rate > Decimal('0.00'):
                taxable_total += base_taxable
                vat_sum += line_vat
                if tax_mode == 'EXCLUSIVE':
                    exclusive_vat_to_add += line_vat
            else:
                non_taxable_total += net_line_payable

            # Allocate Stock & Cost: Safeguard intercepts historical import to protect shelf inventory
            actual_unit_cost, item_instance, batch_ref, warranty_exp, warranty_summary = cls._allocate_stock_and_cost(
                product=product,
                branch=branch,
                base_units=base_units,
                imei_num=imei_num,
                secondary_imei=secondary_imei,
                allow_negative=allow_negative,
                today_ad=today_ad,
                estimate=estimate,
                customer_name=customer_name,
                customer_phone=customer_phone,
                unit_price=unit_price,
                cashier=cashier,
                is_historical=is_historical
            )

            total_cogs += (actual_unit_cost * base_units)
            total_line_discount_amount = pure_item_disc + allocated_bill_disc

            saved_items.append(SalesEstimateItem(
                estimate=estimate,
                product=product,
                unit_conversion=unit_conv,
                quantity=qty,
                conversion_factor=factor,
                base_unit_quantity=base_units,
                unit_price=unit_price,
                official_unit_price=line['official_unit_price'],
                price_override_amount=line['price_override_amount'],
                cost_price=actual_unit_cost,
                discount_type=line['discount_type'],
                discount_input_value=line['discount_input_value'],
                item_discount_amount=pure_item_disc,
                effective_discount_percent=line['discount_percent'],
                allocated_bill_discount_amount=allocated_bill_disc,
                discount_percent=line['discount_percent'],
                discount_amount=total_line_discount_amount,
                tax_pricing_type=tax_mode,
                is_vat_applicable=is_shop_vat_registered and product.is_vat_applicable,
                vat_rate=effective_vat_rate,
                base_taxable_amount=base_taxable,
                taxable_line_amount=base_taxable if is_shop_vat_registered else Decimal('0.00'),
                tax_amount=line_vat,
                line_total=final_line_total,
                item_instance=item_instance,
                batch_reference=batch_ref,
                imei_number=imei_num or None,
                secondary_imei=secondary_imei or None,
                serial_number=item_instance.serial_number if item_instance else None,
                device_condition=item_instance.get_condition_display() if item_instance else "Brand New",
                warranty_months=product.warranty_months if not is_historical else 0,
                warranty_start_date=today_ad if not is_historical else None,
                warranty_expiry_date=warranty_exp,
                warranty_terms=warranty_summary
            ))

        return {
            'saved_items': saved_items,
            'taxable_total': taxable_total,
            'non_taxable_total': non_taxable_total,
            'vat_sum': vat_sum,
            'exclusive_vat_to_add': exclusive_vat_to_add,
            'total_cogs': total_cogs
        }

    @classmethod
    def _allocate_stock_and_cost(
        cls,
        product: Product,
        branch: Branch,
        base_units: Decimal,
        imei_num: str,
        secondary_imei: str,
        allow_negative: bool,
        today_ad: date,
        estimate: SalesEstimate,
        customer_name: str,
        customer_phone: str,
        unit_price: Decimal,
        cashier,
        is_historical: bool = False
    ) -> Tuple[Decimal, Optional[ItemInstance], Optional[str], Optional[date], str]:
        """
        Allocates stock and resolves landed costs.

        SAFEGUARD PROTOCOL:
        - If is_historical=True:
            * Completely skips physical shelf stock deductions (InventoryService.adjust_stock).
            * Leaves non-serialized ProductBatch balances untouched.
            * Skips ItemInstance lookups and updates.
            * Returns actual_unit_cost = 0.00 to guarantee that General Ledger Account 1310
              (Merchandise Inventory Asset) is not credited with false deductions.
        - If is_historical=False (Live POS Counter Operations):
            * Mandates strict 15-digit IMEI check on all smartphones.
            * Locks and marks ItemInstance as SOLD.
            * Deducts warehouse inventory atomically via InventoryService.adjust_stock.
        """
        # =====================================================================
        # 1. HISTORICAL MIGRATION BYPASS (SHELF INVENTORY FULLY PROTECTED)
        # =====================================================================
        if is_historical:
            return Decimal('0.00'), None, None, None, "Historical Migration - Stock & Warranty Unaltered"

        # =====================================================================
        # 2. LIVE DAILY COUNTER BILLING (STRICT IMEI & REAL-TIME STOCK DEDUCTION)
        # =====================================================================
        actual_unit_cost = product.purchase_price
        item_instance = None
        batch_ref = None
        warranty_exp = None
        warranty_summary = f"{product.warranty_months}M General Warranty"

        if product.requires_imei_tracking:
            clean_imei_1 = imei_num.strip() if imei_num else None
            clean_imei_2 = secondary_imei.strip() if secondary_imei else None

            # Enforce 15-digit IMEI on live counter
            if not clean_imei_1 and not clean_imei_2:
                raise ValidationError(f"Primary 15-Digit IMEI is strictly required for smartphone '{product.name}'.")

            if clean_imei_1:
                item_instance = ItemInstance.objects.select_for_update().filter(
                    Q(imei_1=clean_imei_1) | Q(imei_2=clean_imei_1),
                    branch=branch,
                    status='IN_STOCK'
                ).first()

            if not item_instance and clean_imei_2:
                item_instance = ItemInstance.objects.select_for_update().filter(
                    Q(imei_1=clean_imei_2) | Q(imei_2=clean_imei_2),
                    branch=branch,
                    status='IN_STOCK'
                ).first()

            if not item_instance:
                if allow_negative:
                    item_instance = ItemInstance.objects.create(
                        product=product,
                        branch=branch,
                        imei_1=clean_imei_1,
                        imei_2=clean_imei_2,
                        imei_2_pending_scan=False,
                        status='IN_STOCK',
                        landed_cost=product.purchase_price,
                        purchase_date=today_ad,
                        device_barcode=clean_imei_1 or clean_imei_2 or product.barcode
                    )
                else:
                    target_identifier = clean_imei_1 or clean_imei_2
                    raise ValidationError(
                        f"Handset with IMEI '{target_identifier}' was not found in available stock at {branch.name}."
                    )

            if clean_imei_2 and not item_instance.imei_2:
                item_instance.imei_2 = clean_imei_2

            item_instance.imei_2_pending_scan = False
            actual_unit_cost = item_instance.landed_cost
            batch_ref = item_instance.batch_reference

            item_instance.status = 'SOLD'
            item_instance.sold_invoice_reference = estimate.estimate_number
            item_instance.customer_name = customer_name or (estimate.customer.name if estimate.customer else "Walk-in Customer")
            item_instance.customer_phone = customer_phone or (estimate.customer.phone_number if estimate.customer else "")
            item_instance.sale_date = today_ad
            item_instance.sold_price = unit_price
            item_instance.warranty_start_date = today_ad
            if product.warranty_months > 0:
                item_instance.warranty_end_date = today_ad + timedelta(days=product.warranty_months * 30)
            item_instance.save()

            warranty_exp = item_instance.warranty_end_date

            created_warranties = InventoryService.initialize_device_component_warranties(
                item_instance=item_instance,
                sale_date=today_ad,
                custom_warranty_months=product.warranty_months
            )
            w_terms = [f"{cw.component_name}: {cw.warranty_months}M" for cw in created_warranties]
            if w_terms:
                warranty_summary = " | ".join(w_terms)

        else:
            # Non-serialized accessories FIFO batch depletion
            available_batches = ProductBatch.objects.select_for_update().filter(
                product=product, branch=branch, is_depleted=False
            ).order_by('purchase_date', 'created_at')

            remaining_qty = base_units
            weighted_cost_sum = Decimal('0.00')

            for batch in available_batches:
                if batch.quantity_remaining >= remaining_qty:
                    batch.quantity_remaining -= remaining_qty
                    weighted_cost_sum += (remaining_qty * batch.cost_price)
                    batch.save()
                    batch_ref = batch.batch_number
                    remaining_qty = Decimal('0.000')
                    break
                else:
                    weighted_cost_sum += (batch.quantity_remaining * batch.cost_price)
                    remaining_qty -= batch.quantity_remaining
                    batch.quantity_remaining = Decimal('0.000')
                    batch.save()

            if base_units > Decimal('0.000') and remaining_qty < base_units:
                actual_unit_cost = (weighted_cost_sum / (base_units - remaining_qty)).quantize(
                    Decimal('0.01'), rounding=ROUND_HALF_UP
                )
            else:
                actual_unit_cost = product.purchase_price

            if product.warranty_months > 0:
                warranty_exp = today_ad + timedelta(days=product.warranty_months * 30)

        # Deduct physical warehouse stock
        imei_log_str = f"{imei_num} / {secondary_imei}".strip(' /') if (imei_num or secondary_imei) else ""
        InventoryService.adjust_stock(
            product=product,
            branch=branch,
            quantity_delta=-base_units,
            movement_type='SALE',
            reference_doc=estimate.estimate_number,
            imei_or_serial=imei_log_str,
            remarks=f"POS Sale to {estimate.recipient_display_name} by {estimate.salesperson.username}",
            user=cashier,
            allow_negative=allow_negative
        )

        return actual_unit_cost, item_instance, batch_ref, warranty_exp, warranty_summary

    @staticmethod
    def _finalize_estimate_totals(
        estimate: SalesEstimate,
        subtotal: Decimal,
        item_discount_sum: Decimal,
        bill_discount_amt: Decimal,
        trade_in_credit_amt: Decimal,
        calc_result: dict,
        is_historical: bool = False
    ) -> Decimal:
        gross_payable = (subtotal - item_discount_sum - bill_discount_amt) + calc_result['exclusive_vat_to_add']

        if trade_in_credit_amt > gross_payable:
            excess_trade_in_credit = (trade_in_credit_amt - gross_payable).quantize(
                Decimal('0.01'), rounding=ROUND_HALF_UP
            )
            net_payable_after_trade_in = Decimal('0.00')
        else:
            excess_trade_in_credit = Decimal('0.00')
            net_payable_after_trade_in = (gross_payable - trade_in_credit_amt).quantize(
                Decimal('0.01'), rounding=ROUND_HALF_UP
            )

        grand_total = net_payable_after_trade_in
        net_merchandise_revenue = calc_result['taxable_total'] + calc_result['non_taxable_total']
        
        # Zero COGS on historical import protects General Ledger Account 1310
        total_cogs = Decimal('0.00') if is_historical else calc_result['total_cogs']
        total_gross_profit = (net_merchandise_revenue - total_cogs).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP
        )

        estimate.subtotal = subtotal
        estimate.item_discount_total = item_discount_sum
        estimate.taxable_amount = calc_result['taxable_total']
        estimate.non_taxable_amount = calc_result['non_taxable_total']
        estimate.vat_amount = calc_result['vat_sum']
        estimate.round_off = Decimal('0.00')
        estimate.grand_total = grand_total
        estimate.total_cost_amount = total_cogs
        estimate.total_gross_profit = total_gross_profit
        estimate.save()

        for line_item in calc_result['saved_items']:
            line_item.save()

        return excess_trade_in_credit

    @staticmethod
    def _apply_trade_in_restock(trade_in_voucher: PhoneExchangeTradeIn, estimate: SalesEstimate, branch: Branch, cashier):
        trade_in_voucher.pos_estimate = estimate
        trade_in_voucher.status = 'ATTACHED_TO_BILL'
        trade_in_voucher.save(update_fields=['pos_estimate', 'status', 'updated_at'])
        TradeInValuationEngine.restock_traded_in_phone(
            trade_in_voucher=trade_in_voucher,
            branch=branch,
            user=cashier
        )

    @classmethod
    def _process_payments_and_udhaari(
        cls,
        estimate: SalesEstimate,
        branch: Branch,
        cashier,
        payments: list,
        customer_id: Optional[int],
        excess_trade_in_credit: Decimal = Decimal('0.00'),
        manager_override_user=None,
        is_historical: bool = False,
        **kwargs
    ) -> List[SalesPaymentTransaction]:
        """
        Parses split payments, normalizes transaction references, updates customer Udhaari
        ledgers, and records SalesPaymentTransaction records.
        """
        total_real_paid = Decimal('0.00')
        credit_tendered = Decimal('0.00')
        created_transactions: List[SalesPaymentTransaction] = []

        for pay in payments:
            pay_mode = str(pay.get('mode') or pay.get('payment_mode') or '').upper().strip()
            raw_amt = pay.get('amount', 0)
            try:
                amt = Decimal(str(raw_amt if raw_amt is not None else 0)).quantize(
                    Decimal('0.01'), rounding=ROUND_HALF_UP
                )
            except (InvalidOperation, ValueError, TypeError):
                amt = Decimal('0.00')

            if amt > Decimal('0.00'):
                if pay_mode == 'CREDIT':
                    credit_tendered += amt
                else:
                    total_real_paid += amt

        tentative_due = max(Decimal('0.00'), estimate.grand_total - total_real_paid)

        # Restrict anonymous credit sales on live counter (Historical import bypasses)
        if (tentative_due > Decimal('0.00') or credit_tendered > Decimal('0.00')) and not customer_id and not is_historical:
            raise ValidationError(
                "Credit sales (Udhaari) require a registered customer profile. "
                "Please select or register a customer before completing this sale."
            )

        if customer_id and tentative_due > Decimal('0.00') and not is_historical:
            customer = Customer.objects.select_for_update().get(id=customer_id)
            prev_balance = customer.current_credit_balance
            projected_balance = prev_balance + tentative_due

            if customer.credit_limit > Decimal('0.00') and projected_balance > customer.credit_limit:
                is_authorized = bool(
                    manager_override_user or
                    cashier.is_superuser or
                    getattr(cashier, 'role', '') in ['OWNER', 'MANAGER']
                )

                if not is_authorized:
                    raise ValidationError(
                        f"Credit limit exceeded for customer '{customer.name}'. "
                        f"Allowed Limit: Rs. {customer.credit_limit:.2f}, "
                        f"Current Outstanding Debt: Rs. {prev_balance:.2f}, "
                        f"New Debt Requested: Rs. {tentative_due:.2f} "
                        f"(Projected Total: Rs. {projected_balance:.2f}). "
                        f"Manager override PIN approval is required to exceed the allowed credit limit."
                    )

                AuditLog.objects.create(
                    user=manager_override_user or cashier,
                    branch=branch,
                    action_type='PRICE_OVERRIDE',
                    module='CustomerCreditLimitOverride',
                    object_repr=f"Credit Override: {customer.name}",
                    details={
                        'customer': customer.name,
                        'customer_phone': customer.phone_number,
                        'credit_limit': str(customer.credit_limit),
                        'previous_balance': str(prev_balance),
                        'new_balance': str(projected_balance),
                        'exceeded_by': str(projected_balance - customer.credit_limit),
                        'authorized_by': (manager_override_user.username if manager_override_user else cashier.username)
                    }
                )

        for pay in payments:
            pay_mode = str(pay.get('mode') or pay.get('payment_mode') or '').upper().strip()
            raw_amt = pay.get('amount', 0)
            try:
                amt = Decimal(str(raw_amt if raw_amt is not None else 0)).quantize(
                    Decimal('0.01'), rounding=ROUND_HALF_UP
                )
            except (InvalidOperation, ValueError, TypeError):
                amt = Decimal('0.00')

            raw_ref = (
                pay.get('transaction_ref') or
                pay.get('trace_id') or
                pay.get('approval_code') or
                pay.get('txn_id') or
                pay.get('reference') or
                pay.get('reference_number') or
                pay.get('auth_code') or
                pay.get('cheque_number') or
                pay.get('cheque_no') or
                pay.get('ref') or
                ''
            )
            clean_ref = str(raw_ref).strip() if raw_ref else ''

            if amt > Decimal('0.00'):
                tx = SalesPaymentTransaction.objects.create(
                    estimate=estimate,
                    payment_mode=pay_mode,
                    amount=amt,
                    transaction_ref=clean_ref or None
                )
                created_transactions.append(tx)

        estimate.paid_amount = total_real_paid
        estimate.due_amount = tentative_due

        if estimate.due_amount == Decimal('0.00'):
            estimate.payment_status = 'PAID'
            if total_real_paid > estimate.grand_total:
                estimate.change_returned = total_real_paid - estimate.grand_total
            else:
                estimate.change_returned = Decimal('0.00')
        elif total_real_paid > Decimal('0.00'):
            estimate.payment_status = 'PARTIAL'
            estimate.change_returned = Decimal('0.00')
        else:
            estimate.payment_status = 'DUE'
            estimate.change_returned = Decimal('0.00')

        if excess_trade_in_credit > Decimal('0.00') and not is_historical:
            if customer_id:
                customer = Customer.objects.select_for_update().get(id=customer_id)
                prev_bal = customer.current_credit_balance
                new_bal = prev_bal - excess_trade_in_credit
                customer.current_credit_balance = new_bal
                customer.save(update_fields=['current_credit_balance', 'updated_at'])

                CustomerUdhaariLedger.objects.create(
                    customer=customer,
                    branch=branch,
                    entry_type='CREDIT',
                    amount=excess_trade_in_credit,
                    previous_balance=prev_bal,
                    resulting_balance=new_bal,
                    reference_invoice=estimate.estimate_number,
                    payment_mode='OTHER',
                    remarks=f"Trade-in buyback excess credit from voucher {estimate.trade_in_voucher_reference} on estimate {estimate.estimate_number}",
                    recorded_by=cashier
                )
            else:
                estimate.change_returned += excess_trade_in_credit

        estimate.save(update_fields=['paid_amount', 'due_amount', 'change_returned', 'payment_status', 'updated_at'])

        if customer_id and not is_historical:
            customer = Customer.objects.select_for_update().get(id=customer_id)
            if estimate.due_amount > Decimal('0.00'):
                prev_bal = customer.current_credit_balance
                new_bal = prev_bal + estimate.due_amount
                customer.current_credit_balance = new_bal
                customer.total_spent += estimate.grand_total
                customer.save(update_fields=['current_credit_balance', 'total_spent', 'updated_at'])

                CustomerUdhaariLedger.objects.create(
                    customer=customer,
                    branch=branch,
                    entry_type='DEBIT',
                    amount=estimate.due_amount,
                    previous_balance=prev_bal,
                    resulting_balance=new_bal,
                    reference_invoice=estimate.estimate_number,
                    payment_mode='CREDIT',
                    remarks=f"POS credit purchase on estimate {estimate.estimate_number}",
                    recorded_by=cashier
                )
            else:
                customer.total_spent += estimate.grand_total
                customer.save(update_fields=['total_spent', 'updated_at'])

        return created_transactions

    @classmethod
    def _post_gl_sales_estimate(
        cls,
        estimate: SalesEstimate,
        cashier,
        payment_transactions: Optional[List[SalesPaymentTransaction]] = None
    ) -> None:
        """
        Executes double-entry General Ledger auto-posting strictly within the atomic transaction.
        Enriches journal line items with cashier-provided payment reference codes (e.g. FonePay Trace ID,
        eSewa Txn ID, Card Approval Code) for bank reconciliation.
        """
        from apps.accounting.services.auto_posting import AutoPostingService

        if payment_transactions is None:
            payment_transactions = list(
                SalesPaymentTransaction.objects.filter(estimate=estimate).order_by('id')
            )

        payment_details = []
        for ptx in payment_transactions:
            ref_str = ptx.transaction_ref or ""
            payment_details.append({
                'id': ptx.id,
                'mode': ptx.payment_mode,
                'amount': ptx.amount,
                'transaction_ref': ref_str,
                'reference': ref_str,
                'trace_id': ref_str,
                'narration': (
                    f"{ptx.payment_mode} Payment (Ref: {ref_str})"
                    if ref_str else f"{ptx.payment_mode} Payment"
                )
            })

        try:
            sig = inspect.signature(AutoPostingService.post_sales_estimate)
            call_kwargs = {'estimate': estimate, 'user': cashier}

            if 'payment_details' in sig.parameters:
                call_kwargs['payment_details'] = payment_details
            if 'payment_transactions' in sig.parameters:
                call_kwargs['payment_transactions'] = payment_transactions
            if 'payments' in sig.parameters:
                call_kwargs['payments'] = payment_details
            if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
                call_kwargs['payment_details'] = payment_details
                call_kwargs['payment_transactions'] = payment_transactions

            result_voucher = AutoPostingService.post_sales_estimate(**call_kwargs)

            cls._enrich_voucher_payment_narrations(
                estimate=estimate,
                payment_transactions=payment_transactions,
                result_voucher=result_voucher
            )

        except ValidationError:
            raise
        except Exception as err:
            logger.error(
                f"[POS GL Auto-Posting Error] Estimate {estimate.estimate_number} failed to post to GL: {err}",
                exc_info=True
            )
            raise ValidationError(
                f"Checkout could not be completed because General Ledger posting failed: {err}. "
                f"The transaction has been rolled back to prevent accounting ledger discrepancy."
            )

    @classmethod
    def _enrich_voucher_payment_narrations(
        cls,
        estimate: SalesEstimate,
        payment_transactions: List[SalesPaymentTransaction],
        result_voucher=None
    ) -> None:
        """
        Guarantees that every payment line item in the sales journal voucher displays
        the cashier-provided reference code directly on the General Ledger statement.
        """
        from apps.accounting.models import JournalEntry

        voucher = result_voucher if isinstance(result_voucher, JournalEntry) else None
        if not voucher:
            voucher = JournalEntry.objects.filter(
                voucher_type='SALES',
                reference_document=estimate.estimate_number
            ).order_by('-created_at').first()

        if not voucher:
            return

        unmatched_refs = [
            tx for tx in payment_transactions
            if tx.transaction_ref and tx.amount > Decimal('0.00')
        ]

        if not unmatched_refs:
            return

        items = list(voucher.items.filter(debit_amount__gt=Decimal('0.00')).select_related('account'))

        for tx in unmatched_refs:
            tx_ref = tx.transaction_ref
            mode_upper = tx.payment_mode.upper()
            matched_item = None

            for itm in items:
                acct_name = (itm.account.name or '').upper() if itm.account else ''
                line_narr = (itm.narration or '').upper()
                if itm.debit_amount == tx.amount and (mode_upper in acct_name or mode_upper in line_narr):
                    matched_item = itm
                    break

            if not matched_item:
                for itm in items:
                    if itm.debit_amount == tx.amount:
                        matched_item = itm
                        break

            if not matched_item:
                for itm in items:
                    acct_name = (itm.account.name or '').upper() if itm.account else ''
                    if mode_upper in acct_name:
                        matched_item = itm
                        break

            if matched_item:
                if tx_ref not in (matched_item.narration or ""):
                    ref_tag = f"[{tx.payment_mode} Ref: {tx_ref}]"
                    if matched_item.narration:
                        matched_item.narration = f"{matched_item.narration} {ref_tag}"
                    else:
                        matched_item.narration = f"Receipt via {tx.payment_mode} {ref_tag} for {estimate.estimate_number}"
                    matched_item.save(update_fields=['narration'])
                items.remove(matched_item)

    # =========================================================================
    # ITEMIZED SALES RETURN & DEFECTIVE ITEM QUARANTINE ROUTING
    # =========================================================================
    @classmethod
    @transaction.atomic
    def process_sales_return(
        cls,
        original_estimate: SalesEstimate,
        items_to_return: list,
        refund_mode: str,
        reason: str,
        technician_notes: str,
        user
    ) -> SalesReturn:
        """
        Executes itemized customer return workflow, inventory restock/quarantine,
        customer udhaari adjustments, and double-entry General Ledger reversal vouchers.
        """
        if original_estimate.status in ['CANCELLED', 'RETURNED']:
            raise ValidationError("Cannot process returns from an invoice that is already cancelled or fully returned.")

        if not items_to_return:
            raise ValidationError("Please select at least one line item to return.")

        original_estimate = SalesEstimate.objects.select_for_update().get(pk=original_estimate.pk)

        sales_return = SalesReturn.objects.create(
            return_number=f"RET-{uuid.uuid4().hex[:8].upper()}",
            original_estimate=original_estimate,
            branch=original_estimate.branch,
            customer=original_estimate.customer,
            refund_mode=refund_mode,
            reason=reason,
            technician_notes=technician_notes,
            processed_by=user,
            total_refund_amount=Decimal('0.00')
        )

        total_refund = Decimal('0.00')

        for item_data in items_to_return:
            item_id = item_data.get('item_id')
            est_item = SalesEstimateItem.objects.select_for_update().select_related(
                'product', 'product__base_unit', 'item_instance'
            ).get(id=item_id, estimate=original_estimate)

            try:
                return_qty = Decimal(str(item_data['quantity'])).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)
            except (InvalidOperation, ValueError, TypeError):
                raise ValidationError(f"Invalid return quantity specified for {est_item.product.name}.")

            is_defective = bool(item_data.get('is_defective', False))
            defect_desc = str(item_data.get('defect_reason', '') or '').strip()

            already_returned_qty = SalesReturnItem.objects.filter(
                sales_return__original_estimate=original_estimate,
                estimate_item=est_item
            ).aggregate(sum_qty=Sum('return_quantity'))['sum_qty'] or Decimal('0.000')

            remaining_returnable_qty = max(Decimal('0.000'), est_item.quantity - already_returned_qty)

            if return_qty <= Decimal('0.000') or return_qty > remaining_returnable_qty:
                raise ValidationError(
                    f"Invalid return quantity ({return_qty}) for '{est_item.product.name}'. "
                    f"Remaining returnable quantity is {remaining_returnable_qty}."
                )

            factor = est_item.conversion_factor if est_item.conversion_factor > Decimal('0.000') else Decimal('1.000')
            base_return_units = (return_qty * factor).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)

            effective_unit_net_rate = (est_item.line_total / est_item.quantity).quantize(
                Decimal('0.01'), rounding=ROUND_HALF_UP
            ) if est_item.quantity > Decimal('0.000') else est_item.unit_price

            refund_val = (effective_unit_net_rate * return_qty).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            proportionate_item_discount = (
                (est_item.item_discount_amount / est_item.quantity) * return_qty
            ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) if est_item.quantity > Decimal('0.000') else Decimal('0.00')

            SalesReturnItem.objects.create(
                sales_return=sales_return,
                estimate_item=est_item,
                product=est_item.product,
                return_quantity=return_qty,
                base_unit_quantity=base_return_units,
                refund_amount=refund_val,
                discount_type=est_item.discount_type,
                discount_input_value=est_item.discount_input_value,
                item_discount_amount=proportionate_item_discount,
                effective_discount_percent=est_item.effective_discount_percent,
                returned_imei=est_item.imei_number,
                restock_to_inventory=not is_defective,
                is_defective=is_defective,
                defect_reason=defect_desc
            )

            if not is_defective:
                InventoryService.adjust_stock(
                    product=est_item.product,
                    branch=original_estimate.branch,
                    quantity_delta=base_return_units,
                    movement_type='SALE_RETURN',
                    reference_doc=sales_return.return_number,
                    imei_or_serial=est_item.imei_number or "",
                    remarks=f"Customer Return (Working Item Restock) from Bill {original_estimate.estimate_number}",
                    user=user,
                    allow_negative=True
                )
            else:
                branch_stock, _ = BranchStock.objects.select_for_update().get_or_create(
                    branch=original_estimate.branch,
                    product=est_item.product,
                    defaults={
                        'quantity': Decimal('0.000'),
                        'reserved_quantity': Decimal('0.000'),
                        'quarantined_defective_quantity': Decimal('0.000'),
                        'low_stock_threshold': est_item.product.reorder_level or Decimal('5.00')
                    }
                )

                prev_quarantine = branch_stock.quarantined_defective_quantity
                branch_stock.quarantined_defective_quantity += base_return_units
                branch_stock.save(update_fields=['quarantined_defective_quantity', 'updated_at'])

                StockMovementLog.objects.create(
                    product=est_item.product,
                    branch=original_estimate.branch,
                    movement_type='SERVICE_DEFECTIVE_QUARANTINE',
                    quantity_delta=base_return_units,
                    previous_quantity=prev_quarantine,
                    new_quantity=branch_stock.quarantined_defective_quantity,
                    reference_document=sales_return.return_number,
                    imei_or_serial_number=est_item.imei_number or "",
                    remarks=f"Customer Defective Return from Bill {original_estimate.estimate_number}: {defect_desc or 'Hardware Defect'}",
                    user=user
                )

            if est_item.item_instance:
                target_status = 'IN_STOCK' if not is_defective else 'RETURNED_DEFECTIVE'
                est_item.item_instance.status = target_status
                est_item.item_instance.sold_invoice_reference = None
                est_item.item_instance.customer_name = None
                est_item.item_instance.customer_phone = None
                est_item.item_instance.sale_date = None
                est_item.item_instance.sold_price = None
                est_item.item_instance.save(update_fields=[
                    'status', 'sold_invoice_reference', 'customer_name',
                    'customer_phone', 'sale_date', 'sold_price', 'updated_at'
                ])

                DeviceComponentWarranty.objects.filter(
                    item_instance=est_item.item_instance,
                    status='ACTIVE'
                ).update(
                    status='VOID',
                    void_reason=f"Device returned under return voucher {sales_return.return_number} (Defective: {is_defective})",
                    updated_at=timezone.now()
                )

            total_refund += refund_val

        sales_return.total_refund_amount = total_refund
        sales_return.save(update_fields=['total_refund_amount', 'updated_at'])

        # Customer Account Udhaari Reversal
        customer = None
        if original_estimate.customer_id:
            customer = Customer.objects.select_for_update().filter(id=original_estimate.customer_id).first()

        if customer:
            customer.total_spent = max(Decimal('0.00'), customer.total_spent - total_refund)

            if refund_mode == 'STORE_CREDIT':
                prev_bal = customer.current_credit_balance
                new_bal = prev_bal - total_refund
                customer.current_credit_balance = new_bal

                CustomerUdhaariLedger.objects.create(
                    customer=customer,
                    branch=original_estimate.branch,
                    entry_type='ADJUSTMENT',
                    amount=total_refund,
                    previous_balance=prev_bal,
                    resulting_balance=new_bal,
                    reference_invoice=sales_return.return_number,
                    payment_mode='OTHER',
                    remarks=f"Store credit for sales return {sales_return.return_number} from Bill {original_estimate.estimate_number}",
                    recorded_by=user
                )

            customer.save(update_fields=['current_credit_balance', 'total_spent', 'updated_at'])

        # Check Full vs Partial Return
        all_items = original_estimate.items.all()
        is_fully_returned = True

        for itm in all_items:
            tot_ret = SalesReturnItem.objects.filter(
                sales_return__original_estimate=original_estimate,
                estimate_item=itm
            ).aggregate(s=Sum('return_quantity'))['s'] or Decimal('0.000')

            if tot_ret < itm.quantity:
                is_fully_returned = False
                break

        original_estimate.status = 'RETURNED' if is_fully_returned else 'PARTIALLY_RETURNED'
        original_estimate.save(update_fields=['status', 'updated_at'])

        try:
            from apps.accounting.services.auto_posting import AutoPostingService
            AutoPostingService.post_sales_return(sales_return=sales_return, user=user)
        except Exception as err:
            logger.error(f"[SalesReturn GL Auto-Posting Error] Return {sales_return.return_number}: {err}")
            raise ValidationError(f"Sales return processed but General Ledger posting failed: {err}")

        AuditLog.objects.create(
            user=user,
            branch=original_estimate.branch,
            action_type='UPDATE',
            module='SalesReturn',
            object_repr=sales_return.return_number,
            details={
                'original_estimate': original_estimate.estimate_number,
                'refund_amount': str(total_refund),
                'refund_mode': refund_mode,
                'is_fully_returned': is_fully_returned,
                'items_count': len(items_to_return)
            }
        )

        return sales_return

    # =========================================================================
    # BILL CANCELLATION & REVERSING DOUBLE-ENTRY JOURNAL
    # =========================================================================
    @classmethod
    @transaction.atomic
    def cancel_sales_estimate(
        cls,
        estimate: SalesEstimate,
        reason: str,
        user
    ) -> SalesEstimate:
        """
        Atomically cancels a SalesEstimate:
        1. Restores all physical inventory items and marks IMEI ItemInstances as IN_STOCK.
        2. Voids serialized customer warranties.
        3. Reverses customer Udhaari balance if debt was added on this bill.
        4. Updates bill status to 'CANCELLED'.
        5. Voids the original JournalEntry and creates an inverse balancing journal voucher.
        """
        if estimate.status in ['CANCELLED', 'RETURNED']:
            raise ValidationError(f"Bill {estimate.estimate_number} is already {estimate.get_status_display()}.")

        estimate = SalesEstimate.objects.select_for_update().get(pk=estimate.pk)

        # 1. Restore Physical Stock & Handset IMEIs
        for line in estimate.items.select_related('product', 'item_instance'):
            InventoryService.adjust_stock(
                product=line.product,
                branch=estimate.branch,
                quantity_delta=line.base_unit_quantity,
                movement_type='SALE_RETURN',
                reference_doc=f"VOID-{estimate.estimate_number}",
                imei_or_serial=line.imei_number or "",
                remarks=f"Bill Voided / Cancelled: {estimate.estimate_number}. Reason: {reason}",
                user=user,
                allow_negative=True
            )

            if line.item_instance:
                line.item_instance.status = 'IN_STOCK'
                line.item_instance.sold_invoice_reference = None
                line.item_instance.customer_name = None
                line.item_instance.customer_phone = None
                line.item_instance.sale_date = None
                line.item_instance.sold_price = None
                line.item_instance.save(update_fields=[
                    'status', 'sold_invoice_reference', 'customer_name',
                    'customer_phone', 'sale_date', 'sold_price', 'updated_at'
                ])

                DeviceComponentWarranty.objects.filter(
                    item_instance=line.item_instance,
                    status='ACTIVE'
                ).update(
                    status='VOID',
                    void_reason=f"Bill Cancelled ({estimate.estimate_number}): {reason}",
                    updated_at=timezone.now()
                )

        # 2. Reverse Customer Udhaari Debt
        if estimate.customer and estimate.due_amount > Decimal('0.00'):
            cust = Customer.objects.select_for_update().get(pk=estimate.customer.pk)
            prev_bal = cust.current_credit_balance
            new_bal = max(Decimal('0.00'), prev_bal - estimate.due_amount)
            cust.current_credit_balance = new_bal
            cust.total_spent = max(Decimal('0.00'), cust.total_spent - estimate.grand_total)
            cust.save(update_fields=['current_credit_balance', 'total_spent', 'updated_at'])

            CustomerUdhaariLedger.objects.create(
                customer=cust,
                branch=estimate.branch,
                entry_type='ADJUSTMENT',
                amount=estimate.due_amount,
                previous_balance=prev_bal,
                resulting_balance=new_bal,
                reference_invoice=f"VOID-{estimate.estimate_number}",
                payment_mode='OTHER',
                remarks=f"Reversal of debt from cancelled bill {estimate.estimate_number}",
                recorded_by=user
            )

        # 3. Mark Estimate Status as CANCELLED
        estimate.status = 'CANCELLED'
        estimate.cancellation_reason = reason
        estimate.save(update_fields=['status', 'cancellation_reason', 'updated_at'])

        # 4. Void Original Journal Voucher & Post Reversing Journal Voucher
        try:
            from apps.accounting.models import JournalEntry
            from apps.accounting.services.auto_posting import JournalEngine

            orig_entry = JournalEntry.objects.filter(
                voucher_type='SALES',
                reference_document=estimate.estimate_number,
                status='POSTED'
            ).first()

            if orig_entry:
                orig_entry.status = 'CANCELLED'
                orig_entry.save(update_fields=['status', 'updated_at'])

                reversing_lines = []
                for item in orig_entry.items.select_related('account'):
                    reversing_lines.append({
                        'account': item.account,
                        'debit': item.credit_amount,
                        'credit': item.debit_amount,
                        'customer': item.customer,
                        'supplier': item.supplier,
                        'narration': f"Cancellation reversal of {orig_entry.voucher_number} for {estimate.estimate_number}"
                    })

                if reversing_lines:
                    JournalEngine.create_balanced_entry(
                        voucher_type='JOURNAL',
                        date_ad=timezone.now().date(),
                        branch=estimate.branch,
                        lines=reversing_lines,
                        narration=f"Full Reversal of Cancelled Sales Bill {estimate.estimate_number}. Reason: {reason}",
                        reference_doc=f"REV-{estimate.estimate_number}",
                        user=user,
                        auto_post=True
                    )
        except Exception as err:
            logger.error(f"[POS Bill Cancellation GL Error] Estimate {estimate.estimate_number}: {err}")
            raise ValidationError(f"Bill cancelled but General Ledger reversal failed: {err}")

        # 5. Audit Trail
        AuditLog.objects.create(
            user=user,
            branch=estimate.branch,
            action_type='BILL_CANCEL',
            module='POS_Sales',
            object_repr=estimate.estimate_number,
            details={
                'estimate_number': estimate.estimate_number,
                'grand_total': str(estimate.grand_total),
                'due_amount_reversed': str(estimate.due_amount),
                'reason': reason
            }
        )

        return estimate