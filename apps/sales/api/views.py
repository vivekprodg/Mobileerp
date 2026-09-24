import uuid
from decimal import Decimal
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status, permissions
from django.db import transaction
from django.db.models import Q

from apps.sales.models import (
    SalesEstimate, SalesEstimateItem,
    PhoneExchangeTradeIn, TradeInInspectionChecklist, TradeInLegalUndertaking
)
from apps.sales.api.serializers import (
    TradeInValuationCalculateSerializer, PhoneExchangeTradeInSerializer
)
from apps.sales.services.trade_in_engine import TradeInValuationEngine
from apps.integrations.mdms.nta_checker import NTAMDMSClient
from apps.core.models import SystemConfiguration

class SalesEstimateSearchAPIView(APIView):
    """
    Search past completed/finalized sales estimates and POS invoices for:
    - Customer Sales Returns
    - Warranty verification & repair tracking
    - Fast invoice retrieval and slip reprinting

    Behavior:
    1. If `q` is empty:
       Returns up to 15 most recent completed bills for the active branch context.
    2. If `q` is provided:
       Performs high-performance indexed search matching:
       - estimate_number (exact/icontains)
       - customer_name_manual / linked customer name
       - customer_phone_manual / linked customer phone
       - customer_pan
       - Sold device IMEI 1 (items__imei_number) or Secondary IMEI 2 (items__secondary_imei).
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
            # If no query provided, return the most recent active/completed sales
            qs = qs.filter(status__in=['COMPLETED', 'PARTIALLY_RETURNED'])
            qs = qs.order_by('-bill_date_ad', '-created_at')[:limit]
        else:
            # Query provided: search across header and item IMEI attributes
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
    - If `voucher` parameter is supplied:
        Fetches the exact single voucher for direct cart credit deduction.
    - If `voucher` is empty/omitted:
        Returns up to 20 unattached, available vouchers (status DRAFT or VALUATED)
        filtered by active branch context so the cashier can browse or pick one.
    """
    permission_classes = [permissions.IsAuthenticated]

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
                return Response(
                    {'error': f"Trade-In voucher '{voucher_code}' not found, expired, or already attached to another bill."},
                    status=status.HTTP_404_NOT_FOUND
                )

            return Response({
                'status': 'success',
                'id': voucher.id,
                'voucher_number': voucher.voucher_number,
                'brand_name': voucher.brand_name,
                'model_name': voucher.model_name,
                'ram_capacity': voucher.ram_capacity or '',
                'storage_capacity': voucher.storage_capacity or '',
                'color_variant': voucher.color_variant or '',
                'imei_1': voucher.imei_1,
                'mdms_status': voucher.mdms_status,
                'market_base_value': str(voucher.market_base_value),
                'total_deductions': str(voucher.total_deductions),
                'final_trade_in_value': str(voucher.final_trade_in_value),
                'recommended_condition_grade': voucher.recommended_condition_grade,
                'customer_name': voucher.customer_name_manual or (voucher.customer.name if voucher.customer else 'Walk-in'),
                'customer_phone': voucher.customer_phone_manual or (voucher.customer.phone_number if voucher.customer else ''),
                'status': voucher.status
            }, status=status.HTTP_200_OK)

        # 2. Browse / Search list when voucher parameter is not explicitly supplied
        if search_query:
            qs = qs.filter(
                Q(voucher_number__icontains=search_query) |
                Q(imei_1__icontains=search_query) |
                Q(customer_name_manual__icontains=search_query) |
                Q(customer_phone_manual__icontains=search_query) |
                Q(brand_name__icontains=search_query) |
                Q(model_name__icontains=search_query)
            )

        vouchers = qs.order_by('-created_at')[:20]
        results = [
            {
                'id': v.id,
                'voucher_number': v.voucher_number,
                'brand_name': v.brand_name,
                'model_name': v.model_name,
                'ram_capacity': v.ram_capacity or '',
                'storage_capacity': v.storage_capacity or '',
                'color_variant': v.color_variant or '',
                'imei_1': v.imei_1,
                'mdms_status': v.mdms_status,
                'market_base_value': str(v.market_base_value),
                'total_deductions': str(v.total_deductions),
                'final_trade_in_value': str(v.final_trade_in_value),
                'recommended_condition_grade': v.recommended_condition_grade,
                'customer_name': v.customer_name_manual or (v.customer.name if v.customer else 'Walk-in'),
                'customer_phone': v.customer_phone_manual or (v.customer.phone_number if v.customer else ''),
                'status': v.status,
                'created_at': v.created_at.strftime('%Y-%m-%d %H:%M') if v.created_at else ''
            }
            for v in vouchers
        ]

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
            return Response({'errors': serializer.errors}, status=status.HTTP_400_BAD_REQUEST)

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
        })

class NTAMDMSCheckAPIView(APIView):
    """
    Instant 1-click NTA MDMS verification endpoint for counter staff.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, *args, **kwargs):
        imei = request.GET.get('imei', '').strip()
        if not imei:
            return Response({'error': 'IMEI parameter is required'}, status=status.HTTP_400_BAD_REQUEST)

        res = NTAMDMSClient.verify_imei(imei)
        res['badge_html'] = NTAMDMSClient.format_mdms_badge(res['status'])
        return Response(res)

class TradeInVoucherCreateAPIView(APIView):
    """
    API endpoint committing a full Trade-In voucher to attach directly to POS cart.
    Creates PhoneExchangeTradeIn, TradeInInspectionChecklist, and TradeInLegalUndertaking records.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, *args, **kwargs):
        branch = getattr(request, 'active_branch', None)
        if not branch:
            return Response({'error': 'Active branch context required'}, status=status.HTTP_400_BAD_REQUEST)

        data = request.data
        brand = data.get('brand_name', '').strip()
        model = data.get('model_name', '').strip()
        storage = data.get('storage_capacity', '').strip()
        imei = data.get('imei_1', '').strip()
        base_market = Decimal(str(data.get('market_base_value', 0)))
        cust_name = data.get('customer_name_manual', '').strip()
        cust_phone = data.get('customer_phone_manual', '').strip()

        if not brand or not model or not imei or base_market <= 0:
            return Response({'error': 'Brand, Model, IMEI, and Base Value are required.'}, status=status.HTTP_400_BAD_REQUEST)

        checklist_data = data.get('checklist', {})

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

            # Create the baseline TradeInLegalUndertaking record to prevent RelatedObjectDoesNotExist
            undertaking_data = data.get('undertaking', {})
            sys_config = SystemConfiguration.get_solo()
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
                declaration_text=sys_config.undertaking_declaration_text_np,
                declaration_accepted=undertaking_data.get('declaration_accepted', True),
                verified_by=request.user
            )

        return Response({
            'status': 'success',
            'voucher_id': voucher.id,
            'voucher_number': voucher.voucher_number,
            'final_trade_in_value': str(voucher.final_trade_in_value),
            'recommended_grade': voucher.recommended_condition_grade,
            'message': f"Trade-In voucher {voucher.voucher_number} generated with credit Rs. {voucher.final_trade_in_value}."
        }, status=status.HTTP_201_CREATED)