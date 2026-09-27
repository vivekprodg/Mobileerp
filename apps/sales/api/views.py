"""
Sales REST API Endpoints.

Capabilities:
1. SalesEstimateSearchAPIView: Fast indexed invoice lookup by number, customer, phone, PAN, or IMEI.
2. TradeInVoucherLookupAPIView: Looks up active and unattached vouchers with standardized dictionary
   keys ('status' and 'voucher_status') and uniform array envelopes ({'status': 'success', 'results': [...]})
   across both exact code lookups and query searches (Issues A & B Resolved).
3. TradeInValuationCalculateAPIView: Real-time mathematical diagnostic calculator.
4. NTAMDMSCheckAPIView: NTA MDMS verification proxy with UI status badges.
5. TradeInVoucherCreateAPIView: Robust voucher intake with safe Decimal parsing and checklist field sanitization.
"""

import uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status, permissions
from django.db import transaction
from django.db.models import Q

from apps.sales.models import (
    SalesEstimate,
    PhoneExchangeTradeIn,
    TradeInInspectionChecklist,
    TradeInLegalUndertaking
)
from apps.sales.api.serializers import TradeInValuationCalculateSerializer
from apps.sales.services.trade_in_engine import TradeInValuationEngine
from apps.integrations.mdms.nta_checker import NTAMDMSClient
from apps.core.models import SystemConfiguration

class SalesEstimateSearchAPIView(APIView):
    """
    Search past completed/finalized sales estimates and POS invoices for:
    - Customer Sales Returns
    - Warranty verification & repair tracking
    - Fast invoice retrieval and slip reprinting
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, *args, **kwargs):
        q = request.GET.get('q', '').strip()
        branch = getattr(request, 'active_branch', None)

        try:
            limit = int(request.GET.get('limit', 15))
            limit = max(1, min(limit, 50))
        except (ValueError, TypeError):
            limit = 15

        qs = SalesEstimate.objects.all().select_related(
            'customer', 'branch', 'cashier', 'salesperson'
        ).prefetch_related('items__product')

        if branch and not request.user.is_superuser:
            qs = qs.filter(branch=branch)

        if not q:
            qs = qs.filter(status__in=['COMPLETED', 'PARTIALLY_RETURNED'])
            qs = qs.order_by('-bill_date_ad', '-created_at')[:limit]
        else:
            search_filter = (
                Q(estimate_number__icontains=q) |
                Q(customer_name_manual__icontains=q) |
                Q(customer_phone_manual__icontains=q) |
                Q(customer_pan__icontains=q) |
                Q(customer__name__icontains=q) |
                Q(customer__phone_number__icontains=q) |
                Q(items__imei_number__icontains=q) |
                Q(items__secondary_imei__icontains=q)
            )
            qs = qs.filter(search_filter).distinct().order_by('-bill_date_ad', '-created_at')[:limit]

        results = []
        for est in qs:
            results.append({
                'id': est.id,
                'estimate_number': est.estimate_number,
                'bill_date_ad': est.bill_date_ad.isoformat() if est.bill_date_ad else '',
                'bill_date_bs': est.bill_date_bs or '',
                'fiscal_year': est.fiscal_year or '',
                'customer_name': est.recipient_display_name,
                'customer_phone': est.customer_phone_manual or (est.customer.phone_number if est.customer else ''),
                'customer_pan': est.customer_pan or '',
                'subtotal': str(est.subtotal),
                'grand_total': str(est.grand_total),
                'paid_amount': str(est.paid_amount),
                'due_amount': str(est.due_amount),
                'status': est.status,
                'status_display': est.get_status_display(),
                'payment_status': est.payment_status,
                'payment_status_display': est.get_payment_status_display(),
                'items_count': est.items.count(),
                'has_trade_in_exchange': est.has_trade_in_exchange,
                'trade_in_discount_amount': str(est.trade_in_discount_amount),
            })

        return Response({
            'status': 'success',
            'count': len(results),
            'results': results
        }, status=status.HTTP_200_OK)

class TradeInVoucherLookupAPIView(APIView):
    """
    API endpoint for POS cashiers to look up active and unattached Trade-In vouchers.
    - If `voucher` parameter is supplied: Fetches exact single voucher wrapped in a standardized
      `results` array along with top-level attributes, guaranteeing key consistency (`status` & `voucher_status`).
    - If `voucher` is empty: Returns up to 20 unattached vouchers for counter browsing and search queries.
    """
    permission_classes = [permissions.IsAuthenticated]

    @staticmethod
    def _format_voucher(v: PhoneExchangeTradeIn) -> dict:
        """Standardizes voucher attributes across all lookup queries."""
        return {
            'id': v.id,
            'voucher_number': v.voucher_number,
            'brand_name': v.brand_name,
            'model_name': v.model_name,
            'ram_capacity': v.ram_capacity or '',
            'storage_capacity': v.storage_capacity or '',
            'color_variant': v.color_variant or '',
            'imei_1': v.imei_1,
            'imei_2': v.imei_2 or '',
            'mdms_status': v.mdms_status,
            'market_base_value': str(v.market_base_value),
            'total_deductions': str(v.total_deductions),
            'shop_margin_deduction': str(v.shop_margin_deduction),
            'final_trade_in_value': str(v.final_trade_in_value),
            'recommended_condition_grade': v.recommended_condition_grade,
            'condition_grade_display': v.get_recommended_condition_grade_display(),
            'customer_name': v.customer_name_manual or (v.customer.name if v.customer else 'Walk-in'),
            'customer_phone': v.customer_phone_manual or (v.customer.phone_number if v.customer else ''),
            'voucher_status': v.status,
            'status': v.status,
            'status_display': v.get_status_display(),
            'created_at': v.created_at.strftime('%Y-%m-%d %H:%M') if v.created_at else ''
        }

    def get(self, request, *args, **kwargs):
        voucher_code = request.GET.get('voucher', '').strip()
        search_query = request.GET.get('q', '').strip()
        branch = getattr(request, 'active_branch', None)

        qs = PhoneExchangeTradeIn.objects.filter(
            status__in=['DRAFT', 'VALUATED']
        ).select_related('customer', 'branch')

        if branch and not request.user.is_superuser:
            qs = qs.filter(branch=branch)

        # 1. Exact or specified voucher lookup
        if voucher_code:
            voucher = qs.filter(voucher_number__iexact=voucher_code).first()
            if not voucher:
                return Response({
                    'status': 'error',
                    'error_code': 'VOUCHER_NOT_FOUND',
                    'error': f"Trade-In voucher '{voucher_code}' not found, expired, or already attached to another bill.",
                    'count': 0,
                    'results': []
                }, status=status.HTTP_404_NOT_FOUND)

            voucher_data = self._format_voucher(voucher)

            return Response({
                'status': 'success',
                'count': 1,
                'results': [voucher_data],
                # Top-level mirrored keys for backwards compatibility with any flat dictionary consumers
                'voucher': voucher_data,
                'id': voucher_data['id'],
                'voucher_number': voucher_data['voucher_number'],
                'brand_name': voucher_data['brand_name'],
                'model_name': voucher_data['model_name'],
                'ram_capacity': voucher_data['ram_capacity'],
                'storage_capacity': voucher_data['storage_capacity'],
                'color_variant': voucher_data['color_variant'],
                'imei_1': voucher_data['imei_1'],
                'imei_2': voucher_data['imei_2'],
                'mdms_status': voucher_data['mdms_status'],
                'market_base_value': voucher_data['market_base_value'],
                'total_deductions': voucher_data['total_deductions'],
                'shop_margin_deduction': voucher_data['shop_margin_deduction'],
                'final_trade_in_value': voucher_data['final_trade_in_value'],
                'recommended_condition_grade': voucher_data['recommended_condition_grade'],
                'condition_grade_display': voucher_data['condition_grade_display'],
                'customer_name': voucher_data['customer_name'],
                'customer_phone': voucher_data['customer_phone'],
                'voucher_status': voucher_data['voucher_status'],
                'status_display': voucher_data['status_display'],
                'created_at': voucher_data['created_at'],
            }, status=status.HTTP_200_OK)

        # 2. Browse / Search list when voucher parameter is not explicitly supplied
        if search_query:
            qs = qs.filter(
                Q(voucher_number__icontains=search_query) |
                Q(imei_1__icontains=search_query) |
                Q(imei_2__icontains=search_query) |
                Q(customer_name_manual__icontains=search_query) |
                Q(customer_phone_manual__icontains=search_query) |
                Q(brand_name__icontains=search_query) |
                Q(model_name__icontains=search_query)
            )

        vouchers = qs.order_by('-created_at')[:20]
        results = [self._format_voucher(v) for v in vouchers]

        return Response({
            'status': 'success',
            'count': len(results),
            'results': results
        }, status=status.HTTP_200_OK)

class TradeInValuationCalculateAPIView(APIView):
    """
    Live mathematical calculator endpoint for counter staff.
    Accepts 10 inspection criteria and returns real-time itemized buy-back valuation.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, *args, **kwargs):
        serializer = TradeInValuationCalculateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({
                'status': 'error',
                'error_code': 'VALIDATION_ERROR',
                'errors': serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)

        base_val = serializer.validated_data['base_market_value']
        margin_pct = serializer.validated_data.get('shop_margin_percent')

        checklist_dict = {
            'touch_and_display': serializer.validated_data['touch_and_display'],
            'front_and_back_cameras': serializer.validated_data['front_and_back_cameras'],
            'charging_and_battery': serializer.validated_data['charging_and_battery'],
            'wifi_bluetooth_gps': serializer.validated_data['wifi_bluetooth_gps'],
            'cellular_calling_mic_speaker': serializer.validated_data['cellular_calling_mic_speaker'],
            'biometrics_security': serializer.validated_data['biometrics_security'],
            'body_frame_condition': serializer.validated_data['body_frame_condition'],
            'liquid_ingress_ldi': serializer.validated_data['liquid_ingress_ldi'],
            'original_accessories_available': serializer.validated_data['original_accessories_available'],
            'account_lock_factory_reset': serializer.validated_data['account_lock_factory_reset'],
        }

        result = TradeInValuationEngine.calculate_valuation(
            base_market_value=base_val,
            checklist_data=checklist_dict,
            shop_margin_pct=margin_pct
        )

        return Response({
            'status': 'success',
            'final_offer': str(result['final_offer']),
            'total_deductions': str(result['total_deductions']),
            'margin_deduction': str(result['margin_deduction']),
            'score_percent': str(result['score_percent']),
            'condition_grade': result['condition_grade'],
            'deduction_breakdown': result['deduction_breakdown']
        }, status=status.HTTP_200_OK)

class NTAMDMSCheckAPIView(APIView):
    """Instant 1-click NTA MDMS verification endpoint for counter staff."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, *args, **kwargs):
        imei = request.GET.get('imei', '').strip()
        if not imei:
            return Response({
                'status': 'error',
                'error_code': 'MISSING_IMEI',
                'error': 'IMEI parameter is required'
            }, status=status.HTTP_400_BAD_REQUEST)

        res = NTAMDMSClient.verify_imei(imei)
        res['badge_html'] = NTAMDMSClient.format_mdms_badge(res['status'])
        return Response(res, status=status.HTTP_200_OK)

class TradeInVoucherCreateAPIView(APIView):
    """
    API endpoint committing a full Trade-In voucher.
    Safely parses market base value and sanitizes inspection checklist fields.
    """
    permission_classes = [permissions.IsAuthenticated]

    ALLOWED_CHECKLIST_FIELDS = {
        'touch_and_display', 'front_and_back_cameras', 'charging_and_battery',
        'wifi_bluetooth_gps', 'cellular_calling_mic_speaker', 'biometrics_security',
        'body_frame_condition', 'liquid_ingress_ldi', 'original_accessories_available',
        'account_lock_factory_reset', 'technician_remarks'
    }

    def post(self, request, *args, **kwargs):
        branch = getattr(request, 'active_branch', None)
        if not branch:
            return Response({
                'status': 'error',
                'error_code': 'NO_ACTIVE_BRANCH',
                'error': 'Active branch context required.'
            }, status=status.HTTP_400_BAD_REQUEST)

        data = request.data or {}
        brand = str(data.get('brand_name', '')).strip()
        model = str(data.get('model_name', '')).strip()
        storage = str(data.get('storage_capacity', '')).strip()
        imei = str(data.get('imei_1', '')).strip()
        cust_name = str(data.get('customer_name_manual', '')).strip()
        cust_phone = str(data.get('customer_phone_manual', '')).strip()

        # 1. Safe Decimal Parsing for Benchmark Market Value
        raw_base_market = data.get('market_base_value')
        if raw_base_market is None or str(raw_base_market).strip() == '':
            return Response({
                'status': 'error',
                'error_code': 'INVALID_BASE_VALUE',
                'error': 'Benchmark Market Value is required and must be a valid numeric amount greater than zero.'
            }, status=status.HTTP_400_BAD_REQUEST)

        clean_market_val_str = str(raw_base_market).replace(',', '').strip()
        try:
            base_market = Decimal(clean_market_val_str).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        except (InvalidOperation, TypeError, ValueError):
            return Response({
                'status': 'error',
                'error_code': 'INVALID_BASE_VALUE',
                'error': 'Benchmark Market Value must be a valid numeric amount (e.g. 45000.00).'
            }, status=status.HTTP_400_BAD_REQUEST)

        if base_market <= Decimal('0.00'):
            return Response({
                'status': 'error',
                'error_code': 'INVALID_BASE_VALUE',
                'error': 'Benchmark Market Value must be greater than zero.'
            }, status=status.HTTP_400_BAD_REQUEST)

        # 2. Required Fields Validation
        if not brand or not model or not imei:
            return Response({
                'status': 'error',
                'error_code': 'MISSING_REQUIRED_FIELDS',
                'error': 'Brand, Model, and Primary IMEI (15 digits) are strictly required.'
            }, status=status.HTTP_400_BAD_REQUEST)

        # 3. Sanitize Checklist Payload against Allowed Model Fields
        raw_checklist = data.get('checklist', {}) or {}
        checklist_data = {
            k: v for k, v in raw_checklist.items()
            if k in self.ALLOWED_CHECKLIST_FIELDS
        }

        valuation = TradeInValuationEngine.calculate_valuation(
            base_market_value=base_market,
            checklist_data=checklist_data
        )

        voucher_no = f"EXC-{branch.code}-{uuid.uuid4().hex[:6].upper()}"

        with transaction.atomic():
            voucher = PhoneExchangeTradeIn.objects.create(
                voucher_number=voucher_no,
                branch=branch,
                cashier=request.user,
                customer_name_manual=cust_name or 'Walk-in Customer',
                customer_phone_manual=cust_phone,
                brand_name=brand,
                model_name=model,
                ram_capacity=data.get('ram_capacity', ''),
                storage_capacity=storage,
                color_variant=data.get('color_variant', ''),
                imei_1=imei,
                imei_2=data.get('imei_2', ''),
                serial_number=data.get('serial_number', ''),
                mdms_status=data.get('mdms_status', 'REGISTERED_OFFICIAL'),
                market_base_value=base_market,
                total_deductions=valuation['total_deductions'],
                shop_margin_deduction=valuation['margin_deduction'],
                final_trade_in_value=valuation['final_offer'],
                recommended_condition_grade=valuation['condition_grade'],
                status='VALUATED'
            )

            TradeInInspectionChecklist.objects.create(
                trade_in_voucher=voucher,
                diagnostic_score_percent=valuation['score_percent'],
                **checklist_data
            )

            # Create Baseline Statutory Undertaking Record
            undertaking_data = data.get('undertaking', {}) or {}
            sys_config = SystemConfiguration.get_solo()
            default_declaration = getattr(sys_config, 'undertaking_declaration_text_np', '') or "Declaration of legal device ownership."

            TradeInLegalUndertaking.objects.create(
                trade_in_voucher=voucher,
                customer_full_name=undertaking_data.get('customer_full_name') or cust_name or 'Walk-in Customer',
                customer_father_or_spouse_name=undertaking_data.get('customer_father_or_spouse_name', ''),
                id_type=undertaking_data.get('id_type', 'CITIZENSHIP'),
                id_number=undertaking_data.get('id_number') or 'NOT-PROVIDED',
                id_issued_district=undertaking_data.get('id_issued_district', 'Kathmandu'),
                id_issued_date_bs=undertaking_data.get('id_issued_date_bs', ''),
                permanent_address=undertaking_data.get('permanent_address') or 'Kathmandu, Nepal',
                current_address=undertaking_data.get('current_address', ''),
                declaration_text=default_declaration,
                declaration_accepted=undertaking_data.get('declaration_accepted', True),
                verified_by=request.user
            )

        return Response({
            'status': 'success',
            'voucher_id': voucher.id,
            'voucher_number': voucher.voucher_number,
            'final_trade_in_value': str(voucher.final_trade_in_value),
            'recommended_grade': voucher.recommended_condition_grade,
            'condition_grade_display': voucher.get_recommended_condition_grade_display(),
            'voucher_status': voucher.status,
            'status_display': voucher.get_status_display(),
            'message': f"Trade-In voucher {voucher.voucher_number} generated with credit Rs. {voucher.final_trade_in_value}."
        }, status=status.HTTP_201_CREATED)