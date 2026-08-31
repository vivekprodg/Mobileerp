import uuid
from decimal import Decimal
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status, permissions
from django.db import transaction

from apps.sales.models import PhoneExchangeTradeIn, TradeInInspectionChecklist, TradeInLegalUndertaking
from apps.sales.api.serializers import (
    TradeInValuationCalculateSerializer, PhoneExchangeTradeInSerializer
)
from apps.sales.services.trade_in_engine import TradeInValuationEngine
from apps.integrations.mdms.nta_checker import NTAMDMSClient
from apps.core.models import SystemConfiguration


class TradeInVoucherLookupAPIView(APIView):
    """
    API endpoint for POS cashiers to look up an active/unattached Trade-In voucher by voucher number.
    Returns the valuation amount and traded-in device details for cart credit attachment.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, *args, **kwargs):
        voucher_code = request.GET.get('voucher', '').strip()
        if not voucher_code:
            return Response(
                {'error': 'Voucher number parameter is required.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        branch = getattr(request, 'active_branch', None)
        qs = PhoneExchangeTradeIn.objects.filter(
            voucher_number__iexact=voucher_code,
            status__in=['DRAFT', 'VALUATED']
        )

        if branch and not request.user.is_superuser:
            qs = qs.filter(branch=branch)

        voucher = qs.select_related('customer', 'branch').first()

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
    Creates the PhoneExchangeTradeIn, TradeInInspectionChecklist, and TradeInLegalUndertaking records.
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