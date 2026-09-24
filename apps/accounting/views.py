"""
Accounting Views & Financial Report Controllers.

Capabilities:
1. Chart of Accounts (COA) Tree View: Visual hierarchical listing with real-time balances,
   database-level conditional aggregation, and system protection.
2. Journal Entry Management: Browse, search, filter, and create manual double-entry vouchers
   with subledger checks and prefetched items.
3. Operating Expense Vouchers: Cashier/Accountant daily operational expense recorder with
   multi-wallet support and select_related optimizations.
4. General Ledger Statement: Account ledger with Opening Balance, running balance math, and reference links.
5. Trial Balance: Multi-column statement verifying Sum(Debits) == Sum(Credits).
6. Profit & Loss (Income Statement): Operating Revenue, COGS, Gross Profit, and Net Operating Profit.
7. Balance Sheet: Statement of Financial Position enforcing Assets == Liabilities + Equity.
8. Cash Flow Statement: Counter-account based operating, investing, financing, and contra movement analysis.
9. Upgraded Bank Reconciliation: Cleared item checkoffs, BankStatementLine matching, and zero-variance enforcement.
10. Accounting Integrity Check Dashboard: Automated 6-point sub-ledger and control audit engine.
11. Gateway Settlement Engine: Daily digital clearing settlement with MDR commission calculation.
12. Account Search API: High-speed, indexed autocomplete for instant lookup by code, name, Devanagari, or tag.
"""

import uuid
from decimal import Decimal, ROUND_HALF_UP
from datetime import date
from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse
from django.views import View
from django.views.generic import ListView
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib import messages
from django.db import transaction
from django.db.models import (
    Q, Sum, F, Case, When, Value, DecimalField, Exists, OuterRef, Prefetch
)
from django.db.models.functions import Coalesce
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
from apps.accounting.services.auto_posting import AutoPostingService, JournalEngine
from apps.accounting.services.financial_statements import FinancialStatementService
from apps.branches.models import Branch
from apps.core.nepali_calendar import NepaliCalendar

# =============================================================================
# FAST SEARCH API: GENERAL LEDGER ACCOUNTS (SELECT2 / AUTOCOMPLETE)
# =============================================================================
class AccountSearchAPIView(LoginRequiredMixin, View):
    """
    High-Performance Search API for General Ledger Accounts.

    Query Parameters:
      - q: Search string (partial code prefix e.g. '11', name 'cash', Devanagari 'नगद', or system tag).
      - category: Filter category scope:
          * 'EXPENSE'  -> DIRECT_EXPENSE, INDIRECT_EXPENSE
          * 'BANK'     -> system_tag == 'BANK'
          * 'CASH'     -> system_tag == 'CASH'
          * 'PAYMENT'  -> CASH, BANK, FONEPAY, ESEWA, KHALTI, CARD_CLEARING
          * 'REVENUE'  -> Operating Revenue / Sales
          * 'ASSET', 'LIABILITY', 'EQUITY', 'DIRECT_EXPENSE', 'INDIRECT_EXPENSE'
      - system_tag: Filter by exact system automation tag.
      - branch: Target branch ID (defaults to active branch / organization-wide HQ).
      - limit: Maximum results returned (default: 30, max: 100).
    """

    def get(self, request, *args, **kwargs):
        q = request.GET.get('q', '').strip()
        category = request.GET.get('category', '').strip().upper()
        system_tag = request.GET.get('system_tag', '').strip().upper()
        branch_id = request.GET.get('branch')
        
        try:
            limit = min(int(request.GET.get('limit', 30)), 100)
        except (ValueError, TypeError):
            limit = 30

        # Determine target branch scope
        active_branch = getattr(request, 'active_branch', None)
        target_branch = branch_id or active_branch

        qs = Account.objects.select_related('group', 'branch')

        # Scope by branch: include branch-specific accounts and organization-wide (HQ) accounts
        if target_branch:
            qs = qs.filter(Q(branch=target_branch) | Q(branch__isnull=True))

        # Filter by specific system automation tag
        if system_tag:
            qs = qs.filter(system_tag=system_tag)

        # Filter by category / group role
        if category:
            if category in ['EXPENSE', 'EXPENSES']:
                qs = qs.filter(group__category__in=['DIRECT_EXPENSE', 'INDIRECT_EXPENSE'])
            elif category == 'BANK':
                qs = qs.filter(system_tag='BANK')
            elif category == 'CASH':
                qs = qs.filter(system_tag='CASH')
            elif category in ['PAYMENT', 'PAYMENT_SOURCE']:
                qs = qs.filter(system_tag__in=['CASH', 'BANK', 'FONEPAY', 'ESEWA', 'KHALTI', 'CARD_CLEARING'])
            elif category in ['REVENUE', 'INCOME']:
                qs = qs.filter(group__category='REVENUE')
            elif category in ['ASSET', 'LIABILITY', 'EQUITY', 'DIRECT_EXPENSE', 'INDIRECT_EXPENSE']:
                qs = qs.filter(group__category=category)

        # ---------------------------------------------------------------------
        # Empty query: return primary control / active operational accounts
        # ---------------------------------------------------------------------
        if not q:
            if not category and not system_tag:
                primary_tags = [
                    'CASH', 'BANK', 'FONEPAY', 'ESEWA', 'KHALTI',
                    'ACCOUNTS_RECEIVABLE', 'ACCOUNTS_PAYABLE',
                    'INVENTORY_ASSET', 'SALES_REVENUE', 'COGS',
                    'OUTPUT_VAT', 'INPUT_VAT', 'REPAIR_SERVICE_INCOME'
                ]
                primary_qs = qs.filter(system_tag__in=primary_tags)
                if primary_qs.exists():
                    qs = primary_qs

            qs = qs.order_by('code')[:limit]

        # ---------------------------------------------------------------------
        # Non-empty query: search by code, name, Devanagari name, or system tag
        # ---------------------------------------------------------------------
        else:
            qs = qs.filter(
                Q(code__icontains=q) |
                Q(name__icontains=q) |
                Q(name_np__icontains=q) |
                Q(system_tag__icontains=q)
            ).order_by(
                Case(
                    When(code__iexact=q, then=0),
                    When(code__istartswith=q, then=1),
                    When(name__istartswith=q, then=2),
                    default=3
                ),
                'code'
            )[:limit]

        # Build optimized JSON response
        results = []
        for acc in qs:
            bal = acc.current_balance or Decimal('0.00')
            nature_badge = "Dr" if acc.is_debit_nature else "Cr"
            nature_full = "DEBIT" if acc.is_debit_nature else "CREDIT"
            bal_badge = f"Rs. {bal:,.2f} ({nature_badge})"

            display_name = f"{acc.code} - {acc.name}"
            if acc.name_np:
                display_name += f" ({acc.name_np})"
            if acc.branch:
                display_name += f" [{acc.branch.code}]"

            results.append({
                'id': acc.id,
                'code': acc.code,
                'name': acc.name,
                'name_en': acc.name,
                'name_np': acc.name_np or '',
                'display_name': display_name,
                'current_balance': str(bal),
                'current_balance_formatted': f"Rs. {bal:,.2f}",
                'current_balance_badge': bal_badge,
                'nature': nature_full,
                'nature_display': nature_badge,
                'category': acc.group.category,
                'system_tag': acc.system_tag,
                'branch_code': acc.branch.code if acc.branch else 'HQ'
            })

        return JsonResponse({'results': results, 'count': len(results)})

# =============================================================================
# 1. CHART OF ACCOUNTS (COA) VIEW
# =============================================================================
class ChartOfAccountsView(LoginRequiredMixin, View):
    """
    Hierarchical view of all Account Groups and individual General Ledger accounts
    with current debit/credit balances, deletion protection, and system locks.
    Optimized: Uses PostgreSQL conditional aggregations and pre-annotated existence queries.
    """
    template_name = 'accounting/coa.html'

    def get(self, request):
        has_tx_subquery = JournalItem.objects.filter(account=OuterRef('pk'))
        accounts_prefetched = Account.objects.select_related('group', 'branch').annotate(
            has_tx=Exists(has_tx_subquery)
        )

        groups = AccountGroup.objects.filter(parent__isnull=True).prefetch_related(
            Prefetch('accounts', queryset=accounts_prefetched),
            Prefetch('sub_groups__accounts', queryset=accounts_prefetched),
            'sub_groups'
        )

        totals_agg = Account.objects.aggregate(
            total_assets=Coalesce(
                Sum(Case(When(group__category='ASSET', then=F('current_balance')))),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
            total_liabilities=Coalesce(
                Sum(Case(When(group__category='LIABILITY', then=F('current_balance')))),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
            total_equity=Coalesce(
                Sum(Case(When(group__category='EQUITY', then=F('current_balance')))),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
            total_revenue=Coalesce(
                Sum(Case(When(group__category='REVENUE', then=F('current_balance')))),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
            total_expenses=Coalesce(
                Sum(Case(When(group__category__in=['DIRECT_EXPENSE', 'INDIRECT_EXPENSE'], then=F('current_balance')))),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
        )

        form = AccountForm()
        context = {
            'groups': groups,
            'form': form,
            'total_assets': totals_agg['total_assets'].quantize(Decimal('0.01')),
            'total_liabilities': totals_agg['total_liabilities'].quantize(Decimal('0.01')),
            'total_equity': totals_agg['total_equity'].quantize(Decimal('0.01')),
            'total_revenue': totals_agg['total_revenue'].quantize(Decimal('0.01')),
            'total_expenses': totals_agg['total_expenses'].quantize(Decimal('0.01')),
            'active_tab': 'coa'
        }
        return render(request, self.template_name, context)

    def post(self, request):
        action = request.POST.get('action', 'create')

        if action == 'delete':
            account_id = request.POST.get('account_id')
            account = get_object_or_404(Account, pk=account_id)

            if getattr(account, 'is_system_reserved', False):
                messages.error(request, _(f"Account '{account.name}' is a system-reserved control account and cannot be deleted."))
                return redirect('accounting:coa')

            has_transactions = JournalItem.objects.filter(account=account).exists()
            if has_transactions:
                messages.error(request, _(f"Cannot delete account '{account.name}' ({account.code}) because posted journal transactions exist."))
                return redirect('accounting:coa')

            account_name = account.name
            account.delete()
            messages.success(request, _(f"Account '{account_name}' deleted successfully."))
            return redirect('accounting:coa')

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
        qs = JournalEntry.objects.select_related(
            'branch', 'created_by', 'posted_by'
        ).prefetch_related(
            'items__account', 'items__customer', 'items__supplier'
        ).order_by('-entry_date', '-created_at')

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
            'branch', 'expense_account', 'payment_account', 'recorded_by', 'journal_entry'
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

        account = get_object_or_404(Account.objects.select_related('group', 'branch'), pk=account_id)

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
        all_accounts = Account.objects.select_related('group', 'branch').order_by('code')

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
        ).prefetch_related('statement_lines').order_by('-statement_date')[:15]

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

        form = BankReconciliationForm(request.POST)
        if form.is_valid():
            rec = form.save(commit=False)
            rec.reconciled_by = request.user
            rec.gl_book_balance = rec.bank_account.current_balance
            rec.difference = (rec.statement_ending_balance - rec.gl_book_balance).quantize(Decimal('0.01'))

            if abs(rec.difference) == Decimal('0.00'):
                rec.status = 'RECONCILED'
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

# =============================================================================
# 11. DIGITAL GATEWAY BATCH SETTLEMENT ENGINE
# =============================================================================
class GatewaySettlementView(LoginRequiredMixin, View):
    """
    Interactive Digital Gateway Settlement view:
    Transfers accumulated digital clearing balances (FonePay, eSewa, Khalti, Card POS)
    into the Primary Commercial Bank Account, accounting for gateway commission/MDR fees.
    """
    template_name = 'accounting/gateway_settlement.html'

    def get(self, request):
        branch = getattr(request, 'active_branch', None)
        clearing_tags = ['FONEPAY', 'ESEWA', 'KHALTI', 'CARD_CLEARING']
        clearing_accounts = Account.objects.filter(system_tag__in=clearing_tags).order_by('code')

        if branch:
            clearing_accounts = clearing_accounts.filter(Q(branch=branch) | Q(branch__isnull=True))

        bank_accounts = Account.objects.filter(system_tag='BANK').order_by('name')
        if branch:
            bank_accounts = bank_accounts.filter(Q(branch=branch) | Q(branch__isnull=True))

        recent_settlements = JournalEntry.objects.filter(
            source_module='GATEWAY_SETTLEMENT',
            status='POSTED'
        ).order_by('-entry_date', '-created_at')[:20]

        context = {
            'clearing_accounts': clearing_accounts,
            'bank_accounts': bank_accounts,
            'recent_settlements': recent_settlements,
            'today_date': timezone.now().date(),
            'active_tab': 'gateway_settlement'
        }
        return render(request, self.template_name, context)

    @transaction.atomic
    def post(self, request):
        branch = getattr(request, 'active_branch', None)
        gateway = request.POST.get('gateway', '').strip().upper()
        gross_amount = request.POST.get('gross_amount', '0')
        commission_fee = request.POST.get('commission_fee', '0')
        ref_number = request.POST.get('reference_number', '').strip()
        settlement_date_str = request.POST.get('settlement_date', '')

        try:
            gross = Decimal(gross_amount)
            fee = Decimal(commission_fee)
        except Exception:
            messages.error(request, _("Invalid monetary values entered for settlement."))
            return redirect('accounting:gateway_settlement')

        settlement_date = None
        if settlement_date_str:
            try:
                settlement_date = timezone.datetime.strptime(settlement_date_str, '%Y-%m-%d').date()
            except ValueError:
                settlement_date = timezone.now().date()
        else:
            settlement_date = timezone.now().date()

        try:
            voucher = AutoPostingService.post_gateway_settlement(
                branch=branch,
                gateway=gateway,
                gross_amount=gross,
                commission_fee=fee,
                settlement_date=settlement_date,
                reference_number=ref_number or None,
                user=request.user
            )
            messages.success(
                request,
                _(f"Settlement for {gateway} processed successfully! Voucher: {voucher.voucher_number}. "
                  f"Net deposit of Rs. {(gross - fee):,.2f} credited to Primary Bank.")
            )
        except Exception as err:
            messages.error(request, _(f"Failed to post gateway settlement: {err}"))

        return redirect('accounting:gateway_settlement')