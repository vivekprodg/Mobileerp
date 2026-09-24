from django.urls import path, include
from apps.products.views import (
    ProductBarcodePrintView, ProductQuickCreateModalView, ProductPriceTierManagerView
)
from apps.products.api.views import ProductSearchAPIView, ProductDetailAPIView

app_name = 'products'

# Standard nested REST API routing
api_patterns = [
    path('search/', ProductSearchAPIView.as_view(), name='api_product_search'),
    path('products/search/', ProductSearchAPIView.as_view(), name='api_products_search_alias'),
    path('<int:pk>/', ProductDetailAPIView.as_view(), name='api_product_detail'),
]

urlpatterns = [
    # UI Views & Printers
    path('print-barcodes/', ProductBarcodePrintView.as_view(), name='barcode_print'),
    path('quick-create/', ProductQuickCreateModalView.as_view(), name='quick_create'),
    path('<int:product_id>/add-tier/', ProductPriceTierManagerView.as_view(), name='add_price_tier'),

    # REST APIs
    path('api/', include((api_patterns, 'api'))),

    # Direct top-level aliases accessible as /products/api/search/ and /products/api/products/search/
    path('api/search/', ProductSearchAPIView.as_view(), name='product_search_direct'),
    path('api/products/search/', ProductSearchAPIView.as_view(), name='products_search_direct'),
]