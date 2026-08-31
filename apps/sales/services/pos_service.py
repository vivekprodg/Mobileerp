"""
POS Counter Terminal & Parked Bill (Hold Cart) Recovery Service.
File Path: D:\Mobile Shop\Inventory\apps\sales\services\pos_service.py

Core Capabilities:
1. Itemized Sales Return & Defective Quarantine Routing:
   - Allows partial or itemized returns from multi-item invoices without voiding the bill.
   - Working items are restored to sellable live stock counter.
   - Defective items are automatically routed to Quarantined Defective Stock for Vendor RMA claims.
   - Updates ItemInstance statuses (IN_STOCK vs RETURNED_DEFECTIVE) and deactivates active warranties for returned units.
   - Deducts return amounts from customer cumulative lifetime spend (total_spent).
   - Adjusts customer Udhaari balance and posts CustomerUdhaariLedger entries when store credit is chosen.
   - Updates invoice status to PARTIALLY_RETURNED or RETURNED.
2. Price Recovery for Parked Bills: Full preservation of item rates, discounts, quantities, and scanned IMEIs on F9 (Hold) and F10 (Recall).
3. Dual-SIM Sequential Scanning: Automatic prompt and dual-slot validation for IMEI 1 & IMEI 2.
4. Strict Walk-In Credit (Udhaari) Guard: Blocks credit sales for anonymous walk-in customers and enforces credit limits.
5. Server-Side Catalog Price Integrity (SEC-01): Verifies cart prices against official database rates and mandates Manager PIN overrides on deviations.
6. Multi-Mode Payment & Trade-In Excess: Handles split payments, excess buy-back credit refunds, real-time inventory deductions, and FIFO costing.
"""

import re
import uuid
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from datetime import date, timedelta
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
from apps.core.utils.nepali_date_converter import ad_to_bs_string


class TaxCalculator:
    """
    Dedicated tax computation engine supporting Inclusive, Exclusive, and Exempt tax models.
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
    price validation, customer credit checks, live inventory movements, and itemized sales returns.
    """

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
        bill_discount_percent: Decimal = Decimal('0.00'),
        trade_in_voucher_id: Optional[int] = None,
        manager_override_user=None,
        notes: str = ""
    ) -> SalesEstimate:
        cls._validate_cart_items(cart_items)

        if bill_discount_percent is None:
            bill_discount_percent = Decimal('0.00')
        if not isinstance(bill_discount_percent, Decimal):
            try:
                bill_discount_percent = Decimal(str(bill_discount_percent))
            except (InvalidOperation, ValueError, TypeError):
                raise ValidationError("Invalid bill discount percentage format.")

        bill_discount_percent = bill_discount_percent.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        if bill_discount_percent < Decimal('0.00') or bill_discount_percent > Decimal('100.00'):
            raise ValidationError(
                f"Invalid bill discount percentage ({bill_discount_percent}%). "
                f"Discount percentage must be between 0.00% and 100.00%."
            )

        config = SystemConfiguration.get_solo()
        is_shop_vat_registered = (config.tax_system_mode == 'VAT')
        today_ad = timezone.now().date()
        today_bs = ad_to_bs_string(today_ad, lang='en')

        customer_type = 'RETAIL'
        if customer_id:
            customer_record = Customer.objects.filter(id=customer_id, is_active=True).first()
            if customer_record:
                customer_type = customer_record.customer_type

        trade_in_voucher, trade_in_credit_amt = cls._validate_trade_in_voucher(branch, trade_in_voucher_id)

        processed_lines, subtotal, item_discount_sum = cls._parse_cart_lines(
            cart_items=cart_items,
            is_shop_vat_registered=is_shop_vat_registered,
            customer_type=customer_type,
            manager_override_user=manager_override_user,
            config=config,
            cashier=cashier,
            branch=branch
        )

        net_after_item_discounts = max(Decimal('0.00'), subtotal - item_discount_sum)
        threshold = config.require_manager_approval_discount or Decimal('10.00')

        if bill_discount_percent > threshold:
            is_authorized = bool(
                manager_override_user or
                cashier.is_superuser or
                getattr(cashier, 'role', '') in ['OWNER', 'MANAGER']
            )
            if not is_authorized:
                raise ValidationError(
                    f"Bill discount of {bill_discount_percent:.2f}% exceeds the store manager threshold of {threshold:.2f}%. "
                    f"Manager PIN approval is required."
                )

        bill_discount_amt = (net_after_item_discounts * (bill_discount_percent / Decimal('100.00'))).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP
        )

        estimate_number = cls.generate_estimate_number(branch)

        estimate = SalesEstimate(
            estimate_number=estimate_number,
            branch=branch,
            cashier=cashier,
            salesperson=salesperson or cashier,
            bill_date_ad=today_ad,
            bill_date_bs=today_bs,
            customer_id=customer_id,
            customer_name_manual=customer_name,
            customer_phone_manual=customer_phone,
            customer_pan=customer_pan,
            bill_discount_percent=bill_discount_percent,
            has_trade_in_exchange=bool(trade_in_voucher),
            trade_in_discount_amount=trade_in_credit_amt,
            trade_in_voucher_reference=trade_in_voucher.voucher_number if trade_in_voucher else None,
            manager_override_by=manager_override_user,
            notes=notes,
            status='COMPLETED',
            is_vat_applicable=is_shop_vat_registered
        )

        calc_result = cls._process_lines_and_inventory(
            estimate=estimate,
            branch=branch,
            cashier=cashier,
            processed_lines=processed_lines,
            net_after_item_discounts=net_after_item_discounts,
            bill_discount_amt=bill_discount_amt,
            is_shop_vat_registered=is_shop_vat_registered,
            default_vat_rate=config.default_vat_rate,
            allow_negative=config.allow_negative_stock,
            today_ad=today_ad,
            customer_name=customer_name,
            customer_phone=customer_phone
        )

        excess_trade_in_credit = cls._finalize_estimate_totals(
            estimate=estimate,
            subtotal=subtotal,
            item_discount_sum=item_discount_sum,
            bill_discount_amt=bill_discount_amt,
            trade_in_credit_amt=trade_in_credit_amt,
            calc_result=calc_result
        )

        if trade_in_voucher:
            cls._apply_trade_in_restock(trade_in_voucher, estimate, branch, cashier)

        cls._process_payments_and_udhaari(
            estimate=estimate,
            branch=branch,
            cashier=cashier,
            payments=payments,
            customer_id=customer_id,
            excess_trade_in_credit=excess_trade_in_credit,
            manager_override_user=manager_override_user
        )

        AuditLog.objects.create(
            user=cashier,
            branch=branch,
            action_type='CREATE',
            module='POS_Sales',
            object_repr=estimate.estimate_number,
            details={
                'tax_mode': config.tax_system_mode,
                'grand_total': str(estimate.grand_total),
                'bill_discount_pct': str(bill_discount_percent),
                'trade_in_credit': str(trade_in_credit_amt),
                'excess_trade_in_refunded_or_credited': str(excess_trade_in_credit),
                'paid': str(estimate.paid_amount),
                'due': str(estimate.due_amount),
                'payment_status': estimate.payment_status,
                'salesperson': estimate.salesperson.username,
                'manager_override': manager_override_user.username if manager_override_user else None
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
        branch: Branch = None
    ) -> Tuple[list, Decimal, Decimal]:
        processed = []
        subtotal = Decimal('0.00')
        item_discount_sum = Decimal('0.00')

        threshold = config.require_manager_approval_discount if config else Decimal('10.00')
        is_cashier_privileged = bool(
            manager_override_user or
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

            pkg_conversion_id = item_data.get('unit_conversion_id') or item_data.get('conversion_id')
            factor = Decimal('1.000')
            unit_conv = None
            if pkg_conversion_id:
                unit_conv = UnitConversion.objects.filter(id=pkg_conversion_id, product=product).first()
                if unit_conv:
                    factor = unit_conv.conversion_factor

            base_official_price = ProductCatalogService.get_applicable_price(product, qty * factor, customer_type)
            if unit_conv and unit_conv.selling_price_per_unit:
                official_unit_price = unit_conv.selling_price_per_unit
            else:
                official_unit_price = base_official_price * factor
            official_unit_price = official_unit_price.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

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

            raw_item_disc = item_data.get('discount_percent', 0)
            try:
                discount_pct = Decimal(str(raw_item_disc if raw_item_disc is not None else 0)).quantize(
                    Decimal('0.01'), rounding=ROUND_HALF_UP
                )
            except (InvalidOperation, ValueError, TypeError):
                raise ValidationError(f"Invalid line discount percentage for '{product.name}'.")

            if discount_pct < Decimal('0.00') or discount_pct > Decimal('100.00'):
                raise ValidationError(
                    f"Line item discount percentage for '{product.name}' must be between 0.00% and 100.00%. "
                    f"Received: {discount_pct}%."
                )

            line_official_gross = (qty * official_unit_price).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            line_submitted_gross = (qty * submitted_unit_price).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            line_submitted_disc = (line_submitted_gross * (discount_pct / Decimal('100.00'))).quantize(
                Decimal('0.01'), rounding=ROUND_HALF_UP
            )
            line_submitted_net = max(Decimal('0.00'), line_submitted_gross - line_submitted_disc)

            effective_item_discount_pct = Decimal('0.00')
            if line_official_gross > Decimal('0.00'):
                effective_item_discount_pct = (
                    ((line_official_gross - line_submitted_net) / line_official_gross) * Decimal('100.00')
                ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            prod_max_discount = getattr(product, 'max_discount_percent', Decimal('10.00'))
            allowed_threshold = min(threshold, prod_max_discount)

            has_price_reduction = (submitted_unit_price < official_unit_price)
            has_excessive_discount = (effective_item_discount_pct > allowed_threshold)

            if (has_price_reduction or has_excessive_discount) and not is_cashier_privileged:
                raise ValidationError(
                    f"Unauthorized price reduction or discount on '{product.name}'. "
                    f"Official Database Price: Rs. {official_unit_price:.2f}, Submitted Price: Rs. {submitted_unit_price:.2f} "
                    f"(Effective Discount: {effective_item_discount_pct:.2f}% > Allowed: {allowed_threshold:.2f}%). "
                    f"Manager PIN approval is required."
                )

            if has_price_reduction and is_cashier_privileged:
                AuditLog.objects.create(
                    user=manager_override_user or cashier,
                    branch=branch,
                    action_type='PRICE_OVERRIDE',
                    module='POS_Checkout',
                    object_repr=f"{product.name} (SKU: {product.sku})",
                    details={
                        'official_price': str(official_unit_price),
                        'override_price': str(submitted_unit_price),
                        'quantity': str(qty),
                        'effective_discount_pct': str(effective_item_discount_pct),
                        'authorized_by': (manager_override_user.username if manager_override_user else cashier.username)
                    }
                )

            unit_price = submitted_unit_price
            base_units = (qty * factor).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)
            line_gross = line_submitted_gross
            line_disc = line_submitted_disc
            line_after_item_disc = line_submitted_net

            subtotal += line_gross
            item_discount_sum += line_disc

            imei_num = str(item_data.get('imei_number', '') or item_data.get('imei_1', '')).strip()
            secondary_imei = str(item_data.get('secondary_imei', '') or item_data.get('imei_2', '')).strip()

            tax_mode = 'EXEMPT' if not is_shop_vat_registered else (
                item_data.get('tax_pricing_type') or getattr(product, 'tax_pricing_type', 'INCLUSIVE')
            )

            processed.append({
                'product': product,
                'qty': qty,
                'unit_price': unit_price,
                'official_unit_price': official_unit_price,
                'discount_pct': discount_pct,
                'line_gross': line_gross,
                'line_disc': line_disc,
                'line_after_item_disc': line_after_item_disc,
                'factor': factor,
                'unit_conv': unit_conv,
                'base_units': base_units,
                'imei_num': imei_num,
                'secondary_imei': secondary_imei,
                'tax_mode': tax_mode,
            })

        return processed, subtotal, item_discount_sum

    @classmethod
    def _process_lines_and_inventory(
        cls,
        estimate: SalesEstimate,
        branch: Branch,
        cashier,
        processed_lines: list,
        net_after_item_discounts: Decimal,
        bill_discount_amt: Decimal,
        is_shop_vat_registered: bool,
        default_vat_rate: Decimal,
        allow_negative: bool,
        today_ad: date,
        customer_name: str,
        customer_phone: str
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
            line_disc = line['line_disc']
            line_after_item_disc = line['line_after_item_disc']

            line_bill_disc = Decimal('0.00')
            if net_after_item_discounts > Decimal('0.00') and bill_discount_amt > Decimal('0.00'):
                line_bill_disc = (bill_discount_amt * (line_after_item_disc / net_after_item_discounts)).quantize(
                    Decimal('0.01'), rounding=ROUND_HALF_UP
                )

            net_line_payable = max(Decimal('0.00'), line_after_item_disc - line_bill_disc)

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
                cashier=cashier
            )

            total_cogs += (actual_unit_cost * base_units)

            saved_items.append(SalesEstimateItem(
                estimate=estimate,
                product=product,
                unit_conversion=unit_conv,
                quantity=qty,
                conversion_factor=factor,
                base_unit_quantity=base_units,
                unit_price=unit_price,
                cost_price=actual_unit_cost,
                discount_percent=line['discount_pct'],
                discount_amount=line_disc + line_bill_disc,
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
                warranty_months=product.warranty_months,
                warranty_start_date=today_ad,
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
        cashier
    ) -> Tuple[Decimal, Optional[ItemInstance], Optional[str], Optional[date], str]:
        actual_unit_cost = product.purchase_price
        item_instance = None
        batch_ref = None
        warranty_exp = None
        warranty_summary = f"{product.warranty_months}M General Warranty"

        if product.requires_imei_tracking:
            clean_imei_1 = imei_num.strip() if imei_num else None
            clean_imei_2 = secondary_imei.strip() if secondary_imei else None

            if not clean_imei_1 and not clean_imei_2:
                raise ValidationError(f"Primary IMEI 1 is required for smartphone '{product.name}'.")

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
        calc_result: dict
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
        round_off = Decimal('0.00')

        net_merchandise_revenue = calc_result['taxable_total'] + calc_result['non_taxable_total']
        total_gross_profit = (net_merchandise_revenue - calc_result['total_cogs']).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP
        )

        estimate.subtotal = subtotal
        estimate.item_discount_total = item_discount_sum
        estimate.bill_discount_amount = bill_discount_amt
        estimate.taxable_amount = calc_result['taxable_total']
        estimate.non_taxable_amount = calc_result['non_taxable_total']
        estimate.vat_amount = calc_result['vat_sum']
        estimate.round_off = round_off
        estimate.grand_total = grand_total
        estimate.total_cost_amount = calc_result['total_cogs']
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

    @staticmethod
    def _process_payments_and_udhaari(
        estimate: SalesEstimate,
        branch: Branch,
        cashier,
        payments: list,
        customer_id: Optional[int],
        excess_trade_in_credit: Decimal = Decimal('0.00'),
        manager_override_user=None
    ):
        total_real_paid = Decimal('0.00')
        credit_tendered = Decimal('0.00')

        for pay in payments:
            pay_mode = str(pay.get('mode', '')).upper().strip()
            amt = Decimal(str(pay.get('amount', 0))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            if amt > Decimal('0.00'):
                if pay_mode == 'CREDIT':
                    credit_tendered += amt
                else:
                    total_real_paid += amt

        tentative_due = max(Decimal('0.00'), estimate.grand_total - total_real_paid)

        if (tentative_due > Decimal('0.00') or credit_tendered > Decimal('0.00')) and not customer_id:
            raise ValidationError(
                "Credit sales (Udhaari) require a registered customer profile. "
                "Please select or register a customer before completing this sale."
            )

        if customer_id and tentative_due > Decimal('0.00'):
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
            pay_mode = str(pay.get('mode', '')).upper().strip()
            amt = Decimal(str(pay.get('amount', 0))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            ref = str(pay.get('reference', '') or '').strip()

            if amt > Decimal('0.00'):
                SalesPaymentTransaction.objects.create(
                    estimate=estimate,
                    payment_mode=pay_mode,
                    amount=amt,
                    transaction_ref=ref or None
                )

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

        if excess_trade_in_credit > Decimal('0.00'):
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

        if customer_id:
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

    # =========================================================================
    # ITEMIZE SALES RETURN & DEFECTIVE ITEM QUARANTINE ROUTING
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
        Executes itemized customer return workflow:
        1. Itemized Return Selection: Returns single or multiple items from a bill without voiding whole invoice.
        2. Routes Defective vs Working Items:
           - Working Items: Directly restored to Sellable Branch Stock and ItemInstance marked IN_STOCK.
           - Defective Items: Routed to Quarantined Defective Stock (`quarantined_defective_quantity`)
             for Supplier RMA claims and ItemInstance marked RETURNED_DEFECTIVE.
        3. Deactivates customer warranty cards for returned serialized items.
        4. Financial & Debt Accounting:
           - Deducts refund amount from customer's cumulative lifetime spend (`customer.total_spent`).
           - If `refund_mode == 'STORE_CREDIT'`, reduces customer's Udhaari debt or increases credit deposit
             and records an immutable `CustomerUdhaariLedger` ADJUSTMENT entry.
        5. Updates invoice status to `PARTIALLY_RETURNED` or `RETURNED`.
        """
        if original_estimate.status in ['CANCELLED', 'RETURNED']:
            raise ValidationError("Cannot process returns from an invoice that is already cancelled or fully returned.")

        if not items_to_return:
            raise ValidationError("Please select at least one line item to return.")

        # Re-lock original estimate
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

            # Check previously returned quantities
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

            # Compute refund value proportional to net line item billing
            effective_unit_net_rate = (est_item.line_total / est_item.quantity).quantize(
                Decimal('0.01'), rounding=ROUND_HALF_UP
            ) if est_item.quantity > Decimal('0.000') else est_item.unit_price

            refund_val = (effective_unit_net_rate * return_qty).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            SalesReturnItem.objects.create(
                sales_return=sales_return,
                estimate_item=est_item,
                product=est_item.product,
                return_quantity=return_qty,
                base_unit_quantity=base_return_units,
                refund_amount=refund_val,
                returned_imei=est_item.imei_number,
                restock_to_inventory=not is_defective,
                is_defective=is_defective,
                defect_reason=defect_desc
            )

            # -------------------------------------------------------------
            # INVENTORY ROUTING: SELLABLE WORKING STOCK vs DEFECTIVE QUARANTINE
            # -------------------------------------------------------------
            if not is_defective:
                # Working Condition: Restore to active branch sellable stock
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
                # Defective Condition: Move to Quarantined Defective Stock for Vendor RMA
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

            # -------------------------------------------------------------
            # SERIALIZED ITEM INSTANCE & WARRANTY DEACTIVATION
            # -------------------------------------------------------------
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

                # Void customer component warranties issued on this sold handset
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

        # -----------------------------------------------------------------
        # CUSTOMER ACCOUNTING: LIFETIME SPEND & UDHAARI STORE CREDIT ADJUSTMENT
        # -----------------------------------------------------------------
        customer = None
        if original_estimate.customer_id:
            customer = Customer.objects.select_for_update().filter(id=original_estimate.customer_id).first()

        if customer:
            # 1. Deduct refund from customer cumulative lifetime spend
            customer.total_spent = max(Decimal('0.00'), customer.total_spent - total_refund)

            # 2. If Store Credit: reduce customer Udhaari debt or deposit credit balance
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

        # -----------------------------------------------------------------
        # CHECK FULL VS PARTIAL RETURN COMPLETION STATUS
        # -----------------------------------------------------------------
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

        # -----------------------------------------------------------------
        # AUDIT LOGGING
        # -----------------------------------------------------------------
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