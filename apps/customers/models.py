from decimal import Decimal
from django.db import models
from django.conf import settings
from django.utils.translation import gettext_lazy as _
from apps.core.models import TimeStampedModel
from apps.branches.models import Branch

class Customer(TimeStampedModel):
    CUSTOMER_TYPE_CHOICES = [
        ('RETAIL', 'Retail Customer (व्यक्तिगत)'),
        ('WHOLESALE', 'Wholesale Buyer (थोक बिक्रेता)'),
        ('VIP', 'VIP / Regular Customer'),
    ]

    name = models.CharField(max_length=200, verbose_name=_("Customer / Business Name"), db_index=True)
    phone_number = models.CharField(max_length=25, unique=True, verbose_name=_("Mobile / Phone Number"), db_index=True)
    alt_phone_number = models.CharField(max_length=25, blank=True, null=True, verbose_name=_("Alternate Phone"))
    email = models.EmailField(blank=True, null=True)
    address = models.CharField(max_length=255, blank=True, null=True, verbose_name=_("Address / City"))
    
    pan_number = models.CharField(
        max_length=15, blank=True, null=True,
        verbose_name=_("PAN Number (For Business/Wholesale)")
    )
    customer_type = models.CharField(
        max_length=20, choices=CUSTOMER_TYPE_CHOICES, default='RETAIL', db_index=True
    )
    
    # Financials & Udhaari (Credit) Tracking
    credit_limit = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        help_text=_("Maximum allowed credit / udhaari balance (0.00 for no credit allowed)")
    )
    current_credit_balance = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        help_text=_("Outstanding Udhaari payable to the shop")
    )
    total_spent = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00'),
        help_text=_("Cumulative sales amount spent across all visits")
    )
    
    # Special Dates for Loyalty
    date_of_birth_bs = models.CharField(max_length=15, blank=True, null=True, verbose_name=_("DOB (BS: YYYY-MM-DD)"))
    date_of_birth_ad = models.DateField(blank=True, null=True, verbose_name=_("DOB (AD)"))
    preferred_branch = models.ForeignKey(
        Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name='customers'
    )
    notes = models.TextField(blank=True, null=True)

    class Meta:
        db_table = 'customer_records'
        ordering = ['name']
        verbose_name = _('Customer')
        verbose_name_plural = _('Customers')

    def __str__(self):
        return f"{self.name} ({self.phone_number})"

    @property
    def has_outstanding_credit(self) -> bool:
        return self.current_credit_balance > Decimal('0.00')

class CustomerUdhaariLedger(TimeStampedModel):
    """
    Tracks every debit (credit purchase) and credit (cash/digital repayment)
    for customer credit (Udhaari) accounts.
    """
    ENTRY_TYPES = [
        ('DEBIT', 'Credit Purchase (उधारो सामान)'),
        ('CREDIT', 'Payment Received (रकम भुक्तानी)'),
        ('ADJUSTMENT', 'Discount / Balance Adjustment'),
    ]

    customer = models.ForeignKey(
        Customer, on_delete=models.CASCADE, related_name='credit_ledger_entries'
    )
    branch = models.ForeignKey(
        Branch, on_delete=models.SET_NULL, null=True, related_name='customer_ledgers'
    )
    entry_type = models.CharField(max_length=20, choices=ENTRY_TYPES, db_index=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    previous_balance = models.DecimalField(max_digits=12, decimal_places=2)
    resulting_balance = models.DecimalField(max_digits=12, decimal_places=2)
    reference_invoice = models.CharField(max_length=50, blank=True, null=True, help_text="Estimate/Invoice Slip No.")
    payment_mode = models.CharField(
        max_length=30, blank=True, null=True,
        help_text="Cash, eSewa, Khalti, FonePay, Bank"
    )
    remarks = models.TextField(blank=True, null=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='recorded_customer_ledgers'
    )

    class Meta:
        db_table = 'customer_udhaari_ledger'
        ordering = ['-created_at']
        verbose_name = _('Customer Udhaari Entry')
        verbose_name_plural = _('Customer Udhaari Ledgers')

    def __str__(self):
        return f"{self.customer.name} - {self.entry_type} Rs. {self.amount} on {self.created_at.strftime('%Y-%m-%d')}"