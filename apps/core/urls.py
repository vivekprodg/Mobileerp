"""
Core Application Routing Configuration.

Routes:
- Root dashboard and alternate alias paths.
- System settings and forensic audit logs.
- High-speed AJAX APIs for date conversions, barcode previews, and universal search.
"""

from django.urls import path
from apps.core.views import (
    DashboardHomeView,
    SystemSettingsView,
    AuditLogListView,
    ConvertDateAPIView,
    BarcodePreviewAPIView,
    GlobalSearchAPIView
)

app_name = 'core'

urlpatterns = [
    # 1. Primary Owner Executive Dashboard Routes
    path('', DashboardHomeView.as_view(), name='dashboard'),
    path('dashboard/', DashboardHomeView.as_view(), name='dashboard_alias'),

    # 2. System Settings & Governance
    path('settings/', SystemSettingsView.as_view(), name='settings'),
    path('audit-logs/', AuditLogListView.as_view(), name='audit_logs'),

    # 3. Utilities & Global Search API Endpoints (Standard Paths)
    path('api/convert-date/', ConvertDateAPIView.as_view(), name='convert_date_api'),
    path('api/barcode-preview/', BarcodePreviewAPIView.as_view(), name='barcode_preview_api'),
    path('api/global-search/', GlobalSearchAPIView.as_view(), name='global_search_api'),

    # 4. Compatibility Aliases (Supports frontend requests prefixed with /core/)
    path('core/api/convert-date/', ConvertDateAPIView.as_view(), name='convert_date_api_alias'),
    path('core/api/barcode-preview/', BarcodePreviewAPIView.as_view(), name='barcode_preview_api_alias'),
    path('core/api/global-search/', GlobalSearchAPIView.as_view(), name='global_search_api_alias'),
]