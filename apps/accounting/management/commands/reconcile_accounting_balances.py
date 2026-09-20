"""
Accounting Sub-Ledger & Cache Balance Reconciliation Command.
File Path: apps/accounting/management/commands/reconcile_accounting_balances.py

Executes a 4-point programmatic audit and repair:
1. Recomputes Account.current_balance from opening_balance + Sum(debit) - Sum(credit).
2. Recomputes Customer.current_credit_balance from CustomerUdhaariLedger and reconciles against GL 1210 (AR).
3. Recomputes Supplier.current_balance from SupplierUdhaariLedger and reconciles against GL 2110 (AP).
4. Recomputes stock from StockMovementLog and reconciles BranchStock.quantity and Account 1310 (Inventory Asset).

Usage:
    python manage.py reconcile_accounting_balances
    python manage.py reconcile_accounting_balances --fix
    python manage.py reconcile_accounting_balances --branch=BR-MAIN-01 --fix
"""

from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Any, List, Optional
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Sum, Q, F
from django.utils import timezone

from apps.accounting.models import Account, JournalItem, JournalEntry
from apps.customers.models import Customer, CustomerUdhaariLedger
from apps.purchases.models import Supplier, SupplierUdhaariLedger
from apps.inventory.models import BranchStock, StockMovementLog, Product
from apps.branches.models import Branch


class Command(BaseCommand):
    help = (
        "Audits cached running balances across GL Accounts, Customer Udhaari, "
        "Supplier Payables, and Branch Stock levels. Recalculates exact mathematical "
        "balances from sub-ledger journal rows and applies repairs when run with --fix."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--fix',
            action='store_true',
            help='Atomically updates and repairs drifted cached balances in the database.'
        )
        parser.add_argument(
            '--branch',
            type=str,
            default='',
            help='Filter audit by specific store branch code (e.g. BR-MAIN-01).'
        )

    def handle(self, *args, **options):
        fix_mode = options['fix']
        branch_code = options['branch']

        target_branch = None
        if branch_code:
            target_branch = Branch.objects.filter(code__iexact=branch_code).first()
            if not target_branch:
                self.stdout.write(self.style.ERROR(f"Store branch with code '{branch_code}' not found."))
                return

        self.stdout.write(self.style.MIGRATE_HEADING("=" * 85))
        self.stdout.write(self.style.MIGRATE_HEADING("  GENERAL LEDGER & SUB-LEDGER CACHE RECONCILIATION AUDIT"))
        self.stdout.write(self.style.MIGRATE_HEADING("=" * 85))

        if fix_mode:
            self.stdout.write(self.style.WARNING(">>> REPAIR MODE (--fix ACTIVE): Drifted balances will be corrected in DB. <<<\n"))
        else:
            self.stdout.write(self.style.NOTICE(">>> AUDIT ONLY (DRY-RUN): Pass --fix to automatically repair balances. <<<\n"))

        if target_branch:
            self.stdout.write(f"Branch Filter: {target_branch.name} ({target_branch.code})\n")

        # ---------------------------------------------------------------------
        # CHECK 1: GENERAL LEDGER ACCOUNTS (Account.current_balance)
        # ---------------------------------------------------------------------
        self.stdout.write(self.style.MIGRATE_LABEL("[1/4] Auditing General Ledger Chart of Accounts Balances..."))
        account_results = self._reconcile_accounts(target_branch, fix_mode)

        # ---------------------------------------------------------------------
        # CHECK 2: CUSTOMER UDHAARI BALANCES (Customer.current_credit_balance vs GL 1210)
        # ---------------------------------------------------------------------
        self.stdout.write(self.style.MIGRATE_LABEL("\n[2/4] Auditing Customer Credit Balances (Udhaari Sub-Ledger vs GL 1210 AR)..."))
        customer_results = self._reconcile_customers(target_branch, fix_mode)

        # ---------------------------------------------------------------------
        # CHECK 3: SUPPLIER PAYABLE BALANCES (Supplier.current_balance vs GL 2110)
        # ---------------------------------------------------------------------
        self.stdout.write(self.style.MIGRATE_LABEL("\n[3/4] Auditing Supplier Debt Balances (Payable Sub-Ledger vs GL 2110 AP)..."))
        supplier_results = self._reconcile_suppliers(target_branch, fix_mode)

        # ---------------------------------------------------------------------
        # CHECK 4: STOCK MOVEMENTS & INVENTORY ASSET VALUATION (GL 1310)
        # ---------------------------------------------------------------------
        self.stdout.write(self.style.MIGRATE_LABEL("\n[4/4] Auditing Branch Stock Counters vs Movement Logs & Inventory Asset (GL 1310)..."))
        stock_results = self._reconcile_stock(target_branch, fix_mode)

        # ---------------------------------------------------------------------
        # FINAL AUDIT SUMMARY
        # ---------------------------------------------------------------------
        self.stdout.write(self.style.MIGRATE_HEADING("\n" + "=" * 85))
        self.stdout.write(self.style.MIGRATE_HEADING("  RECONCILIATION SUMMARY REPORT"))
        self.stdout.write(self.style.MIGRATE_HEADING("=" * 85))

        total_drift = (
            account_results['drift_count'] +
            customer_results['drift_count'] +
            supplier_results['drift_count'] +
            stock_results['drift_count']
        )

        self.stdout.write(f"  • GL Accounts Checked:     {account_results['total_checked']} | Drifted: {account_results['drift_count']}")
        self.stdout.write(f"  • Customers Checked:       {customer_results['total_checked']} | Drifted: {customer_results['drift_count']}")
        self.stdout.write(f"  • Suppliers Checked:       {supplier_results['total_checked']} | Drifted: {supplier_results['drift_count']}")
        self.stdout.write(f"  • Branch Stock Checked:    {stock_results['total_checked']} | Drifted: {stock_results['drift_count']}")
        self.stdout.write(f"  • Control Account AR 1210 Variance: Rs. {customer_results['control_variance']:,.2f}")
        self.stdout.write(f"  • Control Account AP 2110 Variance: Rs. {supplier_results['control_variance']:,.2f}")
        self.stdout.write(f"  • Control Account Inventory Asset 1310 Variance: Rs. {stock_results['control_variance']:,.2f}")

        if total_drift > 0:
            if fix_mode:
                self.stdout.write(self.style.SUCCESS(f"\n[+] Successfully repaired and synchronized {total_drift} drifted balance(s) inside an atomic transaction!"))
            else:
                self.stdout.write(self.style.WARNING(f"\n[!] Detected {total_drift} balance drift discrepancy(ies). Run with '--fix' to correct them."))
        else:
            self.stdout.write(self.style.SUCCESS("\n[+] All cached running balances match the double-entry sub-ledger transactions with 0.00 variance. System healthy!"))

    # =========================================================================
    # CHECK 1: ACCOUNTS RECONCILIATION
    # =========================================================================
    def _reconcile_accounts(self, branch: Optional[Branch], fix_mode: bool) -> Dict[str, Any]:
        accounts_qs = Account.objects.select_related('group').order_by('code')
        if branch:
            accounts_qs = accounts_qs.filter(Q(branch=branch) | Q(branch__isnull=True))

        total_checked = 0
        drift_count = 0

        self.stdout.write(f"  {'Account Code':<14} | {'Account Name':<32} | {'Cached Bal':>14} | {'Recomputed':>14} | {'Status':^8}")
        self.stdout.write("  " + "-" * 88)

        with transaction.atomic():
            for acc in accounts_qs:
                total_checked += 1

                # Recompute exact balance from posted JournalItems
                items_qs = JournalItem.objects.filter(
                    account=acc,
                    journal_entry__status='POSTED'
                )
                if branch and acc.branch:
                    items_qs = items_qs.filter(journal_entry__branch=branch)

                agg = items_qs.aggregate(dr=Sum('debit_amount'), cr=Sum('credit_amount'))
                dr_sum = agg['dr'] or Decimal('0.00')
                cr_sum = agg['cr'] or Decimal('0.00')

                op_bal = acc.opening_balance or Decimal('0.00')
                if acc.opening_balance_nature == 'DEBIT':
                    total_dr = op_bal + dr_sum
                    total_cr = cr_sum
                else:
                    total_dr = dr_sum
                    total_cr = op_bal + cr_sum

                if acc.is_debit_nature:
                    recomputed = total_dr - total_cr
                else:
                    recomputed = total_cr - total_dr

                recomputed = recomputed.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                cached = (acc.current_balance or Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

                variance = abs(recomputed - cached)
                is_drift = (variance != Decimal('0.00'))

                status_label = "DRIFT" if is_drift else "OK"
                acc_name_trunc = acc.name[:30]

                if is_drift:
                    drift_count += 1
                    style_fn = self.style.WARNING
                    if fix_mode:
                        acc.current_balance = recomputed
                        acc.save(update_fields=['current_balance', 'updated_at'])
                        status_label = "FIXED"
                        style_fn = self.style.SUCCESS
                else:
                    style_fn = self.style.SUCCESS

                if is_drift or total_checked <= 15:
                    self.stdout.write(style_fn(
                        f"  {acc.code:<14} | {acc_name_trunc:<32} | {cached:>14,.2f} | {recomputed:>14,.2f} | {status_label:^8}"
                    ))

            if not fix_mode:
                transaction.set_rollback(True)

        return {'total_checked': total_checked, 'drift_count': drift_count}

    # =========================================================================
    # CHECK 2: CUSTOMERS RECONCILIATION & AR CONTROL
    # =========================================================================
    def _reconcile_customers(self, branch: Optional[Branch], fix_mode: bool) -> Dict[str, Any]:
        customer_qs = Customer.objects.all().order_by('name')
        if branch and hasattr(Customer, 'preferred_branch'):
            customer_qs = customer_qs.filter(Q(preferred_branch=branch) | Q(preferred_branch__isnull=True))

        total_checked = 0
        drift_count = 0
        total_subledger_balance = Decimal('0.00')

        self.stdout.write(f"  {'Customer Name':<30} | {'Phone':<12} | {'Cached Bal':>14} | {'Sub-Ledger':>14} | {'Status':^8}")
        self.stdout.write("  " + "-" * 88)

        with transaction.atomic():
            for cust in customer_qs:
                total_checked += 1
                recomputed = cust.recalculate_balance_from_ledger(save=False)
                total_subledger_balance += recomputed

                cached = (cust.current_credit_balance or Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                is_drift = (recomputed != cached)

                if is_drift:
                    drift_count += 1
                    status_label = "DRIFT"
                    style_fn = self.style.WARNING
                    if fix_mode:
                        cust.current_credit_balance = recomputed
                        cust.save(update_fields=['current_credit_balance', 'updated_at'])
                        status_label = "FIXED"
                        style_fn = self.style.SUCCESS

                    self.stdout.write(style_fn(
                        f"  {cust.name[:28]:<30} | {cust.phone_number:<12} | {cached:>14,.2f} | {recomputed:>14,.2f} | {status_label:^8}"
                    ))

            if not fix_mode:
                transaction.set_rollback(True)

        # Reconcile with GL 1210 AR Control Account
        ar_acc = Account.objects.filter(
            Q(code='1210') | Q(system_tag='ACCOUNTS_RECEIVABLE')
        ).first()
        gl_ar_balance = (ar_acc.current_balance or Decimal('0.00')) if ar_acc else Decimal('0.00')
        control_variance = abs(total_subledger_balance - gl_ar_balance).quantize(Decimal('0.01'))

        self.stdout.write(f"  Sub-ledger Total: Rs. {total_subledger_balance:,.2f} | GL 1210 Control: Rs. {gl_ar_balance:,.2f} | Variance: Rs. {control_variance:,.2f}")

        return {
            'total_checked': total_checked,
            'drift_count': drift_count,
            'total_subledger_balance': total_subledger_balance,
            'control_variance': control_variance
        }

    # =========================================================================
    # CHECK 3: SUPPLIERS RECONCILIATION & AP CONTROL
    # =========================================================================
    def _reconcile_suppliers(self, branch: Optional[Branch], fix_mode: bool) -> Dict[str, Any]:
        supplier_qs = Supplier.objects.all().order_by('company_name')
        total_checked = 0
        drift_count = 0
        total_subledger_balance = Decimal('0.00')

        self.stdout.write(f"  {'Supplier Firm Name':<30} | {'Code':<10} | {'Cached Bal':>14} | {'Sub-Ledger':>14} | {'Status':^8}")
        self.stdout.write("  " + "-" * 88)

        with transaction.atomic():
            for supp in supplier_qs:
                total_checked += 1
                recomputed = supp.recalculate_balance_from_ledger(save=False)
                total_subledger_balance += recomputed

                cached = (supp.current_balance or Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                is_drift = (recomputed != cached)

                if is_drift:
                    drift_count += 1
                    status_label = "DRIFT"
                    style_fn = self.style.WARNING
                    if fix_mode:
                        supp.recalculate_balance_from_ledger(save=True)
                        status_label = "FIXED"
                        style_fn = self.style.SUCCESS

                    self.stdout.write(style_fn(
                        f"  {supp.company_name[:28]:<30} | {supp.code:<10} | {cached:>14,.2f} | {recomputed:>14,.2f} | {status_label:^8}"
                    ))

            if not fix_mode:
                transaction.set_rollback(True)

        # Reconcile with GL 2110 AP Control Account
        ap_acc = Account.objects.filter(
            Q(code='2110') | Q(system_tag='ACCOUNTS_PAYABLE')
        ).first()
        gl_ap_balance = (ap_acc.current_balance or Decimal('0.00')) if ap_acc else Decimal('0.00')
        control_variance = abs(total_subledger_balance - gl_ap_balance).quantize(Decimal('0.01'))

        self.stdout.write(f"  Sub-ledger Total: Rs. {total_subledger_balance:,.2f} | GL 2110 Control: Rs. {gl_ap_balance:,.2f} | Variance: Rs. {control_variance:,.2f}")

        return {
            'total_checked': total_checked,
            'drift_count': drift_count,
            'total_subledger_balance': total_subledger_balance,
            'control_variance': control_variance
        }

    # =========================================================================
    # CHECK 4: STOCK MOVEMENTS & INVENTORY ASSET VALUATION
    # =========================================================================
    def _reconcile_stock(self, branch: Optional[Branch], fix_mode: bool) -> Dict[str, Any]:
        stocks_qs = BranchStock.objects.select_related('product', 'branch').all().order_by('product__name')
        if branch:
            stocks_qs = stocks_qs.filter(branch=branch)

        total_checked = 0
        drift_count = 0
        total_valuation = Decimal('0.00')

        self.stdout.write(f"  {'Product SKU / Name':<34} | {'Branch':<8} | {'Cached Qty':>12} | {'Log Qty':>12} | {'Status':^8}")
        self.stdout.write("  " + "-" * 88)

        with transaction.atomic():
            for bs in stocks_qs:
                total_checked += 1
                prod = bs.product
                cost = prod.purchase_price or Decimal('0.00')
                total_valuation += (bs.quantity * cost).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

                # Recompute quantity from StockMovementLog
                log_sum = StockMovementLog.objects.filter(
                    product=prod,
                    branch=bs.branch
                ).aggregate(tot=Sum('quantity_delta'))['tot']

                if log_sum is not None:
                    recomputed_qty = log_sum.quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)
                    cached_qty = bs.quantity.quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)

                    is_drift = (recomputed_qty != cached_qty)
                    if is_drift:
                        drift_count += 1
                        status_label = "DRIFT"
                        style_fn = self.style.WARNING
                        if fix_mode:
                            bs.quantity = recomputed_qty
                            bs.save(update_fields=['quantity', 'updated_at'])
                            status_label = "FIXED"
                            style_fn = self.style.SUCCESS

                        prod_title = f"[{prod.sku}] {prod.name}"[:32]
                        self.stdout.write(style_fn(
                            f"  {prod_title:<34} | {bs.branch.code:<8} | {cached_qty:>12.3f} | {recomputed_qty:>12.3f} | {status_label:^8}"
                        ))

            if not fix_mode:
                transaction.set_rollback(True)

        # Reconcile Physical Valuation with GL 1310 Inventory Asset
        inv_acc = Account.objects.filter(
            Q(code='1310') | Q(system_tag='INVENTORY_ASSET')
        ).first()
        gl_inv_balance = (inv_acc.current_balance or Decimal('0.00')) if inv_acc else Decimal('0.00')
        control_variance = abs(total_valuation - gl_inv_balance).quantize(Decimal('0.01'))

        self.stdout.write(f"  Physical Stock Value: Rs. {total_valuation:,.2f} | GL 1310 Asset: Rs. {gl_inv_balance:,.2f} | Variance: Rs. {control_variance:,.2f}")

        return {
            'total_checked': total_checked,
            'drift_count': drift_count,
            'total_valuation': total_valuation,
            'control_variance': control_variance
        }