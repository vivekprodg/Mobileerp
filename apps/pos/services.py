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
    and hold cart buffers. Correctly aggregates cash sales (deducting change returned
    and surplus trade-in buy-back cash payouts), cash debt repayments, and cash return refunds.

    Strictly validates payment channels during debt repayments to prevent internal
    non-monetary store credit and trade-in ledger adjustments from falsely contaminating
    digital sales metrics (FonePay, eSewa, Khalti, Card, Bank).

    Automatically posts cash discrepancies (shortage or excess) to the General Ledger
    strictly inside the shift closure atomic transaction using standardized account codes
    (1110 for Cash in Hand, 5050 for Shortage Expense, and 4040 for Surplus Income).
    """

    # Authoritative set of verified digital and banking collection channels
    DIGITAL_PAYMENT_MODES = {
        'FONEPAY', 'ESEWA', 'KHALTI', 'CARD', 'POS', 'POS_CARD',
        'BANK', 'BANK_TRANSFER', 'CONNECT_IPS', 'CONNECTIPS', 'CHEQUE', 'QR'
    }

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
        Correctly accounts for:
        1. Net physical cash from sales = Total Cash Tendered - Total Change Given
           (which mathematically includes surplus cash paid out to walk-in trade-in customers).
        2. Real customer debt repayments collected via CASH and verified DIGITAL channels,
           strictly excluding non-monetary store credit adjustments ('OTHER', 'ADJUSTMENT').
        3. Cash refunds paid out on Sales Returns.
        Synchronously posts balanced GL discrepancy vouchers on shortages or excesses.
        """
        if session.status == 'CLOSED':
            raise ValidationError("Session is already closed.")

        # ---------------------------------------------------------------------
        # 1. Aggregate Sales Completed during this shift
        # ---------------------------------------------------------------------
        sales = SalesEstimate.objects.filter(
            branch=session.branch,
            cashier=session.cashier,
            created_at__gte=session.opening_time,
            status='COMPLETED'
        ).prefetch_related('payment_transactions')

        total_sales_amt = Decimal('0.00')
        total_cash_tendered = Decimal('0.00')
        total_digital = Decimal('0.00')
        total_credit = Decimal('0.00')
        total_change_given = Decimal('0.00')
        total_trade_in_cash_payouts = Decimal('0.00')

        for est in sales:
            total_sales_amt += est.grand_total

            for pay in est.payment_transactions.all():
                mode = pay.payment_mode.upper().strip()
                if mode == 'CASH':
                    total_cash_tendered += pay.amount
                elif mode == 'CREDIT':
                    total_credit += pay.amount
                elif mode in cls.DIGITAL_PAYMENT_MODES:
                    total_digital += pay.amount

            # Track total change handed to customer on this bill
            bill_change = est.change_returned if est.change_returned else Decimal('0.00')
            total_change_given += bill_change

            # Track surplus cash payouts from trade-in exchanges for auditing
            if est.has_trade_in_exchange and not est.customer_id and est.trade_in_discount_amount > est.grand_total:
                surplus_cash = (est.trade_in_discount_amount - est.grand_total).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                total_trade_in_cash_payouts += surplus_cash

        # Net cash retained in drawer from sales transactions:
        # Cash Tendered into drawer minus All Change & Trade-In Cash Payouts leaving drawer
        net_cash_sales = (total_cash_tendered - total_change_given).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        # ---------------------------------------------------------------------
        # 2. Aggregate Repayments for Past Customer Udhaari (With Anti-Contamination Guard)
        # ---------------------------------------------------------------------
        debt_repayments = CustomerUdhaariLedger.objects.filter(
            branch=session.branch,
            recorded_by=session.cashier,
            created_at__gte=session.opening_time,
            entry_type='CREDIT'
        )

        total_cash_debt_repayments = Decimal('0.00')
        total_digital_debt_repayments = Decimal('0.00')
        total_non_monetary_adjustments = Decimal('0.00')

        for repayment in debt_repayments:
            mode = (repayment.payment_mode or '').upper().strip()
            if mode == 'CASH':
                total_cash_debt_repayments += repayment.amount
            elif mode in cls.DIGITAL_PAYMENT_MODES:
                total_digital += repayment.amount
                total_digital_debt_repayments += repayment.amount
            else:
                # Internal store credit, trade-in surplus offset, or manual balance adjustment
                # (e.g., 'OTHER', 'ADJUSTMENT', 'STORE_CREDIT').
                # These do NOT represent real incoming funds and are strictly excluded from digital sales.
                total_non_monetary_adjustments += repayment.amount

        # ---------------------------------------------------------------------
        # 3. Aggregate Cash Sales Returns & Refunds issued during this shift
        # ---------------------------------------------------------------------
        returns_sum = Decimal('0.00')
        returns = SalesReturn.objects.filter(
            branch=session.branch,
            processed_by=session.cashier,
            created_at__gte=session.opening_time,
            refund_mode='CASH'
        )
        for r in returns:
            returns_sum += r.total_refund_amount

        # ---------------------------------------------------------------------
        # 4. Total Expected Physical Cash in Drawer:
        # Opening Float + Net Retained Cash from Sales + Cash Debt Repayments - Cash Refunds
        # ---------------------------------------------------------------------
        expected_cash = (
            session.opening_cash + net_cash_sales + total_cash_debt_repayments - returns_sum
        ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        discrepancy = (actual_cash - expected_cash).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        session.closing_time = timezone.now()
        session.expected_closing_cash = expected_cash
        session.actual_closing_cash = actual_cash
        session.cash_discrepancy = discrepancy
        session.total_sales_amount = total_sales_amt
        session.total_cash_sales = net_cash_sales
        session.total_digital_sales = total_digital
        session.total_credit_sales = total_credit
        session.total_returns_amount = returns_sum
        session.status = 'CLOSED'
        session.verified_by = verifier

        closing_note = (
            f"Cash In: Rs. {total_cash_tendered:.2f} | Change/Surplus Out: Rs. {total_change_given:.2f} "
            f"(Trade-In Payouts: Rs. {total_trade_in_cash_payouts:.2f}) | "
            f"Net Sales Cash: Rs. {net_cash_sales:.2f} | Debt Cash: Rs. {total_cash_debt_repayments:.2f} | "
            f"Digital Collections: Rs. {total_digital:.2f}"
        )
        if total_non_monetary_adjustments > Decimal('0.00'):
            closing_note += f" | Store Credit Adjustments: Rs. {total_non_monetary_adjustments:.2f}"

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
        # 5. General Ledger Integration for Cash Discrepancies (Shortage / Excess)
        # Standardized to Account 1110 (Cash in Hand), 5050 (Shortage), 4040 (Surplus)
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
                'cash_tendered_sales': str(total_cash_tendered),
                'change_given_total': str(total_change_given),
                'trade_in_surplus_cash_paid': str(total_trade_in_cash_payouts),
                'net_cash_sales': str(net_cash_sales),
                'debt_repayments_cash': str(total_cash_debt_repayments),
                'debt_repayments_digital': str(total_digital_debt_repayments),
                'debt_adjustments_non_cash': str(total_non_monetary_adjustments),
                'total_digital_sales': str(total_digital),
                'returns_cash': str(returns_sum),
                'status': 'CLOSED'
            }
        )

        return session

    @classmethod
    def _post_cash_discrepancy_gl(cls, session: CashDrawerSession, user=None) -> None:
        """
        Posts balanced double-entry vouchers for cash drawer discrepancies:
        - If negative (shortage): Debit Cash Shortage Expense (5050), Credit Cash in Hand (1110).
        - If positive (excess): Debit Cash in Hand (1110), Credit Miscellaneous Operating Income (4040).
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
                branch, 'CASH', '1110', 'Cash in Hand (Main Drawer)', 'ASSET', 'DEBIT'
            )

            lines = []
            if discrepancy < Decimal('0.00'):
                # Cash Shortage: Debit Expense (5050), Credit Cash Asset (1110)
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
                # Cash Excess: Debit Cash Asset (1110), Credit Miscellaneous Revenue (4040)
                excess_acc = AutoPostingService.get_or_create_control_account(
                    branch, 'CASH_SURPLUS', '4040', 'Cash Drawer Excess & Surplus Income', 'REVENUE', 'CREDIT'
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