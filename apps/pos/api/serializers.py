from rest_framework import serializers
from apps.pos.models import CashDrawerSession, POSHoldCart

class CashDrawerSessionSerializer(serializers.ModelSerializer):
    cashier_name = serializers.CharField(source='cashier.get_full_name', read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True)

    class Meta:
        model = CashDrawerSession
        fields = [
            'id', 'session_number', 'branch', 'branch_name', 'cashier',
            'cashier_name', 'opening_time', 'closing_time', 'opening_cash',
            'expected_closing_cash', 'actual_closing_cash', 'cash_discrepancy',
            'total_sales_amount', 'total_cash_sales', 'total_digital_sales',
            'total_credit_sales', 'total_returns_amount', 'status', 'remarks'
        ]

class POSHoldCartSerializer(serializers.ModelSerializer):
    class Meta:
        model = POSHoldCart
        fields = [
            'id', 'hold_reference', 'customer', 'customer_name',
            'customer_phone', 'cart_payload', 'subtotal',
            'discount_percent', 'notes', 'created_at'
        ]