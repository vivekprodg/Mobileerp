import uuid
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status, permissions

from apps.pos.models import POSHoldCart
from apps.pos.api.serializers import POSHoldCartSerializer
from apps.customers.models import Customer
from apps.core.models import SystemConfiguration

class HoldCartListCreateAPIView(APIView):
    """
    API endpoint to list and suspend (park) active customer carts with complete preservation of:
    - Explicit item discount types: AMOUNT (Rs.), PERCENTAGE (%), and NONE
    - Item discount input values, calculated deductions, and effective percentages
    - Multi-quantity lines with per-unit discount integrity
    - Bill-level discount types (AMOUNT vs. PERCENTAGE) and values
    - Catalog rates, submitted unit prices, and dual-IMEI phone identifiers
    - Preserved operational linkages: repair_ticket_id, trade_in_voucher_id, and customer_pan
    - Master Switch Compliance: Allows parking backlog phones without IMEI when enforce_imei_tracking=False
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        branch = getattr(request, 'active_branch', None)
        qs = POSHoldCart.objects.select_related('customer', 'branch', 'cashier').all()
        if branch and not request.user.is_superuser:
            qs = qs.filter(branch=branch)
        serializer = POSHoldCartSerializer(qs, many=True)
        data = serializer.data

        # Guarantee that customer_pan, trade_in_voucher_id, and repair_ticket_id are exposed
        for idx, cart_obj in enumerate(qs):
            payload_data = cart_obj.cart_payload if isinstance(cart_obj.cart_payload, dict) else {}
            data[idx]['customer_pan'] = cart_obj.customer_pan or payload_data.get('customer_pan') or ''
            data[idx]['trade_in_voucher_id'] = cart_obj.trade_in_voucher_id or payload_data.get('trade_in_voucher_id') or None
            data[idx]['repair_ticket_id'] = cart_obj.repair_ticket_id or payload_data.get('repair_ticket_id') or None

        return Response({'held_carts': data})

    def post(self, request):
        branch = getattr(request, 'active_branch', None)
        if not branch:
            return Response(
                {'error': 'Active branch context is required to park cart.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        cart_data = request.data.get('cart', [])
        if not cart_data:
            return Response(
                {'error': 'Cannot park an empty cart.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Evaluate Master Configuration switch for IMEI enforcement
        try:
            sys_config = SystemConfiguration.get_solo() if hasattr(SystemConfiguration, 'get_solo') else SystemConfiguration.objects.first()
            enforce_imei = getattr(sys_config, 'enforce_imei_tracking', True) if sys_config else True
        except Exception:
            enforce_imei = True

        sanitized_items = []
        running_subtotal = Decimal('0.00')

        for item in cart_data:
            # Check if this item is a repair service (never demand IMEI for labor/service)
            item_repair_ticket_id = item.get('repair_ticket_id') or None
            is_repair_service = bool(item.get('is_repair_service', False) or item_repair_ticket_id)
            requires_imei = bool(item.get('requires_imei', False)) and not is_repair_service

            imei_1 = (item.get('imei_number') or item.get('imei_1') or item.get('imei1') or '').strip()

            # Enforce Primary IMEI 1 ONLY if the master switch is ON
            if enforce_imei and requires_imei and not imei_1:
                return Response({
                    'error': f"Item '{item.get('name', 'Handset')}' is missing mandatory Primary IMEI 1."
                }, status=status.HTTP_400_BAD_REQUEST)

            # Resolve quantity and price
            try:
                qty = Decimal(str(item.get('quantity', 1) or 1)).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)
            except (InvalidOperation, ValueError, TypeError):
                qty = Decimal('1.000')

            if qty <= Decimal('0.000'):
                qty = Decimal('1.000')

            try:
                raw_price = item.get('unit_price', item.get('price', 0))
                price = Decimal(str(raw_price or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            except (InvalidOperation, ValueError, TypeError):
                price = Decimal('0.00')

            line_gross = (qty * price).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            running_subtotal += line_gross

            # Normalize and validate item discount type: AMOUNT, PERCENTAGE, or NONE
            raw_dtype = str(item.get('discount_type', '')).upper().strip()
            if raw_dtype in ['AMOUNT', 'FIXED', 'FLAT', 'CASH', 'NPR', 'RS']:
                norm_dtype = 'AMOUNT'
            elif raw_dtype in ['PERCENTAGE', '%', 'PERCENT']:
                norm_dtype = 'PERCENTAGE'
            elif raw_dtype == 'NONE':
                norm_dtype = 'NONE'
            else:
                try:
                    norm_dtype = 'PERCENTAGE' if float(item.get('discount_percent', 0) or 0) > 0 else 'NONE'
                except (ValueError, TypeError):
                    norm_dtype = 'NONE'

            # Extract raw discount input value
            raw_dval = item.get('discount_input_value')
            if raw_dval is None or str(raw_dval).strip() == '':
                raw_dval = item.get('discount_value')
            if raw_dval is None or str(raw_dval).strip() == '':
                raw_dval = item.get('discount_percent', 0)

            try:
                disc_val = Decimal(str(raw_dval or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            except (InvalidOperation, ValueError, TypeError):
                disc_val = Decimal('0.00')

            if disc_val < Decimal('0.00'):
                disc_val = Decimal('0.00')

            is_discountable = bool(item.get('is_discountable', True)) and not is_repair_service
            if not is_discountable:
                norm_dtype = 'NONE'
                disc_val = Decimal('0.00')

            # Calculate exact monetary deduction and secondary effective percentage
            if norm_dtype == 'AMOUNT':
                item_discount_amount = min(disc_val, line_gross)
                effective_pct = (
                    ((item_discount_amount / line_gross) * Decimal('100.00')).quantize(
                        Decimal('0.01'), rounding=ROUND_HALF_UP
                    ) if line_gross > Decimal('0.00') else Decimal('0.00')
                )
            elif norm_dtype == 'PERCENTAGE':
                effective_pct = min(Decimal('100.00'), disc_val)
                item_discount_amount = (line_gross * (effective_pct / Decimal('100.00'))).quantize(
                    Decimal('0.01'), rounding=ROUND_HALF_UP
                )
            else:
                norm_dtype = 'NONE'
                disc_val = Decimal('0.00')
                item_discount_amount = Decimal('0.00')
                effective_pct = Decimal('0.00')

            sanitized_item = {
                'product_id': item.get('product_id'),
                'item_instance_id': item.get('item_instance_id') or None,
                'name': item.get('name', ''),
                'unit_price': float(price),
                'price': float(price),
                'official_unit_price': float(Decimal(str(item.get('official_unit_price', item.get('catalog_price', price)) or price)).quantize(Decimal('0.01'))),
                'catalog_price': float(Decimal(str(item.get('catalog_price', item.get('official_unit_price', price)) or price)).quantize(Decimal('0.01'))),
                'cost_price': float(Decimal(str(item.get('cost_price', 0) or 0)).quantize(Decimal('0.01'))),
                'quantity': float(qty),
                'unit_code': item.get('unit_code', 'Pcs'),
                'discount_type': norm_dtype,
                'discount_value': float(disc_val),
                'discount_input_value': float(disc_val),
                'discount_percent': float(effective_pct),
                'item_discount_amount': float(item_discount_amount),
                'effective_discount_percent': float(effective_pct),
                'is_discountable': is_discountable,
                'tax_pricing_type': item.get('tax_pricing_type', 'EXEMPT'),
                'vat_rate': float(Decimal(str(item.get('vat_rate', 0) or 0))),
                'requires_imei': requires_imei,
                'imei_1': imei_1,
                'imei_number': imei_1,
                'imei_2': (item.get('secondary_imei') or item.get('imei_2') or item.get('imei2') or '').strip(),
                'secondary_imei': (item.get('secondary_imei') or item.get('imei_2') or item.get('imei2') or '').strip(),
                'conversion_id': item.get('conversion_id') or item.get('unit_conversion_id') or None,
                'unit_conversion_id': item.get('conversion_id') or item.get('unit_conversion_id') or None,
                'repair_ticket_id': item_repair_ticket_id,
                'is_repair_service': is_repair_service,
            }
            sanitized_items.append(sanitized_item)

        # Parse and sanitize dual-mode bill-level discount
        raw_bill_type = str(request.data.get('bill_discount_type', 'PERCENTAGE') or 'PERCENTAGE').upper().strip()
        if raw_bill_type in ['AMOUNT', 'FIXED', 'FLAT', 'CASH', 'NPR']:
            raw_bill_type = 'AMOUNT'
        elif raw_bill_type in ['PERCENTAGE', '%', 'PERCENT']:
            raw_bill_type = 'PERCENTAGE'
        elif raw_bill_type == 'NONE':
            raw_bill_type = 'NONE'
        else:
            raw_bill_type = 'PERCENTAGE'

        raw_bval = request.data.get('bill_discount_value')
        if raw_bval is None or str(raw_bval).strip() == '':
            raw_bval = request.data.get('bill_discount_percent', request.data.get('discount_percent', 0))

        try:
            bill_discount_value = Decimal(str(raw_bval or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        except (InvalidOperation, ValueError, TypeError):
            bill_discount_value = Decimal('0.00')

        if bill_discount_value < Decimal('0.00'):
            bill_discount_value = Decimal('0.00')

        try:
            discount_percent = Decimal(
                str(request.data.get('bill_discount_percent', request.data.get('discount_percent', 0)) or 0)
            ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        except (InvalidOperation, ValueError, TypeError):
            discount_percent = Decimal('0.00')

        discount_reason = str(request.data.get('discount_reason', '') or '').strip()
        notes = str(request.data.get('notes', '') or '').strip()

        # Customer & Tax PAN Resolution
        customer_obj = None
        customer_id = request.data.get('customer_id') or request.data.get('customer')
        if customer_id:
            customer_obj = Customer.objects.filter(id=customer_id, is_active=True).first()

        cust_name = request.data.get('customer_name', '').strip()
        if not cust_name and customer_obj:
            cust_name = customer_obj.name
        if not cust_name:
            cust_name = 'Walk-in Customer'

        cust_phone = request.data.get('customer_phone', '').strip()
        if not cust_phone and customer_obj:
            cust_phone = customer_obj.phone_number

        cust_pan = (
            request.data.get('customer_pan', '') or
            (customer_obj.pan_number if customer_obj and customer_obj.pan_number else '') or
            ''
        ).strip()

        # Trade-In & Repair Ticket Linkages Resolution
        trade_in_voucher_id = request.data.get('trade_in_voucher_id') or None
        if trade_in_voucher_id:
            try:
                trade_in_voucher_id = int(trade_in_voucher_id)
            except (ValueError, TypeError):
                trade_in_voucher_id = None

        repair_ticket_id = request.data.get('repair_ticket_id') or None
        if not repair_ticket_id:
            for itm in sanitized_items:
                if itm.get('repair_ticket_id'):
                    repair_ticket_id = itm.get('repair_ticket_id')
                    break
        if repair_ticket_id:
            try:
                repair_ticket_id = int(repair_ticket_id)
            except (ValueError, TypeError):
                repair_ticket_id = None

        try:
            subtotal = Decimal(str(request.data.get('subtotal', running_subtotal) or running_subtotal)).quantize(
                Decimal('0.01'), rounding=ROUND_HALF_UP
            )
        except (InvalidOperation, ValueError, TypeError):
            subtotal = running_subtotal

        # Build comprehensive payload snapshot
        cart_payload = {
            'items': sanitized_items,
            'bill_discount_type': raw_bill_type,
            'bill_discount_value': str(bill_discount_value),
            'bill_discount_percent': str(discount_percent),
            'discount_reason': discount_reason,
            'customer_phone': cust_phone,
            'customer_name': cust_name,
            'customer_pan': cust_pan,
            'trade_in_voucher_id': trade_in_voucher_id,
            'repair_ticket_id': repair_ticket_id,
        }

        hold_reference = f"HOLD-{uuid.uuid4().hex[:6].upper()}"

        hold_obj = POSHoldCart.objects.create(
            hold_reference=hold_reference,
            branch=branch,
            cashier=request.user,
            customer=customer_obj,
            customer_name=cust_name,
            customer_phone=cust_phone,
            customer_pan=cust_pan,
            trade_in_voucher_id=trade_in_voucher_id,
            repair_ticket_id=repair_ticket_id,
            cart_payload=cart_payload,
            subtotal=subtotal,
            bill_discount_type=raw_bill_type,
            bill_discount_value=bill_discount_value,
            discount_percent=discount_percent,
            notes=notes
        )

        return Response({
            'status': 'success',
            'hold_reference': hold_obj.hold_reference,
            'message': f"Cart parked successfully with reference {hold_obj.hold_reference}."
        }, status=status.HTTP_201_CREATED)

class HoldCartRecallDeleteAPIView(APIView):
    """
    API endpoint to retrieve (recall) and permanently discard suspended hold carts.
    Guarantees that:
    - Customer PAN, trade-in voucher ID, and repair ticket ID are fully restored.
    - Item discount types (AMOUNT, PERCENTAGE, NONE) and input values are returned intact.
    - Top-level bill discount type, value, and reasons are completely preserved.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, reference):
        cart = POSHoldCart.objects.filter(hold_reference=reference).select_related('customer', 'branch').first()
        if not cart:
            return Response({'error': 'Parked cart not found.'}, status=status.HTTP_404_NOT_FOUND)

        serializer = POSHoldCartSerializer(cart)
        data = serializer.data

        # Normalize and guarantee top-level bill discount fields match model state
        norm_bill_type = cart.bill_discount_type
        if norm_bill_type == 'FIXED':
            norm_bill_type = 'AMOUNT'
        data['bill_discount_type'] = norm_bill_type
        data['bill_discount_value'] = str(cart.bill_discount_value)
        data['discount_percent'] = str(cart.discount_percent)

        # Restore customer PAN, trade-in voucher ID, and repair ticket ID
        payload_data = cart.cart_payload if isinstance(cart.cart_payload, dict) else {}

        customer_pan = cart.customer_pan or payload_data.get('customer_pan') or ''
        trade_in_voucher_id = cart.trade_in_voucher_id or payload_data.get('trade_in_voucher_id') or None
        repair_ticket_id = cart.repair_ticket_id or payload_data.get('repair_ticket_id') or None

        data['customer_pan'] = customer_pan
        data['trade_in_voucher_id'] = trade_in_voucher_id
        data['repair_ticket_id'] = repair_ticket_id

        # Guarantee payload dictionary reflects the restored attributes
        if 'cart_payload' in data and isinstance(data['cart_payload'], dict):
            data['cart_payload']['customer_pan'] = customer_pan
            data['cart_payload']['trade_in_voucher_id'] = trade_in_voucher_id
            data['cart_payload']['repair_ticket_id'] = repair_ticket_id

        return Response(data, status=status.HTTP_200_OK)

    def delete(self, request, reference):
        cart = POSHoldCart.objects.filter(hold_reference=reference).first()
        if cart:
            cart.delete()
            return Response(
                {'status': 'deleted', 'message': f"Parked cart {reference} cleared."},
                status=status.HTTP_200_OK
            )
        return Response({'error': 'Parked cart not found.'}, status=status.HTTP_404_NOT_FOUND)