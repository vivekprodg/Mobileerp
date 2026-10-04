import os
import csv
import json
import uuid
import tempfile
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from datetime import date, datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple
import pandas as pd

from django import forms
from django.shortcuts import render, redirect, get_object_or_404
from django.views.generic import ListView, DetailView, CreateView, UpdateView, View, FormView, TemplateView
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib import messages
from django.urls import reverse_lazy, reverse
from django.http import JsonResponse, HttpResponse
from django.db import transaction
from django.db.models import (
    Q, F, Sum, Count, DecimalField, Value, ExpressionWrapper, Prefetch, Case, When
)
from django.db.models.functions import Coalesce
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.conf import settings
from django.utils import timezone

from apps.branches.models import Branch
from apps.inventory.models import (
    Product, ProductCategory, ProductSubCategory, Brand, UnitOfMeasurement, BranchStock,
    ProductComponentWarrantyRule, UnitConversion, ItemInstance,
    DeviceComponentWarranty, VendorRMAClaim, VendorRMAClaimItem,
    StockMovementLog, ProductBatch
)
from apps.products.models import ProductPriceTier
from apps.repairs.models import RepairTicket, RepairReplacedPart
from apps.inventory.forms import (
    ProductForm, ProductComponentWarrantyRuleFormSet, ProductCategoryForm,
    UnitOfMeasurementForm, UnitConversionForm, ManualStockAdjustmentForm,
    VendorRMAClaimForm, ProductExcelUploadForm, ProductExcelMappingForm
)
from apps.inventory.services import InventoryService, ExcelProductImporter
from apps.core.models import AuditLog, SystemConfiguration


# ==============================================================================
# BRAND FORM HELPER
# ==============================================================================
class BrandForm(forms.ModelForm):
    class Meta:
        model = Brand
        fields = ['name', 'origin_country']
        widgets = {
            'name': forms.TextInput(attrs={
                'class': 'inv-form-input',
                'placeholder': 'e.g. Samsung, Apple, Xiaomi, Nothing',
                'required': 'required'
            }),
            'origin_country': forms.TextInput(attrs={
                'class': 'inv-form-input',
                'placeholder': 'e.g. South Korea, USA, China, Nepal'
            }),
        }


# ==============================================================================
# INVENTORY CATALOG SERVICE HELPER
# ==============================================================================
class InventoryCatalogService:
    """
    Dedicated query filter, multi-warehouse aggregation, and KPI aggregation helper
    for inventory views. Computes total inventory stock quantities and asset valuations
    directly inside the database engine.
    """

    @staticmethod
    def get_filtered_products(request_params: dict, active_branch: Optional[Branch] = None):
        """
        Builds a comprehensive filtered and annotated queryset based on GET query parameters.
        Supports multi-warehouse stock lookups, status filtering, and sorting.
        """
        warehouse_filter = request_params.get('warehouse') or request_params.get('branch')
        target_branch = None

        if warehouse_filter and str(warehouse_filter).strip() not in ['all', '']:
            try:
                target_branch = Branch.objects.filter(id=int(warehouse_filter)).first()
            except (ValueError, TypeError):
                pass

        if not target_branch:
            target_branch = active_branch

        if target_branch:
            branch_stock_prefetch = Prefetch(
                'branch_stocks',
                queryset=BranchStock.objects.filter(branch=target_branch)
            )
            qs = Product.objects.filter(is_active=True).select_related(
                'category', 'brand', 'base_unit'
            ).prefetch_related(branch_stock_prefetch, 'component_warranty_rules')
        else:
            qs = Product.objects.filter(is_active=True).select_related(
                'category', 'brand', 'base_unit'
            ).prefetch_related('branch_stocks', 'component_warranty_rules')

        query = request_params.get('q', '').strip()
        cat_id = request_params.get('category', '').strip()
        brand_id = request_params.get('brand', '').strip()
        network_gen = request_params.get('network', '').strip()
        stock_status = request_params.get('stock_status', '').strip()
        low_stock = request_params.get('low_stock', '').strip()
        spare_part_filter = request_params.get('is_spare_part', '').strip()

        if query:
            qs = qs.filter(
                Q(name__icontains=query) |
                Q(sku__icontains=query) |
                Q(barcode__icontains=query) |
                Q(model_name__icontains=query) |
                Q(model_number__icontains=query) |
                Q(variant_name__icontains=query) |
                Q(ram__icontains=query) |
                Q(internal_storage__icontains=query) |
                Q(processor_chipset__icontains=query) |
                Q(rack_number__icontains=query) |
                Q(shelf_identifier__icontains=query)
            )

        if cat_id and cat_id != 'all':
            qs = qs.filter(category_id=cat_id)

        if brand_id and brand_id != 'all':
            qs = qs.filter(brand_id=brand_id)

        if network_gen and network_gen != 'all':
            qs = qs.filter(network_type=network_gen)

        if spare_part_filter == 'true':
            qs = qs.filter(is_spare_part=True)
        elif spare_part_filter == 'false':
            qs = qs.filter(is_spare_part=False)

        # Stock Status Filter Evaluation
        if target_branch:
            if stock_status in ['low', 'low_stock'] or low_stock == 'true':
                qs = qs.filter(
                    branch_stocks__branch=target_branch,
                    branch_stocks__quantity__gt=Decimal('0.000'),
                    branch_stocks__quantity__lte=F('branch_stocks__low_stock_threshold')
                )
            elif stock_status in ['critical', 'out_of_stock', 'zero']:
                qs = qs.filter(
                    branch_stocks__branch=target_branch,
                    branch_stocks__quantity__lte=Decimal('0.000')
                )
            elif stock_status in ['healthy', 'in_stock']:
                qs = qs.filter(
                    branch_stocks__branch=target_branch,
                    branch_stocks__quantity__gt=F('branch_stocks__low_stock_threshold')
                )
            elif stock_status == 'overstock':
                qs = qs.filter(
                    branch_stocks__branch=target_branch,
                    branch_stocks__quantity__gt=F('branch_stocks__low_stock_threshold') * 4
                )
        else:
            if stock_status in ['low', 'low_stock'] or low_stock == 'true':
                qs = qs.filter(
                    branch_stocks__quantity__gt=Decimal('0.000'),
                    branch_stocks__quantity__lte=F('branch_stocks__low_stock_threshold')
                ).distinct()
            elif stock_status in ['critical', 'out_of_stock', 'zero']:
                qs = qs.filter(
                    branch_stocks__quantity__lte=Decimal('0.000')
                ).distinct()
            elif stock_status in ['healthy', 'in_stock']:
                qs = qs.filter(
                    branch_stocks__quantity__gt=F('branch_stocks__low_stock_threshold')
                ).distinct()

        # Sorting Logic
        sort_by = request_params.get('sort_by') or request_params.get('ordering') or 'name'
        sort_map = {
            'name': 'name',
            '-name': '-name',
            'sku': 'sku',
            '-sku': '-sku',
            'purchase_price': 'purchase_price',
            '-purchase_price': '-purchase_price',
            'cost': 'purchase_price',
            '-cost': '-purchase_price',
            'selling_price': 'selling_price',
            '-selling_price': '-selling_price',
            'price': 'selling_price',
            '-price': '-selling_price',
            'updated_at': '-updated_at',
            '-updated_at': 'updated_at',
        }

        order_field = sort_map.get(sort_by, 'name')
        return qs.order_by(order_field)

    @staticmethod
    def get_dashboard_overview_kpis(active_branch: Optional[Branch] = None) -> Dict[str, Any]:
        """
        Computes the 6 top-level overview card metrics:
        1. Total Catalog SKUs (Active)
        2. Total Stock Units on Hand
        3. Low Stock Items Count
        4. Out of Stock Items Count
        5. Total Inventory Valuation (Landed Cost)
        6. Total Active Warehouses Count
        """
        cache_key = f"inv_catalog_master_kpis_{active_branch.id if active_branch else 'all'}"
        cached_kpis = cache.get(cache_key)
        if cached_kpis is not None:
            return cached_kpis

        total_skus = Product.objects.filter(is_active=True).count()
        total_warehouses = Branch.objects.filter(is_active=True).count()

        stocks_qs = BranchStock.objects.filter(product__is_active=True)
        if active_branch:
            stocks_qs = stocks_qs.filter(branch=active_branch)

        cost_val_expr = ExpressionWrapper(
            F('quantity') * F('product__purchase_price'),
            output_field=DecimalField(max_digits=18, decimal_places=2)
        )

        stock_agg = stocks_qs.aggregate(
            sum_qty=Coalesce(
                Sum('quantity'),
                Value(Decimal('0.000'), output_field=DecimalField(max_digits=18, decimal_places=3))
            ),
            total_val=Coalesce(
                Sum(cost_val_expr),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            )
        )

        total_units = int(stock_agg['sum_qty'] or 0)
        total_val = stock_agg['total_val'] or Decimal('0.00')

        low_stock_count = stocks_qs.filter(
            quantity__lte=F('low_stock_threshold'),
            quantity__gt=Decimal('0.000')
        ).count()

        out_of_stock_count = stocks_qs.filter(
            quantity__lte=Decimal('0.000')
        ).count()

        low_stocks_qs = stocks_qs.filter(
            quantity__lte=F('low_stock_threshold'),
            quantity__gt=Decimal('0.000')
        ).select_related('product', 'product__base_unit').order_by('quantity')
        replenishment_items = list(low_stocks_qs[:5])

        movement_qs = StockMovementLog.objects.all()
        if active_branch:
            movement_qs = movement_qs.filter(branch=active_branch)

        recent_activities = list(
            movement_qs.select_related('product', 'product__base_unit', 'user')
            .order_by('-created_at')[:6]
        )

        result = {
            'kpi_total_skus': total_skus,
            'kpi_active_skus': total_skus,
            'kpi_total_units': total_units,
            'kpi_low_stock_count': low_stock_count,
            'kpi_out_of_stock_count': out_of_stock_count,
            'kpi_inventory_value': total_val,
            'kpi_total_warehouses': total_warehouses,
            'replenishment_items': replenishment_items,
            'recent_activities': recent_activities,
        }

        cache.set(cache_key, result, timeout=30)
        return result

    @staticmethod
    def get_dashboard_kpis(active_branch: Branch):
        """Backward-compatibility wrapper for get_dashboard_overview_kpis."""
        return InventoryCatalogService.get_dashboard_overview_kpis(active_branch)


# ==============================================================================
# CENTRAL IMEI & SERIAL REGISTRY VIEW (SECTION 7)
# ==============================================================================
class ItemInstanceListView(LoginRequiredMixin, ListView):
    """
    Central searchable registry across all serialized smartphones, IMEI 1, IMEI 2,
    serial numbers, customer ownership, sale invoice references, and MDMS compliance statuses.
    """
    model = ItemInstance
    template_name = 'inventory/imei_serial_registry.html'
    context_object_name = 'items'
    paginate_by = 30

    def get_queryset(self):
        qs = ItemInstance.objects.select_related(
            'product', 'product__base_unit', 'branch'
        ).prefetch_related('component_warranties')

        active_branch = getattr(self.request, 'active_branch', None)
        if active_branch and not self.request.user.is_superuser:
            qs = qs.filter(branch=active_branch)

        q = self.request.GET.get('q', '').strip()
        status_filter = self.request.GET.get('status', '').strip()
        imei2_filter = self.request.GET.get('imei2_status', '').strip()
        mdms_filter = self.request.GET.get('mdms_status', '').strip()
        branch_filter = self.request.GET.get('branch', '').strip()

        if q:
            qs = qs.filter(
                Q(imei_1__icontains=q) |
                Q(imei_2__icontains=q) |
                Q(serial_number__icontains=q) |
                Q(device_uid__icontains=q) |
                Q(product__name__icontains=q) |
                Q(product__sku__icontains=q) |
                Q(customer_name__icontains=q) |
                Q(customer_phone__icontains=q) |
                Q(sold_invoice_reference__icontains=q) |
                Q(purchase_reference__icontains=q)
            )

        if status_filter:
            qs = qs.filter(status=status_filter)

        if imei2_filter == 'pending':
            qs = qs.filter(
                Q(imei_2_pending_scan=True) |
                ((Q(imei_2__isnull=True) | Q(imei_2='')) & Q(product__sim_configuration__in=['DUAL_SIM', 'ESIM_DUAL']))
            )
        elif imei2_filter == 'captured':
            qs = qs.filter(imei_2__isnull=False).exclude(imei_2='')

        if mdms_filter:
            qs = qs.filter(mdms_status=mdms_filter)

        if branch_filter and self.request.user.is_superuser:
            qs = qs.filter(branch_id=branch_filter)

        return qs.order_by('-created_at')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        base_qs = ItemInstance.objects.all()
        active_branch = getattr(self.request, 'active_branch', None)
        if active_branch and not self.request.user.is_superuser:
            base_qs = base_qs.filter(branch=active_branch)

        context.update({
            'total_registered_handsets': base_qs.count(),
            'total_in_stock_handsets': base_qs.filter(status='IN_STOCK').count(),
            'total_sold_handsets': base_qs.filter(status='SOLD').count(),
            'total_pending_imei2_handsets': base_qs.filter(
                Q(imei_2_pending_scan=True) |
                ((Q(imei_2__isnull=True) | Q(imei_2='')) & Q(product__sim_configuration__in=['DUAL_SIM', 'ESIM_DUAL']) & Q(status='IN_STOCK'))
            ).count(),
            'branches': Branch.objects.filter(is_active=True).order_by('-is_main_branch', 'name')
        })
        return context


# ==============================================================================
# PRODUCT INVENTORY & MASTER WAREHOUSE CATALOG VIEW (WITH DUAL AJAX/HTML RESPONSES)
# ==============================================================================
class ProductListView(LoginRequiredMixin, ListView):
    """
    Main Inventory Dashboard and Warehouse Management Catalog Controller.
    Supports:
    1. Full HTML Dashboard View containing all 6 summary KPI cards, warehouse summary table,
       inventory alert pills, dynamic stock movement velocity series, and filter toolbars.
    2. Real-Time AJAX/JSON Table Filtering & Sorting responding to live client searches without full reloads.
    3. Filtered CSV Streaming Export.
    """
    model = Product
    template_name = 'inventory/product_list.html'
    context_object_name = 'products'
    paginate_by = 25

    def get(self, request, *args, **kwargs):
        # 1. Handle CSV Export Stream
        if request.GET.get('export') == 'csv':
            active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()
            qs = InventoryCatalogService.get_filtered_products(request.GET, active_branch)
            return self.export_to_csv(qs, active_branch)

        # 2. Handle Asynchronous JSON Requests for Real-Time Table Updates
        is_ajax = (
            request.headers.get('x-requested-with') == 'XMLHttpRequest' or
            request.GET.get('ajax') == 'true' or
            request.GET.get('format') == 'json'
        )

        if is_ajax:
            return self.render_ajax_response(request)

        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        active_branch = getattr(self.request, 'active_branch', None) or Branch.get_default_main_branch()
        return InventoryCatalogService.get_filtered_products(self.request.GET, active_branch)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        active_branch = getattr(self.request, 'active_branch', None) or Branch.get_default_main_branch()
        is_owner_or_super = self.request.user.is_superuser or getattr(self.request.user, 'role', '') in ['OWNER', 'MANAGER']

        # 1. Compute 6 Master Executive KPI Summary Cards
        kpi_data = InventoryCatalogService.get_dashboard_overview_kpis(
            active_branch if not self.request.user.is_superuser else None
        )
        context.update(kpi_data)

        # 2. Multi-Warehouse Summary Dataset (All Active Branches with Live Units & Valuations)
        context['warehouses_summary'] = InventoryService.get_warehouse_summary()

        # 3. The 5 Core Inventory Alert Counts
        context['inventory_alerts'] = InventoryService.get_inventory_alerts(
            active_branch if not self.request.user.is_superuser else None
        )

        # 4. Default 7-Day Stock Movement Velocity Metrics & Chart Coordinates
        context['movement_metrics'] = InventoryService.get_stock_movement_metrics(
            '7d', active_branch if not self.request.user.is_superuser else None
        )

        # 5. Filter Dropdown Options
        active_cats = ProductCategory.objects.filter(is_active=True).order_by('name')
        if not active_cats.exists():
            active_cats = ProductCategory.objects.all().order_by('name')

        context['categories'] = active_cats
        context['brands'] = Brand.objects.all().order_by('name')
        context['units'] = UnitOfMeasurement.objects.all().order_by('name')
        context['branches'] = Branch.objects.filter(is_active=True).order_by('-is_main_branch', 'name')
        context['stock_status_choices'] = [
            ('all', 'All Stock Statuses'),
            ('healthy', 'In Stock (Healthy)'),
            ('low', 'Low Stock (Alert)'),
            ('critical', 'Out of Stock (Zero)'),
            ('overstock', 'Overstock (>4x Reorder)'),
        ]

        # Selected Filter Echo for UI Presets
        context['selected_warehouse'] = self.request.GET.get('warehouse') or self.request.GET.get('branch') or ''
        context['selected_category'] = self.request.GET.get('category') or ''
        context['selected_brand'] = self.request.GET.get('brand') or ''
        context['selected_status'] = self.request.GET.get('stock_status') or ''
        context['current_sort'] = self.request.GET.get('sort_by') or 'name'

        return context

    def render_ajax_response(self, request):
        """Returns JSON-formatted paginated product rows with real-time stock balances and status badges."""
        active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()
        qs = InventoryCatalogService.get_filtered_products(request.GET, active_branch)

        page_number = request.GET.get('page', 1)
        per_page = int(request.GET.get('per_page') or request.GET.get('limit') or self.paginate_by or 25)
        per_page = max(1, min(per_page, 100))

        paginator = self.get_paginator(qs, per_page)
        try:
            page_obj = paginator.page(page_number)
        except Exception:
            page_obj = paginator.page(1)

        target_warehouse_id = request.GET.get('warehouse') or request.GET.get('branch')
        target_branch = None
        if target_warehouse_id and target_warehouse_id != 'all':
            try:
                target_branch = Branch.objects.filter(id=int(target_warehouse_id)).first()
            except (ValueError, TypeError):
                pass
        if not target_branch:
            target_branch = active_branch

        results = []
        for p in page_obj.object_list:
            # Resolve branch stock
            stock_record = next((bs for bs in p.branch_stocks.all() if bs.branch_id == target_branch.id), None) if target_branch else None
            on_hand = stock_record.quantity if stock_record else Decimal('0.000')
            reserved = stock_record.reserved_quantity if stock_record else Decimal('0.000')
            available = stock_record.available_quantity if stock_record else Decimal('0.000')
            threshold = stock_record.low_stock_threshold if stock_record else (p.reorder_level or Decimal('5.00'))

            if on_hand <= Decimal('0.000'):
                status_label = "Out of Stock"
                badge_class = "danger"
            elif on_hand <= threshold:
                status_label = "Low Stock"
                badge_class = "warning"
            elif on_hand > (threshold * 4):
                status_label = "Overstock"
                badge_class = "info"
            else:
                status_label = "In Stock"
                badge_class = "success"

            stock_val = (on_hand * p.purchase_price).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            variant_desc = p.variant_name or f"{p.ram or ''}/{p.internal_storage or ''} {p.color_variant or ''}".strip()

            results.append({
                'id': p.id,
                'name': p.name,
                'sku': p.sku,
                'barcode': p.barcode or '',
                'category_id': p.category_id,
                'category_name': p.category.name if p.category else 'General',
                'brand_id': p.brand_id,
                'brand_name': p.brand.name if p.brand else '',
                'model_name': p.model_name or '',
                'model_number': p.model_number or '',
                'variant_specs': variant_desc,
                'rack_number': p.rack_number or '',
                'shelf_identifier': p.shelf_identifier or '',
                'base_unit_code': p.base_unit.code if p.base_unit else 'Pcs',
                'on_hand_quantity': float(on_hand),
                'reserved_quantity': float(reserved),
                'available_quantity': float(available),
                'purchase_price': float(p.purchase_price),
                'purchase_price_formatted': f"Rs. {p.purchase_price:,.2f}",
                'selling_price': float(p.selling_price),
                'selling_price_formatted': f"Rs. {p.selling_price:,.2f}",
                'stock_value': float(stock_val),
                'stock_value_formatted': f"Rs. {stock_val:,.2f}",
                'status_label': status_label,
                'badge_class': badge_class,
                'requires_imei': p.requires_imei_tracking,
                'is_spare_part': p.is_spare_part,
                'warranty_display': p.effective_warranty_display,
                'detail_url': reverse('inventory:product_detail', kwargs={'pk': p.id}),
                'edit_url': reverse('inventory:product_edit', kwargs={'pk': p.id}),
                'image_url': p.image.url if p.image else '',
            })

        return JsonResponse({
            'status': 'success',
            'total_count': paginator.count,
            'page': page_obj.number,
            'num_pages': paginator.num_pages,
            'has_next': page_obj.has_next(),
            'has_previous': page_obj.has_previous(),
            'results': results
        })

    def export_to_csv(self, queryset, active_branch: Optional[Branch] = None):
        """Streams a standard CSV file of the exact filtered catalog dataset."""
        timestamp = timezone.now().strftime('%Y%m%d_%H%M%S')
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="inventory_catalog_export_{timestamp}.csv"'

        writer = csv.writer(response)
        writer.writerow([
            'S.N.', 'Product Name', 'Model Name', 'Model Number', 'SKU', 'Barcode',
            'Category', 'Brand', 'Variant (RAM/ROM)', 'Rack / Shelf', 'Base Unit',
            'Stock On Hand', 'Stock Available', 'Purchase Cost (NPR)',
            'Stock Value at Cost (NPR)', 'Selling Price / MRP (NPR)', 'Stock Status',
            'Warranty Duration', 'IMEI Serialized'
        ])

        for idx, p in enumerate(queryset, start=1):
            stock = next((bs for bs in p.branch_stocks.all() if bs.branch_id == active_branch.id), None) if active_branch else None
            on_hand = stock.quantity if stock else Decimal('0.000')
            available = stock.available_quantity if stock else Decimal('0.000')
            val = (on_hand * p.purchase_price).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            if on_hand <= Decimal('0.000'):
                stat = "Out of Stock"
            elif stock and on_hand <= stock.low_stock_threshold:
                stat = "Low Stock"
            else:
                stat = "In Stock"

            writer.writerow([
                idx,
                p.name,
                p.model_name or '',
                p.model_number or '',
                p.sku,
                p.barcode or '',
                p.category.name if p.category else '',
                p.brand.name if p.brand else '',
                f"{p.ram or ''}/{p.internal_storage or ''} {p.color_variant or ''}".strip(),
                f"{p.rack_number or ''} {p.shelf_identifier or ''}".strip(),
                p.base_unit.code if p.base_unit else 'Pcs',
                f"{on_hand:.3f}",
                f"{available:.3f}",
                f"{p.purchase_price:.2f}",
                f"{val:.2f}",
                f"{p.selling_price:.2f}",
                stat,
                p.effective_warranty_display,
                "YES" if p.requires_imei_tracking else "NO"
            ])

        return response


# ==============================================================================
# DYNAMIC STOCK MOVEMENT VELOCITY API VIEW (7D, 30D, 3M TIMELINE)
# ==============================================================================
class StockMovementMetricsAPIView(LoginRequiredMixin, View):
    """
    Asynchronous JSON API for updating stock movement velocity metrics and SVG/Chart.js
    timeline data points across time horizons ('today', '7d', '30d', '3m').
    """

    def get(self, request, *args, **kwargs):
        period = request.GET.get('period', '7d').strip().lower()
        if period not in ['today', '7d', '30d', '3m']:
            period = '7d'

        branch_id = request.GET.get('branch_id') or request.GET.get('warehouse')
        branch = None
        if branch_id and str(branch_id).strip() not in ['all', '']:
            try:
                branch = Branch.objects.filter(id=int(branch_id), is_active=True).first()
            except (ValueError, TypeError):
                pass

        if not branch and not request.user.is_superuser:
            branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()

        metrics = InventoryService.get_stock_movement_metrics(period=period, branch=branch)
        return JsonResponse({
            'status': 'success',
            'data': metrics
        })


# ==============================================================================
# DASHBOARD MODAL STOCK ADJUSTMENT API VIEW
# ==============================================================================
class DashboardStockAdjustmentView(LoginRequiredMixin, UserPassesTestMixin, View):
    """
    Dedicated endpoint handling manual stock adjustment submissions directly from
    modal popups on the main inventory catalog and detail screens.
    Supports ADD (+), SUB (-), and SET/REPLACE (=) adjustment paradigms.
    """

    def test_func(self):
        return self.request.user.is_superuser or getattr(self.request.user, 'role', '') in ['OWNER', 'MANAGER']

    def post(self, request, *args, **kwargs):
        try:
            if request.content_type == 'application/json':
                payload = json.loads(request.body.decode('utf-8'))
            else:
                payload = request.POST

            product_id = payload.get('product_id') or kwargs.get('pk')
            if not product_id:
                return JsonResponse({'status': 'error', 'message': 'Product ID is required.'}, status=400)

            product = get_object_or_404(Product, pk=product_id)

            if product.requires_imei_tracking or product.requires_serial_tracking:
                return JsonResponse({
                    'status': 'error',
                    'message': f"Manual adjustment is blocked for serialized device '{product.name}'. "
                               f"Please receive or deduct serialized handsets via Goods Received Notes (GRN) or Sales Returns."
                }, status=400)

            warehouse_id = payload.get('warehouse_id') or payload.get('branch_id') or payload.get('branch')
            branch = None
            if warehouse_id and str(warehouse_id).strip() not in ['all', '']:
                branch = Branch.objects.filter(id=int(warehouse_id), is_active=True).first()
            if not branch:
                branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()

            adj_type = str(payload.get('adjustment_type', 'ADJUSTMENT_ADD')).upper().strip()
            raw_qty = payload.get('quantity')
            remarks = str(payload.get('remarks', '')).strip()

            if not raw_qty:
                return JsonResponse({'status': 'error', 'message': 'Quantity is required.'}, status=400)

            try:
                qty = Decimal(str(raw_qty)).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)
            except (InvalidOperation, ValueError, TypeError):
                return JsonResponse({'status': 'error', 'message': 'Invalid numerical quantity entered.'}, status=400)

            if qty < Decimal('0.000'):
                return JsonResponse({'status': 'error', 'message': 'Quantity cannot be negative.'}, status=400)

            # Determine adjustment delta
            with transaction.atomic():
                bstock, _ = BranchStock.objects.select_for_update().get_or_create(
                    branch=branch,
                    product=product,
                    defaults={
                        'quantity': Decimal('0.000'),
                        'reserved_quantity': Decimal('0.000'),
                        'quarantined_defective_quantity': Decimal('0.000'),
                        'low_stock_threshold': product.reorder_level or Decimal('5.00')
                    }
                )

                if adj_type in ['REPLACE', 'SET']:
                    target_qty = qty
                    current_qty = bstock.quantity
                    delta = target_qty - current_qty
                    movement_type = 'ADJUSTMENT_ADD' if delta >= Decimal('0.000') else 'ADJUSTMENT_SUB'
                elif adj_type == 'ADJUSTMENT_SUB':
                    delta = -qty
                    movement_type = 'ADJUSTMENT_SUB'
                else:  # ADJUSTMENT_ADD
                    delta = qty
                    movement_type = 'ADJUSTMENT_ADD'

                if delta == Decimal('0.000'):
                    return JsonResponse({
                        'status': 'success',
                        'message': f"No change in stock for '{product.name}'. Quantity is already {bstock.quantity}.",
                        'new_quantity': float(bstock.quantity)
                    })

                updated_bstock = InventoryService.adjust_stock(
                    product=product,
                    branch=branch,
                    quantity_delta=delta,
                    movement_type=movement_type,
                    remarks=remarks or f"Dashboard modal adjustment ({adj_type})",
                    user=request.user,
                    allow_negative=False
                )

                AuditLog.objects.create(
                    user=request.user,
                    branch=branch,
                    action_type='STOCK_ADJUST',
                    module='InventoryAdjustment',
                    object_repr=f"{product.name} ({delta:+})",
                    ip_address=request.META.get('REMOTE_ADDR'),
                    details={'delta': str(delta), 'new_quantity': str(updated_bstock.quantity), 'reason': remarks}
                )

            return JsonResponse({
                'status': 'success',
                'message': f"Stock for '{product.name}' adjusted successfully at {branch.name}. New quantity: {updated_bstock.quantity} {product.base_unit.code}.",
                'product_id': product.id,
                'warehouse_id': branch.id,
                'warehouse_name': branch.name,
                'delta': float(delta),
                'new_quantity': float(updated_bstock.quantity),
                'unit': product.base_unit.code
            })

        except ValidationError as ve:
            return JsonResponse({'status': 'error', 'message': str(ve.message if hasattr(ve, 'message') else ve)}, status=400)
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': f"Adjustment failed: {str(e)}"}, status=500)


# ==============================================================================
# DASHBOARD DIRECT WAREHOUSE STOCK TRANSFER API VIEW
# ==============================================================================
class DashboardStockTransferView(LoginRequiredMixin, UserPassesTestMixin, View):
    """
    Dedicated endpoint executing instant inter-warehouse transfers directly from
    the dashboard modal. Deducts source balance, increments destination balance,
    records dual audit logs, and reallocates serialized handset devices.
    """

    def test_func(self):
        return self.request.user.is_superuser or getattr(self.request.user, 'role', '') in ['OWNER', 'MANAGER', 'STAFF']

    def post(self, request, *args, **kwargs):
        try:
            if request.content_type == 'application/json':
                payload = json.loads(request.body.decode('utf-8'))
            else:
                payload = request.POST

            product_id = payload.get('product_id')
            source_id = payload.get('source_warehouse_id') or payload.get('source_branch_id') or payload.get('source_branch')
            dest_id = payload.get('destination_warehouse_id') or payload.get('destination_branch_id') or payload.get('destination_branch')
            raw_qty = payload.get('quantity', 1)
            scanned_imei = str(payload.get('scanned_imei_or_serial', '')).strip()
            remarks = str(payload.get('remarks', '')).strip()
            raw_date = payload.get('transfer_date')

            if not product_id:
                return JsonResponse({'status': 'error', 'message': 'Product selection is required.'}, status=400)
            if not source_id or not dest_id:
                return JsonResponse({'status': 'error', 'message': 'Source and destination warehouses are required.'}, status=400)

            if str(source_id) == str(dest_id):
                return JsonResponse({'status': 'error', 'message': 'Source and destination warehouses must be different.'}, status=400)

            product = get_object_or_404(Product, pk=product_id)
            source_branch = get_object_or_404(Branch, pk=source_id, is_active=True)
            dest_branch = get_object_or_404(Branch, pk=dest_id, is_active=True)

            try:
                qty = Decimal(str(raw_qty)).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)
            except (InvalidOperation, ValueError, TypeError):
                return JsonResponse({'status': 'error', 'message': 'Invalid transfer quantity entered.'}, status=400)

            if qty <= Decimal('0.000'):
                return JsonResponse({'status': 'error', 'message': 'Transfer quantity must be greater than zero.'}, status=400)

            transfer_date = None
            if raw_date:
                try:
                    transfer_date = datetime.strptime(str(raw_date)[:10], '%Y-%m-%d').date()
                except Exception:
                    transfer_date = timezone.now().date()

            result = InventoryService.execute_direct_warehouse_transfer(
                product=product,
                source_branch=source_branch,
                destination_branch=dest_branch,
                quantity=qty,
                scanned_imei_or_serial=scanned_imei,
                transfer_date=transfer_date,
                remarks=remarks,
                user=request.user
            )

            AuditLog.objects.create(
                user=request.user,
                branch=source_branch,
                action_type='CREATE',
                module='DirectWarehouseTransfer',
                object_repr=f"{result['transfer_reference']}: {product.name} x {qty}",
                ip_address=request.META.get('REMOTE_ADDR'),
                details={
                    'source': source_branch.name,
                    'destination': dest_branch.name,
                    'quantity': str(qty),
                    'imei': scanned_imei,
                    'remarks': remarks
                }
            )

            return JsonResponse({
                'status': 'success',
                'message': f"Successfully transferred {qty} {product.base_unit.code} of '{product.name}' "
                           f"from {source_branch.name} to {dest_branch.name} (Ref: {result['transfer_reference']}).",
                'transfer': result
            })

        except ValidationError as ve:
            return JsonResponse({'status': 'error', 'message': str(ve.message if hasattr(ve, 'message') else ve)}, status=400)
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': f"Direct transfer failed: {str(e)}"}, status=500)


# ==============================================================================
# FILTERED CSV EXPORT VIEW
# ==============================================================================
class InventoryExportCSVView(LoginRequiredMixin, View):
    """
    Dedicated view for exporting the filtered inventory dataset to a standard CSV file.
    Streams CSV headers and rows respecting search, category, brand, and warehouse filters.
    """

    def get(self, request, *args, **kwargs):
        active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()
        qs = InventoryCatalogService.get_filtered_products(request.GET, active_branch)
        return ProductListView().export_to_csv(qs, active_branch)


# ==============================================================================
# PRODUCT DETAIL VIEW
# ==============================================================================
class ProductDetailView(LoginRequiredMixin, DetailView):
    model = Product
    template_name = 'inventory/product_detail.html'
    context_object_name = 'product'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # Self-healing sync check: If product is loaded and warranty is out-of-sync, auto-repair it
        body_rule = self.object.component_warranty_rules.filter(component_type='DEVICE').first()
        if body_rule is not None and self.object.warranty_months != body_rule.warranty_months:
            self.object.warranty_months = body_rule.warranty_months
            Product.objects.filter(pk=self.object.pk).update(
                warranty_months=body_rule.warranty_months,
                updated_at=timezone.now()
            )

        context['component_rules'] = self.object.component_warranty_rules.all()
        context['stock_levels'] = self.object.branch_stocks.select_related('branch')
        context['batches'] = self.object.batches.select_related('branch').order_by('-purchase_date')
        context['conversions'] = self.object.unit_conversions.all()
        context['conversion_form'] = UnitConversionForm()
        context['price_tiers'] = self.object.price_tiers.all().order_by('min_quantity')
        context['tracked_units'] = self.object.tracked_instances.select_related('branch').prefetch_related('component_warranties')[:50]
        context['recent_movements'] = self.object.movement_logs.select_related('branch', 'user')[:20]
        context['adjustment_form'] = ManualStockAdjustmentForm()

        repair_tickets = RepairTicket.objects.filter(product=self.object).select_related(
            'technician', 'branch', 'customer'
        ).order_by('-created_at')[:50]

        adapted_service_tickets = []
        for t in repair_tickets:
            cust_name = t.customer_name_manual or (t.customer.name if t.customer else 'Walk-in Customer')
            cust_phone = t.customer_phone_manual or (t.customer.phone_number if t.customer else '')
            adapted_service_tickets.append({
                'id': t.id,
                'ticket_number': t.ticket_number,
                'imei_or_serial': t.imei_or_serial,
                'customer_name': cust_name,
                'customer_phone': cust_phone,
                'complaint_description': t.reported_fault,
                'technician': t.technician,
                'service_status': t.service_status,
                'get_service_status_display': t.get_service_status_display(),
                'total_service_amount': t.final_total_amount,
                'created_at': t.created_at,
            })

        context['service_tickets'] = adapted_service_tickets
        return context

    def post(self, request, *args, **kwargs):
        """Fallback in case packaging unit conversion modal posts directly to product detail URL."""
        self.object = self.get_object()
        return ProductUnitConversionCreateView.as_view()(request, pk=self.object.pk, *args, **kwargs)


# ==============================================================================
# PRODUCT CREATE VIEW
# ==============================================================================
class ProductCreateView(LoginRequiredMixin, UserPassesTestMixin, CreateView):
    model = Product
    form_class = ProductForm
    template_name = 'inventory/product_form.html'
    success_url = reverse_lazy('inventory:product_list')

    def test_func(self):
        return self.request.user.is_superuser or getattr(self.request.user, 'role', '') in ['OWNER', 'MANAGER']

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.request.POST:
            context['warranty_formset'] = ProductComponentWarrantyRuleFormSet(self.request.POST)
        else:
            context['warranty_formset'] = ProductComponentWarrantyRuleFormSet()
        return context

    def form_valid(self, form):
        context = self.get_context_data()
        warranty_formset = context['warranty_formset']

        with transaction.atomic():
            self.object = form.save()

            if warranty_formset.is_valid():
                warranty_formset.instance = self.object
                warranty_formset.save()

            body_rule = self.object.component_warranty_rules.filter(component_type='DEVICE').first()

            if body_rule is not None:
                if self.object.warranty_months != body_rule.warranty_months:
                    self.object.warranty_months = body_rule.warranty_months
                    self.object.save(update_fields=['warranty_months', 'updated_at'])
            elif self.object.requires_imei_tracking:
                body_months = self.object.warranty_months if self.object.warranty_months is not None else 12
                if body_months > 0:
                    standard_exclusion = (
                        "Covers genuine manufacturing defects only. "
                        "Void if physical drop cracks, glass breakage, or liquid/water ingress found."
                    )
                else:
                    standard_exclusion = "Out of warranty / Sold without active manufacturer warranty."

                ProductComponentWarrantyRule.objects.get_or_create(
                    product=self.object, component_type='DEVICE',
                    defaults={
                        'component_name': 'Main Handset Body & Motherboard',
                        'warranty_months': body_months,
                        'coverage_conditions': standard_exclusion
                    }
                )
                ProductComponentWarrantyRule.objects.get_or_create(
                    product=self.object, component_type='BATTERY',
                    defaults={
                        'component_name': 'Internal Battery',
                        'warranty_months': 6 if body_months >= 6 else body_months,
                        'coverage_conditions': standard_exclusion
                    }
                )
                ProductComponentWarrantyRule.objects.get_or_create(
                    product=self.object, component_type='SCREEN',
                    defaults={
                        'component_name': 'Screen / Display Panel',
                        'warranty_months': 3 if body_months >= 3 else body_months,
                        'coverage_conditions': standard_exclusion
                    }
                )

                if self.object.warranty_months != body_months:
                    self.object.warranty_months = body_months
                    self.object.save(update_fields=['warranty_months', 'updated_at'])

            AuditLog.objects.create(
                user=self.request.user,
                branch=getattr(self.request, 'active_branch', None) or Branch.get_default_main_branch(),
                action_type='CREATE',
                module='InventoryProduct',
                object_repr=str(self.object),
                ip_address=self.request.META.get('REMOTE_ADDR'),
                details={
                    'sku': self.object.sku,
                    'barcode': self.object.barcode,
                    'model_name': self.object.model_name,
                    'ram': self.object.ram,
                    'storage': self.object.internal_storage,
                    'requires_imei': self.object.requires_imei_tracking,
                    'is_spare_part': self.object.is_spare_part,
                    'warranty_months': self.object.warranty_months
                }
            )

        messages.success(self.request, f"Product '{self.object.name}' registered successfully.")
        return redirect(self.success_url)


# ==============================================================================
# PRODUCT UPDATE VIEW
# ==============================================================================
class ProductUpdateView(LoginRequiredMixin, UserPassesTestMixin, UpdateView):
    model = Product
    form_class = ProductForm
    template_name = 'inventory/product_form.html'

    def test_func(self):
        return self.request.user.is_superuser or getattr(self.request.user, 'role', '') in ['OWNER', 'MANAGER']

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.request.POST:
            context['warranty_formset'] = ProductComponentWarrantyRuleFormSet(self.request.POST, instance=self.object)
        else:
            context['warranty_formset'] = ProductComponentWarrantyRuleFormSet(instance=self.object)
        return context

    def form_valid(self, form):
        context = self.get_context_data()
        warranty_formset = context['warranty_formset']

        with transaction.atomic():
            self.object = form.save()
            if warranty_formset.is_valid():
                warranty_formset.instance = self.object
                warranty_formset.save()

            body_rule = self.object.component_warranty_rules.filter(component_type='DEVICE').first()
            if body_rule is not None:
                if self.object.warranty_months != body_rule.warranty_months:
                    self.object.warranty_months = body_rule.warranty_months
                    self.object.save(update_fields=['warranty_months', 'updated_at'])

            AuditLog.objects.create(
                user=self.request.user,
                branch=getattr(self.request, 'active_branch', None) or Branch.get_default_main_branch(),
                action_type='UPDATE',
                module='InventoryProduct',
                object_repr=str(self.object),
                ip_address=self.request.META.get('REMOTE_ADDR'),
                details={
                    'updated_fields': list(form.changed_data),
                    'is_spare_part': self.object.is_spare_part,
                    'warranty_months': self.object.warranty_months
                }
            )

        messages.success(self.request, f"Product '{self.object.name}' updated successfully.")
        return redirect(reverse('inventory:product_detail', kwargs={'pk': self.object.pk}))


# ==============================================================================
# PRODUCT DETAIL STOCK ADJUSTMENT VIEW (DUAL REDIRECT & AJAX SUPPORT)
# ==============================================================================
class ProductStockAdjustmentView(LoginRequiredMixin, UserPassesTestMixin, View):
    """
    Handles stock adjustment requests initiated from the Product Detail page or modal forms.
    Seamlessly returns JSON for AJAX callers and standard redirect for traditional form posts.
    """

    def test_func(self):
        return self.request.user.is_superuser or getattr(self.request.user, 'role', '') in ['OWNER', 'MANAGER']

    def post(self, request, pk, *args, **kwargs):
        is_ajax = (
            request.headers.get('x-requested-with') == 'XMLHttpRequest' or
            request.content_type == 'application/json' or
            request.GET.get('ajax') == 'true'
        )

        product = get_object_or_404(Product, pk=pk)
        warehouse_id = request.POST.get('branch') or request.POST.get('warehouse')
        branch = None
        if warehouse_id and str(warehouse_id).strip() not in ['all', '']:
            branch = Branch.objects.filter(id=int(warehouse_id), is_active=True).first()
        if not branch:
            branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()

        if product.requires_imei_tracking or product.requires_serial_tracking:
            err_msg = (
                f"Manual stock adjustment is blocked for serialized device '{product.name}'. "
                f"Please receive stock via a Goods Received Note (GRN) with scanned IMEIs."
            )
            if is_ajax:
                return JsonResponse({'status': 'error', 'message': err_msg}, status=400)
            messages.error(request, err_msg)
            return redirect('inventory:product_detail', pk=product.pk)

        form = ManualStockAdjustmentForm(request.POST)
        if form.is_valid():
            adj_type = form.cleaned_data['adjustment_type']
            qty = form.cleaned_data['quantity']
            remarks = form.cleaned_data['remarks']
            delta = qty if adj_type == 'ADJUSTMENT_ADD' else -qty

            try:
                with transaction.atomic():
                    bstock = InventoryService.adjust_stock(
                        product=product,
                        branch=branch,
                        quantity_delta=delta,
                        movement_type=adj_type,
                        remarks=remarks,
                        user=request.user,
                        allow_negative=False
                    )

                    AuditLog.objects.create(
                        user=request.user,
                        branch=branch,
                        action_type='STOCK_ADJUST',
                        module='InventoryAdjustment',
                        object_repr=f"{product.name} ({delta})",
                        ip_address=request.META.get('REMOTE_ADDR'),
                        details={'delta': str(delta), 'reason': remarks}
                    )

                success_msg = f"Stock successfully adjusted ({delta} {product.base_unit.code}). New balance: {bstock.quantity}."
                if is_ajax:
                    return JsonResponse({
                        'status': 'success',
                        'message': success_msg,
                        'new_quantity': float(bstock.quantity),
                        'product_id': product.id,
                        'warehouse_id': branch.id
                    })
                messages.success(request, success_msg)
            except Exception as e:
                if is_ajax:
                    return JsonResponse({'status': 'error', 'message': str(e)}, status=400)
                messages.error(request, str(e))
        else:
            err_msg = "Invalid adjustment form values: " + "; ".join([f"{f}: {err[0]}" for f, err in form.errors.items()])
            if is_ajax:
                return JsonResponse({'status': 'error', 'message': err_msg}, status=400)
            messages.error(request, err_msg)

        return redirect('inventory:product_detail', pk=product.pk)


# ==============================================================================
# PACKAGING UNIT CONVERSION VIEW
# ==============================================================================
class ProductUnitConversionCreateView(LoginRequiredMixin, UserPassesTestMixin, View):
    """
    Processes new packaging unit conversions (e.g. 1 Box = 50 Pieces) submitted
    from the Product Detail page modal.
    """

    def test_func(self):
        return self.request.user.is_superuser or getattr(self.request.user, 'role', '') in ['OWNER', 'MANAGER', 'STAFF']

    def post(self, request, pk, *args, **kwargs):
        product = get_object_or_404(Product, pk=pk)
        form = UnitConversionForm(request.POST)

        if form.is_valid():
            try:
                with transaction.atomic():
                    conversion = form.save(commit=False)
                    conversion.product = product

                    existing = UnitConversion.objects.filter(
                        product=product,
                        unit_name__iexact=conversion.unit_name
                    ).first()

                    if existing:
                        existing.conversion_factor = conversion.conversion_factor
                        existing.selling_price_per_unit = conversion.selling_price_per_unit
                        existing.barcode = conversion.barcode
                        existing.save()
                        conversion = existing
                    else:
                        conversion.save()

                    AuditLog.objects.create(
                        user=request.user,
                        branch=getattr(request, 'active_branch', None) or Branch.get_default_main_branch(),
                        action_type='CREATE',
                        module='PackagingUnitConversion',
                        object_repr=f"{product.name} - {conversion.unit_name}",
                        ip_address=request.META.get('REMOTE_ADDR'),
                        details={
                            'product_id': product.id,
                            'unit_name': conversion.unit_name,
                            'conversion_factor': str(conversion.conversion_factor),
                            'selling_price': str(conversion.selling_price_per_unit) if conversion.selling_price_per_unit else None,
                            'barcode': conversion.barcode
                        }
                    )

                messages.success(
                    request,
                    f"Packaging unit '{conversion.unit_name}' (1 {conversion.unit_name} = {conversion.conversion_factor} {product.base_unit.code}) saved successfully."
                )
            except Exception as e:
                messages.error(request, f"Error saving packaging unit: {str(e)}")
        else:
            errors = "; ".join([f"{f}: {e[0]}" for f, e in form.errors.items()])
            messages.error(request, f"Failed to save packaging unit: {errors}")

        return redirect('inventory:product_detail', pk=product.pk)


# ==============================================================================
# UNITS OF MEASUREMENT (UOM) MANAGEMENT & SEARCH VIEWS
# ==============================================================================
class UnitListView(LoginRequiredMixin, ListView):
    model = UnitOfMeasurement
    template_name = 'inventory/unit_list.html'
    context_object_name = 'units'
    paginate_by = 25

    def get_queryset(self):
        qs = UnitOfMeasurement.objects.annotate(product_count=Count('base_products')).order_by('name')
        query = self.request.GET.get('q', '').strip()
        if query:
            qs = qs.filter(
                Q(name__icontains=query) |
                Q(code__icontains=query) |
                Q(name_np__icontains=query)
            )
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['unit_form'] = UnitOfMeasurementForm()
        return context


class UnitCreateView(LoginRequiredMixin, UserPassesTestMixin, CreateView):
    model = UnitOfMeasurement
    form_class = UnitOfMeasurementForm
    template_name = 'inventory/unit_form.html'
    success_url = reverse_lazy('inventory:unit_list')

    def test_func(self):
        return self.request.user.is_superuser or getattr(self.request.user, 'role', '') in ['OWNER', 'MANAGER', 'STAFF']

    def form_valid(self, form):
        with transaction.atomic():
            response = super().form_valid(form)
            AuditLog.objects.create(
                user=self.request.user,
                branch=getattr(self.request, 'active_branch', None) or Branch.get_default_main_branch(),
                action_type='CREATE',
                module='InventoryUnit',
                object_repr=str(self.object),
                ip_address=self.request.META.get('REMOTE_ADDR'),
                details={
                    'code': self.object.code,
                    'name': self.object.name,
                    'name_np': self.object.name_np,
                    'allow_decimal': self.object.allow_decimal
                }
            )
        messages.success(self.request, f"Unit '{self.object.name}' ({self.object.code}) created successfully.")
        return response


class UnitUpdateView(LoginRequiredMixin, UserPassesTestMixin, UpdateView):
    model = UnitOfMeasurement
    form_class = UnitOfMeasurementForm
    template_name = 'inventory/unit_form.html'
    success_url = reverse_lazy('inventory:unit_list')

    def test_func(self):
        return self.request.user.is_superuser or getattr(self.request.user, 'role', '') in ['OWNER', 'MANAGER', 'STAFF']

    def form_valid(self, form):
        with transaction.atomic():
            response = super().form_valid(form)
            AuditLog.objects.create(
                user=self.request.user,
                branch=getattr(self.request, 'active_branch', None) or Branch.get_default_main_branch(),
                action_type='UPDATE',
                module='InventoryUnit',
                object_repr=str(self.object),
                ip_address=self.request.META.get('REMOTE_ADDR'),
                details={'updated_fields': list(form.changed_data)}
            )
        messages.success(self.request, f"Unit '{self.object.name}' ({self.object.code}) updated successfully.")
        return response


class UnitQuickCreateAPIView(LoginRequiredMixin, UserPassesTestMixin, View):
    def test_func(self):
        return self.request.user.is_superuser or getattr(self.request.user, 'role', '') in ['OWNER', 'MANAGER', 'STAFF']

    def post(self, request, *args, **kwargs):
        try:
            if request.content_type == 'application/json':
                data = json.loads(request.body.decode('utf-8'))
            else:
                data = request.POST

            name = data.get('name', '').strip()
            code = data.get('code', '').strip().upper()
            name_np = data.get('name_np', '').strip()
            allow_decimal = bool(data.get('allow_decimal', False))

            if not name:
                return JsonResponse({'status': 'error', 'message': 'Unit name is required.'}, status=400)
            if not code:
                return JsonResponse({'status': 'error', 'message': 'Unit short code is required.'}, status=400)

            with transaction.atomic():
                existing = UnitOfMeasurement.objects.filter(Q(name__iexact=name) | Q(code__iexact=code)).first()
                if existing:
                    return JsonResponse({
                        'status': 'success',
                        'message': 'Unit already exists.',
                        'unit': {'id': existing.id, 'name': existing.name, 'code': existing.code}
                    })

                unit = UnitOfMeasurement.objects.create(
                    name=name,
                    code=code,
                    name_np=name_np or None,
                    allow_decimal=allow_decimal
                )

                AuditLog.objects.create(
                    user=request.user,
                    branch=getattr(request, 'active_branch', None) or Branch.get_default_main_branch(),
                    action_type='CREATE',
                    module='InventoryUnit',
                    object_repr=str(unit),
                    ip_address=request.META.get('REMOTE_ADDR'),
                    details={'code': unit.code, 'name': unit.name, 'source': 'QuickCreateModal'}
                )

            return JsonResponse({
                'status': 'success',
                'message': f"Unit '{unit.name}' ({unit.code}) registered.",
                'unit': {'id': unit.id, 'name': unit.name, 'code': unit.code}
            })

        except Exception as err:
            return JsonResponse({'status': 'error', 'message': str(err)}, status=400)


class UnitSearchAPIView(LoginRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        q = request.GET.get('q', '').strip()
        try:
            limit = max(1, min(int(request.GET.get('limit', 30)), 100))
        except (ValueError, TypeError):
            limit = 30

        qs = UnitOfMeasurement.objects.all()
        if q:
            qs = qs.filter(
                Q(name__icontains=q) |
                Q(code__icontains=q) |
                Q(name_np__icontains=q)
            )

        units = qs.order_by('name')[:limit]
        results = [
            {
                'id': u.id,
                'name': u.name,
                'text': f"{u.name} ({u.code})",
                'code': u.code,
                'name_np': u.name_np or '',
                'allow_decimal': u.allow_decimal
            }
            for u in units
        ]
        return JsonResponse({'status': 'success', 'results': results, 'count': len(results)})


# ==============================================================================
# PRODUCT CATEGORY MANAGEMENT & SEARCH VIEWS
# ==============================================================================
class CategoryListView(LoginRequiredMixin, ListView):
    model = ProductCategory
    template_name = 'inventory/category_list.html'
    context_object_name = 'categories'
    paginate_by = 25

    def get_queryset(self):
        qs = ProductCategory.objects.annotate(product_count=Count('products')).order_by('name')
        query = self.request.GET.get('q', '').strip()
        if query:
            qs = qs.filter(
                Q(name__icontains=query) |
                Q(code__icontains=query) |
                Q(name_np__icontains=query) |
                Q(description__icontains=query)
            )
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['category_form'] = ProductCategoryForm(initial={'is_active': True})
        return context


class CategoryCreateView(LoginRequiredMixin, UserPassesTestMixin, CreateView):
    model = ProductCategory
    form_class = ProductCategoryForm
    template_name = 'inventory/category_form.html'
    success_url = reverse_lazy('inventory:category_list')

    def test_func(self):
        return self.request.user.is_superuser or getattr(self.request.user, 'role', '') in ['OWNER', 'MANAGER', 'STAFF']

    def form_valid(self, form):
        with transaction.atomic():
            if form.cleaned_data.get('is_active') is None:
                form.instance.is_active = True
            response = super().form_valid(form)
            AuditLog.objects.create(
                user=self.request.user,
                branch=getattr(self.request, 'active_branch', None) or Branch.get_default_main_branch(),
                action_type='CREATE',
                module='InventoryCategory',
                object_repr=str(self.object),
                ip_address=self.request.META.get('REMOTE_ADDR'),
                details={
                    'code': self.object.code,
                    'name': self.object.name,
                    'name_np': self.object.name_np,
                    'is_active': self.object.is_active
                }
            )
        messages.success(self.request, f"Category '{self.object.name}' created successfully.")
        return response


class CategoryUpdateView(LoginRequiredMixin, UserPassesTestMixin, UpdateView):
    model = ProductCategory
    form_class = ProductCategoryForm
    template_name = 'inventory/category_form.html'
    success_url = reverse_lazy('inventory:category_list')

    def test_func(self):
        return self.request.user.is_superuser or getattr(self.request.user, 'role', '') in ['OWNER', 'MANAGER', 'STAFF']

    def form_valid(self, form):
        with transaction.atomic():
            response = super().form_valid(form)
            AuditLog.objects.create(
                user=self.request.user,
                branch=getattr(self.request, 'active_branch', None) or Branch.get_default_main_branch(),
                action_type='UPDATE',
                module='InventoryCategory',
                object_repr=str(self.object),
                ip_address=self.request.META.get('REMOTE_ADDR'),
                details={'updated_fields': list(form.changed_data), 'is_active': self.object.is_active}
            )
        messages.success(self.request, f"Category '{self.object.name}' updated successfully.")
        return response


class CategoryQuickCreateAPIView(LoginRequiredMixin, UserPassesTestMixin, View):
    def test_func(self):
        return self.request.user.is_superuser or getattr(self.request.user, 'role', '') in ['OWNER', 'MANAGER', 'STAFF']

    def post(self, request, *args, **kwargs):
        try:
            if request.content_type == 'application/json':
                data = json.loads(request.body.decode('utf-8'))
            else:
                data = request.POST

            name = data.get('name', '').strip()
            code = data.get('code', '').strip().upper()
            name_np = data.get('name_np', '').strip()
            description = data.get('description', '').strip()

            if not name:
                return JsonResponse({'status': 'error', 'message': 'Category name is required.'}, status=400)

            if not code:
                cleaned = ''.join(e for e in name if e.isalnum())[:4].upper()
                code = f"{cleaned}-{uuid.uuid4().hex[:3].upper()}" if cleaned else f"CAT-{uuid.uuid4().hex[:4].upper()}"

            with transaction.atomic():
                existing = ProductCategory.objects.filter(name__iexact=name).first()
                if existing:
                    if not existing.is_active:
                        existing.is_active = True
                        existing.save(update_fields=['is_active'])
                    return JsonResponse({
                        'status': 'success',
                        'message': 'Category already exists.',
                        'category': {'id': existing.id, 'name': existing.name, 'code': existing.code}
                    })

                if ProductCategory.objects.filter(code__iexact=code).exists():
                    code = f"{code[:3]}-{uuid.uuid4().hex[:3].upper()}"

                category = ProductCategory.objects.create(
                    name=name,
                    code=code,
                    name_np=name_np or None,
                    description=description or None,
                    is_active=True
                )

                AuditLog.objects.create(
                    user=request.user,
                    branch=getattr(request, 'active_branch', None) or Branch.get_default_main_branch(),
                    action_type='CREATE',
                    module='InventoryCategory',
                    object_repr=str(category),
                    ip_address=request.META.get('REMOTE_ADDR'),
                    details={'code': category.code, 'name': category.name, 'source': 'QuickCreateModal', 'is_active': True}
                )

            return JsonResponse({
                'status': 'success',
                'message': f"Category '{category.name}' created successfully.",
                'category': {'id': category.id, 'name': category.name, 'code': category.code}
            })

        except Exception as err:
            return JsonResponse({'status': 'error', 'message': str(err)}, status=400)


class CategorySearchAPIView(LoginRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        q = request.GET.get('q', '').strip()
        include_inactive = request.GET.get('include_inactive', 'false').lower() == 'true'

        try:
            limit = max(1, min(int(request.GET.get('limit', 50)), 100))
        except (ValueError, TypeError):
            limit = 50

        qs = ProductCategory.objects.all()
        if not include_inactive:
            qs = qs.filter(is_active=True)

        if q:
            qs = qs.filter(
                Q(name__icontains=q) |
                Q(name_np__icontains=q) |
                Q(code__icontains=q)
            )

        categories = qs.order_by('name')[:limit]
        results = [
            {
                'id': c.id,
                'name': c.name,
                'text': f"{c.name} ({c.name_np})" if c.name_np else c.name,
                'name_np': c.name_np or '',
                'code': c.code,
                'is_active': c.is_active
            }
            for c in categories
        ]
        return JsonResponse({'status': 'success', 'results': results, 'count': len(results)})


# ==============================================================================
# PRODUCT SUBCATEGORY QUICK CREATE & SEARCH API VIEWS
# ==============================================================================
class SubCategoryQuickCreateAPIView(LoginRequiredMixin, UserPassesTestMixin, View):
    def test_func(self):
        return self.request.user.is_superuser or getattr(self.request.user, 'role', '') in ['OWNER', 'MANAGER', 'STAFF']

    def post(self, request, *args, **kwargs):
        try:
            if request.content_type == 'application/json':
                data = json.loads(request.body.decode('utf-8'))
            else:
                data = request.POST

            category_id = data.get('category_id') or data.get('category')
            name = data.get('name', '').strip()
            code = data.get('code', '').strip().upper()

            if not category_id:
                return JsonResponse({'status': 'error', 'message': 'Parent category is required.'}, status=400)

            if not name:
                return JsonResponse({'status': 'error', 'message': 'Subcategory name is required.'}, status=400)

            category = ProductCategory.objects.filter(id=category_id, is_active=True).first()
            if not category:
                return JsonResponse({'status': 'error', 'message': 'Selected parent category not found or inactive.'}, status=404)

            if not code:
                cleaned = ''.join(e for e in name if e.isalnum())[:4].upper()
                code = f"{cleaned}-{uuid.uuid4().hex[:3].upper()}" if cleaned else f"SUB-{uuid.uuid4().hex[:4].upper()}"

            with transaction.atomic():
                existing = ProductSubCategory.objects.filter(category=category, name__iexact=name).first()
                if existing:
                    return JsonResponse({
                        'status': 'success',
                        'message': 'Subcategory already exists.',
                        'subcategory': {
                            'id': existing.id,
                            'name': existing.name,
                            'code': existing.code,
                            'category_id': category.id,
                            'category_name': category.name
                        }
                    })

                subcategory = ProductSubCategory.objects.create(
                    category=category,
                    name=name,
                    code=code
                )

                AuditLog.objects.create(
                    user=request.user,
                    branch=getattr(request, 'active_branch', None) or Branch.get_default_main_branch(),
                    action_type='CREATE',
                    module='InventorySubCategory',
                    object_repr=str(subcategory),
                    ip_address=request.META.get('REMOTE_ADDR'),
                    details={
                        'category': category.name,
                        'name': subcategory.name,
                        'code': subcategory.code,
                        'source': 'QuickCreateModal'
                    }
                )

            return JsonResponse({
                'status': 'success',
                'message': f"Subcategory '{subcategory.name}' registered under '{category.name}'.",
                'subcategory': {
                    'id': subcategory.id,
                    'name': subcategory.name,
                    'code': subcategory.code,
                    'category_id': category.id,
                    'category_name': category.name
                }
            })

        except Exception as err:
            return JsonResponse({'status': 'error', 'message': str(err)}, status=400)


class SubCategorySearchAPIView(LoginRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        category_id = request.GET.get('category_id') or request.GET.get('category')
        q = request.GET.get('q', '').strip()

        try:
            limit = max(1, min(int(request.GET.get('limit', 50)), 100))
        except (ValueError, TypeError):
            limit = 50

        qs = ProductSubCategory.objects.select_related('category')
        if category_id:
            qs = qs.filter(category_id=category_id)

        if q:
            qs = qs.filter(
                Q(name__icontains=q) |
                Q(code__icontains=q)
            )

        subcategories = qs.order_by('name')[:limit]
        results = [
            {
                'id': sc.id,
                'name': sc.name,
                'text': sc.name,
                'code': sc.code or '',
                'category_id': sc.category_id,
                'category_name': sc.category.name if sc.category else ''
            }
            for sc in subcategories
        ]
        return JsonResponse({'status': 'success', 'results': results, 'count': len(results)})


# ==============================================================================
# BRAND MANAGEMENT, QUICK CREATE & SEARCH API VIEWS
# ==============================================================================
class BrandListView(LoginRequiredMixin, ListView):
    model = Brand
    template_name = 'inventory/brand_list.html'
    context_object_name = 'brands'
    paginate_by = 25

    def get_queryset(self):
        qs = Brand.objects.annotate(product_count=Count('products')).order_by('name')
        query = self.request.GET.get('q', '').strip()
        if query:
            qs = qs.filter(
                Q(name__icontains=query) |
                Q(origin_country__icontains=query)
            )
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['brand_form'] = BrandForm()
        return context


class BrandCreateView(LoginRequiredMixin, UserPassesTestMixin, CreateView):
    model = Brand
    form_class = BrandForm
    template_name = 'inventory/brand_form.html'
    success_url = reverse_lazy('inventory:brand_list')

    def test_func(self):
        return self.request.user.is_superuser or getattr(self.request.user, 'role', '') in ['OWNER', 'MANAGER', 'STAFF']

    def form_valid(self, form):
        with transaction.atomic():
            response = super().form_valid(form)
            AuditLog.objects.create(
                user=self.request.user,
                branch=getattr(self.request, 'active_branch', None) or Branch.get_default_main_branch(),
                action_type='CREATE',
                module='InventoryBrand',
                object_repr=str(self.object),
                ip_address=self.request.META.get('REMOTE_ADDR'),
                details={
                    'name': self.object.name,
                    'origin_country': self.object.origin_country
                }
            )
        messages.success(self.request, f"Brand '{self.object.name}' created successfully.")
        return response


class BrandUpdateView(LoginRequiredMixin, UserPassesTestMixin, UpdateView):
    model = Brand
    form_class = BrandForm
    template_name = 'inventory/brand_form.html'
    success_url = reverse_lazy('inventory:brand_list')

    def test_func(self):
        return self.request.user.is_superuser or getattr(self.request.user, 'role', '') in ['OWNER', 'MANAGER', 'STAFF']

    def form_valid(self, form):
        with transaction.atomic():
            response = super().form_valid(form)
            AuditLog.objects.create(
                user=self.request.user,
                branch=getattr(self.request, 'active_branch', None) or Branch.get_default_main_branch(),
                action_type='UPDATE',
                module='InventoryBrand',
                object_repr=str(self.object),
                ip_address=self.request.META.get('REMOTE_ADDR'),
                details={'updated_fields': list(form.changed_data)}
            )
        messages.success(self.request, f"Brand '{self.object.name}' updated successfully.")
        return response


class BrandQuickCreateAPIView(LoginRequiredMixin, UserPassesTestMixin, View):
    def test_func(self):
        return self.request.user.is_superuser or getattr(self.request.user, 'role', '') in ['OWNER', 'MANAGER', 'STAFF']

    def post(self, request, *args, **kwargs):
        try:
            if request.content_type == 'application/json':
                data = json.loads(request.body.decode('utf-8'))
            else:
                data = request.POST

            name = data.get('name', '').strip()
            origin_country = data.get('origin_country', '').strip()

            if not name:
                return JsonResponse({'status': 'error', 'message': 'Brand name is required.'}, status=400)

            with transaction.atomic():
                existing = Brand.objects.filter(name__iexact=name).first()
                if existing:
                    return JsonResponse({
                        'status': 'success',
                        'message': 'Brand already exists.',
                        'brand': {
                            'id': existing.id,
                            'name': existing.name,
                            'origin_country': existing.origin_country
                        }
                    })

                brand = Brand.objects.create(
                    name=name,
                    origin_country=origin_country or "Nepal"
                )

                AuditLog.objects.create(
                    user=request.user,
                    branch=getattr(request, 'active_branch', None) or Branch.get_default_main_branch(),
                    action_type='CREATE',
                    module='InventoryBrand',
                    object_repr=str(brand),
                    ip_address=request.META.get('REMOTE_ADDR'),
                    details={
                        'name': brand.name,
                        'origin_country': brand.origin_country,
                        'source': 'QuickCreateModal'
                    }
                )

            return JsonResponse({
                'status': 'success',
                'message': f"Brand '{brand.name}' registered successfully.",
                'brand': {
                    'id': brand.id,
                    'name': brand.name,
                    'origin_country': brand.origin_country
                }
            }, status=201)

        except Exception as err:
            return JsonResponse({'status': 'error', 'message': str(err)}, status=400)


class BrandSearchAPIView(LoginRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        q = request.GET.get('q', '').strip()

        try:
            limit = max(1, min(int(request.GET.get('limit', 50)), 100))
        except (ValueError, TypeError):
            limit = 50

        qs = Brand.objects.all()
        if q:
            qs = qs.filter(
                Q(name__icontains=q) |
                Q(origin_country__icontains=q)
            )

        brands = qs.order_by('name')[:limit]
        results = [
            {
                'id': b.id,
                'name': b.name,
                'text': b.name,
                'origin_country': b.origin_country or ''
            }
            for b in brands
        ]
        return JsonResponse({'status': 'success', 'results': results, 'count': len(results)})


# ==============================================================================
# VENDOR RMA & DISTRIBUTOR WARRANTY CLAIM VIEWS
# ==============================================================================
class VendorRMAListView(LoginRequiredMixin, ListView):
    model = VendorRMAClaim
    template_name = 'inventory/vendor_rma_list.html'
    context_object_name = 'rma_claims'
    paginate_by = 25

    def get_queryset(self):
        qs = VendorRMAClaim.objects.select_related('supplier', 'branch', 'dispatched_by').prefetch_related('claimed_items')
        branch = getattr(self.request, 'active_branch', None) or Branch.get_default_main_branch()
        if branch and not self.request.user.is_superuser:
            qs = qs.filter(branch=branch)
        return qs.order_by('-created_at')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        branch = getattr(self.request, 'active_branch', None) or Branch.get_default_main_branch()

        quarantine_parts = RepairReplacedPart.objects.filter(
            branch=branch,
            defective_part_status='QUARANTINED_FOR_RMA'
        ).select_related('spare_part_product', 'ticket').order_by('-created_at')

        context['pending_defective_parts'] = quarantine_parts
        return context


class VendorRMACreateView(LoginRequiredMixin, UserPassesTestMixin, FormView):
    template_name = 'inventory/vendor_rma_form.html'
    form_class = VendorRMAClaimForm
    success_url = reverse_lazy('inventory:vendor_rma_list')

    def test_func(self):
        return self.request.user.is_superuser or getattr(self.request.user, 'role', '') in ['OWNER', 'MANAGER']

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        branch = getattr(self.request, 'active_branch', None) or Branch.get_default_main_branch()

        quarantine_parts = RepairReplacedPart.objects.filter(
            branch=branch,
            defective_part_status='QUARANTINED_FOR_RMA'
        ).select_related('spare_part_product', 'ticket').order_by('-created_at')

        context['pending_defective_parts'] = quarantine_parts
        return context

    def form_valid(self, form):
        branch = getattr(self.request, 'active_branch', None) or Branch.get_default_main_branch()
        supplier = form.cleaned_data['supplier']
        distributor_center = form.cleaned_data['distributor_service_center']
        tracking_ref = form.cleaned_data['distributor_tracking_ref']
        notes = form.cleaned_data['resolution_notes']

        selected_part_ids = self.request.POST.getlist('selected_parts') or self.request.POST.getlist('selected_products')
        if not selected_part_ids:
            messages.error(self.request, "Please select at least one defective item to include in this RMA dispatch.")
            return self.form_invalid(form)

        rma_no = f"RMA-{branch.code}-{uuid.uuid4().hex[:6].upper()}"
        with transaction.atomic():
            rma_claim = VendorRMAClaim.objects.create(
                rma_number=rma_no,
                supplier=supplier,
                branch=branch,
                status='DISPATCHED_TO_VENDOR',
                dispatch_date=date.today(),
                distributor_tracking_ref=tracking_ref,
                distributor_service_center=distributor_center,
                total_claimed_parts_count=0,
                dispatched_by=self.request.user,
                resolution_notes=notes
            )

            claimed_count = 0
            for item_id_str in selected_part_ids:
                part_obj = RepairReplacedPart.objects.filter(
                    id=item_id_str,
                    branch=branch,
                    defective_part_status='QUARANTINED_FOR_RMA'
                ).select_related('spare_part_product', 'ticket').first()

                if part_obj:
                    prod = part_obj.spare_part_product
                    serial_val = (
                        self.request.POST.get(f"serial_{part_obj.id}") or
                        self.request.POST.get(f"serial_{prod.id}") or
                        part_obj.old_part_serial_or_batch or
                        getattr(part_obj.ticket, 'imei_or_serial', '') or
                        f"SN-{uuid.uuid4().hex[:6].upper()}"
                    )
                    defect_val = (
                        self.request.POST.get(f"defect_{part_obj.id}") or
                        self.request.POST.get(f"defect_{prod.id}") or
                        "Genuine Factory Defect under Warranty"
                    )

                    VendorRMAClaimItem.objects.create(
                        rma_claim=rma_claim,
                        product=prod,
                        defective_serial_or_imei=serial_val,
                        defect_description=defect_val,
                        resolution='PENDING'
                    )

                    part_obj.defective_part_status = 'SCRAPPED_LOCALLY'
                    part_obj.save(update_fields=['defective_part_status', 'updated_at'])

                    bs = BranchStock.objects.filter(branch=branch, product=prod).first()
                    if bs and bs.quarantined_defective_quantity > Decimal('0.000'):
                        bs.quarantined_defective_quantity = max(Decimal('0.000'), bs.quarantined_defective_quantity - part_obj.quantity)
                        bs.save(update_fields=['quarantined_defective_quantity', 'updated_at'])

                    claimed_count += 1
                else:
                    prod = Product.objects.filter(id=item_id_str).first()
                    if prod:
                        serial_val = self.request.POST.get(f"serial_{prod.id}", f"SN-{uuid.uuid4().hex[:6].upper()}")
                        defect_val = self.request.POST.get(f"defect_{prod.id}", "Genuine Factory Defect under Warranty")

                        VendorRMAClaimItem.objects.create(
                            rma_claim=rma_claim,
                            product=prod,
                            defective_serial_or_imei=serial_val,
                            defect_description=defect_val,
                            resolution='PENDING'
                        )

                        pending_part = RepairReplacedPart.objects.filter(
                            branch=branch,
                            spare_part_product=prod,
                            defective_part_status='QUARANTINED_FOR_RMA'
                        ).first()
                        if pending_part:
                            pending_part.defective_part_status = 'SCRAPPED_LOCALLY'
                            pending_part.save(update_fields=['defective_part_status', 'updated_at'])

                        bs = BranchStock.objects.filter(branch=branch, product=prod).first()
                        if bs and bs.quarantined_defective_quantity > Decimal('0.000'):
                            bs.quarantined_defective_quantity = max(Decimal('0.000'), bs.quarantined_defective_quantity - Decimal('1.000'))
                            bs.save(update_fields=['quarantined_defective_quantity', 'updated_at'])

                        claimed_count += 1

            rma_claim.total_claimed_parts_count = claimed_count
            rma_claim.save(update_fields=['total_claimed_parts_count', 'updated_at'])

            AuditLog.objects.create(
                user=self.request.user,
                branch=branch,
                action_type='CREATE',
                module='VendorRMA',
                object_repr=rma_claim.rma_number,
                ip_address=self.request.META.get('REMOTE_ADDR'),
                details={
                    'supplier': supplier.company_name,
                    'claimed_parts_count': claimed_count,
                    'tracking_ref': tracking_ref
                }
            )

        messages.success(self.request, f"Vendor RMA Claim {rma_claim.rma_number} dispatched to {supplier.company_name} with {claimed_count} part(s).")
        return redirect(self.success_url)


class VendorRMADetailView(LoginRequiredMixin, UserPassesTestMixin, DetailView):
    model = VendorRMAClaim
    template_name = 'inventory/vendor_rma_detail.html'
    context_object_name = 'rma_claim'

    def test_func(self):
        user = self.request.user
        return user.is_authenticated and (
            user.is_superuser or getattr(user, 'role', '') in ['OWNER', 'MANAGER']
        )

    def handle_no_permission(self):
        messages.error(self.request, "Permission Denied: Only Store Owners and Branch Managers can settle distributor RMA claims.")
        return redirect('inventory:vendor_rma_list')

    def post(self, request, pk, *args, **kwargs):
        rma_claim = self.get_object()
        item_id = request.POST.get('item_id')
        resolution = request.POST.get('resolution')
        rep_sn = request.POST.get('replacement_batch_or_serial', '')
        credit_amt = Decimal(request.POST.get('credit_amount', '0.00'))
        vendor_notes = request.POST.get('vendor_notes', '')

        rma_item = get_object_or_404(VendorRMAClaimItem, id=item_id, rma_claim=rma_claim)

        with transaction.atomic():
            rma_item.resolution = resolution
            rma_item.replacement_batch_or_serial = rep_sn
            rma_item.credit_amount = credit_amt
            rma_item.vendor_notes = vendor_notes
            rma_item.save()

            if resolution == 'REPLACED_WITH_NEW_PART':
                InventoryService.adjust_stock(
                    product=rma_item.product,
                    branch=rma_claim.branch,
                    quantity_delta=Decimal('1.000'),
                    movement_type='RMA_VENDOR_REPLACEMENT_IN',
                    reference_doc=rma_claim.rma_number,
                    imei_or_serial=rep_sn,
                    remarks=f"RMA New Replacement Received from {rma_claim.supplier.company_name}",
                    user=request.user,
                    allow_negative=True
                )

            all_items = rma_claim.claimed_items.all()
            if not all_items.filter(resolution='PENDING').exists():
                rma_claim.status = 'COMPLETED'
                rma_claim.resolution_date = date.today()
                rma_claim.resolved_by = request.user
                rma_claim.total_credit_amount = all_items.aggregate(
                    total=Coalesce(Sum('credit_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2)))
                )['total']
                rma_claim.save()
            else:
                rma_claim.status = 'PARTIALLY_SETTLED'
                rma_claim.save(update_fields=['status', 'updated_at'])

            AuditLog.objects.create(
                user=request.user,
                branch=rma_claim.branch,
                action_type='UPDATE',
                module='VendorRMASettlement',
                object_repr=f"RMA {rma_claim.rma_number} Item: {rma_item.product.name}",
                ip_address=request.META.get('REMOTE_ADDR'),
                details={
                    'resolution': resolution,
                    'credit_amount': str(credit_amt),
                    'replacement_sn': rep_sn,
                    'is_completed': rma_claim.status == 'COMPLETED'
                }
            )

        messages.success(request, f"RMA Item {rma_item.product.name} settled as: {rma_item.get_resolution_display()}")
        return redirect('inventory:vendor_rma_detail', pk=rma_claim.pk)


# ==============================================================================
# EXCEL IMPORT PIPELINE
# ==============================================================================
class ProductExcelUploadView(LoginRequiredMixin, UserPassesTestMixin, FormView):
    template_name = 'inventory/excel_import.html'
    form_class = ProductExcelUploadForm

    def test_func(self):
        return self.request.user.is_superuser or getattr(self.request.user, 'role', '') in ['OWNER', 'MANAGER']

    def form_valid(self, form):
        file = form.cleaned_data['excel_file']
        try:
            temp_dir = os.path.join(settings.MEDIA_ROOT, 'temp_excel_imports')
            os.makedirs(temp_dir, exist_ok=True)

            ext = os.path.splitext(file.name)[1].lower()
            temp_file_name = f"import_{uuid.uuid4().hex}{ext}"
            temp_file_path = os.path.join(temp_dir, temp_file_name)

            with open(temp_file_path, 'wb+') as destination:
                for chunk in file.chunks():
                    destination.write(chunk)

            df = ExcelProductImporter.read_file(open(temp_file_path, 'rb'))
            if df.empty:
                if os.path.exists(temp_file_path):
                    os.remove(temp_file_path)
                messages.error(self.request, "Uploaded spreadsheet contains no data rows.")
                return self.form_invalid(form)

            headers = list(df.columns)
            preview_rows = df.head(5).to_dict(orient='records')
            auto_mappings = ExcelProductImporter.detect_column_mappings(headers)

            self.request.session['excel_import_file_path'] = temp_file_path
            self.request.session['excel_import_headers'] = headers
            self.request.session['excel_import_preview'] = preview_rows
            self.request.session['excel_import_mappings'] = auto_mappings

            return redirect('inventory:excel_map')

        except Exception as e:
            messages.error(self.request, f"Error parsing spreadsheet: {str(e)}")
            return self.form_invalid(form)


class ProductExcelMappingView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    template_name = 'inventory/excel_mapping.html'

    def test_func(self):
        return self.request.user.is_superuser or getattr(self.request.user, 'role', '') in ['OWNER', 'MANAGER']

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        headers = self.request.session.get('excel_import_headers', [])
        mappings = self.request.session.get('excel_import_mappings', {})
        preview = self.request.session.get('excel_import_preview', [])

        context['headers'] = headers
        context['system_fields'] = ExcelProductImporter.SYSTEM_FIELDS
        context['auto_mappings'] = mappings
        context['preview_rows'] = preview
        context['mapping_form'] = ProductExcelMappingForm()
        context['branches'] = Branch.objects.filter(is_active=True).order_by('-is_main_branch', 'name')
        return context


class ProductExcelProcessAPIView(LoginRequiredMixin, UserPassesTestMixin, View):
    def test_func(self):
        return self.request.user.is_superuser or getattr(self.request.user, 'role', '') in ['OWNER', 'MANAGER']

    def post(self, request, *args, **kwargs):
        temp_file_path = request.session.get('excel_import_file_path')
        if not temp_file_path or not os.path.exists(temp_file_path):
            return JsonResponse({
                'status': 'error',
                'message': 'Import session expired or temporary file not found. Please re-upload your file.'
            }, status=400)

        try:
            payload = json.loads(request.body.decode('utf-8'))
            mapping = payload.get('mapping', {})
            conflict_strategy = payload.get('conflict_strategy', 'MERGE')
            explicit_branch_id = payload.get('branch_id')

            branch = None
            if explicit_branch_id:
                branch = Branch.objects.filter(id=explicit_branch_id, is_active=True).first()
            if not branch:
                branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()

            with open(temp_file_path, 'rb') as f:
                df = ExcelProductImporter.read_file(f)

            summary = ExcelProductImporter.process_import(
                df=df,
                mapping=mapping,
                branch=branch,
                conflict_strategy=conflict_strategy,
                user=request.user
            )

            try:
                if os.path.exists(temp_file_path):
                    os.remove(temp_file_path)
            except Exception:
                pass

            request.session.pop('excel_import_file_path', None)
            request.session.pop('excel_import_headers', None)
            request.session.pop('excel_import_preview', None)
            request.session.pop('excel_import_mappings', None)

            return JsonResponse({
                'status': 'success',
                'summary': summary,
                'redirect_url': reverse('inventory:product_list')
            })

        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)}, status=400)