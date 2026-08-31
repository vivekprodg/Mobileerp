from decimal import Decimal
from django.db import models
from django.conf import settings
from django.utils.translation import gettext_lazy as _
from apps.core.models import TimeStampedModel
from apps.branches.models import Branch
from apps.inventory.models import Product

class ScheduledReportLog(TimeStampedModel):
    """
    Logs automated periodic business summaries exported or dispatched via SMS/Email.
    """
    REPORT_TYPES = [
        ('DAILY_SALES', 'Daily Sales & Collection Summary'),
        ('STOCK_VALUATION', 'Inventory Stock Valuation'),
        ('CUSTOMER_UDHAARI', 'Outstanding Credit (Udhaari) Report'),
        ('SUPPLIER_PAYABLES', 'Supplier Payables Ledger'),
        ('DEAD_STOCK', 'Dead / Slow-Moving Inventory'),
        ('PROFIT_LOSS', 'Gross Profit & Margin Realization'),
    ]

    report_type = models.CharField(max_length=30, choices=REPORT_TYPES, db_index=True)
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, blank=True)
    generated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    export_format = models.CharField(max_length=10, default='CSV')
    file_attachment = models.FileField(upload_to='reports/%Y/%m/', blank=True, null=True)
    recipient_email = models.EmailField(blank=True, null=True)
    summary_metrics = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = 'rep_scheduled_logs'
        ordering = ['-created_at']
        verbose_name = _('Report Export Log')
        verbose_name_plural = _('Report Export Logs')

    def __str__(self):
        return f"{self.get_report_type_display()} on {self.created_at.strftime('%Y-%m-%d')}"

class InventoryValuationSnapshot(TimeStampedModel):
    """
    Point-in-time financial audit lock of warehouse valuation.
    Calculates asset value based on exact historical batch & IMEI acquisition costs.
    """
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name='valuation_snapshots')
    snapshot_date = models.DateField(db_index=True, verbose_name=_("Snapshot Date (AD)"))
    snapshot_date_bs = models.CharField(max_length=15, blank=True, null=True, verbose_name=_("Snapshot Date (BS)"))
    total_units_count = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal('0.000'))
    total_cost_valuation = models.DecimalField(
        max_digits=16, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Total Cost Valuation (NPR - Exact Inward Landed Costs)")
    )
    total_retail_valuation = models.DecimalField(
        max_digits=16, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Total Retail Valuation (NPR - Active Counter MRP)")
    )
    projected_margin = models.DecimalField(
        max_digits=16, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Projected Realizable Margin (NPR)")
    )
    generated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    notes = models.TextField(blank=True, null=True)

    class Meta:
        db_table = 'rep_inventory_valuation_snapshots'
        ordering = ['-snapshot_date', '-created_at']
        verbose_name = _('Inventory Valuation Snapshot')
        verbose_name_plural = _('Inventory Valuation Snapshots')

    def __str__(self):
        return f"{self.branch.name} Valuation ({self.snapshot_date}): Cost Rs. {self.total_cost_valuation} | Retail Rs. {self.total_retail_valuation}"

class ProductCostHistory(TimeStampedModel):
    """
    Audit log of price fluctuation events (e.g. Vivo Y28 bought at Rs. 13,333 in Oct,
    Rs. 15,000 in Dec, Rs. 17,000 in May, Rs. 13,000 in July).
    """
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='cost_history')
    date_effective = models.DateField(db_index=True, verbose_name=_("Effective Date"))
    old_cost_price = models.DecimalField(max_digits=12, decimal_places=2)
    new_cost_price = models.DecimalField(max_digits=12, decimal_places=2)
    old_selling_price = models.DecimalField(max_digits=12, decimal_places=2)
    new_selling_price = models.DecimalField(max_digits=12, decimal_places=2)
    source_reference = models.CharField(max_length=100, blank=True, null=True, help_text="GRN No. or Excel Import Ref")
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    remarks = models.CharField(max_length=255, blank=True, null=True)

    class Meta:
        db_table = 'rep_product_cost_history'
        ordering = ['-date_effective', '-created_at']
        verbose_name = _('Product Price Fluctuation History')
        verbose_name_plural = _('Product Price Fluctuation Histories')

    def __str__(self):
        return f"{self.product.name} ({self.date_effective}): Cost Rs. {self.old_cost_price} -> Rs. {self.new_cost_price}"