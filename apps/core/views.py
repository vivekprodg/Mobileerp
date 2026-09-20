from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP

from django.shortcuts import render
from django.views.generic import TemplateView, View, ListView
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.http import JsonResponse
from django.db.models import Sum, F, Q, Count, DecimalField, Value, Case, When
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.sales.models import SalesEstimate, SalesEstimateItem, SalesPaymentTransaction
from apps.customers.models import Customer
from apps.inventory.models import BranchStock, Product, ItemInstance
from apps.purchases.models import GoodsReceivedNote, Supplier
from apps.repairs.models import RepairTicket
from apps.branches.models import Branch
from apps.core.models import SystemConfiguration, AuditLog
from apps.core.utils.nepali_date_converter import ad_to_bs_string, bs_to_ad_date
from apps.core.utils.barcode_generator import BarcodeGenerator


class DashboardHomeView(LoginRequiredMixin, TemplateView):
    """
    Central Executive Dashboard Controller.
    Computes all 8 client-requested business metrics, tender collection breakdowns,
    and side-by-side short summaries of today's sales and purchases scoped
    to the active store outlet.
    """
    template_name = 'core/dashboard.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        active_branch = getattr(self.request, 'active_branch', None) or Branch.get_default_main_branch()
        today = timezone.now().date()

        # ---------------------------------------------------------------------
        # 1. TODAY'S SALES & BILLS COUNT (आजको कुल बिक्री)
        # ---------------------------------------------------------------------
        sales_qs = SalesEstimate.objects.filter(
            bill_date_ad=today,
            status__in=['COMPLETED', 'PARTIALLY_RETURNED']
        )
        if active_branch and not self.request.user.is_superuser:
            sales_qs = sales_qs.filter(branch=active_branch)

        sales_agg = sales_qs.aggregate(
            total_sales=Coalesce(Sum('grand_total'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_gross=Coalesce(Sum('subtotal'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_cogs=Coalesce(Sum('total_cost_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_profit=Coalesce(Sum('total_gross_profit'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            bills_count=Count('id')
        )

        daily_sales_total = sales_agg['total_sales']
        today_sales_count = sales_agg['bills_count']
        today_cogs_total = sales_agg['total_cogs']

        # ---------------------------------------------------------------------
        # 2. TODAY'S PURCHASES AMOUNT (आजको खरिद - GRN INWARD)
        # ---------------------------------------------------------------------
        grn_qs = GoodsReceivedNote.objects.filter(
            bill_date=today,
            status='RECEIVED'
        )
        if active_branch and not self.request.user.is_superuser:
            grn_qs = grn_qs.filter(branch=active_branch)

        grn_agg = grn_qs.aggregate(
            total_purchases=Coalesce(Sum('net_total_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_landed=Coalesce(Sum('total_landed_cost'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            paid_sum=Coalesce(Sum('paid_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            grn_count=Count('id')
        )

        today_purchase_total = grn_agg['total_purchases']
        today_grn_count = grn_agg['grn_count']
        today_purchase_paid = grn_agg['paid_sum']

        # ---------------------------------------------------------------------
        # 3. CURRENT STOCK VALUATION (Landed Cost & Retail MRP)
        # ---------------------------------------------------------------------
        stock_qs = BranchStock.objects.select_related('product', 'product__base_unit')
        if active_branch and not self.request.user.is_superuser:
            stock_qs = stock_qs.filter(branch=active_branch)

        current_stock_valuation = Decimal('0.00')
        current_stock_retail = Decimal('0.00')
        total_stock_quantity = Decimal('0.000')

        for bs in stock_qs:
            qty = bs.quantity or Decimal('0.000')
            total_stock_quantity += qty
            cost_rate = bs.product.purchase_price or Decimal('0.00')
            retail_rate = bs.product.selling_price or Decimal('0.00')
            current_stock_valuation += (qty * cost_rate)
            current_stock_retail += (qty * retail_rate)

        current_stock_margin = max(Decimal('0.00'), current_stock_retail - current_stock_valuation)

        # ---------------------------------------------------------------------
        # 4. TODAY'S REALIZED PROFIT & MARGIN % (आजको नाफा)
        # ---------------------------------------------------------------------
        today_profit = sales_agg['total_profit']
        if daily_sales_total > Decimal('0.00'):
            today_profit_margin_pct = (
                (today_profit / daily_sales_total) * Decimal('100.00')
            ).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
        else:
            today_profit_margin_pct = Decimal('0.0')

        # ---------------------------------------------------------------------
        # 5. CUSTOMER CREDIT / DUE (ग्राहक बाँकी / कुल उधारो)
        # ---------------------------------------------------------------------
        debtor_qs = Customer.objects.filter(current_credit_balance__gt=Decimal('0.00'), is_active=True)
        total_udhaari = debtor_qs.aggregate(
            total=Coalesce(Sum('current_credit_balance'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2)))
        )['total']
        debtor_customers_count = debtor_qs.count()

        # ---------------------------------------------------------------------
        # 6. SUPPLIER PAYABLE (सप्लायरलाई तिर्न बाँकी)
        # ---------------------------------------------------------------------
        supplier_due_qs = Supplier.objects.filter(current_balance__gt=Decimal('0.00'), is_active=True)
        supplier_payable_total = supplier_due_qs.aggregate(
            total=Coalesce(Sum('current_balance'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2)))
        )['total']
        active_suppliers_count = supplier_due_qs.count()

        # Count overdue suppliers based on credit period
        overdue_suppliers_count = 0
        for sup in supplier_due_qs:
            credit_days = sup.credit_period_days or 30
            if sup.last_purchase_date:
                if (today - sup.last_purchase_date).days > credit_days:
                    overdue_suppliers_count += 1

        # ---------------------------------------------------------------------
        # 7. TOTAL SMARTPHONES (IMEI UNITS) IN STOCK
        # ---------------------------------------------------------------------
        phones_qs = ItemInstance.objects.filter(status='IN_STOCK')
        if active_branch and not self.request.user.is_superuser:
            phones_qs = phones_qs.filter(branch=active_branch)
        total_phones_in_stock = phones_qs.count()

        # ---------------------------------------------------------------------
        # 8. LOW STOCK ALERT ITEMS COUNT & RADAR
        # ---------------------------------------------------------------------
        low_stock_qs = stock_qs.filter(
            quantity__lte=F('low_stock_threshold'),
            quantity__gt=Decimal('0.000')
        ).order_by('quantity')

        low_stock_count = low_stock_qs.count()
        low_stock_items = list(low_stock_qs[:5])

        # ---------------------------------------------------------------------
        # 9. TODAY'S SALES TENDER SPLITS (Cash, FonePay, eSewa, Credit)
        # ---------------------------------------------------------------------
        tender_agg = SalesPaymentTransaction.objects.filter(
            estimate__in=sales_qs
        ).aggregate(
            cash_sum=Coalesce(Sum(Case(When(payment_mode='CASH', then=F('amount')))), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            fonepay_sum=Coalesce(Sum(Case(When(payment_mode='FONEPAY', then=F('amount')))), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            esewa_sum=Coalesce(Sum(Case(When(payment_mode='ESEWA', then=F('amount')))), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            credit_sum=Coalesce(Sum(Case(When(payment_mode='CREDIT', then=F('amount')))), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2)))
        )

        today_cash_sales = tender_agg['cash_sum']
        today_fonepay_sales = tender_agg['fonepay_sum']
        today_esewa_sales = tender_agg['esewa_sum']
        today_credit_sales = tender_agg['credit_sum']

        # ---------------------------------------------------------------------
        # 10. RECENT SUMMARIES (TOP 5 RECENT SALES & PURCHASES)
        # ---------------------------------------------------------------------
        recent_sales = list(
            sales_qs.select_related('customer', 'cashier', 'salesperson')
            .prefetch_related('items__product')
            .order_by('-created_at')[:5]
        )

        recent_purchases = list(
            grn_qs.select_related('supplier', 'received_by')
            .order_by('-created_at')[:5]
        )

        # Workshop repair queue metric
        repair_qs = RepairTicket.objects.exclude(service_status__in=['DELIVERED', 'CANCELLED'])
        if active_branch and not self.request.user.is_superuser:
            repair_qs = repair_qs.filter(branch=active_branch)
        active_repairs_count = repair_qs.count()

        # ---------------------------------------------------------------------
        # PACKAGE ALL REQUIRED VARIABLES INTO CONTEXT
        # ---------------------------------------------------------------------
        context.update({
            # 8 Core Client KPIs
            'daily_sales_total': daily_sales_total,
            'today_sales_count': today_sales_count,
            'today_purchase_total': today_purchase_total,
            'today_grn_count': today_grn_count,
            'today_purchase_paid': today_purchase_paid,
            'current_stock_valuation': current_stock_valuation,
            'current_stock_retail': current_stock_retail,
            'current_stock_margin': current_stock_margin,
            'today_profit': today_profit,
            'today_profit_margin_pct': today_profit_margin_pct,
            'today_cogs_total': today_cogs_total,
            'total_udhaari': total_udhaari,
            'debtor_customers_count': debtor_customers_count,
            'supplier_payable_total': supplier_payable_total,
            'active_suppliers_count': active_suppliers_count,
            'overdue_suppliers_count': overdue_suppliers_count,
            'total_stock_quantity': total_stock_quantity,
            'total_phones_in_stock': total_phones_in_stock,
            'low_stock_count': low_stock_count,

            # Short Summaries & Feeds
            'today_cash_sales': today_cash_sales,
            'today_fonepay_sales': today_fonepay_sales,
            'today_esewa_sales': today_esewa_sales,
            'today_credit_sales': today_credit_sales,
            'recent_sales': recent_sales,
            'recent_purchases': recent_purchases,
            'low_stock_items': low_stock_items,
            'active_repair_count': active_repairs_count,
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