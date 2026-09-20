import uuid
import logging
from decimal import Decimal, ROUND_HALF_UP
from django.db import transaction
from django.core.exceptions import ValidationError
from django.utils import timezone
from apps.pos.models import CashDrawerSession, POSHoldCart
from apps.branches.models import Branch
from apps.sales.models import SalesEstimate, SalesPaymentTransaction, SalesReturn
from apps.customers.models import CustomerUdhaariLedger
from apps.core.models import AuditLog

logger = logging.getLogger(__name__)


class POSSessionService:
    """
    Manages opening and closing shift reconciliations, cash drawer float math,
    and hold cart buffers. Correctly aggregates cash sales (deducting change returned),
    cash debt repayments, and cash return refunds.
    Automatically posts cash discrepancies (shortage or excess) to the General Ledger
    strictly inside the shift closure atomic transaction.
    """

    @classmethod
    def get_active_session(cls, user, branch: Branch):
        """Returns currently active open cash drawer session for user in branch."""
        return CashDrawerSession.objects.filter(
            cashier=user,
            branch=branch,
            status='OPEN'
        ).first()

    @classmethod
    @transaction.atomic
    def open_shift(cls, user, branch: Branch, opening_cash: Decimal, remarks: str = "") -> CashDrawerSession:
        active = cls.get_active_session(user, branch)
        if active:
            raise ValidationError(f"Shift already open for {user.username} (Session: {active.session_number}).")

        session_num = f"SES-{branch.code}-{uuid.uuid4().hex[:6].upper()}"
        session = CashDrawerSession.objects.create(
            session_number=session_num,
            branch=branch,
            cashier=user,
            opening_cash=opening_cash,
            expected_closing_cash=opening_cash,
            status='OPEN',
            remarks=remarks
        )

        AuditLog.objects.create(
            user=user,
            branch=branch,
            action_type='CREATE',
            module='POS_Session',
            object_repr=session_num,
            details={'opening_float': str(opening_cash)}
        )

        return session

    @classmethod
    @transaction.atomic
    def close_shift(cls, session: CashDrawerSession, actual_cash: Decimal, remarks: str = "", verifier=None) -> CashDrawerSession:
        """
        Closes shift, computes expected drawer cash, and registers discrepancies.
        If a shortage or surplus exists, the General Ledger discrepancy voucher is
        posted synchronously. Any failure aborts shift closure to avoid ledger drift.
        """
        if session.status == 'CLOSED':
            raise ValidationError("Session is already closed.")

        # 1. Aggregate Sales Completed during this shift
        sales = SalesEstimate.objects.filter(
            branch=session.branch,
            cashier=session.cashier,
            created_at__gte=session.opening_time,
            status='COMPLETED'
        ).prefetch_related('payment_transactions')

        total_sales_amt = Decimal('0.00')
        total_cash_sales = Decimal('0.00')
        total_digital = Decimal('0.00')
        total_credit = Decimal('0.00')
        total_change_given = Decimal('0.00')

        for est in sales:
            total_sales_amt += est.grand_total
            bill_cash_tendered = Decimal('0.00')

            for pay in est.payment_transactions.all():
                if pay.payment_mode == 'CASH':
                    bill_cash_tendered += pay.amount
                elif pay.payment_mode == 'CREDIT':
                    total_credit += pay.amount
                else:
                    total_digital += pay.amount

            # Deduct change returned on this bill from physical cash tendered
            bill_change = est.change_returned if est.change_returned else Decimal('0.00')
            net_cash_retained_on_bill = max(Decimal('0.00'), bill_cash_tendered - bill_change)
            total_cash_sales += net_cash_retained_on_bill
            total_change_given += bill_change

        # 2. Aggregate Cash Repayments for Past Customer Udhaari collected during this shift
        debt_repayments = CustomerUdhaariLedger.objects.filter(
            branch=session.branch,
            recorded_by=session.cashier,
            created_at__gte=session.opening_time,
            entry_type='CREDIT'
        )

        total_cash_debt_repayments = Decimal('0.00')
        for repayment in debt_repayments:
            if repayment.payment_mode == 'CASH':
                total_cash_debt_repayments += repayment.amount
            else:
                total_digital += repayment.amount

        # 3. Aggregate Cash Sales Returns & Refunds issued during this shift
        returns_sum = Decimal('0.00')
        returns = SalesReturn.objects.filter(
            branch=session.branch,
            processed_by=session.cashier,
            created_at__gte=session.opening_time,
            refund_mode='CASH'
        )
        for r in returns:
            returns_sum += r.total_refund_amount

        # 4. Total Expected Physical Cash in Drawer:
        # Opening Float + Net Retained Direct Cash Sales + Cash Debt Repayments - Cash Refunds
        expected_cash = (
            session.opening_cash + total_cash_sales + total_cash_debt_repayments - returns_sum
        ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        discrepancy = (actual_cash - expected_cash).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        session.closing_time = timezone.now()
        session.expected_closing_cash = expected_cash
        session.actual_closing_cash = actual_cash
        session.cash_discrepancy = discrepancy
        session.total_sales_amount = total_sales_amt
        session.total_cash_sales = total_cash_sales
        session.total_digital_sales = total_digital
        session.total_credit_sales = total_credit
        session.total_returns_amount = returns_sum
        session.status = 'CLOSED'
        session.verified_by = verifier

        closing_note = (
            f"Net Cash Sales: Rs. {total_cash_sales:.2f} (Change Given: Rs. {total_change_given:.2f}) | "
            f"Debt Cash Collected: Rs. {total_cash_debt_repayments:.2f}"
        )
        if discrepancy != Decimal('0.00'):
            status_word = "Shortage" if discrepancy < Decimal('0.00') else "Excess"
            closing_note += f" | Cash {status_word}: Rs. {abs(discrepancy):.2f}"

        if remarks:
            closing_note = f"{closing_note} | {remarks}"

        if session.remarks:
            session.remarks = f"{session.remarks} | Closing: {closing_note}".strip(' |')
        else:
            session.remarks = f"Closing: {closing_note}".strip(' |')

        session.save()

        # ---------------------------------------------------------------------
        # 5. GENERAL LEDGER INTEGRATION FOR CASH DISCREPANCIES (SHORTAGE / EXCESS)
        # ---------------------------------------------------------------------
        if session.cash_discrepancy != Decimal('0.00'):
            cls._post_cash_discrepancy_gl(session=session, user=verifier or session.cashier)

        AuditLog.objects.create(
            user=session.cashier,
            branch=session.branch,
            action_type='UPDATE',
            module='POS_Session',
            object_repr=session.session_number,
            details={
                'opening_float': str(session.opening_cash),
                'expected_cash': str(expected_cash),
                'actual_cash': str(actual_cash),
                'discrepancy': str(discrepancy),
                'net_cash_sales': str(total_cash_sales),
                'change_given_total': str(total_change_given),
                'debt_repayments_cash': str(total_cash_debt_repayments),
                'returns_cash': str(returns_sum),
                'status': 'CLOSED'
            }
        )

        return session

    @classmethod
    def _post_cash_discrepancy_gl(cls, session: CashDrawerSession, user=None) -> None:
        """
        Posts balanced double-entry vouchers for cash drawer discrepancies:
        - If negative (shortage): Debit Cash Shortage Expense, Credit Cash in Hand.
        - If positive (excess): Debit Cash in Hand, Credit Miscellaneous Operating Income (Cash Excess).

        Operates strictly inside the shift closure transaction without swallowing errors.
        """
        try:
            from apps.accounting.models import JournalEntry
            from apps.accounting.services.auto_posting import JournalEngine, AutoPostingService

            ref_doc = f"DISC-{session.session_number}"

            if JournalEntry.objects.filter(
                voucher_type='JOURNAL',
                reference_document=ref_doc,
                status='POSTED'
            ).exists():
                return

            branch = session.branch
            discrepancy = session.cash_discrepancy
            abs_amount = abs(discrepancy).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            if abs_amount <= Decimal('0.00'):
                return

            cash_acc = AutoPostingService.get_or_create_control_account(
                branch, 'CASH', '1010', 'Cash in Hand', 'ASSET', 'DEBIT'
            )

            lines = []
            if discrepancy < Decimal('0.00'):
                # Cash Shortage: Debit Expense, Credit Cash Asset
                shortage_acc = AutoPostingService.get_or_create_control_account(
                    branch, 'CASH_SHORTAGE', '5050', 'Cash Drawer Shortage Expense', 'INDIRECT_EXPENSE', 'DEBIT'
                )
                narration = f"Cash shortage on shift close {session.session_number} ({session.cashier.username})"
                lines.append({
                    'account': shortage_acc,
                    'debit': abs_amount,
                    'credit': Decimal('0.00'),
                    'narration': narration
                })
                lines.append({
                    'account': cash_acc,
                    'debit': Decimal('0.00'),
                    'credit': abs_amount,
                    'narration': f"Cash drawer shortage reconciliation for session {session.session_number}"
                })
                voucher_narration = f"Cash Drawer Shortage: Session {session.session_number} (Shortage: Rs. {abs_amount:.2f})"

            else:
                # Cash Excess: Debit Cash Asset, Credit Miscellaneous Revenue
                excess_acc = AutoPostingService.get_or_create_control_account(
                    branch, 'MISC_INCOME', '4040', 'Cash Drawer Excess & Surplus Income', 'REVENUE', 'CREDIT'
                )
                narration = f"Cash surplus on shift close {session.session_number} ({session.cashier.username})"
                lines.append({
                    'account': cash_acc,
                    'debit': abs_amount,
                    'credit': Decimal('0.00'),
                    'narration': f"Cash drawer surplus recognized for session {session.session_number}"
                })
                lines.append({
                    'account': excess_acc,
                    'debit': Decimal('0.00'),
                    'credit': abs_amount,
                    'narration': narration
                })
                voucher_narration = f"Cash Drawer Excess / Surplus: Session {session.session_number} (Excess: Rs. {abs_amount:.2f})"

            JournalEngine.create_balanced_entry(
                voucher_type='JOURNAL',
                date_ad=timezone.now().date(),
                branch=branch,
                lines=lines,
                narration=voucher_narration,
                reference_doc=ref_doc,
                user=user or session.cashier,
                auto_post=True
            )
        except ValidationError:
            raise
        except Exception as err:
            logger.error(f"[POS Session GL Discrepancy Error] Session {session.session_number}: {err}", exc_info=True)
            raise ValidationError(
                f"Failed to post General Ledger voucher for cash discrepancy on session {session.session_number}: {err}"
            ) from err