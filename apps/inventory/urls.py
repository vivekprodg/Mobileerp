from django.urls import path
from apps.inventory.views import (
    ProductListView, ProductDetailView, ProductCreateView, ProductUpdateView,
    ProductStockAdjustmentView, CategoryListView, CategoryCreateView,
    CategoryUpdateView, CategoryQuickCreateAPIView, SubCategoryQuickCreateAPIView,
    BrandListView, BrandCreateView, BrandUpdateView, BrandQuickCreateAPIView,
    UnitListView, UnitCreateView, UnitUpdateView, UnitQuickCreateAPIView,
    ItemInstanceListView,
    VendorRMAListView, VendorRMACreateView, VendorRMADetailView,
    ProductExcelUploadView, ProductExcelMappingView, ProductExcelProcessAPIView
)

app_name = 'inventory'

urlpatterns = [
    # Catalog Management
    path('', ProductListView.as_view(), name='product_list'),
    path('create/', ProductCreateView.as_view(), name='product_create'),
    path('<int:pk>/', ProductDetailView.as_view(), name='product_detail'),
    path('<int:pk>/edit/', ProductUpdateView.as_view(), name='product_edit'),
    path('<int:pk>/adjust-stock/', ProductStockAdjustmentView.as_view(), name='product_stock_adjust'),

    # Central IMEI & Serial Registry (Section 7)
    path('imei-registry/', ItemInstanceListView.as_view(), name='imei_registry'),

    # Units of Measurement (UOM)
    path('units/', UnitListView.as_view(), name='unit_list'),
    path('units/create/', UnitCreateView.as_view(), name='unit_create'),
    path('units/<int:pk>/edit/', UnitUpdateView.as_view(), name='unit_edit'),
    path('api/units/quick-create/', UnitQuickCreateAPIView.as_view(), name='unit_quick_create_api'),

    # Category & SubCategory Management
    path('categories/', CategoryListView.as_view(), name='category_list'),
    path('categories/create/', CategoryCreateView.as_view(), name='category_create'),
    path('categories/<int:pk>/edit/', CategoryUpdateView.as_view(), name='category_edit'),
    path('api/categories/quick-create/', CategoryQuickCreateAPIView.as_view(), name='category_quick_create_api'),
    path('api/subcategories/quick-create/', SubCategoryQuickCreateAPIView.as_view(), name='subcategory_quick_create_api'),

    # Brand Management & Quick Create API
    path('brands/', BrandListView.as_view(), name='brand_list'),
    path('brands/create/', BrandCreateView.as_view(), name='brand_create'),
    path('brands/<int:pk>/edit/', BrandUpdateView.as_view(), name='brand_edit'),
    path('api/brands/quick-create/', BrandQuickCreateAPIView.as_view(), name='brand_quick_create_api'),

    # Vendor RMA Claims
    path('rma/', VendorRMAListView.as_view(), name='vendor_rma_list'),
    path('rma/create/', VendorRMACreateView.as_view(), name='vendor_rma_create'),
    path('rma/<int:pk>/', VendorRMADetailView.as_view(), name='vendor_rma_detail'),

    # Excel Import Pipeline (3-Step Wizard)
    path('import/excel/', ProductExcelUploadView.as_view(), name='excel_import'),
    path('import/excel/map/', ProductExcelMappingView.as_view(), name='excel_map'),
    path('import/excel/process/', ProductExcelProcessAPIView.as_view(), name='excel_process'),
]