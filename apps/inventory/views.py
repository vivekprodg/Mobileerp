import os
import json
import uuid
import tempfile
from decimal import Decimal
from datetime import date
import pandas as pd

from django import forms
from django.shortcuts import render, redirect, get_object_or_404
from django.views.generic import ListView, DetailView, CreateView, UpdateView, View, FormView, TemplateView
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib import messages
from django.urls import reverse_lazy, reverse
from django.http import JsonResponse
from django.db import transaction
from django.db.models import Q, F, Sum, Count
from django.conf import settings

from apps.branches.models import Branch
from apps.inventory.models import (
    Product, ProductCategory, ProductSubCategory, Brand, UnitOfMeasurement, BranchStock,
    ProductComponentWarrantyRule, UnitConversion, ItemInstance,
    DeviceComponentWarranty, VendorRMAClaim, VendorRMAClaimItem,
    StockMovementLog
)
from apps.products.models import ProductPriceTier
from apps.repairs.models import RepairTicket, RepairReplacedPart
from apps.inventory.forms import (
    ProductForm, ProductComponentWarrantyRuleFormSet, ProductCategoryForm,
    UnitOfMeasurementForm, UnitConversionForm, ManualStockAdjustmentForm,
    VendorRMAClaimForm, ProductExcelUploadForm, ProductExcelMappingForm
)
from apps.inventory.services import InventoryService, ExcelProductImporter
from apps.core.models import AuditLog


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
                'placeholder': 'e.g. Samsung, Apple, Xiaomi',
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
    Dedicated query filter and KPI aggregation helper for inventory views.
    """

    @staticmethod
    def get_filtered_products(request_params: dict, active_branch: Branch):
        qs = Product.objects.select_related('category', 'brand', 'base_unit').prefetch_related(
            'branch_stocks', 'component_warranty_rules'
        )

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

        if network_gen:
            qs = qs.filter(network_type=network_gen)

        if spare_part_filter == 'true':
            qs = qs.filter(is_spare_part=True)
        elif spare_part_filter == 'false':
            qs = qs.filter(is_spare_part=False)

        if active_branch:
            if stock_status == 'low' or low_stock == 'true':
                qs = qs.filter(
                    branch_stocks__branch=active_branch,
                    branch_stocks__quantity__gt=0,
                    branch_stocks__quantity__lte=F('branch_stocks__low_stock_threshold')
                )
            elif stock_status == 'critical':
                qs = qs.filter(
                    branch_stocks__branch=active_branch,
                    branch_stocks__quantity__lte=0
                )
            elif stock_status == 'healthy':
                qs = qs.filter(
                    branch_stocks__branch=active_branch,
                    branch_stocks__quantity__gt=F('branch_stocks__low_stock_threshold')
                )

        return qs.order_by('name')

    @staticmethod
    def get_dashboard_kpis(active_branch: Branch):
        total_skus = Product.objects.count()

        if not active_branch:
            return {
                'kpi_active_skus': total_skus,
                'kpi_total_units': 0,
                'kpi_low_stock_count': 0,
                'kpi_inventory_value': Decimal('0.00'),
                'replenishment_items': [],
                'recent_activities': []
            }

        branch_stocks = BranchStock.objects.filter(branch=active_branch).select_related('product')
        total_units = branch_stocks.aggregate(sum_qty=Sum('quantity'))['sum_qty'] or Decimal('0')

        low_stocks_qs = branch_stocks.filter(quantity__lte=F('low_stock_threshold')).order_by('quantity')
        low_stock_count = low_stocks_qs.count()
        replenishment_items = list(low_stocks_qs[:5])

        total_val = sum((bs.quantity * bs.product.purchase_price for bs in branch_stocks), Decimal('0.00'))

        recent_activities = list(
            StockMovementLog.objects.filter(branch=active_branch)
            .select_related('product', 'user')
            .order_by('-created_at')[:6]
        )

        return {
            'kpi_active_skus': total_skus,
            'kpi_total_units': int(total_units),
            'kpi_low_stock_count': low_stock_count,
            'kpi_inventory_value': total_val,
            'replenishment_items': replenishment_items,
            'recent_activities': recent_activities
        }


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
# UNITS OF MEASUREMENT (UOM) MANAGEMENT VIEWS
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


# ==============================================================================
# PRODUCT CATEGORY MANAGEMENT VIEWS
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


# ==============================================================================
# PRODUCT SUBCATEGORY QUICK CREATE API VIEW
# ==============================================================================

class SubCategoryQuickCreateAPIView(LoginRequiredMixin, UserPassesTestMixin, View):
    """
    Lightweight JSON endpoint to register product subcategories dynamically
    (e.g., from product creation/edit modals or frontend quick-add controls).
    """

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


# ==============================================================================
# BRAND MANAGEMENT & QUICK CREATE API VIEWS
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
    """
    Lightweight JSON endpoint to create a Brand dynamically from modals
    or the quick-add button in product creation interfaces.
    """

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


# ==============================================================================
# PRODUCT INVENTORY & CATALOG VIEWS
# ==============================================================================

class ProductListView(LoginRequiredMixin, ListView):
    model = Product
    template_name = 'inventory/product_list.html'
    context_object_name = 'products'
    paginate_by = 25

    def get_queryset(self):
        active_branch = getattr(self.request, 'active_branch', None) or Branch.get_default_main_branch()
        return InventoryCatalogService.get_filtered_products(self.request.GET, active_branch)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        active_branch = getattr(self.request, 'active_branch', None) or Branch.get_default_main_branch()

        active_cats = ProductCategory.objects.filter(is_active=True).order_by('name')
        if not active_cats.exists():
            active_cats = ProductCategory.objects.all().order_by('name')
        context['categories'] = active_cats
        context['brands'] = Brand.objects.all().order_by('name')
        context['units'] = UnitOfMeasurement.objects.all().order_by('name')

        kpi_data = InventoryCatalogService.get_dashboard_kpis(active_branch)
        context.update(kpi_data)

        return context


class ProductDetailView(LoginRequiredMixin, DetailView):
    model = Product
    template_name = 'inventory/product_detail.html'
    context_object_name = 'product'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['component_rules'] = self.object.component_warranty_rules.all()
        context['stock_levels'] = self.object.branch_stocks.select_related('branch')
        context['batches'] = self.object.batches.select_related('branch').order_by('-purchase_date')
        context['conversions'] = self.object.unit_conversions.all()
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
            elif self.object.requires_imei_tracking:
                ProductComponentWarrantyRule.objects.get_or_create(
                    product=self.object, component_type='DEVICE',
                    defaults={'component_name': 'Main Handset Body & Motherboard', 'warranty_months': self.object.warranty_months}
                )
                ProductComponentWarrantyRule.objects.get_or_create(
                    product=self.object, component_type='BATTERY',
                    defaults={'component_name': 'Internal Battery', 'warranty_months': 6}
                )
                ProductComponentWarrantyRule.objects.get_or_create(
                    product=self.object, component_type='SCREEN',
                    defaults={'component_name': 'Screen / Display Panel', 'warranty_months': 6}
                )

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
                    'is_spare_part': self.object.is_spare_part
                }
            )

        messages.success(self.request, f"Product '{self.object.name}' registered successfully.")
        return redirect(self.success_url)


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

            AuditLog.objects.create(
                user=self.request.user,
                branch=getattr(self.request, 'active_branch', None) or Branch.get_default_main_branch(),
                action_type='UPDATE',
                module='InventoryProduct',
                object_repr=str(self.object),
                ip_address=self.request.META.get('REMOTE_ADDR'),
                details={'updated_fields': list(form.changed_data), 'is_spare_part': self.object.is_spare_part}
            )

        messages.success(self.request, f"Product '{self.object.name}' updated successfully.")
        return redirect(reverse('inventory:product_detail', kwargs={'pk': self.object.pk}))


class ProductStockAdjustmentView(LoginRequiredMixin, UserPassesTestMixin, View):
    def test_func(self):
        return self.request.user.is_superuser or getattr(self.request.user, 'role', '') in ['OWNER', 'MANAGER']

    def post(self, request, pk, *args, **kwargs):
        product = get_object_or_404(Product, pk=pk)
        branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()

        if product.requires_imei_tracking or product.requires_serial_tracking:
            messages.error(
                request,
                f"Manual stock adjustment is blocked for serialized device '{product.name}'. "
                f"Please receive stock via a Goods Received Note (GRN) with scanned IMEIs."
            )
            return redirect('inventory:product_detail', pk=product.pk)

        form = ManualStockAdjustmentForm(request.POST)
        if form.is_valid():
            adj_type = form.cleaned_data['adjustment_type']
            qty = form.cleaned_data['quantity']
            remarks = form.cleaned_data['remarks']
            delta = qty if adj_type == 'ADJUSTMENT_ADD' else -qty

            try:
                with transaction.atomic():
                    InventoryService.adjust_stock(
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

                messages.success(request, f"Stock successfully adjusted ({delta} {product.base_unit.code}).")
            except Exception as e:
                messages.error(request, str(e))
        else:
            messages.error(request, "Invalid adjustment form values.")

        return redirect('inventory:product_detail', pk=product.pk)


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
                rma_claim.total_credit_amount = sum((it.credit_amount for it in all_items), Decimal('0.00'))
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