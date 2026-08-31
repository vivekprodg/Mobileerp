from django.urls import path
from apps.branches.views import (
    BranchListView,
    BranchCreateView,
    BranchUpdateView,
    SwitchBranchContextView,
    StockTransferListView,
    StockTransferCreateView,
    StockTransferDispatchView,
    StockTransferReceiveView,
)

app_name = 'branches'

urlpatterns = [
    # Branch Management & Outlet Switching
    path('', BranchListView.as_view(), name='branch_list'),
    path('create/', BranchCreateView.as_view(), name='branch_create'),
    path('<int:pk>/edit/', BranchUpdateView.as_view(), name='branch_edit'),
    path('switch/', SwitchBranchContextView.as_view(), name='switch_branch'),

    # Inter-Branch Stock Transfers & Logistics Pipeline
    path('transfers/', StockTransferListView.as_view(), name='transfer_list'),
    path('transfers/create/', StockTransferCreateView.as_view(), name='transfer_create'),
    path('transfers/<int:pk>/dispatch/', StockTransferDispatchView.as_view(), name='transfer_dispatch'),
    path('transfers/<int:pk>/receive/', StockTransferReceiveView.as_view(), name='transfer_receive'),
]