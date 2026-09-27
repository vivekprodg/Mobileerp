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
    path('', DashboardHomeView.as_view(), name='dashboard'),
    path('settings/', SystemSettingsView.as_view(), name='settings'),
    path('audit-logs/', AuditLogListView.as_view(), name='audit_logs'),
    path('api/convert-date/', ConvertDateAPIView.as_view(), name='convert_date_api'),
    path('api/barcode-preview/', BarcodePreviewAPIView.as_view(), name='barcode_preview_api'),
    path('api/global-search/', GlobalSearchAPIView.as_view(), name='global_search_api'),
]