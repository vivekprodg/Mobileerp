from datetime import date
from decimal import Decimal
from django.shortcuts import render
from django.views.generic import TemplateView, View, ListView
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.http import JsonResponse
from django.db.models import Sum, F, Q

from apps.sales.models import SalesEstimate
from apps.customers.models import Customer
from apps.inventory.models import BranchStock
from apps.repairs.models import RepairTicket
from apps.branches.models import Branch
from apps.core.models import SystemConfiguration, AuditLog
from apps.core.utils.nepali_date_converter import ad_to_bs_string, bs_to_ad_date
from apps.core.utils.barcode_generator import BarcodeGenerator


class DashboardHomeView(LoginRequiredMixin, TemplateView):
    """
    Central dynamic dashboard providing real-time KPI metrics, quick launchpad,
    and financial health indicators scoped to the current active branch.
    """
    template_name = 'core/dashboard.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        active_branch = getattr(self.request, 'active_branch', None) or Branch.get_default_main_branch()
        today = date.today()

        # 1. Today's Completed Sales Total (Scoped to branch)
        sales_qs = SalesEstimate.objects.filter(bill_date_ad=today, status='COMPLETED')
        if active_branch and not self.request.user.is_superuser:
            sales_qs = sales_qs.filter(branch=active_branch)

        daily_sales = sales_qs.aggregate(total=Sum('grand_total'))['total'] or Decimal('0.00')

        # 2. Total Outstanding Customer Udhaari (Global Debt)
        total_udhaari = Customer.objects.aggregate(total=Sum('current_credit_balance'))['total'] or Decimal('0.00')

        # 3. Low Stock Items Count (Scoped to branch)
        low_stock_qs = BranchStock.objects.filter(quantity__lte=F('low_stock_threshold'))
        if active_branch and not self.request.user.is_superuser:
            low_stock_qs = low_stock_qs.filter(branch=active_branch)

        low_stock_count = low_stock_qs.count()

        # 4. Active Repair Workshop Queue Count
        repair_qs = RepairTicket.objects.exclude(service_status__in=['DELIVERED', 'CANCELLED'])
        if active_branch and not self.request.user.is_superuser:
            repair_qs = repair_qs.filter(branch=active_branch)

        active_repairs = repair_qs.count()

        context.update({
            'page_title': "Dashboard | Mobile & Optical ERP",
            'daily_sales_total': daily_sales,
            'total_udhaari': total_udhaari,
            'low_stock_count': low_stock_count,
            'active_repair_count': active_repairs,
            'recent_sales': sales_qs.select_related('customer', 'cashier').order_by('-created_at')[:5],
            'recent_repairs': repair_qs.select_related('product', 'technician').order_by('-created_at')[:5],
        })
        return context


class AuditLogListView(LoginRequiredMixin, UserPassesTestMixin, ListView):
    """
    Forensic security audit log viewer accessible exclusively to Store Owners and Superusers.
    Displays immutable records of supervisor PIN overrides, price reductions, bill voids,
    manual stock alterations, and failed login events.
    """
    model = AuditLog
    template_name = 'core/audit_logs.html'
    context_object_name = 'logs'
    paginate_by = 30

    def test_func(self):
        user = self.request.user
        return user.is_authenticated and (user.is_superuser or getattr(user, 'role', '') == 'OWNER')

    def get_queryset(self):
        qs = AuditLog.objects.select_related('user', 'branch')

        q = self.request.GET.get('q', '').strip()
        action_type = self.request.GET.get('action_type', '').strip()
        module_name = self.request.GET.get('module', '').strip()
        branch_id = self.request.GET.get('branch', '').strip()
        start_date = self.request.GET.get('start_date', '').strip()
        end_date = self.request.GET.get('end_date', '').strip()

        if q:
            qs = qs.filter(
                Q(object_repr__icontains=q) |
                Q(user__username__icontains=q) |
                Q(module__icontains=q) |
                Q(ip_address__icontains=q)
            )

        if action_type:
            qs = qs.filter(action_type=action_type)

        if module_name:
            qs = qs.filter(module=module_name)

        if branch_id:
            qs = qs.filter(branch_id=branch_id)

        if start_date:
            qs = qs.filter(timestamp__date__gte=start_date)

        if end_date:
            qs = qs.filter(timestamp__date__lte=end_date)

        return qs.order_by('-timestamp')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update({
            'branches': Branch.objects.filter(is_active=True).order_by('name'),
            'action_types': AuditLog.ACTION_CHOICES,
            'total_logs_count': AuditLog.objects.count()
        })
        return context


class ConvertDateAPIView(LoginRequiredMixin, View):
    """AJAX helper to instantly convert between AD and BS dates."""

    def get(self, request, *args, **kwargs):
        action = request.GET.get('action', 'ad_to_bs')
        date_val = request.GET.get('date')

        if not date_val:
            return JsonResponse({'status': 'error', 'message': 'No date provided'}, status=400)

        try:
            if action == 'ad_to_bs':
                from datetime import datetime
                parsed_ad = datetime.strptime(date_val, '%Y-%m-%d').date()
                bs_en = ad_to_bs_string(parsed_ad, lang='en')
                bs_np = ad_to_bs_string(parsed_ad, lang='np')
                return JsonResponse({'status': 'success', 'bs_en': bs_en, 'bs_np': bs_np})
            elif action == 'bs_to_ad':
                ad_date = bs_to_ad_date(date_val)
                return JsonResponse({'status': 'success', 'ad_date': ad_date.strftime('%Y-%m-%d')})
            return JsonResponse({'status': 'error', 'message': 'Unknown action'}, status=400)
        except Exception as err:
            return JsonResponse({'status': 'error', 'message': str(err)}, status=400)


class BarcodePreviewAPIView(LoginRequiredMixin, View):
    """Returns SVG and Base64 representations for instant POS sticker or modal preview."""

    def get(self, request, *args, **kwargs):
        code = request.GET.get('code', '').strip()
        if not code:
            return JsonResponse({'error': 'Code is required'}, status=400)

        svg_data = BarcodeGenerator.generate_code128_svg(code)
        png_data = BarcodeGenerator.generate_code128_base64_png(code)
        qr_data = BarcodeGenerator.generate_qr_base64(code)

        return JsonResponse({
            'code': code,
            'svg': svg_data,
            'png_base64': png_data,
            'qr_base64': qr_data
        })