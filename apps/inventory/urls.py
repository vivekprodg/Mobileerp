from django.urls import path
from apps.inventory.views import (
    # Catalog Views
    ProductListView,
    ProductDetailView,
    ProductCreateView,
    ProductUpdateView,
    ProductStockAdjustmentView,
    ProductUnitConversionCreateView,

    # Dashboard Asynchronous & Modal Action Views
    DashboardStockAdjustmentView,
    DashboardStockTransferView,
    StockMovementMetricsAPIView,
    InventoryExportCSVView,

    # Central IMEI & Serial Registry View
    ItemInstanceListView,

    # Units of Measurement (UOM) Views
    UnitListView,
    UnitCreateView,
    UnitUpdateView,
    UnitQuickCreateAPIView,
    UnitSearchAPIView,

    # Category & SubCategory Views
    CategoryListView,
    CategoryCreateView,
    CategoryUpdateView,
    CategoryQuickCreateAPIView,
    CategorySearchAPIView,
    SubCategoryQuickCreateAPIView,
    SubCategorySearchAPIView,

    # Brand Views
    BrandListView,
    BrandCreateView,
    BrandUpdateView,
    BrandQuickCreateAPIView,
    BrandSearchAPIView,

    # Vendor RMA Claims Views
    VendorRMAListView,
    VendorRMACreateView,
    VendorRMADetailView,

    # Excel / CSV Importer Pipeline Views
    ProductExcelUploadView,
    ProductExcelMappingView,
    ProductExcelProcessAPIView,
)

app_name = 'inventory'

urlpatterns = [
    # =========================================================================
    # 1. CATALOG MANAGEMENT & PRODUCT OVERVIEWS
    # =========================================================================
    path('', ProductListView.as_view(), name='product_list'),
    path('create/', ProductCreateView.as_view(), name='product_create'),
    path('<int:pk>/', ProductDetailView.as_view(), name='product_detail'),
    path('<int:pk>/edit/', ProductUpdateView.as_view(), name='product_edit'),
    path('<int:pk>/adjust-stock/', ProductStockAdjustmentView.as_view(), name='product_stock_adjust'),
    path('<int:pk>/add-conversion/', ProductUnitConversionCreateView.as_view(), name='product_add_conversion'),
    path('<int:pk>/conversion/add/', ProductUnitConversionCreateView.as_view(), name='product_unit_conversion_create'),

    # =========================================================================
    # 2. DASHBOARD MODALS & REAL-TIME AJAX ENDPOINTS
    # =========================================================================
    # Movement Metrics Endpoint for Period Switcher (7D, 30D, 3M)
    path('api/stock-movement/', StockMovementMetricsAPIView.as_view(), name='stock_movement_metrics_api'),
    path('api/movement-metrics/', StockMovementMetricsAPIView.as_view(), name='movement_metrics_api_alias'),

    # Modal Stock Adjustment Endpoint (Direct from Dashboard Table/Modal)
    path('adjust-stock/modal/', DashboardStockAdjustmentView.as_view(), name='dashboard_stock_adjust'),

    # Modal Inter-Warehouse Stock Transfer Endpoint
    path('transfer-stock/modal/', DashboardStockTransferView.as_view(), name='dashboard_stock_transfer'),

    # Filtered Catalog Dataset CSV Export
    path('export/csv/', InventoryExportCSVView.as_view(), name='inventory_export_csv'),

    # =========================================================================
    # 3. CENTRAL IMEI & SERIAL NUMBER REGISTRY
    # =========================================================================
    path('imei-registry/', ItemInstanceListView.as_view(), name='imei_registry'),

    # =========================================================================
    # 4. UNITS OF MEASUREMENT (UOM)
    # =========================================================================
    path('units/', UnitListView.as_view(), name='unit_list'),
    path('units/create/', UnitCreateView.as_view(), name='unit_create'),
    path('units/<int:pk>/edit/', UnitUpdateView.as_view(), name='unit_edit'),
    path('api/units/quick-create/', UnitQuickCreateAPIView.as_view(), name='unit_quick_create_api'),
    path('api/units/search/', UnitSearchAPIView.as_view(), name='unit_search_api'),

    # =========================================================================
    # 5. CATEGORIES & SUBCATEGORIES
    # =========================================================================
    path('categories/', CategoryListView.as_view(), name='category_list'),
    path('categories/create/', CategoryCreateView.as_view(), name='category_create'),
    path('categories/<int:pk>/edit/', CategoryUpdateView.as_view(), name='category_edit'),
    path('api/categories/quick-create/', CategoryQuickCreateAPIView.as_view(), name='category_quick_create_api'),
    path('api/categories/search/', CategorySearchAPIView.as_view(), name='category_search_api'),
    path('api/subcategories/quick-create/', SubCategoryQuickCreateAPIView.as_view(), name='subcategory_quick_create_api'),
    path('api/subcategories/search/', SubCategorySearchAPIView.as_view(), name='subcategory_search_api'),

    # =========================================================================
    # 6. BRANDS
    # =========================================================================
    path('brands/', BrandListView.as_view(), name='brand_list'),
    path('brands/create/', BrandCreateView.as_view(), name='brand_create'),
    path('brands/<int:pk>/edit/', BrandUpdateView.as_view(), name='brand_edit'),
    path('api/brands/quick-create/', BrandQuickCreateAPIView.as_view(), name='brand_quick_create_api'),
    path('api/brands/search/', BrandSearchAPIView.as_view(), name='brand_search_api'),

    # =========================================================================
    # 7. VENDOR RMA CLAIMS & DISTRIBUTOR RETURNS
    # =========================================================================
    path('rma/', VendorRMAListView.as_view(), name='vendor_rma_list'),
    path('rma/create/', VendorRMACreateView.as_view(), name='vendor_rma_create'),
    path('rma/<int:pk>/', VendorRMADetailView.as_view(), name='vendor_rma_detail'),

    # =========================================================================
    # 8. SPREADSHEET (EXCEL/CSV) IMPORT WIZARD
    # =========================================================================
    path('import/excel/', ProductExcelUploadView.as_view(), name='excel_import'),
    path('import/excel/map/', ProductExcelMappingView.as_view(), name='excel_map'),
    path('import/excel/process/', ProductExcelProcessAPIView.as_view(), name='excel_process'),
]