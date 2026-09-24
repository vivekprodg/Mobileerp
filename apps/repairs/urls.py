from django.urls import path
from apps.repairs.views import (
    RepairDashboardView,
    RepairTicketCreateView,
    RepairTicketDetailView,
    RepairDiagnosticsVerdictView,
    RepairSparePartInstallView,
    RepairStatusUpdateView,
    RepairTicketSearchAPIView,
    CustomerQuotationApprovalView,
    PublicTrackingPortalView,
    RepairClaimTokenPrintView,
    RepairJobSheetA4PrintView,
    OpticalServiceDeskView
)

app_name = 'repairs'

urlpatterns = [
    # Workshop Queue & Intake
    path('', RepairDashboardView.as_view(), name='dashboard'),
    path('intake/', RepairTicketCreateView.as_view(), name='ticket_create'),
    path('<int:pk>/', RepairTicketDetailView.as_view(), name='ticket_detail'),

    # Technician Workbench Actions
    path('<int:pk>/verdict/', RepairDiagnosticsVerdictView.as_view(), name='diagnostics_verdict'),
    path('<int:pk>/install-part/', RepairSparePartInstallView.as_view(), name='install_part'),
    path('<int:pk>/status-update/', RepairStatusUpdateView.as_view(), name='status_update'),

    # Printable Slips & Job Sheets
    path('<int:pk>/claim-token/', RepairClaimTokenPrintView.as_view(), name='claim_token_print'),
    path('<int:pk>/job-sheet/', RepairJobSheetA4PrintView.as_view(), name='job_sheet_print'),

    # Customer Quote Approval & Public Tracking
    path('quote/<str:token>/', CustomerQuotationApprovalView.as_view(), name='customer_quote_approval'),
    path('track/', PublicTrackingPortalView.as_view(), name='public_tracking'),
    path('track/<str:ticket_no>/', PublicTrackingPortalView.as_view(), name='public_tracking_direct'),

    # Optical & Eyewear Service Desk
    path('optical/', OpticalServiceDeskView.as_view(), name='optical_desk'),

    # POS / Workshop Search API Endpoint
    path('api/tickets/search/', RepairTicketSearchAPIView.as_view(), name='ticket_search_api'),
]