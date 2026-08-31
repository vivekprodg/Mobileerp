from django.urls import path, include
from apps.sales.views import (
    POSTerminalView, POSCheckoutAPIView, SalesEstimateListView,
    SalesEstimateDetailView, SalesEstimateThermalSlipView, SalesEstimateCancelView,
    SalesReturnListView, SalesReturnCreateView, SalesReturnDetailView, SalesReturnThermalSlipView,
    TradeInListView, TradeInEvaluationWizardView, TradeInDetailView,
    TradeInPoliceUndertakingPrintView
)
from apps.sales.api.views import (
    TradeInVoucherLookupAPIView, TradeInValuationCalculateAPIView,
    NTAMDMSCheckAPIView, TradeInVoucherCreateAPIView
)

app_name = 'sales'

api_patterns = [
    path('checkout/', POSCheckoutAPIView.as_view(), name='api_checkout'),
    path('trade-in/lookup/', TradeInVoucherLookupAPIView.as_view(), name='api_trade_in_lookup'),
    path('trade-in/calculate/', TradeInValuationCalculateAPIView.as_view(), name='api_trade_in_calculate'),
    path('trade-in/create/', TradeInVoucherCreateAPIView.as_view(), name='api_trade_in_create'),
    path('mdms/check/', NTAMDMSCheckAPIView.as_view(), name='api_mdms_check'),
]

urlpatterns = [
    # POS Terminal & Fast Billing
    path('pos/', POSTerminalView.as_view(), name='pos_terminal'),
    path('estimates/', SalesEstimateListView.as_view(), name='estimate_list'),
    path('estimates/<int:pk>/', SalesEstimateDetailView.as_view(), name='estimate_detail'),
    path('estimates/<int:pk>/thermal-slip/', SalesEstimateThermalSlipView.as_view(), name='estimate_thermal_slip'),
    path('estimates/<int:pk>/cancel/', SalesEstimateCancelView.as_view(), name='estimate_cancel'),

    # Sales Returns & Defective Item Restocking (Section 4)
    path('returns/', SalesReturnListView.as_view(), name='return_list'),
    path('returns/create/<int:estimate_id>/', SalesReturnCreateView.as_view(), name='return_create'),
    path('returns/<int:pk>/', SalesReturnDetailView.as_view(), name='return_detail'),
    path('returns/<int:pk>/thermal-slip/', SalesReturnThermalSlipView.as_view(), name='return_thermal_slip'),

    # Trade-In & Buy-Back Vouchers
    path('trade-in/', TradeInListView.as_view(), name='trade_in_list'),
    path('trade-in/wizard/', TradeInEvaluationWizardView.as_view(), name='trade_in_wizard'),
    path('trade-in/<int:pk>/', TradeInDetailView.as_view(), name='trade_in_detail'),
    path('trade-in/<int:pk>/undertaking-print/', TradeInPoliceUndertakingPrintView.as_view(), name='trade_in_undertaking_print'),

    # REST APIs
    path('api/', include((api_patterns, 'api'))),
]