from django.urls import path, include
from apps.pos.views import (
    OpenShiftView, CloseShiftView,
    CashDrawerSessionListView, CashDrawerSessionDetailView
)
from apps.pos.api.views import HoldCartListCreateAPIView, HoldCartRecallDeleteAPIView

app_name = 'pos'

api_patterns = [
    path('hold-carts/', HoldCartListCreateAPIView.as_view(), name='api_hold_carts'),
    path('hold-carts/<str:reference>/', HoldCartRecallDeleteAPIView.as_view(), name='api_hold_cart_detail'),
]

urlpatterns = [
    path('open-shift/', OpenShiftView.as_view(), name='open_shift'),
    path('close-shift/', CloseShiftView.as_view(), name='close_shift'),
    path('sessions/', CashDrawerSessionListView.as_view(), name='session_list'),
    path('sessions/<int:pk>/', CashDrawerSessionDetailView.as_view(), name='session_detail'),
    path('api/', include((api_patterns, 'api'))),
]