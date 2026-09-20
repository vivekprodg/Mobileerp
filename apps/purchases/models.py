"""
Procurement, Supplier Udhaari, Goods Received Notes (GRN) & Commercial Purchase Return Models.

Key Capabilities:
1. Historical Integrity (2080 B.S. & Onwards):
   - Goods Received Notes (GRN), Purchase Orders, and Purchase Returns allow manual setting
     of historical purchase dates during data imports.
   - Automatically synchronizes Gregorian AD dates, Bikram Sambat (BS) date strings, and
     Nepali Fiscal Year identifiers (e.g., '2080/81', '2081/82') inside `.save()`.
   - Supports self-healing two-way date parsing: if only BS date is supplied during migration,
     converts and fills the AD date, and vice versa.
2. Value-Based Overhead Allocation & Landed Cost Tracking:
   - Tracks freight, customs duty, and insurance overheads to arrive at exact unit landed costs.
3. Strict Serialized & Dual-IMEI Tracking:
   - Full alignment with multi-SIM smartphone tracking and NTA MDMS compliance certification.
4. Supplier Ledger Reconciliation:
   - Complete tracking of accounts payable, credit limits, and payment settlement histories.
   - Strict `recalculate_balance_from_ledger()` computes `current_balance` directly from ledger entries.
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
        help_text=_("Positive balance indicates amount shop owes the supplier. Auto-calculated from ledger.")
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
        Ensures mathematical integrity between supplier sub-ledger records and the master balance.
        Positive balance indicates amount owed to the supplier (Payable).
        Negative balance indicates advance paid to supplier (Credit / Advance).
        """
        if not self.pk:
            return self.current_balance or Decimal('0.00')

        # Check if an explicit OPENING_BALANCE ledger entry exists
        opening_entry = self.ledger_entries.filter(
            transaction_type='OPENING_BALANCE'
        ).order_by('created_at', 'id').first()

        if opening_entry:
            base_balance = opening_entry.resulting_balance
        else:
            # Fallback for historical/legacy records without an explicit opening ledger row
            if self.balance_type == 'PAYABLE':
                base_balance = self.opening_balance or Decimal('0.00')
            else:
                base_balance = -(self.opening_balance or Decimal('0.00'))

        # Aggregate bills (purchases increase payable)
        bills_total = self.ledger_entries.filter(
            transaction_type='PURCHASE_BILL'
        ).aggregate(total=models.Sum('amount'))['total'] or Decimal('0.00')

        # Aggregate payments (payouts reduce payable)
        payments_total = self.ledger_entries.filter(
            transaction_type='PAYMENT'
        ).aggregate(total=models.Sum('amount'))['total'] or Decimal('0.00')

        # Aggregate returns (debit notes reduce payable)
        returns_total = self.ledger_entries.filter(
            transaction_type='PURCHASE_RETURN'
        ).aggregate(total=models.Sum('amount'))['total'] or Decimal('0.00')

        # Aggregate adjustments: calculate delta from resulting_balance vs previous_balance if available
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

    # Date Trackers (Allows Historical Imports from 2080 B.S.)
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
        ]

    def __str__(self):
        return f"{self.po_number} - {self.supplier.company_name} ({self.status})"

    def save(self, *args, **kwargs):
        # Auto-synchronize BS date and Nepali Fiscal Year for historical integrity
        if self.order_date:
            if isinstance(self.order_date, datetime):
                ad_date = self.order_date.date()
            else:
                ad_date = self.order_date

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

    def __str__(self):
        return f"{self.product.name} ({self.ordered_quantity} {self.unit.code})"


class GoodsReceivedNote(TimeStampedModel):
    """
    Goods Received Note (GRN) Inward Procurement Voucher.
    Tracks supplier invoice numbers, proportional landed cost calculations,
    NTA MDMS certification, supplier warranty centers, and historical purchase dates.
    """
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

    # Date Trackers (Allows Historical Imports from 2080 B.S.)
    bill_date = models.DateField(
        default=timezone.now,
        db_index=True,
        verbose_name=_("Bill / Challan Date (AD)"),
        help_text=_("Gregorian date for database indexing and accounting. Allows manual historical import dates from 2080 B.S.")
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
            if isinstance(self.bill_date, datetime):
                ad_date = self.bill_date.date()
            else:
                ad_date = self.bill_date

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
        ]

    def __str__(self):
        return f"{self.return_number} | {self.supplier.company_name} | Rs. {self.net_refund_amount}"

    def save(self, *args, **kwargs):
        # Auto-synchronize BS date, AD date, and Nepali Fiscal Year for historical integrity
        if self.return_date:
            if isinstance(self.return_date, datetime):
                ad_date = self.return_date.date()
            else:
                ad_date = self.return_date

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
    """
    Line item for goods returned to a vendor in a PurchaseReturn voucher.
    Links returned product, quantity, unit rate, and scanned IMEIs.
    """
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

    def __str__(self):
        return f"{self.product.name} x {self.returned_quantity} (Rs. {self.line_total})"

    def save(self, *args, **kwargs):
        factor = self.conversion_factor if self.conversion_factor and self.conversion_factor > Decimal('0.000') else Decimal('1.000')
        qty = self.returned_quantity if self.returned_quantity and self.returned_quantity > Decimal('0.000') else Decimal('1.000')
        self.base_unit_quantity = qty * factor
        gross = qty * (self.purchase_rate or Decimal('0.00'))
        if self.tax_rate and self.tax_rate > Decimal('0.00'):
            self.tax_amount = (gross * (self.tax_rate / Decimal('100.00'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        else:
            self.tax_amount = Decimal('0.00')
        self.line_total = gross + self.tax_amount
        super().save(*args, **kwargs)


class SupplierUdhaariLedger(TimeStampedModel):
    """
    Double-entry credit ledger tracking every invoice debt and payout repayment with suppliers.
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

    def __str__(self):
        return f"{self.supplier.company_name} - {self.transaction_type} Rs. {self.amount}"