"""
Inventory & Product Catalog Models: Warehouses, Physical Shelf Stock,
IMEI Handset Instances, FIFO Batches, Warranties, and Immutable Audit Logs.
"""

import uuid
from decimal import Decimal
from datetime import date
from django.db import models
from django.conf import settings
from django.utils.translation import gettext_lazy as _
from apps.core.models import TimeStampedModel
from apps.branches.models import Branch

class UnitOfMeasurement(TimeStampedModel):
    """Standard Base Units: Piece, Pair, Set, Meter, Gram, Kilogram."""
    name = models.CharField(max_length=50, unique=True, db_index=True, verbose_name=_("Unit Name (e.g. Piece, Pair)"))
    name_np = models.CharField(max_length=50, blank=True, null=True, verbose_name=_("Nepali Unit Name (e.g. पिस, जोडी)"))
    code = models.CharField(max_length=10, unique=True, db_index=True, verbose_name=_("Short Code (e.g. PCS, PR, BOX)"))
    allow_decimal = models.BooleanField(default=False, help_text=_("Enable for kg, meter, weight items."))

    class Meta:
        db_table = 'inv_units_of_measurement'
        ordering = ['name']
        verbose_name = _('Unit of Measurement')
        verbose_name_plural = _('Units of Measurement')
        indexes = [
            models.Index(fields=['name'], name='idx_uom_name'),
        ]

    def __str__(self):
        return f"{self.name} ({self.code})"

class ProductCategory(TimeStampedModel):
    """Top-level categories: Mobile Phones, Mobile Accessories, Smartwatches, Spectacles, Sunglasses, etc."""
    name = models.CharField(max_length=100, unique=True, db_index=True, verbose_name=_("Category Name"))
    name_np = models.CharField(max_length=100, blank=True, null=True, verbose_name=_("Category Name (Nepali)"))
    code = models.CharField(max_length=20, unique=True, db_index=True, verbose_name=_("Code (e.g. MOB, ACC, OPT)"))
    description = models.TextField(blank=True, null=True)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        db_table = 'inv_categories'
        ordering = ['name']
        verbose_name = _('Product Category')
        verbose_name_plural = _('Product Categories')
        indexes = [
            models.Index(fields=['name'], name='idx_prodcat_name'),
            models.Index(fields=['is_active', 'name'], name='idx_prodcat_active_name'),
        ]

    def __str__(self):
        return self.name

class ProductSubCategory(TimeStampedModel):
    category = models.ForeignKey(ProductCategory, on_delete=models.CASCADE, related_name='subcategories')
    name = models.CharField(max_length=100, db_index=True, verbose_name=_("Sub-Category Name"))
    code = models.CharField(max_length=20, blank=True, null=True, db_index=True)

    class Meta:
        db_table = 'inv_subcategories'
        unique_together = ('category', 'name')
        ordering = ['name']
        verbose_name = _('Product Sub-Category')
        verbose_name_plural = _('Product Sub-Categories')
        indexes = [
            models.Index(fields=['category', 'name'], name='idx_subcat_cat_name'),
        ]

    def __str__(self):
        return f"{self.category.name} -> {self.name}"

class Brand(TimeStampedModel):
    name = models.CharField(max_length=100, unique=True, db_index=True, verbose_name=_("Brand Name"))
    origin_country = models.CharField(max_length=50, blank=True, default="Nepal")

    class Meta:
        db_table = 'inv_brands'
        ordering = ['name']
        verbose_name = _('Brand')
        verbose_name_plural = _('Brands')
        indexes = [
            models.Index(fields=['name'], name='idx_brand_name'),
        ]

    def __str__(self):
        return self.name

class Product(TimeStampedModel):
    """
    Comprehensive product catalog supporting Brand-New Phones, Pre-Owned (Trade-In) Handsets,
    Accessories, Watches, and Optical equipment with NTA MDMS compliance classification.
    Barcode is fully optional (null=True, blank=True).
    Supports is_discountable for non-discountable goods (e.g., recharge cards, fixed-rate items).
    """
    TRACKING_TYPE_CHOICES = [
        ('STANDARD', 'Standard Quantity / Batch Tracking'),
        ('IMEI', 'Unique IMEI Serialized Tracking (Smartphones/Tablets)'),
        ('SERIAL', 'Serial Number Tracking (Smartwatches/Laptops)'),
    ]

    NETWORK_TYPE_CHOICES = [
        ('5G', '5G / LTE / 3G / 2G'),
        ('4G', '4G VoLTE / 3G / 2G'),
        ('3G', '3G / 2G Only'),
        ('WIFI_ONLY', 'Wi-Fi Only (Non-Cellular)'),
    ]

    SIM_CONFIG_CHOICES = [
        ('DUAL_SIM', 'Dual Physical Nano-SIM'),
        ('SINGLE_SIM', 'Single Nano-SIM'),
        ('ESIM_DUAL', '1 Nano-SIM + 1 eSIM'),
        ('ESIM_ONLY', 'Dual eSIM Only'),
    ]

    TAX_PRICING_TYPE_CHOICES = [
        ('INCLUSIVE', 'Tax-Inclusive (Price includes applicable Tax / कर सहित)'),
        ('EXCLUSIVE', 'Tax-Exclusive (Tax added on top / कर बाहेक)'),
        ('EXEMPT', 'Tax-Exempt / Non-Tax (0% Tax / गैर-कर)'),
    ]

    MDMS_STATUS_CHOICES = [
        ('REGISTERED_OFFICIAL', 'NTA MDMS Registered (Official Nepal Distributor)'),
        ('GRAY_UNREGISTERED', 'Unregistered / Gray Channel (Subject to NTA Notice)'),
        ('INDIVIDUAL_CUSTOMS_PAID', 'Individual Passenger Import (Customs Duty Paid)'),
        ('EXEMPT', 'Exempt / Non-Cellular Device'),
    ]

    # 1. Core Identification
    name = models.CharField(max_length=255, db_index=True, verbose_name=_("Product Name"))
    sku = models.CharField(max_length=50, unique=True, db_index=True, verbose_name=_("SKU / Product Code"))
    barcode = models.CharField(
        max_length=100,
        unique=True,
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Barcode Number (EAN/UPC)"),
        help_text=_("Scan physical box barcode or leave completely blank if not needed.")
    )
    category = models.ForeignKey(ProductCategory, on_delete=models.PROTECT, related_name='products')
    subcategory = models.ForeignKey(ProductSubCategory, on_delete=models.SET_NULL, null=True, blank=True, related_name='products')
    brand = models.ForeignKey(Brand, on_delete=models.SET_NULL, null=True, blank=True, related_name='products')
    model_name = models.CharField(max_length=150, blank=True, null=True, db_index=True, verbose_name=_("Model Name"))
    model_number = models.CharField(max_length=100, blank=True, null=True, db_index=True, verbose_name=_("Model Number"))
    is_spare_part = models.BooleanField(default=False, db_index=True, verbose_name=_("Is Repair Spare Part"))

    # 2. Mobile Variant Information
    variant_name = models.CharField(max_length=100, blank=True, null=True, verbose_name=_("Variant Tag"))
    ram = models.CharField(max_length=30, blank=True, null=True, verbose_name=_("RAM"))
    internal_storage = models.CharField(max_length=30, blank=True, null=True, verbose_name=_("Storage"))
    color_variant = models.CharField(max_length=50, blank=True, null=True, verbose_name=_("Color"))
    network_type = models.CharField(max_length=20, choices=NETWORK_TYPE_CHOICES, default='5G', blank=True, null=True)
    sim_configuration = models.CharField(max_length=25, choices=SIM_CONFIG_CHOICES, default='DUAL_SIM', blank=True, null=True)
    region_variant = models.CharField(max_length=60, blank=True, null=True, default="Global / Nepal Official")

    # 3. Hardware Specifications
    operating_system = models.CharField(max_length=80, blank=True, null=True)
    processor_chipset = models.CharField(max_length=120, blank=True, null=True)
    display_size = models.CharField(max_length=50, blank=True, null=True)
    display_type = models.CharField(max_length=80, blank=True, null=True)
    display_resolution = models.CharField(max_length=50, blank=True, null=True)
    refresh_rate = models.CharField(max_length=30, blank=True, null=True, default="90Hz")
    rear_camera = models.CharField(max_length=150, blank=True, null=True)
    front_camera = models.CharField(max_length=80, blank=True, null=True)
    battery_capacity = models.CharField(max_length=50, blank=True, null=True)
    fast_charging = models.CharField(max_length=60, blank=True, null=True)
    fingerprint_sensor = models.CharField(max_length=60, blank=True, null=True, default="Side-Mounted")
    face_unlock = models.BooleanField(default=True)
    expandable_storage = models.BooleanField(default=True)
    max_memory_card = models.CharField(max_length=30, blank=True, null=True)
    usb_port_type = models.CharField(max_length=40, blank=True, null=True, default="USB Type-C 2.0 (OTG)")
    headphone_jack_35mm = models.BooleanField(default=True)
    wifi_spec = models.CharField(max_length=60, blank=True, null=True, default="Wi-Fi 5 (802.11ac)")
    bluetooth_version = models.CharField(max_length=30, blank=True, null=True, default="Bluetooth 5.0")
    nfc_available = models.BooleanField(default=False)
    gps_capabilities = models.CharField(max_length=100, blank=True, null=True, default="GPS, GLONASS, GALILEO, BDS")

    # 4. Units
    size_dimension = models.CharField(max_length=60, blank=True, null=True)
    base_unit = models.ForeignKey(UnitOfMeasurement, on_delete=models.PROTECT, related_name='base_products')

    # 5. Pricing & Dynamic Taxes
    purchase_price = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Cost Price (NPR)"))
    selling_price = models.DecimalField(max_digits=12, decimal_places=2, verbose_name=_("Selling Price / MRP (NPR)"))
    wholesale_price = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)
    max_discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('10.00'))
    is_discountable = models.BooleanField(
        default=True,
        verbose_name=_("Discount Allowed"),
        help_text=_("If unchecked, no item-level or bill-level discount may be applied to this product.")
    )
    
    tax_pricing_type = models.CharField(
        max_length=20,
        choices=TAX_PRICING_TYPE_CHOICES,
        default='EXEMPT',
        db_index=True,
        verbose_name=_("Tax Pricing Mode"),
        help_text=_("Specifies whether the rate includes tax, adds tax on top, or is non-tax exempt.")
    )
    is_vat_applicable = models.BooleanField(default=False, verbose_name=_("Is Tax Applicable"))
    vat_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Tax Rate (%)")
    )

    # 6. Inventory & Warehouse Shelf
    inventory_tracking_type = models.CharField(max_length=20, choices=TRACKING_TYPE_CHOICES, default='STANDARD')
    requires_imei_tracking = models.BooleanField(default=False)
    requires_serial_tracking = models.BooleanField(default=False)
    reorder_level = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('5.00'))
    rack_number = models.CharField(max_length=50, blank=True, null=True, verbose_name=_("Rack Number"))
    shelf_identifier = models.CharField(max_length=50, blank=True, null=True, verbose_name=_("Shelf ID"))
    bin_location = models.CharField(max_length=50, blank=True, null=True, verbose_name=_("Bin Location"))

    # 7. NTA MDMS Default Classification
    default_mdms_status = models.CharField(
        max_length=30,
        choices=MDMS_STATUS_CHOICES,
        default='REGISTERED_OFFICIAL',
        verbose_name=_("Default NTA MDMS Status"),
        help_text=_("Default status assigned to handsets received under this product model.")
    )

    # 8. Warranty Defaults & Media
    warranty_months = models.PositiveIntegerField(default=12, verbose_name=_("Warranty (Months)"))
    warranty_provider = models.CharField(max_length=120, blank=True, null=True, default="Authorized Distributor")
    image = models.ImageField(upload_to='products/%Y/%m/', blank=True, null=True)
    description = models.TextField(blank=True, null=True)

    class Meta:
        db_table = 'inv_products'
        ordering = ['name']
        verbose_name = _('Product')
        verbose_name_plural = _('Products')
        indexes = [
            models.Index(fields=['category', 'brand'], name='idx_prod_cat_brand'),
            models.Index(fields=['name', 'selling_price'], name='idx_prod_name_price'),
            models.Index(fields=['rack_number', 'shelf_identifier'], name='idx_prod_rack_shelf'),
            models.Index(fields=['model_name'], name='idx_prod_model_name'),
            models.Index(fields=['model_number'], name='idx_prod_model_num'),
            models.Index(fields=['brand', 'model_name'], name='idx_prod_brand_model'),
            models.Index(fields=['name'], name='idx_prod_name'),
        ]

    def __str__(self):
        variant_tag = f" ({self.variant_name})" if self.variant_name else ""
        return f"{self.name}{variant_tag} [{self.sku}]"

    def clean(self):
        super().clean()
        if self.barcode is not None:
            self.barcode = self.barcode.strip()
            if self.barcode == '':
                self.barcode = None

    def save(self, *args, **kwargs):
        if self.barcode is not None:
            self.barcode = str(self.barcode).strip()
            if self.barcode == '':
                self.barcode = None
        super().save(*args, **kwargs)

class ProductComponentWarrantyRule(TimeStampedModel):
    """Component-level warranty rules per product model (e.g. Device 12M, Battery 6M, Screen 6M)."""
    COMPONENT_TYPES = [
        ('DEVICE', 'Main Handset / Motherboard (ह्यान्डसेट / मदरबोर्ड)'),
        ('SCREEN', 'Screen / Display Panel (स्क्रिन / डिस्प्ले)'),
        ('BATTERY', 'Internal Battery (ब्याट्री)'),
        ('CHARGER', 'Charging Adapter & Cable (चार्जर र केबल)'),
        ('CAMERA', 'Camera Module (क्यामेरा मोड्युल)'),
        ('SPEAKER', 'Speaker / Receiver (स्पीकर)'),
        ('ACCESSORY', 'In-Box Accessory / Earphones'),
        ('OTHER', 'Other Specific Component'),
    ]

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='component_warranty_rules')
    component_type = models.CharField(max_length=30, choices=COMPONENT_TYPES, default='DEVICE', db_index=True)
    component_name = models.CharField(max_length=120, verbose_name=_("Component Description"))
    warranty_months = models.PositiveIntegerField(default=6, verbose_name=_("Warranty Months"))
    coverage_conditions = models.CharField(
        max_length=255, blank=True, null=True,
        default="Covers manufacturing defects only. Void if liquid or physical damage found."
    )

    class Meta:
        db_table = 'inv_product_component_warranty_rules'
        unique_together = ('product', 'component_type', 'component_name')
        ordering = ['product', 'component_type']
        verbose_name = _('Product Component Warranty Rule')
        verbose_name_plural = _('Product Component Warranty Rules')

    def __str__(self):
        return f"{self.product.name} -> {self.get_component_type_display()}: {self.warranty_months} Months"

class UnitConversion(TimeStampedModel):
    """Packaging unit conversion: e.g. 1 Box of Tempered Glass = 50 Pieces."""
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='unit_conversions')
    unit_name = models.CharField(max_length=50, verbose_name=_("Packaging Unit Name"))
    conversion_factor = models.DecimalField(max_digits=10, decimal_places=3)
    selling_price_per_unit = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)
    barcode = models.CharField(max_length=100, blank=True, null=True, unique=True)

    class Meta:
        db_table = 'inv_unit_conversions'
        unique_together = ('product', 'unit_name')
        verbose_name = _('Unit Packaging Conversion')
        verbose_name_plural = _('Unit Packaging Conversions')

    def __str__(self):
        return f"1 {self.unit_name} = {self.conversion_factor} {self.product.base_unit.code} ({self.product.name})"

class BranchStock(TimeStampedModel):
    """Real-time sellable, reserved, and quarantined defective stock levels per branch."""
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name='branch_stocks')
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='branch_stocks')
    quantity = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal('0.000'), db_index=True, verbose_name=_("Sellable Stock"))
    reserved_quantity = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal('0.000'), verbose_name=_("Reserved Stock"))
    quarantined_defective_quantity = models.DecimalField(
        max_digits=12, decimal_places=3, default=Decimal('0.000'), db_index=True,
        verbose_name=_("Defective Quarantine Stock (Waiting RMA)")
    )
    low_stock_threshold = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('5.00'))

    class Meta:
        db_table = 'inv_branch_stocks'
        unique_together = ('branch', 'product')
        verbose_name = _('Branch Stock Level')
        verbose_name_plural = _('Branch Stock Levels')
        indexes = [
            models.Index(fields=['branch', 'quantity'], name='idx_bstock_branch_qty'),
            models.Index(fields=['product', 'branch'], name='idx_bstock_prod_branch'),
            models.Index(fields=['branch', 'quarantined_defective_quantity'], name='idx_bstock_branch_quar'),
        ]

    def __str__(self):
        return f"{self.product.name} @ {self.branch.name}: {self.quantity}"

    @property
    def available_quantity(self) -> Decimal:
        return max(Decimal('0.000'), self.quantity - self.reserved_quantity)

    @property
    def is_low_stock(self) -> bool:
        return self.quantity <= self.low_stock_threshold

class ProductBatch(TimeStampedModel):
    """Tracks non-serialized multi-date inventory batches for accessories and spare parts."""
    batch_number = models.CharField(max_length=60, db_index=True, verbose_name=_("Batch Number"))
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='batches')
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name='product_batches')
    purchase_date = models.DateField(db_index=True)
    cost_price = models.DecimalField(max_digits=12, decimal_places=2)
    selling_price = models.DecimalField(max_digits=12, decimal_places=2)
    quantity_received = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal('0.000'))
    quantity_remaining = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal('0.000'), db_index=True)
    is_depleted = models.BooleanField(default=False, db_index=True)
    supplier_name = models.CharField(max_length=150, blank=True, null=True)
    grn_reference = models.CharField(max_length=60, blank=True, null=True)

    class Meta:
        db_table = 'inv_product_batches'
        ordering = ['purchase_date', 'created_at']
        verbose_name = _('Product Batch (FIFO)')
        verbose_name_plural = _('Product Batches (FIFO)')
        indexes = [
            models.Index(fields=['product', 'branch', 'is_depleted'], name='idx_batch_prod_br_depleted'),
            models.Index(fields=['purchase_date'], name='idx_batch_purch_date'),
        ]

    def save(self, *args, **kwargs):
        self.is_depleted = self.quantity_remaining <= Decimal('0.000')
        super().save(*args, **kwargs)

class ItemInstance(TimeStampedModel):
    """
    Physical smartphone/device unit tracked by unique IMEI 1, IMEI 2, Serial Number,
    physical condition grade, NTA MDMS registration status, and source origin (GRN vs Trade-In).
    """
    STATUS_CHOICES = [
        ('IN_STOCK', 'In Stock (उपलब्ध)'),
        ('RESERVED', 'Reserved in Cart / Booking'),
        ('SOLD', 'Sold (बिक्री भएको)'),
        ('UNDER_SERVICE', 'Under Service / Repair (मर्मतमा रहेको)'),
        ('RETURNED_DEFECTIVE', 'Returned Defective (खराब फिर्ता)'),
        ('TRANSFERRED', 'In Transit Transfer'),
        ('ARCHIVED', 'Archived / Re-traded (अभिलेख गरिएको)'),
    ]

    CONDITION_CHOICES = [
        ('BRAND_NEW', 'Brand New / Sealed Box'),
        ('OPEN_BOX', 'Open Box / Like New'),
        ('REFURBISHED', 'Official Refurbished (Certified)'),
        ('USED_GRADE_A', 'Pre-Owned Grade A (Flawless / Trade-In)'),
        ('USED_GRADE_B', 'Pre-Owned Grade B (Minor Scratches / Trade-In)'),
        ('USED_GRADE_C', 'Pre-Owned Grade C (Heavy Wear / Trade-In)'),
    ]

    ACTIVATION_STATUS_CHOICES = [
        ('SEALED_INACTIVE', 'Sealed / Not Activated'),
        ('ACTIVATED', 'Carrier / Manufacturer Activated'),
        ('DEMO_UNIT', 'In-Store Live Demo Device'),
    ]

    SOURCE_TYPE_CHOICES = [
        ('NEW_PURCHASE_GRN', 'Brand New Inward (GRN Purchase)'),
        ('CUSTOMER_EXCHANGE_TRADE_IN', 'Customer Exchange / Buy-Back (Trade-In)'),
        ('REFURBISHED_RETURN', 'Refurbished / Workshop Intake'),
    ]

    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='tracked_instances')
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='tracked_instances')

    device_uid = models.CharField(max_length=60, unique=True, null=True, blank=True, db_index=True)
    imei_1 = models.CharField(max_length=35, blank=True, null=True, db_index=True, verbose_name=_("IMEI 1"))
    imei_2 = models.CharField(max_length=35, blank=True, null=True, db_index=True, verbose_name=_("IMEI 2"))
    
    imei_2_pending_scan = models.BooleanField(
        default=False,
        db_index=True,
        verbose_name=_("IMEI 2 Pending Scan"),
        help_text=_("True if device is Dual-SIM and only IMEI 1 was captured during GRN. Instructs POS to capture IMEI 2 on sale.")
    )

    serial_number = models.CharField(max_length=60, blank=True, null=True, db_index=True)
    device_barcode = models.CharField(max_length=100, blank=True, null=True, db_index=True)

    status = models.CharField(max_length=25, choices=STATUS_CHOICES, default='IN_STOCK', db_index=True)
    condition = models.CharField(max_length=25, choices=CONDITION_CHOICES, default='BRAND_NEW', db_index=True)
    activation_status = models.CharField(max_length=25, choices=ACTIVATION_STATUS_CHOICES, default='SEALED_INACTIVE')

    # Source & Trade-In Linkage
    source_type = models.CharField(
        max_length=30, choices=SOURCE_TYPE_CHOICES, default='NEW_PURCHASE_GRN', db_index=True,
        verbose_name=_("Device Acquisition Source")
    )
    trade_in_voucher_reference = models.CharField(
        max_length=60, blank=True, null=True, db_index=True,
        verbose_name=_("Trade-In Voucher Reference"),
        help_text=_("Links to the customer buy-back undertaking voucher if acquired via exchange.")
    )

    # NTA MDMS Status & Verification
    mdms_status = models.CharField(
        max_length=30,
        choices=Product.MDMS_STATUS_CHOICES,
        default='REGISTERED_OFFICIAL',
        db_index=True,
        verbose_name=_("NTA MDMS Status"),
        help_text=_("Compliance indicator for Nepal Telecommunications Authority MDMS system.")
    )
    mdms_verification_date = models.DateField(blank=True, null=True, verbose_name=_("MDMS Checked Date"))
    mdms_remarks = models.CharField(max_length=255, blank=True, null=True, verbose_name=_("MDMS Remarks / Verification Ref"))

    # Inward Purchase Tracking
    purchase_reference = models.CharField(max_length=60, blank=True, null=True)
    batch_reference = models.CharField(max_length=60, blank=True, null=True)
    supplier_name = models.CharField(max_length=150, blank=True, null=True)
    landed_cost = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    purchase_date = models.DateField(blank=True, null=True, db_index=True)

    # Outward Sales Tracking
    sold_invoice_reference = models.CharField(max_length=60, blank=True, null=True, db_index=True)
    customer_name = models.CharField(max_length=150, blank=True, null=True)
    customer_phone = models.CharField(max_length=30, blank=True, null=True, db_index=True)
    sale_date = models.DateField(blank=True, null=True, db_index=True)
    sold_price = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)
    warranty_start_date = models.DateField(blank=True, null=True)
    warranty_end_date = models.DateField(blank=True, null=True)
    warranty_remarks = models.CharField(max_length=255, blank=True, null=True)

    class Meta:
        db_table = 'inv_item_instances'
        ordering = ['-created_at']
        verbose_name = _('IMEI / Serial Device Instance')
        verbose_name_plural = _('IMEI / Serial Device Instances')
        constraints = [
            models.UniqueConstraint(
                fields=['imei_1'],
                condition=models.Q(status='IN_STOCK') & models.Q(imei_1__isnull=False) & ~models.Q(imei_1=''),
                name='unique_active_in_stock_imei_1'
            ),
            models.UniqueConstraint(
                fields=['imei_2'],
                condition=models.Q(status='IN_STOCK') & models.Q(imei_2__isnull=False) & ~models.Q(imei_2=''),
                name='unique_active_in_stock_imei_2'
            ),
            models.UniqueConstraint(
                fields=['serial_number'],
                condition=models.Q(status='IN_STOCK') & models.Q(serial_number__isnull=False) & ~models.Q(serial_number=''),
                name='unique_active_in_stock_serial_number'
            ),
        ]
        indexes = [
            models.Index(fields=['product', 'branch', 'status'], name='idx_instance_prod_br_status'),
            models.Index(fields=['imei_1', 'status'], name='idx_instance_imei1_status'),
            models.Index(fields=['imei_2', 'status'], name='idx_instance_imei2_status'),
            models.Index(fields=['serial_number', 'status'], name='idx_inst_serial_status'),
            models.Index(fields=['device_barcode', 'status'], name='idx_inst_barcode_status'),
            models.Index(fields=['sold_invoice_reference'], name='idx_inst_sold_invoice'),
            models.Index(fields=['customer_phone'], name='idx_inst_cust_phone'),
            models.Index(fields=['status', 'created_at'], name='idx_inst_status_created'),
            models.Index(fields=['branch', 'status'], name='idx_inst_branch_status'),
        ]

    def clean(self):
        super().clean()
        if self.imei_1:
            self.imei_1 = self.imei_1.strip()
            if self.imei_1 == '':
                self.imei_1 = None

        if self.imei_2:
            self.imei_2 = self.imei_2.strip()
            if self.imei_2 == '':
                self.imei_2 = None

        if self.serial_number:
            self.serial_number = self.serial_number.strip()
            if self.serial_number == '':
                self.serial_number = None

    def save(self, *args, **kwargs):
        if not self.device_uid:
            self.device_uid = f"DEV-{uuid.uuid4().hex[:12].upper()}"

        if self.imei_1:
            self.imei_1 = self.imei_1.strip()
            if self.imei_1 == '':
                self.imei_1 = None
        else:
            self.imei_1 = None

        if self.imei_2:
            self.imei_2 = self.imei_2.strip()
            if self.imei_2 == '':
                self.imei_2 = None
        else:
            self.imei_2 = None

        if self.serial_number:
            self.serial_number = self.serial_number.strip()
            if self.serial_number == '':
                self.serial_number = None
        else:
            self.serial_number = None

        if self.product and self.product.requires_imei_tracking:
            is_dual_sim = getattr(self.product, 'sim_configuration', 'DUAL_SIM') in ['DUAL_SIM', 'ESIM_DUAL']
            if is_dual_sim and not self.imei_2 and self.status == 'IN_STOCK':
                self.imei_2_pending_scan = True
            elif self.imei_2 or self.status != 'IN_STOCK':
                self.imei_2_pending_scan = False

        super().save(*args, **kwargs)

    @property
    def is_mdms_compliant(self) -> bool:
        return self.mdms_status in ['REGISTERED_OFFICIAL', 'INDIVIDUAL_CUSTOMS_PAID', 'EXEMPT']

class DeviceComponentWarranty(TimeStampedModel):
    """Active customer component warranty ledger for a specific sold IMEI (e.g. Battery 6M, Screen 6M)."""
    STATUS_CHOICES = [
        ('ACTIVE', 'Active / In-Warranty (सक्रिय वारेन्टी)'),
        ('CLAIMED', 'Warranty Claimed & Replaced (दावी गरिएको)'),
        ('EXPIRED', 'Warranty Expired (म्याद सकिएको)'),
        ('VOID', 'Void / Ineligible (भौतिक/पानी क्षति)'),
    ]

    item_instance = models.ForeignKey(ItemInstance, on_delete=models.CASCADE, related_name='component_warranties')
    component_type = models.CharField(
        max_length=30, choices=ProductComponentWarrantyRule.COMPONENT_TYPES, default='DEVICE', db_index=True
    )
    component_name = models.CharField(max_length=120)
    warranty_months = models.PositiveIntegerField(default=6)
    warranty_start_date = models.DateField(db_index=True)
    warranty_expiry_date = models.DateField(db_index=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ACTIVE', db_index=True)
    claim_count = models.PositiveIntegerField(default=0)
    void_reason = models.CharField(max_length=255, blank=True, null=True)
    remarks = models.TextField(blank=True, null=True)

    class Meta:
        db_table = 'inv_device_component_warranties'
        ordering = ['item_instance', 'warranty_expiry_date']
        verbose_name = _('Active Device Component Warranty')
        verbose_name_plural = _('Active Device Component Warranties')
        indexes = [
            models.Index(fields=['item_instance', 'status'], name='idx_devwarr_inst_status'),
            models.Index(fields=['warranty_expiry_date', 'status'], name='idx_devwarr_exp_status'),
        ]

    @property
    def is_currently_valid(self) -> bool:
        today = date.today()
        return self.status == 'ACTIVE' and (self.warranty_expiry_date >= today)

class VendorRMAClaim(TimeStampedModel):
    """Return to Vendor (RMA) tracking voucher for claiming defective components from distributors."""
    STATUS_CHOICES = [
        ('DRAFT', 'Draft Challan / Gathering Defective Parts'),
        ('DISPATCHED_TO_VENDOR', 'Dispatched to Distributor Lab (पठाइएको)'),
        ('PARTIALLY_SETTLED', 'Partially Replaced / Credited'),
        ('COMPLETED', 'Fully Resolved & Closed (सम्पन्न)'),
        ('REJECTED', 'Rejected by Distributor (अस्वीकृत)'),
    ]

    rma_number = models.CharField(max_length=50, unique=True, db_index=True, verbose_name=_("RMA Claim No."))
    supplier = models.ForeignKey('purchases.Supplier', on_delete=models.PROTECT, related_name='vendor_rma_claims')
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='vendor_rma_claims')

    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='DRAFT', db_index=True)
    dispatch_date = models.DateField(blank=True, null=True)
    distributor_tracking_ref = models.CharField(max_length=100, blank=True, null=True)
    distributor_service_center = models.CharField(max_length=150, blank=True, null=True, default="Authorized National Service Center")

    resolution_date = models.DateField(blank=True, null=True)
    total_claimed_parts_count = models.PositiveIntegerField(default=0)
    total_credit_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))

    dispatched_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='dispatched_rmas'
    )
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='resolved_rmas'
    )
    resolution_notes = models.TextField(blank=True, null=True)

    class Meta:
        db_table = 'inv_vendor_rma_claims'
        ordering = ['-created_at']
        verbose_name = _('Vendor RMA & Warranty Claim')
        verbose_name_plural = _('Vendor RMA & Warranty Claims')
        indexes = [
            models.Index(fields=['branch', 'status'], name='idx_rma_branch_status'),
            models.Index(fields=['supplier', 'status'], name='idx_rma_supplier_status'),
        ]

class VendorRMAClaimItem(TimeStampedModel):
    """Line item in a Vendor RMA Claim linked to customer repair replacements."""
    RESOLUTION_CHOICES = [
        ('PENDING', 'Pending Distributor Inspection'),
        ('REPLACED_WITH_NEW_PART', 'Replaced with New Factory Spare Part'),
        ('CREDIT_NOTE_ISSUED', 'Credit Note / Purchase Price Reimbursed'),
        ('RETURNED_UNREPAIRED', 'Rejected / Returned Unrepaired (Warranty Void)'),
    ]

    rma_claim = models.ForeignKey(VendorRMAClaim, on_delete=models.CASCADE, related_name='claimed_items')
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='vendor_rma_items')
    defective_serial_or_imei = models.CharField(max_length=80)
    defect_description = models.TextField()

    resolution = models.CharField(max_length=30, choices=RESOLUTION_CHOICES, default='PENDING', db_index=True)
    replacement_batch_or_serial = models.CharField(max_length=80, blank=True, null=True)
    credit_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    vendor_notes = models.CharField(max_length=255, blank=True, null=True)

    class Meta:
        db_table = 'inv_vendor_rma_claim_items'
        verbose_name = _('Vendor RMA Claim Item')
        verbose_name_plural = _('Vendor RMA Claim Items')

class StockMovementLog(TimeStampedModel):
    """Audit ledger tracking inventory movements, IMEI instances, trade-in buybacks, and branch transfers."""
    MOVEMENT_TYPES = [
        ('PURCHASE', 'Purchase Received (GRN Inward)'),
        ('SALE', 'POS Billing Sale (बिक्री)'),
        ('SALE_RETURN', 'Customer Return (फिर्ता)'),
        ('TRADE_IN_ACQUISITION', 'Trade-In / Phone Buy-Back Acquisition (+)'),
        ('TRADE_IN_SALE', 'Pre-Owned / Traded-in Phone Sale (-)'),
        ('TRANSFER_OUT', 'Branch Transfer Out'),
        ('TRANSFER_IN', 'Branch Transfer In'),
        ('ADJUSTMENT_ADD', 'Stock Count Correction (+)'),
        ('ADJUSTMENT_SUB', 'Damage / Lost / Expired (-)'),
        ('SERVICE_INTAKE', 'Repair Service Intake'),
        ('SERVICE_REPLACED_PART_DEDUCT', 'Service Spare Part Installed (-)'),
        ('SERVICE_DEFECTIVE_QUARANTINE', 'Defective Part Moved to Quarantine (+)'),
        ('RMA_VENDOR_DISPATCH', 'Defective Part Dispatched to Vendor (-)'),
        ('RMA_VENDOR_REPLACEMENT_IN', 'Replacement Received from Vendor (+)'),
    ]

    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='movement_logs')
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='movement_logs')
    movement_type = models.CharField(max_length=35, choices=MOVEMENT_TYPES, db_index=True)
    quantity_delta = models.DecimalField(max_digits=12, decimal_places=3)
    previous_quantity = models.DecimalField(max_digits=12, decimal_places=3)
    new_quantity = models.DecimalField(max_digits=12, decimal_places=3)
    reference_document = models.CharField(max_length=100, blank=True, null=True)
    imei_or_serial_number = models.CharField(max_length=100, blank=True, null=True, db_index=True)
    remarks = models.TextField(blank=True, null=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        db_table = 'inv_stock_movement_logs'
        ordering = ['-created_at']
        verbose_name = _('Stock Movement Log')
        verbose_name_plural = _('Stock Movement Logs')
        indexes = [
            models.Index(fields=['product', 'branch', 'created_at'], name='idx_movelog_prod_br_date'),
            models.Index(fields=['movement_type', 'created_at'], name='idx_movelog_type_date'),
            models.Index(fields=['reference_document'], name='idx_movelog_ref_doc'),
            models.Index(fields=['imei_or_serial_number'], name='idx_movelog_imei_serial'),
        ]

# Backward compatibility alias
IMEIEntry = ItemInstance