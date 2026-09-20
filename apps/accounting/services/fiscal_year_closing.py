"""
Fiscal Year Financial Close & Retained Earnings Transfer Engine.
File Path: apps/accounting/services/fiscal_year_closing.py

Executes formal Year-End Financial Close conforming to standard accounting procedures:
1. Mathematical Verification:
   - Validates Trial Balance equality: abs(Sum(Dr) - Sum(Cr)) == 0.00.
   - Executes verify_accounting_integrity(); rejects closing if any critical control check fails.
2. Temporary P&L Accounts Zeroing:
   - Calculates net balances of all Revenue accounts (Credits) and Expense/COGS accounts (Debits) for the year.
   - Generates an automated Closing Journal Entry:
     * Debits each Revenue account for its net credit balance (bringing balance to zero).
     * Credits each Expense/COGS account for its net debit balance (bringing balance to zero).
     * Balances the voucher by Crediting 3210 Retained Earnings (if net profit) or Debiting 3210 (if net loss).
3. Audit Lock:
   - Sets AccountingFiscalYear.is_closed = True, records closed_at timestamp, and closed_by user.
   - Locks all 12 monthly FinancialPeriod records.
4. Permanent Balance Sheet Carry Forward:
   - Evaluates permanent Balance Sheet accounts (Assets, Liabilities, Equity).
   - Initializes or updates next fiscal year opening balances.
5. Supports Preview / Simulation Mode:
   - Allows accountants to review exact closing vouchers and figures before committing.
"""

from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime
from typing import Dict, Any, List, Optional, Tuple

from django.db import transaction
from django.db.models import Sum, Q
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.accounting.models import (
    Account, AccountGroup, JournalEntry, JournalItem,
    AccountingFiscalYear, FinancialPeriod
)
from apps.branches.models import Branch
from apps.accounting.services.auto_posting import JournalEngine, AutoPostingService
from apps.accounting.services.financial_statements import FinancialStatementService
from apps.core.nepali_calendar import NepaliCalendar
from apps.core.models import AuditLog


class FiscalYearClosingService:
    """
    Authoritative Year-End Closing Engine for Nepali Fiscal Years.
    """

    @classmethod
    def simulate_fiscal_year_closing(
        cls,
        fiscal_year_name: str,
        branch: Optional[Branch] = None
    ) -> Dict[str, Any]:
        """
        Simulates year-end close without altering database records or posting vouchers.
        Returns expected profit/loss, lines to be closed, and integrity diagnostic results.
        """
        fy = AccountingFiscalYear.objects.filter(name=fiscal_year_name).first()
        if not fy:
            raise ValidationError(f"Fiscal Year '{fiscal_year_name}' not found in accounting registry.")

        if fy.is_closed:
            raise ValidationError(f"Fiscal Year '{fiscal_year_name}' is already audited and closed.")

        # Run integrity audit
        audit_report = FinancialStatementService.verify_accounting_integrity(
            branch=branch,
            fiscal_year=fiscal_year_name
        )

        pnl_data = FinancialStatementService.get_profit_and_loss(
            branch=branch,
            fiscal_year=fiscal_year_name
        )

        tb_data = FinancialStatementService.get_trial_balance(
            branch=branch,
            fiscal_year=fiscal_year_name
        )

        closing_lines_preview, total_rev, total_exp, net_profit = cls._calculate_closing_lines(
            fy=fy,
            branch=branch
        )

        next_fy_name = cls._get_next_fiscal_year_name(fiscal_year_name)

        return {
            'fiscal_year': fiscal_year_name,
            'is_already_closed': fy.is_closed,
            'audit_status': audit_report['overall_status'],
            'trial_balance_balanced': tb_data['is_balanced'],
            'discrepancies': audit_report['discrepancies'],
            'total_revenue': total_rev,
            'total_expenses': total_exp,
            'net_profit': net_profit,
            'is_net_profit': (net_profit >= Decimal('0.00')),
            'closing_lines_count': len(closing_lines_preview),
            'closing_lines_preview': closing_lines_preview,
            'next_fiscal_year': next_fy_name,
            'can_close': (audit_report['overall_status'] != 'FAIL' and tb_data['is_balanced'])
        }

    @classmethod
    @transaction.atomic
    def close_fiscal_year(
        cls,
        fiscal_year_name: str,
        user=None,
        branch: Optional[Branch] = None
    ) -> Dict[str, Any]:
        """
        Executes the formal, atomic Year-End Financial Close.
        Fails fast if integrity checks or Trial Balance equality fail.
        """
        fy = AccountingFiscalYear.objects.select_for_update().filter(name=fiscal_year_name).first()
        if not fy:
            raise ValidationError(f"Accounting Fiscal Year '{fiscal_year_name}' not found.")

        if fy.is_closed:
            raise ValidationError(f"Fiscal Year '{fiscal_year_name}' is already closed and locked.")

        # ---------------------------------------------------------------------
        # STEP 1: AUDIT INTEGRITY PRE-CHECK
        # ---------------------------------------------------------------------
        audit_report = FinancialStatementService.verify_accounting_integrity(
            branch=branch,
            fiscal_year=fiscal_year_name
        )
        if audit_report['overall_status'] == 'FAIL':
            err_msg = "; ".join(audit_report['discrepancies'])
            raise ValidationError(
                f"Year-End Close REJECTED: Accounting integrity audit detected critical failures: {err_msg}"
            )

        tb_data = FinancialStatementService.get_trial_balance(
            branch=branch,
            fiscal_year=fiscal_year_name
        )
        if not tb_data['is_balanced']:
            raise ValidationError(
                f"Year-End Close REJECTED: Trial Balance is out of balance by Rs. {tb_data['discrepancy']:,.2f}. "
                "Double-entry bookkeeping requires absolute equality before period closure."
            )

        # ---------------------------------------------------------------------
        # STEP 2: CALCULATE CLOSING JOURNAL LINES (ZERO OUT P&L ACCOUNTS)
        # ---------------------------------------------------------------------
        closing_lines, total_rev, total_exp, net_profit = cls._calculate_closing_lines(
            fy=fy,
            branch=branch
        )

        target_branch = branch or Branch.objects.filter(is_main_branch=True).first() or Branch.objects.first()

        # ---------------------------------------------------------------------
        # STEP 3: POST CLOSING JOURNAL ENTRY TO RETAINED EARNINGS (3210)
        # ---------------------------------------------------------------------
        closing_voucher = None
        if closing_lines:
            closing_voucher = JournalEngine.create_balanced_entry(
                voucher_type='JOURNAL',
                date_ad=fy.end_date_ad,
                branch=target_branch,
                lines=closing_lines,
                narration=(
                    f"Fiscal Year {fy.name} Year-End Closing Entry: Net "
                    f"{'Profit' if net_profit >= Decimal('0.00') else 'Loss'} of "
                    f"Rs. {abs(net_profit):,.2f} transferred to Retained Earnings (3210)."
                ),
                reference_doc=f"CLOSE-{fy.name}",
                source_module='YEAR_END_CLOSING',
                source_id=str(fy.id),
                user=user,
                auto_post=True
            )

        # ---------------------------------------------------------------------
        # STEP 4: LOCK FISCAL YEAR & ALL 12 FINANCIAL PERIODS
        # ---------------------------------------------------------------------
        fy.is_closed = True
        fy.closed_at = timezone.now()
        fy.closed_by = user
        fy.save(update_fields=['is_closed', 'closed_at', 'closed_by', 'updated_at'])

        FinancialPeriod.objects.filter(fiscal_year=fy).update(
            is_closed=True,
            updated_at=timezone.now()
        )

        # ---------------------------------------------------------------------
        # STEP 5: INITIALIZE NEXT FISCAL YEAR & CARRY FORWARD BALANCE SHEET
        # ---------------------------------------------------------------------
        next_fy_name = cls._get_next_fiscal_year_name(fy.name)
        next_fy = cls._ensure_next_fiscal_year_initialized(next_fy_name)
        carried_accounts_count = cls._carry_forward_balance_sheet(fy, next_fy, branch)

        # ---------------------------------------------------------------------
        # STEP 6: IMMUTABLE AUDIT LOGGING
        # ---------------------------------------------------------------------
        AuditLog.objects.create(
            user=user,
            branch=target_branch,
            action_type='UPDATE',
            module='FiscalYearClosing',
            object_repr=f"Close FY {fy.name}",
            details={
                'fiscal_year': fy.name,
                'closing_voucher_no': closing_voucher.voucher_number if closing_voucher else 'None',
                'net_profit': str(net_profit),
                'total_revenue_closed': str(total_rev),
                'total_expenses_closed': str(total_exp),
                'accounts_closed': len(closing_lines) - 1 if closing_lines else 0,
                'next_fiscal_year': next_fy_name,
                'carried_accounts_count': carried_accounts_count
            }
        )

        return {
            'status': 'SUCCESS',
            'fiscal_year': fy.name,
            'closing_voucher': closing_voucher,
            'net_profit': net_profit,
            'total_revenue_closed': total_rev,
            'total_expenses_closed': total_exp,
            'next_fiscal_year': next_fy_name,
            'carried_accounts_count': carried_accounts_count,
            'message': (
                f"Fiscal Year {fy.name} has been closed and locked successfully. "
                f"Net {'Profit' if net_profit >= Decimal('0.00') else 'Loss'} of "
                f"Rs. {abs(net_profit):,.2f} transferred to Retained Earnings."
            )
        }

    # =========================================================================
    # INTERNAL CALCULATION HELPERS
    # =========================================================================
    @classmethod
    def _calculate_closing_lines(
        cls,
        fy: AccountingFiscalYear,
        branch: Optional[Branch]
    ) -> Tuple[List[Dict[str, Any]], Decimal, Decimal, Decimal]:
        """
        Inspects all P&L temporary accounts (Revenue, Direct Expense, Indirect Expense)
        in the fiscal year and builds offsetting double-entry lines to zero their balances.
        """
        pnl_accounts = Account.objects.filter(
            group__category__in=['REVENUE', 'DIRECT_EXPENSE', 'INDIRECT_EXPENSE']
        ).select_related('group').order_by('code')

        if branch:
            pnl_accounts = pnl_accounts.filter(Q(branch=branch) | Q(branch__isnull=True))

        lines: List[Dict[str, Any]] = []
        total_revenue = Decimal('0.00')
        total_expenses = Decimal('0.00')

        for acc in pnl_accounts:
            items_qs = JournalItem.objects.filter(
                account=acc,
                journal_entry__status='POSTED',
                journal_entry__entry_date__gte=fy.start_date_ad,
                journal_entry__entry_date__lte=fy.end_date_ad
            )
            if branch and acc.branch:
                items_qs = items_qs.filter(journal_entry__branch=branch)

            agg = items_qs.aggregate(dr=Sum('debit_amount'), cr=Sum('credit_amount'))
            dr = agg['dr'] or Decimal('0.00')
            cr = agg['cr'] or Decimal('0.00')

            category = acc.group.category

            if category == 'REVENUE':
                # Normal credit balance = cr - dr
                net_cr = (cr - dr).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                if net_cr > Decimal('0.00'):
                    total_revenue += net_cr
                    # Debit revenue to bring to zero
                    lines.append({
                        'account': acc,
                        'debit': net_cr,
                        'credit': Decimal('0.00'),
                        'narration': f"Close revenue account {acc.code} for FY {fy.name}"
                    })
                elif net_cr < Decimal('0.00'):
                    # Debit balance in revenue (e.g. returns > sales): Credit to zero
                    total_revenue += net_cr
                    lines.append({
                        'account': acc,
                        'debit': Decimal('0.00'),
                        'credit': abs(net_cr),
                        'narration': f"Close debit-balance revenue account {acc.code} for FY {fy.name}"
                    })

            else:
                # Normal debit balance = dr - cr (DIRECT_EXPENSE, INDIRECT_EXPENSE)
                net_dr = (dr - cr).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                if net_dr > Decimal('0.00'):
                    total_expenses += net_dr
                    # Credit expense to bring to zero
                    lines.append({
                        'account': acc,
                        'debit': Decimal('0.00'),
                        'credit': net_dr,
                        'narration': f"Close expense account {acc.code} for FY {fy.name}"
                    })
                elif net_dr < Decimal('0.00'):
                    # Credit balance in expense: Debit to zero
                    total_expenses += net_dr
                    lines.append({
                        'account': acc,
                        'debit': abs(net_dr),
                        'credit': Decimal('0.00'),
                        'narration': f"Close credit-balance expense account {acc.code} for FY {fy.name}"
                    })

        net_profit = (total_revenue - total_expenses).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        # Balance the entry with Retained Earnings (3210)
        target_branch = branch or Branch.objects.filter(is_main_branch=True).first() or Branch.objects.first()
        re_acc = AutoPostingService.get_or_create_control_account(
            branch=target_branch,
            system_tag='RETAINED_EARNINGS',
            default_code='3210',
            default_name='Retained Earnings',
            group_category='EQUITY',
            nature='CREDIT'
        )

        if net_profit > Decimal('0.00'):
            # Net Profit: Credit Retained Earnings
            lines.append({
                'account': re_acc,
                'debit': Decimal('0.00'),
                'credit': net_profit,
                'narration': f"Net Profit for FY {fy.name} transferred to Retained Earnings"
            })
        elif net_profit < Decimal('0.00'):
            # Net Loss: Debit Retained Earnings
            lines.append({
                'account': re_acc,
                'debit': abs(net_profit),
                'credit': Decimal('0.00'),
                'narration': f"Net Loss for FY {fy.name} absorbed by Retained Earnings"
            })

        return lines, total_revenue, total_expenses, net_profit

    @staticmethod
    def _get_next_fiscal_year_name(fy_name: str) -> str:
        clean = fy_name.replace('-', '/').strip()
        parts = clean.split('/')
        start_year = int(parts[0])
        next_start = start_year + 1
        next_short = str(next_start + 1)[-2:]
        return f"{next_start}/{next_short}"

    @classmethod
    def _ensure_next_fiscal_year_initialized(cls, next_fy_name: str) -> AccountingFiscalYear:
        next_fy = AccountingFiscalYear.objects.filter(name=next_fy_name).first()
        if not next_fy:
            start_ad, end_ad, start_bs, end_bs = NepaliCalendar.get_fiscal_year_range(next_fy_name)
            next_fy = AccountingFiscalYear.objects.create(
                name=next_fy_name,
                start_date_ad=start_ad,
                end_date_ad=end_ad,
                start_date_bs=start_bs,
                end_date_bs=end_bs,
                is_closed=False
            )

        # Ensure all 12 financial periods exist for next fiscal year
        base_year = int(next_fy_name.split('/')[0])
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
                fiscal_year=next_fy,
                period_number=month_idx,
                defaults={
                    'period_name_en': f"{m_name_en} ({next_fy_name})",
                    'period_name_np': f"{m_name_np} ({next_fy_name})",
                    'start_date_ad': s_ad,
                    'end_date_ad': e_ad,
                    'start_date_bs': s_bs,
                    'end_date_bs': e_bs,
                    'is_closed': False
                }
            )

        return next_fy

    @classmethod
    def _carry_forward_balance_sheet(
        cls,
        closed_fy: AccountingFiscalYear,
        next_fy: AccountingFiscalYear,
        branch: Optional[Branch]
    ) -> int:
        """
        Calculates closing cumulative ending balances for permanent Balance Sheet accounts
        (Asset, Liability, Equity) as of the closed fiscal year and updates them.
        """
        bs_accounts = Account.objects.filter(
            group__category__in=['ASSET', 'LIABILITY', 'EQUITY']
        ).select_related('group')

        if branch:
            bs_accounts = bs_accounts.filter(Q(branch=branch) | Q(branch__isnull=True))

        updated_count = 0
        for acc in bs_accounts:
            items_qs = JournalItem.objects.filter(
                account=acc,
                journal_entry__status='POSTED',
                journal_entry__entry_date__lte=closed_fy.end_date_ad
            )
            if branch and acc.branch:
                items_qs = items_qs.filter(journal_entry__branch=branch)

            agg = items_qs.aggregate(dr=Sum('debit_amount'), cr=Sum('credit_amount'))
            dr = agg['dr'] or Decimal('0.00')
            cr = agg['cr'] or Decimal('0.00')

            op_bal = acc.opening_balance or Decimal('0.00')
            if acc.opening_balance_nature == 'DEBIT':
                dr += op_bal
            else:
                cr += op_bal

            if acc.is_debit_nature:
                closing_val = (dr - cr).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            else:
                closing_val = (cr - dr).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            # Store the carried-forward balance as the active opening balance
            if closing_val >= Decimal('0.00'):
                acc.opening_balance = closing_val
                acc.opening_balance_nature = 'DEBIT' if acc.is_debit_nature else 'CREDIT'
            else:
                acc.opening_balance = abs(closing_val)
                acc.opening_balance_nature = 'CREDIT' if acc.is_debit_nature else 'DEBIT'

            acc.save(update_fields=['opening_balance', 'opening_balance_nature', 'updated_at'])
            updated_count += 1

        return updated_count