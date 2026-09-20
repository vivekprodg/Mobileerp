"""
Double-Entry General Ledger & Financial Accounting Models.

Key Capabilities:
1. Multi-Year Fiscal Year & Period Locking (Nepal Context):
   - AccountingFiscalYear: Spans Shrawan 1 to Ashadh 31/32 (e.g. 2080/81 to 2083/84).
   - Classmethods lock_year(), unlock_year(), and contextmanager temporary_unlock()
     guarantee safe migration imports and permanent historical audit locks.
   - FinancialPeriod: Granular month-by-month locks for all 12 Bikram Sambat months.
2. General Ledger Chart of Accounts (COA):
   - Hierarchical AccountGroup and Account models with system control tags.
   - Protects transacted and system-reserved accounts from structural tampering.
3. Strict Double-Entry Journal Engine (JournalEntry & JournalItem):
   - Mathematical balance enforcement: Sum(Debits) == Sum(Credits).
   - Provenance tracking via source_module and source_id.
"""

import uuid
from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime
from contextlib import contextmanager
from typing import Optional, List, Tuple
from django.db import models
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedModel
from apps.branches.models import Branch
from apps.customers.models import Customer
from apps.purchases.models import Supplier
from apps.core.nepali_calendar import NepaliCalendar


class AccountGroup(TimeStampedModel):
    """
    Hierarchical Account Grouping Model conforming to Standard Accounting Principles.
    Roots: Asset, Liability, Equity, Revenue, Direct Expense, Indirect Expense.
    """
    NATURE_CHOICES = [
        ('DEBIT', _('Debit Nature (Dr - Assets / Expenses)')),
        ('CREDIT', _('Credit Nature (Cr - Liabilities / Equity / Revenue)')),
    ]

    CATEGORY_CHOICES = [
        ('ASSET', _('Asset (सम्पत्ति)')),
        ('LIABILITY', _('Liability (दायित्व)')),
        ('EQUITY', _('Equity / Capital (पुँजी)')),
        ('REVENUE', _('Operating Revenue / Income (आम्दानी)')),
        ('DIRECT_EXPENSE', _('Direct Expense / Cost of Goods Sold (प्रत्यक्ष खर्च / COGS)')),
        ('INDIRECT_EXPENSE', _('Indirect Operating Expense (अप्रत्यक्ष सञ्चालन खर्च)')),
    ]

    code = models.CharField(
        max_length=20, unique=True, db_index=True,
        verbose_name=_("Group Code (e.g., 1000, 1100)")
    )
    name = models.CharField(
        max_length=150, db_index=True,
        verbose_name=_("Group Name (English)")
    )
    name_np = models.CharField(
        max_length=150, blank=True, null=True,
        verbose_name=_("Group Name (Nepali / देवनागरी)")
    )
    category = models.CharField(
        max_length=20, choices=CATEGORY_CHOICES, db_index=True,
        verbose_name=_("Primary Financial Statement Category")
    )
    parent = models.ForeignKey(
        'self', on_delete=models.CASCADE, null=True, blank=True,
        related_name='sub_groups', verbose_name=_("Parent Account Group")
    )
    nature = models.CharField(
        max_length=10, choices=NATURE_CHOICES, default='DEBIT',
        verbose_name=_("Normal Balance Nature")
    )
    is_system_reserved = models.BooleanField(
        default=False,
        help_text=_("System control groups cannot be deleted by users.")
    )

    class Meta:
        db_table = 'acc_account_groups'
        ordering = ['code']
        verbose_name = _('Account Group')
        verbose_name_plural = _('Account Groups')

    def __str__(self):
        return f"{self.code} - {self.name}"

    def clean(self):
        super().clean()
        if self.parent:
            if self.parent_id == self.pk:
                raise ValidationError(_("An account group cannot be its own parent."))
            if not self.category:
                self.category = self.parent.category
            if not self.nature:
                self.nature = self.parent.nature

        if self.pk:
            orig = AccountGroup.objects.filter(pk=self.pk).first()
            if orig and (orig.category != self.category or orig.nature != self.nature):
                has_posted_entries = JournalItem.objects.filter(
                    account__group=self,
                    journal_entry__status='POSTED'
                ).exists()
                if has_posted_entries:
                    raise ValidationError(
                        _(f"Cannot change category or nature of group '{self.name}'. "
                          "Transactions have already been posted to accounts belonging to this group.")
                    )


class Account(TimeStampedModel):
    """
    General Ledger Account Master (Chart of Accounts).
    """
    code = models.CharField(
        max_length=30, unique=True, db_index=True,
        verbose_name=_("Account Code (e.g. 1010-01, 1130-KTM)")
    )
    name = models.CharField(
        max_length=200, db_index=True,
        verbose_name=_("Account Ledger Name (English)")
    )
    name_np = models.CharField(
        max_length=200, blank=True, null=True,
        verbose_name=_("Account Ledger Name (Nepali / देवनागरी)")
    )
    group = models.ForeignKey(
        AccountGroup, on_delete=models.PROTECT, related_name='accounts',
        verbose_name=_("Account Group")
    )
    branch = models.ForeignKey(
        Branch, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='ledger_accounts',
        verbose_name=_("Branch Scope (Leave blank for Organization-Wide General Accounts)")
    )
    currency = models.CharField(max_length=5, default='NPR', verbose_name=_("Currency"))

    SYSTEM_TAG_CHOICES = [
        ('NONE', _('Standard Custom Ledger')),
        ('CASH', _('Cash in Hand (Counter Float)')),
        ('BANK', _('Bank Account / Checking Account')),
        ('FONEPAY', _('FonePay Merchant Clearing / Dynamic QR')),
        ('ESEWA', _('eSewa Digital Wallet Clearing')),
        ('KHALTI', _('Khalti Digital Wallet Clearing')),
        ('CARD_CLEARING', _('Card POS Merchant Settlement / In-Transit')),
        ('ACCOUNTS_RECEIVABLE', _('Accounts Receivable / Trade Debtors (Customer Control)')),
        ('ACCOUNTS_PAYABLE', _('Accounts Payable / Trade Creditors (Supplier Control)')),
        ('INVENTORY_ASSET', _('Merchandise Inventory Asset (Sellable Stock)')),
        ('DEFECTIVE_INVENTORY_ASSET', _('Quarantined Defective Inventory Asset')),
        ('INVENTORY_SHRINKAGE', _('Stock Shrinkage & Damage Write-Off Expense')),
        ('INVENTORY_SURPLUS', _('Inventory Count Surplus & Gain')),
        ('ACCUMULATED_DEPRECIATION', _('Accumulated Depreciation (Contra-Asset)')),
        ('COGS', _('Cost of Goods Sold (COGS)')),
        ('SALES_REVENUE', _('Sales Revenue')),
        ('SALES_RETURN', _('Sales Returns & Deductions')),
        ('SALES_DISCOUNT', _('Sales Merchandise Discounts Expense')),
        ('OUTPUT_VAT', _('Output VAT / Tax Payable 13%')),
        ('INPUT_VAT', _('Input VAT / Tax Receivable 13%')),
        ('REPAIR_SERVICE_INCOME', _('Repair & Workshop Service Revenue')),
        ('REPAIR_PARTS_COGS', _('Repair Spare Parts Consumed Expense')),
        ('LOAN_PRINCIPAL', _('Bank Loan & Long-Term Borrowings (Liability)')),
        ('INTEREST_EXPENSE', _('Finance & Interest Charges Expense')),
        ('PAYMENT_GATEWAY_FEE', _('Payment Gateway MDR / POS Commission Expense')),
        ('OWNER_CAPITAL', _('Proprietor Capital / Equity')),
        ('OWNER_DRAWINGS', _('Owner Drawings (Contra-Equity)')),
        ('RETAINED_EARNINGS', _('Retained Earnings / Current Year P&L')),
        ('OPENING_BALANCE_EQUITY', _('Opening Balance Equity Offset')),
    ]
    system_tag = models.CharField(
        max_length=35, choices=SYSTEM_TAG_CHOICES, default='NONE', db_index=True,
        verbose_name=_("System Automation Tag")
    )
    is_system_reserved = models.BooleanField(
        default=False,
        help_text=_("Prevents modification/deletion of core operational control accounts.")
    )

    opening_balance = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Opening Balance (NPR)")
    )
    opening_balance_nature = models.CharField(
        max_length=10, choices=[('DEBIT', 'Debit (Dr)'), ('CREDIT', 'Credit (Cr)')],
        default='DEBIT', verbose_name=_("Opening Balance Nature")
    )
    current_balance = models.DecimalField(
        max_digits=16, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Calculated Real-Time Net Balance (NPR)")
    )
    description = models.TextField(blank=True, null=True)

    class Meta:
        db_table = 'acc_accounts'
        ordering = ['code']
        verbose_name = _('General Ledger Account')
        verbose_name_plural = _('General Ledger Accounts')
        indexes = [
            models.Index(fields=['system_tag', 'branch'], name='idx_acc_tag_branch'),
            models.Index(fields=['group', 'branch'], name='idx_acc_group_branch'),
        ]

    def __str__(self):
        branch_tag = f" [{self.branch.code}]" if self.branch else " [HQ]"
        return f"{self.code} - {self.name}{branch_tag}"

    @property
    def is_debit_nature(self) -> bool:
        return self.group.nature == 'DEBIT'

    def clean(self):
        super().clean()
        if self.pk:
            orig = Account.objects.filter(pk=self.pk).first()
            if orig:
                has_posted = self.journal_lines.filter(journal_entry__status='POSTED').exists()
                if has_posted:
                    errors = {}
                    if orig.group_id != self.group_id:
                        errors['group'] = _("Cannot modify the Account Group of an account with posted journal entries.")
                    if orig.system_tag != self.system_tag:
                        errors['system_tag'] = _("Cannot modify the System Automation Tag of an account with posted journal entries.")
                    if orig.branch_id != self.branch_id:
                        errors['branch'] = _("Cannot alter the Branch Scope of an account with posted journal entries.")
                    if errors:
                        raise ValidationError(errors)


class AccountingFiscalYear(TimeStampedModel):
    """
    Nepali Fiscal Year (आर्थिक वर्ष) Master Record.
    Spans from Shrawan 1 to Ashadh 31/32 (e.g., 2080/81, 2081/82, 2082/83, 2083/84).
    Governs closing locks to prevent tampering with closed audited accounts.
    """
    name = models.CharField(
        max_length=20, unique=True, db_index=True,
        verbose_name=_("Fiscal Year Label (e.g., 2080/81, 2081/82, 2082/83, 2083/84)")
    )
    start_date_bs = models.CharField(max_length=15, verbose_name=_("Start Date (BS: YYYY-MM-DD)"))
    end_date_bs = models.CharField(max_length=15, verbose_name=_("End Date (BS: YYYY-MM-DD)"))
    start_date_ad = models.DateField(verbose_name=_("Start Date (AD)"), db_index=True)
    end_date_ad = models.DateField(verbose_name=_("End Date (AD)"), db_index=True)

    is_closed = models.BooleanField(
        default=False, db_index=True,
        verbose_name=_("Is Fiscal Year Closed / Audited"),
        help_text=_("Locked years reject any new journal entries, sales, or financial modifications.")
    )
    closed_at = models.DateTimeField(blank=True, null=True, verbose_name=_("Closed At Timestamp"))
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='closed_fiscal_years', verbose_name=_("Closed By User")
    )

    class Meta:
        db_table = 'acc_fiscal_years'
        ordering = ['-start_date_ad']
        verbose_name = _('Accounting Fiscal Year')
        verbose_name_plural = _('Accounting Fiscal Years')

    def __str__(self):
        closed_tag = " (LOCKED / CLOSED)" if self.is_closed else " (ACTIVE / OPEN)"
        return f"FY {self.name}{closed_tag}"

    def save(self, *args, **kwargs):
        # Automatically populate Gregorian and BS dates from fiscal year label
        if not self.start_date_ad or not self.end_date_ad or not self.start_date_bs or not self.end_date_bs:
            try:
                ad_start, ad_end, bs_start, bs_end = NepaliCalendar.get_fiscal_year_range(self.name)
                self.start_date_ad = ad_start
                self.end_date_ad = ad_end
                self.start_date_bs = bs_start
                self.end_date_bs = bs_end
            except Exception:
                pass
        super().save(*args, **kwargs)

    # =========================================================================
    # FISCAL YEAR MANAGEMENT CLASSMETHODS & CONTEXT MANAGERS
    # =========================================================================

    @classmethod
    def get_or_create_fiscal_year(cls, fy_name: str, is_closed: bool = False) -> 'AccountingFiscalYear':
        """
        Retrieves or initializes a Fiscal Year and all 12 of its monthly FinancialPeriod records.
        """
        ad_start, ad_end, bs_start, bs_end = NepaliCalendar.get_fiscal_year_range(fy_name)
        fy_obj, _ = cls.objects.get_or_create(
            name=fy_name,
            defaults={
                'start_date_ad': ad_start,
                'end_date_ad': ad_end,
                'start_date_bs': bs_start,
                'end_date_bs': bs_end,
                'is_closed': is_closed
            }
        )

        # Ensure all 12 BS monthly periods exist
        base_year = int(fy_name.split('/')[0])
        for month_idx in range(1, 13):
            if month_idx <= 9:
                bs_y = base_year
                bs_m = month_idx + 3
            else:
                bs_y = base_year + 1
                bs_m = month_idx - 9

            s_ad, e_ad, s_bs, e_bs = NepaliCalendar.get_bs_month_range(bs_y, bs_m)
            m_name_en = NepaliCalendar.NEPALI_MONTH_NAMES_EN[bs_m - 1]
            m_name_np = NepaliCalendar.NEPALI_MONTH_NAMES_NP[bs_m - 1]

            FinancialPeriod.objects.get_or_create(
                fiscal_year=fy_obj,
                period_number=month_idx,
                defaults={
                    'period_name_en': f"{m_name_en} ({fy_name})",
                    'period_name_np': f"{m_name_np} ({fy_name})",
                    'start_date_ad': s_ad,
                    'end_date_ad': e_ad,
                    'start_date_bs': s_bs,
                    'end_date_bs': e_bs,
                    'is_closed': is_closed
                }
            )

        return fy_obj

    @classmethod
    def lock_year(cls, fy_name: str, user=None):
        """
        Permanently locks a fiscal year and cascades the lock across its 12 periods.
        """
        fy = cls.objects.filter(name=fy_name).first()
        if fy:
            fy.is_closed = True
            fy.closed_at = timezone.now()
            fy.closed_by = user
            fy.save(update_fields=['is_closed', 'closed_at', 'closed_by', 'updated_at'])
            FinancialPeriod.objects.filter(fiscal_year=fy).update(is_closed=True, updated_at=timezone.now())

    @classmethod
    def unlock_year(cls, fy_name: str):
        """
        Temporarily unlocks a fiscal year and its 12 periods (for migrations/audits).
        """
        fy = cls.objects.filter(name=fy_name).first()
        if fy:
            fy.is_closed = False
            fy.save(update_fields=['is_closed', 'updated_at'])
            FinancialPeriod.objects.filter(fiscal_year=fy).update(is_closed=False, updated_at=timezone.now())

    @classmethod
    @contextmanager
    def temporary_unlock(cls, fy_names: List[str]):
        """
        Context manager ensuring historical years are unlocked for a batch operation
        and guaranteed to be re-locked in a finally block even on unhandled crashes.
        Usage:
            with AccountingFiscalYear.temporary_unlock(['2080/81', '2081/82']):
                # Run migration here safely
        """
        originally_closed = list(
            cls.objects.filter(name__in=fy_names, is_closed=True).values_list('name', flat=True)
        )
        for name in originally_closed:
            cls.unlock_year(name)
        try:
            yield
        finally:
            for name in originally_closed:
                cls.lock_year(name)

    @classmethod
    def ensure_multi_year_setup(cls) -> Tuple[List['AccountingFiscalYear'], 'AccountingFiscalYear']:
        """
        Enforces the full 4-year lifecycle (FY 2080/81 to 2083/84):
        - FY 2080/81, 2081/82, and 2082/83: Initialized and permanently LOCKED.
        - FY 2083/84: Initialized and OPEN for daily counter billing.
        Returns: (locked_years_list, active_2083_year)
        """
        past_years = ['2080/81', '2081/82', '2082/83']
        locked_objs = []
        for y in past_years:
            obj = cls.get_or_create_fiscal_year(y, is_closed=True)
            cls.lock_year(y)
            locked_objs.append(obj)

        active_obj = cls.get_or_create_fiscal_year('2083/84', is_closed=False)
        cls.unlock_year('2083/84')

        return locked_objs, active_obj


class FinancialPeriod(TimeStampedModel):
    """
    Monthly Accounting Period corresponding to the 12 Bikram Sambat calendar months.
    """
    fiscal_year = models.ForeignKey(
        AccountingFiscalYear, on_delete=models.PROTECT, related_name='periods'
    )
    period_number = models.PositiveSmallIntegerField(
        verbose_name=_("Period Month Index (1=Baishakh ... 12=Chaitra)")
    )
    period_name_en = models.CharField(max_length=50)
    period_name_np = models.CharField(max_length=50)
    start_date_ad = models.DateField(db_index=True)
    end_date_ad = models.DateField(db_index=True)
    start_date_bs = models.CharField(max_length=15)
    end_date_bs = models.CharField(max_length=15)
    is_closed = models.BooleanField(default=False, db_index=True)

    class Meta:
        db_table = 'acc_financial_periods'
        unique_together = ('fiscal_year', 'period_number')
        ordering = ['fiscal_year', 'period_number']
        verbose_name = _('Financial Period (Month)')
        verbose_name_plural = _('Financial Periods (Months)')

    def __str__(self):
        return f"{self.period_name_en} ({self.fiscal_year.name})"


class JournalEntry(TimeStampedModel):
    """
    Double-Entry Journal Entry Header (Voucher).
    Maintains rigorous financial audit integrity with strict equality: Sum(Debits) == Sum(Credits).
    """
    VOUCHER_TYPE_CHOICES = [
        ('JOURNAL', _('Journal Voucher (JV) - सामान्य भौचर')),
        ('PAYMENT', _('Payment Voucher (PV) - भुक्तानी भौचर')),
        ('RECEIPT', _('Receipt Voucher (RV) - रसिद भौचर')),
        ('CONTRA', _('Contra Voucher (CV) - बैंक तथा नगद स्थानान्तरण')),
        ('SALES', _('Sales POS Voucher (SV) - बिक्री भौचर')),
        ('PURCHASE', _('Purchase GRN Voucher (PUV) - खरिद भौचर')),
        ('CREDIT_NOTE', _('Credit Note / Sales Return (CN) - बिक्री फिर्ता')),
        ('DEBIT_NOTE', _('Debit Note / Purchase Return (DN) - खरिद फिर्ता')),
    ]

    STATUS_CHOICES = [
        ('DRAFT', _('Draft / In-Preparation (मस्यौदा)')),
        ('POSTED', _('Posted & Settled (प्रविष्टि भएको)')),
        ('CANCELLED', _('Cancelled / Voided (रद्द गरिएको)')),
    ]

    SOURCE_MODULE_CHOICES = [
        ('MANUAL', _('Manual Journal Voucher')),
        ('POS_SALE', _('Point of Sale (POS) Checkout')),
        ('SALES_RETURN', _('Sales Return / Credit Note')),
        ('PURCHASE_GRN', _('Purchase GRN Intake')),
        ('PURCHASE_RETURN', _('Purchase Return / Debit Note')),
        ('CUSTOMER_PAYMENT', _('Customer Debt / Credit Collection')),
        ('SUPPLIER_PAYMENT', _('Supplier Bill Payment')),
        ('EXPENSE_VOUCHER', _('Shop Operating Expense')),
        ('STOCK_ADJUSTMENT', _('Inventory Adjustment (Damage/Loss/Surplus)')),
        ('TRADE_IN', _('Used Device Trade-In / Buy-Back Intake')),
        ('REPAIR_SERVICE', _('Workshop & Repair Billing')),
        ('BANK_CONTRA', _('Bank / Cash Contra Transfer')),
        ('PAYROLL', _('Staff Salary / Payroll Disbursement')),
        ('DEPRECIATION', _('Asset Depreciation Voucher')),
        ('YEAR_END_CLOSING', _('Fiscal Year Closing / Retained Earnings Transfer')),
    ]

    voucher_number = models.CharField(
        max_length=50, unique=True, db_index=True,
        verbose_name=_("Voucher Number")
    )
    voucher_type = models.CharField(
        max_length=20, choices=VOUCHER_TYPE_CHOICES, default='JOURNAL', db_index=True,
        verbose_name=_("Voucher Type")
    )
    branch = models.ForeignKey(
        Branch, on_delete=models.PROTECT, related_name='journal_entries',
        verbose_name=_("Store Outlet / Branch")
    )

    source_module = models.CharField(
        max_length=30, choices=SOURCE_MODULE_CHOICES, default='MANUAL', db_index=True,
        verbose_name=_("Originating Business Module")
    )
    source_id = models.CharField(
        max_length=100, blank=True, null=True, db_index=True,
        verbose_name=_("Source Document Identifier")
    )
    reference_document = models.CharField(
        max_length=100, blank=True, null=True, db_index=True,
        verbose_name=_("External Reference (Cheque No, Bank Ref, Deposit Slip)")
    )
    narration = models.TextField(
        verbose_name=_("Narration / Operational Description")
    )

    entry_date = models.DateField(
        default=timezone.now, db_index=True,
        verbose_name=_("Voucher Date (AD)")
    )
    entry_date_bs = models.CharField(
        max_length=15, blank=True, null=True, db_index=True,
        verbose_name=_("Voucher Date (BS: YYYY-MM-DD)")
    )
    fiscal_year = models.CharField(
        max_length=15, blank=True, null=True, db_index=True,
        verbose_name=_("Nepali Fiscal Year (आर्थिक वर्ष)")
    )

    total_debit = models.DecimalField(
        max_digits=16, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Total Debit (NPR)")
    )
    total_credit = models.DecimalField(
        max_digits=16, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Total Credit (NPR)")
    )

    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='DRAFT', db_index=True
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name='created_journal_entries'
    )
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='posted_journal_entries'
    )
    posted_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        db_table = 'acc_journal_entries'
        ordering = ['-entry_date', '-created_at']
        verbose_name = _('Journal Entry Voucher')
        verbose_name_plural = _('Journal Entry Vouchers')
        indexes = [
            models.Index(fields=['entry_date', 'status', 'branch'], name='idx_je_date_stat_br'),
            models.Index(fields=['fiscal_year', 'branch'], name='idx_je_fy_br'),
            models.Index(fields=['voucher_type', 'entry_date'], name='idx_je_type_date'),
            models.Index(fields=['source_module', 'source_id'], name='idx_je_src_module_id'),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['source_module', 'source_id', 'voucher_type'],
                condition=models.Q(status='POSTED', source_id__isnull=False) & ~models.Q(source_id=''),
                name='unique_posted_source_module_id_voucher'
            )
        ]

    def __str__(self):
        return f"{self.voucher_number} [{self.get_voucher_type_display()}] - Rs. {self.total_debit}"

    def clean(self):
        super().clean()
        # Enforce Closed Period Lockdown
        if self.fiscal_year:
            fy = AccountingFiscalYear.objects.filter(name=self.fiscal_year, is_closed=True).first()
            if fy:
                raise ValidationError(
                    f"Financial posting rejected: Nepali Fiscal Year {self.fiscal_year} is audited and locked."
                )

        if self.status == 'POSTED' and self.total_debit != self.total_credit:
            raise ValidationError(
                _("A posted journal entry must be balanced: Total Debit (%(debit)s) != Total Credit (%(credit)s)."),
                params={'debit': self.total_debit, 'credit': self.total_credit}
            )

    def save(self, *args, **kwargs):
        if self.entry_date:
            ad_date = self.entry_date.date() if isinstance(self.entry_date, datetime) else self.entry_date
            if not self.entry_date_bs or not self.fiscal_year:
                try:
                    bs_year, bs_month, bs_day = NepaliCalendar.ad_to_bs(ad_date)
                    if not self.entry_date_bs:
                        self.entry_date_bs = NepaliCalendar.format_bs(bs_year, bs_month, bs_day, lang='en')
                    if not self.fiscal_year:
                        self.fiscal_year = NepaliCalendar.get_fiscal_year(bs_year, bs_month)
                except Exception:
                    pass

        super().save(*args, **kwargs)


class JournalItem(TimeStampedModel):
    """
    Atomic Double-Entry Line Item.
    Enforces that either debit_amount OR credit_amount > 0, never both on the same line.
    """
    journal_entry = models.ForeignKey(
        JournalEntry, on_delete=models.CASCADE, related_name='items',
        verbose_name=_("Parent Voucher")
    )
    account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name='journal_lines',
        verbose_name=_("General Ledger Account")
    )

    debit_amount = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Debit (Dr) Amount")
    )
    credit_amount = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Credit (Cr) Amount")
    )

    customer = models.ForeignKey(
        Customer, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='gl_journal_items',
        verbose_name=_("Customer Sub-Ledger (Debtor)")
    )
    supplier = models.ForeignKey(
        Supplier, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='gl_journal_items',
        verbose_name=_("Supplier Sub-Ledger (Creditor)")
    )
    line_narration = models.CharField(
        max_length=255, blank=True, null=True,
        verbose_name=_("Specific Line Narration")
    )

    class Meta:
        db_table = 'acc_journal_items'
        ordering = ['id']
        verbose_name = _('Journal Line Item')
        verbose_name_plural = _('Journal Line Items')
        indexes = [
            models.Index(fields=['account', 'created_at'], name='idx_jitem_acc_date'),
            models.Index(fields=['customer', 'account'], name='idx_jitem_cust_acc'),
            models.Index(fields=['supplier', 'account'], name='idx_jitem_supp_acc'),
        ]

    def __str__(self):
        if self.debit_amount > Decimal('0.00'):
            return f"Dr. {self.account.name}: Rs. {self.debit_amount}"
        return f"Cr. {self.account.name}: Rs. {self.credit_amount}"

    def clean(self):
        super().clean()
        dr = self.debit_amount or Decimal('0.00')
        cr = self.credit_amount or Decimal('0.00')

        if dr < Decimal('0.00') or cr < Decimal('0.00'):
            raise ValidationError(_("Negative amounts are prohibited in double-entry line items."))

        if dr == Decimal('0.00') and cr == Decimal('0.00'):
            raise ValidationError(_("A journal line must specify either a Debit or a Credit amount."))

        if dr > Decimal('0.00') and cr > Decimal('0.00'):
            raise ValidationError(_("A single line cannot have both Debit and Credit amounts. Split into two lines."))


class ExpenseVoucher(TimeStampedModel):
    """
    Operating Shop Expense Voucher (Rent, Electricity, Tea, Internet, Stationery).
    """
    PAYMENT_METHOD_CHOICES = [
        ('CASH', _('Cash Counter Float (नगद)')),
        ('BANK_TRANSFER', _('Bank Transfer / ConnectIPS (बैंक ट्रान्सफर)')),
        ('FONEPAY', _('FonePay / Merchant QR (डिजिटल क्यूआर)')),
        ('CHEQUE', _('Account Payee Cheque (चेक)')),
        ('ESEWA_KHALTI', _('eSewa / Khalti Digital Wallet')),
    ]

    voucher_number = models.CharField(
        max_length=50, unique=True, db_index=True,
        verbose_name=_("Expense Voucher No.")
    )
    branch = models.ForeignKey(
        Branch, on_delete=models.PROTECT, related_name='expense_vouchers',
        verbose_name=_("Store Outlet")
    )
    expense_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name='vouched_expenses',
        verbose_name=_("Expense Ledger Account")
    )
    payment_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name='payment_vouchers_issued',
        verbose_name=_("Payment Source Ledger (Cash or Bank)")
    )
    payment_method = models.CharField(
        max_length=25, choices=PAYMENT_METHOD_CHOICES, default='CASH'
    )
    amount = models.DecimalField(
        max_digits=12, decimal_places=2,
        verbose_name=_("Expense Amount (NPR)")
    )

    expense_date = models.DateField(
        default=timezone.now, db_index=True,
        verbose_name=_("Expense Date (AD)")
    )
    expense_date_bs = models.CharField(
        max_length=15, blank=True, null=True,
        verbose_name=_("Expense Date (BS)")
    )
    fiscal_year = models.CharField(max_length=15, blank=True, null=True)

    payee_recipient = models.CharField(
        max_length=150, verbose_name=_("Paid To / Vendor / Landlord / Staff")
    )
    bill_invoice_number = models.CharField(
        max_length=100, blank=True, null=True,
        verbose_name=_("Vendor Bill / Receipt Ref No.")
    )
    receipt_attachment = models.FileField(
        upload_to='accounting/expenses/%Y/%m/', blank=True, null=True,
        verbose_name=_("Bill / Cash Memo Attachment")
    )
    description = models.TextField(verbose_name=_("Detailed Justification / Purpose"))

    journal_entry = models.OneToOneField(
        JournalEntry, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='source_expense_voucher'
    )
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name='recorded_expenses'
    )

    class Meta:
        db_table = 'acc_expense_vouchers'
        ordering = ['-expense_date', '-created_at']
        verbose_name = _('Operating Expense Voucher')
        verbose_name_plural = _('Operating Expense Vouchers')

    def __str__(self):
        return f"{self.voucher_number} - {self.expense_account.name}: Rs. {self.amount}"

    def save(self, *args, **kwargs):
        if self.expense_date:
            ad_date = self.expense_date.date() if isinstance(self.expense_date, datetime) else self.expense_date
            if not self.expense_date_bs or not self.fiscal_year:
                try:
                    bs_year, bs_month, bs_day = NepaliCalendar.ad_to_bs(ad_date)
                    if not self.expense_date_bs:
                        self.expense_date_bs = NepaliCalendar.format_bs(bs_year, bs_month, bs_day, lang='en')
                    if not self.fiscal_year:
                        self.fiscal_year = NepaliCalendar.get_fiscal_year(bs_year, bs_month)
                except Exception:
                    pass
        super().save(*args, **kwargs)


class BankReconciliation(TimeStampedModel):
    """
    Bank Statement Reconciliation Voucher.
    """
    STATUS_CHOICES = [
        ('IN_PROGRESS', _('Reconciliation In Progress')),
        ('RECONCILED', _('Fully Reconciled & Closed')),
    ]

    bank_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name='reconciliations',
        verbose_name=_("Bank GL Ledger Account")
    )
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT)
    statement_date = models.DateField(verbose_name=_("Statement Ending Date (AD)"))
    statement_date_bs = models.CharField(max_length=15, blank=True, null=True)

    statement_ending_balance = models.DecimalField(
        max_digits=14, decimal_places=2,
        verbose_name=_("Bank Statement Closing Balance (NPR)")
    )
    gl_book_balance = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("System GL Book Balance (NPR)")
    )
    difference = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("Unreconciled Variance")
    )

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='IN_PROGRESS')
    reconciled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    notes = models.TextField(blank=True, null=True)

    class Meta:
        db_table = 'acc_bank_reconciliations'
        ordering = ['-statement_date']
        verbose_name = _('Bank Reconciliation')
        verbose_name_plural = _('Bank Reconciliations')

    def __str__(self):
        return f"{self.bank_account.name} @ {self.statement_date} (Diff: Rs. {self.difference})"


class BankStatementLine(TimeStampedModel):
    """
    Itemized bank statement line entry matched against journal items.
    """
    reconciliation = models.ForeignKey(
        BankReconciliation, on_delete=models.CASCADE, related_name='statement_lines'
    )
    transaction_date = models.DateField()
    description = models.CharField(max_length=255)
    cheque_or_ref_no = models.CharField(max_length=100, blank=True, null=True)
    withdrawal_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    deposit_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))

    matched_journal_item = models.ForeignKey(
        JournalItem, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='bank_statement_matches'
    )
    is_cleared = models.BooleanField(default=False)

    class Meta:
        db_table = 'acc_bank_statement_lines'
        ordering = ['transaction_date', 'id']
        verbose_name = _('Bank Statement Line')
        verbose_name_plural = _('Bank Statement Lines')