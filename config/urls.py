"""
Master URL Configuration for Mobile Shop & Optical Store ERP (Nepal Context).
"""

from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.views.generic import TemplateView, RedirectView
from django.http import HttpResponse

def service_worker_view(request):
    sw_path = settings.BASE_DIR / 'static' / 'serviceworker.js'
    if sw_path.exists():
        with open(sw_path, 'r', encoding='utf-8') as f:
            return HttpResponse(f.read(), content_type='application/javascript')
    return HttpResponse("// Service worker not found", content_type='application/javascript')

urlpatterns = [
    # Root Service Worker & Favicon
    path('sw.js', service_worker_view, name='sw_js'),
    path('serviceworker.js', service_worker_view, name='serviceworker_js'),
    path('favicon.ico', RedirectView.as_view(url='/static/images/icons/icon-192x192.png', permanent=True)),

    # Admin Control Panel
    path('admin/', admin.site.urls),

    # Core Dashboard & Utility APIs
    path('', include('apps.core.urls', namespace='core')),

    # User Accounts & Staff Roles
    path('users/', include('apps.users.urls', namespace='users')),

    # Multi-Branch & Store Outlets
    path('branches/', include('apps.branches.urls', namespace='branches')),

    # Customer Directory & Udhaari Books
    path('customers/', include('apps.customers.urls', namespace='customers')),

    # Inventory Catalog & Warehouse Stocks
    path('inventory/', include('apps.inventory.urls', namespace='inventory')),

    # Product Barcodes & Label Generation
    path('products/', include('apps.products.urls', namespace='products')),

    # Purchases, Suppliers & GRN Inward
    path('purchases/', include('apps.purchases.urls', namespace='purchases')),

    # POS Register Sessions & Hold Carts
    path('pos-session/', include('apps.pos.urls', namespace='pos')),

    # Sales POS Billing Terminal
    path('sales/', include('apps.sales.urls', namespace='sales')),

    # Dedicated Service, Repair & Warranty Workshop
    path('repairs/', include('apps.repairs.urls', namespace='repairs')),

    # Financial Reports & Valuation Analytics
    path('reports/', include('apps.reports.urls', namespace='reports')),

    # Proforma Taxation & Annex Books
    path('taxation/', include('apps.taxation.urls', namespace='taxation')),

    # PWA Offline Screen
    path('offline/', TemplateView.as_view(template_name='offline.html'), name='offline'),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)

admin.site.site_header = f"{settings.SHOP_NAME} - Administration"
admin.site.site_title = "Mobile Shop ERP"
admin.site.index_title = "Store Management & Inventory Control"