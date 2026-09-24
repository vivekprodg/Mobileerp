"""
Procurement, Supplier Udhaari, Goods Received Notes (GRN) & Commercial Purchase Return Models.

Key Architectural Upgrades:
1. Flexible Line & Bill-Level Discount Engines:
   - Line items (GRNItem) support dual discount modes: Flat Cash Amount (रू) or Percentage (%).
   - Preserves user input (`discount_input_value`), rupee deductions (`item_discount_amount`),
     and synchronized secondary percentages (`discount_percent`).
   - GRN Bill Header supports overall invoice discounts (Amount or Percentage) applied on merchandise.
2. Strict Pre-VAT Pricing & 13% VAT Calculation (Nepal Tax Standard):
   - All unit purchase rates and gross calculations are strictly Pre-VAT.
   - When `is_vat_bill` is enabled, 13% VAT is calculated on the Pre-VAT Taxable Base
     (Gross Lines - Total Discounts). When disabled, VAT is strictly Rs. 0.00.
3. Proportional Landed Cost Overhead Allocation:
   - Overhead charges (Freight, Customs/Duty, and Handling/Insurance) are dynamically distributed
     across line items proportional to their net pre-VAT merchandise value.
   - Produces exact unit landed costs (`unit_landed_cost`) for inventory COGS valuation.
4. Historical Integrity (2080 B.S. & Onwards):
   - Supports historical imports with two-way Gregorian AD <-> Bikram Sambat (BS) date synchronization
     and automated Nepali Fiscal Year generation (e.g., '2080/81', '2081/82').
5. Supplier Ledger Reconciliation:
   - Strict `recalculate_balance_from_ledger()` guarantees double-entry integrity with `SupplierUdhaariLedger`.
"""

import re
import uuid
from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime
from django.db import models
from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedModel
from apps.branches.models import Branch
from apps.inventory.models import Product, UnitOfMeasurement, UnitConversion, ItemInstance
from apps.core.nepali_calendar import NepaliCalendar

# =============================================================================
# SUPPLIER / DISTRIBUTOR MODEL
# =============================================================================
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

    # --- 1. System Identification & Core Mandatory Fields ---
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
        max_length=150, db_index=True,
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
    pan_number = models.CharField(
        max_length=15, blank=True, null=True, db_index=True,
        verbose_name=_("PAN Number")
    )
    vat_number = models.CharField(
        max_length=20, blank=True, null=True, db_index=True,
        verbose_name=_("VAT Registration No.")
    )
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
        max_digits=14, decimal_places=2, default=Decimal('0.00'), null=True, blank=True, db_index=True,
        verbose_name=_("Net Outstanding Balance (NPR)"),
        help_text=_("Positive balance indicates amount shop owes supplier. Auto-calculated from ledger.")
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
        help_text=_("Physical service center address, engineer contact, and drop point...")
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
            models.Index(fields=['company_name'], name='idx_sup_company_name'),
            models.Index(fields=['contact_person'], name='idx_sup_contact_person'),
            models.Index(fields=['phone_number'], name='idx_sup_phone_num'),
            models.Index(fields=['pan_number'], name='idx_sup_pan_num'),
            models.Index(fields=['vat_number'], name='idx_sup_vat_num'),
            models.Index(fields=['company_name', 'status'], name='idx_sup_name_status'),
            models.Index(fields=['registration_number'], name='idx_sup_reg_no'),
            models.Index(fields=['current_balance'], name='idx_sup_current_balance'),
        ]

    def __str__(self):
        code_tag = f"[{self.code}] " if self.code else ""
        return f"{code_tag}{self.company_name} ({self.contact_person})"

    def save(self, *args, **kwargs):
        if not self.code:
            last_supplier = Supplier.objects.order_by('-id').first()
            next_num = (last_supplier.id + 1) if last_supplier else 1
            generated_code = f"SUP-{next_num:05d}"
            while Supplier.objects.filter(code=generated_code).exists():
                next_num += 1
                generated_code = f"SUP-{next_num:05d}"
            self.code = generated_code

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

        if hasattr(self, 'is_active'):
            self.is_active = (self.status == 'ACTIVE')

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

        is_new = self.pk is None
        super().save(*args, **kwargs)

        if is_new and self.opening_balance > Decimal('0.00') and self.current_balance == Decimal('0.00'):
            initial_due = self.opening_balance if self.balance_type == 'PAYABLE' else -self.opening_balance
            self.current_balance = initial_due
            Supplier.objects.filter(pk=self.pk).update(current_balance=initial_due)

            SupplierUdhaariLedger.objects.create(
                supplier=self,
                branch=None,
                transaction_type='OPENING_BALANCE',
                amount=self.opening_balance,
                previous_balance=Decimal('0.00'),
                resulting_balance=initial_due,
                payment_mode='OTHER',
                remarks=f"Opening Balance on Registration ({self.get_balance_type_display()})"
            )

    def recalculate_balance_from_ledger(self, save=True):
        """
        Recalculates current_balance strictly from SupplierUdhaariLedger entries.
        Positive balance indicates amount owed to supplier (Payable).
        Negative balance indicates advance paid to supplier (Credit / Advance).
        """
        if not self.pk:
            return self.current_balance or Decimal('0.00')

        opening_entry = self.ledger_entries.filter(
            transaction_type='OPENING_BALANCE'
        ).order_by('created_at', 'id').first()

        if opening_entry:
            base_balance = opening_entry.resulting_balance
        else:
            if self.balance_type == 'PAYABLE':
                base_balance = self.opening_balance or Decimal('0.00')
            else:
                base_balance = -(self.opening_balance or Decimal('0.00'))

        bills_total = self.ledger_entries.filter(
            transaction_type='PURCHASE_BILL'
        ).aggregate(total=models.Sum('amount'))['total'] or Decimal('0.00')

        payments_total = self.ledger_entries.filter(
            transaction_type='PAYMENT'
        ).aggregate(total=models.Sum('amount'))['total'] or Decimal('0.00')

        returns_total = self.ledger_entries.filter(
            transaction_type='PURCHASE_RETURN'
        ).aggregate(total=models.Sum('amount'))['total'] or Decimal('0.00')

        adjustments = self.ledger_entries.filter(transaction_type='ADJUSTMENT')
        adj_total = Decimal('0.00')
        for adj in adjustments:
            if adj.resulting_balance is not None and adj.previous_balance is not None:
                adj_total += (adj.resulting_balance - adj.previous_balance)
            else:
                adj_total += adj.amount

        new_balance = (base_balance + bills_total - payments_total - returns_total + adj_total).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP
        )

        self.current_balance = new_balance
        if save:
            Supplier.objects.filter(pk=self.pk).update(
                current_balance=new_balance,
                updated_at=timezone.now()
            )
        return new_balance

# =============================================================================
# PURCHASE ORDER (PO) MODELS
# =============================================================================
class PurchaseOrder(TimeStampedModel):
    """
    Purchase Order (PO) Header.
    Allows manual specification of historical order dates with automatic BS & Fiscal Year sync.
    """
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

    order_date = models.DateField(
        default=timezone.now,
        db_index=True,
        verbose_name=_("PO Order Date (AD)"),
        help_text=_("Gregorian date for database indexing. Allows manual historical import dates.")
    )
    order_date_bs = models.CharField(
        max_length=15,
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("PO Order Date (BS)"),
        help_text=_("Bikram Sambat formatted date string (YYYY-MM-DD).")
    )
    fiscal_year = models.CharField(
        max_length=10,
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Fiscal Year (BS)"),
        help_text=_("Nepali Fiscal Year derived from BS date (e.g. 2080/81, 2081/82).")
    )

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
        indexes = [
            models.Index(fields=['order_date', 'status', 'branch'], name='idx_po_date_status_branch'),
            models.Index(fields=['fiscal_year', 'branch'], name='idx_po_fy_branch'),
            models.Index(fields=['po_number'], name='idx_po_num'),
            models.Index(fields=['supplier', 'order_date'], name='idx_po_supplier_date'),
        ]

    def __str__(self):
        return f"{self.po_number} - {self.supplier.company_name} ({self.status})"

    def save(self, *args, **kwargs):
        if self.order_date:
            ad_date = self.order_date.date() if isinstance(self.order_date, datetime) else self.order_date
            if not self.order_date_bs or not self.fiscal_year:
                try:
                    bs_year, bs_month, bs_day = NepaliCalendar.ad_to_bs(ad_date)
                    if not self.order_date_bs:
                        self.order_date_bs = NepaliCalendar.format_bs(bs_year, bs_month, bs_day, lang='en')
                    if not self.fiscal_year:
                        self.fiscal_year = NepaliCalendar.get_fiscal_year(bs_year, bs_month)
                except Exception:
                    pass
        elif self.order_date_bs:
            try:
                from apps.core.utils.nepali_date_converter import bs_to_ad_date
                self.order_date = bs_to_ad_date(self.order_date_bs)
                parts = [int(p) for p in re.findall(r'\d+', str(self.order_date_bs))]
                if len(parts) >= 2 and not self.fiscal_year:
                    self.fiscal_year = NepaliCalendar.get_fiscal_year(parts[0], parts[1])
            except Exception:
                pass

        super().save(*args, **kwargs)

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
        indexes = [
            models.Index(fields=['purchase_order', 'product'], name='idx_po_item_po_prod'),
        ]

    def __str__(self):
        return f"{self.product.name} ({self.ordered_quantity} {self.unit.code})"

# =============================================================================
# GOODS RECEIVED NOTE (GRN) & INWARD PROCUREMENT
# =============================================================================
class GoodsReceivedNote(TimeStampedModel):
    """
    Goods Received Note (GRN) Inward Procurement Voucher.
    Tracks supplier invoices, Pre-VAT merchandise values, flexible bill-level discounts,
    dedicated 13% VAT, value-based overhead distribution (Landed Cost/COGS),
    NTA MDMS certification, and historical purchase dates.
    """
    GRN_STATUS = [
        ('DRAFT', 'Draft / In-Inspection'),
        ('RECEIVED', 'Goods Verified & Stock Updated'),
        ('CANCELLED', 'Cancelled / Rejected'),
    ]

    DISCOUNT_TYPE_CHOICES = [
        ('NONE', _('No Bill Discount')),
        ('PERCENTAGE', _('Percentage (%)')),
        ('AMOUNT', _('Flat Amount (रू)')),
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

    # Date Trackers (Allows Historical Imports from 2080 B.S.)
    bill_date = models.DateField(
        default=timezone.now,
        db_index=True,
        verbose_name=_("Bill / Challan Date (AD)"),
        help_text=_("Gregorian date for indexing. Allows manual historical import dates from 2080 B.S.")
    )
    bill_date_bs = models.CharField(
        max_length=15,
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Bill Date (BS)"),
        help_text=_("Bikram Sambat formatted date string (YYYY-MM-DD).")
    )
    fiscal_year = models.CharField(
        max_length=10,
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Fiscal Year (BS)"),
        help_text=_("Nepali Fiscal Year derived from BS date (e.g. 2080/81, 2081/82).")
    )
    
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
    
    # Financial Base & Discount Controls
    gross_amount = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Gross Pre-VAT Amount (NPR)"),
        help_text=_("Sum of all item line pre-VAT merchandise values before discounts.")
    )
    bill_discount_type = models.CharField(
        max_length=15, choices=DISCOUNT_TYPE_CHOICES, default='NONE',
        verbose_name=_("Bill Discount Mode")
    )
    bill_discount_input_value = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Bill Discount Input Value"),
        help_text=_("Value typed by user: flat rupee discount or percentage discount on entire bill.")
    )
    bill_discount_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Bill Discount Amount (NPR)"),
        help_text=_("Cash rupee deduction calculated from bill discount input.")
    )
    total_line_discount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Total Line Discounts (NPR)"),
        help_text=_("Sum of individual item line rupee deductions.")
    )
    discount_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Consolidated Total Discount (NPR)"),
        help_text=_("Total Line Discounts + Whole Bill Discount.")
    )
    taxable_amount = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Pre-VAT Taxable Base (NPR)"),
        help_text=_("Net pre-VAT merchandise value (Gross Lines - Total Discounts).")
    )
    
    # Nepal 13% VAT Module
    is_vat_bill = models.BooleanField(
        default=False,
        verbose_name=_("13% VAT Inward Tax Bill"),
        help_text=_("Enable if supplier issued an official VAT invoice. Calculates 13% on Taxable Pre-VAT Base.")
    )
    vat_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('13.00'),
        verbose_name=_("VAT Rate (%)")
    )
    vat_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Dedicated 13% Input VAT (NPR)"),
        help_text=_("13% VAT on Pre-VAT Taxable Base when VAT toggle is ON; Rs. 0.00 when OFF.")
    )
    
    # Overhead Expenses (Distributed to Landed Cost)
    extra_freight_charge = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Freight / Courier Charge (NPR)")
    )
    customs_import_charge = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Customs / Duty / Tax (NPR)")
    )
    other_handling_charge = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Insurance / Handling Cost (NPR)")
    )
    total_landed_cost = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Total Landed Cost Valuation / COGS (NPR)"),
        help_text=_("Pre-VAT Taxable Merchandise Base + Total Freight, Customs & Handling Overheads.")
    )
    
    # Supplier Settlement & Due Tracking
    net_total_amount = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Supplier Invoice Total (NPR)"),
        help_text=_("Net invoice payable to supplier = Pre-VAT Taxable Base + 13% VAT Amount.")
    )
    paid_amount = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Paid Amount on Receipt (NPR)"),
        help_text=_("Cash, cheque, or digital amount paid on delivery.")
    )
    due_amount = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Net Due / Supplier Debt (NPR)"),
        help_text=_("Invoice Total - Paid Amount. Automatically posted to supplier credit ledger.")
    )
    
    warranty_provider = models.CharField(
        max_length=150, blank=True, null=True, default="Official National Distributor",
        verbose_name=_("Supplier Warranty Provider")
    )
    warranty_months = models.PositiveIntegerField(default=12, verbose_name=_("Warranty Period (Months)"))
    authorized_service_center = models.CharField(
        max_length=200, blank=True, null=True, verbose_name=_("Authorized Service Center Address/Contact")
    )
    
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
        ordering = ['-bill_date', '-created_at']
        verbose_name = _('Goods Received Note (GRN)')
        verbose_name_plural = _('Goods Received Notes (GRN)')
        indexes = [
            models.Index(fields=['bill_date', 'status', 'branch'], name='idx_grn_date_status_branch'),
            models.Index(fields=['fiscal_year', 'branch'], name='idx_grn_fy_branch'),
            models.Index(fields=['supplier', 'bill_date'], name='idx_grn_supplier_date'),
            models.Index(fields=['supplier_bill_no'], name='idx_grn_supp_bill'),
            models.Index(fields=['grn_number'], name='idx_grn_num'),
        ]

    def __str__(self):
        return f"{self.grn_number} | {self.supplier.company_name} | Rs. {self.net_total_amount}"

    def save(self, *args, **kwargs):
        # Auto-synchronize BS date, AD date, and Nepali Fiscal Year for historical integrity
        if self.bill_date:
            ad_date = self.bill_date.date() if isinstance(self.bill_date, datetime) else self.bill_date
            if not self.bill_date_bs or not self.fiscal_year:
                try:
                    bs_year, bs_month, bs_day = NepaliCalendar.ad_to_bs(ad_date)
                    if not self.bill_date_bs:
                        self.bill_date_bs = NepaliCalendar.format_bs(bs_year, bs_month, bs_day, lang='en')
                    if not self.fiscal_year:
                        self.fiscal_year = NepaliCalendar.get_fiscal_year(bs_year, bs_month)
                except Exception:
                    pass
        elif self.bill_date_bs:
            try:
                from apps.core.utils.nepali_date_converter import bs_to_ad_date
                self.bill_date = bs_to_ad_date(self.bill_date_bs)
                parts = [int(p) for p in re.findall(r'\d+', str(self.bill_date_bs))]
                if len(parts) >= 2 and not self.fiscal_year:
                    self.fiscal_year = NepaliCalendar.get_fiscal_year(parts[0], parts[1])
            except Exception:
                pass

        super().save(*args, **kwargs)

    @property
    def bill_date_ad(self) -> date:
        """Alias returning bill_date for consistent naming with SalesEstimate."""
        return self.bill_date

    @property
    def overhead_total(self) -> Decimal:
        """Total landed shipping, customs duties, and handling overheads."""
        return (
            (self.extra_freight_charge or Decimal('0.00')) +
            (self.customs_import_charge or Decimal('0.00')) +
            (self.other_handling_charge or Decimal('0.00'))
        )

    def recalculate_financials(self, save=True):
        """
        Calculates all procurement financials, VAT, and distributes overheads to line items.
        Strict Rules:
        1. gross_amount = Sum of line item Pre-VAT gross values.
        2. total_line_discount = Sum of line item discount deductions.
        3. bill_discount_amount = Bill-level discount computed on merchandise after line discounts.
        4. taxable_amount = gross_amount - total discounts (Pre-VAT Base).
        5. vat_amount = 13% of taxable_amount when is_vat_bill is True; else 0.00.
        6. overhead_total distributed proportionally by line pre-vat net value to compute unit_landed_cost.
        7. net_total_amount = taxable_amount + vat_amount.
        8. due_amount = net_total_amount - paid_amount.
        """
        items = list(self.items.all())
        sum_line_gross = Decimal('0.00')
        sum_line_discount = Decimal('0.00')

        # Step 1: Calculate Line Gross, Line Discounts, and Line Totals
        for item in items:
            qty = item.purchased_quantity if (item.purchased_quantity and item.purchased_quantity > Decimal('0.000')) else Decimal('1.000')
            rate = item.purchase_rate or Decimal('0.00')
            line_gross = (qty * rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            item.gross_amount = line_gross

            if item.discount_type == 'PERCENTAGE':
                pct = item.discount_input_value or Decimal('0.00')
                disc = (line_gross * (pct / Decimal('100.00'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                item.item_discount_amount = min(disc, line_gross)
                item.discount_percent = pct.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            elif item.discount_type == 'AMOUNT':
                amt = (item.discount_input_value or Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                item.item_discount_amount = min(amt, line_gross)
                if line_gross > Decimal('0.00'):
                    item.discount_percent = ((item.item_discount_amount / line_gross) * Decimal('100.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                else:
                    item.discount_percent = Decimal('0.00')
            else:
                item.discount_type = 'NONE'
                item.discount_input_value = Decimal('0.00')
                item.item_discount_amount = Decimal('0.00')
                item.discount_percent = Decimal('0.00')

            item.line_total = line_gross - item.item_discount_amount
            sum_line_gross += line_gross
            sum_line_discount += item.item_discount_amount

        net_lines = max(Decimal('0.00'), sum_line_gross - sum_line_discount)

        # Step 2: Bill-Level Discount Calculation
        if self.bill_discount_type == 'PERCENTAGE':
            pct = self.bill_discount_input_value or Decimal('0.00')
            b_disc = (net_lines * (pct / Decimal('100.00'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            self.bill_discount_amount = min(b_disc, net_lines)
        elif self.bill_discount_type == 'AMOUNT':
            amt = (self.bill_discount_input_value or Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            self.bill_discount_amount = min(amt, net_lines)
        else:
            self.bill_discount_type = 'NONE'
            self.bill_discount_input_value = Decimal('0.00')
            self.bill_discount_amount = Decimal('0.00')

        # Step 3: Base Totals and Pre-VAT Taxable Base
        self.gross_amount = sum_line_gross
        self.total_line_discount = sum_line_discount
        self.discount_amount = sum_line_discount + self.bill_discount_amount
        self.taxable_amount = max(Decimal('0.00'), sum_line_gross - self.discount_amount)

        # Step 4: VAT Calculation (13% on Taxable Base)
        if self.is_vat_bill:
            rate = self.vat_rate if (self.vat_rate and self.vat_rate > Decimal('0.00')) else Decimal('13.00')
            self.vat_rate = rate
            self.vat_amount = (self.taxable_amount * (rate / Decimal('100.00'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        else:
            self.vat_amount = Decimal('0.00')

        # Step 5: Overheads & Landed COGS Valuation
        overheads = self.overhead_total
        self.total_landed_cost = (self.taxable_amount + overheads).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        # Step 6: Net Invoice Payable & Supplier Debt Due
        self.net_total_amount = (self.taxable_amount + self.vat_amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        paid = self.paid_amount or Decimal('0.00')
        self.due_amount = max(Decimal('0.00'), self.net_total_amount - paid)

        # Step 7: Proportional Landed Cost Distribution to Line Items
        for item in items:
            weight = (item.line_total / net_lines) if net_lines > Decimal('0.00') else (
                Decimal('1.00') / Decimal(len(items)) if items else Decimal('0.00')
            )
            line_bill_disc = (self.bill_discount_amount * weight).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            line_overhead = (overheads * weight).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            # Net landed cost for this line item (Merchandise Net - Bill Disc Share + Overhead Share)
            line_landed_total = item.line_total - line_bill_disc + line_overhead

            factor = item.conversion_factor if (item.conversion_factor and item.conversion_factor > Decimal('0.000')) else Decimal('1.000')
            base_qty = (item.purchased_quantity * factor).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)
            item.base_unit_quantity = base_qty

            if base_qty > Decimal('0.000'):
                item.unit_landed_cost = (line_landed_total / base_qty).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            else:
                item.unit_landed_cost = Decimal('0.00')

            item.save(update_fields=[
                'gross_amount', 'discount_type', 'discount_input_value', 'item_discount_amount',
                'discount_percent', 'line_total', 'unit_landed_cost', 'base_unit_quantity'
            ])

        if save and self.pk:
            super(GoodsReceivedNote, self).save(update_fields=[
                'gross_amount', 'bill_discount_type', 'bill_discount_input_value',
                'bill_discount_amount', 'total_line_discount', 'discount_amount',
                'taxable_amount', 'vat_rate', 'vat_amount', 'total_landed_cost',
                'net_total_amount', 'paid_amount', 'due_amount', 'updated_at'
            ])

class GRNItem(TimeStampedModel):
    """
    Line item in GRN with package unit conversion, dual-mode discounts (Amount vs. Percentage),
    proportional unit landed cost, target selling price, and MDMS/serial barcode tracking.
    """
    DISCOUNT_TYPE_CHOICES = [
        ('NONE', _('No Discount')),
        ('PERCENTAGE', _('Percentage (%)')),
        ('AMOUNT', _('Flat Amount (रू)')),
    ]

    grn = models.ForeignKey(GoodsReceivedNote, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='grn_items')
    
    supplier_item_code = models.CharField(
        max_length=100, blank=True, null=True, verbose_name=_("Supplier Item SKU / Code")
    )
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
    
    # Pre-VAT Purchase Rate & Discounts
    purchase_rate = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Unit Purchase Rate (Pre-VAT NPR)")
    )
    gross_amount = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Line Gross Pre-VAT (NPR)"),
        help_text=_("Pre-discount gross value = Quantity * Purchase Rate")
    )
    discount_type = models.CharField(
        max_length=15, choices=DISCOUNT_TYPE_CHOICES, default='NONE',
        verbose_name=_("Discount Type")
    )
    discount_input_value = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Discount Input"),
        help_text=_("Store user input: either percentage (e.g. 5) or cash amount (e.g. 2000).")
    )
    item_discount_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Discount Amount (NPR)"),
        help_text=_("Exact cash rupee deduction applied to this line.")
    )
    discount_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Effective Discount (%)"),
        help_text=_("Calculated percentage equivalent for audit and analytics.")
    )
    line_total = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00'), null=True, blank=True,
        verbose_name=_("Pre-VAT Net Total (NPR)"),
        help_text=_("Line Gross - Discount Amount.")
    )

    is_vat_applicable = models.BooleanField(
        default=True, verbose_name=_("Is VAT Applicable"),
        help_text=_("Uncheck if this specific product is zero-rated or tax-exempt under Nepal Tax Schedule.")
    )
    vat_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('13.00'), verbose_name=_("Tax / VAT Rate (%)")
    )
    
    unit_landed_cost = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Unit Landed Cost (NPR)"),
        help_text=_("Final Pre-VAT unit cost including allocated freight, customs, and bill discounts.")
    )
    new_selling_price = models.DecimalField(
        max_digits=12, decimal_places=2, blank=True, null=True,
        verbose_name=_("New Target Selling Price / MRP (NPR)"),
        help_text=_("Updates master product counter rate upon verification")
    )
    
    default_mdms_status = models.CharField(
        max_length=30,
        choices=Product.MDMS_STATUS_CHOICES if hasattr(Product, 'MDMS_STATUS_CHOICES') else [
            ('REGISTERED_OFFICIAL', 'Official NTA MDMS Registered'),
            ('GREY_MARKET', 'Grey / Unregistered Handset'),
            ('EXEMPT', 'Exempted Non-cellular Device'),
        ],
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
        indexes = [
            models.Index(fields=['grn', 'product'], name='idx_grn_item_grn_prod'),
        ]

    def __str__(self):
        return f"{self.product.name} ({self.base_unit_quantity} {self.product.base_unit.code})"

    def save(self, *args, **kwargs):
        factor = self.conversion_factor if (self.conversion_factor and self.conversion_factor > Decimal('0.000')) else Decimal('1.000')
        qty = self.purchased_quantity if (self.purchased_quantity and self.purchased_quantity > Decimal('0.000')) else Decimal('1.000')
        self.base_unit_quantity = (qty * factor).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)

        rate = self.purchase_rate or Decimal('0.00')
        self.gross_amount = (qty * rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        if self.discount_type == 'PERCENTAGE':
            pct = self.discount_input_value or Decimal('0.00')
            disc = (self.gross_amount * (pct / Decimal('100.00'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            self.item_discount_amount = min(disc, self.gross_amount)
            self.discount_percent = pct.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        elif self.discount_type == 'AMOUNT':
            amt = (self.discount_input_value or Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            self.item_discount_amount = min(amt, self.gross_amount)
            if self.gross_amount > Decimal('0.00'):
                self.discount_percent = ((self.item_discount_amount / self.gross_amount) * Decimal('100.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            else:
                self.discount_percent = Decimal('0.00')
        else:
            self.discount_type = 'NONE'
            self.discount_input_value = Decimal('0.00')
            self.item_discount_amount = Decimal('0.00')
            self.discount_percent = Decimal('0.00')

        self.line_total = self.gross_amount - self.item_discount_amount

        if not self.unit_landed_cost or self.unit_landed_cost <= Decimal('0.00'):
            if self.base_unit_quantity > Decimal('0.000'):
                self.unit_landed_cost = (self.line_total / self.base_unit_quantity).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            else:
                self.unit_landed_cost = Decimal('0.00')

        super().save(*args, **kwargs)

# =============================================================================
# COMMERCIAL PURCHASE RETURN / DEBIT NOTE
# =============================================================================
class PurchaseReturn(TimeStampedModel):
    """
    Commercial Purchase Return / Debit Note Module.
    Tracks outward return of merchandise (smartphones, accessories, parts) back to suppliers/distributors.
    Supports deduction from Supplier Udhaari / Ledger Balance or Cash/Bank Refund.
    """
    STATUS_CHOICES = [
        ('DRAFT', 'Draft / In-Preparation (मस्यौदा)'),
        ('CONFIRMED', 'Confirmed & Stock Deducted (स्वीकृत / मौज्दात कट्टी)'),
        ('CANCELLED', 'Cancelled / Voided (रद्द गरिएको)'),
    ]

    REFUND_MODE_CHOICES = [
        ('DEDUCT_FROM_BALANCE', 'Deduct from Supplier Balance (उधारो कट्टी / हिसाब मिलान)'),
        ('CASH_REFUND', 'Cash / Bank Refund Received (नगद / बैंक फिर्ता)'),
        ('REPLACEMENT', 'Replacement Consignment Expected (सामान साट्ने)'),
    ]

    return_number = models.CharField(
        max_length=50, unique=True, db_index=True, verbose_name=_("Debit Note / Return No.")
    )
    supplier = models.ForeignKey(
        Supplier, on_delete=models.PROTECT, related_name='purchase_returns', verbose_name=_("Supplier / Distributor")
    )
    branch = models.ForeignKey(
        Branch, on_delete=models.PROTECT, related_name='purchase_returns', verbose_name=_("Branch / Outlet")
    )
    original_grn = models.ForeignKey(
        GoodsReceivedNote, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='purchase_returns', verbose_name=_("Original GRN Reference")
    )
    original_bill_reference = models.CharField(
        max_length=100, blank=True, null=True, verbose_name=_("Supplier Invoice / Challan Ref No.")
    )

    # Date Trackers (Allows Historical Imports from 2080 B.S.)
    return_date = models.DateField(
        default=timezone.now,
        db_index=True,
        verbose_name=_("Return Date (AD)"),
        help_text=_("Gregorian date for database indexing. Allows manual historical import dates.")
    )
    return_date_bs = models.CharField(
        max_length=15,
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Return Date (BS)"),
        help_text=_("Bikram Sambat formatted date string (YYYY-MM-DD).")
    )
    fiscal_year = models.CharField(
        max_length=10,
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Fiscal Year (BS)"),
        help_text=_("Nepali Fiscal Year derived from BS date (e.g. 2080/81, 2081/82).")
    )
    
    refund_mode = models.CharField(
        max_length=30, choices=REFUND_MODE_CHOICES, default='DEDUCT_FROM_BALANCE',
        verbose_name=_("Refund / Settlement Mode")
    )
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='CONFIRMED', db_index=True,
        verbose_name=_("Voucher Status")
    )

    total_return_amount = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Total Return Value (NPR)")
    )
    tax_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Tax / VAT Amount (NPR)")
    )
    net_refund_amount = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Net Refund / Debit Note Amount (NPR)")
    )

    processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='processed_purchase_returns', verbose_name=_("Processed By")
    )
    remarks = models.TextField(blank=True, null=True, verbose_name=_("Reason / Remarks for Return"))

    class Meta:
        db_table = 'pur_purchase_returns'
        ordering = ['-return_date', '-created_at']
        verbose_name = _('Purchase Return / Debit Note')
        verbose_name_plural = _('Purchase Returns / Debit Notes')
        indexes = [
            models.Index(fields=['return_date', 'branch'], name='idx_pret_date_branch'),
            models.Index(fields=['fiscal_year', 'branch'], name='idx_pret_fy_branch'),
            models.Index(fields=['return_number'], name='idx_pret_num'),
            models.Index(fields=['supplier', 'return_date'], name='idx_pret_supp_date'),
        ]

    def __str__(self):
        return f"{self.return_number} | {self.supplier.company_name} | Rs. {self.net_refund_amount}"

    def save(self, *args, **kwargs):
        if self.return_date:
            ad_date = self.return_date.date() if isinstance(self.return_date, datetime) else self.return_date
            if not self.return_date_bs or not self.fiscal_year:
                try:
                    bs_year, bs_month, bs_day = NepaliCalendar.ad_to_bs(ad_date)
                    if not self.return_date_bs:
                        self.return_date_bs = NepaliCalendar.format_bs(bs_year, bs_month, bs_day, lang='en')
                    if not self.fiscal_year:
                        self.fiscal_year = NepaliCalendar.get_fiscal_year(bs_year, bs_month)
                except Exception:
                    pass
        elif self.return_date_bs:
            try:
                from apps.core.utils.nepali_date_converter import bs_to_ad_date
                self.return_date = bs_to_ad_date(self.return_date_bs)
                parts = [int(p) for p in re.findall(r'\d+', str(self.return_date_bs))]
                if len(parts) >= 2 and not self.fiscal_year:
                    self.fiscal_year = NepaliCalendar.get_fiscal_year(parts[0], parts[1])
            except Exception:
                pass

        super().save(*args, **kwargs)

class PurchaseReturnItem(TimeStampedModel):
    purchase_return = models.ForeignKey(
        PurchaseReturn, on_delete=models.CASCADE, related_name='items',
        verbose_name=_("Purchase Return / Debit Note")
    )
    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, related_name='purchase_return_items',
        verbose_name=_("Product / Handset Model")
    )
    unit_conversion = models.ForeignKey(
        UnitConversion, on_delete=models.SET_NULL, null=True, blank=True,
        verbose_name=_("Packaging Unit")
    )
    returned_quantity = models.DecimalField(
        max_digits=10, decimal_places=3, default=Decimal('1.000'),
        verbose_name=_("Returned Quantity")
    )
    conversion_factor = models.DecimalField(
        max_digits=10, decimal_places=3, default=Decimal('1.000'),
        verbose_name=_("Conversion Factor")
    )
    base_unit_quantity = models.DecimalField(
        max_digits=12, decimal_places=3, default=Decimal('1.000'),
        verbose_name=_("Base Unit Quantity Deducted")
    )
    purchase_rate = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Unit Purchase Rate (NPR)")
    )
    tax_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Tax / VAT Rate (%)")
    )
    tax_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Tax / VAT Amount (NPR)")
    )
    line_total = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Line Total (NPR)")
    )
    returned_imei_list = models.TextField(
        blank=True, null=True,
        verbose_name=_("Returned IMEIs / Serials"),
        help_text=_("Comma or newline separated 15-digit IMEIs returned to vendor")
    )
    item_instance = models.ForeignKey(
        ItemInstance, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='purchase_return_records',
        verbose_name=_("Linked Serialized Unit")
    )
    return_reason = models.CharField(
        max_length=255, blank=True, null=True,
        verbose_name=_("Specific Defect / Reason for Line"),
        help_text=_("e.g. Factory Defect, Damaged Box, Dead on Arrival, Wrong Model Received")
    )

    class Meta:
        db_table = 'pur_purchase_return_items'
        verbose_name = _('Purchase Return Item')
        verbose_name_plural = _('Purchase Return Items')
        indexes = [
            models.Index(fields=['purchase_return', 'product'], name='idx_pret_item_ret_prod'),
        ]

    def __str__(self):
        return f"{self.product.name} x {self.returned_quantity} (Rs. {self.line_total})"

    def save(self, *args, **kwargs):
        factor = self.conversion_factor if (self.conversion_factor and self.conversion_factor > Decimal('0.000')) else Decimal('1.000')
        qty = self.returned_quantity if (self.returned_quantity and self.returned_quantity > Decimal('0.000')) else Decimal('1.000')
        self.base_unit_quantity = (qty * factor).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)
        gross = qty * (self.purchase_rate or Decimal('0.00'))
        if self.tax_rate and self.tax_rate > Decimal('0.00'):
            self.tax_amount = (gross * (self.tax_rate / Decimal('100.00'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        else:
            self.tax_amount = Decimal('0.00')
        self.line_total = gross + self.tax_amount
        super().save(*args, **kwargs)

# =============================================================================
# SUPPLIER UDHAARI (ACCOUNTS PAYABLE) LEDGER
# =============================================================================
class SupplierUdhaariLedger(TimeStampedModel):
    """
    Double-entry credit ledger tracking every invoice debt, debit return, and payout repayment.
    """
    TRANSACTION_TYPES = [
        ('OPENING_BALANCE', 'Opening Balance (सुरुवाती मौज्दात)'),
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
        ('OTHER', 'Other Adjustment / Opening Balance'),
    ]

    supplier = models.ForeignKey(Supplier, on_delete=models.CASCADE, related_name='ledger_entries')
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, related_name='supplier_ledgers')
    transaction_type = models.CharField(max_length=25, choices=TRANSACTION_TYPES, db_index=True)
    
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    previous_balance = models.DecimalField(max_digits=14, decimal_places=2)
    resulting_balance = models.DecimalField(max_digits=14, decimal_places=2)
    
    payment_mode = models.CharField(max_length=30, choices=PAYMENT_MODES, blank=True, null=True)
    reference_number = models.CharField(max_length=100, blank=True, null=True, help_text="GRN No, Cheque No, Bank Ref, Debit Note No")
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
        indexes = [
            models.Index(fields=['supplier', 'transaction_type'], name='idx_sup_ledg_sup_type'),
            models.Index(fields=['supplier', '-created_at'], name='idx_sup_ledg_sup_created'),
            models.Index(fields=['branch', '-created_at'], name='idx_sup_ledg_br_created'),
            models.Index(fields=['reference_number'], name='idx_sup_ledg_ref_no'),
        ]

    def __str__(self):
        return f"{self.supplier.company_name} - {self.transaction_type} Rs. {self.amount}"