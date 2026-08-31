import uuid
from decimal import Decimal
from django.db import models
from django.conf import settings
from django.utils.translation import gettext_lazy as _
from apps.core.models import TimeStampedModel
from apps.branches.models import Branch
from apps.customers.models import Customer

class CashDrawerSession(TimeStampedModel):
    """
    Cash Register / Shift Session opened by Cashier at the start of shift
    and closed at day/shift end with physical cash reconciliation.
    """
    STATUS_CHOICES = [
        ('OPEN', 'Open / Active Shift (सक्रिय)'),
        ('CLOSED', 'Closed & Reconciled (बन्द गरिएको)'),
    ]

    session_number = models.CharField(max_length=50, unique=True, db_index=True)
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='drawer_sessions')
    cashier = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='drawer_sessions'
    )
    
    opening_time = models.DateTimeField(auto_now_add=True)
    closing_time = models.DateTimeField(blank=True, null=True)
    
    opening_cash = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Opening Float Cash (सुरुवाती नगद)")
    )
    expected_closing_cash = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Calculated Cash in Drawer")
    )
    actual_closing_cash = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Counted Cash at Shift End (गनेको नगद)")
    )
    cash_discrepancy = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'),
        help_text=_("Discrepancy (Actual - Expected). Negative indicates cash shortage.")
    )
    
    total_sales_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    total_cash_sales = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    total_digital_sales = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    total_credit_sales = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    total_returns_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='OPEN', db_index=True)
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='verified_drawer_sessions'
    )
    remarks = models.TextField(blank=True, null=True)

    class Meta:
        db_table = 'pos_cash_drawer_sessions'
        ordering = ['-created_at']
        verbose_name = _('Cash Drawer Session')
        verbose_name_plural = _('Cash Drawer Sessions')

    def __str__(self):
        return f"{self.session_number} - {self.cashier.username} @ {self.branch.code} [{self.status}]"

class POSHoldCart(TimeStampedModel):
    """
    Suspended / On-Hold Cart allowing cashiers to park an active customer cart
    and serve another customer without losing items or scanned IMEIs.
    """
    hold_reference = models.CharField(max_length=50, unique=True, db_index=True)
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name='held_carts')
    cashier = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    customer = models.ForeignKey(Customer, on_delete=models.SET_NULL, null=True, blank=True)
    customer_name = models.CharField(max_length=150, blank=True, null=True)
    customer_phone = models.CharField(max_length=25, blank=True, null=True)
    
    cart_payload = models.JSONField(
        default=dict,
        help_text=_("JSON snapshot of cart line items, package units, and prices")
    )
    subtotal = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.00'))
    notes = models.CharField(max_length=255, blank=True, null=True)

    class Meta:
        db_table = 'pos_held_carts'
        ordering = ['-created_at']
        verbose_name = _('Held POS Cart')
        verbose_name_plural = _('Held POS Carts')

    def __str__(self):
        return f"{self.hold_reference} ({self.customer_name or 'Walk-in'}) - Rs. {self.subtotal}"