from decimal import Decimal, ROUND_HALF_UP
from django.db import models
from django.conf import settings
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedModel
from apps.branches.models import Branch
from apps.core.nepali_calendar import NepaliCalendar


class TaxPeriodSummary(TimeStampedModel):
    """
    Monthly VAT / Estimation Summary record corresponding to Nepali Bikram Sambat months.
    Directly tagged with official Nepali Fiscal Year (आर्थिक वर्ष, e.g. '2080/81', '2081/82')
    and Gregorian AD start/end boundary dates for accurate accounting and audit reconciliation.
    """

    MONTH_CHOICES = [
        (1, 'Baishakh (वैशाख)'),
        (2, 'Jestha (जेठ)'),
        (3, 'Ashadh (असार)'),
        (4, 'Shrawan (साउन)'),
        (5, 'Bhadra (भदौ)'),
        (6, 'Ashwin (असोज)'),
        (7, 'Kartik (कार्तिक)'),
        (8, 'Mangsir (मंसिर)'),
        (9, 'Poush (पुस)'),
        (10, 'Magh (माघ)'),
        (11, 'Falgun (फागुन)'),
        (12, 'Chaitra (चैत)')
    ]

    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        related_name='tax_summaries',
        verbose_name=_("Branch / Store Outlet")
    )
    nepali_year = models.PositiveIntegerField(
        default=2081,
        db_index=True,
        verbose_name=_("BS Year (e.g. 2080, 2081)")
    )
    nepali_month = models.PositiveIntegerField(
        choices=MONTH_CHOICES,
        db_index=True,
        verbose_name=_("BS Month")
    )
    fiscal_year = models.CharField(
        max_length=15,
        db_index=True,
        blank=True,
        verbose_name=_("Nepali Fiscal Year (आर्थिक वर्ष)"),
        help_text=_("Official Nepali Fiscal Year (e.g. '2080/81', '2081/82') derived from Shrawan-Ashadh cycle.")
    )

    # Gregorian Boundary Dates for Query Alignment
    period_start_ad = models.DateField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name=_("Period Start Date (AD)")
    )
    period_end_ad = models.DateField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name=_("Period End Date (AD)")
    )

    # Sales Estimation Register (Output VAT / बिक्री खाता)
    total_sales_taxable = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name=_("Total Taxable Sales (NPR)")
    )
    total_sales_non_taxable = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name=_("Total Non-Taxable / Exempt Sales (NPR)")
    )
    total_sales_vat = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name=_("Output VAT Collected (NPR)")
    )

    # Purchase Register (Input VAT / खरिद खाता)
    total_purchase_taxable = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name=_("Total Taxable Purchases (NPR)")
    )
    total_purchase_non_taxable = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name=_("Total Non-Taxable / Exempt Purchases (NPR)")
    )
    total_purchase_vat = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name=_("Input VAT Paid (NPR)")
    )

    # Net Assessment & Period Status
    net_vat_payable = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name=_("Net VAT Payable / (Refundable) (NPR)"),
        help_text=_("Calculated as Output VAT - Input VAT.")
    )
    is_estimation_report = models.BooleanField(
        default=True,
        verbose_name=_("Proforma / Estimation Notice Active"),
        help_text=_("Mandatory disclaimer flag signifying proforma non-tax status.")
    )
    is_locked = models.BooleanField(
        default=False,
        db_index=True,
        verbose_name=_("Period Locked / Reconciled")
    )
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='closed_tax_periods',
        verbose_name=_("Reconciled / Closed By")
    )

    class Meta:
        db_table = 'tax_period_summaries'
        unique_together = ('branch', 'nepali_year', 'nepali_month')
        ordering = ['-nepali_year', '-nepali_month']
        verbose_name = _('Tax Period Summary')
        verbose_name_plural = _('Tax Period Summaries')
        indexes = [
            models.Index(fields=['fiscal_year', 'branch'], name='idx_tax_fy_branch'),
            models.Index(fields=['nepali_year', 'nepali_month'], name='idx_tax_yr_mo'),
            models.Index(fields=['period_start_ad', 'period_end_ad'], name='idx_tax_period_dates'),
        ]

    def __str__(self):
        month_label = self.get_nepali_month_display()
        fy_label = f" [FY {self.fiscal_year}]" if self.fiscal_year else ""
        return f"{self.branch.code} - {month_label} {self.nepali_year}{fy_label} (Net VAT: Rs. {self.net_vat_payable})"

    def clean(self):
        super().clean()
        if not self.fiscal_year and self.nepali_year and self.nepali_month:
            self.fiscal_year = NepaliCalendar.get_fiscal_year(self.nepali_year, self.nepali_month)

    def save(self, *args, **kwargs):
        # 1. Automatically calculate Nepali Fiscal Year if not provided
        if not self.fiscal_year and self.nepali_year and self.nepali_month:
            self.fiscal_year = NepaliCalendar.get_fiscal_year(self.nepali_year, self.nepali_month)

        # 2. Automatically synchronize Gregorian AD start and end bounds for the BS month
        if not self.period_start_ad or not self.period_end_ad:
            try:
                start_ad, end_ad, _, _ = NepaliCalendar.get_bs_month_range(self.nepali_year, self.nepali_month)
                self.period_start_ad = start_ad
                self.period_end_ad = end_ad
            except Exception:
                pass

        # 3. Automatically compute Net VAT Assessment
        output_vat = self.total_sales_vat or Decimal('0.00')
        input_vat = self.total_purchase_vat or Decimal('0.00')
        self.net_vat_payable = (output_vat - input_vat).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        super().save(*args, **kwargs)

    @property
    def month_name_en(self) -> str:
        if 1 <= self.nepali_month <= 12:
            return NepaliCalendar.NEPALI_MONTH_NAMES_EN[self.nepali_month - 1]
        return ""

    @property
    def month_name_np(self) -> str:
        if 1 <= self.nepali_month <= 12:
            return NepaliCalendar.NEPALI_MONTH_NAMES_NP[self.nepali_month - 1]
        return ""