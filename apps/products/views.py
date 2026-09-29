from decimal import Decimal, InvalidOperation
from django.shortcuts import render, redirect, get_object_or_404
from django.views.generic import TemplateView, View, FormView
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib import messages
from django.urls import reverse
from django.http import JsonResponse
from django.db import transaction
from django.utils import timezone

from apps.inventory.models import (
    Product, BranchStock, ProductCategory, ProductSubCategory, Brand, UnitOfMeasurement,
    ProductComponentWarrantyRule, UnitConversion
)
from apps.inventory.services import InventoryService
from apps.products.models import BarcodeLabelTemplate, ProductPriceTier
from apps.products.forms import ProductQuickCreateForm, BarcodePrintBatchForm, ProductPriceTierForm
from apps.products.services import ProductCatalogService
from apps.core.models import AuditLog

class ProductBarcodePrintView(LoginRequiredMixin, FormView):
    """
    Renders configured barcode sticker print sheets for 50x25mm and 80mm rolls.
    """
    template_name = 'products/barcode_print_sheet.html'
    form_class = BarcodePrintBatchForm

    def form_valid(self, form):
        product = form.cleaned_data['product']
        quantity = form.cleaned_data['quantity']
        template = form.cleaned_data.get('template') or BarcodeLabelTemplate.objects.filter(is_active=True).first()

        sticker_data = ProductCatalogService.compile_sticker_data(
            product=product,
            branch=getattr(self.request, 'active_branch', None)
        )

        labels = [sticker_data for _ in range(quantity)]

        return render(self.request, 'products/barcode_label_sheet.html', {
            'labels': labels,
            'template': template,
            'product': product,
            'quantity': quantity
        })

class ProductQuickCreateModalView(LoginRequiredMixin, UserPassesTestMixin, View):
    """
    AJAX endpoint for instant product addition at the POS counter and catalog modals.
    - Barcode is completely optional and never auto-generated if blank.
    - Subcategory is explicitly saved and returned in the JSON confirmation payload.
    - Synchronized Multi-Component Warranty Engine:
      * When registered as a serialized phone (or with warranty > 0), the product's overall
        warranty_months field is explicitly synchronized to the Handset Body rule (default 12M).
      * Component rules for DEVICE (12M), BATTERY (6M), and SCREEN (3M) are registered.
      * For non-serialized accessories, warranty cleanly sets to 0M with single source of truth.
    """

    def test_func(self):
        return self.request.user.is_superuser or getattr(self.request.user, 'role', '') in ['OWNER', 'MANAGER', 'STAFF']

    def post(self, request, *args, **kwargs):
        form = ProductQuickCreateForm(request.POST)

        # Allow dynamic category creation if a new name was passed
        cat_name = request.POST.get('new_category_name', '').strip()
        if cat_name and not request.POST.get('category'):
            cat, _ = ProductCategory.objects.get_or_create(
                name=cat_name,
                defaults={'code': cat_name[:4].upper(), 'is_active': True}
            )
            post_data = request.POST.copy()
            post_data['category'] = cat.id
            form = ProductQuickCreateForm(post_data)

        if form.is_valid():
            try:
                with transaction.atomic():
                    product = form.save(commit=False)

                    # Auto-generate unique internal SKU if omitted
                    if not product.sku:
                        product.sku = ProductCatalogService.generate_unique_sku()

                    # Clean barcode: Save scanned barcode if entered, or None if blank
                    raw_barcode = form.cleaned_data.get('barcode')
                    if raw_barcode and str(raw_barcode).strip():
                        product.barcode = str(raw_barcode).strip()
                    else:
                        product.barcode = None

                    # -----------------------------------------------------------------
                    # AUTHORITATIVE WARRANTY RESOLUTION & SYNCHRONIZATION
                    # -----------------------------------------------------------------
                    is_phone = bool(
                        product.requires_imei_tracking or
                        form.cleaned_data.get('requires_imei_tracking') or
                        request.POST.get('requires_imei_tracking') in ['true', '1', 'on']
                    )
                    product.requires_imei_tracking = is_phone

                    # Resolve Body / Overall warranty duration
                    raw_body_warranty = form.cleaned_data.get('warranty_months')
                    if raw_body_warranty is None or str(raw_body_warranty).strip() == '':
                        body_warranty = 12 if is_phone else 0
                    else:
                        try:
                            body_warranty = max(0, int(raw_body_warranty))
                        except (ValueError, TypeError):
                            body_warranty = 12 if is_phone else 0

                    raw_battery_warranty = form.cleaned_data.get('battery_warranty_months')
                    if raw_battery_warranty is None or str(raw_battery_warranty).strip() == '':
                        battery_warranty = 6 if is_phone else 0
                    else:
                        try:
                            battery_warranty = max(0, int(raw_battery_warranty))
                        except (ValueError, TypeError):
                            battery_warranty = 6 if is_phone else 0

                    raw_screen_warranty = form.cleaned_data.get('screen_warranty_months')
                    if raw_screen_warranty is None or str(raw_screen_warranty).strip() == '':
                        screen_warranty = 3 if is_phone else 0
                    else:
                        try:
                            screen_warranty = max(0, int(raw_screen_warranty))
                        except (ValueError, TypeError):
                            screen_warranty = 3 if is_phone else 0

                    conditions = form.cleaned_data.get('warranty_conditions')
                    if not conditions:
                        if is_phone or body_warranty > 0:
                            conditions = (
                                "Covers genuine manufacturing defects only. "
                                "Void if physical drop cracks, glass breakage, or liquid/water ingress found."
                            )
                        else:
                            conditions = "No warranty for consumable accessory."

                    # Guarantee top-level product warranty_months receives body warranty
                    product.warranty_months = body_warranty
                    product.save()

                    # -----------------------------------------------------------------
                    # COMPONENT-LEVEL WARRANTY RULES REGISTRATION
                    # -----------------------------------------------------------------
                    if is_phone or body_warranty > 0:
                        # 1. Main Handset Body & Motherboard (e.g. 12M)
                        ProductComponentWarrantyRule.objects.update_or_create(
                            product=product,
                            component_type='DEVICE',
                            defaults={
                                'component_name': 'Main Handset Body & Motherboard',
                                'warranty_months': body_warranty,
                                'coverage_conditions': conditions
                            }
                        )

                        # 2. Internal Battery (e.g. 6M)
                        ProductComponentWarrantyRule.objects.update_or_create(
                            product=product,
                            component_type='BATTERY',
                            defaults={
                                'component_name': 'Internal Battery',
                                'warranty_months': battery_warranty,
                                'coverage_conditions': conditions
                            }
                        )

                        # 3. Screen / Display Panel (e.g. 3M)
                        ProductComponentWarrantyRule.objects.update_or_create(
                            product=product,
                            component_type='SCREEN',
                            defaults={
                                'component_name': 'Screen / Display Panel',
                                'warranty_months': screen_warranty,
                                'coverage_conditions': conditions
                            }
                        )

                        # Single Source of Truth Guarantee: Explicitly update product warranty_months
                        Product.objects.filter(pk=product.pk).update(
                            warranty_months=body_warranty,
                            updated_at=timezone.now()
                        )
                    else:
                        Product.objects.filter(pk=product.pk).update(
                            warranty_months=0,
                            updated_at=timezone.now()
                        )

                    initial_stock = form.cleaned_data.get('initial_stock') or Decimal('0.000')
                    active_branch = getattr(request, 'active_branch', None)

                    # Prevent Ghost Stock on Serialized / IMEI-Tracked items
                    imei_warning = None
                    if product.requires_imei_tracking or product.requires_serial_tracking:
                        if initial_stock > Decimal('0.000'):
                            initial_stock = Decimal('0.000')
                            imei_warning = (
                                f"Product model '{product.name}' was registered with 0 initial stock. "
                                f"Because IMEI tracking is enabled, physical phone units must be inwarded with valid IMEIs "
                                f"via Goods Received Note (GRN) or the Excel Importer before they can be sold."
                            )
                    elif initial_stock > Decimal('0.000') and active_branch:
                        InventoryService.adjust_stock(
                            product=product,
                            branch=active_branch,
                            quantity_delta=initial_stock,
                            movement_type='ADJUSTMENT_ADD',
                            remarks="Initial stock via POS quick-add modal",
                            user=request.user,
                            allow_negative=False
                        )

                    AuditLog.objects.create(
                        user=request.user,
                        branch=active_branch,
                        action_type='CREATE',
                        module='ProductQuickCreate',
                        object_repr=str(product),
                        ip_address=request.META.get('REMOTE_ADDR'),
                        details={
                            'sku': product.sku,
                            'barcode': product.barcode or "None",
                            'category': product.category.name if product.category else "",
                            'subcategory': product.subcategory.name if product.subcategory else "",
                            'initial_stock': str(initial_stock),
                            'requires_imei': product.requires_imei_tracking,
                            'is_discountable': product.is_discountable,
                            'warranty_body': body_warranty,
                            'warranty_battery': battery_warranty,
                            'warranty_screen': screen_warranty
                        }
                    )

                if is_phone or body_warranty > 0:
                    warranty_summary_str = f"Phone: {body_warranty}M | Batt: {battery_warranty}M | Screen: {screen_warranty}M"
                else:
                    warranty_summary_str = "No Warranty"

                # Standardized Response Payload returning newly assigned subcategory details
                resp_payload = {
                    'status': 'success',
                    'product_id': product.id,
                    'name': product.name,
                    'sku': product.sku,
                    'barcode': product.barcode or '',
                    'model_name': product.model_name or '',
                    'model_number': product.model_number or '',
                    'selling_price': f"{product.selling_price:.2f}",
                    'cost_price': f"{product.purchase_price:.2f}",
                    'wholesale_price': f"{product.wholesale_price:.2f}" if product.wholesale_price else '',
                    'unit': product.base_unit.code if product.base_unit else 'PCS',
                    'category_id': product.category_id,
                    'category_name': product.category.name if product.category else '',
                    'subcategory_id': product.subcategory_id,
                    'subcategory_name': product.subcategory.name if product.subcategory else '',
                    'is_discountable': product.is_discountable,
                    'warranty_months': body_warranty,
                    'warranty_summary': warranty_summary_str,
                    'requires_imei': product.requires_imei_tracking
                }
                if imei_warning:
                    resp_payload['warning'] = imei_warning

                return JsonResponse(resp_payload)

            except Exception as err:
                return JsonResponse({'status': 'error', 'message': str(err)}, status=500)

        return JsonResponse({'status': 'error', 'errors': form.errors}, status=400)

class ProductPriceTierManagerView(LoginRequiredMixin, UserPassesTestMixin, View):
    """Manages volume-based wholesale and VIP price tiers."""

    def test_func(self):
        return self.request.user.is_superuser or getattr(self.request.user, 'role', '') in ['OWNER', 'MANAGER']

    def post(self, request, product_id, *args, **kwargs):
        product = get_object_or_404(Product, id=product_id)
        form = ProductPriceTierForm(request.POST)
        if form.is_valid():
            tier = form.save(commit=False)
            tier.product = product
            tier.save()
            messages.success(request, f"Price tier for '{product.name}' saved successfully.")
        else:
            messages.error(request, "Failed to save price tier. Please verify input values.")
        return redirect(reverse('inventory:product_detail', kwargs={'pk': product.id}))

class ProductUnitConversionManagerView(LoginRequiredMixin, UserPassesTestMixin, View):
    """Manages packaging unit conversions (e.g. 1 Box = 50 Pieces)."""

    def test_func(self):
        return self.request.user.is_superuser or getattr(self.request.user, 'role', '') in ['OWNER', 'MANAGER']

    def post(self, request, product_id, *args, **kwargs):
        product = get_object_or_404(Product, id=product_id)
        unit_name = request.POST.get('unit_name', '').strip()
        conversion_factor = request.POST.get('conversion_factor', '1')
        selling_price_per_unit = request.POST.get('selling_price_per_unit', '').strip() or None
        barcode = request.POST.get('barcode', '').strip() or None

        if not unit_name:
            messages.error(request, "Packaging unit name is required.")
            return redirect(reverse('inventory:product_detail', kwargs={'pk': product.id}))

        try:
            factor = Decimal(str(conversion_factor))
            if factor <= Decimal('0.000'):
                raise ValueError()
        except (ValueError, TypeError, InvalidOperation):
            messages.error(request, "Conversion factor must be a valid positive number greater than zero.")
            return redirect(reverse('inventory:product_detail', kwargs={'pk': product.id}))

        price = None
        if selling_price_per_unit:
            try:
                price = Decimal(str(selling_price_per_unit))
            except (ValueError, TypeError, InvalidOperation):
                price = None

        if barcode and UnitConversion.objects.filter(barcode=barcode).exclude(product=product, unit_name=unit_name).exists():
            messages.error(request, f"Barcode '{barcode}' is already in use by another packaging unit.")
            return redirect(reverse('inventory:product_detail', kwargs={'pk': product.id}))

        UnitConversion.objects.update_or_create(
            product=product,
            unit_name=unit_name,
            defaults={
                'conversion_factor': factor,
                'selling_price_per_unit': price,
                'barcode': barcode,
            }
        )
        messages.success(request, f"Packaging unit '{unit_name}' (1 {unit_name} = {factor} {product.base_unit.code}) saved successfully.")
        return redirect(reverse('inventory:product_detail', kwargs={'pk': product.id}))