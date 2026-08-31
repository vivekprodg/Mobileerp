import uuid
from decimal import Decimal
from django.db import models
from django.conf import settings
from django.utils.translation import gettext_lazy as _
from apps.core.models import TimeStampedModel
from apps.branches.models import Branch
from apps.customers.models import Customer
from apps.inventory.models import Product, UnitConversion, ItemInstance


class SalesEstimate(TimeStampedModel):
    """
    Sales Estimation Slip / POS Invoice.
    Tracks salesperson, customer information, multi-mode split payments,
    dynamic tax calculations, trade-in exchange deductions, and customer warranty cards.
    """
    STATUS_CHOICES = [
        ('DRAFT', 'Draft / On Hold (होल्ड)'),
        ('COMPLETED', 'Completed / Finalized (सम्पन्न)'),
        ('CANCELLED', 'Cancelled (रद्द गरिएको)'),
        ('RETURNED', 'Fully Returned (फिर्ता भएको)'),
        ('PARTIALLY_RETURNED', 'Partially Returned (आंशिक फिर्ता)'),
    ]

    PAYMENT_STATUS_CHOICES = [
        ('PAID', 'Fully Paid (पूरा भुक्तानी)'),
        ('PARTIAL', 'Partial Payment (आंशिक)'),
        ('DUE', 'Full Udhaari / Due (उधारो)'),
    ]

    estimate_number = models.CharField(
        max_length=50, unique=True, db_index=True, verbose_name=_("Estimate Slip No.")
    )
    branch = models.ForeignKey(
        Branch, on_delete=models.PROTECT, related_name='sales_estimates'
    )
    customer = models.ForeignKey(
        Customer, on_delete=models.SET_NULL, null=True, blank=True, related_name='sales_estimates'
    )
    customer_name_manual = models.CharField(
        max_length=200, blank=True, null=True, verbose_name=_("Walk-in Customer Name")
    )
    customer_phone_manual = models.CharField(
        max_length=25, blank=True, null=True, verbose_name=_("Walk-in Phone")
    )
    customer_pan = models.CharField(
        max_length=15, blank=True, null=True, verbose_name=_("Customer PAN (Optional)")
    )

    # Date Trackers
    bill_date_ad = models.DateField(auto_now_add=True, db_index=True)
    bill_date_bs = models.CharField(max_length=15, blank=True, null=True, verbose_name=_("Bill Date (BS)"))

    # Financial Breakdown
    subtotal = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Gross Items Subtotal"))
    item_discount_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Total Line Discounts"))
    bill_discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Bill Discount (%)"))
    bill_discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Bill Discount (NPR)"))
    
    # Old Phone Trade-In / Exchange Deduction
    has_trade_in_exchange = models.BooleanField(default=False, verbose_name=_("Has Old Phone Trade-In Exchange"))
    trade_in_discount_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Trade-In Buy-Back Valuation Credit (NPR)"),
        help_text=_("Amount deducted directly from bill total for traded-in old handset.")
    )
    trade_in_voucher_reference = models.CharField(
        max_length=50, blank=True, null=True, db_index=True,
        verbose_name=_("Trade-In Voucher No.")
    )

    # Tax Breakdown
    is_vat_applicable = models.BooleanField(default=False, verbose_name=_("Is Tax Applicable"))
    taxable_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Taxable Base Amount"))
    non_taxable_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Non-Taxable / Exempt Amount"))
    vat_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Total Tax / VAT Amount"))
    
    round_off = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Round Off Offset"))
    grand_total = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'), db_index=True, verbose_name=_("Payable Grand Total"))

    # Cost of Goods Sold (COGS) & Total Margin Tracker
    total_cost_amount = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Total Acquisition Cost (COGS) (NPR)")
    )
    total_gross_profit = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Gross Profit Realized (NPR)")
    )

    # Payments & Collections
    paid_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    due_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    change_returned = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))

    status = models.CharField(max_length=25, choices=STATUS_CHOICES, default='COMPLETED', db_index=True)
    payment_status = models.CharField(max_length=20, choices=PAYMENT_STATUS_CHOICES, default='PAID', db_index=True)

    cashier = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='billed_estimates'
    )
    salesperson = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='credited_sales'
    )
    manager_override_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='approved_discounts'
    )
    cancellation_reason = models.TextField(blank=True, null=True)
    notes = models.TextField(blank=True, null=True)

    class Meta:
        db_table = 'pos_sales_estimates'
        ordering = ['-created_at']
        verbose_name = _('Sales Estimate Slip')
        verbose_name_plural = _('Sales Estimate Slips')
        indexes = [
            models.Index(fields=['bill_date_ad', 'status', 'branch'], name='idx_est_date_status_branch'),
            models.Index(fields=['branch', 'payment_status', 'created_at'], name='idx_est_branch_pay_created'),
            models.Index(fields=['customer', 'bill_date_ad'], name='idx_est_cust_date'),
            models.Index(fields=['salesperson', 'bill_date_ad'], name='idx_est_salesperson_date'),
        ]

    def __str__(self):
        return f"{self.estimate_number} - Rs. {self.grand_total} ({self.status})"

    @property
    def recipient_display_name(self) -> str:
        if self.customer:
            return self.customer.name
        return self.customer_name_manual or "Cash Customer (खुदरा ग्राहक)"

    @property
    def total_discount_given(self) -> Decimal:
        return self.item_discount_total + self.bill_discount_amount + self.trade_in_discount_amount


class SalesEstimateItem(TimeStampedModel):
    """Line item in sales estimate linked to exact sold IMEI, pricing mode, batch, and warranty card."""
    estimate = models.ForeignKey(SalesEstimate, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='sales_lines')
    unit_conversion = models.ForeignKey(
        UnitConversion, on_delete=models.SET_NULL, null=True, blank=True
    )
    
    quantity = models.DecimalField(max_digits=10, decimal_places=3, default=Decimal('1.000'))
    conversion_factor = models.DecimalField(max_digits=10, decimal_places=3, default=Decimal('1.000'))
    base_unit_quantity = models.DecimalField(max_digits=12, decimal_places=3)
    
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, verbose_name=_("Unit Selling Price (NPR)"))
    cost_price = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Exact Acquisition Cost Price (NPR)")
    )
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.00'))
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    
    tax_pricing_type = models.CharField(
        max_length=20,
        choices=Product.TAX_PRICING_TYPE_CHOICES,
        default='EXEMPT',
        verbose_name=_("Line Tax Pricing Mode")
    )
    is_vat_applicable = models.BooleanField(default=False, verbose_name=_("Is Tax Applicable"))
    vat_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Tax / VAT Rate (%)"))
    base_taxable_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Pre-Tax Base Amount"))
    taxable_line_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Calculated Tax Amount"))
    line_total = models.DecimalField(max_digits=14, decimal_places=2, verbose_name=_("Final Line Total"))

    # Smartphone / IMEI / Batch tracking linkage
    item_instance = models.ForeignKey(
        ItemInstance, on_delete=models.SET_NULL, null=True, blank=True, related_name='sold_records'
    )
    batch_reference = models.CharField(max_length=60, blank=True, null=True, help_text=_("Associated Batch Identifier"))
    imei_number = models.CharField(max_length=35, blank=True, null=True, db_index=True)
    secondary_imei = models.CharField(max_length=35, blank=True, null=True)
    serial_number = models.CharField(max_length=60, blank=True, null=True)
    device_condition = models.CharField(max_length=30, blank=True, null=True, default="Brand New")
    
    # Customer Warranty Card
    warranty_months = models.PositiveIntegerField(default=12)
    warranty_start_date = models.DateField(blank=True, null=True)
    warranty_expiry_date = models.DateField(blank=True, null=True)
    warranty_terms = models.CharField(max_length=255, blank=True, null=True, default="1 Year Official Brand Warranty")

    class Meta:
        db_table = 'pos_sales_estimate_items'
        verbose_name = _('Sales Estimate Item')
        verbose_name_plural = _('Sales Estimate Items')
        indexes = [
            models.Index(fields=['estimate', 'product'], name='idx_estitem_est_prod'),
            models.Index(fields=['imei_number'], name='idx_estitem_imei'),
        ]

    def __str__(self):
        return f"{self.product.name} x {self.quantity} = Rs. {self.line_total}"

    @property
    def line_gross_profit(self) -> Decimal:
        total_cost = self.cost_price * self.base_unit_quantity
        net_revenue = self.base_taxable_amount if (self.is_vat_applicable and self.vat_rate > 0) else (self.line_total - self.tax_amount)
        return net_revenue - total_cost


class SalesPaymentTransaction(TimeStampedModel):
    """Split payment recording across Cash, Digital Wallets, Cards, and Udhaari."""
    PAYMENT_MODES = [
        ('CASH', 'Cash (नगद)'),
        ('ESEWA', 'eSewa (ईसेवा)'),
        ('KHALTI', 'Khalti (खल्ती)'),
        ('FONEPAY', 'FonePay QR (फोनपे)'),
        ('CARD', 'POS Card Swipe (कार्ड)'),
        ('BANK_TRANSFER', 'Bank Transfer / ConnectIPS'),
        ('CREDIT', 'Udhaari / Account Balance (उधारो)'),
    ]

    estimate = models.ForeignKey(
        SalesEstimate, on_delete=models.CASCADE, related_name='payment_transactions'
    )
    payment_mode = models.CharField(max_length=30, choices=PAYMENT_MODES, db_index=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    transaction_ref = models.CharField(
        max_length=100, blank=True, null=True, help_text=_("QR / Bank / Card Auth Reference")
    )
    notes = models.CharField(max_length=255, blank=True, null=True)

    class Meta:
        db_table = 'pos_payment_transactions'
        verbose_name = _('Payment Transaction')
        verbose_name_plural = _('Payment Transactions')
        indexes = [
            models.Index(fields=['estimate', 'payment_mode'], name='idx_pay_est_mode'),
            models.Index(fields=['payment_mode', 'created_at'], name='idx_pay_mode_date'),
        ]

    def __str__(self):
        return f"{self.estimate.estimate_number} - {self.payment_mode}: Rs. {self.amount}"


class PhoneExchangeTradeIn(TimeStampedModel):
    """
    Second-Hand Phone Buy-Back & Trade-In Exchange Order.
    Manages 10-point technical diagnosis, algorithmic valuation, police-compliant
    ownership undertaking (KYC), POS bill deduction offset, and inventory restocking.
    """
    TRADE_IN_STATUS_CHOICES = [
        ('DRAFT', '1. Inspection In-Progress (जाँच हुँदै)'),
        ('VALUATED', '2. Valuated / Offer Generated (मूल्याङ्कन तयार)'),
        ('ATTACHED_TO_BILL', '3. Deducted Against POS Bill (बिलमा समायोजन)'),
        ('RESTOCKED', '4. Added to Used Inventory (मौज्दातमा दर्ता)'),
        ('CANCELLED', '5. Cancelled / Customer Rejected (रद्द)'),
    ]

    voucher_number = models.CharField(
        max_length=50, unique=True, db_index=True, verbose_name=_("Trade-In Voucher No.")
    )
    branch = models.ForeignKey(
        Branch, on_delete=models.PROTECT, related_name='trade_in_vouchers'
    )
    pos_estimate = models.ForeignKey(
        SalesEstimate, on_delete=models.SET_NULL, null=True, blank=True, related_name='trade_in_exchanges'
    )
    customer = models.ForeignKey(
        Customer, on_delete=models.SET_NULL, null=True, blank=True, related_name='trade_in_vouchers'
    )
    customer_name_manual = models.CharField(max_length=150, verbose_name=_("Customer Name"))
    customer_phone_manual = models.CharField(max_length=30, db_index=True, verbose_name=_("Customer Phone"))

    cashier = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='processed_trade_ins'
    )
    inspector_technician = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='inspected_trade_ins'
    )

    # Traded-in Device Profile
    brand_name = models.CharField(max_length=80, verbose_name=_("Brand (e.g. Apple, Samsung)"))
    model_name = models.CharField(max_length=120, verbose_name=_("Phone Model (e.g. iPhone 13)"))
    ram_capacity = models.CharField(max_length=30, blank=True, null=True, verbose_name=_("RAM (e.g. 6GB)"))
    storage_capacity = models.CharField(max_length=30, verbose_name=_("Storage (e.g. 128GB)"))
    color_variant = models.CharField(max_length=50, blank=True, null=True, verbose_name=_("Color / Finish"))

    imei_1 = models.CharField(max_length=35, db_index=True, verbose_name=_("Primary IMEI 1"))
    imei_2 = models.CharField(max_length=35, blank=True, null=True, verbose_name=_("Secondary IMEI 2"))
    serial_number = models.CharField(max_length=60, blank=True, null=True, verbose_name=_("Serial Number"))

    mdms_status = models.CharField(
        max_length=30,
        choices=Product.MDMS_STATUS_CHOICES,
        default='REGISTERED_OFFICIAL',
        verbose_name=_("Old Device NTA MDMS Status")
    )

    # Mathematical Valuation Breakdown
    market_base_value = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Benchmark Market Value (Pristine Condition) (NPR)")
    )
    total_deductions = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Total Defect & Physical Deductions (NPR)")
    )
    shop_margin_deduction = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Shop Buy-Back Margin Buffer (NPR)")
    )
    final_trade_in_value = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Final Buy-Back / Trade-In Offer Value (NPR)")
    )

    # Recommended Pre-Owned Inventory Classification
    recommended_condition_grade = models.CharField(
        max_length=25,
        choices=ItemInstance.CONDITION_CHOICES,
        default='USED_GRADE_B',
        verbose_name=_("Calculated Condition Grade")
    )
    restocked_product = models.ForeignKey(
        Product, on_delete=models.SET_NULL, null=True, blank=True, related_name='trade_in_origins'
    )
    restocked_item_instance = models.ForeignKey(
        ItemInstance, on_delete=models.SET_NULL, null=True, blank=True, related_name='trade_in_origin'
    )

    status = models.CharField(
        max_length=30, choices=TRADE_IN_STATUS_CHOICES, default='DRAFT', db_index=True
    )
    evaluation_notes = models.TextField(blank=True, null=True)

    class Meta:
        db_table = 'pos_phone_trade_in_exchanges'
        ordering = ['-created_at']
        verbose_name = _('Phone Exchange / Trade-In Voucher')
        verbose_name_plural = _('Phone Exchange / Trade-In Vouchers')
        indexes = [
            models.Index(fields=['branch', 'status'], name='idx_tradein_branch_status'),
            models.Index(fields=['imei_1'], name='idx_tradein_imei1'),
            models.Index(fields=['voucher_number'], name='idx_tradein_voucher_no'),
        ]

    def __str__(self):
        return f"{self.voucher_number} - {self.brand_name} {self.model_name} (Rs. {self.final_trade_in_value}) [{self.status}]"


class TradeInInspectionChecklist(TimeStampedModel):
    """
    10-Point Technical Diagnostic Inspection for Old Traded-In Phones.
    Automatically scores functionality and calculates penalty deductions.
    """
    TOUCH_CHOICES = [
        ('PASS_FLAWLESS', 'Pass: Flawless Screen & Touch (0% Deduction)'),
        ('MINOR_SCRATCHES', 'Minor Hairline Scratches (-5% Deduction)'),
        ('GREEN_LINE_BLEED', 'Display Lines / OLED Bleed (-35% Deduction)'),
        ('CRACKED_GLASS', 'Front Glass Cracked (-40% Deduction)'),
        ('DEAD_TOUCH', 'Dead Touch Digitizer / Black Display (-60% Deduction)'),
    ]

    CAMERA_CHOICES = [
        ('BOTH_WORKING', 'Both Front & Rear Cameras Clear (0% Deduction)'),
        ('FRONT_DEFECTIVE', 'Front Selfie Camera Blurry/Dead (-10% Deduction)'),
        ('REAR_DEFECTIVE', 'Rear Main Camera Lens/Focus Broken (-15% Deduction)'),
        ('BOTH_DEFECTIVE', 'Both Cameras Defective (-25% Deduction)'),
    ]

    BATTERY_CHOICES = [
        ('HEALTH_GOOD_85_PLUS', 'Battery Health 85%+ / Normal Backup (0% Deduction)'),
        ('HEALTH_FAIR_70_85', 'Battery Health 70-85% / Fair Backup (-8% Deduction)'),
        ('POOR_DRAINING_FAST', 'Battery Draining Rapidly / Service Needed (-15% Deduction)'),
        ('PORT_FAULTY', 'Charging Port Loose / Intermittent (-10% Deduction)'),
    ]

    CONNECTIVITY_CHOICES = [
        ('ALL_WORKING', 'Wi-Fi, Bluetooth & GPS All Working (0% Deduction)'),
        ('WIFI_FAULTY', 'Wi-Fi Greyed Out / Cannot Connect (-20% Deduction)'),
        ('BT_FAULTY', 'Bluetooth Failure (-10% Deduction)'),
        ('NO_SIGNAL', 'Baseband / Signal Chip Failure (-40% Deduction)'),
    ]

    AUDIO_CALLING_CHOICES = [
        ('ALL_CLEAR', 'Mic, Earpiece & Loudspeaker Clear (0% Deduction)'),
        ('MIC_FAULTY', 'Primary Microphone Not Working (-10% Deduction)'),
        ('EARPIECE_FAULTY', 'Ear Speaker Muffled (-8% Deduction)'),
        ('LOUDSPEAKER_CRACKLE', 'Loudspeaker Crackling / Distorted (-8% Deduction)'),
    ]

    BIOMETRICS_CHOICES = [
        ('FINGERPRINT_FACEID_OK', 'Fingerprint & Face ID Fully Functional (0% Deduction)'),
        ('FINGERPRINT_DEAD', 'Fingerprint Sensor Dead (-10% Deduction)'),
        ('FACEID_FAIL', 'Face ID / TrueDepth Disabled (-18% Deduction)'),
        ('NOT_SUPPORTED', 'Not Supported on this Model (0% Deduction)'),
    ]

    BODY_GRADE_CHOICES = [
        ('PRISTINE_GRADE_A', 'Grade A: Pristine / Near Mint Condition (0% Deduction)'),
        ('LIGHT_SCUFFS_GRADE_B', 'Grade B: Normal Scuffs / Minor Edge Wear (-6% Deduction)'),
        ('CORNER_DENTS_GRADE_C', 'Grade C: Noticeable Drop Dents / Back Scratches (-12% Deduction)'),
        ('BENT_FRAME_GRADE_D', 'Grade D: Bent Frame / Heavy Structural Damage (-25% Deduction)'),
    ]

    LDI_CHOICES = [
        ('WHITE_NO_LIQUID', 'White / Clean: No Liquid Ingress (0% Deduction)'),
        ('PINK_RED_TRIGGERED', 'Pink / Red: Liquid Damage Indicator Triggered (-30% Deduction)'),
    ]

    ACCESSORIES_CHOICES = [
        ('BOX_AND_ORIGINAL_CHARGER', 'Original Box + Original Fast Charger (0% Deduction)'),
        ('CHARGER_ONLY', 'Charger Only (No Box) (-4% Deduction)'),
        ('BOX_ONLY', 'Box Only (No Charger) (-4% Deduction)'),
        ('HANDSET_ONLY', 'Handset Only (No Box / No Charger) (-8% Deduction)'),
    ]

    RESET_LOCK_CHOICES = [
        ('ICLOUD_MI_ACCOUNT_REMOVED', 'iCloud / Mi Account / Google FRP Fully Removed (Ready for Sale)'),
        ('FRP_LOCKED_CANNOT_RESET', 'Account Locked / Cannot Reset (PROHIBITED - Zero Value)'),
    ]

    trade_in_voucher = models.OneToOneField(
        PhoneExchangeTradeIn, on_delete=models.CASCADE, related_name='inspection_checklist'
    )

    # 10 Diagnostic Criteria
    touch_and_display = models.CharField(max_length=30, choices=TOUCH_CHOICES, default='PASS_FLAWLESS')
    front_and_back_cameras = models.CharField(max_length=30, choices=CAMERA_CHOICES, default='BOTH_WORKING')
    charging_and_battery = models.CharField(max_length=30, choices=BATTERY_CHOICES, default='HEALTH_GOOD_85_PLUS')
    wifi_bluetooth_gps = models.CharField(max_length=30, choices=CONNECTIVITY_CHOICES, default='ALL_WORKING')
    cellular_calling_mic_speaker = models.CharField(max_length=30, choices=AUDIO_CALLING_CHOICES, default='ALL_CLEAR')
    biometrics_security = models.CharField(max_length=30, choices=BIOMETRICS_CHOICES, default='FINGERPRINT_FACEID_OK')
    body_frame_condition = models.CharField(max_length=30, choices=BODY_GRADE_CHOICES, default='LIGHT_SCUFFS_GRADE_B')
    liquid_ingress_ldi = models.CharField(max_length=30, choices=LDI_CHOICES, default='WHITE_NO_LIQUID')
    original_accessories_available = models.CharField(max_length=30, choices=ACCESSORIES_CHOICES, default='HANDSET_ONLY')
    account_lock_factory_reset = models.CharField(max_length=35, choices=RESET_LOCK_CHOICES, default='ICLOUD_MI_ACCOUNT_REMOVED')

    diagnostic_score_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('100.00'), verbose_name=_("Diagnostic Condition Score (%)")
    )
    technician_remarks = models.TextField(blank=True, null=True)

    class Meta:
        db_table = 'pos_trade_in_inspection_checklists'
        verbose_name = _('Trade-In 10-Point Inspection Checklist')
        verbose_name_plural = _('Trade-In 10-Point Inspection Checklists')


class TradeInLegalUndertaking(TimeStampedModel):
    """
    Police-Compliant Customer Ownership Handover & Undertaking Record (जिम्मानामा तथा मञ्जुरीनामा).
    Protects shop owners from legal liability, stolen property claims, and police investigations.
    """
    ID_TYPE_CHOICES = [
        ('CITIZENSHIP', 'Nepali Citizenship Card (नागरिकता प्रमाणपत्र)'),
        ('NATIONAL_ID', 'National Identity Card (राष्ट्रिय परिचयपत्र)'),
        ('DRIVING_LICENSE', 'Smart Driving License (सवारी चालक अनुमतिपत्र)'),
        ('PASSPORT', 'Passport (राहदानी)'),
    ]

    trade_in_voucher = models.OneToOneField(
        PhoneExchangeTradeIn, on_delete=models.CASCADE, related_name='legal_undertaking'
    )

    # Customer Identity Details
    customer_full_name = models.CharField(max_length=150, verbose_name=_("Customer Full Name (English/Nepali)"))
    customer_father_or_spouse_name = models.CharField(max_length=150, blank=True, null=True, verbose_name=_("Father / Spouse Name"))
    
    id_type = models.CharField(max_length=30, choices=ID_TYPE_CHOICES, default='CITIZENSHIP')
    id_number = models.CharField(max_length=60, db_index=True, verbose_name=_("Identification / Citizenship No."))
    id_issued_district = models.CharField(max_length=100, default="Kathmandu", verbose_name=_("Issued District"))
    id_issued_date_bs = models.CharField(max_length=20, blank=True, null=True, verbose_name=_("Issued Date (BS)"))
    
    permanent_address = models.CharField(max_length=255, verbose_name=_("Permanent Address (District, Ward, Municipality)"))
    current_address = models.CharField(max_length=255, blank=True, null=True, verbose_name=_("Current Residence / Room Address"))

    # KYC Uploads & Evidence Photos
    id_front_image = models.ImageField(
        upload_to=settings.TRADE_IN_DOCUMENT_UPLOAD_DIR, blank=True, null=True,
        verbose_name=_("Citizenship / NID Front Photo")
    )
    id_back_image = models.ImageField(
        upload_to=settings.TRADE_IN_DOCUMENT_UPLOAD_DIR, blank=True, null=True,
        verbose_name=_("Citizenship / NID Back Photo")
    )
    customer_live_photo = models.ImageField(
        upload_to=settings.TRADE_IN_DOCUMENT_UPLOAD_DIR, blank=True, null=True,
        verbose_name=_("Customer Live Photo Snapshot (Holding Phone)")
    )
    customer_digital_signature = models.TextField(
        blank=True, null=True,
        help_text=_("Base64 string of digital signature or thumbprint canvas data")
    )

    # Legal Declaration & Acceptance
    declaration_text = models.TextField(verbose_name=_("Full Undertaking Text"))
    declaration_accepted = models.BooleanField(
        default=True,
        verbose_name=_("Customer Accepted Legal Ownership Responsibility")
    )
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='verified_trade_in_undertakings'
    )

    class Meta:
        db_table = 'pos_trade_in_legal_undertakings'
        verbose_name = _('Customer Ownership Undertaking (Police Compliance)')
        verbose_name_plural = _('Customer Ownership Undertakings (Police Compliance)')
        indexes = [
            models.Index(fields=['id_number', 'id_type'], name='idx_undertaking_id'),
        ]

    def __str__(self):
        return f"Undertaking: {self.customer_full_name} (ID: {self.id_number}) - Voucher {self.trade_in_voucher.voucher_number}"


class SalesReturn(TimeStampedModel):
    """Customer sales return or warranty replacement voucher."""
    return_number = models.CharField(max_length=50, unique=True, db_index=True)
    original_estimate = models.ForeignKey(
        SalesEstimate, on_delete=models.PROTECT, related_name='returns'
    )
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='sales_returns')
    customer = models.ForeignKey(
        Customer, on_delete=models.SET_NULL, null=True, blank=True, related_name='returns'
    )
    
    total_refund_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    refund_mode = models.CharField(
        max_length=30,
        choices=[
            ('CASH', 'Cash Refund'),
            ('STORE_CREDIT', 'Customer Store Credit'),
            ('EXCHANGE_ADJUST', 'Adjusted in Exchange Bill'),
        ],
        default='CASH'
    )
    reason = models.TextField(verbose_name=_("Reason for Return"))
    technician_notes = models.TextField(blank=True, null=True, verbose_name=_("Diagnostic / Inspection Findings"))
    processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='processed_returns'
    )

    class Meta:
        db_table = 'pos_sales_returns'
        ordering = ['-created_at']
        verbose_name = _('Sales Return')
        verbose_name_plural = _('Sales Returns')
        indexes = [
            models.Index(fields=['branch', 'created_at'], name='idx_return_branch_date'),
            models.Index(fields=['return_number'], name='idx_return_num'),
        ]

    def __str__(self):
        return f"{self.return_number} for {self.original_estimate.estimate_number} (Rs. {self.total_refund_amount})"


class SalesReturnItem(TimeStampedModel):
    sales_return = models.ForeignKey(SalesReturn, on_delete=models.CASCADE, related_name='items')
    estimate_item = models.ForeignKey(SalesEstimateItem, on_delete=models.PROTECT)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    return_quantity = models.DecimalField(max_digits=10, decimal_places=3)
    base_unit_quantity = models.DecimalField(max_digits=12, decimal_places=3)
    refund_amount = models.DecimalField(max_digits=12, decimal_places=2)
    returned_imei = models.CharField(max_length=35, blank=True, null=True)
    restock_to_inventory = models.BooleanField(
        default=True, help_text=_("Re-add returned item back to active branch stock")
    )
    is_defective = models.BooleanField(
        default=False, help_text=_("Mark item as defective/damaged for vendor warranty claim")
    )
    defect_reason = models.CharField(max_length=255, blank=True, null=True)

    class Meta:
        db_table = 'pos_sales_return_items'
        verbose_name = _('Sales Return Item')
        verbose_name_plural = _('Sales Return Items')
        indexes = [
            models.Index(fields=['sales_return', 'product'], name='idx_retitem_ret_prod'),
        ]