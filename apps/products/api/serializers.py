from decimal import Decimal
from rest_framework import serializers
from apps.inventory.models import (
    Product, ProductCategory, ProductSubCategory, Brand,
    UnitOfMeasurement, UnitConversion, BranchStock, ItemInstance
)
from apps.products.models import ProductPriceTier, BarcodeLabelTemplate

class ProductCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductCategory
        fields = ['id', 'name', 'name_np', 'code', 'description']


class BrandSerializer(serializers.ModelSerializer):
    class Meta:
        model = Brand
        fields = ['id', 'name', 'origin_country']


class UnitOfMeasurementSerializer(serializers.ModelSerializer):
    class Meta:
        model = UnitOfMeasurement
        fields = ['id', 'name', 'name_np', 'code', 'allow_decimal']


class UnitConversionSerializer(serializers.ModelSerializer):
    class Meta:
        model = UnitConversion
        fields = ['id', 'unit_name', 'conversion_factor', 'selling_price_per_unit', 'barcode']


class ProductPriceTierSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductPriceTier
        fields = ['id', 'tier_type', 'min_quantity', 'price_per_unit']


class BarcodeLabelTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = BarcodeLabelTemplate
        fields = '__all__'


class ItemInstanceSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source='product.name', read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    condition_display = serializers.CharField(source='get_condition_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    activation_display = serializers.CharField(source='get_activation_status_display', read_only=True)
    source_type_display = serializers.CharField(source='get_source_type_display', read_only=True)
    mdms_status_display = serializers.CharField(source='get_mdms_status_display', read_only=True)

    class Meta:
        model = ItemInstance
        fields = [
            'id', 'device_uid', 'product', 'product_name', 'branch', 'branch_name',
            'imei_1', 'imei_2', 'serial_number', 'device_barcode',
            'condition', 'condition_display', 'activation_status', 'activation_display',
            'source_type', 'source_type_display', 'trade_in_voucher_reference',
            'mdms_status', 'mdms_status_display', 'mdms_verification_date', 'mdms_remarks',
            'status', 'status_display', 'landed_cost', 'purchase_reference', 'supplier_name',
            'sold_invoice_reference', 'customer_name', 'customer_phone', 'sale_date',
            'warranty_start_date', 'warranty_end_date', 'warranty_remarks', 'created_at'
        ]


class ProductListSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source='category.name', read_only=True)
    brand_name = serializers.CharField(source='brand.name', read_only=True, default='')
    base_unit_code = serializers.CharField(source='base_unit.code', read_only=True)
    default_mdms_status_display = serializers.CharField(source='get_default_mdms_status_display', read_only=True)
    current_stock = serializers.SerializerMethodField()
    available_stock = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            'id', 'name', 'sku', 'barcode', 'category', 'category_name',
            'brand', 'brand_name', 'model_name', 'model_number',
            'variant_name', 'ram', 'internal_storage', 'color_variant',
            'network_type', 'sim_configuration', 'base_unit_code',
            'purchase_price', 'selling_price', 'wholesale_price',
            'default_mdms_status', 'default_mdms_status_display',
            'requires_imei_tracking', 'requires_serial_tracking',
            'is_spare_part', 'is_vat_applicable', 'vat_rate', 'rack_number', 'shelf_identifier',
            'warranty_months', 'warranty_provider', 'current_stock', 'available_stock'
        ]

    def get_current_stock(self, obj) -> str:
        request = self.context.get('request')
        branch = getattr(request, 'active_branch', None) if request else None
        if branch:
            stock = obj.branch_stocks.filter(branch=branch).first()
            return str(stock.quantity) if stock else "0.000"
        return "0.000"

    def get_available_stock(self, obj) -> str:
        request = self.context.get('request')
        branch = getattr(request, 'active_branch', None) if request else None
        if branch:
            stock = obj.branch_stocks.filter(branch=branch).first()
            return str(stock.available_quantity) if stock else "0.000"
        return "0.000"


class ProductDetailSerializer(serializers.ModelSerializer):
    category = ProductCategorySerializer(read_only=True)
    brand = BrandSerializer(read_only=True)
    base_unit = UnitOfMeasurementSerializer(read_only=True)
    unit_conversions = UnitConversionSerializer(many=True, read_only=True)
    price_tiers = ProductPriceTierSerializer(many=True, read_only=True)
    default_mdms_status_display = serializers.CharField(source='get_default_mdms_status_display', read_only=True)
    stock_by_branch = serializers.SerializerMethodField()
    in_stock_imei_units = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            'id', 'name', 'sku', 'barcode', 'category', 'brand',
            'model_name', 'model_number', 'is_spare_part', 'description', 'image',
            'variant_name', 'ram', 'internal_storage', 'color_variant',
            'network_type', 'sim_configuration', 'region_variant',
            'default_mdms_status', 'default_mdms_status_display',
            'operating_system', 'processor_chipset', 'display_size', 'display_type',
            'display_resolution', 'refresh_rate', 'rear_camera', 'front_camera',
            'battery_capacity', 'fast_charging', 'fingerprint_sensor', 'face_unlock',
            'expandable_storage', 'max_memory_card', 'usb_port_type', 'headphone_jack_35mm',
            'wifi_spec', 'bluetooth_version', 'nfc_available', 'gps_capabilities',
            'size_dimension', 'base_unit',
            'purchase_price', 'selling_price', 'wholesale_price', 'max_discount_percent',
            'is_vat_applicable', 'vat_rate',
            'inventory_tracking_type', 'requires_imei_tracking', 'requires_serial_tracking',
            'reorder_level', 'rack_number', 'shelf_identifier', 'bin_location',
            'warranty_months', 'warranty_provider', 'unit_conversions',
            'price_tiers', 'stock_by_branch', 'in_stock_imei_units'
        ]

    def get_stock_by_branch(self, obj):
        return [
            {
                'branch_id': bs.branch.id,
                'branch_name': bs.branch.name,
                'quantity': str(bs.quantity),
                'reserved_quantity': str(bs.reserved_quantity),
                'quarantined_defective_quantity': str(bs.quarantined_defective_quantity),
                'available_quantity': str(bs.available_quantity),
                'is_low_stock': bs.is_low_stock
            }
            for bs in obj.branch_stocks.select_related('branch').all()
        ]

    def get_in_stock_imei_units(self, obj):
        request = self.context.get('request')
        branch = getattr(request, 'active_branch', None) if request else None
        qs = obj.tracked_instances.filter(status='IN_STOCK')
        if branch:
            qs = qs.filter(branch=branch)
        return [
            {
                'id': unit.id,
                'device_uid': unit.device_uid,
                'imei_1': unit.imei_1,
                'imei_2': unit.imei_2,
                'serial_number': unit.serial_number,
                'condition': unit.get_condition_display(),
                'source_type': unit.get_source_type_display(),
                'mdms_status': unit.mdms_status,
                'mdms_status_display': unit.get_mdms_status_display(),
                'activation_status': unit.get_activation_status_display(),
                'landed_cost': str(unit.landed_cost),
                'purchase_date': unit.purchase_date
            }
            for unit in qs[:50]
        ]