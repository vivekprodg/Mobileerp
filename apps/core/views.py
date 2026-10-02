import logging
import json
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

from django.shortcuts import render, redirect
from django.views.generic import TemplateView, View, ListView
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib import messages
from django.http import JsonResponse
from django.urls import reverse, NoReverseMatch
from django.db.models import Sum, F, Q, Count, DecimalField, Value, Case, When, ExpressionWrapper
from django.db.models.functions import Coalesce, TruncMonth
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.core.cache import cache

from apps.sales.models import SalesEstimate, SalesEstimateItem, SalesPaymentTransaction
from apps.customers.models import Customer
from apps.inventory.models import BranchStock, Product, ItemInstance
from apps.purchases.models import GoodsReceivedNote, Supplier
from apps.repairs.models import RepairTicket
from apps.branches.models import Branch
from apps.core.models import SystemConfiguration, AuditLog
from apps.core.utils.nepali_date_converter import ad_to_bs_string, bs_to_ad_date
from apps.core.utils.barcode_generator import BarcodeGenerator

logger = logging.getLogger(__name__)

def safe_url(view_candidates, pk=None, default_path='#', **kwargs):
    """
    Safely resolves Django view names without throwing NoReverseMatch exceptions.
    Attempts multiple view names in order and falls back to default_path.
    """
    if isinstance(view_candidates, str):
        view_candidates = [view_candidates]

    for vn in view_candidates:
        try:
            if pk is not None:
                return reverse(vn, args=[pk])
            elif kwargs:
                return reverse(vn, kwargs=kwargs)
            else:
                return reverse(vn)
        except NoReverseMatch:
            continue
    return default_path

class SystemSettingsView(LoginRequiredMixin, UserPassesTestMixin, View):
    """
    System & Hardware Configuration Controller.
    Allows Store Owners and Superusers to update shop branding, tax parameters,
    thermal printer width, cashier backdating permissions, and master IMEI tracking rules.
    Atomically writes immutable AuditLog records upon policy transitions.
    """
    template_name = 'core/settings.html'

    def test_func(self):
        user = self.request.user
        return user.is_authenticated and (user.is_superuser or getattr(user, 'role', '') in ['OWNER', 'MANAGER'])

    def handle_no_permission(self):
        messages.error(self.request, _("Access Restricted: Only Store Owners and Managers can access system settings."))
        return redirect('core:dashboard')

    def get(self, request, *args, **kwargs):
        config = SystemConfiguration.get_solo()
        return render(request, self.template_name, {
            'config': config,
            'SYS_CONFIG': config
        })

    def post(self, request, *args, **kwargs):
        config = SystemConfiguration.get_solo()
        post_data = request.POST

        company_name_en = post_data.get('company_name_en', '').strip()
        company_name_np = post_data.get('company_name_np', '').strip()
        pan_number = post_data.get('pan_number', '').strip()
        raw_vat_rate = post_data.get('default_vat_rate', '0.00').strip()
        printer_width = post_data.get('thermal_printer_paper_width', '80mm').strip()
        bill_title = post_data.get('bill_header_title', '').strip()
        bill_disclaimer = post_data.get('bill_estimate_disclaimer', '').strip()

        # Parse operational backdating and master IMEI tracking booleans
        new_allow_backdating = post_data.get('allow_cashier_backdating') in ['true', 'on', '1', True]
        prev_allow_backdating = getattr(config, 'allow_cashier_backdating', False)

        new_enforce_imei = post_data.get('enforce_imei_tracking') in ['true', 'on', '1', True]
        prev_enforce_imei = config.enforce_imei_tracking

        if not company_name_en:
            messages.error(request, _("Company Name (English) is required."))
            return self.get(request, *args, **kwargs)

        try:
            default_vat_rate = Decimal(raw_vat_rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        except (InvalidOperation, ValueError, TypeError):
            default_vat_rate = Decimal('0.00')

        active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()

        # Detect and audit Staff Backdating Policy change
        if prev_allow_backdating != new_allow_backdating:
            backdate_policy_str = "Enabled / Unrestricted Backlog Mode" if new_allow_backdating else "Strict / Supervisor PIN Required"
            AuditLog.objects.create(
                user=request.user,
                branch=active_branch,
                action_type='UPDATE',
                module='SystemConfiguration',
                object_repr="Staff Backdating Policy",
                ip_address=request.META.get('REMOTE_ADDR'),
                details={
                    'previous_allow_cashier_backdating': prev_allow_backdating,
                    'new_allow_cashier_backdating': new_allow_backdating,
                    'policy_state': backdate_policy_str,
                    'summary': f"Store owner changed cashier backdating policy to [{backdate_policy_str}]."
                }
            )

        # Detect and audit Master IMEI Enforcement Policy change
        if prev_enforce_imei != new_enforce_imei:
            imei_policy_str = "Strict / Mandatory Serialized" if new_enforce_imei else "Relaxed / Backlog Mode"
            AuditLog.objects.create(
                user=request.user,
                branch=active_branch,
                action_type='UPDATE',
                module='SystemConfiguration',
                object_repr="IMEI Enforcement Policy",
                ip_address=request.META.get('REMOTE_ADDR'),
                details={
                    'previous_enforce_imei': prev_enforce_imei,
                    'new_enforce_imei': new_enforce_imei,
                    'policy_state': imei_policy_str,
                    'summary': f"Store owner changed IMEI requirement policy to [{imei_policy_str}]."
                }
            )

        # Update and persist configuration attributes
        config.company_name_en = company_name_en
        config.company_name_np = company_name_np
        config.pan_number = pan_number or None
        config.default_vat_rate = default_vat_rate
        config.thermal_printer_paper_width = printer_width
        config.bill_header_title = bill_title or "SALES ESTIMATE SLIP"
        config.bill_estimate_disclaimer = bill_disclaimer
        config.allow_cashier_backdating = new_allow_backdating
        config.enforce_imei_tracking = new_enforce_imei
        config.save()

        messages.success(request, _("System configuration policies updated successfully."))
        return redirect('core:settings')

class DashboardHomeView(LoginRequiredMixin, TemplateView):
    """
    Central Executive Dashboard Controller.
    Aggregates the 10 vital owner metrics: Sales, Purchases, Gross Profit, Liquid Cash/Bank,
    Inventory Valuation, Receivables, Payables, Low Stock Radar, Daily/Monthly Trends, and
    Fastest-Selling Counter Products.
    """
    template_name = 'core/dashboard.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        active_branch = getattr(self.request, 'active_branch', None) or Branch.get_default_main_branch()
        today = timezone.now().date()
        branch_id = active_branch.id if active_branch else 0
        is_super = self.request.user.is_superuser

        # ---------------------------------------------------------------------
        # CACHE LAYER (60s Cache Key Per Scope)
        # ---------------------------------------------------------------------
        cache_key = f"dashboard_kpis_branch_{branch_id}_{today.isoformat()}_{is_super}"
        cached_data = cache.get(cache_key)

        if cached_data is not None:
            context.update(cached_data)
            return context

        # ---------------------------------------------------------------------
        # 1. TODAY'S SALES & BILLS COUNT (Database Aggregation)
        # ---------------------------------------------------------------------
        sales_base_qs = SalesEstimate.objects.filter(
            status__in=['COMPLETED', 'PARTIALLY_RETURNED']
        )
        if active_branch and not is_super:
            sales_base_qs = sales_base_qs.filter(branch=active_branch)

        sales_qs = sales_base_qs.filter(bill_date_ad=today)

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
        today_profit = sales_agg['total_profit']

        if daily_sales_total > Decimal('0.00'):
            today_profit_margin_pct = (
                (today_profit / daily_sales_total) * Decimal('100.00')
            ).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
        else:
            today_profit_margin_pct = Decimal('0.0')

        # ---------------------------------------------------------------------
        # 2. TODAY'S PURCHASES AMOUNT (GRN INWARD - Database Aggregation)
        # ---------------------------------------------------------------------
        grn_base_qs = GoodsReceivedNote.objects.filter(status='RECEIVED')
        if active_branch and not is_super:
            grn_base_qs = grn_base_qs.filter(branch=active_branch)

        grn_qs = grn_base_qs.filter(bill_date=today)

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
        # 3. CURRENT STOCK VALUATION (DATABASE AGGREGATION)
        # ---------------------------------------------------------------------
        stock_qs = BranchStock.objects.filter(product__is_active=True)
        if active_branch and not is_super:
            stock_qs = stock_qs.filter(branch=active_branch)

        cost_val_expr = ExpressionWrapper(
            F('quantity') * F('product__purchase_price'),
            output_field=DecimalField(max_digits=18, decimal_places=2)
        )
        retail_val_expr = ExpressionWrapper(
            F('quantity') * F('product__selling_price'),
            output_field=DecimalField(max_digits=18, decimal_places=2)
        )

        stock_agg = stock_qs.aggregate(
            total_qty=Coalesce(Sum('quantity'), Value(Decimal('0.000'), output_field=DecimalField(max_digits=18, decimal_places=3))),
            total_cost_val=Coalesce(Sum(cost_val_expr), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_retail_val=Coalesce(Sum(retail_val_expr), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2)))
        )

        total_stock_quantity = stock_agg['total_qty']
        current_stock_valuation = stock_agg['total_cost_val']
        current_stock_retail = stock_agg['total_retail_val']
        current_stock_margin = max(Decimal('0.00'), current_stock_retail - current_stock_valuation)

        # ---------------------------------------------------------------------
        # 4. CUSTOMER CREDIT / DUE (UDHAARI)
        # ---------------------------------------------------------------------
        debtor_qs = Customer.objects.filter(current_credit_balance__gt=Decimal('0.00'), is_active=True)
        debtor_agg = debtor_qs.aggregate(
            total=Coalesce(Sum('current_credit_balance'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            count=Count('id')
        )
        total_udhaari = debtor_agg['total']
        debtor_customers_count = debtor_agg['count']

        # ---------------------------------------------------------------------
        # 5. SUPPLIER PAYABLE & OVERDUE (LIGHTWEIGHT DATABASE TUPLES)
        # ---------------------------------------------------------------------
        supplier_due_qs = Supplier.objects.filter(current_balance__gt=Decimal('0.00'), is_active=True)
        supplier_agg = supplier_due_qs.aggregate(
            total=Coalesce(Sum('current_balance'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            count=Count('id')
        )
        supplier_payable_total = supplier_agg['total']
        active_suppliers_count = supplier_agg['count']

        supplier_dates = supplier_due_qs.filter(last_purchase_date__isnull=False).values_list('last_purchase_date', 'credit_period_days')
        overdue_suppliers_count = sum(
            1 for lp_date, credit_days in supplier_dates
            if (today - lp_date).days > (credit_days or 30)
        )

        # Stock coverage percentage of debt
        if supplier_payable_total > Decimal('0.00'):
            stock_coverage_pct = ((current_stock_valuation / supplier_payable_total) * Decimal('100.00')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
        else:
            stock_coverage_pct = Decimal('100.0') if current_stock_valuation > Decimal('0.00') else Decimal('0.0')

        net_stock_buffer = current_stock_valuation - supplier_payable_total

        # ---------------------------------------------------------------------
        # 6. TOTAL SMARTPHONES (IMEI UNITS) IN STOCK
        # ---------------------------------------------------------------------
        phones_qs = ItemInstance.objects.filter(status='IN_STOCK')
        if active_branch and not is_super:
            phones_qs = phones_qs.filter(branch=active_branch)
        total_phones_in_stock = phones_qs.count()

        # ---------------------------------------------------------------------
        # 7. LOW STOCK ALERT ITEMS COUNT & RADAR
        # ---------------------------------------------------------------------
        low_stock_qs = stock_qs.filter(
            quantity__lte=F('low_stock_threshold'),
            quantity__gt=Decimal('0.000')
        ).select_related('product', 'product__category', 'product__base_unit').order_by('quantity')

        low_stock_count = low_stock_qs.count()
        low_stock_items = list(low_stock_qs[:5])

        # ---------------------------------------------------------------------
        # 8. TODAY'S SALES TENDER SPLITS
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
        # 9. CASH & BANK LIQUIDITY AGGREGATION (FIELD-SAFE QUERY)
        # ---------------------------------------------------------------------
        cash_in_hand_total = Decimal('0.00')
        bank_balance_total = Decimal('0.00')
        try:
            from apps.accounting.models import Account
            acc_qs = Account.objects.filter(is_active=True)
            if hasattr(Account, 'branch') and active_branch and not is_super:
                acc_qs = acc_qs.filter(Q(branch=active_branch) | Q(branch__isnull=True))

            # Query exclusively on confirmed fields: system_tag and name
            cash_filter = Q(system_tag__icontains='CASH') | Q(name__icontains='Cash')
            bank_filter = Q(system_tag__icontains='BANK') | Q(name__icontains='Bank')

            cash_in_hand_total = acc_qs.filter(cash_filter).aggregate(
                s=Coalesce(Sum('current_balance'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2)))
            )['s']
            bank_balance_total = acc_qs.filter(bank_filter).aggregate(
                s=Coalesce(Sum('current_balance'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2)))
            )['s']
        except Exception as e:
            logger.warning(f"Error querying Chart of Accounts balances: {e}")

        # Fallback to tender receipts if chart of accounts is unconfigured or zero
        if cash_in_hand_total == Decimal('0.00') and bank_balance_total == Decimal('0.00'):
            cash_in_hand_total = today_cash_sales
            bank_balance_total = today_fonepay_sales + today_esewa_sales

        total_cash_bank = cash_in_hand_total + bank_balance_total

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

        repair_qs = RepairTicket.objects.exclude(service_status__in=['DELIVERED', 'CANCELLED'])
        if active_branch and not is_super:
            repair_qs = repair_qs.filter(branch=active_branch)
        active_repairs_count = repair_qs.count()

        # ---------------------------------------------------------------------
        # 11. TOP-SELLING PRODUCTS (LAST 30 DAYS)
        # ---------------------------------------------------------------------
        thirty_days_ago = today - timedelta(days=30)
        recent_items_qs = SalesEstimateItem.objects.filter(
            estimate__bill_date_ad__gte=thirty_days_ago,
            estimate__status__in=['COMPLETED', 'PARTIALLY_RETURNED']
        )
        if active_branch and not is_super:
            recent_items_qs = recent_items_qs.filter(estimate__branch=active_branch)

        # Inspect SalesEstimateItem field schema safely
        item_fields = {f.name for f in SalesEstimateItem._meta.get_fields()}
        if 'line_total' in item_fields:
            amount_expr = F('line_total')
        elif 'total_amount' in item_fields:
            amount_expr = F('total_amount')
        elif 'rate' in item_fields:
            amount_expr = F('quantity') * F('rate')
        else:
            amount_expr = F('quantity') * F('unit_price')

        top_selling_products = list(
            recent_items_qs.values(
                'product__id',
                'product__name',
                'product__sku',
                'product__category__name'
            ).annotate(
                units_sold=Coalesce(Sum('quantity'), Value(Decimal('0'))),
                revenue=Coalesce(Sum(amount_expr), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2)))
            ).order_by('-revenue')[:5]
        )

        # ---------------------------------------------------------------------
        # 12. SALES & PURCHASE TRENDS (DAILY & MONTHLY SERIALIZATION)
        # ---------------------------------------------------------------------
        # (A) Daily Trend: Last 7 Days
        start_date_7d = today - timedelta(days=6)
        sales_7d_dict = dict(
            sales_base_qs.filter(bill_date_ad__gte=start_date_7d, bill_date_ad__lte=today)
            .values('bill_date_ad')
            .annotate(total=Sum('grand_total'))
            .values_list('bill_date_ad', 'total')
        )
        grn_7d_dict = dict(
            grn_base_qs.filter(bill_date__gte=start_date_7d, bill_date__lte=today)
            .values('bill_date')
            .annotate(total=Sum('net_total_amount'))
            .values_list('bill_date', 'total')
        )

        daily_labels = []
        daily_sales_data = []
        daily_purchase_data = []
        for i in range(6, -1, -1):
            d = today - timedelta(days=i)
            lbl = 'Today' if i == 0 else d.strftime('%a')
            daily_labels.append(lbl)
            daily_sales_data.append(float(sales_7d_dict.get(d) or 0))
            daily_purchase_data.append(float(grn_7d_dict.get(d) or 0))

        # (B) Monthly Trend: Last 6 Calendar Months
        months_list = []
        cur_year, cur_month = today.year, today.month
        for i in range(5, -1, -1):
            m = cur_month - i
            y = cur_year
            while m <= 0:
                m += 12
                y -= 1
            months_list.append((y, m))

        first_month_date = date(months_list[0][0], months_list[0][1], 1)
        sales_monthly_dict = dict(
            sales_base_qs.filter(bill_date_ad__gte=first_month_date)
            .annotate(month=TruncMonth('bill_date_ad'))
            .values('month')
            .annotate(total=Sum('grand_total'))
            .values_list('month', 'total')
        )
        grn_monthly_dict = dict(
            grn_base_qs.filter(bill_date__gte=first_month_date)
            .annotate(month=TruncMonth('bill_date'))
            .values('month')
            .annotate(total=Sum('net_total_amount'))
            .values_list('month', 'total')
        )

        monthly_labels = []
        monthly_sales_data = []
        monthly_purchase_data = []
        for y, m in months_list:
            dt_key = date(y, m, 1)
            monthly_labels.append(dt_key.strftime('%b %Y'))
            s_val = sum(v for k, v in sales_monthly_dict.items() if k and k.year == y and k.month == m)
            p_val = sum(v for k, v in grn_monthly_dict.items() if k and k.year == y and k.month == m)
            monthly_sales_data.append(float(s_val or 0))
            monthly_purchase_data.append(float(p_val or 0))

        # ---------------------------------------------------------------------
        # PACKAGE & COMMIT DATA DICTIONARY TO CACHE
        # ---------------------------------------------------------------------
        kpi_payload = {
            'active_branch': active_branch,
            'today_date_ad': today,
            'today_date_bs': ad_to_bs_string(today, lang='en'),
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
            'stock_coverage_pct': stock_coverage_pct,
            'net_stock_buffer': net_stock_buffer,
            'total_stock_quantity': total_stock_quantity,
            'total_phones_in_stock': total_phones_in_stock,
            'low_stock_count': low_stock_count,
            'today_cash_sales': today_cash_sales,
            'today_fonepay_sales': today_fonepay_sales,
            'today_esewa_sales': today_esewa_sales,
            'today_credit_sales': today_credit_sales,
            'total_cash_bank': total_cash_bank,
            'cash_in_hand_total': cash_in_hand_total,
            'bank_balance_total': bank_balance_total,
            'recent_sales': recent_sales,
            'recent_purchases': recent_purchases,
            'low_stock_items': low_stock_items,
            'active_repair_count': active_repairs_count,
            'top_selling_products': top_selling_products,
            'trend_daily_labels_json': json.dumps(daily_labels),
            'trend_daily_sales_json': json.dumps(daily_sales_data),
            'trend_daily_purchases_json': json.dumps(daily_purchase_data),
            'trend_monthly_labels_json': json.dumps(monthly_labels),
            'trend_monthly_sales_json': json.dumps(monthly_sales_data),
            'trend_monthly_purchases_json': json.dumps(monthly_purchase_data),
        }

        cache.set(cache_key, kpi_payload, timeout=60)
        context.update(kpi_payload)
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

class GlobalSearchAPIView(LoginRequiredMixin, View):
    """
    Unified Global Search API.
    Performs instantaneous multi-entity lookups across:
      1. Products (name, SKU, barcode, model)
      2. Handset IMEI Instances (IMEI 1, IMEI 2, Serial, UID)
      3. Customers (Name, Mobile, PAN)
      4. Suppliers (Company, Contact, Phone, PAN, Code)
      5. Sales Invoices / Estimates (Estimate #, Manual Customer, Phone, PAN)
      6. Goods Received Notes / GRN (GRN #, Supplier Bill #, Supplier Name)
      7. Repair Tickets (Ticket #, IMEI, Device, Customer)
      8. Chart of Accounts (Code, Name, System Tag)
      9. Staff Users (Username, Name, Mobile)
    Each query block is isolated in try-except safeguards so an error in one module
    never halts the search response.
    """

    def get(self, request, *args, **kwargs):
        q = request.GET.get('q', '').strip()
        if len(q) < 2:
            return JsonResponse({
                'status': 'success',
                'query': q,
                'total_count': 0,
                'categories': [],
                'results': []
            })

        active_branch = getattr(request, 'active_branch', None) or getattr(request.user, 'assigned_branch', None)
        is_owner_or_super = request.user.is_superuser or getattr(request.user, 'role', '') == 'OWNER'

        categories = []
        flat_results = []

        # ---------------------------------------------------------------------
        # 1. PRODUCTS
        # ---------------------------------------------------------------------
        try:
            prod_filter = Q(name__icontains=q) | Q(sku__icontains=q) | Q(barcode__icontains=q)
            if hasattr(Product, 'model_name'):
                prod_filter |= Q(model_name__icontains=q)
            if hasattr(Product, 'model_number'):
                prod_filter |= Q(model_number__icontains=q)

            products = Product.objects.filter(prod_filter, is_active=True).select_related('category', 'brand')[:5]
            if products.exists():
                prod_items = []
                for p in products:
                    detail_url = safe_url(
                        ['inventory:product_detail', 'inventory:product_edit'],
                        pk=p.id,
                        default_path=f"/inventory/products/{p.id}/"
                    )
                    cat_name = p.category.name if getattr(p, 'category', None) else "Product"
                    item_dict = {
                        'title': p.name,
                        'subtitle': f"SKU: {p.sku or 'N/A'} | Barcode: {p.barcode or 'N/A'} | Price: Rs. {p.selling_price:,.2f}",
                        'badge': cat_name,
                        'badge_color': 'info',
                        'url': detail_url,
                        'category': 'Products',
                        'icon': 'bi-box-seam'
                    }
                    prod_items.append(item_dict)
                    flat_results.append(item_dict)

                categories.append({
                    'category': 'Products',
                    'icon': 'bi-box-seam',
                    'count': len(prod_items),
                    'items': prod_items
                })
        except Exception as e:
            logger.warning(f"GlobalSearch error querying Products: {e}")

        # ---------------------------------------------------------------------
        # 2. ITEM INSTANCES (IMEI / SERIAL)
        # ---------------------------------------------------------------------
        try:
            imei_filter = Q(imei_1__icontains=q)
            if hasattr(ItemInstance, 'imei_2'):
                imei_filter |= Q(imei_2__icontains=q)
            if hasattr(ItemInstance, 'serial_number'):
                imei_filter |= Q(serial_number__icontains=q)
            if hasattr(ItemInstance, 'device_uid'):
                imei_filter |= Q(device_uid__icontains=q)

            instances_qs = ItemInstance.objects.filter(imei_filter).select_related('product', 'branch')
            if active_branch and not is_owner_or_super:
                instances_qs = instances_qs.filter(branch=active_branch)

            instances = instances_qs[:5]
            if instances.exists():
                instance_items = []
                for inst in instances:
                    inst_url = safe_url(
                        ['inventory:instance_detail', 'inventory:product_detail'],
                        pk=inst.product_id,
                        default_path=f"/inventory/products/{inst.product_id}/"
                    )
                    status_text = inst.get_status_display() if hasattr(inst, 'get_status_display') else inst.status
                    item_dict = {
                        'title': f"{inst.product.name} (IMEI: {inst.imei_1})",
                        'subtitle': f"Branch: {inst.branch.name if inst.branch else 'HQ'} | Serial: {getattr(inst, 'serial_number', 'N/A') or 'N/A'}",
                        'badge': status_text,
                        'badge_color': 'success' if inst.status == 'IN_STOCK' else 'secondary',
                        'url': inst_url,
                        'category': 'Smartphones & IMEI',
                        'icon': 'bi-phone'
                    }
                    instance_items.append(item_dict)
                    flat_results.append(item_dict)

                categories.append({
                    'category': 'Smartphones & IMEI',
                    'icon': 'bi-phone',
                    'count': len(instance_items),
                    'items': instance_items
                })
        except Exception as e:
            logger.warning(f"GlobalSearch error querying ItemInstances: {e}")

        # ---------------------------------------------------------------------
        # 3. CUSTOMERS
        # ---------------------------------------------------------------------
        try:
            cust_filter = Q(name__icontains=q) | Q(phone_number__icontains=q)
            if hasattr(Customer, 'pan_number'):
                cust_filter |= Q(pan_number__icontains=q)

            customers = Customer.objects.filter(cust_filter, is_active=True)[:5]
            if customers.exists():
                cust_items = []
                for c in customers:
                    cust_url = safe_url(
                        ['customers:customer_detail', 'customers:customer_edit'],
                        pk=c.id,
                        default_path=f"/customers/{c.id}/"
                    )
                    bal = getattr(c, 'current_credit_balance', Decimal('0.00'))
                    item_dict = {
                        'title': c.name,
                        'subtitle': f"Phone: {c.phone_number} | Due Balance: Rs. {bal:,.2f}",
                        'badge': f"PAN: {c.pan_number}" if getattr(c, 'pan_number', None) else "Customer",
                        'badge_color': 'danger' if bal > Decimal('0.00') else 'primary',
                        'url': cust_url,
                        'category': 'Customers',
                        'icon': 'bi-people'
                    }
                    cust_items.append(item_dict)
                    flat_results.append(item_dict)

                categories.append({
                    'category': 'Customers',
                    'icon': 'bi-people',
                    'count': len(cust_items),
                    'items': cust_items
                })
        except Exception as e:
            logger.warning(f"GlobalSearch error querying Customers: {e}")

        # ---------------------------------------------------------------------
        # 4. SUPPLIERS
        # ---------------------------------------------------------------------
        try:
            supp_filter = Q(company_name__icontains=q)
            if hasattr(Supplier, 'contact_person'):
                supp_filter |= Q(contact_person__icontains=q)
            if hasattr(Supplier, 'phone_number'):
                supp_filter |= Q(phone_number__icontains=q)
            if hasattr(Supplier, 'pan_number'):
                supp_filter |= Q(pan_number__icontains=q)
            if hasattr(Supplier, 'code'):
                supp_filter |= Q(code__icontains=q)

            suppliers = Supplier.objects.filter(supp_filter, is_active=True)[:5]
            if suppliers.exists():
                supp_items = []
                for s in suppliers:
                    supp_url = safe_url(
                        ['purchases:supplier_detail', 'purchases:supplier_edit'],
                        pk=s.id,
                        default_path=f"/purchases/suppliers/{s.id}/"
                    )
                    payable = getattr(s, 'current_balance', Decimal('0.00'))
                    item_dict = {
                        'title': s.company_name,
                        'subtitle': f"Contact: {getattr(s, 'contact_person', 'N/A') or 'N/A'} | Phone: {getattr(s, 'phone_number', 'N/A') or 'N/A'} | Payable: Rs. {payable:,.2f}",
                        'badge': f"PAN: {s.pan_number}" if getattr(s, 'pan_number', None) else "Supplier",
                        'badge_color': 'warning text-dark',
                        'url': supp_url,
                        'category': 'Suppliers',
                        'icon': 'bi-truck'
                    }
                    supp_items.append(item_dict)
                    flat_results.append(item_dict)

                categories.append({
                    'category': 'Suppliers',
                    'icon': 'bi-truck',
                    'count': len(supp_items),
                    'items': supp_items
                })
        except Exception as e:
            logger.warning(f"GlobalSearch error querying Suppliers: {e}")

        # ---------------------------------------------------------------------
        # 5. SALES INVOICES / ESTIMATES
        # ---------------------------------------------------------------------
        try:
            sales_filter = Q(estimate_number__icontains=q)
            if hasattr(SalesEstimate, 'customer_name_manual'):
                sales_filter |= Q(customer_name_manual__icontains=q)
            if hasattr(SalesEstimate, 'customer_phone_manual'):
                sales_filter |= Q(customer_phone_manual__icontains=q)
            if hasattr(SalesEstimate, 'customer_pan'):
                sales_filter |= Q(customer_pan__icontains=q)

            sales_qs = SalesEstimate.objects.filter(sales_filter).select_related('customer', 'branch')
            if active_branch and not is_owner_or_super:
                sales_qs = sales_qs.filter(branch=active_branch)

            estimates = sales_qs.order_by('-created_at')[:5]
            if estimates.exists():
                sale_items = []
                for est in estimates:
                    sale_url = safe_url(
                        ['sales:estimate_detail', 'sales:sales_receipt'],
                        pk=est.id,
                        default_path=f"/sales/{est.id}/"
                    )
                    buyer = est.customer.name if est.customer else (getattr(est, 'customer_name_manual', '') or "Walk-in Customer")
                    item_dict = {
                        'title': f"Bill #{est.estimate_number}",
                        'subtitle': f"Buyer: {buyer} | Total: Rs. {est.grand_total:,.2f} | Date: {est.bill_date_ad}",
                        'badge': est.get_status_display() if hasattr(est, 'get_status_display') else est.status,
                        'badge_color': 'success' if est.status == 'COMPLETED' else 'secondary',
                        'url': sale_url,
                        'category': 'Sales Estimates',
                        'icon': 'bi-receipt'
                    }
                    sale_items.append(item_dict)
                    flat_results.append(item_dict)

                categories.append({
                    'category': 'Sales Estimates',
                    'icon': 'bi-receipt',
                    'count': len(sale_items),
                    'items': sale_items
                })
        except Exception as e:
            logger.warning(f"GlobalSearch error querying SalesEstimate: {e}")

        # ---------------------------------------------------------------------
        # 6. GOODS RECEIVED NOTES (PURCHASES)
        # ---------------------------------------------------------------------
        try:
            grn_filter = Q(grn_number__icontains=q)
            if hasattr(GoodsReceivedNote, 'supplier_bill_no'):
                grn_filter |= Q(supplier_bill_no__icontains=q)
            grn_filter |= Q(supplier__company_name__icontains=q)

            grn_qs = GoodsReceivedNote.objects.filter(grn_filter).select_related('supplier', 'branch')
            if active_branch and not is_owner_or_super:
                grn_qs = grn_qs.filter(branch=active_branch)

            grns = grn_qs.order_by('-created_at')[:5]
            if grns.exists():
                grn_items = []
                for g in grns:
                    grn_url = safe_url(
                        ['purchases:grn_detail'],
                        pk=g.id,
                        default_path=f"/purchases/grn/{g.id}/"
                    )
                    supp_name = g.supplier.company_name if g.supplier else "Direct Vendor"
                    item_dict = {
                        'title': f"GRN #{g.grn_number}",
                        'subtitle': f"Vendor: {supp_name} | Bill Ref: {g.supplier_bill_no or 'N/A'} | Total: Rs. {g.net_total_amount:,.2f}",
                        'badge': g.get_status_display() if hasattr(g, 'get_status_display') else g.status,
                        'badge_color': 'info',
                        'url': grn_url,
                        'category': 'Purchases & GRN',
                        'icon': 'bi-bag-check'
                    }
                    grn_items.append(item_dict)
                    flat_results.append(item_dict)

                categories.append({
                    'category': 'Purchases & GRN',
                    'icon': 'bi-bag-check',
                    'count': len(grn_items),
                    'items': grn_items
                })
        except Exception as e:
            logger.warning(f"GlobalSearch error querying GRNs: {e}")

        # ---------------------------------------------------------------------
        # 7. REPAIR TICKETS
        # ---------------------------------------------------------------------
        try:
            rep_filter = Q(ticket_number__icontains=q)
            if hasattr(RepairTicket, 'imei_or_serial'):
                rep_filter |= Q(imei_or_serial__icontains=q)
            if hasattr(RepairTicket, 'customer_name_manual'):
                rep_filter |= Q(customer_name_manual__icontains=q)
            if hasattr(RepairTicket, 'customer_phone_manual'):
                rep_filter |= Q(customer_phone_manual__icontains=q)
            if hasattr(RepairTicket, 'device_model'):
                rep_filter |= Q(device_model__icontains=q)

            rep_qs = RepairTicket.objects.filter(rep_filter).select_related('customer', 'branch')
            if active_branch and not is_owner_or_super:
                rep_qs = rep_qs.filter(branch=active_branch)

            repairs = rep_qs.order_by('-created_at')[:5]
            if repairs.exists():
                rep_items = []
                for r in repairs:
                    rep_url = safe_url(
                        ['repairs:ticket_detail'],
                        pk=r.id,
                        default_path=f"/repairs/{r.id}/"
                    )
                    buyer = r.customer.name if r.customer else (getattr(r, 'customer_name_manual', '') or 'Counter Client')
                    status_lbl = r.get_service_status_display() if hasattr(r, 'get_service_status_display') else getattr(r, 'service_status', 'OPEN')
                    item_dict = {
                        'title': f"Repair Ticket #{r.ticket_number}",
                        'subtitle': f"Client: {buyer} | Device: {getattr(r, 'device_model', 'Handset')} | IMEI: {getattr(r, 'imei_or_serial', 'N/A')}",
                        'badge': status_lbl,
                        'badge_color': 'warning text-dark',
                        'url': rep_url,
                        'category': 'Repair Tickets',
                        'icon': 'bi-tools'
                    }
                    rep_items.append(item_dict)
                    flat_results.append(item_dict)

                categories.append({
                    'category': 'Repair Tickets',
                    'icon': 'bi-tools',
                    'count': len(rep_items),
                    'items': rep_items
                })
        except Exception as e:
            logger.warning(f"GlobalSearch error querying RepairTickets: {e}")

        # ---------------------------------------------------------------------
        # 8. CHART OF ACCOUNTS
        # ---------------------------------------------------------------------
        try:
            from apps.accounting.models import Account
            acc_filter = Q(code__icontains=q) | Q(name__icontains=q)
            if hasattr(Account, 'system_tag'):
                acc_filter |= Q(system_tag__icontains=q)

            accounts = Account.objects.filter(acc_filter, is_active=True)[:5]
            if accounts.exists():
                acc_items = []
                for a in accounts:
                    acc_url = safe_url(
                        ['accounting:ledger_detail', 'accounting:account_detail'],
                        pk=a.id,
                        default_path=f"/accounting/ledger/{a.id}/"
                    )
                    item_dict = {
                        'title': f"GL {a.code} - {a.name}",
                        'subtitle': f"Balance: Rs. {getattr(a, 'current_balance', Decimal('0.00')):,.2f}",
                        'badge': getattr(a, 'system_tag', 'GL Account') or 'Account',
                        'badge_color': 'secondary',
                        'url': acc_url,
                        'category': 'Chart of Accounts',
                        'icon': 'bi-journal-bookmark'
                    }
                    acc_items.append(item_dict)
                    flat_results.append(item_dict)

                categories.append({
                    'category': 'Chart of Accounts',
                    'icon': 'bi-journal-bookmark',
                    'count': len(acc_items),
                    'items': acc_items
                })
        except Exception as e:
            logger.warning(f"GlobalSearch error querying Accounts: {e}")

        # ---------------------------------------------------------------------
        # 9. SYSTEM USERS & STAFF
        # ---------------------------------------------------------------------
        try:
            from apps.users.models import User
            user_filter = (
                Q(username__icontains=q) |
                Q(first_name__icontains=q) |
                Q(last_name__icontains=q) |
                Q(phone_number__icontains=q)
            )
            users_qs = User.objects.filter(user_filter, is_active=True).select_related('assigned_branch')
            if not is_owner_or_super:
                users_qs = users_qs.filter(assigned_branch=active_branch).exclude(role='OWNER').filter(is_superuser=False)

            users = users_qs[:5]
            if users.exists():
                user_items = []
                for u in users:
                    user_url = safe_url(
                        ['users:user_edit', 'users:user_list'],
                        pk=u.id,
                        default_path=f"/users/{u.id}/edit/"
                    )
                    item_dict = {
                        'title': u.get_full_name() or u.username,
                        'subtitle': f"Username: {u.username} | Mobile: {u.phone_number} | Branch: {u.assigned_branch.name if u.assigned_branch else 'HQ'}",
                        'badge': u.get_role_display(),
                        'badge_color': 'dark',
                        'url': user_url,
                        'category': 'Staff & Users',
                        'icon': 'bi-person-badge'
                    }
                    user_items.append(item_dict)
                    flat_results.append(item_dict)

                categories.append({
                    'category': 'Staff & Users',
                    'icon': 'bi-person-badge',
                    'count': len(user_items),
                    'items': user_items
                })
        except Exception as e:
            logger.warning(f"GlobalSearch error querying Users: {e}")

        return JsonResponse({
            'status': 'success',
            'query': q,
            'total_count': len(flat_results),
            'categories': categories,
            'results': flat_results
        })

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