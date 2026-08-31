from django.urls import path, include
from apps.products.views import (
    ProductBarcodePrintView, ProductQuickCreateModalView, ProductPriceTierManagerView
)
from apps.products.api.views import ProductSearchAPIView, ProductDetailAPIView

app_name = 'products'

api_patterns = [
    path('search/', ProductSearchAPIView.as_view(), name='api_product_search'),
    path('<int:pk>/', ProductDetailAPIView.as_view(), name='api_product_detail'),
]

urlpatterns = [
    path('print-barcodes/', ProductBarcodePrintView.as_view(), name='barcode_print'),
    path('quick-create/', ProductQuickCreateModalView.as_view(), name='quick_create'),
    path('<int:product_id>/add-tier/', ProductPriceTierManagerView.as_view(), name='add_price_tier'),
    path('api/', include((api_patterns, 'api'))),
]