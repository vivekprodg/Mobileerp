from django.contrib import admin
from apps.products.models import ProductPriceTier, BarcodeLabelTemplate

@admin.register(ProductPriceTier)
class ProductPriceTierAdmin(admin.ModelAdmin):
    list_display = [
        'product', 'tier_type', 'min_quantity',
        'price_per_unit', 'is_active', 'created_at'
    ]
    list_filter = ['tier_type', 'is_active', 'product__brand', 'product__category']
    search_fields = [
        'product__name', 'product__sku', 'product__barcode',
        'product__model_name', 'product__model_number'
    ]
    list_select_related = ['product']

    fieldsets = (
        ("Tier Configuration", {
            'fields': ('product', 'tier_type', 'min_quantity', 'price_per_unit', 'is_active')
        }),
    )

@admin.register(BarcodeLabelTemplate)
class BarcodeLabelTemplateAdmin(admin.ModelAdmin):
    list_display = [
        'name', 'width_mm', 'height_mm', 'show_shop_name',
        'show_variant_info', 'show_model_number', 'show_warranty_badge',
        'show_mrp', 'show_estimate_tag', 'is_active'
    ]
    list_filter = [
        'is_active', 'show_shop_name', 'show_variant_info',
        'show_model_number', 'show_warranty_badge', 'show_estimate_tag'
    ]
    search_fields = ['name', 'custom_note']

    fieldsets = (
        ("Template Dimensions", {
            'fields': (
                'name',
                ('width_mm', 'height_mm'),
                'is_active'
            )
        }),
        ("Print Content Options for Smartphones & Items", {
            'fields': (
                ('show_shop_name', 'show_mrp'),
                ('show_product_code', 'show_model_number'),
                ('show_variant_info', 'show_warranty_badge'),
                ('show_estimate_tag', 'custom_note')
            )
        }),
    )