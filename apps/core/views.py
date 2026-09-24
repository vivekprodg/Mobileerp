import logging
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.shortcuts import render
from django.views.generic import TemplateView, View, ListView
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.http import JsonResponse
from django.urls import reverse, NoReverseMatch
from django.db.models import Sum, F, Q, Count, DecimalField, Value, Case, When, ExpressionWrapper
from django.db.models.functions import Coalesce
from django.utils import timezone
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

class DashboardHomeView(LoginRequiredMixin, TemplateView):
    """
    Central Executive Dashboard Controller.
    Optimized to eliminate Python in-memory loops by delegating all stock valuations,
    landed cost aggregations, and overdue metrics to PostgreSQL/database expressions.
    Includes a 60-second caching layer for near-instantaneous page reloads.
    """
    template_name = 'core/dashboard.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        active_branch = getattr(self.request, 'active_branch', None) or Branch.get_default_main_branch()
        today = timezone.now().date()
        branch_id = active_branch.id if active_branch else 0
        is_super = self.request.user.is_superuser

        # ---------------------------------------------------------------------
        # CACHE LAYER: 60-second cache per branch & user scope to prevent DB thrashing
        # ---------------------------------------------------------------------
        cache_key = f"dashboard_kpis_branch_{branch_id}_{today.isoformat()}_{is_super}"
        cached_data = cache.get(cache_key)

        if cached_data is not None:
            context.update(cached_data)
            return context

        # ---------------------------------------------------------------------
        # 1. TODAY'S SALES & BILLS COUNT (Database Aggregation)
        # ---------------------------------------------------------------------
        sales_qs = SalesEstimate.objects.filter(
            bill_date_ad=today,
            status__in=['COMPLETED', 'PARTIALLY_RETURNED']
        )
        if active_branch and not is_super:
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
        grn_qs = GoodsReceivedNote.objects.filter(
            bill_date=today,
            status='RECEIVED'
        )
        if active_branch and not is_super:
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
        # 3. CURRENT STOCK VALUATION (DATABASE AGGREGATION - REPLACED PYTHON FOR-LOOP)
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
        # 9. RECENT SUMMARIES (TOP 5 RECENT SALES & PURCHASES)
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
        # PACKAGE & CACHE DATA DICTIONARY
        # ---------------------------------------------------------------------
        kpi_payload = {
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
            'today_cash_sales': today_cash_sales,
            'today_fonepay_sales': today_fonepay_sales,
            'today_esewa_sales': today_esewa_sales,
            'today_credit_sales': today_credit_sales,
            'recent_sales': recent_sales,
            'recent_purchases': recent_purchases,
            'low_stock_items': low_stock_items,
            'active_repair_count': active_repairs_count,
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
                        'subtitle': f"Type: {a.get_account_type_display() if hasattr(a, 'get_account_type_display') else getattr(a, 'account_type', '')} | Balance: Rs. {getattr(a, 'current_balance', Decimal('0.00')):,.2f}",
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