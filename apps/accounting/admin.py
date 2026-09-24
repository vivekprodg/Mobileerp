"""
Django Admin Configuration for Accounting Module.
Aligned with General Ledger Models and Dual English/Bikram Sambat Systems.
"""

from django.contrib import admin, messages
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from django.db.models import Sum

from apps.accounting.models import (
    AccountGroup, Account, AccountingFiscalYear, FinancialPeriod,
    JournalEntry, JournalItem, ExpenseVoucher, BankReconciliation, BankStatementLine
)

@admin.register(AccountGroup)
class AccountGroupAdmin(admin.ModelAdmin):
    list_display = ('code', 'name', 'name_np', 'category', 'nature', 'parent', 'is_system_reserved')
    list_filter = ('category', 'nature', 'is_system_reserved')
    search_fields = ('code', 'name', 'name_np')
    ordering = ('code',)
    raw_id_fields = ('parent',)

@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = [
        'code', 'name', 'name_np', 'group', 'branch_display', 'system_tag',
        'current_balance_badge', 'is_system_reserved'
    ]
    list_filter = [
        'system_tag', 'group__category', 'group__nature', 'branch', 'is_system_reserved'
    ]
    search_fields = ['code', 'name', 'name_np', 'system_tag']
    search_help_text = _("Search accounts by partial code, English name, Nepali name, or system tag (e.g., 1010, cash, नगद, BANK)")
    ordering = ['code']
    raw_id_fields = ['group', 'branch']
    readonly_fields = ['current_balance', 'created_at', 'updated_at']
    actions = ['recalculate_account_balances']

    def branch_display(self, obj):
        return obj.branch.code if obj.branch else "Organization-Wide (HQ)"
    branch_display.short_description = _("Branch Scope")

    def current_balance_badge(self, obj):
        bal = obj.current_balance
        color = "#10b981" if bal >= 0 else "#ef4444"
        nature_tag = "Dr" if obj.is_debit_nature else "Cr"
        return format_html(
            '<span style="font-weight: bold; color: {};">Rs. {:,.2f} ({})</span>',
            color, bal, nature_tag
        )
    current_balance_badge.short_description = _("Net Balance")

    @admin.action(description=_("Recalculate running balance from posted journal lines"))
    def recalculate_account_balances(self, request, queryset):
        recalculated = 0
        for acc in queryset:
            agg = acc.journal_lines.filter(journal_entry__status='POSTED').aggregate(
                dr=Sum('debit_amount'), cr=Sum('credit_amount')
            )
            total_dr = agg['dr'] or 0
            total_cr = agg['cr'] or 0
            op = acc.opening_balance or 0
            if acc.opening_balance_nature == 'DEBIT':
                total_dr += op
            else:
                total_cr += op

            if acc.is_debit_nature:
                new_bal = total_dr - total_cr
            else:
                new_bal = total_cr - total_dr

            acc.current_balance = new_bal
            acc.save(update_fields=['current_balance', 'updated_at'])
            recalculated += 1

        self.message_user(request, f"Successfully recalculated balance for {recalculated} account(s).")

class JournalItemInline(admin.TabularInline):
    model = JournalItem
    extra = 0
    fields = ['account', 'debit_amount', 'credit_amount', 'customer', 'supplier', 'line_narration']
    raw_id_fields = ['account', 'customer', 'supplier']

@admin.register(JournalEntry)
class JournalEntryAdmin(admin.ModelAdmin):
    list_display = [
        'voucher_number', 'voucher_type', 'source_module',
        'branch', 'entry_date', 'entry_date_bs', 'fiscal_year',
        'balance_check', 'status', 'created_by'
    ]
    list_filter = ['voucher_type', 'source_module', 'status', 'branch', 'fiscal_year']
    search_fields = ['voucher_number', 'source_id', 'reference_document', 'narration']
    inlines = [JournalItemInline]
    readonly_fields = [
        'voucher_number', 'total_debit', 'total_credit',
        'posted_at', 'posted_by', 'created_at', 'updated_at'
    ]
    ordering = ['-entry_date', '-created_at']
    raw_id_fields = ['branch', 'created_by', 'posted_by']

    def balance_check(self, obj):
        if obj.total_debit == obj.total_credit:
            return format_html(
                '<span style="color: #10b981; font-weight: bold;">Balanced: Rs. {:,.2f}</span>',
                obj.total_debit
            )
        return format_html(
            '<span style="color: #ef4444; font-weight: bold;">UNBALANCED Dr: {:,.2f} / Cr: {:,.2f}</span>',
            obj.total_debit, obj.total_credit
        )
    balance_check.short_description = _("Balance Equality")

@admin.register(ExpenseVoucher)
class ExpenseVoucherAdmin(admin.ModelAdmin):
    list_display = [
        'voucher_number', 'branch', 'expense_date', 'expense_date_bs',
        'expense_account', 'payment_account', 'amount_display',
        'payee_recipient', 'payment_method'
    ]
    list_filter = ['payment_method', 'branch', 'expense_account', 'fiscal_year']
    search_fields = ['voucher_number', 'payee_recipient', 'description', 'bill_invoice_number']
    raw_id_fields = ['branch', 'expense_account', 'payment_account', 'journal_entry', 'recorded_by']
    ordering = ['-expense_date', '-created_at']

    def amount_display(self, obj):
        return format_html('<strong>Rs. {:,.2f}</strong>', obj.amount)
    amount_display.short_description = _("Amount")

@admin.register(AccountingFiscalYear)
class AccountingFiscalYearAdmin(admin.ModelAdmin):
    list_display = ['name', 'start_date_bs', 'end_date_bs', 'start_date_ad', 'end_date_ad', 'status_badge']
    list_filter = ['is_closed']
    search_fields = ['name']
    raw_id_fields = ['closed_by']
    actions = ['lock_fiscal_years_action', 'unlock_fiscal_years_action', 'enforce_multi_year_setup_action']

    def status_badge(self, obj):
        if obj.is_closed:
            return format_html(
                '<span style="color: white; background-color: #ef4444; padding: 2px 8px; border-radius: 999px; font-weight: 700; font-size: 10px;">'
                'LOCKED / AUDITED'
                '</span>'
            )
        return format_html(
            '<span style="color: white; background-color: #10b981; padding: 2px 8px; border-radius: 999px; font-weight: 700; font-size: 10px;">'
            'ACTIVE / OPEN'
            '</span>'
        )
    status_badge.short_description = _("Period Status")

    @admin.action(description=_("Lock selected fiscal years and monthly periods (Close / Audit)"))
    def lock_fiscal_years_action(self, request, queryset):
        for fy in queryset:
            AccountingFiscalYear.lock_year(fy.name, user=request.user)
        self.message_user(request, f"Successfully locked {queryset.count()} fiscal year(s).")

    @admin.action(description=_("Unlock selected fiscal years and monthly periods (Temporary)"))
    def unlock_fiscal_years_action(self, request, queryset):
        for fy in queryset:
            AccountingFiscalYear.unlock_year(fy.name)
        self.message_user(request, f"Successfully unlocked {queryset.count()} fiscal year(s).")

    @admin.action(description=_("Enforce Full Setup: Lock 2080-82 & Activate FY 2083/84"))
    def enforce_multi_year_setup_action(self, request, queryset=None):
        locked_years, active_year = AccountingFiscalYear.ensure_multi_year_setup()
        self.message_user(
            request,
            format_html(
                "Multi-Year Setup Verified:<br>"
                "• <strong>LOCKED:</strong> {}<br>"
                "• <strong>ACTIVE & OPEN:</strong> {}",
                ", ".join(y.name for y in locked_years),
                active_year.name
            )
        )

@admin.register(FinancialPeriod)
class FinancialPeriodAdmin(admin.ModelAdmin):
    list_display = ['period_name_en', 'period_name_np', 'fiscal_year', 'period_number', 'start_date_bs', 'end_date_bs', 'is_closed']
    list_filter = ['fiscal_year', 'is_closed']
    search_fields = ['period_name_en', 'period_name_np']
    raw_id_fields = ['fiscal_year']

class BankStatementLineInline(admin.TabularInline):
    model = BankStatementLine
    extra = 0
    raw_id_fields = ['matched_journal_item']

@admin.register(BankReconciliation)
class BankReconciliationAdmin(admin.ModelAdmin):
    list_display = [
        'bank_account', 'branch', 'statement_date', 'statement_date_bs',
        'statement_ending_balance', 'gl_book_balance', 'difference', 'status'
    ]
    list_filter = ['status', 'branch', 'bank_account']
    search_fields = ['bank_account__name', 'notes']
    raw_id_fields = ['bank_account', 'branch', 'reconciled_by']
    inlines = [BankStatementLineInline]

@admin.register(BankStatementLine)
class BankStatementLineAdmin(admin.ModelAdmin):
    list_display = [
        'transaction_date', 'reconciliation', 'description', 'cheque_or_ref_no',
        'withdrawal_amount', 'deposit_amount', 'is_cleared', 'matched_journal_item'
    ]
    list_filter = ['is_cleared', 'transaction_date']
    search_fields = ['description', 'cheque_or_ref_no']
    raw_id_fields = ['reconciliation', 'matched_journal_item']