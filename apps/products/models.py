from decimal import Decimal
from django.db import models
from django.utils.translation import gettext_lazy as _
from apps.core.models import TimeStampedModel
from apps.inventory.models import Product

class ProductPriceTier(TimeStampedModel):
    """
    Tiered pricing configuration for Standard Retail, Wholesale, Dealer, and VIP Customers.
    """
    TIER_CHOICES = [
        ('RETAIL', 'Standard Retail (खुद्रा मूल्य)'),
        ('WHOLESALE', 'Wholesale Rate (थोक मूल्य)'),
        ('DEALER', 'Dealer / Bulk Rate'),
        ('SPECIAL', 'Special VIP Rate'),
    ]

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='price_tiers')
    tier_type = models.CharField(max_length=20, choices=TIER_CHOICES, db_index=True)
    min_quantity = models.DecimalField(
        max_digits=10, decimal_places=3, default=Decimal('1.000'),
        help_text=_("Minimum base units required to qualify for this rate")
    )
    price_per_unit = models.DecimalField(max_digits=12, decimal_places=2, verbose_name=_("Price (NPR)"))

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
    
    show_shop_name = models.BooleanField(default=True)
    show_mrp = models.BooleanField(default=True)
    show_product_code = models.BooleanField(default=True)
    show_variant_info = models.BooleanField(default=True, help_text=_("Print RAM/Storage & Color variant on sticker"))
    show_model_number = models.BooleanField(default=True)
    show_warranty_badge = models.BooleanField(default=True, help_text=_("Print e.g. 1 Year Warranty on label"))
    show_estimate_tag = models.BooleanField(
        default=True,
        help_text=_("Prints 'EST' or 'Proforma' badge on barcode sticker")
    )
    custom_note = models.CharField(max_length=50, blank=True, null=True, default="Incl. All Taxes")

    class Meta:
        db_table = 'prod_barcode_templates'
        verbose_name = _('Barcode Label Template')
        verbose_name_plural = _('Barcode Label Templates')

    def __str__(self):
        return f"{self.name} ({self.width_mm}x{self.height_mm}mm)"