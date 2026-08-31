from decimal import Decimal
from rest_framework import serializers
from apps.sales.models import PhoneExchangeTradeIn, TradeInInspectionChecklist, TradeInLegalUndertaking

class TradeInChecklistSerializer(serializers.ModelSerializer):
    class Meta:
        model = TradeInInspectionChecklist
        fields = [
            'touch_and_display', 'front_and_back_cameras', 'charging_and_battery',
            'wifi_bluetooth_gps', 'cellular_calling_mic_speaker', 'biometrics_security',
            'body_frame_condition', 'liquid_ingress_ldi', 'original_accessories_available',
            'account_lock_factory_reset', 'diagnostic_score_percent', 'technician_remarks'
        ]


class TradeInLegalUndertakingSerializer(serializers.ModelSerializer):
    class Meta:
        model = TradeInLegalUndertaking
        fields = [
            'id', 'customer_full_name', 'customer_father_or_spouse_name',
            'id_type', 'id_number', 'id_issued_district', 'id_issued_date_bs',
            'permanent_address', 'current_address', 'id_front_image', 'id_back_image',
            'customer_live_photo', 'declaration_accepted'
        ]


class PhoneExchangeTradeInSerializer(serializers.ModelSerializer):
    checklist = TradeInChecklistSerializer(source='inspection_checklist', read_only=True)
    undertaking = TradeInLegalUndertakingSerializer(source='legal_undertaking', read_only=True)
    condition_grade_display = serializers.CharField(source='get_recommended_condition_grade_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True)

    class Meta:
        model = PhoneExchangeTradeIn
        fields = [
            'id', 'voucher_number', 'branch', 'branch_name',
            'customer_name_manual', 'customer_phone_manual',
            'brand_name', 'model_name', 'ram_capacity', 'storage_capacity', 'color_variant',
            'imei_1', 'imei_2', 'serial_number', 'mdms_status',
            'market_base_value', 'total_deductions', 'shop_margin_deduction',
            'final_trade_in_value', 'recommended_condition_grade', 'condition_grade_display',
            'status', 'status_display', 'checklist', 'undertaking', 'created_at'
        ]


class TradeInValuationCalculateSerializer(serializers.Serializer):
    base_market_value = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal('0.00'))
    touch_and_display = serializers.CharField(required=True)
    front_and_back_cameras = serializers.CharField(required=True)
    charging_and_battery = serializers.CharField(required=True)
    wifi_bluetooth_gps = serializers.CharField(required=True)
    cellular_calling_mic_speaker = serializers.CharField(required=True)
    biometrics_security = serializers.CharField(required=True)
    body_frame_condition = serializers.CharField(required=True)
    liquid_ingress_ldi = serializers.CharField(required=True)
    original_accessories_available = serializers.CharField(required=True)
    account_lock_factory_reset = serializers.CharField(required=True)
    shop_margin_percent = serializers.DecimalField(max_digits=5, decimal_places=2, required=False, default=None)