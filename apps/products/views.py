from decimal import Decimal
from django.shortcuts import render, redirect, get_object_or_404
from django.views.generic import TemplateView, View, FormView
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib import messages
from django.urls import reverse
from django.http import JsonResponse

from apps.inventory.models import Product, BranchStock, ProductCategory, Brand, UnitOfMeasurement
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
    AJAX endpoint for instant product addition at the POS counter.
    Barcode is completely optional and is NEVER auto-generated if left blank.
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
            # Inject created category ID into form data
            post_data = request.POST.copy()
            post_data['category'] = cat.id
            form = ProductQuickCreateForm(post_data)

        if form.is_valid():
            product = form.save(commit=False)
            
            # Auto-generate unique internal SKU if not provided
            if not product.sku:
                product.sku = ProductCatalogService.generate_unique_sku()

            # Clean barcode: Save scanned barcode if entered, or None if blank (NO auto-generation)
            raw_barcode = form.cleaned_data.get('barcode')
            if raw_barcode and str(raw_barcode).strip():
                product.barcode = str(raw_barcode).strip()
            else:
                product.barcode = None

            product.save()

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
                    'initial_stock': str(initial_stock),
                    'requires_imei': product.requires_imei_tracking
                }
            )

            resp_payload = {
                'status': 'success',
                'product_id': product.id,
                'name': product.name,
                'sku': product.sku,
                'barcode': product.barcode or '',
                'selling_price': f"{product.selling_price:.2f}",
                'cost_price': f"{product.purchase_price:.2f}",
                'unit': product.base_unit.code if product.base_unit else 'PCS',
                'category_id': product.category_id,
                'category_name': product.category.name if product.category else ''
            }
            if imei_warning:
                resp_payload['warning'] = imei_warning

            return JsonResponse(resp_payload)

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