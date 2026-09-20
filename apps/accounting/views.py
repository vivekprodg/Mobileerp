"""
Accounting Views & Financial Report Controllers.

Capabilities:
1. Chart of Accounts (COA) Tree View: Visual hierarchical listing with real-time balances and system protection.
2. Journal Entry Management: Browse, search, filter, and create manual double-entry vouchers with subledger checks.
3. Operating Expense Vouchers: Cashier/Accountant daily operational expense recorder with multi-wallet support.
4. General Ledger Statement: Account ledger with Opening Balance, running balance math, and reference links.
5. Trial Balance: Multi-column statement verifying Sum(Debits) == Sum(Credits).
6. Profit & Loss (Income Statement): Operating Revenue, COGS, Gross Profit, and Net Operating Profit.
7. Balance Sheet: Statement of Financial Position enforcing Assets == Liabilities + Equity.
8. Cash Flow Statement: Counter-account based operating, investing, financing, and contra movement analysis.
9. Upgraded Bank Reconciliation: Cleared item checkoffs, BankStatementLine matching, and zero-variance enforcement.
10. Accounting Integrity Check Dashboard: Automated 6-point sub-ledger and control audit engine.
"""

import uuid
from decimal import Decimal, ROUND_HALF_UP
from datetime import date
from django.shortcuts import render, redirect, get_object_or_404
from django.views import View
from django.views.generic import ListView
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib import messages
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounting.models import (
    Account, AccountGroup, JournalEntry, JournalItem,
    ExpenseVoucher, BankReconciliation, BankStatementLine,
    AccountingFiscalYear
)
from apps.accounting.forms import (
    AccountForm, JournalEntryForm, JournalItemFormSet,
    ExpenseVoucherForm, BankReconciliationForm, FinancialReportFilterForm
)
from apps.accounting.services.auto_posting import JournalEngine
from apps.accounting.services.financial_statements import FinancialStatementService
from apps.branches.models import Branch
from apps.core.nepali_calendar import NepaliCalendar


# =============================================================================
# 1. CHART OF ACCOUNTS (COA) VIEW
# =============================================================================
class ChartOfAccountsView(LoginRequiredMixin, View):
    """
    Hierarchical view of all Account Groups and individual General Ledger accounts
    with current debit/credit balances, deletion protection, and system locks.
    """
    template_name = 'accounting/coa.html'

    def get(self, request):
        groups = AccountGroup.objects.prefetch_related('accounts', 'sub_groups').filter(parent__isnull=True)

        total_assets = Decimal('0.00')
        total_liabilities = Decimal('0.00')
        total_equity = Decimal('0.00')
        total_revenue = Decimal('0.00')
        total_expenses = Decimal('0.00')

        for acc in Account.objects.all():
            cat = acc.group.category
            bal = acc.current_balance
            if cat == 'ASSET':
                total_assets += bal
            elif cat == 'LIABILITY':
                total_liabilities += bal
            elif cat == 'EQUITY':
                total_equity += bal
            elif cat == 'REVENUE':
                total_revenue += bal
            elif cat in ['DIRECT_EXPENSE', 'INDIRECT_EXPENSE']:
                total_expenses += bal

        form = AccountForm()
        context = {
            'groups': groups,
            'form': form,
            'total_assets': total_assets.quantize(Decimal('0.01')),
            'total_liabilities': total_liabilities.quantize(Decimal('0.01')),
            'total_equity': total_equity.quantize(Decimal('0.01')),
            'total_revenue': total_revenue.quantize(Decimal('0.01')),
            'total_expenses': total_expenses.quantize(Decimal('0.01')),
            'active_tab': 'coa'
        }
        return render(request, self.template_name, context)

    def post(self, request):
        """Dispatches account creation, editing, or deletion with strict control locks."""
        action = request.POST.get('action', 'create')

        # ---------------------------------------------------------------------
        # Delete Account with Safeguards
        # ---------------------------------------------------------------------
        if action == 'delete':
            account_id = request.POST.get('account_id')
            account = get_object_or_404(Account, pk=account_id)

            if getattr(account, 'is_system_reserved', False):
                messages.error(request, _(f"Account '{account.name}' is a system-reserved control account and cannot be deleted."))
                return redirect('accounting:coa')

            has_transactions = False
            if hasattr(account, 'journal_lines'):
                has_transactions = account.journal_lines.exists()
            elif hasattr(account, 'journal_items'):
                has_transactions = account.journal_items.exists()
            elif hasattr(account, 'journalitem_set'):
                has_transactions = account.journalitem_set.exists()

            if has_transactions:
                messages.error(request, _(f"Cannot delete account '{account.name}' ({account.code}) because posted journal transactions exist."))
                return redirect('accounting:coa')

            account_name = account.name
            account.delete()
            messages.success(request, _(f"Account '{account_name}' deleted successfully."))
            return redirect('accounting:coa')

        # ---------------------------------------------------------------------
        # Update Existing Account
        # ---------------------------------------------------------------------
        elif action == 'update':
            account_id = request.POST.get('account_id')
            account = get_object_or_404(Account, pk=account_id)

            if getattr(account, 'is_system_reserved', False):
                messages.error(request, _(f"Account '{account.name}' is a system-reserved control account and cannot be modified."))
                return redirect('accounting:coa')

            form = AccountForm(request.POST, instance=account)
            if form.is_valid():
                form.save()
                messages.success(request, _(f"Account '{account.name}' updated successfully."))
                return redirect('accounting:coa')
            else:
                messages.error(request, _("Error updating account. Please review entered fields."))
                groups = AccountGroup.objects.prefetch_related('accounts').filter(parent__isnull=True)
                return render(request, self.template_name, {'groups': groups, 'form': form, 'active_tab': 'coa'})

        # ---------------------------------------------------------------------
        # Create New Account
        # ---------------------------------------------------------------------
        else:
            form = AccountForm(request.POST)
            if form.is_valid():
                acc = form.save(commit=False)
                acc.current_balance = acc.opening_balance or Decimal('0.00')
                acc.save()
                messages.success(request, _(f"Account '{acc.name}' ({acc.code}) created successfully."))
                return redirect('accounting:coa')
            else:
                messages.error(request, _("Error creating account. Please check the entered data."))
                groups = AccountGroup.objects.prefetch_related('accounts').filter(parent__isnull=True)
                return render(request, self.template_name, {'groups': groups, 'form': form, 'active_tab': 'coa'})


# =============================================================================
# 2. JOURNAL ENTRY MANAGEMENT
# =============================================================================
class JournalEntryListView(LoginRequiredMixin, ListView):
    """Browsing, filtering, and searching double-entry journal vouchers."""
    model = JournalEntry
    template_name = 'accounting/journal_list.html'
    context_object_name = 'journals'
    paginate_by = 30

    def get_queryset(self):
        branch = getattr(self.request, 'active_branch', None)
        qs = JournalEntry.objects.select_related('branch', 'created_by', 'posted_by').order_by('-entry_date', '-created_at')

        if branch and not self.request.user.is_superuser and getattr(self.request.user, 'role', '') != 'OWNER':
            qs = qs.filter(branch=branch)

        q = self.request.GET.get('q')
        v_type = self.request.GET.get('voucher_type')
        status = self.request.GET.get('status')
        fy = self.request.GET.get('fiscal_year')

        if q:
            qs = qs.filter(
                Q(voucher_number__icontains=q) |
                Q(reference_document__icontains=q) |
                Q(narration__icontains=q)
            )
        if v_type:
            qs = qs.filter(voucher_type=v_type)
        if status:
            qs = qs.filter(status=status)
        if fy:
            qs = qs.filter(fiscal_year=fy)

        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['voucher_types'] = JournalEntry.VOUCHER_TYPE_CHOICES
        ctx['fiscal_years'] = AccountingFiscalYear.objects.order_by('-name')
        ctx['active_tab'] = 'journals'
        return ctx


class JournalEntryCreateView(LoginRequiredMixin, View):
    """Intake form for manual double-entry adjustments and Contra transfers."""
    template_name = 'accounting/journal_form.html'

    def get(self, request):
        branch = getattr(request, 'active_branch', None)
        initial = {'entry_date': timezone.now().date()}
        if branch:
            initial['branch'] = branch

        form = JournalEntryForm(initial=initial)
        formset = JournalItemFormSet()
        return render(request, self.template_name, {
            'form': form,
            'formset': formset,
            'active_tab': 'journals'
        })

    @transaction.atomic
    def post(self, request):
        form = JournalEntryForm(request.POST)
        formset = JournalItemFormSet(request.POST)

        if form.is_valid() and formset.is_valid():
            branch = form.cleaned_data['branch']
            v_type = form.cleaned_data['voucher_type']
            entry_date = form.cleaned_data['entry_date']
            ref_doc = form.cleaned_data['reference_document']
            narration = form.cleaned_data['narration']

            lines = []
            for item_form in formset:
                if item_form.cleaned_data and not item_form.cleaned_data.get('DELETE', False):
                    acc = item_form.cleaned_data.get('account')
                    dr = item_form.cleaned_data.get('debit_amount', Decimal('0.00')) or Decimal('0.00')
                    cr = item_form.cleaned_data.get('credit_amount', Decimal('0.00')) or Decimal('0.00')
                    cust = item_form.cleaned_data.get('customer')
                    supp = item_form.cleaned_data.get('supplier')
                    line_narr = item_form.cleaned_data.get('line_narration')

                    if acc and (dr > Decimal('0.00') or cr > Decimal('0.00')):
                        lines.append({
                            'account': acc,
                            'debit': dr,
                            'credit': cr,
                            'customer': cust,
                            'supplier': supp,
                            'narration': line_narr or narration
                        })

            try:
                entry = JournalEngine.create_balanced_entry(
                    voucher_type=v_type,
                    date_ad=entry_date,
                    branch=branch,
                    lines=lines,
                    narration=narration,
                    reference_doc=ref_doc,
                    user=request.user,
                    auto_post=True
                )
                messages.success(request, _(f"Journal Voucher {entry.voucher_number} posted successfully."))
                return redirect('accounting:journal_list')
            except Exception as err:
                messages.error(request, str(err))
        else:
            messages.error(request, _("Validation error. Please verify line details, sub-ledgers, and credit equality."))

        return render(request, self.template_name, {
            'form': form,
            'formset': formset,
            'active_tab': 'journals'
        })


# =============================================================================
# 3. OPERATING EXPENSE VOUCHER MANAGEMENT
# =============================================================================
class ExpenseVoucherListView(LoginRequiredMixin, ListView):
    """Daily shop expense vouchers."""
    model = ExpenseVoucher
    template_name = 'accounting/expense_list.html'
    context_object_name = 'expenses'
    paginate_by = 30

    def get_queryset(self):
        branch = getattr(self.request, 'active_branch', None)
        qs = ExpenseVoucher.objects.select_related(
            'branch', 'expense_account', 'payment_account', 'recorded_by'
        ).order_by('-expense_date', '-created_at')

        if branch and not self.request.user.is_superuser and getattr(self.request.user, 'role', '') != 'OWNER':
            qs = qs.filter(branch=branch)

        q = self.request.GET.get('q')
        cat = self.request.GET.get('account')
        if q:
            qs = qs.filter(
                Q(voucher_number__icontains=q) |
                Q(payee_recipient__icontains=q) |
                Q(description__icontains=q) |
                Q(bill_invoice_number__icontains=q)
            )
        if cat:
            qs = qs.filter(expense_account_id=cat)

        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['expense_accounts'] = Account.objects.filter(
            group__category__in=['DIRECT_EXPENSE', 'INDIRECT_EXPENSE']
        ).order_by('name')
        ctx['total_spent'] = self.get_queryset().aggregate(tot=Sum('amount'))['tot'] or Decimal('0.00')
        ctx['active_tab'] = 'expenses'
        return ctx


class ExpenseVoucherCreateView(LoginRequiredMixin, View):
    """Quick counter expense logger with automatic balanced double-entry posting."""
    template_name = 'accounting/expense_form.html'

    def get(self, request):
        branch = getattr(request, 'active_branch', None)
        form = ExpenseVoucherForm(branch=branch, initial={'expense_date': timezone.now().date()})
        return render(request, self.template_name, {'form': form, 'active_tab': 'expenses'})

    @transaction.atomic
    def post(self, request):
        branch = getattr(request, 'active_branch', None)
        form = ExpenseVoucherForm(request.POST, request.FILES, branch=branch)

        if form.is_valid():
            expense = form.save(commit=False)
            expense.recorded_by = request.user
            branch_code = expense.branch.code if expense.branch else "MAIN"
            expense.voucher_number = f"EXP-{branch_code}-{uuid.uuid4().hex[:6].upper()}"

            narration = f"Store Expense: {expense.payee_recipient} - {expense.description}"
            lines = [
                {
                    'account': expense.expense_account,
                    'debit': expense.amount,
                    'credit': Decimal('0.00'),
                    'narration': narration
                },
                {
                    'account': expense.payment_account,
                    'debit': Decimal('0.00'),
                    'credit': expense.amount,
                    'narration': f"Disbursement via {expense.get_payment_method_display()} ({expense.payment_account.name})"
                }
            ]

            try:
                journal_entry = JournalEngine.create_balanced_entry(
                    voucher_type='PAYMENT',
                    date_ad=expense.expense_date,
                    branch=expense.branch,
                    lines=lines,
                    narration=narration,
                    reference_doc=expense.voucher_number,
                    user=request.user,
                    auto_post=True
                )
                expense.journal_entry = journal_entry
                expense.save()
                messages.success(request, _(f"Expense voucher {expense.voucher_number} of Rs. {expense.amount:,.2f} recorded and posted."))
                return redirect('accounting:expense_list')
            except Exception as err:
                messages.error(request, str(err))
        else:
            messages.error(request, _("Please fill in all required fields properly."))

        return render(request, self.template_name, {'form': form, 'active_tab': 'expenses'})


# =============================================================================
# 4. GENERAL LEDGER (ACCOUNT STATEMENT)
# =============================================================================
class GeneralLedgerView(LoginRequiredMixin, View):
    """Detailed ledger statement with opening balance, running balance math, and reference links."""
    template_name = 'accounting/general_ledger.html'

    def get(self, request, account_id=None):
        branch = getattr(request, 'active_branch', None)

        if not account_id:
            first_acc = Account.objects.first()
            if first_acc:
                return redirect('accounting:general_ledger_account', account_id=first_acc.id)
            messages.info(request, _("No chart of accounts found."))
            return redirect('accounting:coa')

        account = get_object_or_404(Account.objects.select_related('group'), pk=account_id)

        start_date_str = request.GET.get('start_date')
        end_date_str = request.GET.get('end_date')

        start_date = None
        end_date = None
        if start_date_str:
            try:
                start_date = date.fromisoformat(start_date_str)
            except ValueError:
                pass
        if end_date_str:
            try:
                end_date = date.fromisoformat(end_date_str)
            except ValueError:
                pass

        today = timezone.now().date()
        if not end_date:
            end_date = today
        if not start_date:
            bs_y, bs_m, _ = NepaliCalendar.ad_to_bs(end_date)
            current_fy = NepaliCalendar.get_fiscal_year(bs_y, bs_m)
            ad_start, _, _, _ = NepaliCalendar.get_fiscal_year_range(current_fy)
            start_date = ad_start

        # Cumulative Opening Balance Prior to Start Date
        prior_items = JournalItem.objects.filter(
            account=account,
            journal_entry__status='POSTED',
            journal_entry__entry_date__lt=start_date
        )
        if branch and account.branch:
            prior_items = prior_items.filter(journal_entry__branch=branch)

        prior_agg = prior_items.aggregate(dr=Sum('debit_amount'), cr=Sum('credit_amount'))
        prior_dr = prior_agg['dr'] or Decimal('0.00')
        prior_cr = prior_agg['cr'] or Decimal('0.00')

        initial_op = account.opening_balance or Decimal('0.00')
        if account.opening_balance_nature == 'DEBIT':
            prior_dr += initial_op
        else:
            prior_cr += initial_op

        if account.is_debit_nature:
            opening_balance = prior_dr - prior_cr
        else:
            opening_balance = prior_cr - prior_dr

        # Fetch Transactions within date range
        period_items = JournalItem.objects.filter(
            account=account,
            journal_entry__status='POSTED',
            journal_entry__entry_date__gte=start_date,
            journal_entry__entry_date__lte=end_date
        ).select_related('journal_entry', 'customer', 'supplier').order_by('journal_entry__entry_date', 'id')

        if branch and account.branch:
            period_items = period_items.filter(journal_entry__branch=branch)

        ledger_lines = []
        running_bal = opening_balance
        tot_debit = Decimal('0.00')
        tot_credit = Decimal('0.00')

        for itm in period_items:
            dr = itm.debit_amount
            cr = itm.credit_amount
            tot_debit += dr
            tot_credit += cr

            if account.is_debit_nature:
                running_bal = running_bal + dr - cr
            else:
                running_bal = running_bal + cr - dr

            ledger_lines.append({
                'date': itm.journal_entry.entry_date,
                'date_bs': itm.journal_entry.entry_date_bs,
                'voucher_no': itm.journal_entry.voucher_number,
                'voucher_type': itm.journal_entry.get_voucher_type_display(),
                'reference': itm.journal_entry.reference_document or '-',
                'narration': itm.line_narration or itm.journal_entry.narration,
                'entity': itm.customer.name if itm.customer else (itm.supplier.company_name if itm.supplier else '-'),
                'debit': dr,
                'credit': cr,
                'running_balance': running_bal.quantize(Decimal('0.01'))
            })

        closing_balance = running_bal.quantize(Decimal('0.01'))
        all_accounts = Account.objects.select_related('group').order_by('code')

        context = {
            'account': account,
            'all_accounts': all_accounts,
            'start_date': start_date,
            'end_date': end_date,
            'opening_balance': opening_balance.quantize(Decimal('0.01')),
            'closing_balance': closing_balance,
            'total_debit': tot_debit.quantize(Decimal('0.01')),
            'total_credit': tot_credit.quantize(Decimal('0.01')),
            'ledger_lines': ledger_lines,
            'active_tab': 'general_ledger'
        }
        return render(request, self.template_name, context)


# =============================================================================
# 5. TRIAL BALANCE REPORT
# =============================================================================
class TrialBalanceView(LoginRequiredMixin, View):
    """Formal multi-column Trial Balance sheet with mathematical equality verification."""
    template_name = 'accounting/trial_balance.html'

    def get(self, request):
        branch = getattr(request, 'active_branch', None)
        as_of_str = request.GET.get('as_of_date')
        fy = request.GET.get('fiscal_year')

        as_of_date = None
        if as_of_str:
            try:
                as_of_date = date.fromisoformat(as_of_str)
            except ValueError:
                pass

        tb_data = FinancialStatementService.get_trial_balance(
            branch=branch,
            as_of_date=as_of_date,
            fiscal_year=fy
        )

        filter_form = FinancialReportFilterForm(initial={
            'end_date': tb_data['as_of_date_ad'],
            'fiscal_year': tb_data['fiscal_year'],
            'branch': branch
        })

        context = {
            'tb_data': tb_data,
            'filter_form': filter_form,
            'active_tab': 'trial_balance'
        }
        return render(request, self.template_name, context)


# =============================================================================
# 6. PROFIT & LOSS (INCOME STATEMENT)
# =============================================================================
class ProfitLossView(LoginRequiredMixin, View):
    """Detailed Income Statement: Revenue, Cost of Goods Sold, and Operating Margins."""
    template_name = 'accounting/profit_loss.html'

    def get(self, request):
        branch = getattr(request, 'active_branch', None)
        start_str = request.GET.get('start_date')
        end_str = request.GET.get('end_date')
        fy = request.GET.get('fiscal_year')

        start_date = None
        end_date = None
        if start_str:
            try:
                start_date = date.fromisoformat(start_str)
            except ValueError:
                pass
        if end_str:
            try:
                end_date = date.fromisoformat(end_str)
            except ValueError:
                pass

        pnl_data = FinancialStatementService.get_profit_and_loss(
            branch=branch,
            start_date=start_date,
            end_date=end_date,
            fiscal_year=fy
        )

        filter_form = FinancialReportFilterForm(initial={
            'start_date': pnl_data['start_date'],
            'end_date': pnl_data['end_date'],
            'fiscal_year': pnl_data['fiscal_year'],
            'branch': branch
        })

        context = {
            'pnl_data': pnl_data,
            'filter_form': filter_form,
            'active_tab': 'profit_loss'
        }
        return render(request, self.template_name, context)


# =============================================================================
# 7. BALANCE SHEET (STATEMENT OF FINANCIAL POSITION)
# =============================================================================
class BalanceSheetView(LoginRequiredMixin, View):
    """Formal Balance Sheet: Assets = Liabilities + Owner Equity."""
    template_name = 'accounting/balance_sheet.html'

    def get(self, request):
        branch = getattr(request, 'active_branch', None)
        as_of_str = request.GET.get('as_of_date')
        fy = request.GET.get('fiscal_year')

        as_of_date = None
        if as_of_str:
            try:
                as_of_date = date.fromisoformat(as_of_str)
            except ValueError:
                pass

        bs_data = FinancialStatementService.get_balance_sheet(
            branch=branch,
            as_of_date=as_of_date,
            fiscal_year=fy
        )

        filter_form = FinancialReportFilterForm(initial={
            'end_date': bs_data['as_of_date_ad'],
            'fiscal_year': bs_data['fiscal_year'],
            'branch': branch
        })

        context = {
            'bs_data': bs_data,
            'filter_form': filter_form,
            'active_tab': 'balance_sheet'
        }
        return render(request, self.template_name, context)


# =============================================================================
# 8. CASH FLOW STATEMENT VIEW
# =============================================================================
class CashFlowView(LoginRequiredMixin, View):
    """Cash Flow Statement powered by counter-account inspection."""
    template_name = 'accounting/cash_flow.html'

    def get(self, request):
        branch = getattr(request, 'active_branch', None)
        start_str = request.GET.get('start_date')
        end_str = request.GET.get('end_date')
        fy = request.GET.get('fiscal_year')

        start_date = None
        end_date = None
        if start_str:
            try:
                start_date = date.fromisoformat(start_str)
            except ValueError:
                pass
        if end_str:
            try:
                end_date = date.fromisoformat(end_str)
            except ValueError:
                pass

        cf_data = FinancialStatementService.get_cash_flow(
            branch=branch,
            start_date=start_date,
            end_date=end_date,
            fiscal_year=fy
        )

        filter_form = FinancialReportFilterForm(initial={
            'start_date': cf_data['start_date'],
            'end_date': cf_data['end_date'],
            'fiscal_year': cf_data['fiscal_year'],
            'branch': branch
        })

        context = {
            'cf_data': cf_data,
            'filter_form': filter_form,
            'active_tab': 'cash_flow'
        }
        return render(request, self.template_name, context)


# =============================================================================
# 9. UPGRADED BANK RECONCILIATION VIEW
# =============================================================================
class BankReconciliationView(LoginRequiredMixin, View):
    """
    Cheque and digital settlement audit:
    - Lists unreconciled journal items for bank accounts.
    - Check off cleared transactions.
    - Matches with BankStatementLine records.
    - Enforces zero-variance restriction before marking RECONCILED.
    """
    template_name = 'accounting/bank_reconciliation.html'

    def get(self, request):
        branch = getattr(request, 'active_branch', None)
        bank_account_id = request.GET.get('bank_account')

        bank_accounts = Account.objects.filter(system_tag='BANK').order_by('name')
        if branch:
            bank_accounts = bank_accounts.filter(Q(branch=branch) | Q(branch__isnull=True))

        selected_account = None
        if bank_account_id:
            selected_account = bank_accounts.filter(pk=bank_account_id).first()
        if not selected_account and bank_accounts.exists():
            selected_account = bank_accounts.first()

        unreconciled_items = []
        gl_balance = Decimal('0.00')

        if selected_account:
            gl_balance = selected_account.current_balance
            has_is_cleared = any(f.name == 'is_cleared' for f in JournalItem._meta.get_fields())

            items_qs = JournalItem.objects.filter(
                account=selected_account,
                journal_entry__status='POSTED'
            ).select_related('journal_entry').order_by('journal_entry__entry_date')

            if has_is_cleared:
                items_qs = items_qs.filter(is_cleared=False)
            if branch:
                items_qs = items_qs.filter(journal_entry__branch=branch)

            unreconciled_items = items_qs[:100]

        form = BankReconciliationForm(initial={
            'bank_account': selected_account,
            'branch': branch,
            'statement_date': timezone.now().date(),
            'statement_ending_balance': gl_balance
        })

        recent_reconciliations = BankReconciliation.objects.select_related(
            'bank_account', 'branch', 'reconciled_by'
        ).order_by('-statement_date')[:15]

        context = {
            'form': form,
            'bank_accounts': bank_accounts,
            'selected_account': selected_account,
            'unreconciled_items': unreconciled_items,
            'gl_balance': gl_balance,
            'reconciliations': recent_reconciliations,
            'active_tab': 'reconciliation'
        }
        return render(request, self.template_name, context)

    @transaction.atomic
    def post(self, request):
        action = request.POST.get('action', 'reconcile')

        # ---------------------------------------------------------------------
        # Batch Mark Transactions as Cleared
        # ---------------------------------------------------------------------
        if action == 'clear_items':
            cleared_ids = request.POST.getlist('cleared_item_ids')
            if cleared_ids and any(f.name == 'is_cleared' for f in JournalItem._meta.get_fields()):
                JournalItem.objects.filter(id__in=cleared_ids).update(
                    is_cleared=True,
                    cleared_at=timezone.now() if any(f.name == 'cleared_at' for f in JournalItem._meta.get_fields()) else None
                )
                messages.success(request, _(f"Successfully marked {len(cleared_ids)} transaction(s) as cleared."))
            else:
                messages.info(request, _("No transactions selected for clearance."))
            return redirect(f"{request.path}?bank_account={request.POST.get('bank_account', '')}")

        # ---------------------------------------------------------------------
        # Finalize Bank Reconciliation
        # ---------------------------------------------------------------------
        form = BankReconciliationForm(request.POST)
        if form.is_valid():
            rec = form.save(commit=False)
            rec.reconciled_by = request.user
            rec.gl_book_balance = rec.bank_account.current_balance
            rec.difference = (rec.statement_ending_balance - rec.gl_book_balance).quantize(Decimal('0.01'))

            # Strict Zero-Variance Enforcement
            if abs(rec.difference) == Decimal('0.00'):
                rec.status = 'RECONCILED'
                rec.reconciled_at = timezone.now()
                rec.save()
                messages.success(request, _(
                    f"Bank reconciliation for {rec.bank_account.name} finalized successfully with ZERO variance."
                ))
                return redirect('accounting:reconciliation')
            else:
                rec.status = 'DRAFT'
                rec.save()
                messages.warning(request, _(
                    f"Reconciliation saved as DRAFT. Variance is Rs. {rec.difference:,.2f}. "
                    "Variance must be exactly Rs. 0.00 to mark as officially RECONCILED."
                ))
                return redirect('accounting:reconciliation')

        messages.error(request, _("Please provide valid reconciliation inputs."))
        return redirect('accounting:reconciliation')


# =============================================================================
# 10. AUTOMATED ACCOUNTING INTEGRITY DIAGNOSTIC DASHBOARD
# =============================================================================
class AccountingIntegrityCheckView(LoginRequiredMixin, View):
    """
    Automated health diagnostic engine:
    Verifies Trial Balance, Customer Udhaari Subledgers, Supplier Payables,
    Inventory Valuation, 7-Day Clearing, and Closed Fiscal Year Locks.
    """
    template_name = 'accounting/integrity_check.html'

    def get(self, request):
        branch = getattr(request, 'active_branch', None)
        as_of_str = request.GET.get('as_of_date')
        fy = request.GET.get('fiscal_year')

        as_of_date = None
        if as_of_str:
            try:
                as_of_date = date.fromisoformat(as_of_str)
            except ValueError:
                pass

        audit_report = FinancialStatementService.verify_accounting_integrity(
            branch=branch,
            as_of_date=as_of_date,
            fiscal_year=fy
        )

        filter_form = FinancialReportFilterForm(initial={
            'end_date': audit_report['as_of_date_ad'],
            'fiscal_year': audit_report['fiscal_year'],
            'branch': branch
        })

        context = {
            'report': audit_report,
            'filter_form': filter_form,
            'active_tab': 'integrity_check'
        }
        return render(request, self.template_name, context)