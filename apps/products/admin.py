"""
Django Admin Configuration for Products Module.

Registers:
1. ProductPriceTier: Wholesale, Retail, Dealer quantity pricing tiers.
2. BarcodeLabelTemplate: Thermal barcode sticker printer profiles.
3. HistoricalProduct: Django Admin interface for generic non-serialized placeholder items
   ('Mobile (Historical)' and 'Various Items (Historical)') with 1-click generation action.
"""

from django.contrib import admin
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from apps.products.models import ProductPriceTier, BarcodeLabelTemplate, HistoricalProduct


# =============================================================================
# 1. PRODUCT PRICE TIER ADMIN
# =============================================================================

@admin.register(ProductPriceTier)
class ProductPriceTierAdmin(admin.ModelAdmin):
    list_display = [
        'product', 'tier_type_badge', 'min_quantity',
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

    def tier_type_badge(self, obj):
        colors = {
            'RETAIL': '#10b981',
            'WHOLESALE': '#3b82f6',
            'DEALER': '#f59e0b',
            'SPECIAL': '#8b5cf6',
        }
        color = colors.get(obj.tier_type, '#64748b')
        return format_html(
            '<span style="color: white; background-color: {}; padding: 2px 8px; border-radius: 999px; font-weight: 700; font-size: 10px;">{}</span>',
            color, obj.get_tier_type_display()
        )
    tier_type_badge.short_description = _("Tier Type")


# =============================================================================
# 2. BARCODE LABEL TEMPLATE ADMIN
# =============================================================================

@admin.register(BarcodeLabelTemplate)
class BarcodeLabelTemplateAdmin(admin.ModelAdmin):
    list_display = [
        'name', 'dimensions_display', 'show_shop_name',
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

    def dimensions_display(self, obj):
        return format_html('<span class="font-monospace fw-bold">{} &times; {} mm</span>', obj.width_mm, obj.height_mm)
    dimensions_display.short_description = _("Dimensions")


# =============================================================================
# 3. HISTORICAL PRODUCT PLACEHOLDER ADMIN (FOR MOBILESOFT MIGRATION)
# =============================================================================

@admin.register(HistoricalProduct)
class HistoricalProductAdmin(admin.ModelAdmin):
    """
    Dedicated Admin for managing Mobilesoft historical migration placeholder products:
    'Mobile (Historical)' and 'Various Items (Historical)'.
    Provides 1-click action to create or re-verify both items with zero stock impact and no IMEI prompts.
    """
    list_display = [
        'name', 'sku', 'category', 'imei_status_badge',
        'tax_compliance_badge', 'selling_price', 'is_discountable_badge', 'updated_at'
    ]
    search_fields = ['name', 'sku', 'description']
    actions = ['seed_placeholders_action']

    fieldsets = (
        ("Historical Tax Product Placeholder Identity", {
            'description': _(
                "These items are used strictly for importing historical 2080 B.S. Mobilesoft tax sales. "
                "They are non-serialized (IMEI disabled), non-discountable, and 13% VAT tax-inclusive."
            ),
            'fields': (
                ('name', 'sku'),
                ('category', 'brand', 'base_unit'),
                ('model_name', 'description')
            )
        }),
        ("Tax Compliance & Pricing", {
            'fields': (
                ('selling_price', 'purchase_price'),
                ('tax_pricing_type', 'is_vat_applicable', 'vat_rate'),
                'is_discountable'
            )
        }),
        ("Serialized Tracking Controls (Must Remain OFF)", {
            'fields': (
                ('requires_imei_tracking', 'requires_serial_tracking', 'inventory_tracking_type'),
            )
        }),
    )

    def imei_status_badge(self, obj):
        if not obj.requires_imei_tracking:
            return format_html(
                '<span style="color: #065f46; background-color: #ecfdf5; border: 1px solid #a7f3d0; padding: 2px 8px; border-radius: 999px; font-weight: 700; font-size: 10px;">'
                '<i class="fas fa-check me-1"></i> NON-SERIALIZED (NO IMEI REQUIRED)'
                '</span>'
            )
        return format_html(
            '<span style="color: #991b1b; background-color: #fef2f2; border: 1px solid #fecaca; padding: 2px 8px; border-radius: 999px; font-weight: 700; font-size: 10px;">'
            '<i class="fas fa-exclamation-triangle me-1"></i> SERIALIZED (ERROR FOR IMPORT)'
            '</span>'
        )
    imei_status_badge.short_description = _("IMEI Policy")

    def tax_compliance_badge(self, obj):
        if obj.is_vat_applicable and obj.vat_rate > 0:
            return format_html(
                '<span style="color: #1e40af; background-color: #eff6ff; border: 1px solid #bfdbfe; padding: 2px 8px; border-radius: 999px; font-weight: 700; font-size: 10px;">'
                '13% VAT (ANNEX-5 ALIGNED)'
                '</span>'
            )
        return format_html('<span style="color: #64748b; font-size: 10px;">NON-TAX</span>')
    tax_compliance_badge.short_description = _("Tax Mode")

    def is_discountable_badge(self, obj):
        if not obj.is_discountable:
            return format_html(
                '<span style="color: #475569; background-color: #f1f5f9; padding: 2px 7px; border-radius: 999px; font-size: 10px;">LOCKED (NO DISCOUNT)</span>'
            )
        return format_html('<span style="color: #f59e0b; font-size: 10px;">DISCOUNTABLE</span>')
    is_discountable_badge.short_description = _("Discount Flag")

    @admin.action(description=_("Seed / Refresh Historical Placeholders ('Mobile' & 'Various Items')"))
    def seed_placeholders_action(self, request, queryset=None):
        """
        Admin action that initializes or verifies both placeholder items on the fly.
        """
        p_mobile, p_various = HistoricalProduct.get_or_create_placeholders()
        self.message_user(
            request,
            format_html(
                "Successfully initialized/verified generic historical placeholders: <br>"
                "• <strong>{}</strong> (SKU: {}) [IMEI Tracking: OFF, 13% VAT]<br>"
                "• <strong>{}</strong> (SKU: {}) [IMEI Tracking: OFF, 13% VAT]",
                p_mobile.name, p_mobile.sku,
                p_various.name, p_various.sku
            )
        )

    def has_delete_permission(self, request, obj=None):
        """Prevents deletion of system placeholders."""
        if obj and obj.sku in ['HIST-MOBILE-001', 'HIST-VARIOUS-001']:
            return False
        return super().has_delete_permission(request, obj)