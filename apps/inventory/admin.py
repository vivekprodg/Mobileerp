"""
Django Admin Configuration for Inventory, Warehouses, IMEI Handset Tracking, Batches & Audit Logs.
"""

from django.contrib import admin
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from apps.inventory.models import (
    UnitOfMeasurement, ProductCategory, ProductSubCategory, Brand,
    Product, ProductComponentWarrantyRule, UnitConversion, BranchStock,
    ProductBatch, ItemInstance, DeviceComponentWarranty,
    VendorRMAClaim, VendorRMAClaimItem, StockMovementLog
)

# ==============================================================================
# 1. INLINE MODEL ADMINS
# ==============================================================================

class ProductComponentWarrantyRuleInline(admin.TabularInline):
    model = ProductComponentWarrantyRule
    extra = 1
    fields = ['component_type', 'component_name', 'warranty_months', 'coverage_conditions']


class UnitConversionInline(admin.TabularInline):
    model = UnitConversion
    extra = 0
    fields = ['unit_name', 'conversion_factor', 'selling_price_per_unit', 'barcode']


class BranchStockInline(admin.TabularInline):
    model = BranchStock
    extra = 0
    fields = ['branch', 'quantity', 'reserved_quantity', 'quarantined_defective_quantity', 'low_stock_threshold']
    readonly_fields = ['branch', 'quantity', 'reserved_quantity', 'quarantined_defective_quantity']
    can_delete = False


class ProductBatchInline(admin.TabularInline):
    model = ProductBatch
    extra = 0
    fields = [
        'batch_number', 'branch', 'purchase_date', 'cost_price',
        'selling_price', 'quantity_received', 'quantity_remaining', 'is_depleted'
    ]
    readonly_fields = [
        'batch_number', 'branch', 'purchase_date', 'cost_price',
        'selling_price', 'quantity_received', 'quantity_remaining', 'is_depleted'
    ]
    can_delete = False
    show_change_link = True


class ItemInstanceInline(admin.TabularInline):
    model = ItemInstance
    extra = 0
    fields = [
        'imei_1', 'imei_2', 'imei_2_pending_scan', 'condition', 'mdms_status',
        'source_type', 'landed_cost', 'purchase_date', 'status', 'branch', 'sold_invoice_reference'
    ]
    readonly_fields = ['device_uid', 'landed_cost', 'purchase_date', 'sold_invoice_reference', 'customer_name']
    show_change_link = True


class DeviceComponentWarrantyInline(admin.TabularInline):
    model = DeviceComponentWarranty
    extra = 0
    fields = [
        'component_type', 'component_name', 'warranty_months',
        'warranty_start_date', 'warranty_expiry_date', 'status', 'claim_count'
    ]
    readonly_fields = [
        'component_type', 'component_name', 'warranty_months',
        'warranty_start_date', 'warranty_expiry_date', 'claim_count'
    ]
    can_delete = False


class VendorRMAClaimItemInline(admin.TabularInline):
    model = VendorRMAClaimItem
    extra = 0
    fields = [
        'product', 'defective_serial_or_imei', 'defect_description',
        'resolution', 'replacement_batch_or_serial', 'credit_amount'
    ]
    show_change_link = True


# ==============================================================================
# 2. PRODUCT CATALOG ADMIN
# ==============================================================================

@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = [
        'name', 'sku', 'barcode_display', 'category', 'brand',
        'display_variant_info', 'selling_price', 'purchase_price',
        'imei_tracking_badge', 'is_discountable_badge', 'tax_pricing_type_badge',
        'mdms_default_badge', 'is_spare_part_badge', 'rack_number'
    ]
    list_filter = [
        'requires_imei_tracking', 'is_discountable', 'default_mdms_status',
        'tax_pricing_type', 'is_spare_part', 'category', 'brand',
        'network_type', 'is_vat_applicable', 'created_at'
    ]
    search_fields = [
        'name', 'sku', 'barcode', 'model_name', 'model_number',
        'processor_chipset', 'rack_number'
    ]
    list_select_related = ['category', 'brand', 'base_unit']
    inlines = [
        ProductComponentWarrantyRuleInline, UnitConversionInline,
        BranchStockInline, ProductBatchInline, ItemInstanceInline
    ]

    fieldsets = (
        ("1. Product Identification", {
            'fields': (
                ('name', 'sku', 'barcode'),
                ('category', 'subcategory', 'brand'),
                ('model_name', 'model_number'),
                ('is_spare_part', 'image'),
                'description'
            )
        }),
        ("2. Mobile Variant & NTA MDMS Classification", {
            'fields': (
                ('variant_name', 'ram', 'internal_storage'),
                ('color_variant', 'network_type', 'sim_configuration'),
                ('region_variant', 'default_mdms_status')
            )
        }),
        ("3. Smartphone Hardware & Display Specifications", {
            'classes': ('collapse',),
            'fields': (
                ('operating_system', 'processor_chipset'),
                ('display_size', 'display_type'),
                ('display_resolution', 'refresh_rate'),
                ('rear_camera', 'front_camera'),
                ('battery_capacity', 'fast_charging'),
                ('fingerprint_sensor', 'face_unlock'),
                ('expandable_storage', 'max_memory_card'),
                ('usb_port_type', 'headphone_jack_35mm'),
                ('wifi_spec', 'bluetooth_version'),
                ('nfc_available', 'gps_capabilities'),
                ('size_dimension', 'base_unit')
            )
        }),
        ("4. Pricing & Dynamic Tax Calculations", {
            'fields': (
                ('purchase_price', 'selling_price', 'wholesale_price'),
                ('is_discountable', 'max_discount_percent'),
                ('tax_pricing_type', 'is_vat_applicable', 'vat_rate')
            )
        }),
        ("5. Inventory & Warehouse Shelf Location", {
            'fields': (
                ('inventory_tracking_type', 'requires_imei_tracking', 'requires_serial_tracking'),
                ('reorder_level', 'rack_number', 'shelf_identifier', 'bin_location')
            )
        }),
        ("6. Official Overall Warranty Terms", {
            'fields': (('warranty_months', 'warranty_provider'),)
        }),
    )

    def barcode_display(self, obj):
        if obj.barcode:
            return format_html('<span class="font-monospace text-primary fw-bold">{}</span>', obj.barcode)
        return format_html('<span style="color: #94a3b8; font-style: italic;">(None)</span>')
    barcode_display.short_description = _("Barcode")

    def display_variant_info(self, obj):
        if obj.ram and obj.internal_storage:
            return f"{obj.ram}/{obj.internal_storage} ({obj.color_variant or 'Std'})"
        return obj.variant_name or "-"
    display_variant_info.short_description = _("Variant (RAM/ROM)")

    def imei_tracking_badge(self, obj):
        if obj.requires_imei_tracking:
            return format_html(
                '<span style="color: #1e40af; background-color: #dbeafe; border: 1px solid #bfdbfe; padding: 2px 7px; border-radius: 999px; font-weight: 700; font-size: 10px;">'
                '<i class="fas fa-barcode me-1"></i> IMEI MANDATORY'
                '</span>'
            )
        return format_html(
            '<span style="color: #475569; background-color: #f1f5f9; padding: 2px 7px; border-radius: 999px; font-size: 10px;">'
            'NON-SERIALIZED'
            '</span>'
        )
    imei_tracking_badge.short_description = _("IMEI Policy")

    def is_discountable_badge(self, obj):
        if obj.is_discountable:
            return format_html(
                '<span style="color: #065f46; background-color: #ecfdf5; border: 1px solid #a7f3d0; padding: 2px 7px; border-radius: 999px; font-weight: 700; font-size: 10px;">'
                '<i class="fas fa-check me-1"></i> YES (Max {}%)</span>',
                obj.max_discount_percent
            )
        return format_html(
            '<span style="color: #991b1b; background-color: #fef2f2; border: 1px solid #fecaca; padding: 2px 7px; border-radius: 999px; font-weight: 700; font-size: 10px;">'
            '<i class="fas fa-ban me-1"></i> NO DISCOUNT</span>'
        )
    is_discountable_badge.short_description = _("Discount Allowed")

    def tax_pricing_type_badge(self, obj):
        if not obj.is_vat_applicable or obj.tax_pricing_type == 'EXEMPT':
            return format_html(
                '<span style="color: white; background-color: #64748b; padding: 2px 8px; border-radius: 999px; font-weight: bold; font-size: 10px;">EXEMPT (0%)</span>'
            )
        elif obj.tax_pricing_type == 'INCLUSIVE':
            return format_html(
                '<span style="color: white; background-color: #10b981; padding: 2px 8px; border-radius: 999px; font-weight: bold; font-size: 10px;">INCL ({}%)</span>',
                obj.vat_rate
            )
        else:
            return format_html(
                '<span style="color: white; background-color: #3b82f6; padding: 2px 8px; border-radius: 999px; font-weight: bold; font-size: 10px;">+EXCL ({}%)</span>',
                obj.vat_rate
            )
    tax_pricing_type_badge.short_description = _("Tax Mode")

    def mdms_default_badge(self, obj):
        if obj.default_mdms_status == 'REGISTERED_OFFICIAL':
            return format_html('<span style="color: #065f46; background-color: #ecfdf5; border: 1px solid #a7f3d0; padding: 2px 8px; border-radius: 999px; font-weight: 700; font-size: 10px;">MDMS REGISTERED</span>')
        elif obj.default_mdms_status == 'GRAY_UNREGISTERED':
            return format_html('<span style="color: #991b1b; background-color: #fef2f2; border: 1px solid #fecaca; padding: 2px 8px; border-radius: 999px; font-weight: 700; font-size: 10px;">GRAY MARKET</span>')
        return format_html('<span style="color: #475569; background-color: #f1f5f9; padding: 2px 8px; border-radius: 999px; font-size: 10px;">{}</span>', obj.get_default_mdms_status_display())
    mdms_default_badge.short_description = _("Default MDMS")

    def is_spare_part_badge(self, obj):
        if obj.is_spare_part:
            return format_html('<span style="color: white; background-color: #ec4899; padding: 2px 7px; border-radius: 999px; font-weight: bold; font-size: 10px;">SPARE PART</span>')
        return format_html('<span style="color: #64748b; font-size: 10px;">STANDARD</span>')
    is_spare_part_badge.short_description = _("Item Type")


# ==============================================================================
# 3. ITEM INSTANCE (IMEI / SERIAL) ADMIN
# ==============================================================================

@admin.register(ItemInstance)
class ItemInstanceAdmin(admin.ModelAdmin):
    list_display = [
        'product', 'branch', 'imei_1', 'imei_2', 'imei_2_status_badge',
        'condition_badge', 'mdms_status_badge', 'source_type_badge',
        'landed_cost', 'purchase_date', 'status_badge', 'sold_invoice_reference', 'customer_name'
    ]
    list_filter = [
        'status', 'imei_2_pending_scan', 'mdms_status', 'source_type',
        'condition', 'activation_status', 'branch', 'purchase_date'
    ]
    search_fields = [
        'imei_1', 'imei_2', 'serial_number', 'device_uid',
        'trade_in_voucher_reference', 'sold_invoice_reference', 'customer_phone'
    ]
    list_select_related = ['product', 'branch']
    readonly_fields = ['device_uid', 'created_at', 'updated_at']
    inlines = [DeviceComponentWarrantyInline]

    fieldsets = (
        ("Device Identification & Dual-IMEI Tracking", {
            'fields': (
                ('product', 'branch'),
                ('imei_1', 'imei_2', 'imei_2_pending_scan'),
                ('serial_number', 'device_uid', 'device_barcode')
            )
        }),
        ("Condition, Origin & NTA MDMS Compliance", {
            'fields': (
                ('condition', 'activation_status'),
                ('source_type', 'trade_in_voucher_reference'),
                ('mdms_status', 'mdms_verification_date'),
                'mdms_remarks'
            )
        }),
        ("Acquisition & Cost", {
            'fields': (
                ('purchase_reference', 'batch_reference', 'supplier_name'),
                ('landed_cost', 'purchase_date')
            )
        }),
        ("Sales & Customer Allocation", {
            'fields': (
                ('status', 'sold_invoice_reference'),
                ('customer_name', 'customer_phone', 'sale_date', 'sold_price'),
                ('warranty_start_date', 'warranty_end_date', 'warranty_remarks')
            )
        }),
    )

    def imei_2_status_badge(self, obj):
        if not obj.product.requires_imei_tracking:
            return "-"
        if obj.imei_2 and obj.imei_2.strip() != '':
            return format_html(
                '<span style="color: #065f46; background-color: #ecfdf5; border: 1px solid #a7f3d0; padding: 2px 7px; border-radius: 999px; font-weight: 700; font-size: 10px;">'
                '<i class="fas fa-check-circle me-1"></i> CAPTURED'
                '</span>'
            )
        if obj.imei_2_pending_scan:
            return format_html(
                '<span style="color: #92400e; background-color: #fef08a; border: 1px solid #eab308; padding: 2px 7px; border-radius: 999px; font-weight: 700; font-size: 10px;">'
                '<i class="fas fa-triangle-exclamation me-1"></i> PENDING SCAN'
                '</span>'
            )
        return format_html('<span style="color: #64748b; font-size: 10px;">SINGLE SIM</span>')
    imei_2_status_badge.short_description = _("IMEI 2 Status")

    def condition_badge(self, obj):
        colors = {
            'BRAND_NEW': '#10b981',
            'OPEN_BOX': '#3b82f6',
            'REFURBISHED': '#8b5cf6',
            'USED_GRADE_A': '#0ea5e9',
            'USED_GRADE_B': '#f59e0b',
            'USED_GRADE_C': '#ef4444',
        }
        color = colors.get(obj.condition, '#64748b')
        return format_html(
            '<span style="color: white; background-color: {}; padding: 3px 8px; border-radius: 999px; font-weight: 600; font-size: 11px;">{}</span>',
            color, obj.get_condition_display()
        )
    condition_badge.short_description = _("Condition")

    def mdms_status_badge(self, obj):
        if obj.mdms_status == 'REGISTERED_OFFICIAL':
            return format_html(
                '<span style="color: #065f46; background-color: #ecfdf5; border: 1px solid #a7f3d0; padding: 3px 8px; border-radius: 999px; font-weight: 700; font-size: 10.5px;">'
                '<i class="fas fa-check-circle me-1"></i> MDMS OK</span>'
            )
        elif obj.mdms_status == 'GRAY_UNREGISTERED':
            return format_html(
                '<span style="color: #991b1b; background-color: #fef2f2; border: 1px solid #fecaca; padding: 3px 8px; border-radius: 999px; font-weight: 700; font-size: 10.5px;">'
                '<i class="fas fa-exclamation-triangle me-1"></i> GRAY MARKET</span>'
            )
        elif obj.mdms_status == 'INDIVIDUAL_CUSTOMS_PAID':
            return format_html(
                '<span style="color: #1e40af; background-color: #eff6ff; border: 1px solid #bfdbfe; padding: 3px 8px; border-radius: 999px; font-weight: 700; font-size: 10.5px;">CUSTOMS PAID</span>'
            )
        return format_html('<span style="color: #475569; background-color: #f1f5f9; padding: 3px 8px; border-radius: 999px; font-size: 10.5px;">EXEMPT</span>')
    mdms_status_badge.short_description = _("NTA MDMS")

    def source_type_badge(self, obj):
        if obj.source_type == 'CUSTOMER_EXCHANGE_TRADE_IN':
            return format_html(
                '<span style="color: #92400e; background-color: #fffbeb; border: 1px solid #fde68a; padding: 2px 7px; border-radius: 999px; font-weight: 700; font-size: 10px;">TRADE-IN</span>'
            )
        elif obj.source_type == 'NEW_PURCHASE_GRN':
            return format_html(
                '<span style="color: #065f46; background-color: #ecfdf5; padding: 2px 7px; border-radius: 999px; font-weight: 700; font-size: 10px;">BRAND NEW</span>'
            )
        return format_html('<span style="color: #475569; font-size: 10px;">{}</span>', obj.get_source_type_display())
    source_type_badge.short_description = _("Source Origin")

    def status_badge(self, obj):
        colors = {
            'IN_STOCK': '#10b981',
            'SOLD': '#64748b',
            'UNDER_SERVICE': '#f59e0b',
            'RETURNED_DEFECTIVE': '#ef4444',
            'ARCHIVED': '#475569',
            'TRANSFERRED': '#0ea5e9',
        }
        color = colors.get(obj.status, '#334155')
        return format_html(
            '<span style="color: white; background-color: {}; padding: 3px 8px; border-radius: 999px; font-weight: 600; font-size: 11px;">{}</span>',
            color, obj.get_status_display()
        )
    status_badge.short_description = _("Stock Status")


# ==============================================================================
# 4. COMPONENT WARRANTY ADMINS
# ==============================================================================

@admin.register(ProductComponentWarrantyRule)
class ProductComponentWarrantyRuleAdmin(admin.ModelAdmin):
    list_display = ['product', 'component_type', 'component_name', 'warranty_months', 'coverage_conditions']
    list_filter = ['component_type', 'warranty_months', 'product__brand']
    search_fields = ['product__name', 'component_name', 'coverage_conditions']
    list_select_related = ['product']


@admin.register(DeviceComponentWarranty)
class DeviceComponentWarrantyAdmin(admin.ModelAdmin):
    list_display = [
        'item_instance', 'component_name', 'component_type', 'warranty_months',
        'warranty_start_date', 'warranty_expiry_date', 'status_badge', 'claim_count'
    ]
    list_filter = ['status', 'component_type', 'warranty_expiry_date']
    search_fields = [
        'item_instance__imei_1', 'item_instance__imei_2',
        'item_instance__serial_number', 'item_instance__customer_name', 'component_name'
    ]
    list_select_related = ['item_instance']
    readonly_fields = ['created_at', 'updated_at']

    def status_badge(self, obj):
        colors = {
            'ACTIVE': '#10b981',
            'CLAIMED': '#3b82f6',
            'EXPIRED': '#94a3b8',
            'VOID': '#ef4444',
        }
        color = colors.get(obj.status, '#64748b')
        return format_html(
            '<span style="color: white; background-color: {}; padding: 3px 8px; border-radius: 999px; font-weight: 600; font-size: 11px;">{}</span>',
            color, obj.get_status_display()
        )
    status_badge.short_description = _("Warranty Status")


# ==============================================================================
# 5. VENDOR RMA CLAIM ADMINS
# ==============================================================================

@admin.register(VendorRMAClaim)
class VendorRMAClaimAdmin(admin.ModelAdmin):
    list_display = [
        'rma_number', 'supplier', 'branch', 'status_badge', 'dispatch_date',
        'total_claimed_parts_count', 'total_credit_amount', 'dispatched_by'
    ]
    list_filter = ['status', 'branch', 'dispatch_date', 'supplier']
    search_fields = ['rma_number', 'supplier__company_name', 'distributor_tracking_ref', 'resolution_notes']
    list_select_related = ['supplier', 'branch', 'dispatched_by']
    readonly_fields = ['rma_number', 'created_at', 'updated_at']
    inlines = [VendorRMAClaimItemInline]

    def status_badge(self, obj):
        colors = {
            'DRAFT': '#94a3b8',
            'DISPATCHED_TO_VENDOR': '#f59e0b',
            'PARTIALLY_SETTLED': '#3b82f6',
            'COMPLETED': '#10b981',
            'REJECTED': '#ef4444',
        }
        color = colors.get(obj.status, '#64748b')
        return format_html(
            '<span style="color: white; background-color: {}; padding: 3px 8px; border-radius: 999px; font-weight: 600; font-size: 11px;">{}</span>',
            color, obj.get_status_display()
        )
    status_badge.short_description = _("RMA Status")


@admin.register(VendorRMAClaimItem)
class VendorRMAClaimItemAdmin(admin.ModelAdmin):
    list_display = ['rma_claim', 'product', 'defective_serial_or_imei', 'resolution', 'replacement_batch_or_serial', 'credit_amount']
    list_filter = ['resolution', 'rma_claim__supplier']
    search_fields = ['defective_serial_or_imei', 'product__name', 'rma_claim__rma_number']
    list_select_related = ['rma_claim', 'product']


# ==============================================================================
# 6. BATCHES, UNITS & CATEGORIES
# ==============================================================================

@admin.register(ProductBatch)
class ProductBatchAdmin(admin.ModelAdmin):
    list_display = [
        'batch_number', 'product', 'branch', 'purchase_date',
        'cost_price', 'selling_price', 'quantity_received',
        'quantity_remaining', 'is_depleted'
    ]
    list_filter = ['is_depleted', 'branch', 'purchase_date', 'product__category']
    search_fields = ['batch_number', 'product__name', 'product__sku', 'grn_reference', 'supplier_name']
    list_select_related = ['product', 'branch']
    readonly_fields = ['created_at', 'updated_at']


@admin.register(UnitOfMeasurement)
class UnitOfMeasurementAdmin(admin.ModelAdmin):
    list_display = ['name', 'name_np', 'code', 'allow_decimal']
    search_fields = ['name', 'name_np', 'code']


@admin.register(ProductCategory)
class ProductCategoryAdmin(admin.ModelAdmin):
    list_display = ['name', 'code', 'name_np', 'is_active']
    list_filter = ['is_active']
    search_fields = ['name', 'code', 'name_np']


@admin.register(ProductSubCategory)
class ProductSubCategoryAdmin(admin.ModelAdmin):
    list_display = ['name', 'category', 'code']
    list_filter = ['category']
    search_fields = ['name', 'code', 'category__name']
    list_select_related = ['category']


@admin.register(Brand)
class BrandAdmin(admin.ModelAdmin):
    list_display = ['name', 'origin_country']
    search_fields = ['name', 'origin_country']


# ==============================================================================
# 7. BRANCH STOCK & IMMUTABLE LEDGERS
# ==============================================================================

@admin.register(BranchStock)
class BranchStockAdmin(admin.ModelAdmin):
    list_display = [
        'product', 'branch', 'quantity', 'reserved_quantity',
        'quarantined_defective_quantity', 'low_stock_threshold'
    ]
    list_filter = ['branch', 'product__category', 'product__is_spare_part']
    search_fields = ['product__name', 'product__sku', 'product__barcode']
    list_select_related = ['product', 'branch']


@admin.register(StockMovementLog)
class StockMovementLogAdmin(admin.ModelAdmin):
    """
    Immutable audit ledger for all warehouse movements.
    Creation, editing, and deletion are permanently disabled.
    """
    list_display = [
        'created_at', 'movement_type', 'product', 'branch',
        'quantity_delta', 'new_quantity', 'reference_document',
        'imei_or_serial_number', 'user'
    ]
    list_filter = ['movement_type', 'branch', 'created_at']
    search_fields = ['product__name', 'reference_document', 'imei_or_serial_number', 'remarks']
    list_select_related = ['product', 'branch', 'user']
    readonly_fields = [f.name for f in StockMovementLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False