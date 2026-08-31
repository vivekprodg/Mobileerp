import uuid
from decimal import Decimal
from django.db import models
from django.conf import settings
from django.core.cache import cache
from django.utils.translation import gettext_lazy as _


class TimeStampedModel(models.Model):
    """Abstract base model tracking creation, update, and soft state."""
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        abstract = True


class SystemConfiguration(TimeStampedModel):
    """
    Central master parameter singleton governing shop tax registration identity,
    dynamic tax rates, estimate/proforma bill headers, manager discount thresholds,
    NTA MDMS compliance policies, and second-hand phone trade-in margin buffers.
    """
    CACHE_KEY = 'system_configuration_singleton'

    TAX_SYSTEM_MODE_CHOICES = [
        ('VAT', 'VAT Registered Shop (Calculates 13% VAT on Taxable Items)'),
        ('PAN', 'PAN Only Registered Shop (Operates in 0.00% Non-VAT Mode)'),
        ('NO_TAX', 'Simple Counter / No-Tax Mode (Internal Proforma Quotations)'),
    ]

    # Shop Profile & Identity
    company_name_en = models.CharField(
        max_length=255, default="Smart Mobile & Optics Hub", verbose_name=_("Company Name (English)")
    )
    company_name_np = models.CharField(
        max_length=255, blank=True, default="स्मार्ट मोबाइल तथा अप्टिकल हब", verbose_name=_("Company Name (Nepali)")
    )
    pan_number = models.CharField(
        max_length=15, blank=True, null=True, verbose_name=_("Permanent Account Number (PAN)")
    )
    vat_number = models.CharField(
        max_length=20, blank=True, null=True, verbose_name=_("VAT Registration No.")
    )

    # Master Operating Tax Mode Switch
    tax_system_mode = models.CharField(
        max_length=20,
        choices=TAX_SYSTEM_MODE_CHOICES,
        default='PAN',
        db_index=True,
        verbose_name=_("Shop Operating Tax Mode"),
        help_text=_("Controls whether the POS calculates VAT, operates in zero-tax PAN mode, or simple estimation.")
    )
    default_vat_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name=_("Default Tax / VAT Rate (%)"),
        help_text=_("Set to 13.00% for VAT shops, or 0.00% for PAN/No-Tax stores.")
    )

    # Dynamic Estimation & Proforma Header Customization
    is_estimation_bill_only = models.BooleanField(
        default=True,
        verbose_name=_("Is Estimation Slip Only"),
        help_text=_("Indicates printed receipts are internal estimations (Non-IRD Tax Invoices).")
    )
    bill_header_title = models.CharField(
        max_length=100,
        default="SALES ESTIMATE SLIP",
        verbose_name=_("Bill Title Header"),
        help_text=_("Header text printed on slips (e.g. 'SALES ESTIMATE SLIP', 'ESTIMATE BILL', 'PROFORMA INVOICE').")
    )
    bill_estimate_disclaimer = models.TextField(
        default="NOTICE: This is an internal quotation / estimation slip only and NOT an official Tax Invoice approved by IRD Nepal.",
        verbose_name=_("Estimate Disclaimer Notice")
    )

    # Localization, Hardware & POS Controls
    currency_symbol = models.CharField(max_length=10, default="Rs.", verbose_name=_("Currency Symbol"))
    currency_code = models.CharField(max_length=5, default="NPR", verbose_name=_("Currency Code"))
    enable_nepali_calendar = models.BooleanField(default=True, verbose_name=_("Enable Bikram Sambat (BS) View"))
    default_language = models.CharField(
        max_length=5,
        choices=[('en', 'English'), ('np', 'Nepali (नेपाली)')],
        default='en',
        verbose_name=_("Default Language")
    )
    allow_negative_stock = models.BooleanField(
        default=False,
        verbose_name=_("Allow Negative Stock"),
        help_text=_("Allow checkout transactions even when calculated system inventory is zero or below.")
    )
    require_manager_approval_discount = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal('10.00'),
        verbose_name=_("Manager Override Discount Threshold (%)"),
        help_text=_("Cashier discounts exceeding this percentage require a Manager Override PIN to checkout.")
    )
    thermal_printer_paper_width = models.CharField(
        max_length=10,
        choices=[('80mm', '80mm Roll (Standard Thermal POS)'), ('58mm', '58mm Roll (Mini Thermal)'), ('a4', 'Standard A4 Sheet')],
        default='80mm',
        verbose_name=_("Thermal Printer Width")
    )

    # NTA MDMS Compliance Module
    enable_nta_mdms_tracking = models.BooleanField(
        default=True,
        verbose_name=_("Enable NTA MDMS Tracking"),
        help_text=_("Enforces MDMS registration status verification on all imported and sold handsets.")
    )
    warn_on_gray_market_sale = models.BooleanField(
        default=True,
        verbose_name=_("Warn On Unregistered / Gray Market Sale"),
        help_text=_("Displays an alert banner and customer disclaimer when selling a non-MDMS phone.")
    )

    # Trade-In & Police Legal Undertaking Config
    default_trade_in_margin_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal('15.00'),
        verbose_name=_("Default Trade-In Profit Margin (%)"),
        help_text=_("Percentage deducted from market fair value to safeguard store buy-back profitability.")
    )
    undertaking_declaration_text_np = models.TextField(
        default=(
            "म यस पसलमा मेरो स्वामित्वमा रहेको पुरानो मोबाइल फोन बिक्री/साटासाट गर्न आएको र "
            "उक्त फोन कुनै पनि चोरी, ठगी वा गैरकानुनी कार्यमा संलग्न नरहेको पूर्ण व्यहोरा सत्य हो। "
            "यदि भविष्यमा उक्त फोनका सम्बन्धमा कुनै कानुनी विवाद वा प्रहरी छानबिन भएमा म स्वयम् "
            "प्रचलित नेपाल कानुन बमोजिम पूर्ण जिम्मेवार रहनेछु भनी यो जिम्मानामा तथा मञ्जुरीनामा फारममा सहिछाप गरिदिएँ।"
        ),
        verbose_name=_("Police-Compliant Handover Undertaking Declaration (Nepali)")
    )

    class Meta:
        db_table = 'core_system_configuration'
        verbose_name = _('System Configuration')
        verbose_name_plural = _('System Configuration')

    def __str__(self):
        return f"{self.company_name_en} Configuration [{self.get_tax_system_mode_display()}]"

    @property
    def is_vat_registered(self) -> bool:
        return self.tax_system_mode == 'VAT'

    def save(self, *args, **kwargs):
        if self.tax_system_mode == 'NO_TAX':
            self.default_vat_rate = Decimal('0.00')
        elif self.tax_system_mode == 'VAT' and self.default_vat_rate == Decimal('0.00'):
            self.default_vat_rate = Decimal('13.00')
        super().save(*args, **kwargs)
        cache.delete(self.CACHE_KEY)

    @classmethod
    def get_solo(cls):
        cached = cache.get(cls.CACHE_KEY)
        if cached is not None:
            return cached

        obj = cls.objects.filter(is_active=True).first()
        if not obj:
            obj = cls.objects.create(
                company_name_en="Smart Mobile & Optics Hub",
                tax_system_mode='PAN',
                default_vat_rate=Decimal('0.00'),
                bill_header_title="SALES ESTIMATE SLIP",
                require_manager_approval_discount=Decimal('10.00'),
                default_trade_in_margin_percent=Decimal('15.00')
            )
        cache.set(cls.CACHE_KEY, obj, timeout=300)
        return obj


class AuditLog(models.Model):
    """
    Immutable audit logging record for forensic security tracking across prices,
    stock deduction, bill deletion, MDMS status overrides, and trade-in purchases.
    """
    ACTION_CHOICES = [
        ('CREATE', 'Creation'),
        ('UPDATE', 'Modification'),
        ('DELETE', 'Deletion / Soft Delete'),
        ('BILL_CANCEL', 'Bill Cancellation / Void'),
        ('PRICE_OVERRIDE', 'Price / Discount Override'),
        ('STOCK_ADJUST', 'Manual Stock Adjustment'),
        ('MDMS_OVERRIDE', 'NTA MDMS Status Override'),
        ('TRADE_IN_PURCHASE', 'Trade-In / Old Phone Buy-Back Intake'),
        ('LOGIN_FAIL', 'Failed Login Limit Warning'),
    ]

    id = models.BigAutoField(primary_key=True)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='core_audit_logs',
        db_index=True
    )
    branch = models.ForeignKey(
        'branches.Branch', on_delete=models.SET_NULL, null=True, blank=True, related_name='audit_logs',
        db_index=True
    )
    action_type = models.CharField(max_length=30, choices=ACTION_CHOICES, db_index=True)
    module = models.CharField(max_length=50, db_index=True, help_text="e.g. POS, Inventory, TradeIn, MDMS, Purchases, Users")
    object_repr = models.CharField(max_length=255)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = 'core_audit_log'
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['timestamp', 'module']),
            models.Index(fields=['action_type', 'timestamp']),
            models.Index(fields=['branch', 'timestamp']),
        ]
        verbose_name = _('Audit Log')
        verbose_name_plural = _('Audit Logs')

    def __str__(self):
        return f"[{self.timestamp.strftime('%Y-%m-%d %H:%M:%S')}] {self.action_type} on {self.module} by {self.user}"