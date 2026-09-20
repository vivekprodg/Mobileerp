"""
Accounting Forms & Formsets.

User interface forms with hardened validation:
1. AccountForm: Locks code, group, opening balance, and nature when transactions exist or if system-reserved.
2. JournalEntryForm & JournalItemFormSet:
   - Enforces Sum(Debit) == Sum(Credit).
   - Requires Customer sub-ledger for Accounts Receivable (1210).
   - Requires Supplier sub-ledger for Accounts Payable (2110).
   - Strictly restricts CONTRA vouchers to transfers between Cash, Bank, and Digital Wallet clearing accounts.
   - Prohibits direct manual journal lines to Opening Balance Equity (3120).
3. ExpenseVoucherForm: Simplified cashier-friendly expense recorder supporting Cash, Bank, and Digital Wallets.
4. BankReconciliationForm: Statement reconciliation matching.
5. FinancialReportFilterForm: Selecting date bounds, Nepali Fiscal Year, and branch scoping.
"""

from decimal import Decimal
from django import forms
from django.forms import inlineformset_factory, BaseInlineFormSet
from django.utils.translation import gettext_lazy as _

from apps.accounting.models import (
    Account, AccountGroup, JournalEntry, JournalItem,
    ExpenseVoucher, BankReconciliation, AccountingFiscalYear
)
from apps.branches.models import Branch
from apps.customers.models import Customer
from apps.purchases.models import Supplier


LIQUID_SYSTEM_TAGS = {'CASH', 'BANK', 'FONEPAY', 'ESEWA', 'KHALTI', 'CARD_CLEARING'}


# =============================================================================
# 1. ACCOUNT FORM (WITH TAMPER PROTECTION)
# =============================================================================
class AccountForm(forms.ModelForm):
    """Form for adding and modifying General Ledger accounts with immutable system protection."""
    class Meta:
        model = Account
        fields = [
            'code', 'name', 'name_np', 'group', 'branch',
            'opening_balance', 'opening_balance_nature', 'description'
        ]
        widgets = {
            'code': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. 1010-01'}),
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Nabil Bank Current A/C'}),
            'name_np': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. नबिल बैंक चालु खाता'}),
            'group': forms.Select(attrs={'class': 'form-select'}),
            'branch': forms.Select(attrs={'class': 'form-select'}),
            'opening_balance': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'opening_balance_nature': forms.Select(attrs={'class': 'form-select'}),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': 'Optional notes'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if self.instance and self.instance.pk:
            # Check if journal transactions exist
            has_transactions = False
            if hasattr(self.instance, 'journal_lines'):
                has_transactions = self.instance.journal_lines.exists()
            elif hasattr(self.instance, 'journal_items'):
                has_transactions = self.instance.journal_items.exists()
            elif hasattr(self.instance, 'journalitem_set'):
                has_transactions = self.instance.journalitem_set.exists()

            is_reserved = getattr(self.instance, 'is_system_reserved', False)

            if has_transactions or is_reserved:
                reason = _("transactions already exist") if has_transactions else _("it is a system control account")
                for field_name in ['code', 'group', 'opening_balance', 'opening_balance_nature']:
                    if field_name in self.fields:
                        self.fields[field_name].disabled = True
                        self.fields[field_name].help_text = _(
                            f"Locked: This attribute cannot be modified because {reason}."
                        )

    def clean_code(self):
        instance = getattr(self, 'instance', None)
        if instance and instance.pk:
            has_transactions = False
            if hasattr(instance, 'journal_lines'):
                has_transactions = instance.journal_lines.exists()
            elif hasattr(instance, 'journal_items'):
                has_transactions = instance.journal_items.exists()
            elif hasattr(instance, 'journalitem_set'):
                has_transactions = instance.journalitem_set.exists()

            if has_transactions or getattr(instance, 'is_system_reserved', False):
                return instance.code
        return self.cleaned_data.get('code')

    def clean_group(self):
        instance = getattr(self, 'instance', None)
        if instance and instance.pk:
            has_transactions = False
            if hasattr(instance, 'journal_lines'):
                has_transactions = instance.journal_lines.exists()
            elif hasattr(instance, 'journal_items'):
                has_transactions = instance.journal_items.exists()
            elif hasattr(instance, 'journalitem_set'):
                has_transactions = instance.journalitem_set.exists()

            if has_transactions or getattr(instance, 'is_system_reserved', False):
                return instance.group
        return self.cleaned_data.get('group')


# =============================================================================
# 2. JOURNAL ENTRY FORMS (DOUBLE-ENTRY AUDIT CONTROLS)
# =============================================================================
class JournalEntryForm(forms.ModelForm):
    """Header form for manual journal vouchers, contra transfers, and adjustments."""
    class Meta:
        model = JournalEntry
        fields = [
            'voucher_type', 'branch', 'entry_date', 'reference_document', 'narration'
        ]
        widgets = {
            'voucher_type': forms.Select(attrs={'class': 'form-select', 'id': 'id_voucher_type'}),
            'branch': forms.Select(attrs={'class': 'form-select'}),
            'entry_date': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
            'reference_document': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Cheque No, Bank Ref, Bill No'}),
            'narration': forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': 'Detailed narration explaining this entry'}),
        }


class JournalItemForm(forms.ModelForm):
    """Atomic line item form with sub-ledger validation and equity tampering protection."""
    class Meta:
        model = JournalItem
        fields = ['account', 'debit_amount', 'credit_amount', 'customer', 'supplier', 'line_narration']
        widgets = {
            'account': forms.Select(attrs={'class': 'form-select form-select-sm account-select'}),
            'debit_amount': forms.NumberInput(attrs={'class': 'form-control form-control-sm debit-input', 'step': '0.01', 'min': '0'}),
            'credit_amount': forms.NumberInput(attrs={'class': 'form-control form-control-sm credit-input', 'step': '0.01', 'min': '0'}),
            'customer': forms.Select(attrs={'class': 'form-select form-select-sm customer-select'}),
            'supplier': forms.Select(attrs={'class': 'form-select form-select-sm supplier-select'}),
            'line_narration': forms.TextInput(attrs={'class': 'form-control form-control-sm', 'placeholder': 'Line note'}),
        }

    def clean(self):
        cleaned_data = super().clean()
        if self.cleaned_data.get('DELETE', False):
            return cleaned_data

        acc = cleaned_data.get('account')
        dr = cleaned_data.get('debit_amount') or Decimal('0.00')
        cr = cleaned_data.get('credit_amount') or Decimal('0.00')
        cust = cleaned_data.get('customer')
        supp = cleaned_data.get('supplier')

        if not acc:
            return cleaned_data

        # 1. Single-line Debit/Credit Mutual Exclusivity
        if dr > Decimal('0.00') and cr > Decimal('0.00'):
            raise forms.ValidationError(_("A single line cannot have both Debit and Credit amounts."))
        if dr == Decimal('0.00') and cr == Decimal('0.00'):
            raise forms.ValidationError(_("Specify either a positive Debit or Credit amount for this line."))

        # 2. Disallow manual journal lines to Opening Balance Equity (3120)
        if str(acc.code).strip() == '3120' or acc.system_tag == 'OPENING_BALANCE_EQUITY':
            raise forms.ValidationError(_(
                "Direct manual journal entries to Opening Balance Equity (3120) are strictly prohibited."
            ))

        # 3. Require Customer sub-ledger on Accounts Receivable (1210)
        is_ar = (str(acc.code).strip() == '1210' or acc.system_tag == 'ACCOUNTS_RECEIVABLE')
        if is_ar and not cust:
            raise forms.ValidationError(_(
                f"Line with Account '{acc.name}' (Accounts Receivable) requires a Customer to be selected."
            ))

        # 4. Require Supplier sub-ledger on Accounts Payable (2110)
        is_ap = (str(acc.code).strip() == '2110' or acc.system_tag == 'ACCOUNTS_PAYABLE')
        if is_ap and not supp:
            raise forms.ValidationError(_(
                f"Line with Account '{acc.name}' (Accounts Payable) requires a Supplier to be selected."
            ))

        return cleaned_data


class BaseJournalItemFormSet(BaseInlineFormSet):
    """Inline formset enforcing equality and strict voucher-type constraints."""

    def clean(self):
        super().clean()
        if any(self.errors):
            return

        total_debit = Decimal('0.00')
        total_credit = Decimal('0.00')
        valid_lines_count = 0
        voucher_type = self.data.get('voucher_type') or getattr(self.instance, 'voucher_type', None)

        for form in self.forms:
            if not form.cleaned_data or form.cleaned_data.get('DELETE', False):
                continue

            acc = form.cleaned_data.get('account')
            dr = form.cleaned_data.get('debit_amount') or Decimal('0.00')
            cr = form.cleaned_data.get('credit_amount') or Decimal('0.00')

            if acc and (dr > Decimal('0.00') or cr > Decimal('0.00')):
                total_debit += dr
                total_credit += cr
                valid_lines_count += 1

                # Strict Contra Voucher Validation
                if voucher_type == 'CONTRA':
                    tag = acc.system_tag or ''
                    if tag not in LIQUID_SYSTEM_TAGS:
                        raise forms.ValidationError(_(
                            "Contra vouchers are strictly restricted to transfers between Cash, "
                            "Bank, and Digital Wallet clearing accounts."
                        ))

        if valid_lines_count < 2:
            raise forms.ValidationError(_("A double-entry voucher requires at least two valid lines (at least one Debit and one Credit)."))

        discrepancy = abs(total_debit - total_credit)
        if discrepancy > Decimal('0.00'):
            raise forms.ValidationError(_(
                f"Unbalanced Journal Voucher: Total Debits (Rs. {total_debit:,.2f}) must equal "
                f"Total Credits (Rs. {total_credit:,.2f}). Variance: Rs. {discrepancy:,.2f}."
            ))


JournalItemFormSet = inlineformset_factory(
    JournalEntry,
    JournalItem,
    form=JournalItemForm,
    formset=BaseJournalItemFormSet,
    extra=4,
    can_delete=True
)


# =============================================================================
# 3. EXPENSE VOUCHER FORM (WALLET & BANK AWARE)
# =============================================================================
class ExpenseVoucherForm(forms.ModelForm):
    """
    Cashier-friendly operating expense logger.
    Supports Cash, Bank, FonePay, eSewa, and Khalti payment sources.
    """
    class Meta:
        model = ExpenseVoucher
        fields = [
            'branch', 'expense_date', 'expense_account', 'payment_account',
            'payment_method', 'amount', 'payee_recipient', 'bill_invoice_number',
            'receipt_attachment', 'description'
        ]
        widgets = {
            'branch': forms.Select(attrs={'class': 'form-select'}),
            'expense_date': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
            'expense_account': forms.Select(attrs={'class': 'form-select'}),
            'payment_account': forms.Select(attrs={'class': 'form-select'}),
            'payment_method': forms.Select(attrs={'class': 'form-select'}),
            'amount': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'placeholder': '0.00'}),
            'payee_recipient': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Landlord, NEA, Staff Name'}),
            'bill_invoice_number': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Bill / Receipt / Memo No.'}),
            'receipt_attachment': forms.ClearableFileInput(attrs={'class': 'form-control'}),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': 'Reason for expense'}),
        }

    def __init__(self, *args, **kwargs):
        branch = kwargs.pop('branch', None)
        super().__init__(*args, **kwargs)

        self.fields['expense_account'].queryset = Account.objects.filter(
            group__category__in=['DIRECT_EXPENSE', 'INDIRECT_EXPENSE']
        ).order_by('name')

        # Allow payment source to include Cash, Bank, FonePay, eSewa, Khalti, and Clearing
        self.fields['payment_account'].queryset = Account.objects.filter(
            system_tag__in=['CASH', 'BANK', 'FONEPAY', 'ESEWA', 'KHALTI', 'CARD_CLEARING']
        ).order_by('name')

        if branch:
            self.fields['branch'].initial = branch


# =============================================================================
# 4. BANK RECONCILIATION FORM
# =============================================================================
class BankReconciliationForm(forms.ModelForm):
    """Bank statement reconciliation initialization form."""
    class Meta:
        model = BankReconciliation
        fields = ['bank_account', 'branch', 'statement_date', 'statement_ending_balance', 'notes']
        widgets = {
            'bank_account': forms.Select(attrs={'class': 'form-select'}),
            'branch': forms.Select(attrs={'class': 'form-select'}),
            'statement_date': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
            'statement_ending_balance': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'notes': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['bank_account'].queryset = Account.objects.filter(system_tag='BANK')


# =============================================================================
# 5. FINANCIAL REPORT FILTER FORM
# =============================================================================
class FinancialReportFilterForm(forms.Form):
    """Toolbar filter for Trial Balance, P&L, Balance Sheet, and Cash Flow."""
    fiscal_year = forms.ChoiceField(
        required=False,
        widget=forms.Select(attrs={'class': 'form-select form-select-sm'})
    )
    branch = forms.ModelChoiceField(
        queryset=Branch.objects.filter(is_active=True),
        required=False,
        empty_label=_("All Branches (Consolidated)"),
        widget=forms.Select(attrs={'class': 'form-select form-select-sm'})
    )
    start_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={'class': 'form-control form-control-sm', 'type': 'date'})
    )
    end_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={'class': 'form-control form-control-sm', 'type': 'date'})
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        fy_choices = [('', _('Current Ongoing Fiscal Year'))]
        years = AccountingFiscalYear.objects.order_by('-start_date_ad').values_list('name', flat=True)
        for y in years:
            fy_choices.append((y, f"FY {y}"))
        self.fields['fiscal_year'].choices = fy_choices