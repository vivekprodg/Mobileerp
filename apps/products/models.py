"""
Products Module Models: Pricing Tiers, Barcode Sticker Templates & Historical Migration Placeholders.
"""

from decimal import Decimal
from django.db import models
from django.utils.translation import gettext_lazy as _
from apps.core.models import TimeStampedModel
from apps.inventory.models import Product, ProductCategory, Brand, UnitOfMeasurement


class ProductPriceTier(TimeStampedModel):
    """
    Tiered pricing configuration for Standard Retail, Wholesale, Dealer, and VIP Customers.
    Enforces minimum quantity threshold before discounted bulk rate applies.
    """
    TIER_CHOICES = [
        ('RETAIL', _('Standard Retail (खुद्रा मूल्य)')),
        ('WHOLESALE', _('Wholesale Rate (थोक मूल्य)')),
        ('DEALER', _('Dealer / Bulk Rate')),
        ('SPECIAL', _('Special VIP Rate')),
    ]

    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name='price_tiers',
        verbose_name=_("Catalog Product")
    )
    tier_type = models.CharField(
        max_length=20, choices=TIER_CHOICES, db_index=True,
        verbose_name=_("Customer Tier Type")
    )
    min_quantity = models.DecimalField(
        max_digits=10, decimal_places=3, default=Decimal('1.000'),
        verbose_name=_("Minimum Quantity Qualifying Threshold"),
        help_text=_("Minimum base units required to qualify for this rate")
    )
    price_per_unit = models.DecimalField(
        max_digits=12, decimal_places=2,
        verbose_name=_("Price per Base Unit (NPR)")
    )

    class Meta:
        db_table = 'prod_price_tiers'
        unique_together = ('product', 'tier_type', 'min_quantity')
        ordering = ['product', 'min_quantity']
        verbose_name = _('Product Price Tier')
        verbose_name_plural = _('Product Price Tiers')

    def __str__(self):
        return f"{self.product.name} - {self.get_tier_type_display()}: Rs. {self.price_per_unit} (>= {self.min_quantity})"


class BarcodeLabelTemplate(TimeStampedModel):
    """
    Thermal barcode sticker template configurations (e.g. 50x25mm single, 38x25mm double column, 80mm POS).
    Supports printing phone variant tags (RAM/ROM), model numbers, MRP, and non-IRD disclaimer badges.
    """
    name = models.CharField(max_length=100, unique=True, verbose_name=_("Template Name"))
    width_mm = models.PositiveIntegerField(default=50, help_text=_("Width in millimeters"))
    height_mm = models.PositiveIntegerField(default=25, help_text=_("Height in millimeters"))
    
    show_shop_name = models.BooleanField(default=True, verbose_name=_("Print Shop Business Name"))
    show_mrp = models.BooleanField(default=True, verbose_name=_("Print Selling Price / MRP"))
    show_product_code = models.BooleanField(default=True, verbose_name=_("Print Product SKU Code"))
    show_variant_info = models.BooleanField(
        default=True, verbose_name=_("Print Variant Info"),
        help_text=_("Print RAM/Storage & Color variant on sticker")
    )
    show_model_number = models.BooleanField(default=True, verbose_name=_("Print Model Number"))
    show_warranty_badge = models.BooleanField(
        default=True, verbose_name=_("Print Warranty Badge"),
        help_text=_("Print e.g. 1 Year Warranty on label")
    )
    show_estimate_tag = models.BooleanField(
        default=True,
        verbose_name=_("Print Estimation / Proforma Badge"),
        help_text=_("Prints 'EST' or 'Proforma' badge on barcode sticker")
    )
    custom_note = models.CharField(
        max_length=50, blank=True, null=True, default="Incl. All Taxes",
        verbose_name=_("Custom Sticker Footer Note")
    )

    class Meta:
        db_table = 'prod_barcode_templates'
        verbose_name = _('Barcode Label Template')
        verbose_name_plural = _('Barcode Label Templates')

    def __str__(self):
        return f"{self.name} ({self.width_mm}x{self.height_mm}mm)"


# =============================================================================
# HISTORICAL MIGRATION PLACEHOLDER PROXY (ZERO-MIGRATION DB EXTENSION)
# =============================================================================

class HistoricalProductManager(models.Manager):
    """
    Filters catalog to exclusively show generic historical migration placeholder items.
    """
    def get_queryset(self):
        return super().get_queryset().filter(
            models.Q(sku__startswith='HIST-') |
            models.Q(name__icontains='(Historical)') |
            models.Q(name__in=['Mobile', 'Various Items'])
        )


class HistoricalProduct(Product):
    """
    Proxy Model representing generic historical placeholder products:
    - 'Mobile (Historical)'
    - 'Various Items (Historical)'

    Characteristics:
    - Non-serialized (requires_imei_tracking = False, requires_serial_tracking = False).
    - General service / summary items (never prompts cashier for IMEIs).
    - Inventory stock tracking disabled (no physical shelf stock deduction).
    - Proxy model: Uses existing 'inv_products' table with ZERO database schema migrations needed.
    """
    objects = HistoricalProductManager()
    all_objects = models.Manager()

    class Meta:
        proxy = True
        verbose_name = _('Historical Migration Placeholder Product')
        verbose_name_plural = _('Historical Migration Placeholder Products')

    @classmethod
    def get_or_create_placeholders(cls) -> tuple:
        """
        Idempotently initializes the two required Mobilesoft historical placeholders:
        1. 'Mobile (Historical)'
        2. 'Various Items (Historical)'
        Returns tuple of (mobile_product, various_items_product).
        """
        # 1. Ensure Historical Category Exists
        cat_obj, _ = ProductCategory.objects.get_or_create(
            name__iexact="Historical Tax Sales",
            defaults={
                'name': "Historical Tax Sales",
                'name_np': "ऐतिहासिक कर बिक्री",
                'code': "HIST",
                'description': "Non-serialized summary category for Mobilesoft 2080 B.S. tax register imports",
                'is_active': True
            }
        )

        # 2. Ensure Standard Piece Base Unit Exists
        base_unit, _ = UnitOfMeasurement.objects.get_or_create(
            code='PCS',
            defaults={
                'name': 'Piece',
                'name_np': 'पिस',
                'allow_decimal': False
            }
        )

        # 3. Ensure Generic Brand Exists
        brand_obj, _ = Brand.objects.get_or_create(
            name__iexact="Historical Generic",
            defaults={
                'name': "Historical Generic",
                'origin_country': "Nepal"
            }
        )

        # 4. Initialize Placeholder Product 1: "Mobile (Historical)"
        p_mobile = Product.objects.filter(
            models.Q(name__iexact="Mobile (Historical)") |
            models.Q(name__iexact="Mobile") |
            models.Q(sku="HIST-MOBILE-001")
        ).first()

        if not p_mobile:
            p_mobile = Product.objects.create(
                name="Mobile (Historical)",
                model_name="Mobile Summary Item",
                sku="HIST-MOBILE-001",
                category=cat_obj,
                brand=brand_obj,
                base_unit=base_unit,
                purchase_price=Decimal('0.00'),
                selling_price=Decimal('0.00'),
                wholesale_price=Decimal('0.00'),
                requires_imei_tracking=False,
                requires_serial_tracking=False,
                inventory_tracking_type='STANDARD',
                is_discountable=False,
                tax_pricing_type='INCLUSIVE',
                is_vat_applicable=True,
                vat_rate=Decimal('13.00'),
                description="Mobilesoft FY 2080/81 IRD VAT Sales Register Summary Placeholder for Phone Sales"
            )
        else:
            # Enforce non-serialized, tax-compliant configuration
            p_mobile.requires_imei_tracking = False
            p_mobile.requires_serial_tracking = False
            p_mobile.is_discountable = False
            p_mobile.is_vat_applicable = True
            p_mobile.vat_rate = Decimal('13.00')
            p_mobile.save(update_fields=[
                'requires_imei_tracking', 'requires_serial_tracking',
                'is_discountable', 'is_vat_applicable', 'vat_rate'
            ])

        # 5. Initialize Placeholder Product 2: "Various Items (Historical)"
        p_various = Product.objects.filter(
            models.Q(name__iexact="Various Items (Historical)") |
            models.Q(name__iexact="Various Items") |
            models.Q(sku="HIST-VARIOUS-001")
        ).first()

        if not p_various:
            p_various = Product.objects.create(
                name="Various Items (Historical)",
                model_name="Various Items Summary",
                sku="HIST-VARIOUS-001",
                category=cat_obj,
                brand=brand_obj,
                base_unit=base_unit,
                purchase_price=Decimal('0.00'),
                selling_price=Decimal('0.00'),
                wholesale_price=Decimal('0.00'),
                requires_imei_tracking=False,
                requires_serial_tracking=False,
                inventory_tracking_type='STANDARD',
                is_discountable=False,
                tax_pricing_type='INCLUSIVE',
                is_vat_applicable=True,
                vat_rate=Decimal('13.00'),
                description="Mobilesoft FY 2080/81 IRD VAT Sales Register Summary Placeholder for Accessories and Miscellaneous Items"
            )
        else:
            # Enforce non-serialized, tax-compliant configuration
            p_various.requires_imei_tracking = False
            p_various.requires_serial_tracking = False
            p_various.is_discountable = False
            p_various.is_vat_applicable = True
            p_various.vat_rate = Decimal('13.00')
            p_various.save(update_fields=[
                'requires_imei_tracking', 'requires_serial_tracking',
                'is_discountable', 'is_vat_applicable', 'vat_rate'
            ])

        return p_mobile, p_various