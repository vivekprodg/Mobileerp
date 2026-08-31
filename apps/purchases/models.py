import uuid
from decimal import Decimal
from django.db import models
from django.conf import settings
from django.utils.translation import gettext_lazy as _
from apps.core.models import TimeStampedModel
from apps.branches.models import Branch
from apps.inventory.models import Product, UnitOfMeasurement, UnitConversion


class Supplier(TimeStampedModel):
    """
    Comprehensive Supplier / Distributor Entity for Nepal's Mobile & Optical Ecosystem.
    Tracks government registration, tiered brand authorizations, DOA policies,
    credit terms, and structured banking details.
    """

    SUPPLIER_TYPE_CHOICES = [
        ('NATIONAL_DISTRIBUTOR', 'National Importer / Distributor (राष्ट्रिय आयातकर्ता)'),
        ('REGIONAL_WHOLESALER', 'Regional Wholesaler / Stockist (क्षेत्रीय थोक विक्रेता)'),
        ('MANUFACTURER', 'Authorized Brand Manufacturer (उत्पादक)'),
        ('LOCAL_TRADER', 'Local Dealer / Trader (स्थानीय बिक्रेता)'),
        ('SERVICE_PARTS_VENDOR', 'Spare Parts & Service Supplier (पार्ट्स आपूर्तिकर्ता)'),
    ]

    PROVINCE_CHOICES = [
        ('Koshi', 'Koshi Province (कोशी प्रदेश)'),
        ('Madhesh', 'Madhesh Province (मधेश प्रदेश)'),
        ('Bagmati', 'Bagmati Province (बागमती प्रदेश)'),
        ('Gandaki', 'Gandaki Province (गण्डकी प्रदेश)'),
        ('Lumbini', 'Lumbini Province (लुम्बिनी प्रदेश)'),
        ('Karnali', 'Karnali Province (कर्णाली प्रदेश)'),
        ('Sudurpashchim', 'Sudurpashchim Province (सुदूरपश्चिम प्रदेश)'),
    ]

    BALANCE_TYPE_CHOICES = [
        ('PAYABLE', 'Payable to Supplier (हामीले तिर्नुपर्ने दायित्व)'),
        ('ADVANCE', 'Advance Paid to Supplier (अग्रिम भुक्तानी)'),
    ]

    PAYMENT_METHOD_CHOICES = [
        ('BANK_TRANSFER', 'Bank Transfer / ConnectIPS (बैंक ट्रान्सफर)'),
        ('CHEQUE', 'Account Payee Cheque (चेक)'),
        ('DIGITAL_QR', 'FonePay / Digital QR (डिजिटल क्यूआर)'),
        ('CASH', 'Cash Counter (नगद)'),
    ]

    DISTRIBUTOR_TIER_CHOICES = [
        ('TIER_1_NATIONAL', 'Tier-1 National Importer (Official)'),
        ('TIER_2_REGIONAL', 'Tier-2 Regional Super Stockist'),
        ('MASTER_DEALER', 'Master Dealer / Area Distributor'),
        ('LOCAL_DEALER', 'Authorized Local Sub-Dealer'),
    ]

    STATUS_CHOICES = [
        ('ACTIVE', 'Active & Verified (सक्रिय)'),
        ('INACTIVE', 'Inactive / Paused (निष्क्रिय)'),
        ('BLOCKED', 'Blocked / Disputed (रोक्का गरिएको)'),
    ]

    # --- 1. System Identification & 5 Strictly Mandatory Fields ---
    code = models.CharField(
        max_length=25, unique=True, db_index=True, blank=True,
        verbose_name=_("Supplier Code"),
        help_text=_("Auto-generated sequential code (e.g. SUP-00001)")
    )
    company_name = models.CharField(
        max_length=200, db_index=True,
        verbose_name=_("Company / Firm Name")
    )
    contact_person = models.CharField(
        max_length=150,
        verbose_name=_("Authorized Contact Person")
    )
    phone_number = models.CharField(
        max_length=25, unique=True, db_index=True,
        verbose_name=_("Primary Mobile / Phone Number")
    )
    address = models.CharField(
        max_length=255,
        verbose_name=_("Street Address / Market Location")
    )
    registration_number = models.CharField(
        max_length=100, default="PENDING-REG", db_index=True,
        verbose_name=_("Company Registrar / Govt Reg No.")
    )

    # --- 2. Location, Type & Secondary Contact ---
    supplier_type = models.CharField(
        max_length=30, choices=SUPPLIER_TYPE_CHOICES, default='NATIONAL_DISTRIBUTOR',
        verbose_name=_("Supplier Classification")
    )
    designation = models.CharField(
        max_length=100, blank=True, null=True,
        verbose_name=_("Contact Person Designation")
    )
    alt_phone = models.CharField(
        max_length=25, blank=True, null=True,
        verbose_name=_("Alternate / Office Landline")
    )
    email = models.EmailField(blank=True, null=True, verbose_name=_("Official Email Address"))
    pan_number = models.CharField(max_length=15, blank=True, null=True, verbose_name=_("PAN Number"))
    vat_number = models.CharField(max_length=20, blank=True, null=True, verbose_name=_("VAT Registration No."))
    city = models.CharField(max_length=100, default="Kathmandu", blank=True, verbose_name=_("City"))
    province = models.CharField(
        max_length=50, choices=PROVINCE_CHOICES, default='Bagmati',
        verbose_name=_("Province (नेपालको प्रदेश)")
    )
    country = models.CharField(max_length=50, default="Nepal", blank=True, verbose_name=_("Country"))

    # --- 3. Business Terms, Credit & Structured Banking ---
    credit_period_days = models.PositiveIntegerField(
        default=30, null=True, blank=True, verbose_name=_("Credit Terms (Days)")
    )
    credit_limit = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00'), null=True, blank=True,
        verbose_name=_("Credit Limit (NPR)"),
        help_text=_("Maximum allowed credit line with this vendor (0.00 for unlimited/cash-only)")
    )
    opening_balance = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00'), null=True, blank=True,
        verbose_name=_("Opening Balance (NPR)")
    )
    balance_type = models.CharField(
        max_length=15, choices=BALANCE_TYPE_CHOICES, default='PAYABLE',
        verbose_name=_("Opening Balance Nature")
    )
    current_balance = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00'), null=True, blank=True,
        verbose_name=_("Net Outstanding Balance (NPR)"),
        help_text=_("Positive balance indicates amount shop owes the supplier.")
    )
    preferred_payment_method = models.CharField(
        max_length=25, choices=PAYMENT_METHOD_CHOICES, default='BANK_TRANSFER',
        verbose_name=_("Preferred Payment Mode")
    )
    bank_name = models.CharField(max_length=120, blank=True, null=True, verbose_name=_("Bank Name"))
    bank_account_number = models.CharField(max_length=60, blank=True, null=True, verbose_name=_("Bank Account Number"))
    account_holder_name = models.CharField(max_length=150, blank=True, null=True, verbose_name=_("Account Holder Name"))
    bank_branch = models.CharField(max_length=100, blank=True, null=True, verbose_name=_("Bank Branch Location"))
    qr_payment_details = models.TextField(
        blank=True, null=True,
        verbose_name=_("FonePay / Merchant QR Details"),
        help_text=_("Paste FonePay Merchant ID, UPI ID, or account transfer remarks.")
    )
    bank_details = models.TextField(
        blank=True, null=True,
        verbose_name=_("Legacy Banking Notes"),
        help_text=_("Consolidated bank text notes.")
    )

    # --- 4. Catalog Coverage & Vendor Evaluation ---
    product_categories_supplied = models.TextField(
        blank=True, null=True,
        verbose_name=_("Product Categories Supplied"),
        help_text=_("e.g. Smartphones, Optical Frames, Tempered Glass, Chargers")
    )
    brands_supplied = models.TextField(
        blank=True, null=True,
        verbose_name=_("Authorized Brands Supplied"),
        help_text=_("e.g. Samsung, Apple, Xiaomi, Vivo, Realme, Ray-Ban")
    )
    is_preferred = models.BooleanField(
        default=False, verbose_name=_("Preferred / Regular Supplier")
    )
    rating = models.PositiveSmallIntegerField(
        default=5, null=True, blank=True,
        choices=[(1, '1 Star (Poor)'), (2, '2 Stars (Fair)'), (3, '3 Stars (Good)'), (4, '4 Stars (Very Good)'), (5, '5 Stars (Excellent)')],
        verbose_name=_("Vendor Performance Rating")
    )
    last_purchase_date = models.DateField(blank=True, null=True, verbose_name=_("Last Purchase Date"))
    last_payment_date = models.DateField(blank=True, null=True, verbose_name=_("Last Payment Date"))

    # --- 5. Mobile-Specific Distributor & Warranty Policies ---
    is_authorized_distributor = models.BooleanField(
        default=False, verbose_name=_("Is Authorized Brand Distributor"),
        help_text=_("Check if this vendor is an official national or regional brand distributor in Nepal.")
    )
    distributor_tier = models.CharField(
        max_length=30, choices=DISTRIBUTOR_TIER_CHOICES, blank=True, null=True,
        verbose_name=_("Distributor / Dealer Tier")
    )
    warranty_support_available = models.BooleanField(
        default=True, verbose_name=_("Warranty Support Available"),
        help_text=_("Indicates whether this distributor provides in-house warranty and spare parts support.")
    )
    brand_authorization_details = models.TextField(
        blank=True, null=True, verbose_name=_("Brand Authorization Certificate Details"),
        help_text=_("e.g. Authorized National Distributor for Samsung Nepal (Auth Ref: SAM-NP-2024)")
    )
    warranty_claim_contact = models.TextField(
        blank=True, null=True, verbose_name=_("Warranty Claim Lab / Service Center Location"),
        help_text=_("Physical service center address, engineer contact, and dispatch instructions.")
    )
    doa_policy = models.TextField(
        blank=True, null=True, verbose_name=_("7-Day DOA Replacement Policy"),
        help_text=_("Terms for Dead On Arrival (DOA) instant unboxed handset replacement.")
    )
    defective_return_policy = models.TextField(
        blank=True, null=True, verbose_name=_("General Defective RMA Return Policy"),
        help_text=_("Standard return policies for swollen batteries, line displays, and dead boards.")
    )

    # --- 6. Control, KYC Documents & Remarks ---
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='ACTIVE', db_index=True,
        verbose_name=_("Account Status")
    )
    kyc_document = models.FileField(
        upload_to='suppliers/kyc/%Y/%m/', blank=True, null=True,
        verbose_name=_("Company Registration / PAN Certificate Document"),
        help_text=_("Upload PDF, JPG, or PNG copy of VAT/PAN certificate or authorized distributor agreement.")
    )
    notes = models.TextField(blank=True, null=True, verbose_name=_("General Remarks / Internal Notes"))

    class Meta:
        db_table = 'pur_suppliers'
        ordering = ['company_name']
        verbose_name = _('Supplier / Distributor')
        verbose_name_plural = _('Suppliers & Distributors')
        indexes = [
            models.Index(fields=['code'], name='idx_sup_code'),
            models.Index(fields=['company_name', 'status'], name='idx_sup_name_status'),
            models.Index(fields=['registration_number'], name='idx_sup_reg_no'),
        ]

    def __str__(self):
        code_tag = f"[{self.code}] " if self.code else ""
        return f"{code_tag}{self.company_name} ({self.contact_person})"

    def save(self, *args, **kwargs):
        # 1. Auto-generate sequential supplier code if not assigned
        if not self.code:
            last_supplier = Supplier.objects.order_by('-id').first()
            next_num = (last_supplier.id + 1) if last_supplier else 1
            generated_code = f"SUP-{next_num:05d}"
            while Supplier.objects.filter(code=generated_code).exists():
                next_num += 1
                generated_code = f"SUP-{next_num:05d}"
            self.code = generated_code

        # 2. Defensive defaults for numeric fields preventing NULL integrity errors
        if self.credit_limit is None:
            self.credit_limit = Decimal('0.00')
        if self.opening_balance is None:
            self.opening_balance = Decimal('0.00')
        if self.current_balance is None:
            self.current_balance = Decimal('0.00')
        if self.credit_period_days is None:
            self.credit_period_days = 30
        if self.rating is None:
            self.rating = 5

        # 3. Sync is_active boolean with status enum
        self.is_active = (self.status == 'ACTIVE')

        # 4. Synchronize legacy bank_details string for backward compatibility
        if self.bank_name or self.bank_account_number:
            parts = []
            if self.bank_name:
                parts.append(f"Bank: {self.bank_name}")
            if self.account_holder_name:
                parts.append(f"A/C Name: {self.account_holder_name}")
            if self.bank_account_number:
                parts.append(f"A/C No: {self.bank_account_number}")
            if self.bank_branch:
                parts.append(f"Branch: {self.bank_branch}")
            self.bank_details = " | ".join(parts)

        # 5. Handle initial opening balance allocation on creation
        is_new = self.pk is None
        super().save(*args, **kwargs)

        if is_new and self.opening_balance > Decimal('0.00') and self.current_balance == Decimal('0.00'):
            initial_due = self.opening_balance if self.balance_type == 'PAYABLE' else -self.opening_balance
            self.current_balance = initial_due
            Supplier.objects.filter(pk=self.pk).update(current_balance=initial_due)


class PurchaseOrder(TimeStampedModel):
    PO_STATUS = [
        ('DRAFT', 'Draft PO'),
        ('ISSUED', 'Issued to Supplier'),
        ('PARTIALLY_RECEIVED', 'Partially Received'),
        ('COMPLETED', 'Fully Received & Closed'),
        ('CANCELLED', 'Cancelled'),
    ]

    po_number = models.CharField(max_length=50, unique=True, db_index=True, verbose_name=_("PO Number"))
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name='purchase_orders')
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='purchase_orders')
    order_date = models.DateField()
    expected_delivery_date = models.DateField(blank=True, null=True)
    status = models.CharField(max_length=25, choices=PO_STATUS, default='DRAFT', db_index=True)
    
    subtotal = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    total_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    
    notes = models.TextField(blank=True, null=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='created_pos'
    )

    class Meta:
        db_table = 'pur_purchase_orders'
        ordering = ['-order_date', '-created_at']
        verbose_name = _('Purchase Order')
        verbose_name_plural = _('Purchase Orders')

    def __str__(self):
        return f"{self.po_number} - {self.supplier.company_name} ({self.status})"


class PurchaseOrderItem(TimeStampedModel):
    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='po_items')
    unit = models.ForeignKey(UnitOfMeasurement, on_delete=models.PROTECT)
    ordered_quantity = models.DecimalField(max_digits=10, decimal_places=3)
    received_quantity = models.DecimalField(max_digits=10, decimal_places=3, default=Decimal('0.000'))
    unit_cost_price = models.DecimalField(max_digits=12, decimal_places=2)
    line_total = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        db_table = 'pur_po_items'
        verbose_name = _('PO Item')
        verbose_name_plural = _('PO Items')

    def __str__(self):
        return f"{self.product.name} ({self.ordered_quantity} {self.unit.code})"


class GoodsReceivedNote(TimeStampedModel):
    GRN_STATUS = [
        ('DRAFT', 'Draft / In-Inspection'),
        ('RECEIVED', 'Goods Verified & Stock Updated'),
        ('CANCELLED', 'Cancelled / Rejected'),
    ]

    grn_number = models.CharField(max_length=50, unique=True, db_index=True, verbose_name=_("GRN No."))
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name='goods_receipts')
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='goods_receipts')
    purchase_order = models.ForeignKey(
        PurchaseOrder, on_delete=models.SET_NULL, null=True, blank=True, related_name='grn_vouchers'
    )
    
    supplier_bill_no = models.CharField(
        max_length=100, verbose_name=_("Supplier Invoice / Challan Ref No."), db_index=True
    )
    supplier_product_code = models.CharField(
        max_length=100, blank=True, null=True, verbose_name=_("Supplier Batch / Product Code")
    )
    bill_date = models.DateField(verbose_name=_("Bill / Challan Date (AD)"), db_index=True)
    bill_date_bs = models.CharField(max_length=15, blank=True, null=True, verbose_name=_("Bill Date (BS)"))
    
    status = models.CharField(max_length=20, choices=GRN_STATUS, default='DRAFT', db_index=True)

    distributor_mdms_certified = models.BooleanField(
        default=True,
        verbose_name=_("Distributor MDMS Certified"),
        help_text=_("Certifies that all handset IMEIs in this consignment are official imports registered in NTA MDMS.")
    )
    mdms_tax_invoice_ref = models.CharField(
        max_length=100, blank=True, null=True,
        verbose_name=_("Customs Entry / NTA Declaration No."),
        help_text=_("Pragyapan Patra / Customs Declaration or Distributor MDMS Clearance Ref No.")
    )
    
    gross_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    is_vat_bill = models.BooleanField(
        default=False,
        verbose_name=_("Is Tax / VAT Inward Bill"),
        help_text=_("Enable if the supplier provided a VAT bill with dynamic input tax calculation.")
    )
    vat_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Input Tax / VAT Amount"))
    
    extra_freight_charge = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Freight / Courier Charge (NPR)"))
    customs_import_charge = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Customs / Duty / Tax (NPR)"))
    other_handling_charge = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Insurance / Unloading Cost (NPR)"))
    total_landed_cost = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Total Landed Cost (NPR)"))
    
    net_total_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    paid_amount = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00'),
        help_text=_("Amount paid on spot during stock receipt")
    )
    due_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    
    warranty_provider = models.CharField(max_length=150, blank=True, null=True, default="Official National Distributor", verbose_name=_("Supplier Warranty Provider"))
    warranty_months = models.PositiveIntegerField(default=12, verbose_name=_("Warranty Period (Months)"))
    authorized_service_center = models.CharField(max_length=200, blank=True, null=True, verbose_name=_("Authorized Service Center Address/Contact"))
    
    estimate_disclaimer_noted = models.BooleanField(
        default=True,
        help_text=_("Marks this internal GRN voucher as internal proforma/estimation document")
    )
    
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='received_grns'
    )
    remarks = models.TextField(blank=True, null=True)

    class Meta:
        db_table = 'pur_goods_received_notes'
        ordering = ['-created_at']
        verbose_name = _('Goods Received Note (GRN)')
        verbose_name_plural = _('Goods Received Notes (GRN)')

    def __str__(self):
        return f"{self.grn_number} | {self.supplier.company_name} | Rs. {self.net_total_amount}"


class GRNItem(TimeStampedModel):
    """
    Line item in GRN with package unit conversion, landed cost per unit,
    new target selling price, supplier item code, MDMS status default, and batch IMEI scanner payload.
    """
    grn = models.ForeignKey(GoodsReceivedNote, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='grn_items')
    
    supplier_item_code = models.CharField(max_length=100, blank=True, null=True, verbose_name=_("Supplier Item SKU / Code"))
    unit_conversion = models.ForeignKey(
        UnitConversion, on_delete=models.SET_NULL, null=True, blank=True,
        help_text=_("If bought in packaging unit (e.g. Box of 50)")
    )
    purchased_quantity = models.DecimalField(
        max_digits=10, decimal_places=3, default=Decimal('1.000'),
        help_text=_("Quantity in the purchased package/unit")
    )
    conversion_factor = models.DecimalField(
        max_digits=10, decimal_places=3, default=Decimal('1.000'),
        help_text=_("Multiplier to convert to smallest base atomic unit")
    )
    base_unit_quantity = models.DecimalField(
        max_digits=12, decimal_places=3, default=Decimal('1.000'), null=True, blank=True,
        help_text=_("Effective units added to live stock counter")
    )
    
    purchase_rate = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Unit Purchase Rate (NPR)"))
    new_selling_price = models.DecimalField(
        max_digits=12, decimal_places=2, blank=True, null=True,
        verbose_name=_("New Target Selling Price / MRP (NPR)"),
        help_text=_("Updates master product counter rate upon verification")
    )
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.00'))
    is_vat_applicable = models.BooleanField(default=False, verbose_name=_("Is Tax / VAT Applicable"))
    vat_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Tax / VAT Rate (%)"))
    
    unit_landed_cost = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Unit Landed Cost (NPR)"))
    line_total = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'), null=True, blank=True)
    
    default_mdms_status = models.CharField(
        max_length=30,
        choices=Product.MDMS_STATUS_CHOICES,
        default='REGISTERED_OFFICIAL',
        verbose_name=_("Line MDMS Status")
    )
    scanned_imei_list = models.TextField(
        blank=True, null=True,
        help_text=_("Paste or scan IMEIs (one per line or comma-separated) for phones in this batch")
    )
    warranty_months = models.PositiveIntegerField(default=12)
    warranty_provider = models.CharField(max_length=150, blank=True, null=True)

    class Meta:
        db_table = 'pur_grn_items'
        verbose_name = _('GRN Item Line')
        verbose_name_plural = _('GRN Item Lines')

    def __str__(self):
        return f"{self.product.name} ({self.base_unit_quantity} {self.product.base_unit.code})"

    def save(self, *args, **kwargs):
        # Auto-compute base quantity and line total if not explicitly set
        factor = self.conversion_factor if self.conversion_factor and self.conversion_factor > Decimal('0.000') else Decimal('1.000')
        qty = self.purchased_quantity if self.purchased_quantity and self.purchased_quantity > Decimal('0.000') else Decimal('1.000')
        
        if not self.base_unit_quantity or self.base_unit_quantity <= Decimal('0.000'):
            self.base_unit_quantity = qty * factor
            
        if not self.line_total or self.line_total <= Decimal('0.00'):
            rate = self.purchase_rate or Decimal('0.00')
            gross = qty * rate
            disc = gross * ((self.discount_percent or Decimal('0.00')) / Decimal('100.00'))
            self.line_total = gross - disc
            
        super().save(*args, **kwargs)


class SupplierUdhaariLedger(TimeStampedModel):
    TRANSACTION_TYPES = [
        ('PURCHASE_BILL', 'Stock Purchase / GRN (+)'),
        ('PAYMENT', 'Supplier Payout (-)'),
        ('PURCHASE_RETURN', 'Defective Return to Vendor (-)'),
        ('ADJUSTMENT', 'Credit Note / Rate Difference Adjustment'),
    ]

    PAYMENT_MODES = [
        ('CASH', 'Cash (नगद)'),
        ('BANK_TRANSFER', 'Bank Transfer / IPS (बैंक ट्रान्सफर)'),
        ('CHEQUE', 'Cheque (चेक)'),
        ('FONEPAY', 'FonePay QR / Digital'),
        ('OTHER', 'Other Adjustment'),
    ]

    supplier = models.ForeignKey(Supplier, on_delete=models.CASCADE, related_name='ledger_entries')
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, related_name='supplier_ledgers')
    transaction_type = models.CharField(max_length=25, choices=TRANSACTION_TYPES, db_index=True)
    
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    previous_balance = models.DecimalField(max_digits=14, decimal_places=2)
    resulting_balance = models.DecimalField(max_digits=14, decimal_places=2)
    
    payment_mode = models.CharField(max_length=30, choices=PAYMENT_MODES, blank=True, null=True)
    reference_number = models.CharField(max_length=100, blank=True, null=True, help_text="GRN No, Cheque No, Bank Ref")
    cheque_date = models.DateField(blank=True, null=True)
    cheque_cleared = models.BooleanField(default=True)
    
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='recorded_supplier_ledgers'
    )
    remarks = models.TextField(blank=True, null=True)

    class Meta:
        db_table = 'pur_supplier_ledger'
        ordering = ['-created_at']
        verbose_name = _('Supplier Udhaari Entry')
        verbose_name_plural = _('Supplier Udhaari Ledgers')

    def __str__(self):
        return f"{self.supplier.company_name} - {self.transaction_type} Rs. {self.amount}"