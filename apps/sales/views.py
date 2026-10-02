"""
Sales & POS Counter Views Module.

Core Architecture & Capabilities:
1. POSTerminalView:
   - High-speed zero-storage POS billing single page interface.
   - Enforces an active open cash drawer session before accepting transactions.
   - Supplies comprehensive template context (Bikram Sambat dates, active fiscal year, store branding, VAT status, staff permissions).
2. POSCheckoutAPIView (Security, Document Sequencing & Authoritative Discount Engine):
   - Accepts structured checkout payloads from the POS terminal.
   - Normalizes and extracts bill_type ('SALES' vs 'ESTIMATE') from incoming payloads.
   - Idempotency-Key validation preventing double-billing on network lags.
   - Multi-format backdated Nepali Bikram Sambat (B.S.) date extraction and AD calendar synchronization.
   - Validates that backdated bills fall strictly within the active, open fiscal year.
   - Enforces strict server-side validation on dual-mode line and bill discounts.
   - Standardizes error responses with machine-readable error_code across all validation failures.
   - Validates that attached repair tickets have corresponding billing items before marking as delivered.
   - Returns authoritative document titles, sequence numbers, and thermal print URLs.
3. SalesEstimateDetailView & SalesEstimateThermalSlipView:
   - Detailed invoice review, thermal 80mm/58mm printing, and formal A4 sheet views.
4. SalesEstimateUpdateView (Safe Counter Bill Corrections):
   - Provides safe counter-level metadata corrections (Dates, Customer Name, Phone, PAN, Remarks).
   - Strict Lockout Rule: CANCELLED, RETURNED, and PARTIALLY_RETURNED bills are permanently locked.
   - Explicit `get_success_url` implementation and direct form saving to prevent ImproperlyConfigured errors.
   - Atomic Save: Recalculates BS/AD dates, synchronizes linked accounting journals, item warranties,
     and customer debt records, while writing full forensic audit trails into AuditLog.
5. Sales Estimate Voiding & Cancellation:
   - cancel_estimate_view & SalesEstimateCancelView:
     * Strictly restricted to Owners, Managers, and Superusers (cashiers blocked).
     * Blocks cancellation of PARTIALLY_RETURNED bills to prevent phantom inventory duplication.
     * Fully delegates atomic reversal (merchandise stock, Udhaari debt, trade-in vouchers,
       repair tickets, and General Ledger journal vouchers) to SalesPOSService.cancel_sales_estimate.
     * Enhanced redirection support: Redirects back to list view or detail view cleanly with clear audit alerts.
6. Itemized Sales Returns:
   - SalesReturnListView, SalesReturnCreateView, SalesReturnDetailView, SalesReturnThermalSlipView.
   - Enforces active cash drawer shift when issuing CASH refunds to prevent drawer discrepancies.
7. Trade-In & Buy-Back Vouchers:
   - TradeInListView, TradeInEvaluationWizardView, TradeInDetailView, TradeInPoliceUndertakingPrintView.
   - Bidirectional customer identification fallback between Step 1 intake and Step 3 KYC undertaking.
   - Guarantees SYS_CONFIG injection for statutory police anti-theft undertaking documents.
"""

import re
import json
import uuid
import logging
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from datetime import date, datetime, timedelta
from typing import Any, Optional, Dict, List, Tuple

from django.shortcuts import render, redirect, get_object_or_404
from django.views.generic import TemplateView, ListView, DetailView, UpdateView, View
from django.views.decorators.http import require_http_methods
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib import messages
from django.http import JsonResponse, HttpResponseRedirect
from django.db.models import Q, Sum
from django.db import transaction
from django.utils import timezone
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.urls import reverse, reverse_lazy

from apps.sales.models import (
    SalesEstimate, SalesEstimateItem, SalesPaymentTransaction, SalesReturn, SalesReturnItem,
    PhoneExchangeTradeIn, TradeInInspectionChecklist, TradeInLegalUndertaking
)
from apps.sales.forms import (
    SalesEstimateEditForm, TradeInDeviceIntakeForm, TradeIn10PointChecklistForm,
    TradeInLegalUndertakingForm, SalesReturnProcessForm
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
from apps.pos.services import POSSessionService
from apps.core.models import SystemConfiguration, AuditLog
from apps.core.nepali_calendar import NepaliCalendar
from apps.core.utils.nepali_date_converter import parse_bs_date_components
from apps.accounting.models import AccountingFiscalYear
from apps.users.models import User

logger = logging.getLogger(__name__)

# Standard operational reasons for voiding sales bills
VOID_REASON_CHOICES = [
    ('CASHIER_ENTRY_MISTAKE', 'Cashier Entry Mistake / Typo (काउन्टरमा इन्ट्री गल्ती)'),
    ('CUSTOMER_CHANGED_MIND', 'Customer Changed Mind Before Handover (ग्राहकले सामान लिन नचाहेको)'),
    ('DUPLICATE_BILLING', 'Duplicate Bill Generated (दोहोरो बिल बनेको)'),
    ('INCORRECT_PRICING_DISCOUNT', 'Incorrect Pricing or Discount Applied (मूल्य वा छुट गलत भएको)'),
    ('PAYMENT_METHOD_DISCREPANCY', 'Payment Method Discrepancy (भुक्तानी माध्यम त्रुटि)'),
    ('OTHER', 'Other Operational Reason (अन्य कारण खुलाउनुहोस्)'),
]

# ==============================================================================
# 1. POS COUNTER TERMINAL & CHECKOUT VIEWS
# ==============================================================================
class POSTerminalView(LoginRequiredMixin, TemplateView):
    """
    Primary POS Counter Billing Single Page Interface (Zero-Storage Fast Interface).
    Enforces active open cash drawer session and loads active categories,
    registered customer profiles, and comprehensive calendar/tax context.
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

        # Derive active Bikram Sambat (BS) date components
        today_ad = timezone.now().date()
        try:
            bs_y, bs_m, bs_d = NepaliCalendar.ad_to_bs(today_ad)
            current_bs_en = NepaliCalendar.format_bs(bs_y, bs_m, bs_d, lang='en')
            current_bs_np = NepaliCalendar.format_bs(bs_y, bs_m, bs_d, lang='ne')
            current_fy = NepaliCalendar.get_fiscal_year(bs_y, bs_m)
        except Exception:
            current_bs_en = "2081-01-01"
            current_bs_np = "२०८१-०१-०१"
            current_fy = "2083/84"

        # Dynamically resolve active open fiscal year from database registry
        active_fy_obj = AccountingFiscalYear.objects.filter(is_closed=False).order_by('-start_date_ad').first()
        active_fiscal_year = active_fy_obj.name if active_fy_obj else current_fy

        # Staff role permissions
        user = self.request.user
        user_role = getattr(user, 'role', '')
        is_owner = bool(user_role == 'OWNER' or user.is_superuser)
        is_manager = bool(user_role == 'MANAGER' or is_owner)

        # Tax configuration
        is_vat_mode = (config.tax_system_mode == 'VAT')

        # Load active categories and registered customers
        categories = ProductCategory.objects.filter(is_active=True).order_by('name')
        customers = Customer.objects.filter(is_active=True).order_by('name')[:100]

        context.update({
            'config': config,
            'SYS_CONFIG': config,
            'active_branch': branch,
            'active_session': POSSessionService.get_active_session(user, branch),
            'categories': categories,
            'customers': customers,
            'is_estimate_only': config.is_estimation_bill_only,
            'estimate_disclaimer': config.bill_estimate_disclaimer,
            'SHOP_TAX_MODE': config.tax_system_mode,
            'DEFAULT_TAX_RATE': str(config.default_vat_rate) if is_vat_mode else '0.00',
            'IS_VAT_MODE': is_vat_mode,
            'CURRENT_BS_DATE_EN': current_bs_en,
            'CURRENT_BS_DATE_NP': current_bs_np,
            'active_fiscal_year': active_fiscal_year,
            'STORE_OUTLET_NAME': branch.name if branch else "",
            'STORE_OUTLET_CODE': branch.code if branch else "",
            'BILL_HEADER_TITLE': config.bill_header_title,
            'ESTIMATE_DISCLAIMER': config.bill_estimate_disclaimer,
            'ENABLE_MDMS_TRACKING': getattr(config, 'enable_mdms_tracking', True),
            'IS_OWNER': is_owner,
            'IS_MANAGER': is_manager,
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
            return JsonResponse({
                'status': 'error',
                'error_code': 'NO_ACTIVE_BRANCH',
                'message': 'No active branch selected.'
            }, status=400)

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
                        'error_code': 'TRANSACTION_IN_PROGRESS',
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

            # 3. Extract and Authoritatively Standardize Bill Type
            raw_bill_type = str(payload.get('bill_type') or 'SALES').upper().strip()
            bill_type = 'ESTIMATE' if raw_bill_type in ['ESTIMATE', 'EST'] else 'SALES'

            # 4. Multi-Format Backdated B.S. Date Extraction & Active Fiscal Year Verification
            raw_bill_date_bs = str(payload.get('bill_date_bs') or '').strip()
            target_date_ad: Optional[date] = None
            target_date_bs: Optional[str] = None
            target_fiscal_year: Optional[str] = None

            if raw_bill_date_bs:
                try:
                    bs_y, bs_m, bs_d = parse_bs_date_components(raw_bill_date_bs)
                    target_date_ad = NepaliCalendar.bs_to_ad(bs_y, bs_m, bs_d)
                    target_date_bs = f"{bs_y:04d}-{bs_m:02d}-{bs_d:02d}"
                    target_fiscal_year = NepaliCalendar.get_fiscal_year(bs_y, bs_m)

                    locked_fy = AccountingFiscalYear.objects.filter(name=target_fiscal_year, is_closed=True).first()
                    if locked_fy:
                        active_open_fy = AccountingFiscalYear.objects.filter(is_closed=False).order_by('-start_date_ad').first()
                        open_fy_name = active_open_fy.name if active_open_fy else "the current active fiscal year"
                        if idempotency_key and cache_key:
                            cache.delete(f"lock_{cache_key}")
                        return JsonResponse({
                            'status': 'error',
                            'error_code': 'CLOSED_FISCAL_YEAR',
                            'message': (
                                f"Financial posting rejected: Selected date ({target_date_bs}) belongs to Fiscal Year {target_fiscal_year}, "
                                f"which is audited and locked. Only transactions within {open_fy_name} are permitted."
                            )
                        }, status=400)

                except ValueError as ve:
                    logger.warning(f"Invalid B.S. date input '{raw_bill_date_bs}': {ve}")
                    if idempotency_key and cache_key:
                        cache.delete(f"lock_{cache_key}")
                    return JsonResponse({
                        'status': 'error',
                        'error_code': 'INVALID_BILL_DATE',
                        'message': f"Invalid Bikram Sambat date format: '{raw_bill_date_bs}'. Expected YYYY-MM-DD or YYYY.MM.DD."
                    }, status=400)
                except Exception as ex:
                    logger.error(f"Failed to synchronize custom B.S. date '{raw_bill_date_bs}': {ex}", exc_info=True)
                    if idempotency_key and cache_key:
                        cache.delete(f"lock_{cache_key}")
                    return JsonResponse({
                        'status': 'error',
                        'error_code': 'DATE_CONVERSION_ERROR',
                        'message': f"Could not convert Nepali date '{raw_bill_date_bs}' to Gregorian calendar."
                    }, status=400)

            if not cart:
                if idempotency_key and cache_key:
                    cache.delete(f"lock_{cache_key}")
                return JsonResponse({
                    'status': 'error',
                    'error_code': 'EMPTY_CART',
                    'message': 'Cart is empty. Please add items.'
                }, status=400)

            # 5. Customer Profile & Tier Resolution
            customer_type = 'RETAIL'
            if customer_id:
                cust_record = Customer.objects.filter(id=customer_id, is_active=True).first()
                if cust_record:
                    customer_type = cust_record.customer_type

            # 6. Structured Server-Side Discount Parsing & Authoritative Calculation
            threshold = config.require_manager_approval_discount or Decimal('10.00')
            requires_manager_pin = False
            override_reasons = []

            running_subtotal = Decimal('0.00')
            running_item_discount_total = Decimal('0.00')
            discountable_net_subtotal = Decimal('0.00')

            for item in cart:
                prod_id = item.get('product_id')
                if not prod_id:
                    continue

                try:
                    product_obj = Product.objects.get(id=prod_id)
                except Product.DoesNotExist:
                    if idempotency_key and cache_key:
                        cache.delete(f"lock_{cache_key}")
                    return JsonResponse({
                        'status': 'error',
                        'error_code': 'PRODUCT_NOT_FOUND',
                        'message': f"Product ID {prod_id} not found in catalog."
                    }, status=400)

                try:
                    qty = Decimal(str(item.get('quantity', 1))).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)
                except (InvalidOperation, ValueError, TypeError):
                    qty = Decimal('1.000')

                if qty <= Decimal('0.000'):
                    if idempotency_key and cache_key:
                        cache.delete(f"lock_{cache_key}")
                    return JsonResponse({
                        'status': 'error',
                        'error_code': 'INVALID_QUANTITY',
                        'message': f"Invalid quantity for '{product_obj.name}'."
                    }, status=400)

                pkg_conversion_id = item.get('unit_conversion_id') or item.get('conversion_id')
                factor = Decimal('1.000')
                unit_conv = None
                if pkg_conversion_id:
                    unit_conv = UnitConversion.objects.filter(id=pkg_conversion_id, product=product_obj).first()
                    if unit_conv:
                        factor = unit_conv.conversion_factor

                base_official = ProductCatalogService.get_applicable_price(product_obj, qty * factor, customer_type)
                if unit_conv and unit_conv.selling_price_per_unit:
                    official_price = unit_conv.selling_price_per_unit
                else:
                    official_price = base_official * factor
                official_price = official_price.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

                raw_unit_price = item.get('unit_price') or item.get('price') or item.get('selling_price')
                try:
                    submitted_price = Decimal(str(raw_unit_price if raw_unit_price is not None else official_price)).quantize(
                        Decimal('0.01'), rounding=ROUND_HALF_UP
                    )
                except (InvalidOperation, ValueError, TypeError):
                    submitted_price = official_price

                if submitted_price < Decimal('0.00'):
                    if idempotency_key and cache_key:
                        cache.delete(f"lock_{cache_key}")
                    return JsonResponse({
                        'status': 'error',
                        'error_code': 'NEGATIVE_PRICE',
                        'message': f"Unit price for '{product_obj.name}' cannot be negative."
                    }, status=400)

                line_official_gross = (qty * official_price).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                line_submitted_gross = (qty * submitted_price).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

                raw_item_disc_type = self._normalize_discount_type(item.get('discount_type'))
                raw_item_disc_val = item.get('discount_input_value') or item.get('discount_value') or item.get('discount_percent', 0)

                try:
                    item_disc_val = Decimal(str(raw_item_disc_val if raw_item_disc_val is not None else 0)).quantize(
                        Decimal('0.01'), rounding=ROUND_HALF_UP
                    )
                except (InvalidOperation, ValueError, TypeError):
                    item_disc_val = Decimal('0.00')

                if item_disc_val < Decimal('0.00'):
                    if idempotency_key and cache_key:
                        cache.delete(f"lock_{cache_key}")
                    return JsonResponse({
                        'status': 'error',
                        'error_code': 'NEGATIVE_DISCOUNT',
                        'message': f"Discount for '{product_obj.name}' cannot be negative."
                    }, status=400)

                is_item_discountable = getattr(product_obj, 'is_discountable', True)
                if not is_item_discountable and item_disc_val > Decimal('0.00'):
                    if idempotency_key and cache_key:
                        cache.delete(f"lock_{cache_key}")
                    return JsonResponse({
                        'status': 'error',
                        'error_code': 'NON_DISCOUNTABLE_ITEM',
                        'message': f"Product '{product_obj.name}' is designated as non-discountable."
                    }, status=400)

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
                            'error_code': 'DISCOUNT_EXCEEDS_GROSS',
                            'message': f"Discount amount of Rs. {item_disc_val:.2f} on '{product_obj.name}' cannot exceed line gross value."
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
                            'error_code': 'INVALID_DISCOUNT_PERCENT',
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

                item['discount_type'] = raw_item_disc_type
                item['discount_value'] = item_disc_val
                item['discount_input_value'] = item_disc_val
                item['discount_percent'] = effective_item_disc_pct
                item['unit_price'] = submitted_price

                total_concession = max(Decimal('0.00'), line_official_gross - line_submitted_net)
                effective_commercial_pct = (
                    (total_concession / line_official_gross) * Decimal('100.00')
                ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) if line_official_gross > Decimal('0.00') else Decimal('0.00')

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

            # Bill-Level Discount Parsing
            raw_bill_disc_type = self._normalize_discount_type(
                payload.get('bill_discount_type') or payload.get('discount_type')
            )
            raw_bill_disc_val = payload.get('bill_discount_input_value') or payload.get('bill_discount_value') or payload.get('discount_value') or payload.get('discount_percent', Decimal('0.00'))

            try:
                bill_discount_input_value = Decimal(str(raw_bill_disc_val if raw_bill_disc_val is not None else 0)).quantize(
                    Decimal('0.01'), rounding=ROUND_HALF_UP
                )
            except (InvalidOperation, ValueError, TypeError):
                bill_discount_input_value = Decimal('0.00')

            if bill_discount_input_value < Decimal('0.00'):
                if idempotency_key and cache_key:
                    cache.delete(f"lock_{cache_key}")
                return JsonResponse({
                    'status': 'error',
                    'error_code': 'NEGATIVE_BILL_DISCOUNT',
                    'message': 'Bill discount cannot be negative.'
                }, status=400)

            discount_reason = str(payload.get('discount_reason', '') or payload.get('reason', '')).strip()

            if bill_discount_input_value > Decimal('0.00') and discountable_net_subtotal <= Decimal('0.00'):
                if idempotency_key and cache_key:
                    cache.delete(f"lock_{cache_key}")
                return JsonResponse({
                    'status': 'error',
                    'error_code': 'NO_DISCOUNTABLE_MERCHANDISE',
                    'message': 'Bill discount cannot be applied because there are no discountable items in the cart.'
                }, status=400)

            if raw_bill_disc_type == 'AMOUNT':
                if bill_discount_input_value > discountable_net_subtotal:
                    if idempotency_key and cache_key:
                        cache.delete(f"lock_{cache_key}")
                    return JsonResponse({
                        'status': 'error',
                        'error_code': 'BILL_DISCOUNT_EXCEEDS_SUBTOTAL',
                        'message': f"Bill discount amount of Rs. {bill_discount_input_value:.2f} cannot exceed the discountable subtotal."
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
                    return JsonResponse({
                        'status': 'error',
                        'error_code': 'INVALID_BILL_DISCOUNT_PERCENT',
                        'message': 'Bill discount percentage cannot exceed 100.00%.'
                    }, status=400)
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
                    f"Bill discount of {effective_bill_disc_pct:.2f}% exceeds store manager threshold ({threshold:.2f}%)"
                )

            # Authenticate Manager PIN if required
            is_cashier_privileged = bool(request.user.is_superuser or getattr(request.user, 'role', '') in ['OWNER', 'MANAGER'])
            manager_override_user = None

            if requires_manager_pin:
                if is_cashier_privileged:
                    manager_override_user = request.user
                    if not discount_reason:
                        discount_reason = "Management Approved Concession"
                else:
                    if not manager_pin:
                        if idempotency_key and cache_key:
                            cache.delete(f"lock_{cache_key}")
                        return JsonResponse({
                            'status': 'error',
                            'error_code': 'MANAGER_PIN_REQUIRED',
                            'requires_manager_pin': True,
                            'override_reasons': override_reasons,
                            'message': f"Manager PIN authorization required: {'; '.join(override_reasons)}"
                        }, status=403)

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
                            'error_code': 'INVALID_MANAGER_PIN',
                            'requires_manager_pin': True,
                            'message': 'Invalid Manager Override PIN. Price override rejected.'
                        }, status=403)

            # Validate Linked Repair Ticket Handover
            if repair_ticket_id:
                repair_ticket = RepairTicket.objects.select_for_update().filter(id=repair_ticket_id).first()
                if not repair_ticket:
                    if idempotency_key and cache_key:
                        cache.delete(f"lock_{cache_key}")
                    return JsonResponse({
                        'status': 'error',
                        'error_code': 'REPAIR_NOT_FOUND',
                        'message': f"Repair Ticket ID {repair_ticket_id} was not found."
                    }, status=400)

                if repair_ticket.service_status == 'DELIVERED':
                    if idempotency_key and cache_key:
                        cache.delete(f"lock_{cache_key}")
                    return JsonResponse({
                        'status': 'error',
                        'error_code': 'REPAIR_ALREADY_DELIVERED',
                        'message': f"Repair Ticket {repair_ticket.ticket_number} has already been marked as delivered."
                    }, status=400)

                is_free_warranty = (repair_ticket.claim_type == 'FREE_WARRANTY')
                repair_fee = repair_ticket.final_total_amount

                if not is_free_warranty and repair_fee > Decimal('0.00'):
                    has_repair_billing_item = any(
                        item.get('repair_ticket_id') == repair_ticket.id or
                        item.get('is_repair_service') is True or
                        'REPAIR' in str(item.get('sku', '')).upper() or
                        'SERVICE' in str(item.get('sku', '')).upper()
                        for item in cart
                    )

                    if not has_repair_billing_item:
                        if idempotency_key and cache_key:
                            cache.delete(f"lock_{cache_key}")
                        return JsonResponse({
                            'status': 'error',
                            'error_code': 'REPAIR_FEE_NOT_IN_CART',
                            'message': f"Repair Ticket {repair_ticket.ticket_number} has a fee of Rs. {repair_fee:.2f}. Add service to cart."
                        }, status=400)

            # Process Checkout
            with transaction.atomic():
                estimate = SalesPOSService.process_checkout(
                    branch=branch,
                    cashier=request.user,
                    cart_items=cart,
                    payments=payments,
                    bill_type=bill_type,
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
                    notes=notes,
                    bill_date_ad=target_date_ad,
                    bill_date_bs=target_date_bs,
                    fiscal_year=target_fiscal_year
                )

                if repair_ticket_id:
                    repair_ticket = RepairTicket.objects.select_for_update().filter(id=repair_ticket_id).first()
                    repair_ticket.service_status = 'DELIVERED'
                    repair_ticket.delivered_date = timezone.now()
                    repair_ticket.pos_invoice_reference = estimate.estimate_number
                    repair_ticket.paid_amount = repair_ticket.final_total_amount
                    repair_ticket.save(update_fields=['service_status', 'delivered_date', 'pos_invoice_reference', 'paid_amount', 'updated_at'])

                    if repair_ticket.technician and repair_ticket.labor_charge > Decimal('0.00'):
                        technician_user = repair_ticket.technician
                        commission_rate = getattr(technician_user, 'commission_percentage', None) or Decimal('30.00')
                        TechnicianCommissionLog.objects.create(
                            ticket=repair_ticket,
                            technician=technician_user,
                            branch=branch,
                            labor_amount_collected=repair_ticket.labor_charge,
                            commission_percentage=commission_rate
                        )

            bill_title = "Tax Invoice / Sales Bill" if bill_type == 'SALES' else (config.bill_header_title or "Estimation Slip")

            response_data = {
                'status': 'success',
                'estimate_id': estimate.id,
                'estimate_number': estimate.estimate_number,
                'bill_type': bill_type,
                'bill_title': bill_title,
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
            return JsonResponse({'status': 'error', 'error_code': 'VALIDATION_ERROR', 'message': msg}, status=400)
        except Exception as err:
            if idempotency_key and cache_key:
                cache.delete(f"lock_{cache_key}")
            return JsonResponse({'status': 'error', 'error_code': 'INTERNAL_ERROR', 'message': str(err)}, status=400)

# ==============================================================================
# 2. SALES ESTIMATE INVOICE VIEWS & SAFE COUNTER CORRECTIONS
# ==============================================================================
class SalesEstimateListView(LoginRequiredMixin, ListView):
    """
    Lists historical estimation slips and POS invoices with pagination,
    branch scoping, and multi-parameter search (slip no, customer, phone, PAN).
    Injects cancellation reasons and user permissions so that the cancellation
    confirmation modal can operate directly from this master table.
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
        bill_type_filter = self.request.GET.get('bill_type', '').strip()

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
        if bill_type_filter:
            qs = qs.filter(bill_type=bill_type_filter)

        return qs.order_by('-created_at')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        is_supervisor = bool(user.is_superuser or getattr(user, 'role', '') in ['OWNER', 'MANAGER'])

        context.update({
            'is_supervisor': is_supervisor,
            'void_reason_choices': VOID_REASON_CHOICES,
            'SYS_CONFIG': SystemConfiguration.get_solo(),
            'config': SystemConfiguration.get_solo(),
        })
        return context

class SalesEstimateDetailView(LoginRequiredMixin, DetailView):
    """
    Renders detailed estimation invoice view (invoice_detail.html).
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
        user = self.request.user
        is_supervisor = bool(user.is_superuser or getattr(user, 'role', '') in ['OWNER', 'MANAGER'])

        context['items'] = self.object.items.select_related('product', 'product__base_unit', 'item_instance').all()
        context['payments'] = self.object.payment_transactions.all()
        context['returns'] = self.object.returns.select_related('processed_by').prefetch_related('items__product').order_by('-created_at')
        context['config'] = SystemConfiguration.get_solo()
        context['SYS_CONFIG'] = SystemConfiguration.get_solo()
        context['is_supervisor'] = is_supervisor
        context['void_reason_choices'] = VOID_REASON_CHOICES
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
        context['SYS_CONFIG'] = SystemConfiguration.get_solo()
        return context

class SalesEstimateUpdateView(LoginRequiredMixin, UserPassesTestMixin, UpdateView):
    """
    Counter interface for correcting bill mistakes (e.g. wrong dates, typos in customer name/phone/PAN).

    Security & Forensic Safeguards:
    1. Only Owners, Managers, or the billing Cashier can access this view.
    2. CANCELLED, RETURNED, and PARTIALLY_RETURNED bills are permanently locked.
    3. Prevents modifying financial amounts (monetary quantities, tax, COGS) to protect ledger integrity.
    4. Automatically records an immutable entry in AuditLog detailing changes.
    5. Explicit get_success_url and self.object = form.save() completely resolves ImproperlyConfigured errors.
    """
    model = SalesEstimate
    form_class = SalesEstimateEditForm
    template_name = 'sales/estimate_form.html'
    context_object_name = 'estimate'

    def test_func(self):
        user = self.request.user
        if not user.is_authenticated:
            return False
        if user.is_superuser or getattr(user, 'role', '') in ['OWNER', 'MANAGER']:
            return True
        obj = self.get_object()
        return obj.cashier == user

    def handle_no_permission(self):
        messages.error(
            self.request,
            "Permission Denied: Only Store Owners, Managers, or the billing cashier can edit this bill."
        )
        return redirect('sales:estimate_list')

    def dispatch(self, request, *args, **kwargs):
        self.object = self.get_object()
        if not self.object.can_be_edited:
            messages.error(
                request,
                f"Security Lockout: Bill '{self.object.estimate_number}' is {self.object.get_status_display().lower()} "
                f"and legally locked from counter editing."
            )
            return redirect('sales:estimate_detail', pk=self.object.pk)
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self) -> str:
        """Explicitly defines redirect destination upon successful bill correction."""
        return reverse('sales:estimate_detail', kwargs={'pk': self.object.pk})

    def form_valid(self, form):
        orig_obj = self.get_object()
        old_date_bs = orig_obj.bill_date_bs
        old_date_ad = orig_obj.bill_date_ad
        old_customer = orig_obj.recipient_display_name
        old_phone = orig_obj.customer_phone_manual
        old_pan = orig_obj.customer_pan

        with transaction.atomic():
            # 1. Commit updated metadata to database & execute model save hooks
            self.object = form.save()

            # 2. Update customer udhaari ledger remarks if debt exists
            if self.object.customer_id and self.object.due_amount > Decimal('0.00'):
                try:
                    CustomerUdhaariLedger.objects.filter(
                        reference_invoice=self.object.estimate_number
                    ).update(
                        remarks=(
                            f"Bill Total: Rs. {self.object.grand_total:,.2f} | "
                            f"Recipient: {self.object.recipient_display_name} | "
                            f"Balance Due: Rs. {self.object.due_amount:,.2f} on {self.object.estimate_number} "
                            f"({self.object.bill_date_bs or self.object.bill_date_ad})"
                        )
                    )
                except Exception as e:
                    logger.warning(f"Could not update ledger remarks on bill edit: {e}")

            # 3. Forensic Audit Trail Logging
            changed_fields = list(form.changed_data)
            diff_summary = {
                'estimate_number': self.object.estimate_number,
                'changed_fields': changed_fields,
                'old_values': {
                    'bill_date_bs': old_date_bs,
                    'bill_date_ad': str(old_date_ad) if old_date_ad else None,
                    'customer_name': old_customer,
                    'customer_phone': old_phone,
                    'customer_pan': old_pan
                },
                'new_values': {
                    'bill_date_bs': self.object.bill_date_bs,
                    'bill_date_ad': str(self.object.bill_date_ad) if self.object.bill_date_ad else None,
                    'customer_name': self.object.recipient_display_name,
                    'customer_phone': self.object.customer_phone_manual,
                    'customer_pan': self.object.customer_pan
                }
            }

            AuditLog.objects.create(
                user=self.request.user,
                branch=self.object.branch,
                action_type='UPDATE',
                module='POS_Sales_Edit',
                object_repr=self.object.estimate_number,
                details=diff_summary
            )

        messages.success(
            self.request,
            f"Bill '{self.object.estimate_number}' details were updated successfully."
        )
        return redirect(self.get_success_url())

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update({
            'SYS_CONFIG': SystemConfiguration.get_solo(),
            'config': SystemConfiguration.get_solo(),
            'is_manager': bool(self.request.user.is_superuser or getattr(self.request.user, 'role', '') in ['OWNER', 'MANAGER']),
        })
        return context

# ==============================================================================
# DEDICATED ESTIMATE CANCELLATION CONTROLLER (RESTRICTED TO SUPERVISORS)
# ==============================================================================
@login_required
@require_http_methods(["POST"])
def cancel_estimate_view(request, pk):
    """
    Dedicated view function to atomically void / cancel a SalesEstimate:
    1. Strictly restricted to Superusers, Owners, or Managers (regular cashiers blocked).
    2. Strictly blocks cancellation if invoice is already CANCELLED, RETURNED, or PARTIALLY_RETURNED.
    3. Mandates a formal cancellation justification reason (min 5 characters).
    4. Authoritatively delegates full atomic reversal (merchandise stock, Udhaari debt,
       repair ticket restoration, trade-in buy-back rollback, and GL vouchers) directly
       to SalesPOSService.cancel_sales_estimate.
    5. Clean redirect handling: returns to estimate list or bill detail view with green audit alerts.
    """
    estimate = get_object_or_404(SalesEstimate, pk=pk)

    is_authorized = bool(
        request.user.is_superuser or
        getattr(request.user, 'role', '') in ['OWNER', 'MANAGER']
    )
    if not is_authorized:
        err_msg = "Permission Denied: Supervisor authorization (Owner/Manager) is strictly required to void sales bills."
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.content_type == 'application/json':
            return JsonResponse({'status': 'error', 'error_code': 'PERMISSION_DENIED', 'message': err_msg}, status=403)
        messages.error(request, err_msg)
        return redirect('sales:estimate_detail', pk=estimate.pk)

    # Extract cancellation reason
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

    if not reason or len(reason) < 5:
        err_msg = "A valid, mandatory cancellation reason is required to void this bill (minimum 5 characters)."
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.content_type == 'application/json':
            return JsonResponse({'status': 'error', 'error_code': 'REASON_TOO_SHORT', 'message': err_msg}, status=400)
        messages.error(request, err_msg)
        return redirect('sales:estimate_detail', pk=estimate.pk)

    # Block cancellation of already cancelled or returned invoices
    if estimate.status in ['CANCELLED', 'RETURNED']:
        err_msg = f"Bill {estimate.estimate_number} is already {estimate.get_status_display().lower()}."
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.content_type == 'application/json':
            return JsonResponse({'status': 'error', 'error_code': 'ALREADY_CLOSED', 'message': err_msg}, status=400)
        messages.warning(request, err_msg)
        return redirect('sales:estimate_detail', pk=estimate.pk)

    if estimate.status == 'PARTIALLY_RETURNED':
        err_msg = (
            f"Bill {estimate.estimate_number} has already had items returned and restocked. "
            f"To prevent duplicate stock inflation, partially returned bills cannot be voided. "
            f"Please process a sales return for the remaining items instead."
        )
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.content_type == 'application/json':
            return JsonResponse({'status': 'error', 'error_code': 'PARTIALLY_RETURNED', 'message': err_msg}, status=400)
        messages.error(request, err_msg)
        return redirect('sales:estimate_detail', pk=estimate.pk)

    try:
        cancelled_estimate = SalesPOSService.cancel_sales_estimate(
            estimate=estimate,
            reason=reason,
            user=request.user
        )

        success_msg = (
            f"Bill '{cancelled_estimate.estimate_number}' was successfully voided. "
            f"Sold items and IMEIs have been returned to active stock, customer debt has been reversed, "
            f"and accounting ledgers have been balanced."
        )

        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.content_type == 'application/json':
            return JsonResponse({
                'status': 'success',
                'message': success_msg,
                'estimate_number': cancelled_estimate.estimate_number
            })

        messages.success(request, success_msg)

        # Determine smart redirect destination
        redirect_target = request.POST.get('next') or request.GET.get('next')
        if redirect_target == 'list':
            return redirect('sales:estimate_list')
        return redirect('sales:estimate_detail', pk=cancelled_estimate.pk)

    except Exception as err:
        err_msg = f"Error voiding bill {estimate.estimate_number}: {str(err)}"
        logger.error(f"[Bill Voiding Error] {err_msg}", exc_info=True)
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.content_type == 'application/json':
            return JsonResponse({'status': 'error', 'error_code': 'CANCEL_ERROR', 'message': err_msg}, status=500)
        messages.error(request, err_msg)
        return redirect('sales:estimate_detail', pk=estimate.pk)

class SalesEstimateCancelView(LoginRequiredMixin, UserPassesTestMixin, View):
    """Class-based wrapper around cancel_estimate_view for routing compatibility."""
    def test_func(self):
        return bool(
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
    Enforces active open cash drawer session when issuing cash refunds.
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
                f"Sales bill '{estimate.estimate_number}' is already {estimate.get_status_display().lower()} and cannot accept returns."
            )
            return redirect('sales:estimate_detail', pk=estimate.pk)

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
            'config': SystemConfiguration.get_solo(),
            'SYS_CONFIG': SystemConfiguration.get_solo()
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

        if refund_mode == 'CASH':
            active_session = POSSessionService.get_active_session(request.user, estimate.branch)
            if not active_session:
                messages.error(
                    request,
                    "Cash refund requires an active open cash drawer session. Please open your cash shift before issuing cash payouts."
                )
                return self.get(request, estimate_id)

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
    """
    model = SalesReturn
    template_name = 'sales/return_detail.html'
    context_object_name = 'sales_return'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['items'] = self.object.items.select_related('product', 'product__base_unit', 'estimate_item').all()
        context['config'] = SystemConfiguration.get_solo()
        context['SYS_CONFIG'] = SystemConfiguration.get_solo()
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
        context['items'] = self.object.items.select_related('product', 'product__base_unit', 'estimate_item').all()
        context['config'] = SystemConfiguration.get_solo()
        context['SYS_CONFIG'] = SystemConfiguration.get_solo()
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
    """
    Full 3-step trade-in wizard for counter staff.
    """
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
            'config': config,
            'SYS_CONFIG': config
        })

    def post(self, request, *args, **kwargs):
        branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()

        post_data = request.POST.copy()
        cust_name_manual = post_data.get('customer_name_manual', '').strip()
        cust_phone_manual = post_data.get('customer_phone_manual', '').strip()
        cust_full_name_undertaking = post_data.get('customer_full_name', '').strip()

        if not cust_name_manual and cust_full_name_undertaking:
            post_data['customer_name_manual'] = cust_full_name_undertaking
        elif not cust_full_name_undertaking and cust_name_manual:
            post_data['customer_full_name'] = cust_name_manual

        if not cust_phone_manual:
            alt_phone = post_data.get('customer_phone', '').strip() or post_data.get('phone_number', '').strip()
            if alt_phone:
                post_data['customer_phone_manual'] = alt_phone

        intake_form = TradeInDeviceIntakeForm(post_data)
        checklist_form = TradeIn10PointChecklistForm(post_data)
        undertaking_form = TradeInLegalUndertakingForm(post_data, request.FILES)

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
            'config': SystemConfiguration.get_solo(),
            'SYS_CONFIG': SystemConfiguration.get_solo()
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
        context['SYS_CONFIG'] = SystemConfiguration.get_solo()
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
            'config': config,
            'SYS_CONFIG': config
        })