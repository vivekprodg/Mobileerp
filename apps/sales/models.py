"""
Sales & POS Billing Module Models: Estimations, Line Items, Split Payments,
Itemized Sales Returns, and Pre-Owned Trade-In Exchanges.

Key Capabilities & Forensic Architecture:
1. Persistent Bill Type Architecture:
   - Tracks official VAT Sales Invoices ('SALES') vs Internal Estimation Slips ('ESTIMATE')
     directly in the database (`bill_type`).
2. Status Helper & Security Properties:
   - Provides `can_be_edited`, `can_be_voided`, and `is_cancelled` guards.
   - Enforces the golden rule: CANCELLED, RETURNED, and PARTIALLY_RETURNED bills are
     strictly locked from front-end editing.
3. True Bidirectional Historical Date & Fiscal Year Synchronization:
   - When bill_date_bs is explicitly supplied, converts to Gregorian AD and sets the Nepali
     Fiscal Year (e.g., 2080/81 to 2083/84).
   - Automatically propagates date updates down to linked General Ledger Journal Entries,
     ItemInstance sale records, active component warranties, and customer udhaari debt ledgers.
4. Retail Turnover Accounting Model:
   - grand_total strictly represents the total gross sales turnover of merchandise sold plus applicable taxes.
   - Trade-in buy-back valuation allowance (trade_in_discount_amount) is treated as a tender settlement offset.
5. Canonical Routing & URL Resolution:
   - Implements standard `get_absolute_url()` resolving to the detailed invoice sheet view.
6. Safe IMEI Schema Architecture:
   - imei_number and secondary_imei allow null=True, blank=True at the database level.
"""

import re
import uuid
import logging
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from datetime import date, datetime, timedelta
from typing import Optional, Tuple

from django.db import models
from django.conf import settings
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError

from apps.core.models import TimeStampedModel
from apps.branches.models import Branch
from apps.customers.models import Customer
from apps.inventory.models import Product, UnitConversion, ItemInstance
from apps.core.nepali_calendar import NepaliCalendar
from apps.core.utils.nepali_date_converter import parse_bs_date_components

logger = logging.getLogger(__name__)

# =============================================================================
# CHOICES DEFINITIONS
# =============================================================================
DISCOUNT_TYPE_CHOICES = [
    ('NONE', _('No Discount (छुट छैन)')),
    ('PERCENTAGE', _('Percentage Concession (%)')),
    ('AMOUNT', _('Fixed Amount Concession (नगद रकम छुट)')),
    ('FIXED', _('Fixed Amount [Legacy Alias] (नगद रकम छुट)')),
]

BILL_TYPE_CHOICES = [
    ('SALES', _('Official VAT / Sales Invoice (कर बिजक)')),
    ('ESTIMATE', _('Internal Estimation Slip (अनुमानित पर्चा)')),
]

def sync_nepali_and_ad_dates(
    instance,
    ad_field_name: str,
    bs_field_name: str,
    fy_field_name: str = 'fiscal_year'
) -> None:
    """
    Ensures strict bidirectional synchronization between Gregorian (AD) and Bikram Sambat (BS) date fields:
    1. If an existing instance is being updated and bs_field changed -> converts BS to AD, updates FY.
    2. Else if an existing instance is being updated and ad_field changed -> converts AD to BS, updates FY.
    3. For new instances (or if neither specifically changed):
       - If bs_field is provided (non-empty) -> it takes precedence over any model default on ad_field!
         Converts BS to AD, standardizes BS string to YYYY-MM-DD, and updates FY.
       - Else if ad_field is provided -> converts AD to BS, standardizes BS string to YYYY-MM-DD, and updates FY.
    """
    current_bs = str(getattr(instance, bs_field_name, '') or '').strip()
    current_ad = getattr(instance, ad_field_name, None)
    if isinstance(current_ad, datetime):
        current_ad = current_ad.date()

    # Case A: Check if updating an existing record in the database
    if instance.pk:
        try:
            orig = instance.__class__.objects.filter(pk=instance.pk).values(ad_field_name, bs_field_name).first()
            if orig:
                orig_bs = str(orig.get(bs_field_name) or '').strip()
                orig_ad = orig.get(ad_field_name)
                if isinstance(orig_ad, datetime):
                    orig_ad = orig_ad.date()

                bs_changed = bool(current_bs and current_bs != orig_bs)
                ad_changed = bool(current_ad and current_ad != orig_ad)

                # If BS date was explicitly changed, it takes precedence
                if bs_changed:
                    try:
                        bs_y, bs_m, bs_d = parse_bs_date_components(current_bs)
                        target_ad = NepaliCalendar.bs_to_ad(bs_y, bs_m, bs_d)
                        setattr(instance, ad_field_name, target_ad)
                        setattr(instance, bs_field_name, f"{bs_y:04d}-{bs_m:02d}-{bs_d:02d}")
                        if fy_field_name:
                            setattr(instance, fy_field_name, NepaliCalendar.get_fiscal_year(bs_y, bs_m))
                        return
                    except Exception as e:
                        logger.warning(f"[sync_nepali_and_ad_dates] Could not parse changed BS date '{current_bs}': {e}")

                # If AD date was explicitly changed
                elif ad_changed:
                    try:
                        bs_y, bs_m, bs_d = NepaliCalendar.ad_to_bs(current_ad)
                        setattr(instance, ad_field_name, current_ad)
                        setattr(instance, bs_field_name, f"{bs_y:04d}-{bs_m:02d}-{bs_d:02d}")
                        if fy_field_name:
                            setattr(instance, fy_field_name, NepaliCalendar.get_fiscal_year(bs_y, bs_m))
                        return
                    except Exception as e:
                        logger.warning(f"[sync_nepali_and_ad_dates] Could not convert changed AD date '{current_ad}': {e}")
        except Exception as e:
            logger.debug(f"[sync_nepali_and_ad_dates] Checking original instance failed: {e}")

    # Case B: New instance OR baseline synchronization
    if current_bs:
        try:
            bs_y, bs_m, bs_d = parse_bs_date_components(current_bs)
            target_ad = NepaliCalendar.bs_to_ad(bs_y, bs_m, bs_d)
            setattr(instance, ad_field_name, target_ad)
            setattr(instance, bs_field_name, f"{bs_y:04d}-{bs_m:02d}-{bs_d:02d}")
            if fy_field_name:
                setattr(instance, fy_field_name, NepaliCalendar.get_fiscal_year(bs_y, bs_m))
            return
        except Exception as e:
            logger.warning(f"[sync_nepali_and_ad_dates] Could not parse BS date '{current_bs}': {e}")

    if current_ad:
        try:
            bs_y, bs_m, bs_d = NepaliCalendar.ad_to_bs(current_ad)
            setattr(instance, ad_field_name, current_ad)
            setattr(instance, bs_field_name, f"{bs_y:04d}-{bs_m:02d}-{bs_d:02d}")
            if fy_field_name:
                setattr(instance, fy_field_name, NepaliCalendar.get_fiscal_year(bs_y, bs_m))
        except Exception as e:
            logger.warning(f"[sync_nepali_and_ad_dates] Could not convert AD date '{current_ad}': {e}")

# =============================================================================
# 1. SALES ESTIMATE / INVOICE MODEL
# =============================================================================
class SalesEstimate(TimeStampedModel):
    """
    Sales Estimation Slip / POS Invoice Header.
    Tracks salesperson, customer information, multi-mode split payments,
    dynamic tax calculations, trade-in exchange deductions, customer warranty cards,
    and structured merchandise discounts (Percentage or Fixed Cash Amount).
    """
    STATUS_CHOICES = [
        ('DRAFT', _('Draft / On Hold (होल्ड)')),
        ('COMPLETED', _('Completed / Finalized (सम्पन्न)')),
        ('CANCELLED', _('Cancelled / Voided (रद्द गरिएको)')),
        ('RETURNED', _('Fully Returned (फिर्ता भएको)')),
        ('PARTIALLY_RETURNED', _('Partially Returned (आंशिक फिर्ता)')),
    ]

    PAYMENT_STATUS_CHOICES = [
        ('PAID', _('Fully Paid (पूरा भुक्तानी)')),
        ('PARTIAL', _('Partial Payment (आंशिक)')),
        ('DUE', _('Full Udhaari / Due (उधारो)')),
    ]

    DISCOUNT_TYPE_CHOICES = DISCOUNT_TYPE_CHOICES
    BILL_TYPE_CHOICES = BILL_TYPE_CHOICES

    estimate_number = models.CharField(
        max_length=50, unique=True, db_index=True, verbose_name=_("Estimate Slip No.")
    )
    bill_type = models.CharField(
        max_length=20,
        choices=BILL_TYPE_CHOICES,
        default='SALES',
        db_index=True,
        verbose_name=_("Document / Bill Type"),
        help_text=_("Designates whether this transaction is an official VAT sales invoice or an internal estimation voucher.")
    )
    branch = models.ForeignKey(
        Branch, on_delete=models.PROTECT, related_name='sales_estimates',
        verbose_name=_("Store Branch")
    )
    customer = models.ForeignKey(
        Customer, on_delete=models.SET_NULL, null=True, blank=True, related_name='sales_estimates',
        verbose_name=_("Linked Customer Profile")
    )
    customer_name_manual = models.CharField(
        max_length=200, blank=True, null=True, db_index=True, verbose_name=_("Walk-in Customer Name")
    )
    customer_phone_manual = models.CharField(
        max_length=25, blank=True, null=True, db_index=True, verbose_name=_("Walk-in Phone")
    )
    customer_pan = models.CharField(
        max_length=15, blank=True, null=True, db_index=True, verbose_name=_("Customer PAN (Optional)")
    )

    # Date Trackers
    bill_date_ad = models.DateField(
        default=timezone.now,
        db_index=True,
        verbose_name=_("Bill Date (AD)"),
        help_text=_("Gregorian date for database indexing and accounting. Allows historical import dates.")
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
        help_text=_("Nepali Fiscal Year derived from BS date (e.g. 2080/81, 2081/82, 2082/83, 2083/84).")
    )

    # Financial Breakdown & Subtotals
    subtotal = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Gross Items Subtotal")
    )
    item_discount_total = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Total Line Discounts")
    )

    # Bill-Level Discount Configuration
    bill_discount_type = models.CharField(
        max_length=15,
        choices=DISCOUNT_TYPE_CHOICES,
        default='PERCENTAGE',
        db_index=True,
        verbose_name=_("Bill Discount Type")
    )
    bill_discount_input_value = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name=_("Bill Discount Input Value"),
        help_text=_("Raw input value entered by cashier in UI (e.g. 5.00 for 5%, or 1000.00 for flat Rs. 1,000).")
    )
    bill_discount_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Bill Discount (%)")
    )
    bill_discount_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Bill Discount (NPR)")
    )
    discount_reason = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        verbose_name=_("Commercial Discount Reason / Justification"),
        help_text=_("e.g. Customer Negotiation, Clearance, Damaged Packaging, VIP Loyalty.")
    )
    discount_approved_at = models.DateTimeField(
        blank=True,
        null=True,
        verbose_name=_("Discount Approval Timestamp"),
        help_text=_("Exact timestamp when manager override PIN authorized the discount/price override.")
    )

    # Old Phone Trade-In / Exchange Buy-Back Credit
    has_trade_in_exchange = models.BooleanField(default=False, verbose_name=_("Has Old Phone Trade-In Exchange"))
    trade_in_discount_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Trade-In Buy-Back Valuation Allowance (NPR)"),
        help_text=_("Agreed buy-back valuation of customer's old phone treated as a payment tender offset (barter settlement).")
    )
    trade_in_voucher_reference = models.CharField(
        max_length=50, blank=True, null=True, db_index=True,
        verbose_name=_("Trade-In Voucher No.")
    )

    # Tax Breakdown
    is_vat_applicable = models.BooleanField(default=False, verbose_name=_("Is Tax Applicable"))
    taxable_amount = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Taxable Base Amount")
    )
    non_taxable_amount = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Non-Taxable / Exempt Amount")
    )
    vat_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Total Tax / VAT Amount")
    )

    round_off = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Round Off Offset"))
    grand_total = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00'), db_index=True,
        verbose_name=_("Gross Merchandise Grand Total"),
        help_text=_("Full commercial value of merchandise sold + taxes. Trade-in allowances act as a tender offset against this total.")
    )

    # Cost of Goods Sold (COGS) & Margin Realization
    total_cost_amount = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Total Acquisition Cost (COGS) (NPR)")
    )
    total_gross_profit = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Gross Profit Realized (NPR)")
    )

    # Payments & Collections
    paid_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Paid Amount"))
    due_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Due / Udhaari Amount"))
    change_returned = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Change Returned"))

    status = models.CharField(max_length=25, choices=STATUS_CHOICES, default='COMPLETED', db_index=True)
    payment_status = models.CharField(max_length=20, choices=PAYMENT_STATUS_CHOICES, default='PAID', db_index=True)

    cashier = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='billed_estimates',
        verbose_name=_("Billed Cashier")
    )
    salesperson = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='credited_sales',
        verbose_name=_("Credited Salesperson")
    )
    manager_override_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='approved_discounts',
        verbose_name=_("Authorized Manager Override")
    )
    cancellation_reason = models.TextField(blank=True, null=True)
    notes = models.TextField(blank=True, null=True)

    class Meta:
        db_table = 'pos_sales_estimates'
        ordering = ['-bill_date_ad', '-created_at']
        verbose_name = _('Sales Estimate Slip')
        verbose_name_plural = _('Sales Estimate Slips')
        indexes = [
            models.Index(fields=['bill_date_ad', 'status', 'branch'], name='idx_est_date_status_branch'),
            models.Index(fields=['fiscal_year', 'branch'], name='idx_est_fy_branch'),
            models.Index(fields=['branch', 'payment_status', 'created_at'], name='idx_est_branch_pay_created'),
            models.Index(fields=['customer', 'bill_date_ad'], name='idx_est_cust_date'),
            models.Index(fields=['salesperson', 'bill_date_ad'], name='idx_est_salesperson_date'),
            models.Index(fields=['customer_phone_manual'], name='idx_est_cust_phone_man'),
            models.Index(fields=['customer_name_manual'], name='idx_est_cust_name_man'),
            models.Index(fields=['customer_pan'], name='idx_est_cust_pan'),
            models.Index(fields=['branch', 'customer_phone_manual'], name='idx_est_branch_cust_phone'),
            models.Index(fields=['bill_type', 'status'], name='idx_est_type_status'),
        ]

    def __str__(self):
        return f"{self.estimate_number} - Rs. {self.grand_total} ({self.status})"

    def get_absolute_url(self) -> str:
        """
        Standard Django model canonical URL: resolves directly to the bill details view.
        """
        return reverse('sales:estimate_detail', kwargs={'pk': self.pk})

    def clean(self):
        super().clean()
        if self.bill_discount_type == 'FIXED':
            self.bill_discount_type = 'AMOUNT'

    def save(self, *args, **kwargs):
        if self.bill_discount_type == 'FIXED':
            self.bill_discount_type = 'AMOUNT'

        # Auto-infer bill_type from estimate_number prefix if not explicitly set
        if not self.bill_type:
            if self.estimate_number and self.estimate_number.upper().startswith(('INV', 'TAX')):
                self.bill_type = 'SALES'
            else:
                self.bill_type = 'ESTIMATE'

        # Detect date modification on existing instances for downstream propagation
        date_changed = False
        old_date_ad = None
        if self.pk:
            orig = SalesEstimate.objects.filter(pk=self.pk).values('bill_date_ad', 'bill_date_bs').first()
            if orig:
                old_date_ad = orig.get('bill_date_ad')

        # Robust bidirectional date and fiscal year synchronization
        sync_nepali_and_ad_dates(
            instance=self,
            ad_field_name='bill_date_ad',
            bs_field_name='bill_date_bs',
            fy_field_name='fiscal_year'
        )

        if old_date_ad and self.bill_date_ad and old_date_ad != self.bill_date_ad:
            date_changed = True

        super().save(*args, **kwargs)

        if date_changed:
            self._propagate_date_changes()

    def _propagate_date_changes(self) -> None:
        """
        Propagates updated historical dates across linked records:
        - Accounting General Ledger Journal Entries (dynamically handling model column naming)
        - Serialized ItemInstance sale and warranty records
        - Active DeviceComponentWarranty expiration schedules
        - Customer Udhaari Debt Ledger entries
        """
        # 1. Synchronize Accounting Journal Entries (Dynamically checking field names)
        try:
            from apps.accounting.models import JournalEntry
            je_fields = {f.name for f in JournalEntry._meta.get_fields()}
            je_updates = {}

            if 'entry_date' in je_fields:
                je_updates['entry_date'] = self.bill_date_ad
            elif 'date_ad' in je_fields:
                je_updates['date_ad'] = self.bill_date_ad

            if 'entry_date_bs' in je_fields:
                je_updates['entry_date_bs'] = self.bill_date_bs
            elif 'date_bs' in je_fields:
                je_updates['date_bs'] = self.bill_date_bs

            if 'fiscal_year' in je_fields:
                je_updates['fiscal_year'] = self.fiscal_year

            if 'updated_at' in je_fields:
                je_updates['updated_at'] = timezone.now()

            if je_updates:
                JournalEntry.objects.filter(
                    reference_document=self.estimate_number
                ).update(**je_updates)
        except Exception as e:
            logger.warning(f"Could not propagate date update to JournalEntry for {self.estimate_number}: {e}")

        # 2. Synchronize Sold Item Instances & Component Warranties
        try:
            instances = ItemInstance.objects.filter(sold_invoice_reference=self.estimate_number)
            for inst in instances:
                inst.sale_date = self.bill_date_ad
                inst.warranty_start_date = self.bill_date_ad
                if inst.product.warranty_months and inst.product.warranty_months > 0:
                    inst.warranty_end_date = self.bill_date_ad + timedelta(days=inst.product.warranty_months * 30)
                inst.save(update_fields=['sale_date', 'warranty_start_date', 'warranty_end_date', 'updated_at'])

                # Recalculate component warranties
                from apps.inventory.models import DeviceComponentWarranty
                comp_warranties = DeviceComponentWarranty.objects.filter(item_instance=inst, status='ACTIVE')
                for cw in comp_warranties:
                    days = 365 if cw.warranty_months == 12 else (180 if cw.warranty_months == 6 else (90 if cw.warranty_months == 3 else cw.warranty_months * 30))
                    cw.warranty_start_date = self.bill_date_ad
                    cw.warranty_expiry_date = self.bill_date_ad + timedelta(days=days)
                    cw.save(update_fields=['warranty_start_date', 'warranty_expiry_date', 'updated_at'])
        except Exception as e:
            logger.warning(f"Could not propagate date update to ItemInstances for {self.estimate_number}: {e}")

        # 3. Synchronize Customer Udhaari Ledger
        try:
            from apps.customers.models import CustomerUdhaariLedger
            ledger_fields = {f.name for f in CustomerUdhaariLedger._meta.get_fields()}
            ledger_updates = {}
            if 'entry_date' in ledger_fields:
                ledger_updates['entry_date'] = self.bill_date_ad
            if 'entry_date_bs' in ledger_fields:
                ledger_updates['entry_date_bs'] = self.bill_date_bs
            if ledger_updates:
                CustomerUdhaariLedger.objects.filter(reference_invoice=self.estimate_number).update(**ledger_updates)
        except Exception as e:
            logger.warning(f"Could not propagate date update to CustomerUdhaariLedger for {self.estimate_number}: {e}")

    # =========================================================================
    # STATUS & PERMISSION GUARDS
    # =========================================================================
    @property
    def is_cancelled(self) -> bool:
        """Returns True if the bill is cancelled / voided."""
        return self.status == 'CANCELLED'

    @property
    def is_returned(self) -> bool:
        """Returns True if the bill has had any sales returns processed."""
        return self.status in ['RETURNED', 'PARTIALLY_RETURNED']

    @property
    def can_be_edited(self) -> bool:
        """
        Strict front-end edit lockdown guard:
        CANCELLED, RETURNED, or PARTIALLY_RETURNED bills can NEVER be edited from the counter front-end.
        Only active COMPLETED or DRAFT bills can have their metadata corrected.
        """
        if self.status in ['CANCELLED', 'RETURNED', 'PARTIALLY_RETURNED']:
            return False
        return True

    @property
    def can_be_voided(self) -> bool:
        """
        Void / Cancellation guard:
        Only active COMPLETED or DRAFT bills can be voided.
        Partially returned bills are blocked to prevent phantom inventory duplication.
        """
        if self.status in ['CANCELLED', 'RETURNED', 'PARTIALLY_RETURNED']:
            return False
        return True

    @property
    def is_official_vat_bill(self) -> bool:
        """Returns True if this is an official VAT/Tax invoice."""
        return self.bill_type == 'SALES' or (self.estimate_number and self.estimate_number.startswith('INV-'))

    @property
    def recipient_display_name(self) -> str:
        if self.customer:
            return self.customer.name
        return self.customer_name_manual or "Cash Customer (खुदरा ग्राहक)"

    @property
    def total_sales_discount(self) -> Decimal:
        """
        Returns genuine merchandise concessions (line-item discounts + bill-level discount).
        Excludes trade-in buy-back valuation credits to prevent false discount inflation.
        """
        item_disc = self.item_discount_total if self.item_discount_total is not None else Decimal('0.00')
        bill_disc = self.bill_discount_amount if self.bill_discount_amount is not None else Decimal('0.00')
        return item_disc + bill_disc

    @property
    def price_override_total(self) -> Decimal:
        """
        Aggregates total price reduction concessions where unit_price was overridden below official_unit_price.
        """
        if hasattr(self, '_prefetched_objects_cache') and 'items' in self._prefetched_objects_cache:
            return sum((item.price_override_amount for item in self.items.all()), Decimal('0.00'))
        total = self.items.aggregate(total=models.Sum('price_override_amount'))['total']
        return total if total is not None else Decimal('0.00')

    @property
    def total_commercial_reduction(self) -> Decimal:
        """
        Returns total commercial price reductions:
        Official Catalog Price Overrides + Merchandise Sales Discounts.
        """
        return self.price_override_total + self.total_sales_discount

    @property
    def total_discount_given(self) -> Decimal:
        """Backward-compatibility alias returning total_sales_discount."""
        return self.total_sales_discount

    @property
    def trade_in_credit(self) -> Decimal:
        """Returns the buy-back valuation allowance applied from an old phone trade-in exchange."""
        if self.has_trade_in_exchange and self.trade_in_discount_amount:
            return self.trade_in_discount_amount
        return Decimal('0.00')

    @property
    def effective_trade_in_tender(self) -> Decimal:
        """
        Calculates trade-in buy-back allowance consumed as tender against this invoice.
        Capped strictly at grand_total. Any excess represents surplus credit/cash change.
        """
        if self.has_trade_in_exchange and self.trade_in_discount_amount > Decimal('0.00'):
            return min(self.grand_total, self.trade_in_discount_amount)
        return Decimal('0.00')

    @property
    def excess_trade_in_credit(self) -> Decimal:
        """
        Calculates surplus trade-in buy-back valuation exceeding the invoice grand_total,
        which is refunded as cash change (walk-in) or deposited into customer store credit.
        """
        if self.has_trade_in_exchange and self.trade_in_discount_amount > self.grand_total:
            return (self.trade_in_discount_amount - self.grand_total).quantize(
                Decimal('0.01'), rounding=ROUND_HALF_UP
            )
        return Decimal('0.00')

    @property
    def net_customer_payable(self) -> Decimal:
        """
        Turnover Accounting: Merchandise Grand Total minus Trade-In Barter Tender.
        This represents the net cash/electronic payment or debt required from the customer.
        """
        return max(Decimal('0.00'), self.grand_total - self.effective_trade_in_tender)

# =============================================================================
# 2. SALES ESTIMATE ITEM (LINE ITEMS)
# =============================================================================
class SalesEstimateItem(TimeStampedModel):
    """
    Line item in sales estimate linked to exact sold IMEI, pricing mode, batch, and warranty card.
    """
    DISCOUNT_TYPE_CHOICES = DISCOUNT_TYPE_CHOICES

    estimate = models.ForeignKey(SalesEstimate, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='sales_lines')
    unit_conversion = models.ForeignKey(
        UnitConversion, on_delete=models.SET_NULL, null=True, blank=True
    )

    quantity = models.DecimalField(max_digits=10, decimal_places=3, default=Decimal('1.000'))
    conversion_factor = models.DecimalField(max_digits=10, decimal_places=3, default=Decimal('1.000'))
    base_unit_quantity = models.DecimalField(max_digits=12, decimal_places=3)

    # Selling Price & Historical Catalog Price Audit
    unit_price = models.DecimalField(
        max_digits=12, decimal_places=2, verbose_name=_("Unit Selling Price (NPR)")
    )
    official_unit_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name=_("Official Catalog Price (NPR)"),
        help_text=_("Official catalog price at the moment of billing before any cashier price override.")
    )
    price_override_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name=_("Price Override Concession Amount (NPR)"),
        help_text=_("Total concession: (official_unit_price - unit_price) * quantity.")
    )
    cost_price = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Exact Acquisition Cost Price (NPR)")
    )

    # Dual-Mode Line Discount Structure
    discount_type = models.CharField(
        max_length=15,
        choices=DISCOUNT_TYPE_CHOICES,
        default='NONE',
        db_index=True,
        verbose_name=_("Discount Type")
    )
    discount_input_value = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name=_("Discount Input Value"),
        help_text=_("Exact numeric input typed by cashier (e.g. 5.00 for 5%, or 2500.00 for Rs. 2,500).")
    )
    item_discount_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name=_("Actual Item Discount Amount (NPR)"),
        help_text=_("Actual monetary deduction resulting strictly from the item discount input (isolated from bill discounts).")
    )
    effective_discount_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name=_("Effective Discount (%)"),
        help_text=_("Secondary control percentage calculated against line selling base for authorization, limits, and audit.")
    )
    allocated_bill_discount_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name=_("Allocated Bill Discount (NPR)"),
        help_text=_("Line item's proportionate share of the invoice-level bill discount.")
    )

    # Backwards-compatibility aliases
    discount_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name=_("Legacy Discount (%)"),
        help_text=_("Synchronized with effective_discount_percent for backward compatibility.")
    )
    discount_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name=_("Total Line Discount Concession (NPR)"),
        help_text=_("Total line concession: item_discount_amount + allocated_bill_discount_amount.")
    )

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
    imei_number = models.CharField(max_length=35, blank=True, null=True, db_index=True, verbose_name=_("Sold IMEI 1"))
    secondary_imei = models.CharField(max_length=35, blank=True, null=True, verbose_name=_("Sold IMEI 2"))
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
            models.Index(fields=['discount_type'], name='idx_estitem_disc_type'),
        ]

    def __str__(self):
        return f"{self.product.name} x {self.quantity} = Rs. {self.line_total}"

    def clean(self):
        super().clean()
        if self.discount_type == 'FIXED':
            self.discount_type = 'AMOUNT'

    def save(self, *args, **kwargs):
        if self.discount_type == 'FIXED':
            self.discount_type = 'AMOUNT'

        if (not self.official_unit_price or self.official_unit_price <= Decimal('0.00')) and self.unit_price:
            self.official_unit_price = self.unit_price

        if self.official_unit_price and self.unit_price and self.official_unit_price > self.unit_price:
            qty = self.quantity if self.quantity else Decimal('1.000')
            self.price_override_amount = ((self.official_unit_price - self.unit_price) * qty).quantize(Decimal('0.01'))
        elif not self.price_override_amount:
            self.price_override_amount = Decimal('0.00')

        if self.item_discount_amount is None:
            self.item_discount_amount = Decimal('0.00')
        if self.allocated_bill_discount_amount is None:
            self.allocated_bill_discount_amount = Decimal('0.00')
        if self.effective_discount_percent is None:
            self.effective_discount_percent = self.discount_percent or Decimal('0.00')

        if self.discount_amount is None or self.discount_amount == Decimal('0.00'):
            self.discount_amount = self.item_discount_amount + self.allocated_bill_discount_amount
        if not self.discount_percent and self.effective_discount_percent:
            self.discount_percent = self.effective_discount_percent

        super().save(*args, **kwargs)

    @property
    def line_gross_profit(self) -> Decimal:
        total_cost = self.cost_price * self.base_unit_quantity
        net_revenue = self.base_taxable_amount if (self.is_vat_applicable and self.vat_rate > 0) else (self.line_total - self.tax_amount)
        return net_revenue - total_cost

    @property
    def is_price_overridden(self) -> bool:
        return bool(self.official_unit_price and self.unit_price < self.official_unit_price)

    @property
    def is_amount_discount(self) -> bool:
        return self.discount_type in ['AMOUNT', 'FIXED']

    @property
    def is_percentage_discount(self) -> bool:
        return self.discount_type == 'PERCENTAGE'

    @property
    def unit_discount_amount(self) -> Decimal:
        """Returns the per-unit equivalent of the item discount for Quantity > 1."""
        qty = self.quantity if self.quantity and self.quantity > Decimal('0.000') else Decimal('1.000')
        return (self.item_discount_amount / qty).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

# =============================================================================
# 3. SPLIT PAYMENT TRANSACTIONS
# =============================================================================
class SalesPaymentTransaction(TimeStampedModel):
    """Split payment recording across Cash, Digital Wallets, Cards, and Udhaari."""
    PAYMENT_MODES = [
        ('CASH', _('Cash (नगद)')),
        ('ESEWA', _('eSewa (ईसेवा)')),
        ('KHALTI', _('Khalti (खल्ती)')),
        ('FONEPAY', _('FonePay QR (फोनपे)')),
        ('CARD', _('POS Card Swipe (कार्ड)')),
        ('BANK_TRANSFER', _('Bank Transfer / ConnectIPS')),
        ('CREDIT', _('Udhaari / Account Balance (उधारो)')),
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

# =============================================================================
# 4. PHONE EXCHANGE & TRADE-IN VOUCHERS
# =============================================================================
class PhoneExchangeTradeIn(TimeStampedModel):
    """
    Second-Hand Phone Buy-Back & Trade-In Exchange Order.
    """
    TRADE_IN_STATUS_CHOICES = [
        ('DRAFT', _('1. Inspection In-Progress (जाँच हुँदै)')),
        ('VALUATED', _('2. Valuated / Offer Generated (मूल्याङ्कन तयार)')),
        ('ATTACHED_TO_BILL', _('3. Deducted Against POS Bill (बिलमा समायोजन)')),
        ('RESTOCKED', _('4. Added to Used Inventory (मौज्दातमा दर्ता)')),
        ('CANCELLED', _('5. Cancelled / Customer Rejected (रद्द)')),
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

    intake_date_ad = models.DateField(
        default=timezone.now,
        db_index=True,
        verbose_name=_("Trade-In Date (AD)")
    )
    intake_date_bs = models.CharField(
        max_length=15,
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Trade-In Date (BS)")
    )
    fiscal_year = models.CharField(
        max_length=10,
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Fiscal Year (BS)")
    )

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
        ordering = ['-intake_date_ad', '-created_at']
        verbose_name = _('Phone Exchange / Trade-In Voucher')
        verbose_name_plural = _('Phone Exchange / Trade-In Vouchers')
        indexes = [
            models.Index(fields=['branch', 'status'], name='idx_tradein_branch_status'),
            models.Index(fields=['fiscal_year', 'branch'], name='idx_tradein_fy_branch'),
            models.Index(fields=['imei_1'], name='idx_tradein_imei1'),
            models.Index(fields=['voucher_number'], name='idx_tradein_voucher_no'),
        ]

    def __str__(self):
        return f"{self.voucher_number} - {self.brand_name} {self.model_name} (Rs. {self.final_trade_in_value}) [{self.status}]"

    def save(self, *args, **kwargs):
        sync_nepali_and_ad_dates(
            instance=self,
            ad_field_name='intake_date_ad',
            bs_field_name='intake_date_bs',
            fy_field_name='fiscal_year'
        )
        super().save(*args, **kwargs)

class TradeInInspectionChecklist(TimeStampedModel):
    """
    10-Point Technical Diagnostic Inspection for Old Traded-In Phones.
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
    """
    ID_TYPE_CHOICES = [
        ('CITIZENSHIP', _('Nepali Citizenship Card (नागरिकता प्रमाणपत्र)')),
        ('NATIONAL_ID', _('National Identity Card (राष्ट्रिय परिचयपत्र)')),
        ('DRIVING_LICENSE', _('Smart Driving License (सवारी चालक अनुमतिपत्र)')),
        ('PASSPORT', _('Passport (राहदानी)')),
    ]

    trade_in_voucher = models.OneToOneField(
        PhoneExchangeTradeIn, on_delete=models.CASCADE, related_name='legal_undertaking'
    )

    customer_full_name = models.CharField(max_length=150, verbose_name=_("Customer Full Name (English/Nepali)"))
    customer_father_or_spouse_name = models.CharField(max_length=150, blank=True, null=True, verbose_name=_("Father / Spouse Name"))

    id_type = models.CharField(max_length=30, choices=ID_TYPE_CHOICES, default='CITIZENSHIP')
    id_number = models.CharField(max_length=60, db_index=True, verbose_name=_("Identification / Citizenship No."))
    id_issued_district = models.CharField(max_length=100, default="Kathmandu", verbose_name=_("Issued District"))
    id_issued_date_bs = models.CharField(max_length=20, blank=True, null=True, verbose_name=_("Issued Date (BS)"))

    permanent_address = models.CharField(max_length=255, verbose_name=_("Permanent Address (District, Ward, Municipality)"))
    current_address = models.CharField(max_length=255, blank=True, null=True, verbose_name=_("Current Residence / Room Address"))

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

    declaration_text = models.TextField(
        blank=True,
        default="",
        verbose_name=_("Full Undertaking Text"),
        help_text=_("Statutory declaration text (defaults to system configuration text if left blank).")
    )
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

    def save(self, *args, **kwargs):
        if not self.declaration_text:
            try:
                from apps.core.models import SystemConfiguration
                sys_cfg = SystemConfiguration.get_solo()
                if sys_cfg and getattr(sys_cfg, 'undertaking_declaration_text_np', None):
                    self.declaration_text = sys_cfg.undertaking_declaration_text_np
            except Exception:
                pass
        super().save(*args, **kwargs)

# =============================================================================
# 5. SALES RETURNS (CREDIT NOTES) & RETURN ITEMS
# =============================================================================
class SalesReturn(TimeStampedModel):
    """
    Customer sales return or warranty replacement voucher.
    """
    return_number = models.CharField(max_length=50, unique=True, db_index=True, verbose_name=_("Return Voucher No."))
    original_estimate = models.ForeignKey(
        SalesEstimate, on_delete=models.PROTECT, related_name='returns', verbose_name=_("Original Sales Invoice")
    )
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='sales_returns', verbose_name=_("Store Branch"))
    customer = models.ForeignKey(
        Customer, on_delete=models.SET_NULL, null=True, blank=True, related_name='returns', verbose_name=_("Customer")
    )

    return_date_ad = models.DateField(
        default=timezone.now,
        db_index=True,
        verbose_name=_("Return Date (AD)")
    )
    return_date_bs = models.CharField(
        max_length=15,
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Return Date (BS)")
    )
    fiscal_year = models.CharField(
        max_length=10,
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Fiscal Year (BS)")
    )

    total_refund_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Total Refund Amount"))
    refund_mode = models.CharField(
        max_length=30,
        choices=[
            ('CASH', _('Cash Refund')),
            ('STORE_CREDIT', _('Customer Store Credit')),
            ('EXCHANGE_ADJUST', _('Adjusted in Exchange Bill')),
        ],
        default='CASH',
        verbose_name=_("Refund Mode")
    )
    reason = models.TextField(verbose_name=_("Reason for Return"))
    technician_notes = models.TextField(blank=True, null=True, verbose_name=_("Diagnostic / Inspection Findings"))
    processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='processed_returns', verbose_name=_("Processed By")
    )

    class Meta:
        db_table = 'pos_sales_returns'
        ordering = ['-return_date_ad', '-created_at']
        verbose_name = _('Sales Return')
        verbose_name_plural = _('Sales Returns')
        indexes = [
            models.Index(fields=['return_date_ad', 'branch'], name='idx_ret_date_branch'),
            models.Index(fields=['fiscal_year', 'branch'], name='idx_ret_fy_branch'),
            models.Index(fields=['branch', 'created_at'], name='idx_return_branch_date'),
            models.Index(fields=['return_number'], name='idx_return_num'),
        ]

    def __str__(self):
        return f"{self.return_number} for {self.original_estimate.estimate_number} (Rs. {self.total_refund_amount})"

    def save(self, *args, **kwargs):
        sync_nepali_and_ad_dates(
            instance=self,
            ad_field_name='return_date_ad',
            bs_field_name='return_date_bs',
            fy_field_name='fiscal_year'
        )
        super().save(*args, **kwargs)

class SalesReturnItem(TimeStampedModel):
    """
    Line item within a customer sales return voucher.
    """
    DISCOUNT_TYPE_CHOICES = DISCOUNT_TYPE_CHOICES

    sales_return = models.ForeignKey(SalesReturn, on_delete=models.CASCADE, related_name='items')
    estimate_item = models.ForeignKey(SalesEstimateItem, on_delete=models.PROTECT, verbose_name=_("Original Sales Line"))
    product = models.ForeignKey(Product, on_delete=models.PROTECT, verbose_name=_("Returned Product"))
    return_quantity = models.DecimalField(max_digits=10, decimal_places=3, verbose_name=_("Return Quantity"))
    base_unit_quantity = models.DecimalField(max_digits=12, decimal_places=3, verbose_name=_("Base Unit Quantity"))
    refund_amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name=_("Net Refund Amount"))

    discount_type = models.CharField(
        max_length=15,
        choices=DISCOUNT_TYPE_CHOICES,
        default='NONE',
        db_index=True,
        verbose_name=_("Original Item Discount Type")
    )
    discount_input_value = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name=_("Original Discount Input Value")
    )
    item_discount_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name=_("Original Item Discount Amount (NPR)")
    )
    effective_discount_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name=_("Original Effective Discount (%)")
    )

    returned_imei = models.CharField(max_length=35, blank=True, null=True, verbose_name=_("Returned IMEI"))
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
            models.Index(fields=['discount_type'], name='idx_retitem_disc_type'),
        ]

    def __str__(self):
        return f"{self.product.name} x {self.return_quantity} (Refund: Rs. {self.refund_amount})"

    def save(self, *args, **kwargs):
        if self.discount_type == 'FIXED':
            self.discount_type = 'AMOUNT'
        super().save(*args, **kwargs)