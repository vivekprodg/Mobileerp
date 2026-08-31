from decimal import Decimal
from django.db import models
from django.conf import settings
from django.utils.translation import gettext_lazy as _
from apps.core.models import TimeStampedModel
from apps.branches.models import Branch

class TaxPeriodSummary(TimeStampedModel):
    """
    Monthly VAT / Estimation Summary record corresponding to Nepali Bikram Sambat months.
    """
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name='tax_summaries')
    nepali_year = models.PositiveIntegerField(default=2081, verbose_name=_("BS Year (e.g. 2081)"))
    nepali_month = models.PositiveIntegerField(
        choices=[
            (1, 'Baishakh (वैशाख)'), (2, 'Jestha (जेठ)'), (3, 'Ashadh (असार)'),
            (4, 'Shrawan (साउन)'), (5, 'Bhadra (भदौ)'), (6, 'Ashwin (असोज)'),
            (7, 'Kartik (कार्तिक)'), (8, 'Mangsir (मंसिर)'), (9, 'Poush (पुस)'),
            (10, 'Magh (माघ)'), (11, 'Falgun (फागुन)'), (12, 'Chaitra (चैत)')
        ],
        verbose_name=_("BS Month")
    )
    
    # Sales Estimation Register (Output VAT)
    total_sales_taxable = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    total_sales_non_taxable = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    total_sales_vat = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    
    # Purchase Register (Input VAT)
    total_purchase_taxable = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    total_purchase_non_taxable = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    total_purchase_vat = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    
    # Net Assessment
    net_vat_payable = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    is_estimation_report = models.BooleanField(
        default=True,
        help_text=_("Mandatory disclaimer flag signifying proforma non-tax status")
    )
    is_locked = models.BooleanField(default=False, verbose_name=_("Lock Period"))
    closed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        db_table = 'tax_period_summaries'
        unique_together = ('branch', 'nepali_year', 'nepali_month')
        ordering = ['-nepali_year', '-nepali_month']
        verbose_name = _('Tax Period Summary')
        verbose_name_plural = _('Tax Period Summaries')

    def __str__(self):
        return f"{self.branch.name} - {self.get_nepali_month_display()} {self.nepali_year} (Net VAT: Rs. {self.net_vat_payable})"