from django.urls import path
from apps.customers.views import (
    CustomerListView, CustomerDetailView, CustomerCreateView,
    CustomerUpdateView, CustomerUdhaariPaymentView, CustomerSearchAPIView
)

app_name = 'customers'

urlpatterns = [
    path('', CustomerListView.as_view(), name='customer_list'),
    path('create/', CustomerCreateView.as_view(), name='customer_create'),
    path('<int:pk>/', CustomerDetailView.as_view(), name='customer_detail'),
    path('<int:pk>/edit/', CustomerUpdateView.as_view(), name='customer_edit'),
    path('<int:pk>/payment/', CustomerUdhaariPaymentView.as_view(), name='customer_payment'),
    path('api/search/', CustomerSearchAPIView.as_view(), name='customer_search_api'),
]