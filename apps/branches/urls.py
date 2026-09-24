from django.urls import path
from apps.branches.views import (
    BranchListView,
    BranchCreateView,
    BranchUpdateView,
    SwitchBranchContextView,
    BranchSearchAPIView,
    StockTransferListView,
    StockTransferCreateView,
    StockTransferDispatchView,
    StockTransferReceiveView,
    StockTransferSearchAPIView,
)

app_name = 'branches'

urlpatterns = [
    # Branch Management & Outlet Switching
    path('', BranchListView.as_view(), name='branch_list'),
    path('create/', BranchCreateView.as_view(), name='branch_create'),
    path('<int:pk>/edit/', BranchUpdateView.as_view(), name='branch_edit'),
    path('switch/', SwitchBranchContextView.as_view(), name='switch_branch'),
    path('api/search/', BranchSearchAPIView.as_view(), name='branch_search_api'),

    # Inter-Branch Stock Transfers & Logistics Pipeline
    path('transfers/', StockTransferListView.as_view(), name='transfer_list'),
    path('transfers/create/', StockTransferCreateView.as_view(), name='transfer_create'),
    path('transfers/<int:pk>/dispatch/', StockTransferDispatchView.as_view(), name='transfer_dispatch'),
    path('transfers/<int:pk>/receive/', StockTransferReceiveView.as_view(), name='transfer_receive'),
    path('transfers/api/search/', StockTransferSearchAPIView.as_view(), name='transfer_search_api'),
]