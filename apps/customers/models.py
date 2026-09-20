"""
Customer Management & Credit (Udhaari) Ledger Models.

Key Capabilities:
1. Smart B2B / PAN Customer Resolution:
   - resolve_or_create_by_pan(): Automatically looks up existing customers by 9-digit PAN.
     If not found, creates a B2B / Wholesale customer profile with verified PAN.
   - get_or_create_default_cash_customer(): Automatically routes walk-in retail sales (blank PAN or '-')
     to the standardized Cash Customer profile.
2. Thread-Safe Sub-Ledger Accounting:
   - recalculate_balance_from_ledger(): Mathematically computes current_credit_balance
     strictly from CustomerUdhaariLedger entries.
"""

import re
import uuid
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Tuple
from django.db import models
from django.conf import settings
from django.utils.translation import gettext_lazy as _
from apps.core.models import TimeStampedModel
from apps.branches.models import Branch


class Customer(TimeStampedModel):
    """
    Customer Profile Entity supporting Retail Walk-Ins, VIP Loyalty,
    and Institutional B2B / Wholesale Buyers with Inland Revenue Department (IRD) PAN tracking.
    """
    CUSTOMER_TYPE_CHOICES = [
        ('RETAIL', _('Retail Customer (व्यक्तिगत)')),
        ('WHOLESALE', _('Wholesale / B2B Buyer (थोक बिक्रेता)')),
        ('VIP', _('VIP / Regular Customer')),
    ]

    name = models.CharField(
        max_length=200, db_index=True, verbose_name=_("Customer / Business Name")
    )
    phone_number = models.CharField(
        max_length=25, unique=True, db_index=True, verbose_name=_("Mobile / Phone Number")
    )
    alt_phone_number = models.CharField(
        max_length=25, blank=True, null=True, verbose_name=_("Alternate Phone")
    )
    email = models.EmailField(blank=True, null=True, verbose_name=_("Email Address"))
    address = models.CharField(
        max_length=255, blank=True, null=True, verbose_name=_("Address / City")
    )

    pan_number = models.CharField(
        max_length=15, blank=True, null=True, db_index=True,
        verbose_name=_("Permanent Account Number (PAN)"),
        help_text=_("9-Digit IRD PAN for Institutional / Wholesale Buyers")
    )
    customer_type = models.CharField(
        max_length=20, choices=CUSTOMER_TYPE_CHOICES, default='RETAIL', db_index=True,
        verbose_name=_("Customer Category")
    )

    # Financials & Udhaari (Credit) Tracking
    credit_limit = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Credit Limit (NPR)"),
        help_text=_("Maximum allowed credit / udhaari balance (0.00 for cash-only)")
    )
    current_credit_balance = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Outstanding Udhaari Balance (NPR)"),
        help_text=_("Calculated strictly from customer credit sub-ledger entries.")
    )
    total_spent = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Cumulative Lifetime Spend (NPR)"),
        help_text=_("Total invoice purchases spent across all store visits.")
    )

    # Loyalty & Dates
    date_of_birth_bs = models.CharField(
        max_length=15, blank=True, null=True, verbose_name=_("DOB (BS: YYYY-MM-DD)")
    )
    date_of_birth_ad = models.DateField(
        blank=True, null=True, verbose_name=_("DOB (AD)")
    )
    preferred_branch = models.ForeignKey(
        Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name='customers',
        verbose_name=_("Preferred Store Branch")
    )
    notes = models.TextField(blank=True, null=True, verbose_name=_("Internal Notes / Terms"))

    class Meta:
        db_table = 'customer_records'
        ordering = ['name']
        verbose_name = _('Customer Profile')
        verbose_name_plural = _('Customer Profiles')
        indexes = [
            models.Index(fields=['pan_number'], name='idx_cust_pan'),
            models.Index(fields=['customer_type', 'is_active'], name='idx_cust_type_active'),
            models.Index(fields=['current_credit_balance'], name='idx_cust_credit_bal'),
        ]

    def __str__(self):
        pan_tag = f" [PAN: {self.pan_number}]" if self.pan_number else ""
        return f"{self.name} ({self.phone_number}){pan_tag}"

    def clean(self):
        super().clean()
        if self.pan_number:
            clean_pan = re.sub(r'\D', '', str(self.pan_number).strip())
            self.pan_number = clean_pan if clean_pan else None
        else:
            self.pan_number = None

    def save(self, *args, **kwargs):
        if self.pan_number:
            clean_pan = re.sub(r'\D', '', str(self.pan_number).strip())
            self.pan_number = clean_pan if clean_pan else None
        else:
            self.pan_number = None
        super().save(*args, **kwargs)

    @property
    def has_outstanding_credit(self) -> bool:
        return (self.current_credit_balance or Decimal('0.00')) > Decimal('0.00')

    @property
    def is_b2b(self) -> bool:
        return bool(self.pan_number and len(self.pan_number) == 9)

    def recalculate_balance_from_ledger(self, save: bool = False) -> Decimal:
        """
        Recalculates the exact outstanding credit debt strictly from CustomerUdhaariLedger entries:
        - DEBIT: Increases debt (Credit Purchases / Udhaari taken)
        - CREDIT: Decreases debt (Cash / Digital Repayments received)
        - ADJUSTMENT: Decreases debt (Discounts allowed or balance adjustments)

        If save=True, persists the verified balance to current_credit_balance.
        """
        totals = self.credit_ledger_entries.aggregate(
            total_debit=models.Sum('amount', filter=models.Q(entry_type='DEBIT')),
            total_credit=models.Sum('amount', filter=models.Q(entry_type='CREDIT')),
            total_adjustment=models.Sum('amount', filter=models.Q(entry_type='ADJUSTMENT'))
        )
        debits = totals['total_debit'] or Decimal('0.00')
        credits = totals['total_credit'] or Decimal('0.00')
        adjustments = totals['total_adjustment'] or Decimal('0.00')

        calculated_balance = (debits - credits - adjustments).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        if save:
            self.current_credit_balance = calculated_balance
            self.save(update_fields=['current_credit_balance', 'updated_at'])

        return calculated_balance

    # =========================================================================
    # SMART MATCHING & RESOLUTION CLASSMETHODS (FOR MIGRATION & POS BILLING)
    # =========================================================================

    @classmethod
    def get_or_create_default_cash_customer(cls, branch: Optional[Branch] = None) -> 'Customer':
        """
        Retrieves or initializes the standard default Cash Customer profile for walk-in retail sales.
        """
        customer = cls.objects.filter(
            models.Q(name__iexact="Cash Customer (खुदरा ग्राहक)") |
            models.Q(name__iexact="Cash Customer") |
            models.Q(phone_number="9800000000")
        ).first()

        if not customer:
            customer = cls.objects.create(
                name="Cash Customer (खुदरा ग्राहक)",
                phone_number="9800000000",
                customer_type='RETAIL',
                address="Kathmandu, Nepal",
                credit_limit=Decimal('0.00'),
                current_credit_balance=Decimal('0.00'),
                preferred_branch=branch,
                notes="Standard System Profile for Walk-in Retail Cash Sales"
            )

        return customer

    @classmethod
    def resolve_or_create_by_pan(
        cls,
        name: str,
        pan: Optional[str] = None,
        phone: Optional[str] = None,
        branch: Optional[Branch] = None
    ) -> Tuple['Customer', bool]:
        """
        Smart Customer Identification & Auto-Creation Engine:
        1. If PAN is valid 9 digits:
           - Searches for an existing Customer by PAN.
           - If found, returns (customer, False).
           - If not found, creates a new B2B / Wholesale Customer profile with verified PAN.
        2. If PAN is blank, empty, or a hyphen ('-'):
           - Searches for an existing customer by exact name (if not a generic cash term).
           - If not found or name is generic, returns (default_cash_customer, False).
        Returns: Tuple[Customer instance, created_boolean]
        """
        clean_name = str(name or '').strip()
        raw_pan = str(pan or '').strip()
        clean_pan = re.sub(r'\D', '', raw_pan)

        # 1. B2B / Institutional Client with 9-digit PAN
        if len(clean_pan) == 9:
            existing = cls.objects.filter(pan_number=clean_pan).first()
            if existing:
                if clean_name and existing.name.startswith("B2B Client (PAN"):
                    existing.name = clean_name
                    existing.save(update_fields=['name', 'updated_at'])
                return existing, False

            fallback_phone = str(phone).strip() if phone else f"98{clean_pan}"
            while cls.objects.filter(phone_number=fallback_phone).exists():
                fallback_phone = f"98{clean_pan[:6]}{re.sub(r'[^0-9]', '', str(uuid.uuid4().int))[:3]}"

            new_b2b_customer = cls.objects.create(
                name=clean_name or f"B2B Client (PAN {clean_pan})",
                phone_number=fallback_phone,
                pan_number=clean_pan,
                customer_type='WHOLESALE',
                address="Kathmandu, Nepal",
                credit_limit=Decimal('100000.00'),
                current_credit_balance=Decimal('0.00'),
                preferred_branch=branch,
                notes="Auto-created from Mobilesoft historical IRD VAT sales register."
            )
            return new_b2b_customer, True

        # 2. Match by exact Name if provided and not generic
        generic_terms = ['cash customer', 'walk-in', 'walk in', 'mobile soft a', 'mobilesoft a', 'खुदरा ग्राहक', '']
        if clean_name and clean_name.lower() not in generic_terms:
            existing_by_name = cls.objects.filter(name__iexact=clean_name).first()
            if existing_by_name:
                return existing_by_name, False

        # 3. Fallback to Standard Cash Customer Profile
        return cls.get_or_create_default_cash_customer(branch), False


class CustomerUdhaariLedger(TimeStampedModel):
    """
    Sub-ledger tracking every debit (credit purchase / Udhaari) and credit (repayment)
    for customer accounts with complete audit trail.
    """
    ENTRY_TYPES = [
        ('DEBIT', _('Credit Purchase / Udhaari Taken (+) (उधारो सामान)')),
        ('CREDIT', _('Payment Received / Settlement (-) (रकम भुक्तानी)')),
        ('ADJUSTMENT', _('Discount / Balance Adjustment (-) (हिसाब मिलान)')),
    ]

    PAYMENT_MODES = [
        ('CASH', _('Cash (नगद)')),
        ('ESEWA', _('eSewa (ईसेवा)')),
        ('KHALTI', _('Khalti (खल्ती)')),
        ('FONEPAY', _('FonePay QR (फोनपे)')),
        ('BANK', _('Bank Transfer / ConnectIPS (बैंक)')),
        ('OTHER', _('Other / Store Credit Adjustment')),
    ]

    customer = models.ForeignKey(
        Customer, on_delete=models.CASCADE, related_name='credit_ledger_entries',
        verbose_name=_("Customer Profile")
    )
    branch = models.ForeignKey(
        Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name='customer_ledgers',
        verbose_name=_("Store Branch")
    )
    entry_type = models.CharField(
        max_length=20, choices=ENTRY_TYPES, db_index=True, verbose_name=_("Entry Type")
    )
    amount = models.DecimalField(
        max_digits=12, decimal_places=2, verbose_name=_("Transaction Amount (NPR)")
    )
    previous_balance = models.DecimalField(
        max_digits=12, decimal_places=2, verbose_name=_("Balance Prior to Entry")
    )
    resulting_balance = models.DecimalField(
        max_digits=12, decimal_places=2, verbose_name=_("Resulting Outstanding Balance")
    )
    reference_invoice = models.CharField(
        max_length=50, blank=True, null=True, db_index=True,
        verbose_name=_("Reference Invoice / Slip No.")
    )
    payment_mode = models.CharField(
        max_length=30, choices=PAYMENT_MODES, default='CASH', blank=True, null=True,
        verbose_name=_("Payment Channel")
    )
    remarks = models.TextField(blank=True, null=True, verbose_name=_("Ledger Remarks"))
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='recorded_customer_ledgers', verbose_name=_("Recorded By Cashier")
    )

    class Meta:
        db_table = 'customer_udhaari_ledger'
        ordering = ['-created_at']
        verbose_name = _('Customer Udhaari Ledger Entry')
        verbose_name_plural = _('Customer Udhaari Ledger Entries')
        indexes = [
            models.Index(fields=['customer', 'created_at'], name='idx_cledger_cust_date'),
            models.Index(fields=['reference_invoice'], name='idx_cledger_ref_inv'),
            models.Index(fields=['entry_type', 'payment_mode'], name='idx_cledger_type_mode'),
        ]

    def __str__(self):
        return f"{self.customer.name} - {self.get_entry_type_display()} Rs. {self.amount} ({self.created_at.strftime('%Y-%m-%d')})"