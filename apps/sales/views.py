"""
Sales & POS Counter Views Module.

Core Architecture & Capabilities:
1. POSTerminalView:
   - High-speed zero-storage POS billing single page interface.
   - Enforces an active open cash drawer session before accepting transactions.
2. POSCheckoutAPIView (Security & Authoritative Discount Engine):
   - Accepts structured checkout payloads from the POS terminal.
   - Idempotency-Key validation preventing double-billing on network lags.
   - Explicit acceptance and parsing of line-item discounts: AMOUNT, PERCENTAGE, and NONE.
   - Explicit acceptance and parsing of bill-level discounts: AMOUNT, PERCENTAGE, and NONE.
   - Server-Side Validation: Rejects client-side attempts to submit negative discount amounts,
     discounts greater than line gross, or discounts on non-discountable items (is_discountable == False).
   - Authoritative Calculations: Never trusts frontend totals or percentages. Extracts raw inputs,
     calculates exact line net amounts and computes secondary control percentages.
   - Server-Side Commercial Concession Verification & Manager Threshold Enforcement:
     If an entered amount or percentage discount causes the effective discount percentage
     to exceed product limits or global system thresholds, prompts for Manager Override PIN.
   - Cryptographically Salted Supervisor PIN Authentication (SEC-02).
   - Preserves structured commercial justification reasons (e.g., "CUSTOMER_NEGOTIATION", "CLEARANCE_SALE").
   - Returns authoritative calculated effective percentages and line totals in JSON response.
   - Bridges linked service repair ticket delivery and dynamic commission recording.
3. SalesEstimateDetailView & SalesEstimateThermalSlipView:
   - Detailed invoice review, thermal 80mm/58mm printing, and formal A4 sheet views.
4. Sales Estimate Voiding & Cancellation:
   - cancel_estimate_view (Dedicated View Function):
     * Mandates a formal cancellation justification reason.
     * Changes bill status to 'CANCELLED'.
     * Restores physical inventory to BranchStock and FIFO batches.
     * Re-marks serialized handset ItemInstances as 'IN_STOCK' and voids customer warranties.
     * Reverses customer Udhaari debt and lifetime spend.
     * Voids General Ledger journal vouchers and creates reversing journal vouchers.
     * Logs immutable forensic AuditLog record.
   - SalesEstimateCancelView (Class-Based View Wrapper for routing compatibility).
5. Itemized Sales Returns:
   - SalesReturnListView, SalesReturnCreateView, SalesReturnDetailView, SalesReturnThermalSlipView.
6. Trade-In & Buy-Back Vouchers:
   - TradeInListView, TradeInEvaluationWizardView, TradeInDetailView, TradeInPoliceUndertakingPrintView.
"""

import json
import uuid
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Any, Optional, Dict, List, Tuple

from django.shortcuts import render, redirect, get_object_or_404
from django.views.generic import TemplateView, ListView, DetailView, View
from django.views.decorators.http import require_http_methods, require_POST
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib import messages
from django.http import JsonResponse
from django.db.models import Q, Sum
from django.db import transaction
from django.utils import timezone
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.sales.models import (
    SalesEstimate, SalesEstimateItem, SalesPaymentTransaction, SalesReturn, SalesReturnItem,
    PhoneExchangeTradeIn, TradeInInspectionChecklist, TradeInLegalUndertaking
)
from apps.sales.forms import (
    TradeInDeviceIntakeForm, TradeIn10PointChecklistForm, TradeInLegalUndertakingForm,
    SalesReturnProcessForm
)
from apps.sales.services import SalesPOSService, TradeInValuationEngine
from apps.inventory.models import (
    Product, ProductCategory, Brand, UnitConversion, ItemInstance, ProductBatch,
    DeviceComponentWarranty, BranchStock
)
from apps.products.services import ProductCatalogService
from apps.customers.models import Customer, CustomerUdhaariLedger
from apps.inventory.services import InventoryService
from apps.branches.models import Branch
from apps.repairs.models import RepairTicket, TechnicianCommissionLog
from apps.pos.models import CashDrawerSession
from apps.pos.services import POSSessionService
from apps.core.models import SystemConfiguration, AuditLog
from apps.users.models import User

# ==============================================================================
# 1. POS COUNTER TERMINAL & CHECKOUT VIEWS
# ==============================================================================
class POSTerminalView(LoginRequiredMixin, TemplateView):
    """
    Primary POS Counter Billing Single Page Interface (Zero-Storage Fast Interface).
    Enforces active open cash drawer session and loads active categories,
    registered customer profiles, and tax configurations directly for instant launch.
    """
    template_name = 'sales/pos_terminal.html'

    def dispatch(self, request, *args, **kwargs):
        branch = getattr(request, 'active_branch', None)
        if not branch:
            messages.error(request, "Please select an active store branch before accessing the POS terminal.")
            return redirect('branches:branch_list')

        # Verify cashier has an active open cash drawer shift
        active_session = POSSessionService.get_active_session(request.user, branch)
        if not active_session:
            messages.warning(request, "Please open your cash drawer shift float before accessing the POS billing terminal.")
            return redirect('pos:open_shift')

        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        config = SystemConfiguration.get_solo()
        branch = getattr(self.request, 'active_branch', None) or Branch.get_default_main_branch()

        # Load active categories and registered customers
        categories = ProductCategory.objects.filter(is_active=True).order_by('name')
        customers = Customer.objects.filter(is_active=True).order_by('name')[:100]

        context.update({
            'config': config,
            'active_branch': branch,
            'active_session': POSSessionService.get_active_session(self.request.user, branch),
            'categories': categories,
            'customers': customers,
            'is_estimate_only': config.is_estimation_bill_only,
            'estimate_disclaimer': config.bill_estimate_disclaimer,
            'SHOP_TAX_MODE': config.tax_system_mode,
            'DEFAULT_TAX_RATE': str(config.default_vat_rate) if config.tax_system_mode == 'VAT' else '0.00',
        })

        repair_id = self.request.GET.get('repair_ticket')
        if repair_id:
            context['prefill_repair_ticket'] = RepairTicket.objects.filter(
                id=repair_id, service_status='READY_FOR_PICKUP'
            ).first()

        return context


class POSCheckoutAPIView(LoginRequiredMixin, View):
    """
    Atomic endpoint invoked by the POS Terminal upon checkout.

    Security & Authoritative Processing Flow:
    1. Validates active cash drawer shift session.
    2. Enforces Idempotency-Key validation to prevent duplicate billing on network lags.
    3. Normalizes and validates dual-mode line discounts: AMOUNT, PERCENTAGE, and NONE.
       - Rejects negative discount inputs.
       - Rejects discount amounts exceeding the line gross value.
       - Rejects discounts on products marked as non-discountable (product.is_discountable == False).
    4. Normalizes and validates dual-mode bill discounts: AMOUNT, PERCENTAGE, and NONE.
       - Validates that bill discount amounts do not exceed the discountable merchandise subtotal.
       - Captures commercial justification reasons (e.g. CUSTOMER_NEGOTIATION, CLEARANCE_SALE).
    5. Re-runs all calculations authoritatively on the server, ignoring untrusted client figures.
    6. Converts fixed AMOUNT discounts into effective control percentages on the fly to test
       against line item maximum thresholds and global supervisor limits.
    7. Authenticates supervisor override PINs via salted PBKDF2 hash verification (SEC-02).
    8. Dispatches structured data to SalesPOSService.process_checkout.
    9. Bridges linked repair ticket completion, deliveries, and dynamic technician commission logging.
    10. Returns authoritative calculated effective percentages and financial figures in the response.
    """

    @staticmethod
    def _normalize_discount_type(raw_val: Any) -> str:
        """Standardizes discount type to 'AMOUNT', 'PERCENTAGE', or 'NONE'."""
        cleaned = str(raw_val or 'PERCENTAGE').upper().strip()
        if cleaned in ['AMOUNT', 'FIXED', 'FLAT', 'CASH', 'NPR', 'RS']:
            return 'AMOUNT'
        elif cleaned in ['PERCENTAGE', '%', 'PERCENT']:
            return 'PERCENTAGE'
        elif cleaned == 'NONE':
            return 'NONE'
        return 'PERCENTAGE'

    def post(self, request, *args, **kwargs):
        branch = getattr(request, 'active_branch', None)
        if not branch:
            return JsonResponse({'status': 'error', 'message': 'No active branch selected.'}, status=400)

        # 1. Require active open shift session before processing billing
        active_session = POSSessionService.get_active_session(request.user, branch)
        if not active_session:
            return JsonResponse({
                'status': 'error',
                'error_code': 'NO_ACTIVE_SHIFT',
                'redirect_url': reverse('pos:open_shift'),
                'message': 'No active cash drawer shift found. You must open your register shift with a starting cash float before making sales.'
            }, status=403)

        config = SystemConfiguration.get_solo()
        idempotency_key = None
        cache_key = None

        try:
            payload = json.loads(request.body.decode('utf-8'))

            # 2. Idempotency Verification & Replay Protection
            idempotency_key = payload.get('idempotency_key') or request.headers.get('X-Idempotency-Key')
            if idempotency_key:
                cache_key = f"pos_checkout_idempotency_{idempotency_key}"
                cached_response = cache.get(cache_key)
                if cached_response:
                    return JsonResponse(cached_response)
                if not cache.add(f"lock_{cache_key}", "1", timeout=15):
                    return JsonResponse({
                        'status': 'error',
                        'message': 'Transaction is already being processed. Please do not submit again.'
                    }, status=409)

            cart = payload.get('cart', [])
            payments = payload.get('payments', [])
            customer_id = payload.get('customer_id') or None
            cust_name = payload.get('customer_name', '').strip()
            cust_phone = payload.get('customer_phone', '').strip()
            cust_pan = payload.get('customer_pan', '').strip()
            trade_in_voucher_id = payload.get('trade_in_voucher_id') or None
            notes = payload.get('notes', '').strip()
            manager_pin = payload.get('manager_pin', '').strip()
            repair_ticket_id = payload.get('repair_ticket_id')

            if not cart:
                if idempotency_key and cache_key:
                    cache.delete(f"lock_{cache_key}")
                return JsonResponse({'status': 'error', 'message': 'Cart is empty. Please add items.'}, status=400)

            # 3. Customer Profile & Tier Resolution
            customer_type = 'RETAIL'
            if customer_id:
                cust_record = Customer.objects.filter(id=customer_id, is_active=True).first()
                if cust_record:
                    customer_type = cust_record.customer_type

            # 4. Structured Server-Side Discount Parsing & Authoritative Calculation
            threshold = config.require_manager_approval_discount or Decimal('10.00')
            requires_manager_pin = False
            override_reasons = []

            running_subtotal = Decimal('0.00')
            running_item_discount_total = Decimal('0.00')
            discountable_net_subtotal = Decimal('0.00')

            # 4A. Validate Line-Item Pricing and Dual-Mode Discounts
            for item in cart:
                prod_id = item.get('product_id')
                if not prod_id:
                    continue

                try:
                    product_obj = Product.objects.get(id=prod_id)
                except Product.DoesNotExist:
                    if idempotency_key and cache_key:
                        cache.delete(f"lock_{cache_key}")
                    return JsonResponse({'status': 'error', 'message': f"Product ID {prod_id} not found in catalog."}, status=400)

                try:
                    qty = Decimal(str(item.get('quantity', 1))).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)
                except (InvalidOperation, ValueError, TypeError):
                    qty = Decimal('1.000')

                if qty <= Decimal('0.000'):
                    if idempotency_key and cache_key:
                        cache.delete(f"lock_{cache_key}")
                    return JsonResponse({'status': 'error', 'message': f"Invalid quantity for '{product_obj.name}'."}, status=400)

                # Packaging unit factor
                pkg_conversion_id = item.get('unit_conversion_id') or item.get('conversion_id')
                factor = Decimal('1.000')
                unit_conv = None
                if pkg_conversion_id:
                    unit_conv = UnitConversion.objects.filter(id=pkg_conversion_id, product=product_obj).first()
                    if unit_conv:
                        factor = unit_conv.conversion_factor

                # Official catalog price verification
                base_official = ProductCatalogService.get_applicable_price(product_obj, qty * factor, customer_type)
                if unit_conv and unit_conv.selling_price_per_unit:
                    official_price = unit_conv.selling_price_per_unit
                else:
                    official_price = base_official * factor
                official_price = official_price.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

                # Submitted unit selling price
                raw_unit_price = item.get('unit_price')
                if raw_unit_price is None or str(raw_unit_price).strip() == '':
                    raw_unit_price = item.get('price')
                if raw_unit_price is None or str(raw_unit_price).strip() == '':
                    raw_unit_price = item.get('selling_price')

                try:
                    submitted_price = Decimal(str(raw_unit_price if raw_unit_price is not None else official_price)).quantize(
                        Decimal('0.01'), rounding=ROUND_HALF_UP
                    )
                except (InvalidOperation, ValueError, TypeError):
                    submitted_price = official_price

                if submitted_price < Decimal('0.00'):
                    if idempotency_key and cache_key:
                        cache.delete(f"lock_{cache_key}")
                    return JsonResponse({'status': 'error', 'message': f"Unit price for '{product_obj.name}' cannot be negative."}, status=400)

                line_official_gross = (qty * official_price).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                line_submitted_gross = (qty * submitted_price).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

                # Parse and Authoritatively Validate Item Discount Type
                raw_item_disc_type = self._normalize_discount_type(item.get('discount_type'))

                raw_item_disc_val = item.get('discount_input_value')
                if raw_item_disc_val is None or str(raw_item_disc_val).strip() == '':
                    raw_item_disc_val = item.get('discount_value')
                if raw_item_disc_val is None or str(raw_item_disc_val).strip() == '':
                    raw_item_disc_val = item.get('discount_percent', 0)

                try:
                    item_disc_val = Decimal(str(raw_item_disc_val if raw_item_disc_val is not None else 0)).quantize(
                        Decimal('0.01'), rounding=ROUND_HALF_UP
                    )
                except (InvalidOperation, ValueError, TypeError):
                    item_disc_val = Decimal('0.00')

                # Reject negative discount numbers
                if item_disc_val < Decimal('0.00'):
                    if idempotency_key and cache_key:
                        cache.delete(f"lock_{cache_key}")
                    return JsonResponse({'status': 'error', 'message': f"Discount for '{product_obj.name}' cannot be negative."}, status=400)

                # Enforce non-discountable products
                is_item_discountable = getattr(product_obj, 'is_discountable', True)
                if not is_item_discountable and item_disc_val > Decimal('0.00'):
                    if idempotency_key and cache_key:
                        cache.delete(f"lock_{cache_key}")
                    return JsonResponse({
                        'status': 'error',
                        'message': f"Product '{product_obj.name}' is designated as non-discountable. Line item discount cannot be applied."
                    }, status=400)

                # Calculate item discount deduction and secondary control percentage
                if not is_item_discountable or raw_item_disc_type == 'NONE' or item_disc_val == Decimal('0.00'):
                    raw_item_disc_type = 'NONE'
                    item_disc_val = Decimal('0.00')
                    line_disc_amt = Decimal('0.00')
                    effective_item_disc_pct = Decimal('0.00')
                elif raw_item_disc_type == 'AMOUNT':
                    if item_disc_val > line_submitted_gross:
                        if idempotency_key and cache_key:
                            cache.delete(f"lock_{cache_key}")
                        return JsonResponse({
                            'status': 'error',
                            'message': f"Discount amount of Rs. {item_disc_val:.2f} on '{product_obj.name}' cannot exceed line gross value of Rs. {line_submitted_gross:.2f}."
                        }, status=400)
                    line_disc_amt = item_disc_val
                    effective_item_disc_pct = (
                        ((line_disc_amt / line_submitted_gross) * Decimal('100.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                        if line_submitted_gross > Decimal('0.00') else Decimal('0.00')
                    )
                elif raw_item_disc_type == 'PERCENTAGE':
                    if item_disc_val > Decimal('100.00'):
                        if idempotency_key and cache_key:
                            cache.delete(f"lock_{cache_key}")
                        return JsonResponse({
                            'status': 'error',
                            'message': f"Discount percentage for '{product_obj.name}' cannot exceed 100.00%."
                        }, status=400)
                    effective_item_disc_pct = item_disc_val
                    line_disc_amt = (line_submitted_gross * (effective_item_disc_pct / Decimal('100.00'))).quantize(
                        Decimal('0.01'), rounding=ROUND_HALF_UP
                    )

                line_submitted_net = max(Decimal('0.00'), line_submitted_gross - line_disc_amt)
                running_subtotal += line_submitted_gross
                running_item_discount_total += line_disc_amt
                if is_item_discountable:
                    discountable_net_subtotal += line_submitted_net

                # Re-sync validated authoritative parameters onto the cart item
                item['discount_type'] = raw_item_disc_type
                item['discount_value'] = item_disc_val
                item['discount_input_value'] = item_disc_val
                item['discount_percent'] = effective_item_disc_pct
                item['unit_price'] = submitted_price

                # Check Commercial Concessions vs Supervisor Thresholds
                total_concession = max(Decimal('0.00'), line_official_gross - line_submitted_net)
                effective_commercial_pct = Decimal('0.00')
                if line_official_gross > Decimal('0.00'):
                    effective_commercial_pct = (
                        (total_concession / line_official_gross) * Decimal('100.00')
                    ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

                prod_max_disc = getattr(product_obj, 'max_discount_percent', Decimal('10.00'))
                item_threshold = min(threshold, prod_max_disc)

                if submitted_price < official_price or effective_commercial_pct > item_threshold:
                    requires_manager_pin = True
                    if submitted_price < official_price:
                        override_reasons.append(
                            f"Price override on '{product_obj.name}' (Catalog: Rs. {official_price:.2f} -> Submitted: Rs. {submitted_price:.2f})"
                        )
                    if effective_commercial_pct > item_threshold:
                        override_reasons.append(
                            f"Discount on '{product_obj.name}' ({effective_commercial_pct:.2f}% exceeds allowed {item_threshold:.2f}%)"
                        )

            # 4B. Validate Bill-Level Dual-Mode Discount
            raw_bill_disc_type = self._normalize_discount_type(
                payload.get('bill_discount_type') or payload.get('discount_type')
            )

            raw_bill_disc_val = payload.get('bill_discount_input_value')
            if raw_bill_disc_val is None or str(raw_bill_disc_val).strip() == '':
                raw_bill_disc_val = payload.get('bill_discount_value')
            if raw_bill_disc_val is None or str(raw_bill_disc_val).strip() == '':
                raw_bill_disc_val = payload.get('discount_value')
            if raw_bill_disc_val is None or str(raw_bill_disc_val).strip() == '':
                raw_bill_disc_val = payload.get('discount_percent', Decimal('0.00'))

            try:
                bill_discount_input_value = Decimal(str(raw_bill_disc_val if raw_bill_disc_val is not None else 0)).quantize(
                    Decimal('0.01'), rounding=ROUND_HALF_UP
                )
            except (InvalidOperation, ValueError, TypeError):
                bill_discount_input_value = Decimal('0.00')

            if bill_discount_input_value < Decimal('0.00'):
                if idempotency_key and cache_key:
                    cache.delete(f"lock_{cache_key}")
                return JsonResponse({'status': 'error', 'message': 'Bill discount cannot be negative.'}, status=400)

            discount_reason = str(payload.get('discount_reason', '') or payload.get('reason', '')).strip()

            if bill_discount_input_value > Decimal('0.00') and discountable_net_subtotal <= Decimal('0.00'):
                if idempotency_key and cache_key:
                    cache.delete(f"lock_{cache_key}")
                return JsonResponse({
                    'status': 'error',
                    'message': 'Bill discount cannot be applied because there are no discountable items in the cart.'
                }, status=400)

            if raw_bill_disc_type == 'AMOUNT':
                if bill_discount_input_value > discountable_net_subtotal:
                    if idempotency_key and cache_key:
                        cache.delete(f"lock_{cache_key}")
                    return JsonResponse({
                        'status': 'error',
                        'message': f"Bill discount amount of Rs. {bill_discount_input_value:.2f} cannot exceed the discountable subtotal of Rs. {discountable_net_subtotal:.2f}."
                    }, status=400)
                bill_discount_amt = bill_discount_input_value
                effective_bill_disc_pct = (
                    ((bill_discount_amt / discountable_net_subtotal) * Decimal('100.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                    if discountable_net_subtotal > Decimal('0.00') else Decimal('0.00')
                )
            elif raw_bill_disc_type == 'PERCENTAGE':
                if bill_discount_input_value > Decimal('100.00'):
                    if idempotency_key and cache_key:
                        cache.delete(f"lock_{cache_key}")
                    return JsonResponse({'status': 'error', 'message': 'Bill discount percentage cannot exceed 100.00%.'}, status=400)
                effective_bill_disc_pct = bill_discount_input_value
                bill_discount_amt = (discountable_net_subtotal * (effective_bill_disc_pct / Decimal('100.00'))).quantize(
                    Decimal('0.01'), rounding=ROUND_HALF_UP
                )
            else:
                raw_bill_disc_type = 'NONE'
                bill_discount_input_value = Decimal('0.00')
                effective_bill_disc_pct = Decimal('0.00')
                bill_discount_amt = Decimal('0.00')

            if effective_bill_disc_pct > threshold:
                requires_manager_pin = True
                override_reasons.append(
                    f"Bill discount of {effective_bill_disc_pct:.2f}% (Rs. {bill_discount_amt:.2f}) exceeds store manager threshold ({threshold:.2f}%)"
                )

            # 5. Authenticate Manager PIN if Threshold Exceeded (SEC-02 Salted Hash Check)
            is_cashier_privileged = request.user.is_superuser or getattr(request.user, 'role', '') in ['OWNER', 'MANAGER']
            manager_override_user = None

            if requires_manager_pin:
                if is_cashier_privileged:
                    manager_override_user = request.user
                else:
                    if not manager_pin:
                        if idempotency_key and cache_key:
                            cache.delete(f"lock_{cache_key}")
                        return JsonResponse({
                            'status': 'error',
                            'requires_manager_pin': True,
                            'override_reasons': override_reasons,
                            'message': f"Manager PIN authorization required: {'; '.join(override_reasons)}"
                        }, status=403)

                    # Find and verify supervisor via constant-time salted hash check
                    eligible_supervisors = User.objects.filter(
                        is_active=True
                    ).filter(
                        Q(role__in=['OWNER', 'MANAGER']) | Q(is_superuser=True)
                    ).exclude(
                        Q(pin_code__isnull=True) | Q(pin_code='')
                    )

                    for candidate in eligible_supervisors:
                        if candidate.check_pin(manager_pin):
                            manager_override_user = candidate
                            break

                    if not manager_override_user:
                        if idempotency_key and cache_key:
                            cache.delete(f"lock_{cache_key}")
                        return JsonResponse({
                            'status': 'error',
                            'requires_manager_pin': True,
                            'message': 'Invalid Manager Override PIN. Price override rejected.'
                        }, status=403)

            # 6. Atomic Checkout Execution via SalesPOSService
            with transaction.atomic():
                estimate = SalesPOSService.process_checkout(
                    branch=branch,
                    cashier=request.user,
                    cart_items=cart,
                    payments=payments,
                    customer_id=customer_id,
                    customer_name=cust_name,
                    customer_phone=cust_phone,
                    customer_pan=cust_pan,
                    bill_discount_type=raw_bill_disc_type,
                    bill_discount_input_value=bill_discount_input_value,
                    bill_discount_percent=effective_bill_disc_pct,
                    discount_reason=discount_reason,
                    trade_in_voucher_id=trade_in_voucher_id,
                    manager_override_user=manager_override_user,
                    notes=notes
                )

                # 7. Bridge Handover of Linked Repair Ticket
                if repair_ticket_id:
                    repair_ticket = RepairTicket.objects.select_for_update().filter(id=repair_ticket_id).first()
                    if not repair_ticket:
                        raise ValidationError(f"Repair Ticket ID {repair_ticket_id} was not found.")

                    if repair_ticket.service_status == 'DELIVERED':
                        raise ValidationError(f"Repair Ticket {repair_ticket.ticket_number} has already been marked as delivered.")

                    is_free_warranty = (repair_ticket.claim_type == 'FREE_WARRANTY')
                    repair_cost = repair_ticket.final_total_amount

                    if not is_free_warranty and repair_cost > Decimal('0.00'):
                        if estimate.grand_total < repair_cost:
                            raise ValidationError(
                                f"Repair Ticket {repair_ticket.ticket_number} requires Rs. {repair_cost:.2f}, "
                                f"but the sales bill total (Rs. {estimate.grand_total:.2f}) does not cover this repair fee."
                            )

                    repair_ticket.service_status = 'DELIVERED'
                    repair_ticket.delivered_date = timezone.now()
                    repair_ticket.pos_invoice_reference = estimate.estimate_number
                    repair_ticket.paid_amount = repair_ticket.final_total_amount
                    repair_ticket.save(update_fields=['service_status', 'delivered_date', 'pos_invoice_reference', 'paid_amount', 'updated_at'])

                    if repair_ticket.technician and repair_ticket.labor_charge > Decimal('0.00'):
                        technician_user = repair_ticket.technician
                        commission_rate = getattr(technician_user, 'commission_percentage', None)
                        if commission_rate is None:
                            commission_rate = getattr(technician_user, 'repair_commission_rate', None)
                        if commission_rate is None:
                            commission_rate = getattr(config, 'default_technician_commission_rate', None)
                        if commission_rate is None:
                            commission_rate = getattr(config, 'technician_commission_percent', None)
                        if commission_rate is None:
                            commission_rate = Decimal('30.00')
                        else:
                            commission_rate = Decimal(str(commission_rate)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

                        TechnicianCommissionLog.objects.create(
                            ticket=repair_ticket,
                            technician=technician_user,
                            branch=branch,
                            labor_amount_collected=repair_ticket.labor_charge,
                            commission_percentage=commission_rate
                        )

            # Authoritative response including verified numbers
            response_data = {
                'status': 'success',
                'estimate_id': estimate.id,
                'estimate_number': estimate.estimate_number,
                'grand_total': str(estimate.grand_total),
                'subtotal': str(estimate.subtotal),
                'item_discount_total': str(estimate.item_discount_total),
                'bill_discount_amount': str(estimate.bill_discount_amount),
                'bill_discount_type': estimate.bill_discount_type,
                'bill_discount_percent': str(estimate.bill_discount_percent),
                'total_sales_discount': str(estimate.total_sales_discount),
                'discount_reason': estimate.discount_reason or "",
                'print_url': f"/sales/estimates/{estimate.id}/thermal-slip/"
            }

            if idempotency_key and cache_key:
                cache.set(cache_key, response_data, timeout=3600)
                cache.delete(f"lock_{cache_key}")

            return JsonResponse(response_data)

        except ValidationError as ve:
            if idempotency_key and cache_key:
                cache.delete(f"lock_{cache_key}")
            msg = ve.message if hasattr(ve, 'message') else str(ve)
            return JsonResponse({'status': 'error', 'message': msg}, status=400)
        except Exception as err:
            if idempotency_key and cache_key:
                cache.delete(f"lock_{cache_key}")
            return JsonResponse({'status': 'error', 'message': str(err)}, status=400)

# ==============================================================================
# 2. SALES ESTIMATE INVOICE VIEWS
# ==============================================================================
class SalesEstimateListView(LoginRequiredMixin, ListView):
    """
    Lists historical estimation slips and POS invoices with pagination,
    branch scoping, and multi-parameter search (slip no, customer, phone, PAN).
    """
    model = SalesEstimate
    template_name = 'sales/estimate_list.html'
    context_object_name = 'estimates'
    paginate_by = 25

    def get_queryset(self):
        qs = SalesEstimate.objects.select_related('customer', 'branch', 'cashier', 'salesperson')
        branch = getattr(self.request, 'active_branch', None)
        if branch and not self.request.user.is_superuser:
            qs = qs.filter(branch=branch)

        query = self.request.GET.get('q', '').strip()
        status_filter = self.request.GET.get('status', '').strip()
        payment_status = self.request.GET.get('payment_status', '').strip()

        if query:
            qs = qs.filter(
                Q(estimate_number__icontains=query) |
                Q(customer__name__icontains=query) |
                Q(customer_phone_manual__icontains=query) |
                Q(customer_name_manual__icontains=query) |
                Q(customer_pan__icontains=query)
            )
        if status_filter:
            qs = qs.filter(status=status_filter)
        if payment_status:
            qs = qs.filter(payment_status=payment_status)

        return qs.order_by('-created_at')


class SalesEstimateDetailView(LoginRequiredMixin, DetailView):
    """
    Renders detailed estimation invoice view (invoice_detail.html)
    with complete item breakdown, dual-IMEI records, payment transactions,
    and linked sales return vouchers.
    Supports '?format=a4' to seamlessly render the formal A4 estimation sheet.
    """
    model = SalesEstimate
    template_name = 'sales/invoice_detail.html'
    context_object_name = 'estimate'

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        if request.GET.get('format') == 'a4':
            context = self.get_context_data(object=self.object)
            return render(request, 'sales/invoice_a4_tax.html', context)
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['items'] = self.object.items.select_related('product', 'product__base_unit', 'item_instance').all()
        context['payments'] = self.object.payment_transactions.all()
        context['returns'] = self.object.returns.select_related('processed_by').prefetch_related('items__product').order_by('-created_at')
        context['config'] = SystemConfiguration.get_solo()
        return context


class SalesEstimateThermalSlipView(LoginRequiredMixin, DetailView):
    """Renders standard 80mm / 58mm POS thermal estimation slip."""
    model = SalesEstimate
    template_name = 'sales/invoice_thermal_80mm.html'
    context_object_name = 'estimate'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['items'] = self.object.items.select_related('product', 'product__base_unit').all()
        context['payments'] = self.object.payment_transactions.all()
        context['config'] = SystemConfiguration.get_solo()
        return context


# ==============================================================================
# DEDICATED ESTIMATE CANCELLATION FUNCTION & VIEW
# ==============================================================================
@login_required
@require_http_methods(["POST"])
def cancel_estimate_view(request, pk):
    """
    Dedicated view function to atomically void / cancel an erroneous SalesEstimate:
    1. Requires a mandatory cancellation justification reason.
    2. Atomically changes estimate status to 'CANCELLED'.
    3. Returns physical inventory items back to BranchStock and replenishes FIFO batches.
    4. Re-marks serialized handset ItemInstances as 'IN_STOCK' and voids active component warranties.
    5. Reverses customer debt (Udhaari) if due balance was posted and updates lifetime spend.
    6. Safely handles attached trade-in buy-back safe state and detaches delivered repair tickets.
    7. Voids original General Ledger sales journal voucher and posts reversing double-entry voucher.
    8. Records an immutable forensic event in AuditLog.
    Supports both standard HTML form submissions and asynchronous AJAX / JSON payloads.
    """
    estimate = get_object_or_404(SalesEstimate, pk=pk)

    # Permission check: superusers, owners, managers, or assigned cashier
    is_authorized = (
        request.user.is_superuser or
        getattr(request.user, 'role', '') in ['OWNER', 'MANAGER'] or
        request.user == estimate.cashier
    )
    if not is_authorized:
        err_msg = "Permission Denied: Supervisor authorization required to void sales bills."
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.content_type == 'application/json':
            return JsonResponse({'status': 'error', 'message': err_msg}, status=403)
        messages.error(request, err_msg)
        return redirect('sales:estimate_detail', pk=estimate.pk)

    # Extract cancellation reason from JSON or POST form data
    reason = ""
    if request.content_type == 'application/json':
        try:
            body_data = json.loads(request.body.decode('utf-8'))
            reason = body_data.get('reason') or body_data.get('cancellation_reason', '')
        except Exception:
            reason = ""
    else:
        reason = request.POST.get('cancellation_reason') or request.POST.get('reason', '')

    reason = str(reason or '').strip()

    # Mandatory cancellation reason check
    if not reason or len(reason) < 3:
        err_msg = "A valid, mandatory cancellation reason is required to void this bill (minimum 3 characters)."
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.content_type == 'application/json':
            return JsonResponse({'status': 'error', 'message': err_msg}, status=400)
        messages.error(request, err_msg)
        return redirect('sales:estimate_detail', pk=estimate.pk)

    # Prevent re-cancelling already voided or returned estimates
    if estimate.status in ['CANCELLED', 'RETURNED']:
        err_msg = f"Estimate slip {estimate.estimate_number} is already {estimate.get_status_display().lower()}."
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.content_type == 'application/json':
            return JsonResponse({'status': 'error', 'message': err_msg}, status=400)
        messages.warning(request, err_msg)
        return redirect('sales:estimate_detail', pk=estimate.pk)

    try:
        with transaction.atomic():
            estimate = SalesEstimate.objects.select_for_update().get(pk=pk)

            # 1. Reverse Inventory & Batch FIFO Costs / Handset Serial Status for sold line items
            for item in estimate.items.select_related('product', 'item_instance').all():
                # Re-add base unit stock back to branch
                InventoryService.adjust_stock(
                    product=item.product,
                    branch=estimate.branch,
                    quantity_delta=item.base_unit_quantity,
                    movement_type='SALE_RETURN',
                    reference_doc=f"VOID-{estimate.estimate_number}",
                    imei_or_serial=item.imei_number or "",
                    remarks=f"Bill Voided: {reason}",
                    user=request.user,
                    allow_negative=True
                )

                # Non-Serialized Accessory FIFO Batch Reversal
                if item.batch_reference and not item.item_instance:
                    batch = ProductBatch.objects.select_for_update().filter(
                        batch_number=item.batch_reference,
                        product=item.product,
                        branch=estimate.branch
                    ).first()
                    if batch:
                        batch.quantity_remaining += item.base_unit_quantity
                        batch.is_depleted = (batch.quantity_remaining <= Decimal('0.000'))
                        batch.save(update_fields=['quantity_remaining', 'is_depleted', 'updated_at'])

                # Serialized Smartphone Status & Warranty Reversal
                if item.item_instance:
                    item.item_instance.status = 'IN_STOCK'
                    item.item_instance.sold_invoice_reference = None
                    item.item_instance.customer_name = None
                    item.item_instance.customer_phone = None
                    item.item_instance.sale_date = None
                    item.item_instance.sold_price = None
                    item.item_instance.save(update_fields=[
                        'status', 'sold_invoice_reference', 'customer_name',
                        'customer_phone', 'sale_date', 'sold_price', 'updated_at'
                    ])

                    # Deactivate any active customer component warranties generated on this sale
                    DeviceComponentWarranty.objects.filter(
                        item_instance=item.item_instance,
                        status='ACTIVE'
                    ).update(
                        status='VOID',
                        void_reason=f"Bill {estimate.estimate_number} voided: {reason}",
                        updated_at=timezone.now()
                    )

            # 2. Reverse Customer Debt (Udhaari) & Lifetime Spend
            if estimate.customer or estimate.customer_id:
                customer = Customer.objects.select_for_update().filter(id=estimate.customer_id).first()
                if customer:
                    prev_bal = customer.current_credit_balance
                    due_reversed = estimate.due_amount

                    if due_reversed > Decimal('0.00'):
                        new_bal = max(Decimal('0.00'), prev_bal - due_reversed)
                        customer.current_credit_balance = new_bal

                        CustomerUdhaariLedger.objects.create(
                            customer=customer,
                            branch=estimate.branch,
                            entry_type='ADJUSTMENT',
                            amount=due_reversed,
                            previous_balance=prev_bal,
                            resulting_balance=new_bal,
                            reference_invoice=f"VOID-{estimate.estimate_number}",
                            payment_mode='OTHER',
                            remarks=f"Reversal of due amount for cancelled bill {estimate.estimate_number}: {reason}",
                            recorded_by=request.user
                        )

                    # Deduct bill grand total from customer's cumulative lifetime spend
                    customer.total_spent = max(Decimal('0.00'), customer.total_spent - estimate.grand_total)
                    customer.save(update_fields=['current_credit_balance', 'total_spent', 'updated_at'])

            # 3. Safe State Handling for Attached Trade-In Old Phone Exchange Vouchers
            trade_in_vouchers = PhoneExchangeTradeIn.objects.select_for_update().filter(
                Q(pos_estimate=estimate) | Q(voucher_number=estimate.trade_in_voucher_reference)
            )
            for voucher in trade_in_vouchers:
                restocked_instance = voucher.restocked_item_instance
                is_already_resold = False

                if restocked_instance:
                    if restocked_instance.status == 'SOLD' and restocked_instance.sold_invoice_reference != estimate.estimate_number:
                        is_already_resold = True

                if is_already_resold:
                    # Safe State Transition: Device was already resold. Preserve inventory and buyer ownership
                    AuditLog.objects.create(
                        user=request.user,
                        branch=estimate.branch,
                        action_type='UPDATE',
                        module='TradeInVoidSafeState',
                        object_repr=voucher.voucher_number,
                        ip_address=request.META.get('REMOTE_ADDR'),
                        details={
                            'notice': f"Trade-in device {voucher.brand_name} {voucher.model_name} (IMEI: {voucher.imei_1}) was already resold on bill {restocked_instance.sold_invoice_reference}. Physical stock and warranty preserved.",
                            'resold_invoice': restocked_instance.sold_invoice_reference,
                            'current_owner': restocked_instance.customer_name
                        }
                    )
                else:
                    # Device is still unsold: safe to archive restocked instance and deduct stock
                    if restocked_instance and restocked_instance.status == 'IN_STOCK':
                        restocked_instance.status = 'ARCHIVED'
                        restocked_instance.save(update_fields=['status', 'updated_at'])

                        if voucher.restocked_product:
                            InventoryService.adjust_stock(
                                product=voucher.restocked_product,
                                branch=estimate.branch,
                                quantity_delta=Decimal('-1.000'),
                                movement_type='ADJUSTMENT_SUB',
                                reference_doc=f"VOID-{estimate.estimate_number}",
                                imei_or_serial=voucher.imei_1 or "",
                                remarks=f"Trade-in buyback reversal for voided bill {estimate.estimate_number}",
                                user=request.user,
                                allow_negative=True
                            )

                # Detach voucher from this voided estimate
                voucher.status = 'VALUATED'
                voucher.pos_estimate = None
                voucher.save(update_fields=['status', 'pos_estimate', 'updated_at'])

            # 4. Revert Linked Repair Ticket (if handed over via this invoice)
            repair_ticket = RepairTicket.objects.select_for_update().filter(
                pos_invoice_reference=estimate.estimate_number
            ).first()
            if repair_ticket:
                repair_ticket.service_status = 'READY_FOR_PICKUP'
                repair_ticket.delivered_date = None
                repair_ticket.pos_invoice_reference = None
                repair_ticket.paid_amount = Decimal('0.00')
                repair_ticket.save(update_fields=['service_status', 'delivered_date', 'pos_invoice_reference', 'paid_amount', 'updated_at'])

                # Remove unfinalized technician commissions generated for this voided ticket
                TechnicianCommissionLog.objects.filter(
                    ticket=repair_ticket,
                    is_settled_in_payroll=False
                ).delete()

            # 5. Void Original Journal Voucher & Post Reversing Journal Voucher
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
                    for itm in orig_entry.items.select_related('account'):
                        reversing_lines.append({
                            'account': itm.account,
                            'debit': itm.credit_amount,
                            'credit': itm.debit_amount,
                            'customer': itm.customer,
                            'supplier': itm.supplier,
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
                            user=request.user,
                            auto_post=True
                        )
            except Exception:
                pass  # Do not block bill cancellation if accounting entries are already reconciled or non-existent

            # 6. Mark Estimate Status as CANCELLED
            estimate.status = 'CANCELLED'
            estimate.cancellation_reason = reason
            estimate.save(update_fields=['status', 'cancellation_reason', 'updated_at'])

            # 7. Forensic Audit Log
            AuditLog.objects.create(
                user=request.user,
                branch=estimate.branch,
                action_type='BILL_CANCEL',
                module='POS_Sales',
                object_repr=estimate.estimate_number,
                ip_address=request.META.get('REMOTE_ADDR'),
                details={
                    'reason': reason,
                    'grand_total': str(estimate.grand_total),
                    'subtotal': str(estimate.subtotal),
                    'paid_amount_reversed': str(estimate.paid_amount),
                    'due_amount_reversed': str(estimate.due_amount),
                    'trade_in_credit_reversed': str(estimate.trade_in_discount_amount),
                    'items_count': estimate.items.count()
                }
            )

        success_msg = f"Estimate slip {estimate.estimate_number} was successfully voided and all ledger balances, stocks, and trade-in entries were safely reversed."
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.content_type == 'application/json':
            return JsonResponse({'status': 'success', 'message': success_msg, 'estimate_number': estimate.estimate_number})

        messages.success(request, success_msg)
        return redirect('sales:estimate_detail', pk=estimate.pk)

    except Exception as err:
        err_msg = f"Error voiding bill {estimate.estimate_number}: {str(err)}"
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.content_type == 'application/json':
            return JsonResponse({'status': 'error', 'message': err_msg}, status=500)
        messages.error(request, err_msg)
        return redirect('sales:estimate_detail', pk=estimate.pk)


class SalesEstimateCancelView(LoginRequiredMixin, UserPassesTestMixin, View):
    """
    Class-based wrapper around cancel_estimate_view to maintain strict backwards compatibility
    with existing URL confs referencing SalesEstimateCancelView.as_view().
    """
    def test_func(self):
        return (
            self.request.user.is_superuser or
            getattr(self.request.user, 'role', '') in ['OWNER', 'MANAGER']
        )

    def post(self, request, pk, *args, **kwargs):
        return cancel_estimate_view(request, pk)


# ==============================================================================
# 3. SALES RETURN & ITEM RESTOCKING CONTROLLERS
# ==============================================================================
class SalesReturnListView(LoginRequiredMixin, ListView):
    """
    Lists historical customer sales return and exchange vouchers.
    Scoped by branch with multi-parameter search (voucher, bill number, customer, refund mode, dates).
    """
    model = SalesReturn
    template_name = 'sales/return_list.html'
    context_object_name = 'returns'
    paginate_by = 25

    def get_queryset(self):
        qs = SalesReturn.objects.select_related(
            'original_estimate', 'branch', 'customer', 'processed_by'
        ).prefetch_related('items__product')

        branch = getattr(self.request, 'active_branch', None)
        if branch and not self.request.user.is_superuser:
            qs = qs.filter(branch=branch)

        q = self.request.GET.get('q', '').strip()
        refund_mode = self.request.GET.get('refund_mode', '').strip()
        start_date = self.request.GET.get('start_date', '').strip()
        end_date = self.request.GET.get('end_date', '').strip()

        if q:
            qs = qs.filter(
                Q(return_number__icontains=q) |
                Q(original_estimate__estimate_number__icontains=q) |
                Q(customer__name__icontains=q) |
                Q(customer__phone_number__icontains=q) |
                Q(items__returned_imei__icontains=q) |
                Q(reason__icontains=q)
            ).distinct()

        if refund_mode:
            qs = qs.filter(refund_mode=refund_mode)

        if start_date:
            qs = qs.filter(created_at__date__gte=start_date)
        if end_date:
            qs = qs.filter(created_at__date__lte=end_date)

        return qs.order_by('-created_at')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        branch = getattr(self.request, 'active_branch', None)

        base_qs = SalesReturn.objects.all()
        if branch and not self.request.user.is_superuser:
            base_qs = base_qs.filter(branch=branch)

        total_refund_sum = base_qs.aggregate(total=Sum('total_refund_amount'))['total'] or Decimal('0.00')
        defective_returns_count = SalesReturnItem.objects.filter(sales_return__in=base_qs, is_defective=True).count()

        context.update({
            'total_refund_sum': total_refund_sum,
            'defective_returns_count': defective_returns_count,
            'total_returns_count': base_qs.count()
        })
        return context


class SalesReturnCreateView(LoginRequiredMixin, View):
    """
    Dedicated Itemized Sales Return Interface.
    Allows cashiers to select specific line items from an existing sales estimate,
    specify return quantities, toggle defective flags (to quarantine for vendor RMA claims
    instead of sellable restock), select refund method (Cash vs. Customer Store Credit),
    and execute return accounting atomically without voiding the entire multi-item bill.
    """
    template_name = 'sales/return_form.html'

    def get(self, request, estimate_id, *args, **kwargs):
        estimate = get_object_or_404(
            SalesEstimate.objects.select_related('customer', 'branch', 'cashier'),
            pk=estimate_id
        )

        if estimate.status in ['CANCELLED', 'RETURNED']:
            messages.error(
                request,
                f"Sales estimate '{estimate.estimate_number}' is already {estimate.get_status_display().lower()} and cannot accept further returns."
            )
            return redirect('sales:estimate_detail', pk=estimate.pk)

        # Build list of items and calculate previously returned quantities
        raw_items = estimate.items.select_related('product', 'product__base_unit', 'item_instance').all()
        items_with_return_state = []

        for item in raw_items:
            already_returned_qty = SalesReturnItem.objects.filter(
                sales_return__original_estimate=estimate,
                estimate_item=item
            ).aggregate(sum_qty=Sum('return_quantity'))['sum_qty'] or Decimal('0.000')

            remaining_returnable_qty = max(Decimal('0.000'), item.quantity - already_returned_qty)

            items_with_return_state.append({
                'item': item,
                'already_returned_qty': already_returned_qty,
                'remaining_returnable_qty': remaining_returnable_qty,
                'is_fully_returned': remaining_returnable_qty <= Decimal('0.000')
            })

        form = SalesReturnProcessForm()

        return render(request, self.template_name, {
            'estimate': estimate,
            'items_data': items_with_return_state,
            'form': form,
            'config': SystemConfiguration.get_solo()
        })

    def post(self, request, estimate_id, *args, **kwargs):
        estimate = get_object_or_404(
            SalesEstimate.objects.select_related('customer', 'branch'),
            pk=estimate_id
        )

        if estimate.status in ['CANCELLED', 'RETURNED']:
            messages.error(request, "This invoice is already voided or fully returned.")
            return redirect('sales:estimate_detail', pk=estimate.pk)

        form = SalesReturnProcessForm(request.POST)

        if not form.is_valid():
            messages.error(request, "Please enter a valid reason and refund mode for the return.")
            return self.get(request, estimate_id)

        reason = form.cleaned_data['reason']
        refund_mode = form.cleaned_data['refund_mode']
        technician_notes = form.cleaned_data.get('technician_notes', '')

        # Parse selected return items from POST form
        items_to_return = []
        raw_items = estimate.items.all()

        for item in raw_items:
            is_selected = request.POST.get(f'item_select_{item.id}') == '1'
            if not is_selected:
                continue

            try:
                qty_val = Decimal(str(request.POST.get(f'return_qty_{item.id}', 1)))
            except (ValueError, TypeError):
                qty_val = Decimal('1.000')

            if qty_val <= Decimal('0.000'):
                continue

            already_returned_qty = SalesReturnItem.objects.filter(
                sales_return__original_estimate=estimate,
                estimate_item=item
            ).aggregate(sum_qty=Sum('return_quantity'))['sum_qty'] or Decimal('0.000')

            remaining_qty = max(Decimal('0.000'), item.quantity - already_returned_qty)

            if qty_val > remaining_qty:
                messages.error(
                    request,
                    f"Return quantity for '{item.product.name}' ({qty_val}) exceeds remaining returnable quantity ({remaining_qty})."
                )
                return self.get(request, estimate_id)

            is_defective = request.POST.get(f'is_defective_{item.id}') == '1'
            defect_reason = request.POST.get(f'defect_reason_{item.id}', '').strip()

            items_to_return.append({
                'item_id': item.id,
                'quantity': qty_val,
                'is_defective': is_defective,
                'defect_reason': defect_reason
            })

        if not items_to_return:
            messages.error(request, "Please select at least one line item to return.")
            return self.get(request, estimate_id)

        try:
            with transaction.atomic():
                sales_return = SalesPOSService.process_sales_return(
                    original_estimate=estimate,
                    items_to_return=items_to_return,
                    refund_mode=refund_mode,
                    reason=reason,
                    technician_notes=technician_notes,
                    user=request.user
                )

                AuditLog.objects.create(
                    user=request.user,
                    branch=estimate.branch,
                    action_type='UPDATE',
                    module='SalesReturn',
                    object_repr=sales_return.return_number,
                    ip_address=request.META.get('REMOTE_ADDR'),
                    details={
                        'original_estimate': estimate.estimate_number,
                        'refund_amount': str(sales_return.total_refund_amount),
                        'refund_mode': refund_mode,
                        'items_count': len(items_to_return)
                    }
                )

            messages.success(
                request,
                f"Sales Return '{sales_return.return_number}' processed successfully! "
                f"Refund amount: Rs. {sales_return.total_refund_amount:.2f} ({sales_return.get_refund_mode_display()})."
            )
            return redirect('sales:return_detail', pk=sales_return.pk)

        except Exception as e:
            messages.error(request, f"Error processing return: {str(e)}")
            return self.get(request, estimate_id)


class SalesReturnDetailView(LoginRequiredMixin, DetailView):
    """
    Renders detailed voucher overview for a specific Sales Return.
    Shows item breakdown, restock status, defective quarantine routing,
    and customer store credit / cash refund details.
    """
    model = SalesReturn
    template_name = 'sales/return_detail.html'
    context_object_name = 'sales_return'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['items'] = self.object.items.select_related('product', 'product__base_unit', 'estimate_item').all()
        context['config'] = SystemConfiguration.get_solo()
        return context


class SalesReturnThermalSlipView(LoginRequiredMixin, DetailView):
    """
    Renders 80mm thermal receipt slip for a customer sales return / exchange voucher.
    """
    model = SalesReturn
    template_name = 'sales/return_thermal_80mm.html'
    context_object_name = 'sales_return'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['items'] = self.object.items.select_related('product', 'product__base_unit').all()
        context['config'] = SystemConfiguration.get_solo()
        return context


# ==============================================================================
# 4. TRADE-IN & BUY-BACK VIEWS
# ==============================================================================
class TradeInListView(LoginRequiredMixin, ListView):
    """Lists all historical and in-progress trade-in vouchers."""
    model = PhoneExchangeTradeIn
    template_name = 'sales/trade_in_list.html'
    context_object_name = 'trade_ins'
    paginate_by = 25

    def get_queryset(self):
        qs = PhoneExchangeTradeIn.objects.select_related('branch', 'cashier', 'pos_estimate')
        branch = getattr(self.request, 'active_branch', None)
        if branch and not self.request.user.is_superuser:
            qs = qs.filter(branch=branch)

        q = self.request.GET.get('q', '').strip()
        status_filter = self.request.GET.get('status', '').strip()

        if q:
            qs = qs.filter(
                Q(voucher_number__icontains=q) |
                Q(imei_1__icontains=q) |
                Q(customer_name_manual__icontains=q) |
                Q(customer_phone_manual__icontains=q) |
                Q(model_name__icontains=q)
            )
        if status_filter:
            qs = qs.filter(status=status_filter)

        return qs.order_by('-created_at')


class TradeInEvaluationWizardView(LoginRequiredMixin, View):
    """Full 3-step trade-in wizard for counter staff."""
    template_name = 'sales/trade_in_wizard.html'

    def get(self, request, *args, **kwargs):
        intake_form = TradeInDeviceIntakeForm()
        checklist_form = TradeIn10PointChecklistForm()
        undertaking_form = TradeInLegalUndertakingForm()
        config = SystemConfiguration.get_solo()

        return render(request, self.template_name, {
            'intake_form': intake_form,
            'checklist_form': checklist_form,
            'undertaking_form': undertaking_form,
            'config': config
        })

    def post(self, request, *args, **kwargs):
        branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()
        intake_form = TradeInDeviceIntakeForm(request.POST)
        checklist_form = TradeIn10PointChecklistForm(request.POST)
        undertaking_form = TradeInLegalUndertakingForm(request.POST, request.FILES)

        if intake_form.is_valid() and checklist_form.is_valid() and undertaking_form.is_valid():
            try:
                base_market = intake_form.cleaned_data['market_base_value']
                valuation = TradeInValuationEngine.calculate_valuation(
                    base_market_value=base_market,
                    checklist_data=checklist_form.cleaned_data
                )

                voucher_no = f"EXC-{branch.code}-{uuid.uuid4().hex[:6].upper()}"

                with transaction.atomic():
                    trade_in = intake_form.save(commit=False)
                    trade_in.voucher_number = voucher_no
                    trade_in.branch = branch
                    trade_in.cashier = request.user
                    trade_in.total_deductions = valuation['total_deductions']
                    trade_in.shop_margin_deduction = valuation['margin_deduction']
                    trade_in.final_trade_in_value = valuation['final_offer']
                    trade_in.recommended_condition_grade = valuation['condition_grade']
                    trade_in.status = 'VALUATED'
                    trade_in.save()

                    checklist = checklist_form.save(commit=False)
                    checklist.trade_in_voucher = trade_in
                    checklist.diagnostic_score_percent = valuation['score_percent']
                    checklist.save()

                    undertaking = undertaking_form.save(commit=False)
                    undertaking.trade_in_voucher = trade_in
                    undertaking.verified_by = request.user
                    undertaking.declaration_text = SystemConfiguration.get_solo().undertaking_declaration_text_np
                    undertaking.save()

                messages.success(request, f"Trade-In Voucher {trade_in.voucher_number} created with valuation Rs. {trade_in.final_trade_in_value:.2f}.")
                return redirect('sales:trade_in_detail', pk=trade_in.pk)

            except Exception as e:
                messages.error(request, f"Error processing trade-in: {str(e)}")
        else:
            messages.error(request, "Please correct the errors in the evaluation forms.")

        return render(request, self.template_name, {
            'intake_form': intake_form,
            'checklist_form': checklist_form,
            'undertaking_form': undertaking_form,
            'config': SystemConfiguration.get_solo()
        })


class TradeInDetailView(LoginRequiredMixin, DetailView):
    """Detailed summary of a trade-in buy-back transaction."""
    model = PhoneExchangeTradeIn
    template_name = 'sales/trade_in_detail.html'
    context_object_name = 'trade_in'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['checklist'] = getattr(self.object, 'inspection_checklist', None)
        context['undertaking'] = getattr(self.object, 'legal_undertaking', None)
        context['config'] = SystemConfiguration.get_solo()
        return context


class TradeInPoliceUndertakingPrintView(LoginRequiredMixin, View):
    """Renders A4 Police-Compliant Ownership Handover & Undertaking Certificate."""

    def get(self, request, pk, *args, **kwargs):
        trade_in = get_object_or_404(PhoneExchangeTradeIn, pk=pk)
        undertaking = getattr(trade_in, 'legal_undertaking', None)
        checklist = getattr(trade_in, 'inspection_checklist', None)
        config = SystemConfiguration.get_solo()

        return render(request, 'sales/trade_in_undertaking_a4.html', {
            'trade_in': trade_in,
            'undertaking': undertaking,
            'checklist': checklist,
            'config': config
        })