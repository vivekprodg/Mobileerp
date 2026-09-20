"""
Purchases, Supplier Ledgers & Commercial Purchase Return URL Routes.
File Path: apps/purchases/urls.py
"""

from django.urls import path
from apps.purchases.views import (
    SupplierListView, SupplierDetailView, SupplierCreateView,
    SupplierUpdateView, SupplierPaymentRecordView,
    PurchaseOrderListView, PurchaseOrderCreateView,
    PurchaseOrderDetailView, PurchaseOrderStatusUpdateView,
    GRNListView, GRNDetailView, GRNCreateView,
    PurchaseReturnListView, PurchaseReturnCreateView, PurchaseReturnDetailView
)

app_name = 'purchases'

urlpatterns = [
    # Supplier Management
    path('suppliers/', SupplierListView.as_view(), name='supplier_list'),
    path('suppliers/create/', SupplierCreateView.as_view(), name='supplier_create'),
    path('suppliers/<int:pk>/', SupplierDetailView.as_view(), name='supplier_detail'),
    path('suppliers/<int:pk>/edit/', SupplierUpdateView.as_view(), name='supplier_edit'),
    path('suppliers/<int:pk>/payment/', SupplierPaymentRecordView.as_view(), name='supplier_payment'),

    # Purchase Orders (PO) & Requisitions
    path('orders/', PurchaseOrderListView.as_view(), name='po_list'),
    path('orders/create/', PurchaseOrderCreateView.as_view(), name='po_create'),
    path('orders/<int:pk>/', PurchaseOrderDetailView.as_view(), name='po_detail'),
    path('orders/<int:pk>/status/', PurchaseOrderStatusUpdateView.as_view(), name='po_status_update'),

    # Goods Received Note (GRN) & Stock Inward
    path('grn/', GRNListView.as_view(), name='grn_list'),
    path('grn/create/', GRNCreateView.as_view(), name='grn_create'),
    path('grn/<int:pk>/', GRNDetailView.as_view(), name='grn_detail'),

    # Commercial Purchase Returns & Debit Notes
    path('returns/', PurchaseReturnListView.as_view(), name='purchase_return_list'),
    path('returns/create/', PurchaseReturnCreateView.as_view(), name='purchase_return_create'),
    path('returns/<int:pk>/', PurchaseReturnDetailView.as_view(), name='purchase_return_detail'),
]