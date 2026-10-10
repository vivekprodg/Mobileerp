"""
POS Counter Terminal, Sales Estimation & Parked Bill Recovery Service.

Core Capabilities & Architectural Safeguards:
1. One Clear VAT Calculation & Snapshot Chain (Front-End to Back-End Alignment):
   POS Input
       ↓
   Calculate VAT (Net Line Payable × Rate / Pricing Mode)
       ↓
   Save Exact VAT Snapshot on SalesEstimateItem
       (vat_rate, tax_pricing_type, is_vat_applicable, base_taxable_amount, tax_amount, line_total)
       ↓
   Add All Line VAT
       (Sum of line base_taxable_amount, Sum of line tax_amount, Sum of non-taxable lines)
       ↓
   Save Exact VAT Snapshot on SalesEstimate
       (taxable_amount, non_taxable_amount, vat_amount, grand_total, is_vat_applicable)
       ↓
   VAT Reports (Annex 5 Sales Book, Annex 7, Day-Wise VAT Ledger, TaxPeriodSummary)

2. Absolute Historical Tax Immutability:
   - All tax attributes (rate, pricing type, taxable base, VAT amount) are captured and
     frozen onto `SalesEstimateItem` and `SalesEstimate` at the exact moment of sale.
   - Any subsequent alteration to `Product.vat_rate`, `Product.is_vat_applicable`, or
     `Product.tax_pricing_type` in the product master NEVER mutates historical invoices.
   - Example: A January invoice sold at 13% VAT remains permanently locked at 13% VAT,
     retaining identical taxable base and tax amount throughout all accounting and audit reports.

3. Corrected Sales Return VAT Processing:
   - Derives returned item VAT snapshots directly from the original invoice line item's stored
     historical tax data (base_taxable_amount, tax_amount, line_total, vat_rate, tax_pricing_type).
   - Prevents calculating tax again on VAT-exclusive bills where refund_amount already includes VAT.
   - Accurately allocates returned portion based on return_quantity vs. original quantity.
   - Caps cumulative VAT reversals across partial returns to never exceed the original item's VAT.
   - Sets and saves SalesReturn header totals (taxable_amount, non_taxable_amount, vat_amount,
     total_refund_amount) before General Ledger auto-posting dispatches.

4. Dual Document Sequencing (Official Sales Bill vs Internal Estimate):
   - generate_estimate_number() dynamically checks bill_type:
     * If bill_type == 'SALES': Generates official Sales/Tax Invoices (e.g. INV-NR-000001)
       using document_type='SALES_INVOICE'.
     * If bill_type == 'ESTIMATE': Generates quotation estimation slips (e.g. EST-NR-000001)
       using document_type='SALES_ESTIMATE'.

5. Standard Retail Turnover Accounting (No Revenue Distortion):
   - grand_total strictly represents the full merchandise gross sales value + applicable tax.
   - trade_in_discount_amount is treated exclusively as a tender settlement offset (barter payment),
     protecting statutory revenue reporting and customer spend analytics from distortion.

6. Robust Two-Way Historical Date & Fiscal Period Synchronization:
   - When bill_date_bs is provided, converts immediately to Gregorian AD date and automatically
     calculates the proper Bikram Sambat fiscal year without requiring manual fiscal_year input.
   - Ensures estimate.bill_date_ad, estimate.bill_date_bs, and estimate.fiscal_year are preserved.
   - Calibrates ItemInstance sale dates, warranty start dates, and component expiration schedules
     to the historical bill date rather than the current server clock.

7. Intelligent Customer Resolution for Credit Purchases:
   - In _process_payments_and_udhaari, when a credit sale is initiated without an explicit customer_id,
     the system automatically attempts resolution via Phone number, 9-digit PAN (via Customer.resolve_or_create_by_pan),
     or business name before raising a validation error.

8. Itemized Audit Description in CustomerUdhaariLedger:
   - Multi-tender payments (Cash, FonePay, eSewa, Cards, Bank, Trade-In) are compiled into an itemized
     audit summary stored directly in CustomerUdhaariLedger.remarks.

9. Prevention of Double-Accounting of Credit:
   - estimate.paid_amount strictly captures genuine monetary tenders.
   - estimate.due_amount strictly equals the unpaid balance / credit tender.
   - Non-monetary credit transactions are isolated so General Ledger auto-posting does not double-debit AR (1210).

10. Dynamic Master Switch Sensitive IMEI Allocation:
   - When enforce_imei_tracking is OFF (Backlog Mode): Mobile phones can be sold without IMEIs,
     deducting directly from shelf stock and depleting FIFO batches like standard accessories.
   - When enforce_imei_tracking is ON (Strict Mode): Enforces strict 15-digit IMEI verification.
   - Transition Safety Guard: Seamlessly registers and sells backlog shelf stock when scanned with
     a live physical IMEI in Strict Mode without throwing "Stock Not Found" crashes.

11. Digital Tender-Aware Trade-In Cash Return Guard (Cash Refund Scam Prevention):
    - In process_sales_return, inspects all genuine monetary tenders (Cash, FonePay, eSewa, Khalti, Card, Bank).
    - Customers who paid via digital channels are not blocked from refunds up to their total monetary spend.

12. Comprehensive Atomic Bill Cancellation with Payroll & Trade-In Safeguards:
    - Leaves original journal entry as POSTED and posts an inverted balancing entry to eliminate double-reversal.
    - Inspects traded-in handsets; if already resold to another customer, locks voucher from reactivation.
    - Blocks voiding of PARTIALLY_RETURNED bills to prevent phantom inventory duplication.
    - Restores sold physical merchandise stock, batches, serials, and voids active device warranties.
"""

import re
import uuid
import inspect
import logging
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from datetime import date, datetime, timedelta
from typing import List, Dict, Any, Optional, Tuple, Set

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
from apps.repairs.models import RepairTicket, TechnicianCommissionLog
from apps.core.models import SystemConfiguration, AuditLog
from apps.core.nepali_calendar import NepaliCalendar
from apps.core.utils.nepali_date_converter import parse_bs_date_components

logger = logging.getLogger(__name__)

# =============================================================================
# TAX CALCULATOR & SNAPSHOT ENGINE
# =============================================================================
class TaxCalculator:
    """
    Dedicated tax computation engine supporting Exclusive, Inclusive, and Exempt tax regimes.
    Calculates tax strictly on the net post-discount merchandise base and snapshots values.
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
            - base_taxable_amount: Pre-tax merchandise base subject to tax
            - line_vat: Exact VAT amount generated by this line
            - final_line_total: Final payable line total
        """
        if not is_vat_registered or not is_product_taxable or effective_vat_rate <= Decimal('0.00'):
            return Decimal('0.00'), Decimal('0.00'), net_line_payable

        clean_tax_mode = str(tax_mode or 'INCLUSIVE').upper().strip()

        if clean_tax_mode == 'EXCLUSIVE':
            # Tax added on top of net price
            base_taxable = net_line_payable
            line_vat = (base_taxable * (effective_vat_rate / Decimal('100.00'))).quantize(
                Decimal('0.01'), rounding=ROUND_HALF_UP
            )
            final_line_total = (base_taxable + line_vat).quantize(
                Decimal('0.01'), rounding=ROUND_HALF_UP
            )
            return base_taxable, line_vat, final_line_total

        elif clean_tax_mode == 'INCLUSIVE':
            # Tax extracted from gross net price using exact statutory divisor (e.g. 1.13)
            final_line_total = net_line_payable
            multiplier = Decimal('1.00') + (effective_vat_rate / Decimal('100.00'))
            base_taxable = (final_line_total / multiplier).quantize(
                Decimal('0.01'), rounding=ROUND_HALF_UP
            )
            # Line VAT is the exact difference: ensures base_taxable + line_vat == final_line_total
            line_vat = (final_line_total - base_taxable).quantize(
                Decimal('0.01'), rounding=ROUND_HALF_UP
            )
            return base_taxable, line_vat, final_line_total

        # Default fallback: Non-taxable / Exempt
        return Decimal('0.00'), Decimal('0.00'), net_line_payable

# =============================================================================
# SALES POS SERVICE
# =============================================================================
class SalesPOSService:
    """
    Modular POS Engine executing instant billing, sales scoping, dual-IMEI tagging,
    price validation, customer credit checks, live inventory movements, itemized sales returns,
    and automated General Ledger double-entry synchronization.
    """

    DIGITAL_PAYMENT_MODES: Set[str] = {
        'FONEPAY', 'ESEWA', 'KHALTI', 'CARD', 'POS', 'POS_CARD',
        'BANK_TRANSFER', 'CONNECT_IPS', 'CHEQUE', 'BANK'
    }

    GENUINE_MONETARY_PAYMENT_MODES: Set[str] = {
        'CASH', 'FONEPAY', 'ESEWA', 'KHALTI', 'CARD', 'POS', 'POS_CARD',
        'BANK_TRANSFER', 'CONNECT_IPS', 'CHEQUE', 'BANK'
    }

    @staticmethod
    def generate_estimate_number(branch: Branch, bill_type: str = 'SALES') -> str:
        """
        Atomically allocates a unique sequential document number using row-level locking.
        - If bill_type == 'SALES': Uses document_type='SALES_INVOICE' with prefix 'INV' (e.g. INV-NR-000001).
        - If bill_type == 'ESTIMATE': Uses document_type='SALES_ESTIMATE' with prefix 'EST' (e.g. EST-NR-000001).
        """
        clean_bill_type = str(bill_type or 'SALES').upper().strip()
        is_estimate = clean_bill_type in ['ESTIMATE', 'EST']

        if is_estimate:
            doc_type = 'SALES_ESTIMATE'
            prefix = f"EST-{branch.code}"
        else:
            doc_type = 'SALES_INVOICE'
            branch_prefix = getattr(branch, 'invoice_prefix', '') or ''
            if branch_prefix and not branch_prefix.upper().startswith('EST'):
                prefix = branch_prefix
            else:
                prefix = f"INV-{branch.code}"

        try:
            return BranchDocumentSequence.get_next_sequence_number(
                branch=branch,
                document_type=doc_type,
                prefix_override=prefix,
                padding=6
            )
        except Exception:
            return f"{prefix}-{uuid.uuid4().hex[:6].upper()}"

    @classmethod
    def generate_document_number(cls, branch: Branch, bill_type: str = 'SALES') -> str:
        """Convenience alias for generate_estimate_number supporting dual sequencing."""
        return cls.generate_estimate_number(branch=branch, bill_type=bill_type)

    # =========================================================================
    # PRIMARY CHECKOUT PIPELINE
    # =========================================================================
    @classmethod
    @transaction.atomic
    def process_checkout(
        cls,
        branch: Branch,
        cashier,
        cart_items: list,
        payments: list,
        bill_type: str = 'SALES',
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
        Main transactional checkout coordinator supporting dual document sequencing (Sales vs Estimate).
        Executes the authoritative VAT snapshot chain:
            POS input -> Calculate VAT -> Save line snapshot -> Add line VATs -> Save estimate snapshot.
        """
        cls._validate_cart_items(cart_items)

        config = SystemConfiguration.get_solo()
        is_shop_vat_registered = (config.tax_system_mode == 'VAT')

        # Normalize Bill Type: 'SALES' or 'ESTIMATE'
        raw_bill_type = kwargs.get('bill_type') or bill_type or 'SALES'
        normalized_bill_type = 'ESTIMATE' if str(raw_bill_type).upper().strip() in ['ESTIMATE', 'EST'] else 'SALES'

        # ---------------------------------------------------------------------
        # 1. DATE & FISCAL PERIOD RESOLUTION (TWO-WAY SYNCHRONIZATION)
        # ---------------------------------------------------------------------
        target_date_ad: Optional[date] = None
        target_date_bs: Optional[str] = None
        target_fiscal_year: Optional[str] = None

        if bill_date_bs and str(bill_date_bs).strip():
            try:
                bs_y, bs_m, bs_d = parse_bs_date_components(str(bill_date_bs).strip())
                target_date_ad = NepaliCalendar.bs_to_ad(bs_y, bs_m, bs_d)
                target_date_bs = f"{bs_y:04d}-{bs_m:02d}-{bs_d:02d}"
                target_fiscal_year = fiscal_year or NepaliCalendar.get_fiscal_year(bs_y, bs_m)
            except Exception as e:
                logger.warning(f"[SalesPOSService] Could not parse bill_date_bs '{bill_date_bs}': {e}. Falling back to bill_date_ad.")
                target_date_ad = None

        if not target_date_ad:
            if bill_date_ad:
                target_date_ad = bill_date_ad.date() if isinstance(bill_date_ad, datetime) else bill_date_ad
            else:
                target_date_ad = timezone.now().date()

            bs_y, bs_m, bs_d = NepaliCalendar.ad_to_bs(target_date_ad)
            target_date_bs = target_date_bs or NepaliCalendar.format_bs(bs_y, bs_m, bs_d, lang='en')
            target_fiscal_year = fiscal_year or NepaliCalendar.get_fiscal_year(bs_y, bs_m)

        # ---------------------------------------------------------------------
        # 2. RESOLVE CUSTOMER PROFILE & TIER (SAFE LOOKUP WITH FALLBACK)
        # ---------------------------------------------------------------------
        customer_type = 'RETAIL'
        resolved_customer_id = customer_id
        if resolved_customer_id:
            customer_record = Customer.objects.filter(id=resolved_customer_id, is_active=True).first()
            if customer_record:
                customer_type = customer_record.customer_type
            elif not is_historical_import:
                raise ValidationError(f"Selected customer (ID {resolved_customer_id}) is inactive or no longer exists.")
        else:
            # Attempt early identification via PAN or Phone
            clean_pan = re.sub(r'\D', '', str(customer_pan or '').strip())
            clean_phone = str(customer_phone or '').strip()
            clean_name = str(customer_name or '').strip()

            if len(clean_pan) == 9:
                cust_obj, _ = Customer.resolve_or_create_by_pan(
                    name=clean_name,
                    pan=clean_pan,
                    phone=clean_phone if (clean_phone and clean_phone != '-') else None,
                    branch=branch
                )
                if cust_obj:
                    resolved_customer_id = cust_obj.id
                    customer_type = cust_obj.customer_type
            elif clean_phone and clean_phone not in ['-', '9800000000', '']:
                cust_by_phone = Customer.objects.filter(phone_number=clean_phone, is_active=True).first()
                if cust_by_phone:
                    resolved_customer_id = cust_by_phone.id
                    customer_type = cust_by_phone.customer_type

        # ---------------------------------------------------------------------
        # 3. VALIDATE TRADE-IN VOUCHER (IF ATTACHED)
        # ---------------------------------------------------------------------
        trade_in_voucher, trade_in_credit_amt = cls._validate_trade_in_voucher(branch, trade_in_voucher_id)

        # ---------------------------------------------------------------------
        # 4. PARSE CART LINES (DUAL-MODE ITEM DISCOUNTS & PRICE OVERRIDE AUDIT)
        # ---------------------------------------------------------------------
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

        # ---------------------------------------------------------------------
        # 5. DUAL-MODE BILL-LEVEL DISCOUNT EVALUATION & AUTHORIZATION CHECK
        # ---------------------------------------------------------------------
        discountable_net_base = sum(
            line['line_after_item_disc'] for line in processed_lines if line['is_discountable']
        )

        raw_bill_disc_type = str(bill_discount_type or 'PERCENTAGE').upper().strip()
        if raw_bill_disc_type in ['AMOUNT', 'FIXED', 'FLAT', 'CASH', 'NPR', 'RS']:
            raw_bill_disc_type = 'AMOUNT'
        elif raw_bill_disc_type in ['PERCENTAGE', '%', 'PERCENT']:
            raw_bill_disc_type = 'PERCENTAGE'
        elif raw_bill_disc_type == 'NONE':
            raw_bill_disc_type = 'NONE'
        else:
            raw_bill_disc_type = 'PERCENTAGE'

        if bill_discount_input_value is not None and str(bill_discount_input_value).strip() != '':
            try:
                raw_bill_input = Decimal(str(bill_discount_input_value))
            except (InvalidOperation, ValueError, TypeError):
                raise ValidationError("Invalid bill discount input format.")
        elif bill_discount_percent is not None and str(bill_discount_percent).strip() != '':
            try:
                raw_bill_input = Decimal(str(bill_discount_percent))
                raw_bill_disc_type = 'PERCENTAGE'
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
        if raw_bill_disc_type == 'AMOUNT':
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
        elif raw_bill_disc_type == 'PERCENTAGE':
            if raw_bill_input > Decimal('100.00'):
                raise ValidationError(f"Bill discount percentage ({raw_bill_input:.2f}%) cannot exceed 100.00%.")
            effective_bill_discount_pct = raw_bill_input
            bill_discount_amt = (
                discountable_net_base * (effective_bill_discount_pct / Decimal('100.00'))
            ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        else:
            raw_bill_disc_type = 'NONE'
            raw_bill_input = Decimal('0.00')
            effective_bill_discount_pct = Decimal('0.00')
            bill_discount_amt = Decimal('0.00')

        # Check global manager approval threshold
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

        # ---------------------------------------------------------------------
        # 6. PROPORTIONAL BILL DISCOUNT ALLOCATION & RESIDUAL RECONCILIATION
        # ---------------------------------------------------------------------
        cls._allocate_bill_discount_with_residual_reconciliation(
            processed_lines=processed_lines,
            discountable_net_base=discountable_net_base,
            bill_discount_amt=bill_discount_amt
        )

        # ---------------------------------------------------------------------
        # 7. INITIALIZE SALES ESTIMATE / INVOICE RECORD
        # ---------------------------------------------------------------------
        estimate_number = estimate_number_override or cls.generate_estimate_number(
            branch=branch,
            bill_type=normalized_bill_type
        )

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
            customer_id=resolved_customer_id,
            customer_name_manual=customer_name,
            customer_phone_manual=customer_phone,
            customer_pan=customer_pan,
            bill_discount_type=raw_bill_disc_type,
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

        if hasattr(estimate, 'bill_type'):
            estimate.bill_type = normalized_bill_type
        else:
            setattr(estimate, 'bill_type', normalized_bill_type)

        # ---------------------------------------------------------------------
        # 8. PROCESS LINE ITEMS, SNAPSHOT EXACT VAT, COGS & INVENTORY
        # ---------------------------------------------------------------------
        calc_result = cls._process_lines_and_inventory(
            estimate=estimate,
            branch=branch,
            cashier=cashier,
            processed_lines=processed_lines,
            is_shop_vat_registered=is_shop_vat_registered,
            default_vat_rate=config.default_vat_rate,
            allow_negative=config.allow_negative_stock,
            bill_date_ad=target_date_ad,
            customer_name=customer_name,
            customer_phone=customer_phone,
            is_historical=is_historical_import
        )

        # ---------------------------------------------------------------------
        # 9. FINALIZE ESTIMATE VAT SNAPSHOT, TOTALS & ROUND-OFF
        # ---------------------------------------------------------------------
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

        # ---------------------------------------------------------------------
        # 10. SPLIT PAYMENTS, TENDER SETTLEMENT & CUSTOMER DEBT (UDHAARI)
        # ---------------------------------------------------------------------
        payment_transactions = cls._process_payments_and_udhaari(
            estimate=estimate,
            branch=branch,
            cashier=cashier,
            payments=payments,
            customer_id=resolved_customer_id,
            customer_name=customer_name,
            customer_phone=customer_phone,
            customer_pan=customer_pan,
            trade_in_voucher=trade_in_voucher,
            excess_trade_in_credit=excess_trade_in_credit,
            manager_override_user=manager_override_user,
            is_historical=is_historical_import,
            **kwargs
        )

        # ---------------------------------------------------------------------
        # 11. GENERAL LEDGER DOUBLE-ENTRY POSTING
        # ---------------------------------------------------------------------
        cls._post_gl_sales_estimate(
            estimate=estimate,
            cashier=cashier,
            payment_transactions=payment_transactions
        )

        # ---------------------------------------------------------------------
        # 12. FORENSIC AUDIT LOG
        # ---------------------------------------------------------------------
        AuditLog.objects.create(
            user=cashier,
            branch=branch,
            action_type='CREATE',
            module='POS_Sales',
            object_repr=estimate.estimate_number,
            details={
                'bill_type': normalized_bill_type,
                'tax_mode': config.tax_system_mode,
                'bill_date_ad': str(estimate.bill_date_ad),
                'bill_date_bs': estimate.bill_date_bs,
                'fiscal_year': estimate.fiscal_year,
                'subtotal': str(estimate.subtotal),
                'item_discount_total': str(estimate.item_discount_total),
                'bill_discount_type': estimate.bill_discount_type,
                'bill_discount_input': str(estimate.bill_discount_input_value),
                'bill_discount_amount': str(estimate.bill_discount_amount),
                'bill_discount_pct': str(estimate.bill_discount_percent),
                'taxable_amount': str(estimate.taxable_amount),
                'non_taxable_amount': str(estimate.non_taxable_amount),
                'vat_amount': str(estimate.vat_amount),
                'discount_reason': estimate.discount_reason or "",
                'trade_in_credit': str(trade_in_credit_amt),
                'excess_trade_in_credit': str(excess_trade_in_credit),
                'round_off': str(estimate.round_off),
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
        Parses cart items, verifies prices, and captures explicit tax attributes per line
        from the POS cart input (or falls back to the current product snapshot).
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

            product = Product.objects.select_for_update().filter(id=product_id).first()
            if not product:
                raise ValidationError(f"Product ID {product_id} was not found in catalog.")

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

            # -------------------------------------------------------------
            # EXACT TAX SNAPSHOT CAPTURE FROM CART / PRODUCT STATE
            # -------------------------------------------------------------
            if not is_shop_vat_registered:
                line_tax_mode = 'EXEMPT'
                line_is_vat_applicable = False
                line_vat_rate = Decimal('0.00')
            else:
                raw_tax_mode = item_data.get('tax_pricing_type') or getattr(product, 'tax_pricing_type', 'INCLUSIVE')
                line_tax_mode = str(raw_tax_mode).upper().strip()
                if line_tax_mode not in ['INCLUSIVE', 'EXCLUSIVE', 'EXEMPT']:
                    line_tax_mode = 'INCLUSIVE'

                if item_data.get('is_vat_applicable') is not None:
                    line_is_vat_applicable = bool(item_data.get('is_vat_applicable'))
                else:
                    line_is_vat_applicable = bool(getattr(product, 'is_vat_applicable', True))

                if line_tax_mode == 'EXEMPT':
                    line_is_vat_applicable = False

                if not line_is_vat_applicable:
                    line_vat_rate = Decimal('0.00')
                else:
                    raw_line_rate = item_data.get('vat_rate')
                    if raw_line_rate is not None and str(raw_line_rate).strip() != '':
                        try:
                            line_vat_rate = Decimal(str(raw_line_rate)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                        except (InvalidOperation, ValueError, TypeError):
                            line_vat_rate = getattr(product, 'vat_rate', Decimal('13.00'))
                    else:
                        line_vat_rate = getattr(product, 'vat_rate', None)
                        if line_vat_rate is None or line_vat_rate <= Decimal('0.00'):
                            line_vat_rate = getattr(config, 'default_vat_rate', Decimal('13.00'))

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
                'tax_mode': line_tax_mode,
                'is_vat_applicable': line_is_vat_applicable,
                'effective_vat_rate': line_vat_rate,
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
        bill_date_ad: date,
        customer_name: str,
        customer_phone: str,
        is_historical: bool = False
    ) -> Dict[str, Any]:
        """
        Calculates exact line-level VAT, builds `SalesEstimateItem` records with permanently
        snapshotted tax values, and performs real-time stock allocation/deduction.
        """
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
            pure_item_disc = line['line_disc']
            allocated_bill_disc = line['allocated_bill_discount']
            line_after_item_disc = line['line_after_item_disc']

            # Post-discount net payable base for this line
            net_line_payable = max(Decimal('0.00'), line_after_item_disc - allocated_bill_disc)

            # Snapshotted Tax Configuration (Isolated from future product edits)
            tax_mode = line['tax_mode']
            is_line_taxable = line['is_vat_applicable']
            effective_vat_rate = line['effective_vat_rate']

            # -----------------------------------------------------------------
            # 1. CALCULATE EXACT LINE-LEVEL VAT
            # -----------------------------------------------------------------
            base_taxable, line_vat, final_line_total = TaxCalculator.compute_line_tax(
                net_line_payable=net_line_payable,
                is_vat_registered=is_shop_vat_registered,
                is_product_taxable=is_line_taxable,
                tax_mode=tax_mode,
                effective_vat_rate=effective_vat_rate
            )

            # -----------------------------------------------------------------
            # 2. ACCUMULATE EXACT TOTALS
            # -----------------------------------------------------------------
            if is_shop_vat_registered and is_line_taxable and effective_vat_rate > Decimal('0.00'):
                taxable_total += base_taxable
                vat_sum += line_vat
                if tax_mode == 'EXCLUSIVE':
                    exclusive_vat_to_add += line_vat
            else:
                non_taxable_total += net_line_payable

            # -----------------------------------------------------------------
            # 3. STOCK ALLOCATION & SERIAL TRACKING
            # -----------------------------------------------------------------
            actual_unit_cost, item_instance, batch_ref, warranty_exp, warranty_summary = cls._allocate_stock_and_cost(
                product=product,
                branch=branch,
                base_units=base_units,
                imei_num=imei_num,
                secondary_imei=secondary_imei,
                allow_negative=allow_negative,
                bill_date_ad=bill_date_ad,
                estimate=estimate,
                customer_name=customer_name,
                customer_phone=customer_phone,
                unit_price=unit_price,
                cashier=cashier,
                is_historical=is_historical
            )

            total_cogs += (actual_unit_cost * base_units)
            total_line_discount_amount = pure_item_disc + allocated_bill_disc

            # -----------------------------------------------------------------
            # 4. SAVE EXACT VAT SNAPSHOT ON SALES ESTIMATE ITEM
            # -----------------------------------------------------------------
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
                # Frozen Tax Snapshot:
                tax_pricing_type=tax_mode,
                is_vat_applicable=is_line_taxable,
                vat_rate=effective_vat_rate,
                base_taxable_amount=base_taxable,
                taxable_line_amount=base_taxable if is_shop_vat_registered else Decimal('0.00'),
                tax_amount=line_vat,
                line_total=final_line_total,
                # Serial & Warranty Info:
                item_instance=item_instance,
                batch_reference=batch_ref,
                imei_number=imei_num or None,
                secondary_imei=secondary_imei or None,
                serial_number=item_instance.serial_number if item_instance else None,
                device_condition=item_instance.get_condition_display() if item_instance else "Brand New",
                warranty_months=product.warranty_months if (not is_historical and product.warranty_months) else 0,
                warranty_start_date=bill_date_ad if (not is_historical and product.warranty_months and product.warranty_months > 0) else None,
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
        bill_date_ad: date,
        estimate: SalesEstimate,
        customer_name: str,
        customer_phone: str,
        unit_price: Decimal,
        cashier,
        is_historical: bool = False
    ) -> Tuple[Decimal, Optional[ItemInstance], Optional[str], Optional[date], str]:
        if is_historical:
            return Decimal('0.00'), None, None, None, "Historical Migration - Stock & Warranty Unaltered"

        actual_unit_cost = product.purchase_price
        item_instance = None
        batch_ref = None
        warranty_exp = None

        is_phone = product.requires_imei_tracking
        has_warranty = bool(product.warranty_months and product.warranty_months > 0)

        if has_warranty:
            warranty_summary = f"{product.warranty_months}M General Warranty"
        else:
            warranty_summary = "No Warranty"

        sys_config = SystemConfiguration.get_solo()
        enforce_imei = getattr(sys_config, 'enforce_imei_tracking', True)

        clean_imei_1 = imei_num.strip() if imei_num else None
        clean_imei_2 = secondary_imei.strip() if secondary_imei else None
        has_imeis = bool(clean_imei_1 or clean_imei_2)

        # ---------------------------------------------------------------------
        # SERIALIZED PHONE ALLOCATION LOGIC
        # ---------------------------------------------------------------------
        if is_phone and (enforce_imei or has_imeis):
            if not has_imeis and enforce_imei:
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

            # Transition Safety Guard: Handle Backlog Stock Sold with Live IMEI
            if not item_instance:
                b_stock = BranchStock.objects.filter(branch=branch, product=product).first()
                avail_stock = b_stock.available_quantity if b_stock else Decimal('0.000')

                if avail_stock >= base_units or allow_negative or not enforce_imei:
                    item_instance = ItemInstance.objects.create(
                        product=product,
                        branch=branch,
                        imei_1=clean_imei_1,
                        imei_2=clean_imei_2,
                        imei_2_pending_scan=False,
                        status='IN_STOCK',
                        landed_cost=product.purchase_price,
                        purchase_date=bill_date_ad,
                        device_barcode=clean_imei_1 or clean_imei_2 or product.barcode,
                        source_type='NEW_PURCHASE_GRN',
                        condition='BRAND_NEW',
                        mdms_status=product.default_mdms_status or 'REGISTERED_OFFICIAL',
                        mdms_remarks="Backlog transition: IMEI captured and registered on live POS sale"
                    )
                else:
                    target_identifier = clean_imei_1 or clean_imei_2
                    raise ValidationError(
                        f"Handset with IMEI '{target_identifier}' was not found in available stock at {branch.name}. "
                        f"(Available shelf count: {avail_stock} {product.base_unit.code})."
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
            item_instance.sale_date = bill_date_ad
            item_instance.sold_price = unit_price
            item_instance.warranty_start_date = bill_date_ad
            if product.warranty_months > 0:
                item_instance.warranty_end_date = bill_date_ad + timedelta(days=product.warranty_months * 30)
            item_instance.save()

            warranty_exp = item_instance.warranty_end_date

            created_warranties = InventoryService.initialize_device_component_warranties(
                item_instance=item_instance,
                sale_date=bill_date_ad,
                custom_warranty_months=product.warranty_months
            )

            comp_map = {}
            for cw in created_warranties:
                c_type = (cw.component_type or '').upper()
                c_name_up = (cw.component_name or '').upper()
                c_months = cw.warranty_months
                if c_type == 'DEVICE' or 'DEVICE' in c_name_up or 'BODY' in c_name_up or 'MOTHERBOARD' in c_name_up:
                    comp_map['Device'] = f"{c_months}M"
                elif c_type == 'BATTERY' or 'BATTERY' in c_name_up:
                    comp_map['Battery'] = f"{c_months}M"
                elif c_type == 'SCREEN' or 'SCREEN' in c_name_up or 'DISPLAY' in c_name_up:
                    comp_map['Screen'] = f"{c_months}M"
                else:
                    comp_map[cw.component_name] = f"{c_months}M"

            if created_warranties:
                parts = []
                dev_val = comp_map.pop('Device', f"{product.warranty_months or 12}M")
                parts.append(f"Device: {dev_val}")

                bat_val = comp_map.pop('Battery', '6M')
                parts.append(f"Battery: {bat_val}")

                scr_val = comp_map.pop('Screen', '3M')
                parts.append(f"Screen: {scr_val}")

                for extra_name, extra_val in comp_map.items():
                    parts.append(f"{extra_name}: {extra_val}")

                warranty_summary = f"{' | '.join(parts)} (Physical Damage Excluded)"
            else:
                body_m = product.warranty_months or 12
                warranty_summary = f"Device: {body_m}M | Battery: 6M | Screen: 3M (Physical Damage Excluded)"

            cls._deplete_fifo_batch_if_exists(product, branch, base_units)

        # ---------------------------------------------------------------------
        # NON-SERIALIZED ACCESSORIES & BACKLOG PHONE ALLOCATION (WITHOUT IMEIS)
        # ---------------------------------------------------------------------
        else:
            actual_unit_cost, batch_ref = cls._deplete_fifo_batches(product, branch, base_units)

            if has_warranty:
                warranty_exp = bill_date_ad + timedelta(days=product.warranty_months * 30)
                if not is_phone:
                    warranty_summary = f"{product.warranty_months}M General Warranty"
                else:
                    body_m = product.warranty_months or 12
                    warranty_summary = f"Device: {body_m}M | Battery: 6M | Screen: 3M (Physical Damage Excluded)"
            else:
                warranty_summary = "No Warranty"

        # Deduct physical warehouse shelf stock
        imei_log_str = f"{imei_num} / {secondary_imei}".strip(' /') if (imei_num or secondary_imei) else ""
        date_str_for_log = estimate.bill_date_bs or str(bill_date_ad)
        InventoryService.adjust_stock(
            product=product,
            branch=branch,
            quantity_delta=-base_units,
            movement_type='SALE',
            reference_doc=estimate.estimate_number,
            imei_or_serial=imei_log_str,
            remarks=f"POS Sale to {estimate.recipient_display_name} on {date_str_for_log} by {estimate.salesperson.username}",
            user=cashier,
            allow_negative=allow_negative
        )

        return actual_unit_cost, item_instance, batch_ref, warranty_exp, warranty_summary

    @staticmethod
    def _deplete_fifo_batches(product: Product, branch: Branch, base_units: Decimal) -> Tuple[Decimal, Optional[str]]:
        available_batches = ProductBatch.objects.select_for_update().filter(
            product=product, branch=branch, is_depleted=False
        ).order_by('purchase_date', 'created_at')

        remaining_qty = base_units
        weighted_cost_sum = Decimal('0.00')
        batch_ref = None

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

        return actual_unit_cost, batch_ref

    @staticmethod
    def _deplete_fifo_batch_if_exists(product: Product, branch: Branch, base_units: Decimal) -> None:
        batches = ProductBatch.objects.select_for_update().filter(
            product=product, branch=branch, is_depleted=False
        ).order_by('purchase_date', 'created_at')

        rem = base_units
        for b in batches:
            if b.quantity_remaining >= rem:
                b.quantity_remaining -= rem
                b.save()
                break
            else:
                rem -= b.quantity_remaining
                b.quantity_remaining = Decimal('0.000')
                b.save()

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
        """
        Finalizes invoice financial snapshot. Sums line VAT and taxable amounts,
        saving the exact snapshot onto the SalesEstimate header.
        """
        gross_merchandise = (subtotal - item_discount_sum - bill_discount_amt) + calc_result['exclusive_vat_to_add']
        gross_merchandise = gross_merchandise.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        # ---------------------------------------------------------------------
        # EXACT AGGREGATION FROM SNAPSHOTTED LINES
        # ---------------------------------------------------------------------
        taxable_total = calc_result['taxable_total'].quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        non_taxable_total = calc_result['non_taxable_total'].quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        vat_sum = calc_result['vat_sum'].quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        merchandise_tax_sum = taxable_total + non_taxable_total + vat_sum
        round_off = (gross_merchandise - merchandise_tax_sum).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        grand_total = gross_merchandise

        if trade_in_credit_amt > grand_total:
            excess_trade_in_credit = (trade_in_credit_amt - grand_total).quantize(
                Decimal('0.01'), rounding=ROUND_HALF_UP
            )
        else:
            excess_trade_in_credit = Decimal('0.00')

        net_merchandise_revenue = taxable_total + non_taxable_total
        total_cogs = Decimal('0.00') if is_historical else calc_result['total_cogs'].quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP
        )
        total_gross_profit = (net_merchandise_revenue - total_cogs).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP
        )

        # Save Header VAT and Financial Snapshots
        estimate.subtotal = subtotal
        estimate.item_discount_total = item_discount_sum
        estimate.taxable_amount = taxable_total
        estimate.non_taxable_amount = non_taxable_total
        estimate.vat_amount = vat_sum
        estimate.round_off = round_off
        estimate.grand_total = grand_total
        estimate.total_cost_amount = total_cogs
        estimate.total_gross_profit = total_gross_profit
        estimate.is_vat_applicable = bool(vat_sum > Decimal('0.00') or taxable_total > Decimal('0.00'))
        estimate.save()

        # Save all line item snapshots
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

    # =========================================================================
    # MULTI-TENDER PAYMENTS, UDHAARI & CUSTOMER AUTO-RESOLUTION
    # =========================================================================
    @classmethod
    def _process_payments_and_udhaari(
        cls,
        estimate: SalesEstimate,
        branch: Branch,
        cashier,
        payments: list,
        customer_id: Optional[int],
        customer_name: str = "",
        customer_phone: str = "",
        customer_pan: str = "",
        trade_in_voucher: Optional[PhoneExchangeTradeIn] = None,
        excess_trade_in_credit: Decimal = Decimal('0.00'),
        manager_override_user=None,
        is_historical: bool = False,
        **kwargs
    ) -> List[SalesPaymentTransaction]:
        """
        Processes payment distributions, resolves customer accounts intelligently,
        builds an itemized multi-tender description in CustomerUdhaariLedger, and
        strictly prevents double-accounting of credit sales.
        """
        effective_trade_in_tender = Decimal('0.00')
        if estimate.has_trade_in_exchange and estimate.trade_in_discount_amount > Decimal('0.00'):
            effective_trade_in_tender = min(estimate.grand_total, estimate.trade_in_discount_amount)

        net_customer_payable = max(Decimal('0.00'), estimate.grand_total - effective_trade_in_tender)

        total_real_paid = Decimal('0.00')
        credit_tendered = Decimal('0.00')
        real_payment_lines = []
        created_transactions: List[SalesPaymentTransaction] = []

        # 1. Parse Payments & Separate Genuine Monetary Tenders from Credit
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
                pay.get('transaction_ref') or pay.get('trace_id') or pay.get('approval_code') or
                pay.get('txn_id') or pay.get('reference') or pay.get('reference_number') or
                pay.get('auth_code') or pay.get('cheque_number') or pay.get('cheque_no') or
                pay.get('ref') or ''
            )
            clean_ref = str(raw_ref).strip() if raw_ref else ''

            if amt > Decimal('0.00'):
                if pay_mode in ['CREDIT', 'UDHAARI']:
                    credit_tendered += amt
                else:
                    total_real_paid += amt
                    real_payment_lines.append({
                        'mode': pay_mode,
                        'amount': amt,
                        'ref': clean_ref
                    })

        tentative_due = max(Decimal('0.00'), net_customer_payable - total_real_paid)
        is_credit_sale = (tentative_due > Decimal('0.00') or credit_tendered > Decimal('0.00'))

        # 2. Intelligent Customer Resolution for Credit Purchases
        if is_credit_sale and not is_historical:
            if not customer_id:
                resolved_cust = None
                clean_phone = str(customer_phone or '').strip()
                clean_pan = re.sub(r'\D', '', str(customer_pan or '').strip())
                clean_name = str(customer_name or '').strip()

                if clean_phone and clean_phone not in ['-', '9800000000', '']:
                    resolved_cust = Customer.objects.filter(phone_number=clean_phone, is_active=True).first()

                if not resolved_cust and len(clean_pan) == 9:
                    resolved_cust, _ = Customer.resolve_or_create_by_pan(
                        name=clean_name,
                        pan=clean_pan,
                        phone=clean_phone if (clean_phone and clean_phone != '-') else None,
                        branch=branch
                    )

                generic_terms = ['walk-in customer', 'walk-in', 'walk in', 'cash customer', 'खुदरा ग्राहक', '', '-']
                if not resolved_cust and clean_name and clean_name.lower() not in generic_terms:
                    resolved_cust = Customer.objects.filter(name__iexact=clean_name, is_active=True).first()

                if resolved_cust:
                    customer_id = resolved_cust.id
                    estimate.customer = resolved_cust
                    estimate.customer_id = resolved_cust.id
                    estimate.customer_name_manual = resolved_cust.name
                    if resolved_cust.phone_number:
                        estimate.customer_phone_manual = resolved_cust.phone_number
                    if resolved_cust.pan_number:
                        estimate.customer_pan = resolved_cust.pan_number
                    estimate.save(update_fields=['customer', 'customer_name_manual', 'customer_phone_manual', 'customer_pan', 'updated_at'])
                else:
                    raise ValidationError(
                        "Credit sales (Customer Udhaari) strictly require selecting or registering a customer profile. "
                        "Please select an existing customer or provide a valid mobile number / PAN."
                    )

        # 3. Credit Limit & Cash-Only Guard
        if customer_id and not is_historical:
            customer = Customer.objects.select_for_update().filter(id=customer_id, is_active=True).first()
            if not customer:
                raise ValidationError(f"Customer record (ID {customer_id}) is inactive or does not exist.")

            effective_credit_debt = max(tentative_due, credit_tendered)
            if effective_credit_debt > Decimal('0.00'):
                cust_limit = customer.credit_limit if customer.credit_limit is not None else Decimal('0.00')
                prev_balance = customer.current_credit_balance if customer.current_credit_balance is not None else Decimal('0.00')
                projected_balance = prev_balance + effective_credit_debt

                is_cash_only = (cust_limit <= Decimal('0.00'))
                is_limit_exceeded = (projected_balance > cust_limit)

                if is_cash_only or is_limit_exceeded:
                    is_authorized = bool(
                        manager_override_user or
                        cashier.is_superuser or
                        getattr(cashier, 'role', '') in ['OWNER', 'MANAGER']
                    )

                    if not is_authorized:
                        if is_cash_only:
                            raise ValidationError(
                                f"Customer '{customer.name}' is designated as Cash-Only (Credit Limit: Rs. 0.00). "
                                f"Credit purchase of Rs. {effective_credit_debt:.2f} requires Manager PIN override authorization."
                            )
                        else:
                            raise ValidationError(
                                f"Credit limit exceeded for customer '{customer.name}'. "
                                f"Allowed Limit: Rs. {cust_limit:.2f}, "
                                f"Current Outstanding Debt: Rs. {prev_balance:.2f}, "
                                f"New Debt Requested: Rs. {effective_credit_debt:.2f} "
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
                            'credit_limit': str(cust_limit),
                            'is_cash_only': is_cash_only,
                            'previous_balance': str(prev_balance),
                            'new_balance': str(projected_balance),
                            'requested_due': str(effective_credit_debt),
                            'authorized_by': (manager_override_user.username if manager_override_user else cashier.username)
                        }
                    )

        # 4. Create Payment Transactions
        for pay in payments:
            pay_mode = str(pay.get('mode') or pay.get('payment_mode') or '').upper().strip()
            raw_amt = pay.get('amount', 0)
            try:
                amt = Decimal(str(raw_amt if raw_amt is not None else 0)).quantize(
                    Decimal('0.01'), rounding=ROUND_HALF_UP
                )
            except (InvalidOperation, ValueError, TypeError):
                amt = Decimal('0.00')

            clean_ref = str(pay.get('transaction_ref') or pay.get('trace_id') or pay.get('ref') or '').strip()

            if amt > Decimal('0.00'):
                tx = SalesPaymentTransaction.objects.create(
                    estimate=estimate,
                    payment_mode=pay_mode,
                    amount=amt,
                    transaction_ref=clean_ref or None,
                    notes='Credit / Udhaari Reference' if pay_mode in ['CREDIT', 'UDHAARI'] else None
                )
                created_transactions.append(tx)

        # 5. Prevent Double-Accounting: Paid Amount Strictly Equals Real Monetary Tenders
        estimate.paid_amount = total_real_paid
        estimate.due_amount = tentative_due

        if estimate.due_amount == Decimal('0.00'):
            estimate.payment_status = 'PAID'
            if total_real_paid > net_customer_payable:
                monetary_change = total_real_paid - net_customer_payable
            else:
                monetary_change = Decimal('0.00')
        elif (total_real_paid + effective_trade_in_tender) > Decimal('0.00'):
            estimate.payment_status = 'PARTIAL'
            monetary_change = Decimal('0.00')
        else:
            estimate.payment_status = 'DUE'
            monetary_change = Decimal('0.00')

        estimate.change_returned = monetary_change
        estimate.save(update_fields=['paid_amount', 'due_amount', 'change_returned', 'payment_status', 'updated_at'])

        # 6. Settle Excess Trade-In Credit if Present
        if excess_trade_in_credit > Decimal('0.00') and not is_historical and trade_in_voucher:
            TradeInValuationEngine.settle_excess_trade_in_credit(
                estimate=estimate,
                trade_in_voucher=trade_in_voucher,
                excess_amount=excess_trade_in_credit,
                user=cashier
            )

        # 7. Post Sub-Ledger Entry with Itemized Audit Description in CustomerUdhaariLedger
        if customer_id and not is_historical:
            customer = Customer.objects.select_for_update().filter(id=customer_id, is_active=True).first()
            if customer:
                if estimate.due_amount > Decimal('0.00'):
                    prev_bal = customer.current_credit_balance if customer.current_credit_balance is not None else Decimal('0.00')
                    new_bal = prev_bal + estimate.due_amount
                    customer.current_credit_balance = new_bal
                    customer.total_spent += estimate.grand_total
                    customer.save(update_fields=['current_credit_balance', 'total_spent', 'updated_at'])

                    # Compile Itemized Multi-Tender Payment Description
                    paid_parts = []
                    for rp in real_payment_lines:
                        ref_info = f" Ref: {rp['ref']}" if rp['ref'] else ""
                        paid_parts.append(f"Rs. {rp['amount']:,.2f} ({rp['mode']}{ref_info})")

                    if estimate.has_trade_in_exchange and effective_trade_in_tender > Decimal('0.00'):
                        trade_ref = f" Ref: {estimate.trade_in_voucher_reference}" if estimate.trade_in_voucher_reference else ""
                        paid_parts.append(f"Rs. {effective_trade_in_tender:,.2f} (Trade-In Buyback{trade_ref})")

                    paid_summary = ", ".join(paid_parts) if paid_parts else "None (Full Credit)"
                    bill_date_label = estimate.bill_date_bs or str(estimate.bill_date_ad)

                    itemized_remarks = (
                        f"Bill Total: Rs. {estimate.grand_total:,.2f} | "
                        f"Paid: {paid_summary} | "
                        f"Balance Due (Udhaari): Rs. {estimate.due_amount:,.2f} "
                        f"on bill {estimate.estimate_number} ({bill_date_label})"
                    )

                    ledger_kwargs = {
                        'customer': customer,
                        'branch': branch,
                        'entry_type': 'DEBIT',
                        'amount': estimate.due_amount,
                        'previous_balance': prev_bal,
                        'resulting_balance': new_bal,
                        'reference_invoice': estimate.estimate_number,
                        'payment_mode': 'OTHER',
                        'remarks': itemized_remarks,
                        'recorded_by': cashier
                    }

                    customer_ledger_fields = {f.name for f in CustomerUdhaariLedger._meta.get_fields()}
                    if 'entry_date' in customer_ledger_fields:
                        ledger_kwargs['entry_date'] = estimate.bill_date_ad
                    if 'entry_date_bs' in customer_ledger_fields:
                        ledger_kwargs['entry_date_bs'] = estimate.bill_date_bs

                    CustomerUdhaariLedger.objects.create(**ledger_kwargs)
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
        Dispatches double-entry voucher to AutoPostingService, ensuring that credit transactions
        are excluded from monetary payment lines to prevent double-debiting Accounts Receivable (1210).
        """
        from apps.accounting.services.auto_posting import AutoPostingService

        if payment_transactions is None:
            payment_transactions = list(
                SalesPaymentTransaction.objects.filter(estimate=estimate).order_by('id')
            )

        payment_details = []
        monetary_transactions = []

        for ptx in payment_transactions:
            if ptx.payment_mode.upper() in ['CREDIT', 'UDHAARI']:
                continue

            monetary_transactions.append(ptx)
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

        if estimate.has_trade_in_exchange and estimate.trade_in_discount_amount > Decimal('0.00'):
            effective_trade_in_amt = min(estimate.grand_total, estimate.trade_in_discount_amount)
            trade_in_ref = estimate.trade_in_voucher_reference or "TRADE-IN"
            payment_details.append({
                'id': None,
                'mode': 'TRADE_IN',
                'amount': effective_trade_in_amt,
                'transaction_ref': trade_in_ref,
                'reference': trade_in_ref,
                'trace_id': trade_in_ref,
                'narration': f"Trade-In Buy-Back Voucher Allowance ({trade_in_ref})",
                'is_trade_in': True,
                'voucher_number': trade_in_ref
            })

        try:
            sig = inspect.signature(AutoPostingService.post_sales_estimate)
            call_kwargs = {'estimate': estimate, 'user': cashier}

            if 'payment_details' in sig.parameters:
                call_kwargs['payment_details'] = payment_details
            if 'payment_transactions' in sig.parameters:
                call_kwargs['payment_transactions'] = monetary_transactions
            if 'payments' in sig.parameters:
                call_kwargs['payments'] = payment_details
            if 'trade_in_amount' in sig.parameters:
                call_kwargs['trade_in_amount'] = estimate.trade_in_discount_amount
            if 'bill_type' in sig.parameters and hasattr(estimate, 'bill_type'):
                call_kwargs['bill_type'] = getattr(estimate, 'bill_type', 'SALES')
            if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
                call_kwargs['payment_details'] = payment_details
                call_kwargs['payment_transactions'] = monetary_transactions
                call_kwargs['trade_in_amount'] = estimate.trade_in_discount_amount
                call_kwargs['trade_in_voucher_reference'] = estimate.trade_in_voucher_reference

            result_voucher = AutoPostingService.post_sales_estimate(**call_kwargs)

            cls._enrich_voucher_payment_narrations(
                estimate=estimate,
                payment_transactions=monetary_transactions,
                result_voucher=result_voucher
            )

        except ValidationError:
            raise
        except Exception as err:
            logger.error(
                f"[POS GL Auto-Posting Error] Bill {estimate.estimate_number} failed to post to GL: {err}",
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
                line_narr = (getattr(itm, 'line_narration', '') or '').upper()
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
                current_narr = getattr(matched_item, 'line_narration', '') or ''
                if tx_ref not in current_narr:
                    ref_tag = f"[{tx.payment_mode} Ref: {tx_ref}]"
                    if current_narr:
                        matched_item.line_narration = f"{current_narr} {ref_tag}"[:255]
                    else:
                        matched_item.line_narration = f"Receipt via {tx.payment_mode} {ref_tag} for {estimate.estimate_number}"[:255]
                    matched_item.save(update_fields=['line_narration'])
                items.remove(matched_item)

    # =========================================================================
    # ITEMIZED SALES RETURN & REVERSALS (DIGITAL PAYMENTS TENDER AWARE)
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
        if original_estimate.status in ['CANCELLED', 'RETURNED']:
            raise ValidationError("Cannot process returns from an invoice that is already cancelled or fully returned.")

        if not items_to_return:
            raise ValidationError("Please select at least one line item to return.")

        original_estimate = SalesEstimate.objects.select_for_update().get(pk=original_estimate.pk)

        today_ad = timezone.now().date()
        bs_y, bs_m, bs_d = NepaliCalendar.ad_to_bs(today_ad)
        today_bs = NepaliCalendar.format_bs(bs_y, bs_m, bs_d, lang='en')
        active_fy = NepaliCalendar.get_fiscal_year(bs_y, bs_m)

        sales_return = SalesReturn.objects.create(
            return_number=f"RET-{uuid.uuid4().hex[:8].upper()}",
            original_estimate=original_estimate,
            branch=original_estimate.branch,
            customer=original_estimate.customer,
            return_date_ad=today_ad,
            return_date_bs=today_bs,
            fiscal_year=active_fy,
            refund_mode=refund_mode,
            reason=reason,
            technician_notes=technician_notes,
            processed_by=user,
            total_refund_amount=Decimal('0.00'),
            taxable_amount=Decimal('0.00'),
            non_taxable_amount=Decimal('0.00'),
            vat_amount=Decimal('0.00')
        )

        total_refund = Decimal('0.00')
        total_taxable_return = Decimal('0.00')
        total_non_taxable_return = Decimal('0.00')
        total_vat_reversal = Decimal('0.00')

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

            orig_qty = est_item.quantity if (est_item.quantity and est_item.quantity > Decimal('0.000')) else Decimal('1.000')

            prior_items_qs = SalesReturnItem.objects.filter(
                sales_return__original_estimate=original_estimate,
                estimate_item=est_item
            )
            prior_agg = prior_items_qs.aggregate(
                tot_qty=Sum('return_quantity'),
                tot_taxable=Sum('taxable_return_amount'),
                tot_vat=Sum('vat_reversal_amount'),
                tot_non_taxable=Sum('non_taxable_return_amount'),
                tot_refund=Sum('refund_amount')
            )
            already_returned_qty = (prior_agg['tot_qty'] or Decimal('0.000')).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)
            prior_taxable = (prior_agg['tot_taxable'] or Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            prior_vat = (prior_agg['tot_vat'] or Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            prior_non_taxable = (prior_agg['tot_non_taxable'] or Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            prior_refund = (prior_agg['tot_refund'] or Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            remaining_returnable_qty = max(Decimal('0.000'), orig_qty - already_returned_qty)

            if return_qty <= Decimal('0.000') or return_qty > remaining_returnable_qty:
                raise ValidationError(
                    f"Invalid return quantity ({return_qty}) for '{est_item.product.name}'. "
                    f"Remaining returnable quantity is {remaining_returnable_qty}."
                )

            factor = est_item.conversion_factor if est_item.conversion_factor > Decimal('0.000') else Decimal('1.000')
            base_return_units = (return_qty * factor).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)

            is_final_return = bool(already_returned_qty + return_qty >= orig_qty)
            ratio = min(Decimal('1.000000'), return_qty / orig_qty) if orig_qty > Decimal('0.000') else Decimal('1.000000')

            orig_total = (est_item.line_total or Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            if is_final_return:
                refund_val = max(Decimal('0.00'), orig_total - prior_refund)
            else:
                effective_unit_net_rate = (orig_total / orig_qty).quantize(
                    Decimal('0.01'), rounding=ROUND_HALF_UP
                ) if orig_qty > Decimal('0.000') else est_item.unit_price
                refund_val = (effective_unit_net_rate * return_qty).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            proportionate_item_discount = (
                (est_item.item_discount_amount / est_item.quantity) * return_qty
            ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) if est_item.quantity > Decimal('0.000') else Decimal('0.00')

            # -------------------------------------------------------------
            # STORED HISTORICAL VAT SNAPSHOT DERIVATION FROM ORIGINAL INVOICE
            # -------------------------------------------------------------
            orig_base_taxable = (est_item.base_taxable_amount or est_item.taxable_line_amount or Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            orig_vat = (est_item.tax_amount or Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            orig_vat_rate = (est_item.vat_rate or Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            is_taxable = bool(
                est_item.is_vat_applicable and 
                orig_vat_rate > Decimal('0.00') and 
                (orig_vat > Decimal('0.00') or orig_base_taxable > Decimal('0.00'))
            )

            # Defensive recovery for legacy invoices where VAT rate was set but base/tax were not snapshotted
            if not is_taxable and est_item.is_vat_applicable and orig_vat_rate > Decimal('0.00') and orig_total > Decimal('0.00'):
                divisor = Decimal('1.00') + (orig_vat_rate / Decimal('100.00'))
                orig_base_taxable = (orig_total / divisor).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                orig_vat = (orig_total - orig_base_taxable).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                is_taxable = (orig_vat > Decimal('0.00'))

            if is_taxable:
                line_vat_rate = orig_vat_rate
                rem_vat = max(Decimal('0.00'), orig_vat - prior_vat)
                rem_taxable = max(Decimal('0.00'), orig_base_taxable - prior_taxable)

                if is_final_return:
                    calc_vat = rem_vat
                else:
                    calc_vat = min(rem_vat, (orig_vat * ratio).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))

                line_vat_reversal = calc_vat
                # In both VAT-inclusive and VAT-exclusive items, refund_val already incorporates VAT.
                # Taxable base reversed is the pre-tax portion of the refund: refund_val - line_vat_reversal
                line_taxable_return = max(Decimal('0.00'), refund_val - line_vat_reversal)
                line_non_taxable_return = Decimal('0.00')
            else:
                line_vat_rate = Decimal('0.00')
                line_taxable_return = Decimal('0.00')
                line_vat_reversal = Decimal('0.00')
                line_non_taxable_return = refund_val

            SalesReturnItem.objects.create(
                sales_return=sales_return,
                estimate_item=est_item,
                product=est_item.product,
                return_quantity=return_qty,
                base_unit_quantity=base_return_units,
                refund_amount=refund_val,
                vat_rate=line_vat_rate,
                taxable_return_amount=line_taxable_return,
                non_taxable_return_amount=line_non_taxable_return,
                vat_reversal_amount=line_vat_reversal,
                discount_type=est_item.discount_type,
                discount_input_value=est_item.discount_input_value,
                item_discount_amount=proportionate_item_discount,
                effective_discount_percent=est_item.effective_discount_percent,
                returned_imei=est_item.imei_number,
                restock_to_inventory=not is_defective,
                is_defective=is_defective,
                defect_reason=defect_desc
            )

            total_refund += refund_val
            total_taxable_return += line_taxable_return
            total_non_taxable_return += line_non_taxable_return
            total_vat_reversal += line_vat_reversal

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

                if not est_item.product.requires_imei_tracking:
                    batch = None
                    if est_item.batch_reference:
                        batch = ProductBatch.objects.select_for_update().filter(
                            batch_number=est_item.batch_reference,
                            product=est_item.product,
                            branch=original_estimate.branch
                        ).first()

                    if batch:
                        batch.quantity_remaining += base_return_units
                        batch.is_depleted = False
                        batch.save(update_fields=['quantity_remaining', 'is_depleted', 'updated_at'])
                    else:
                        batch = ProductBatch.objects.select_for_update().filter(
                            product=est_item.product,
                            branch=original_estimate.branch,
                            cost_price=est_item.cost_price,
                            is_depleted=False
                        ).order_by('-created_at').first()

                        if batch:
                            batch.quantity_remaining += base_return_units
                            batch.save(update_fields=['quantity_remaining', 'updated_at'])
                        else:
                            return_batch_no = f"RET-{original_estimate.estimate_number[-6:]}-{uuid.uuid4().hex[:4].upper()}"
                            ProductBatch.objects.create(
                                product=est_item.product,
                                branch=original_estimate.branch,
                                batch_number=return_batch_no,
                                cost_price=est_item.cost_price if est_item.cost_price > Decimal('0.00') else est_item.product.purchase_price,
                                initial_quantity=base_return_units,
                                quantity_remaining=base_return_units,
                                purchase_date=today_ad,
                                is_depleted=False
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

                shelf_stock_qty = branch_stock.quantity
                prev_quarantine = branch_stock.quarantined_defective_quantity
                branch_stock.quarantined_defective_quantity += base_return_units
                branch_stock.save(update_fields=['quarantined_defective_quantity', 'updated_at'])

                StockMovementLog.objects.create(
                    product=est_item.product,
                    branch=original_estimate.branch,
                    movement_type='SERVICE_DEFECTIVE_QUARANTINE',
                    quantity_delta=Decimal('0.000'),
                    previous_quantity=shelf_stock_qty,
                    new_quantity=shelf_stock_qty,
                    reference_document=sales_return.return_number,
                    imei_or_serial_number=est_item.imei_number or "",
                    remarks=(
                        f"Customer Defective Return from Bill {original_estimate.estimate_number}: {defect_desc or 'Hardware Defect'}. "
                        f"Quarantined intake: +{base_return_units} units (Quarantine stock: {prev_quarantine} -> {branch_stock.quarantined_defective_quantity}, "
                        f"Active shelf stock maintained at {shelf_stock_qty})"
                    ),
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

        # ANTI-SCAM REIMBURSEMENT ENGINE (EXPANDED TO ALL GENUINE MONETARY TENDERS)
        has_trade_in = original_estimate.has_trade_in_exchange and (original_estimate.trade_in_discount_amount > Decimal('0.00'))
        actual_cash_refund = total_refund
        excess_store_credit = Decimal('0.00')

        if refund_mode == 'CASH' and has_trade_in:
            monetary_payments_qs = original_estimate.payment_transactions.exclude(
                payment_mode__in=['CREDIT', 'UDHAARI', 'TRADE_IN', 'EXCHANGE']
            )
            total_monetary_paid_on_invoice = monetary_payments_qs.aggregate(
                total=Sum('amount')
            )['total'] or Decimal('0.00')

            previous_cash_refunded = SalesReturn.objects.filter(
                original_estimate=original_estimate,
                refund_mode='CASH'
            ).exclude(pk=sales_return.pk).aggregate(total=Sum('total_refund_amount'))['total'] or Decimal('0.00')

            remaining_monetary_refundable = max(Decimal('0.00'), total_monetary_paid_on_invoice - previous_cash_refunded)

            if total_refund > remaining_monetary_refundable:
                actual_cash_refund = remaining_monetary_refundable
                excess_store_credit = total_refund - remaining_monetary_refundable

                if excess_store_credit > Decimal('0.00') and not original_estimate.customer_id:
                    raise ValidationError(
                        f"Anti-Scam Protection: Original invoice {original_estimate.estimate_number} used "
                        f"Rs. {original_estimate.trade_in_discount_amount:.2f} Trade-In exchange allowance. "
                        f"Total real money tendered on this bill (Cash, FonePay, Card, Bank) was Rs. {total_monetary_paid_on_invoice:.2f} "
                        f"(remaining monetary refund allowed: Rs. {remaining_monetary_refundable:.2f}). "
                        f"The excess return value of Rs. {excess_store_credit:.2f} cannot be paid out as hard cash and "
                        f"must be issued as Store Credit to a registered customer profile. Please select or register the customer."
                    )

        # ---------------------------------------------------------------------
        # RECALCULATE AND SAVE COMPLETE SALES RETURN HEADER TOTALS BEFORE POSTING
        # ---------------------------------------------------------------------
        sales_return.total_refund_amount = total_refund
        sales_return.taxable_amount = total_taxable_return
        sales_return.non_taxable_amount = total_non_taxable_return
        sales_return.vat_amount = total_vat_reversal

        if excess_store_credit > Decimal('0.00'):
            notice = (
                f"\n[Trade-In Anti-Scam Safeguard] Capped Cash Refund: Rs. {actual_cash_refund:.2f} cash paid, "
                f"Rs. {excess_store_credit:.2f} converted to Store Credit due to original trade-in allowance of Rs. {original_estimate.trade_in_discount_amount:.2f}."
            )
            sales_return.technician_notes = f"{sales_return.technician_notes or ''}{notice}".strip()
            if actual_cash_refund == Decimal('0.00'):
                sales_return.refund_mode = 'STORE_CREDIT'

        sales_return.save(update_fields=[
            'total_refund_amount', 'taxable_amount', 'non_taxable_amount',
            'vat_amount', 'refund_mode', 'technician_notes', 'updated_at'
        ])

        customer = None
        if original_estimate.customer_id:
            customer = Customer.objects.select_for_update().filter(id=original_estimate.customer_id, is_active=True).first()

        if customer:
            customer.total_spent = max(Decimal('0.00'), customer.total_spent - total_refund)
            store_credit_to_apply = total_refund if refund_mode == 'STORE_CREDIT' else excess_store_credit

            if store_credit_to_apply > Decimal('0.00'):
                prev_bal = customer.current_credit_balance if customer.current_credit_balance is not None else Decimal('0.00')
                new_bal = prev_bal - store_credit_to_apply
                customer.current_credit_balance = new_bal

                CustomerUdhaariLedger.objects.create(
                    customer=customer,
                    branch=original_estimate.branch,
                    entry_type='ADJUSTMENT',
                    amount=store_credit_to_apply,
                    previous_balance=prev_bal,
                    resulting_balance=new_bal,
                    reference_invoice=sales_return.return_number,
                    payment_mode='OTHER',
                    remarks=(
                        f"Store credit for sales return {sales_return.return_number} from Bill {original_estimate.estimate_number}"
                        + (f" (Capped cash refund split: Rs. {excess_store_credit:.2f} store credit)" if excess_store_credit > Decimal('0.00') else "")
                    ),
                    recorded_by=user
                )

            customer.save(update_fields=['current_credit_balance', 'total_spent', 'updated_at'])

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
                'taxable_amount': str(total_taxable_return),
                'non_taxable_amount': str(total_non_taxable_return),
                'vat_amount': str(total_vat_reversal),
                'actual_cash_refund': str(actual_cash_refund),
                'excess_store_credit': str(excess_store_credit),
                'refund_mode': sales_return.refund_mode,
                'is_fully_returned': is_fully_returned,
                'items_count': len(items_to_return)
            }
        )

        return sales_return

    # =========================================================================
    # BILL CANCELLATION & BALANCED DOUBLE-ENTRY REVERSAL
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
        Atomically voids / cancels an active SalesEstimate:
        1. Leaves original sales journal voucher as POSTED and posts a distinct reversing
           journal voucher as POSTED, preventing the double-reversal bug in trial balance.
        2. Guards against resurrecting already-resold trade-in phones: if the traded-in phone
           was already sold to another customer, the voucher is locked from re-activation.
        3. Restores unsold inventory, voids device component warranties, and reverses debt.
        """
        if estimate.status in ['CANCELLED', 'RETURNED']:
            raise ValidationError(f"Bill {estimate.estimate_number} is already {estimate.get_status_display().lower()}.")

        if estimate.status == 'PARTIALLY_RETURNED':
            raise ValidationError(
                f"Bill {estimate.estimate_number} has already had items returned and restocked. "
                f"To prevent duplicate stock inflation, partially returned bills cannot be voided. "
                f"Please process a sales return for the remaining items instead."
            )

        estimate = SalesEstimate.objects.select_for_update().get(pk=estimate.pk)

        # 1. Reverse Sold Line Items & Physical Warehouse Stock
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

            if not line.product.requires_imei_tracking:
                batch = None
                if line.batch_reference:
                    batch = ProductBatch.objects.select_for_update().filter(
                        batch_number=line.batch_reference,
                        product=line.product,
                        branch=estimate.branch
                    ).first()

                if batch:
                    batch.quantity_remaining += line.base_unit_quantity
                    batch.is_depleted = False
                    batch.save(update_fields=['quantity_remaining', 'is_depleted', 'updated_at'])
                else:
                    batch = ProductBatch.objects.select_for_update().filter(
                        product=line.product,
                        branch=estimate.branch,
                        cost_price=line.cost_price,
                        is_depleted=False
                    ).order_by('-created_at').first()

                    if batch:
                        batch.quantity_remaining += line.base_unit_quantity
                        batch.save(update_fields=['quantity_remaining', 'updated_at'])

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

        # 2. Revert Trade-In Vouchers with Resold Phone Security Guard
        trade_in_filter = Q(pos_estimate=estimate)
        if estimate.trade_in_voucher_reference:
            trade_in_filter |= Q(voucher_number__iexact=estimate.trade_in_voucher_reference)

        linked_trade_ins = PhoneExchangeTradeIn.objects.select_for_update().filter(trade_in_filter).distinct()
        trade_ins_reversed = []
        trade_ins_locked_resold = []

        for voucher in linked_trade_ins:
            restocked_item = voucher.restocked_item_instance
            if not restocked_item and voucher.imei_1:
                restocked_item = ItemInstance.objects.select_for_update().filter(
                    trade_in_voucher_reference=voucher.voucher_number
                ).exclude(status='ARCHIVED').first()

            # SECURITY GUARD: Inspect if the traded-in phone was ALREADY resold to another customer!
            if restocked_item and restocked_item.status == 'SOLD':
                lock_note = (
                    f"[LOCKED ON VOID] Traded-in phone (IMEI: {voucher.imei_1}) was already resold on another invoice "
                    f"({restocked_item.sold_invoice_reference or 'Active Sale'}). "
                    f"Voucher cannot be reset to VALUATED to prevent duplicate credit claims."
                )
                if hasattr(voucher, 'evaluation_notes'):
                    voucher.evaluation_notes = f"{voucher.evaluation_notes or ''}\n{lock_note}".strip()
                voucher.pos_estimate = None
                voucher.status = 'RESTOCKED'
                voucher.save(update_fields=['pos_estimate', 'status', 'evaluation_notes', 'updated_at'])
                trade_ins_locked_resold.append(voucher.voucher_number)
                logger.warning(f"[TradeIn Security Lock] {lock_note}")
                continue

            # If still IN_STOCK, archive it and remove from inventory
            if restocked_item and restocked_item.status == 'IN_STOCK':
                restocked_item.status = 'ARCHIVED'
                restocked_item.save(update_fields=['status', 'updated_at'])

                if voucher.restocked_product:
                    InventoryService.adjust_stock(
                        product=voucher.restocked_product,
                        branch=estimate.branch,
                        quantity_delta=-Decimal('1.000'),
                        movement_type='TRADE_IN_CANCELLATION',
                        reference_doc=f"VOID-{estimate.estimate_number}",
                        imei_or_serial=voucher.imei_1 or voucher.serial_number or "",
                        remarks=f"Trade-in buy-back intake reversed due to voided bill {estimate.estimate_number}",
                        user=user,
                        allow_negative=True
                    )

            # Reversal of surplus credit on customer account if applicable
            if estimate.customer_id and estimate.excess_trade_in_credit > Decimal('0.00'):
                cust = Customer.objects.select_for_update().filter(id=estimate.customer_id, is_active=True).first()
                if cust:
                    prev_bal = cust.current_credit_balance if cust.current_credit_balance is not None else Decimal('0.00')
                    new_bal = prev_bal + estimate.excess_trade_in_credit
                    cust.current_credit_balance = new_bal
                    cust.save(update_fields=['current_credit_balance', 'updated_at'])

                    CustomerUdhaariLedger.objects.create(
                        customer=cust,
                        branch=estimate.branch,
                        entry_type='DEBIT',
                        amount=estimate.excess_trade_in_credit,
                        previous_balance=prev_bal,
                        resulting_balance=new_bal,
                        reference_invoice=f"VOID-{estimate.estimate_number}",
                        payment_mode='OTHER',
                        remarks=f"Reversal of surplus trade-in credit from voucher {voucher.voucher_number} on voided bill {estimate.estimate_number}",
                        recorded_by=user
                    )

            # Safe to reset un-sold device voucher back to VALUATED
            voucher.pos_estimate = None
            voucher.status = 'VALUATED'
            if restocked_item and restocked_item.status == 'ARCHIVED':
                voucher.restocked_item_instance = None
                voucher.restocked_product = None
            voucher.save(update_fields=['pos_estimate', 'status', 'restocked_item_instance', 'restocked_product', 'updated_at'])
            trade_ins_reversed.append(voucher.voucher_number)

        # 3. Revert Linked Repair Tickets & Payroll Commission
        linked_repairs = RepairTicket.objects.select_for_update().filter(
            pos_invoice_reference=estimate.estimate_number
        )
        repairs_reverted = []
        payroll_adjustment_notes = []

        for ticket in linked_repairs:
            ticket.service_status = 'READY_FOR_PICKUP'
            ticket.pos_invoice_reference = None
            ticket.delivered_date = None
            ticket.paid_amount = Decimal('0.00')

            comm_qs = TechnicianCommissionLog.objects.select_for_update().filter(ticket=ticket)
            for comm_log in comm_qs:
                is_settled = getattr(comm_log, 'is_settled_in_payroll', False)
                if not is_settled:
                    comm_log.delete()
                else:
                    adj_note = (
                        f"Commission of Rs. {comm_log.commission_earned:.2f} for technician "
                        f"'{comm_log.technician.username}' on Ticket {ticket.ticket_number} was already "
                        f"settled in payroll before Bill {estimate.estimate_number} was voided. "
                        f"Manual clawback/deduction required in next payroll run."
                    )
                    payroll_adjustment_notes.append(adj_note)
                    logger.warning(f"[Payroll Commission Adjustment Required] {adj_note}")

                    if hasattr(comm_log, 'remarks'):
                        comm_log.remarks = f"{getattr(comm_log, 'remarks') or ''} [CLAWBACK DUE: {adj_note}]".strip()
                        comm_log.save(update_fields=['remarks', 'updated_at'])
                    elif hasattr(comm_log, 'notes'):
                        comm_log.notes = f"{getattr(comm_log, 'notes') or ''} [CLAWBACK DUE: {adj_note}]".strip()
                        comm_log.save(update_fields=['notes', 'updated_at'])

                    AuditLog.objects.create(
                        user=user,
                        branch=estimate.branch,
                        action_type='UPDATE',
                        module='PayrollCommissionClawback',
                        object_repr=f"Commission #{comm_log.id} ({comm_log.technician.username})",
                        details={
                            'ticket_number': ticket.ticket_number,
                            'technician_username': comm_log.technician.username,
                            'commission_earned': str(comm_log.commission_earned),
                            'estimate_number': estimate.estimate_number,
                            'is_settled_in_payroll': True,
                            'adjustment_note': adj_note
                        }
                    )

            if payroll_adjustment_notes:
                notes_text = " | ".join(payroll_adjustment_notes)
                if ticket.technician_diagnostic_findings:
                    ticket.technician_diagnostic_findings = f"{ticket.technician_diagnostic_findings}\n[PAYROLL NOTE]: {notes_text}"
                else:
                    ticket.technician_diagnostic_findings = f"[PAYROLL NOTE]: {notes_text}"

            ticket.save(update_fields=[
                'service_status', 'pos_invoice_reference', 'delivered_date',
                'paid_amount', 'technician_diagnostic_findings', 'updated_at'
            ])
            repairs_reverted.append(ticket.ticket_number)

        # 4. Reconcile Customer Udhaari Debt
        if estimate.customer_id and estimate.due_amount > Decimal('0.00'):
            cust = Customer.objects.select_for_update().filter(id=estimate.customer_id, is_active=True).first()
            if cust:
                prev_bal = cust.current_credit_balance if cust.current_credit_balance is not None else Decimal('0.00')
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

        # 5. Mark Estimate Status as CANCELLED
        estimate.status = 'CANCELLED'
        estimate.cancellation_reason = reason
        estimate.save(update_fields=['status', 'cancellation_reason', 'updated_at'])

        # 6. Audit Standard General Ledger Reversal (No Double-Reversal)
        try:
            from apps.accounting.models import JournalEntry
            from apps.accounting.services.auto_posting import JournalEngine

            # In standard double-entry bookkeeping:
            # - Keep the original sales entry as status='POSTED' (do NOT mark CANCELLED).
            # - Post an equal and opposite reversing entry as status='POSTED'.
            # - Both entries offset each other to exactly zero in general ledger recalculations.
            orig_entry = JournalEntry.objects.filter(
                voucher_type='SALES',
                reference_document=estimate.estimate_number,
                status='POSTED'
            ).first()

            if orig_entry:
                cross_ref_tag = f"[REVERSED by REV-{estimate.estimate_number} on {timezone.now().strftime('%Y-%m-%d')}]"
                if cross_ref_tag not in (orig_entry.narration or ""):
                    orig_entry.narration = f"{orig_entry.narration or ''} {cross_ref_tag}".strip()
                    orig_entry.save(update_fields=['narration', 'updated_at'])

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

        # 7. Forensic Audit Log Entry
        audit_details = {
            'estimate_number': estimate.estimate_number,
            'grand_total': str(estimate.grand_total),
            'due_amount_reversed': str(estimate.due_amount),
            'trade_ins_reversed': trade_ins_reversed,
            'trade_ins_locked_resold': trade_ins_locked_resold,
            'repairs_reverted': repairs_reverted,
            'reason': reason
        }
        if payroll_adjustment_notes:
            audit_details['payroll_adjustment_notes'] = payroll_adjustment_notes

        AuditLog.objects.create(
            user=user,
            branch=estimate.branch,
            action_type='BILL_CANCEL',
            module='POS_Sales',
            object_repr=estimate.estimate_number,
            details=audit_details
        )

        return estimate