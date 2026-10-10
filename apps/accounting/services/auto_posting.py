"""
Double-Entry Journal Posting Engine & Automated Operational Dispatcher.

Core Architectural Features & VAT Audit Linkage:
1. Strict VAT Entry-to-Document Traceability (Audit Drilldown Invariant):
   - Every Output VAT (Account 2210) and Input VAT (Account 1410) journal line maintains
     an explicit, bidirectional link back to the originating source document:
       Output VAT 13% Line (2210 Cr / Dr)
           ↓
       Journal Entry (reference_document = INV-..., source_module = POS_SALE, source_id = ID)
           ↓
       Sales Invoice / Estimation Slip / Sales Return Voucher
   
       Input VAT 13% Line (1410 Dr / Cr)
           ↓
       Journal Entry (reference_document = GRN-..., source_module = PURCHASE_GRN, source_id = ID)
           ↓
       Inward Goods Received Note / Supplier Bill / Purchase Return Debit Note

   - Every VAT JournalItem line:
     * Stores the explicit counterparty relationship (Customer on Sales/Returns, Supplier on GRN/Returns).
     * Includes an audit-standard line narration containing document numbers, party PAN,
       taxable base amount, and invoice cross-references.
     * Enables end-to-end drilldowns: VAT Register Report ↔ Source Document ↔ General Ledger Entry.

2. Strict GRN Inward Double-Entry Synchronization (Nepal Tax Standard):
   - Debit: Merchandise Inventory Asset (Account 1310 at Landed Cost = Pre-VAT Base + All 5 Overheads:
     Freight, Customs Duty, Handling & Unloading, Transit Insurance, and Other Overheads).
   - Debit: Dedicated 13% Input VAT (Account 1410 for Input VAT claimable on supplier tax bill).
   - Credit: Cash/Bank/Wallet for spot cash payments made to merchandise supplier upon delivery.
   - Credit: Accounts Payable (Account 2110) strictly for the merchandise supplier's remaining debt (Net Invoice - Paid).
   - Credit: Cash in Hand (1110) or Freight/Logistics Clearing (2160) for the 5-tier shipping overheads.
     Strictly isolates third-party logistics overheads from the merchandise supplier's debt ledger.
   - Enforces absolute mathematical equality: Sum(Debits) == Sum(Credits) with penny rounding reconciliation.

3. Three-Way Sales Tax Split & Trade-In Clearing Alignment:
   - Dr: Genuine Monetary Payment Modes (Cash, FonePay, eSewa, Khalti, Card, Bank) for paid collections.
   - Dr: Customer Accounts Receivable (1210) strictly for the remaining unpaid Udhaari due debt.
   - Dr: Trade-In Buy-Back Clearing (Account 2150) for the FULL buy-back valuation of the traded-in device.
   - Dr: Sales Discount Allowed (6170) if commercial concessions were granted.
   - Cr: Cash in Hand (1110) if physical cash change was returned to a customer or trade-in surplus cash paid to walk-in customer.
   - Cr: Customer Accounts Receivable (1210) for surplus trade-in buy-back credit deposited into a registered customer profile.
   - Cr: Sales Revenue Account (4110) (Taxable Base + Non-Taxable / Exempt Base).
   - Cr: Output VAT 13% Account (2210) (Output VAT Collected with document and customer linkage).
   - COGS / Inventory Asset: Relieved at landed cost (skipped if cost == 0.00 for historical migrations).

4. Sales & Purchase Returns VAT Reversal Synchronizer (Verified Multi-Attribute Linkage):
   - Sales Return: Debits Output VAT (Account 2210) by the exact return VAT amount obtained from
     verified header and line-item snapshots, linking back to the return voucher and original sales invoice.
     (Voucher Type: CREDIT_NOTE, Source Module: SALES_RETURN, Source ID: sales_return.id, Reference: sales_return.return_number).
   - Purchase Return: Credits Input VAT (Account 1410) by the exact debit note tax amount, linking
     back to the debit note voucher, supplier PAN, and original GRN/bill reference.
     (Voucher Type: DEBIT_NOTE, Source Module: PURCHASE_RETURN, Source ID: purchase_return.id, Reference: purchase_return.return_number).

5. Non-Monetary Tender Isolation (Double-Accounting Prevention):
   - In post_sales_estimate, 'CREDIT' and 'UDHAARI' tenders inside the payment loop are skipped.
   - The dedicated estimate.due_amount handler creates the single authoritative debit to AR (1210).
   - In post_customer_payment, non-monetary store credit / trade-in adjustments are shielded from posting fake cash receipts.

6. Historical Backdating Integrity & Fiscal Year Lock Enforcement:
   - post_sales_estimate strictly stamps vouchers with estimate.bill_date_ad.
   - post_grn_receipt strictly stamps vouchers with grn.bill_date.
   - post_customer_payment and post_supplier_payment read the true historical payment date
     from the subledger (entry_date / entry_date_bs) rather than using today's creation timestamp.
   - Rejects any transaction where the date falls into an audited, closed fiscal year
     while allowing back-dated postings within the active open fiscal year (2083/84).

7. Omnichannel Payment Ledger Routing:
   - Cash (1110), Bank (1120), FonePay (1130), eSewa (1140), Khalti (1150), Card POS (1160),
     AR (1210), AP (2110), Trade-In Clearing (2150), Freight Clearing (2160).
"""

import uuid
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from datetime import date, datetime
from typing import List, Dict, Any, Optional

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.core.exceptions import ValidationError

from apps.accounting.models import (
    Account, AccountGroup, JournalEntry, JournalItem,
    AccountingFiscalYear, FinancialPeriod
)
from apps.sales.models import SalesEstimate, SalesReturn, SalesPaymentTransaction
from apps.purchases.models import GoodsReceivedNote, PurchaseReturn, SupplierUdhaariLedger, Supplier
from apps.customers.models import Customer, CustomerUdhaariLedger
from apps.branches.models import Branch, BranchDocumentSequence
from apps.core.nepali_calendar import NepaliCalendar
from apps.core.utils.nepali_date_converter import parse_bs_date_components

# =============================================================================
# 1. CORE DOUBLE-ENTRY JOURNAL ENGINE
# =============================================================================
class JournalEngine:
    """
    Authoritative double-entry transaction engine for all general ledger postings.
    Guarantees Sum(Debits) == Sum(Credits) and protects closed fiscal periods.
    """

    @staticmethod
    def generate_voucher_number(branch: Branch, voucher_type: str) -> str:
        """
        Atomically generates unique sequential voucher numbers.
        Example: JV-BR01-000001, SV-BR01-000045, PUV-BR01-000012, CN-BR01-000008, DN-BR01-000004
        """
        prefix_map = {
            'JOURNAL': f"JV-{branch.code}",
            'PAYMENT': f"PV-{branch.code}",
            'RECEIPT': f"RV-{branch.code}",
            'CONTRA': f"CV-{branch.code}",
            'SALES': f"SV-{branch.code}",
            'PURCHASE': f"PUV-{branch.code}",
            'CREDIT_NOTE': f"CN-{branch.code}",
            'DEBIT_NOTE': f"DN-{branch.code}",
        }
        prefix = prefix_map.get(voucher_type, f"VCH-{branch.code}")
        try:
            return BranchDocumentSequence.get_next_sequence_number(
                branch=branch,
                document_type=f"ACC_{voucher_type}",
                prefix_override=prefix,
                padding=6
            )
        except Exception:
            return f"{prefix}-{uuid.uuid4().hex[:6].upper()}"

    @classmethod
    @transaction.atomic
    def create_balanced_entry(
        cls,
        voucher_type: str,
        date_ad: Optional[date],
        branch: Branch,
        lines: List[Dict[str, Any]],
        narration: str,
        reference_doc: Optional[str] = None,
        source_module: Optional[str] = None,
        source_id: Optional[str] = None,
        user=None,
        auto_post: bool = True
    ) -> JournalEntry:
        """
        Main transactional creator for balanced double-entry vouchers.
        """
        if not lines:
            raise ValidationError("Cannot create an empty journal voucher without line items.")

        if date_ad is None:
            date_ad = timezone.now().date()
        elif isinstance(date_ad, datetime):
            date_ad = date_ad.date()

        # 1. Resolve Nepali BS Date & Fiscal Year
        bs_year, bs_month, bs_day = NepaliCalendar.ad_to_bs(date_ad)
        date_bs_str = NepaliCalendar.format_bs(bs_year, bs_month, bs_day, lang='en')
        fiscal_year_str = NepaliCalendar.get_fiscal_year(bs_year, bs_month)

        # 2. Check if Fiscal Year or Financial Period is Locked / Closed
        locked_fy = AccountingFiscalYear.objects.filter(name=fiscal_year_str, is_closed=True).first()
        if locked_fy:
            active_open_fy = AccountingFiscalYear.objects.filter(is_closed=False).order_by('-start_date_ad').first()
            open_name = active_open_fy.name if active_open_fy else "an active fiscal year (2083/84)"
            raise ValidationError(
                f"Financial posting rejected: Voucher date ({date_bs_str} BS / {date_ad} AD) belongs to "
                f"Fiscal Year {fiscal_year_str}, which is audited and closed. Only entries within {open_name} are permitted."
            )

        locked_period = FinancialPeriod.objects.filter(
            fiscal_year__name=fiscal_year_str,
            start_date_ad__lte=date_ad,
            end_date_ad__gte=date_ad,
            is_closed=True
        ).first()
        if locked_period:
            raise ValidationError(
                f"Financial posting rejected: Financial period '{locked_period.period_name_en}' is closed."
            )

        # 3. Validate Mathematical Balance: Sum(Debits) == Sum(Credits)
        sum_debit = Decimal('0.00')
        sum_credit = Decimal('0.00')
        cleaned_lines = []

        for idx, line in enumerate(lines):
            acc = line.get('account')
            if not acc:
                raise ValidationError(f"Line {idx + 1}: General Ledger Account is required.")

            if not isinstance(acc, Account):
                acc = Account.objects.select_for_update().get(pk=acc)

            raw_dr = line.get('debit', Decimal('0.00')) or Decimal('0.00')
            raw_cr = line.get('credit', Decimal('0.00')) or Decimal('0.00')

            dr = Decimal(str(raw_dr)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            cr = Decimal(str(raw_cr)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            if dr < Decimal('0.00') or cr < Decimal('0.00'):
                raise ValidationError(f"Line {idx + 1} ({acc.name}): Negative debit or credit values are prohibited.")

            if dr == Decimal('0.00') and cr == Decimal('0.00'):
                continue

            if dr > Decimal('0.00') and cr > Decimal('0.00'):
                raise ValidationError(f"Line {idx + 1} ({acc.name}): Cannot specify both Debit and Credit on the same line.")

            sum_debit += dr
            sum_credit += cr

            cleaned_lines.append({
                'account': acc,
                'debit': dr,
                'credit': cr,
                'customer': line.get('customer'),
                'supplier': line.get('supplier'),
                'narration': line.get('narration', '')
            })

        if not cleaned_lines:
            raise ValidationError("Journal entry must contain at least one non-zero debit or credit line.")

        discrepancy = abs(sum_debit - sum_credit)
        if discrepancy != Decimal('0.00'):
            raise ValidationError(
                f"Unbalanced Journal Voucher! Total Debit (Rs. {sum_debit:.2f}) does not match "
                f"Total Credit (Rs. {sum_credit:.2f}). Variance: Rs. {discrepancy:.2f}. "
                f"Double-entry bookkeeping mandates absolute mathematical equality."
            )

        # 4. Generate Voucher Number & Header
        voucher_number = cls.generate_voucher_number(branch, voucher_type)
        entry_status = 'POSTED' if auto_post else 'DRAFT'

        entry_kwargs = {
            'voucher_number': voucher_number,
            'voucher_type': voucher_type,
            'branch': branch,
            'entry_date': date_ad,
            'entry_date_bs': date_bs_str,
            'fiscal_year': fiscal_year_str,
            'reference_document': reference_doc,
            'narration': narration,
            'total_debit': sum_debit,
            'total_credit': sum_credit,
            'status': entry_status,
            'created_by': user,
            'posted_by': user if auto_post else None,
            'posted_at': timezone.now() if auto_post else None
        }

        je_field_names = {f.name for f in JournalEntry._meta.get_fields()}
        if 'source_module' in je_field_names and source_module:
            entry_kwargs['source_module'] = source_module
        if 'source_id' in je_field_names and source_id:
            entry_kwargs['source_id'] = str(source_id)

        journal_entry = JournalEntry.objects.create(**entry_kwargs)

        # 5. Save Line Items & Update Master Running Balances Atomically
        for item_data in cleaned_lines:
            acc = item_data['account']
            dr = item_data['debit']
            cr = item_data['credit']

            JournalItem.objects.create(
                journal_entry=journal_entry,
                account=acc,
                debit_amount=dr,
                credit_amount=cr,
                customer=item_data['customer'],
                supplier=item_data['supplier'],
                line_narration=item_data['narration'] or None
            )

            if auto_post:
                locked_acc = Account.objects.select_for_update().get(pk=acc.pk)
                is_debit = getattr(locked_acc, 'is_debit_nature', None)
                if is_debit is None:
                    is_debit = (getattr(locked_acc.group, 'nature', 'DEBIT') == 'DEBIT')

                current_bal = locked_acc.current_balance or Decimal('0.00')
                if is_debit:
                    locked_acc.current_balance = current_bal + dr - cr
                else:
                    locked_acc.current_balance = current_bal + cr - dr

                acc_fields = {f.name for f in Account._meta.get_fields()}
                update_fields = ['current_balance']
                if 'updated_at' in acc_fields:
                    update_fields.append('updated_at')

                locked_acc.save(update_fields=update_fields)

        return journal_entry

# =============================================================================
# 2. AUTOMATIC POSTING DISPATCHER SERVICE
# =============================================================================
class AutoPostingService:
    """
    Dispatches automated double-entry vouchers for retail and wholesale store transactions.
    Supports multi-channel digital wallet isolation, gateway settlement batches, and strict audit tags.
    """

    @classmethod
    def get_or_create_control_account(
        cls,
        branch: Branch,
        system_tag: str,
        default_code: str,
        default_name: str,
        group_category: str,
        nature: str
    ) -> Account:
        """
        Finds the exact GL control account scoped to this branch or organization-wide;
        auto-initializes it if not present.
        """
        acc = None
        if system_tag and system_tag != 'NONE':
            acc = Account.objects.filter(
                system_tag=system_tag
            ).filter(Q(branch=branch) | Q(branch__isnull=True)).first()

        if not acc and default_code:
            acc = Account.objects.filter(
                Q(code=default_code) | Q(code__startswith=f"{default_code}-")
            ).filter(Q(branch=branch) | Q(branch__isnull=True)).first()

        if not acc:
            group = AccountGroup.objects.filter(category=group_category).first()
            if not group:
                group = AccountGroup.objects.create(
                    code=default_code[:2] + "00",
                    name=f"{group_category.title()} Group",
                    category=group_category,
                    nature=nature,
                    is_system_reserved=True
                )

            create_kwargs = {
                'code': f"{default_code}-{branch.code}",
                'name': f"{default_name} ({branch.code})",
                'group': group,
                'branch': branch,
                'system_tag': system_tag,
                'is_system_reserved': True
            }

            account_fields = {f.name for f in Account._meta.get_fields()}
            if 'nature' in account_fields:
                create_kwargs['nature'] = nature
            if 'is_debit_nature' in account_fields:
                create_kwargs['is_debit_nature'] = (nature == 'DEBIT')

            acc = Account.objects.create(**create_kwargs)

        return acc

    @classmethod
    def resolve_payment_account(
        cls,
        branch: Branch,
        payment_mode: str,
        for_party: str = 'CUSTOMER'
    ) -> Account:
        """
        Maps payment modes to exact GL asset/clearing accounts:
        - CASH -> 1110 Cash in Hand
        - FONEPAY / QR -> 1130 FonePay Clearing
        - ESEWA -> 1140 eSewa Clearing
        - KHALTI -> 1150 Khalti Clearing
        - CARD -> 1160 POS Card Clearing
        - BANK / IPS / CHEQUE -> 1120 Primary Bank
        - CREDIT / UDHAARI -> 1210 AR (Customer) / 2110 AP (Supplier)
        """
        mode = str(payment_mode or '').strip().upper()

        if mode in ['CASH']:
            return cls.get_or_create_control_account(
                branch, 'CASH', '1110', 'Cash in Hand (Main Drawer)', 'ASSET', 'DEBIT'
            )
        elif mode in ['FONEPAY', 'QR', 'DYNAMIC_QR', 'FONE_PAY', 'MERCHANT_QR']:
            return cls.get_or_create_control_account(
                branch, 'FONEPAY', '1130', 'FonePay / QR Settlement Clearing', 'ASSET', 'DEBIT'
            )
        elif mode in ['ESEWA', 'E_SEWA']:
            return cls.get_or_create_control_account(
                branch, 'ESEWA', '1140', 'eSewa / Digital Wallet Clearing', 'ASSET', 'DEBIT'
            )
        elif mode in ['KHALTI']:
            return cls.get_or_create_control_account(
                branch, 'KHALTI', '1150', 'Khalti / Digital Wallet Clearing', 'ASSET', 'DEBIT'
            )
        elif mode in ['CARD', 'CREDIT_CARD', 'DEBIT_CARD', 'POS', 'CARD_SWIPE', 'POS_CARD']:
            return cls.get_or_create_control_account(
                branch, 'CARD_CLEARING', '1160', 'POS Card Settlement Clearing', 'ASSET', 'DEBIT'
            )
        elif mode in ['BANK', 'BANK_TRANSFER', 'CHEQUE', 'CONNECT_IPS', 'CONNECTIPS', 'IBFT', 'WIRE', 'DIRECT_TRANSFER']:
            return cls.get_or_create_control_account(
                branch, 'BANK', '1120', 'Primary Bank Current Account', 'ASSET', 'DEBIT'
            )
        elif mode in ['CREDIT', 'UDHAARI', 'ON_CREDIT', 'DUE']:
            if for_party.upper() == 'SUPPLIER':
                return cls.get_or_create_control_account(
                    branch, 'ACCOUNTS_PAYABLE', '2110', 'Accounts Payable (Trade Creditors)', 'LIABILITY', 'CREDIT'
                )
            else:
                return cls.get_or_create_control_account(
                    branch, 'ACCOUNTS_RECEIVABLE', '1210', 'Accounts Receivable (Trade Debtors)', 'ASSET', 'DEBIT'
                )
        else:
            if 'FONE' in mode or 'QR' in mode:
                return cls.get_or_create_control_account(branch, 'FONEPAY', '1130', 'FonePay / QR Settlement Clearing', 'ASSET', 'DEBIT')
            elif 'ESEWA' in mode:
                return cls.get_or_create_control_account(branch, 'ESEWA', '1140', 'eSewa / Digital Wallet Clearing', 'ASSET', 'DEBIT')
            elif 'KHALTI' in mode:
                return cls.get_or_create_control_account(branch, 'KHALTI', '1150', 'Khalti / Digital Wallet Clearing', 'ASSET', 'DEBIT')
            elif 'CARD' in mode:
                return cls.get_or_create_control_account(branch, 'CARD_CLEARING', '1160', 'POS Card Settlement Clearing', 'ASSET', 'DEBIT')
            elif 'CASH' in mode:
                return cls.get_or_create_control_account(branch, 'CASH', '1110', 'Cash in Hand (Main Drawer)', 'ASSET', 'DEBIT')
            else:
                return cls.get_or_create_control_account(branch, 'BANK', '1120', 'Primary Bank Current Account', 'ASSET', 'DEBIT')

    # =========================================================================
    # 1. POS SALES CHECKOUT POSTING (3-WAY VAT SPLIT & TRADE-IN ALIGNMENT)
    # =========================================================================
    @classmethod
    @transaction.atomic
    def post_sales_estimate(cls, estimate: SalesEstimate, user=None, **kwargs) -> Optional[JournalEntry]:
        """
        Creates a balanced double-entry voucher for a finalized Sales POS Invoice.
        Strictly uses estimate.bill_date_ad and validates against closed fiscal years.
        
        VAT Entry Linkage:
        - Output VAT 13% (Account 2210) is credited for `estimate.vat_amount`.
        - The VAT JournalItem line attaches `customer=estimate.customer` and records an
          audit-standard narration:
          `Output VAT 13% [Doc: <estimate_number>] (Recipient: <name>, Taxable Base: Rs. <amount>)`
        - JournalEntry establishes the document link via:
          `reference_document=estimate.estimate_number`, `source_module='POS_SALE'`, `source_id=str(estimate.id)`
        """
        source_module = 'POS_SALE'
        source_id = str(estimate.id)

        duplicate_filter = Q(source_module__in=['SALES', 'POS_SALE'], source_id=source_id, status='POSTED')
        duplicate_filter |= Q(voucher_type='SALES', reference_document=estimate.estimate_number, status='POSTED')
        existing = JournalEntry.objects.filter(duplicate_filter).first()
        if existing:
            return existing

        branch = estimate.branch

        # Strictly assign historical bill_date_ad
        raw_date_ad = estimate.bill_date_ad or timezone.now().date()
        date_ad = raw_date_ad.date() if isinstance(raw_date_ad, datetime) else raw_date_ad

        # Fiscal Year Lock Guard: Reject audited/closed fiscal years
        bs_y, bs_m, bs_d = NepaliCalendar.ad_to_bs(date_ad)
        fy_name = estimate.fiscal_year or NepaliCalendar.get_fiscal_year(bs_y, bs_m)
        locked_fy = AccountingFiscalYear.objects.filter(name=fy_name, is_closed=True).first()
        if locked_fy:
            active_open_fy = AccountingFiscalYear.objects.filter(is_closed=False).order_by('-start_date_ad').first()
            open_name = active_open_fy.name if active_open_fy else "the active fiscal year (2083/84)"
            raise ValidationError(
                f"GL Posting Rejected: Bill {estimate.estimate_number} is dated {date_ad} (FY {fy_name}), "
                f"which is audited and closed. Only transactions within {open_name} are permitted."
            )

        lines: List[Dict[str, Any]] = []

        inv_asset_acc = cls.get_or_create_control_account(
            branch, 'INVENTORY_ASSET', '1310', 'Merchandise Inventory Asset', 'ASSET', 'DEBIT'
        )
        ar_acc = cls.get_or_create_control_account(
            branch, 'ACCOUNTS_RECEIVABLE', '1210', 'Accounts Receivable (Trade Debtors)', 'ASSET', 'DEBIT'
        )
        cash_acc = cls.get_or_create_control_account(
            branch, 'CASH', '1110', 'Cash in Hand (Main Drawer)', 'ASSET', 'DEBIT'
        )
        rev_acc = cls.get_or_create_control_account(
            branch, 'SALES_REVENUE', '4110', 'Merchandise Sales Revenue', 'REVENUE', 'CREDIT'
        )
        trade_in_clearing_acc = cls.get_or_create_control_account(
            branch, 'TRADE_IN_CLEARING', '2150', 'Trade-In Buy-Back Clearing / Payable', 'LIABILITY', 'CREDIT'
        )

        # ---------------------------------------------------------------------
        # 1. DEBIT: Genuine Monetary Payment Settlements
        # ---------------------------------------------------------------------
        raw_payments = (
            kwargs.get('payment_details') or
            kwargs.get('payments') or
            kwargs.get('payment_transactions')
        )
        if raw_payments is None:
            raw_payments = estimate.payment_transactions.all()

        has_recorded_payments = False

        for p in raw_payments:
            if isinstance(p, dict):
                p_mode = str(p.get('mode') or p.get('payment_mode') or '').strip().upper()
                raw_amt = p.get('amount', 0)
                ref = str(p.get('transaction_ref') or p.get('reference') or p.get('trace_id') or '').strip() or '-'
                display_mode = p.get('mode') or p.get('payment_mode') or p_mode
            else:
                p_mode = str(getattr(p, 'payment_mode', '') or '').strip().upper()
                raw_amt = getattr(p, 'amount', 0)
                ref = str(getattr(p, 'transaction_ref', '') or '').strip() or '-'
                display_mode = p.get_payment_mode_display() if hasattr(p, 'get_payment_mode_display') else p_mode

            try:
                p_amount = Decimal(str(raw_amt or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            except (InvalidOperation, ValueError, TypeError):
                p_amount = Decimal('0.00')

            if p_amount <= Decimal('0.00'):
                continue

            # Skip non-monetary CREDIT / UDHAARI to prevent double-debiting AR 1210
            if p_mode in ['CREDIT', 'UDHAARI', 'ON_CREDIT', 'DUE']:
                continue

            # Skip Trade-In here as it is handled authoritatively in Section 2
            if p_mode in ['TRADE_IN', 'EXCHANGE']:
                continue

            has_recorded_payments = True
            target_acc = cls.resolve_payment_account(branch, p_mode, for_party='CUSTOMER')
            lines.append({
                'account': target_acc,
                'debit': p_amount,
                'credit': Decimal('0.00'),
                'customer': estimate.customer,
                'narration': f"Sale collection ({display_mode}) [Doc: {estimate.estimate_number}] - Ref: {ref}"
            })

        if not has_recorded_payments:
            if estimate.paid_amount > Decimal('0.00'):
                lines.append({
                    'account': cash_acc,
                    'debit': estimate.paid_amount,
                    'credit': Decimal('0.00'),
                    'customer': estimate.customer,
                    'narration': f"Cash sale on {estimate.estimate_number}"
                })

        # Customer Udhaari Debt (Single Authoritative Debit to 1210 Accounts Receivable)
        if estimate.due_amount > Decimal('0.00'):
            lines.append({
                'account': ar_acc,
                'debit': estimate.due_amount,
                'credit': Decimal('0.00'),
                'customer': estimate.customer,
                'narration': f"Customer Udhaari on estimate {estimate.estimate_number}"
            })

        # ---------------------------------------------------------------------
        # 2. DEBIT: Trade-In Buy-Back Tender Settlement & Surplus Allocation
        # ---------------------------------------------------------------------
        if estimate.has_trade_in_exchange and estimate.trade_in_discount_amount > Decimal('0.00'):
            trade_in_val = estimate.trade_in_discount_amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            voucher_ref = estimate.trade_in_voucher_reference or "EXCHANGE"

            # 1. Full agreed buy-back value of the old device MUST debit Trade-In Buy-Back Clearing (2150)
            lines.append({
                'account': trade_in_clearing_acc,
                'debit': trade_in_val,
                'credit': Decimal('0.00'),
                'customer': estimate.customer,
                'narration': f"Trade-In buy-back voucher {voucher_ref} applied against bill {estimate.estimate_number}"
            })

            # 2. Check if old phone valuation exceeds the new merchandise bill
            if trade_in_val > estimate.grand_total:
                excess_trade_in = (trade_in_val - estimate.grand_total).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

                if estimate.customer_id:
                    # Registered customer: Credit Customer Accounts Receivable (1210) as store credit / advance liability
                    # Never routes surplus trade-in credit through Cash or Bank accounts!
                    lines.append({
                        'account': ar_acc,
                        'debit': Decimal('0.00'),
                        'credit': excess_trade_in,
                        'customer': estimate.customer,
                        'narration': f"Surplus trade-in buy-back credit deposited as store credit from voucher {voucher_ref}"
                    })
                else:
                    # Walk-in customer: Physical cash change paid out from drawer float.
                    # Handled below in change_returned (which credits Cash in Hand 1110).
                    pass

        # ---------------------------------------------------------------------
        # 3. CREDIT: Cash Change Returned (Normal Change & Walk-In Trade-In Surplus Cash)
        # ---------------------------------------------------------------------
        if estimate.change_returned > Decimal('0.00'):
            lines.append({
                'account': cash_acc,
                'debit': Decimal('0.00'),
                'credit': estimate.change_returned,
                'narration': f"Cash change returned on bill {estimate.estimate_number}"
            })

        # ---------------------------------------------------------------------
        # 4. DEBIT: Merchandise Sales Discounts Allowed
        # ---------------------------------------------------------------------
        total_disc = estimate.total_sales_discount
        if total_disc > Decimal('0.00'):
            disc_acc = cls.get_or_create_control_account(
                branch, 'SALES_DISCOUNT', '6170', 'POS Sales Discounts Allowed', 'INDIRECT_EXPENSE', 'DEBIT'
            )
            lines.append({
                'account': disc_acc,
                'debit': total_disc,
                'credit': Decimal('0.00'),
                'narration': f"Commercial sales discount on estimate {estimate.estimate_number}"
            })

        # ---------------------------------------------------------------------
        # 5. CREDIT: Sales Revenue & 13% Output VAT (AUDIT-LINKED TRACEABILITY)
        # ---------------------------------------------------------------------
        if estimate.is_vat_applicable and estimate.vat_amount > Decimal('0.00'):
            pre_tax_base = (estimate.taxable_amount + estimate.non_taxable_amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            if pre_tax_base <= Decimal('0.00'):
                pre_tax_base = max(Decimal('0.00'), estimate.subtotal - estimate.vat_amount)

            revenue_credit = (pre_tax_base + total_disc).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            lines.append({
                'account': rev_acc,
                'debit': Decimal('0.00'),
                'credit': revenue_credit,
                'narration': f"Sales revenue (Taxable Base) on {estimate.estimate_number}"
            })

            vat_acc = cls.get_or_create_control_account(
                branch, 'OUTPUT_VAT', '2210', 'Output VAT 13%', 'LIABILITY', 'CREDIT'
            )

            # Reliable VAT-to-Document Linkage Narration
            cust_pan_str = (
                f", PAN: {estimate.customer_pan}" if estimate.customer_pan else (
                    f", PAN: {estimate.customer.pan_number}" if estimate.customer and estimate.customer.pan_number else ""
                )
            )
            vat_narration = (
                f"13% Output VAT [Doc: {estimate.estimate_number}] "
                f"(Recipient: {estimate.recipient_display_name}{cust_pan_str}, Taxable Base: Rs. {pre_tax_base:,.2f})"
            )

            lines.append({
                'account': vat_acc,
                'debit': Decimal('0.00'),
                'credit': estimate.vat_amount,
                'customer': estimate.customer,
                'narration': vat_narration
            })
        else:
            revenue_credit = estimate.subtotal
            if total_disc > Decimal('0.00') and revenue_credit < (estimate.grand_total + total_disc):
                revenue_credit = estimate.grand_total + total_disc

            lines.append({
                'account': rev_acc,
                'debit': Decimal('0.00'),
                'credit': revenue_credit,
                'narration': f"Sales revenue for estimate {estimate.estimate_number}"
            })

        # ---------------------------------------------------------------------
        # 6. Penny Rounding Residual Reconciliation
        # ---------------------------------------------------------------------
        sum_dr = sum(l['debit'] for l in lines)
        sum_cr = sum(l['credit'] for l in lines)
        diff = sum_dr - sum_cr
        if Decimal('0.00') < abs(diff) <= Decimal('0.05'):
            for l in lines:
                if l['account'] == rev_acc and l['credit'] > Decimal('0.00'):
                    l['credit'] += diff
                    break

        # ---------------------------------------------------------------------
        # 7. COGS & Inventory Asset Relief (Skipped if cost == 0.00)
        # ---------------------------------------------------------------------
        cogs_amount = estimate.total_cost_amount or Decimal('0.00')
        if cogs_amount > Decimal('0.00'):
            cogs_acc = cls.get_or_create_control_account(
                branch, 'COGS', '5110', 'Cost of Goods Sold (COGS)', 'DIRECT_EXPENSE', 'DEBIT'
            )
            lines.append({
                'account': cogs_acc,
                'debit': cogs_amount,
                'credit': Decimal('0.00'),
                'narration': f"COGS realized on estimate {estimate.estimate_number}"
            })
            lines.append({
                'account': inv_asset_acc,
                'debit': Decimal('0.00'),
                'credit': cogs_amount,
                'narration': f"Inventory asset relief at landed cost for {estimate.estimate_number}"
            })

        narration = f"POS Sales Finalization for Slip {estimate.estimate_number} ({estimate.recipient_display_name})"
        return JournalEngine.create_balanced_entry(
            voucher_type='SALES',
            date_ad=date_ad,
            branch=branch,
            lines=lines,
            narration=narration,
            reference_doc=estimate.estimate_number,
            source_module=source_module,
            source_id=source_id,
            user=user or estimate.cashier,
            auto_post=True
        )

    # =========================================================================
    # 2. INWARD GRN PROCUREMENT POSTING (ALL 5 OVERHEADS & ISOLATED AP DEBT)
    # =========================================================================
    @classmethod
    @transaction.atomic
    def post_grn_receipt(cls, grn: GoodsReceivedNote, user=None, **kwargs) -> Optional[JournalEntry]:
        """
        Creates a balanced double-entry voucher for an approved Goods Received Note (GRN):
        1. Debit: Merchandise Inventory Asset (1310) at Total Landed Cost (Pre-VAT Base + All 5 Overheads:
           Freight, Customs Duty, Handling & Unloading, Transit Insurance, and Other Overheads).
        2. Debit: Dedicated 13% Input VAT (1410) for tax claimable on supplier tax bill,
           explicitly linked with supplier, PAN, GRN number, and bill reference.
        3. Credit: Cash/Bank/Wallet for spot cash payments made to merchandise supplier upon delivery.
        4. Credit: Accounts Payable (2110) strictly for the merchandise supplier's remaining debt (Net Invoice - Paid).
        5. Credit: Cash in Hand (1110) or Freight/Logistics Clearing (2160) for the 5-tier shipping overheads.
           Strictly isolates third-party logistics overheads from the merchandise supplier's debt ledger.
        6. Enforces exact mathematical double-entry equality: Sum(Debits) == Sum(Credits).
        """
        source_module = 'PURCHASE_GRN'
        source_id = str(grn.id)

        duplicate_filter = Q(source_module__in=['PURCHASE', 'PURCHASE_GRN'], source_id=source_id, status='POSTED')
        duplicate_filter |= Q(voucher_type='PURCHASE', reference_document=grn.grn_number, status='POSTED')
        existing = JournalEntry.objects.filter(duplicate_filter).first()
        if existing:
            return existing

        branch = grn.branch

        # Strictly use grn.bill_date
        raw_date_ad = grn.bill_date or timezone.now().date()
        date_ad = raw_date_ad.date() if isinstance(raw_date_ad, datetime) else raw_date_ad

        # Fiscal Year Lock Guard
        bs_y, bs_m, bs_d = NepaliCalendar.ad_to_bs(date_ad)
        fy_name = grn.fiscal_year or NepaliCalendar.get_fiscal_year(bs_y, bs_m)
        locked_fy = AccountingFiscalYear.objects.filter(name=fy_name, is_closed=True).first()
        if locked_fy:
            active_open_fy = AccountingFiscalYear.objects.filter(is_closed=False).order_by('-start_date_ad').first()
            open_name = active_open_fy.name if active_open_fy else "the active fiscal year (2083/84)"
            raise ValidationError(
                f"GL Posting Rejected: GRN {grn.grn_number} is dated {date_ad} (FY {fy_name}), "
                f"which is audited and closed. Only bills within {open_name} are permitted."
            )

        lines: List[Dict[str, Any]] = []

        inv_asset_acc = cls.get_or_create_control_account(
            branch, 'INVENTORY_ASSET', '1310', 'Merchandise Inventory Asset', 'ASSET', 'DEBIT'
        )
        ap_acc = cls.get_or_create_control_account(
            branch, 'ACCOUNTS_PAYABLE', '2110', 'Accounts Payable (Trade Creditors)', 'LIABILITY', 'CREDIT'
        )

        # -------------------------------------------------------------
        # 1. DEBIT: Merchandise Inventory Asset (Landed Cost with All 5 Overheads)
        # -------------------------------------------------------------
        extra_freight = getattr(grn, 'extra_freight_charge', Decimal('0.00')) or Decimal('0.00')
        customs_charge = getattr(grn, 'customs_import_charge', Decimal('0.00')) or Decimal('0.00')
        handling_charge = getattr(grn, 'other_handling_charge', Decimal('0.00')) or Decimal('0.00')
        insurance_charge = getattr(grn, 'insurance_charge', Decimal('0.00')) or Decimal('0.00')
        other_overheads = getattr(grn, 'other_overheads_charge', Decimal('0.00')) or Decimal('0.00')

        overheads_5_tier = (
            extra_freight + customs_charge + handling_charge + insurance_charge + other_overheads
        ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        pre_tax_base = (
            grn.taxable_amount or grn.gross_amount or Decimal('0.00')
        ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        if grn.total_landed_cost and grn.total_landed_cost > Decimal('0.00'):
            landed_asset_value = grn.total_landed_cost.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        else:
            landed_asset_value = (pre_tax_base + overheads_5_tier).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        lines.append({
            'account': inv_asset_acc,
            'debit': landed_asset_value,
            'credit': Decimal('0.00'),
            'supplier': grn.supplier,
            'narration': (
                f"Stock received at landed cost under GRN {grn.grn_number} "
                f"(Bill: {grn.supplier_bill_no}, Pre-VAT: Rs. {pre_tax_base:,.2f}, Overheads: Rs. {overheads_5_tier:,.2f})"
            )
        })

        # -------------------------------------------------------------
        # 2. DEBIT: Dedicated 13% Input VAT (AUDIT-LINKED TRACEABILITY)
        # -------------------------------------------------------------
        input_vat = Decimal('0.00')
        if grn.is_vat_bill and (grn.vat_amount or Decimal('0.00')) > Decimal('0.00'):
            input_vat = grn.vat_amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            input_vat_acc = cls.get_or_create_control_account(
                branch, 'INPUT_VAT', '1410', 'Input VAT 13%', 'ASSET', 'DEBIT'
            )

            # Reliable VAT-to-Document Linkage Narration
            supp_pan_str = f", PAN: {grn.supplier.pan_number}" if grn.supplier and grn.supplier.pan_number else ""
            vat_narration = (
                f"13% Input VAT Claim [GRN: {grn.grn_number}] "
                f"(Supplier Bill: {grn.supplier_bill_no}, Supplier: {grn.supplier.company_name}{supp_pan_str}, "
                f"Taxable Base: Rs. {pre_tax_base:,.2f})"
            )

            lines.append({
                'account': input_vat_acc,
                'debit': input_vat,
                'credit': Decimal('0.00'),
                'supplier': grn.supplier,
                'narration': vat_narration
            })

        # -------------------------------------------------------------
        # 3. CREDIT: Merchandise Supplier Settlement (Strictly Merchandise Net + VAT)
        # -------------------------------------------------------------
        net_supplier_invoice = (
            grn.net_total_amount or (pre_tax_base + input_vat)
        ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        paid = (grn.paid_amount or Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        effective_paid = min(paid, net_supplier_invoice)

        # Spot cash/bank payment disbursed to merchandise supplier
        if effective_paid > Decimal('0.00'):
            payment_mode = getattr(grn, 'preferred_payment_method', None) or getattr(grn, 'payment_mode', 'CASH') or 'CASH'
            spot_acc = cls.resolve_payment_account(branch, payment_mode, for_party='SUPPLIER')
            lines.append({
                'account': spot_acc,
                'debit': Decimal('0.00'),
                'credit': effective_paid,
                'supplier': grn.supplier,
                'narration': f"Spot payment disbursed to {grn.supplier.company_name} on GRN {grn.grn_number} via {payment_mode}"
            })

        # Remaining due owed strictly to the merchandise supplier (matches grn.due_amount and SupplierUdhaariLedger)
        supplier_due = max(Decimal('0.00'), net_supplier_invoice - effective_paid)
        if supplier_due > Decimal('0.00'):
            lines.append({
                'account': ap_acc,
                'debit': Decimal('0.00'),
                'credit': supplier_due,
                'supplier': grn.supplier,
                'narration': f"Supplier Accounts Payable owed to {grn.supplier.company_name} on bill {grn.supplier_bill_no}"
            })

        # -------------------------------------------------------------
        # 4. CREDIT: 5-Tier Overheads (Freight, Customs, Handling, Insurance, Other)
        # Strictly isolates shipping/customs overheads from the merchandise supplier's debt!
        # -------------------------------------------------------------
        if overheads_5_tier > Decimal('0.00'):
            overhead_payment_mode = str(
                kwargs.get('overhead_payment_mode') or
                getattr(grn, 'overhead_payment_mode', '') or
                kwargs.get('overhead_payment_method') or
                getattr(grn, 'overhead_payment_method', '') or ''
            ).strip().upper()

            if overhead_payment_mode in ['CASH', 'COD', 'PAID_IN_CASH', 'SPOT_CASH', 'CASH_ON_DELIVERY'] or kwargs.get('overhead_paid_in_cash') is True:
                overhead_acc = cls.get_or_create_control_account(
                    branch, 'CASH', '1110', 'Cash in Hand (Main Drawer)', 'ASSET', 'DEBIT'
                )
                overhead_narr = f"Overhead expenses (Freight/Duty/Handling) paid in cash for GRN {grn.grn_number}"
            elif overhead_payment_mode in ['BANK', 'BANK_TRANSFER', 'CHEQUE', 'CONNECT_IPS', 'WIRE']:
                overhead_acc = cls.get_or_create_control_account(
                    branch, 'BANK', '1120', 'Primary Bank Current Account', 'ASSET', 'DEBIT'
                )
                overhead_narr = f"Overhead expenses (Freight/Duty/Handling) paid via bank for GRN {grn.grn_number}"
            else:
                overhead_acc = cls.get_or_create_control_account(
                    branch, 'NONE', '2160', 'Freight & Logistics Clearing / Payables', 'LIABILITY', 'CREDIT'
                )
                overhead_narr = f"Overhead expenses (Freight/Duty/Handling) clearing payable for GRN {grn.grn_number}"

            lines.append({
                'account': overhead_acc,
                'debit': Decimal('0.00'),
                'credit': overheads_5_tier,
                'supplier': None,  # Strictly NO merchandise supplier debt created for freight/customs
                'narration': overhead_narr
            })

        # -------------------------------------------------------------
        # 5. Penny Rounding Residual Reconciliation
        # -------------------------------------------------------------
        sum_dr = sum(l['debit'] for l in lines)
        sum_cr = sum(l['credit'] for l in lines)
        diff = sum_dr - sum_cr
        if Decimal('0.00') < abs(diff) <= Decimal('0.05'):
            for l in lines:
                if l['account'] == ap_acc and l['credit'] > Decimal('0.00'):
                    l['credit'] += diff
                    break

        net_total = (grn.net_total_amount or Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        narration = (
            f"Inward GRN stock procurement from {grn.supplier.company_name} "
            f"(Challan/Bill: {grn.supplier_bill_no}, Net: Rs. {net_total:,.2f}, Landed: Rs. {landed_asset_value:,.2f})"
        )
        return JournalEngine.create_balanced_entry(
            voucher_type='PURCHASE',
            date_ad=date_ad,
            branch=branch,
            lines=lines,
            narration=narration,
            reference_doc=grn.grn_number,
            source_module=source_module,
            source_id=source_id,
            user=user or grn.received_by,
            auto_post=True
        )

    post_grn_journal = post_grn_receipt

    # =========================================================================
    # 3. CUSTOMER DEBT REPAYMENT (READS SUB-LEDGER HISTORICAL ENTRY DATE)
    # =========================================================================
    @classmethod
    @transaction.atomic
    def post_customer_payment(
        cls,
        ledger_entry: Optional[CustomerUdhaariLedger] = None,
        customer: Optional[Customer] = None,
        amount: Optional[Decimal] = None,
        payment_mode: str = 'CASH',
        reference: Optional[str] = None,
        branch: Optional[Branch] = None,
        user=None,
        **kwargs
    ) -> Optional[JournalEntry]:
        """
        Posts customer Udhaari debt collection into double-entry accounts.
        Reads the true historical payment date (entry_date) from CustomerUdhaariLedger.
        Filters out non-monetary store credit / trade-in adjustments to avoid phantom cash debits.
        """
        if ledger_entry:
            cust = ledger_entry.customer
            amt = ledger_entry.amount
            mode = (ledger_entry.payment_mode or 'CASH').upper().strip()

            if mode in ['OTHER', 'ADJUSTMENT', 'STORE_CREDIT', 'TRADE_IN', 'NON_MONETARY']:
                return None

            remarks_str = (getattr(ledger_entry, 'remarks', '') or '').upper()
            if 'SURPLUS TRADE-IN' in remarks_str or 'STORE CREDIT' in remarks_str:
                return None

            br = ledger_entry.branch or getattr(cust, 'preferred_branch', None) or Branch.get_default_main_branch()
            ref_doc = getattr(ledger_entry, 'reference_invoice', None) or f"UDH-CUST-{ledger_entry.id}"
            source_id = str(ledger_entry.id)
            user = user or getattr(ledger_entry, 'recorded_by', None)

            raw_date = getattr(ledger_entry, 'entry_date', None) or kwargs.get('date_ad')
            if not raw_date:
                raw_date = ledger_entry.created_at.date() if hasattr(ledger_entry.created_at, 'date') else ledger_entry.created_at
            date_ad = raw_date.date() if isinstance(raw_date, datetime) else raw_date
        else:
            cust = customer
            amt = Decimal(str(amount or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            mode = (payment_mode or 'CASH').upper().strip()

            if mode in ['OTHER', 'ADJUSTMENT', 'STORE_CREDIT', 'TRADE_IN', 'NON_MONETARY']:
                return None

            br = branch or getattr(cust, 'preferred_branch', None) or Branch.get_default_main_branch()
            ref_doc = reference or f"UDH-CUST-{cust.id}-{timezone.now().strftime('%Y%m%d%H%M%S')}"
            source_id = ref_doc
            raw_date = kwargs.get('date_ad') or timezone.now().date()
            date_ad = raw_date.date() if isinstance(raw_date, datetime) else raw_date

        if amt <= Decimal('0.00') or not cust:
            return None

        bs_y, bs_m, _ = NepaliCalendar.ad_to_bs(date_ad)
        fy_name = NepaliCalendar.get_fiscal_year(bs_y, bs_m)
        locked_fy = AccountingFiscalYear.objects.filter(name=fy_name, is_closed=True).first()
        if locked_fy:
            active_open_fy = AccountingFiscalYear.objects.filter(is_closed=False).order_by('-start_date_ad').first()
            open_name = active_open_fy.name if active_open_fy else "the active fiscal year (2083/84)"
            raise ValidationError(
                f"Customer Repayment Posting Rejected: Transaction date {date_ad} belongs to Fiscal Year {fy_name}, "
                f"which is audited and closed. Only collections within {open_name} are permitted."
            )

        source_module = 'CUSTOMER_PAYMENT'
        duplicate_filter = Q(source_module=source_module, source_id=source_id, status='POSTED')
        duplicate_filter |= Q(voucher_type='RECEIPT', reference_document=ref_doc, status='POSTED')
        existing = JournalEntry.objects.filter(duplicate_filter).first()
        if existing:
            return existing

        ar_acc = cls.get_or_create_control_account(
            br, 'ACCOUNTS_RECEIVABLE', '1210', 'Accounts Receivable (Trade Debtors)', 'ASSET', 'DEBIT'
        )
        dest_acc = cls.resolve_payment_account(br, mode, for_party='CUSTOMER')

        lines = [
            {
                'account': dest_acc,
                'debit': amt,
                'credit': Decimal('0.00'),
                'customer': cust,
                'narration': f"Customer Udhaari repayment from {cust.name} via {mode}"
            },
            {
                'account': ar_acc,
                'debit': Decimal('0.00'),
                'credit': amt,
                'customer': cust,
                'narration': f"Settlement of outstanding debt by {cust.name}"
            }
        ]

        narration = f"Customer debt collection: {cust.name} (Rs. {amt:.2f}) via {mode}"
        return JournalEngine.create_balanced_entry(
            voucher_type='RECEIPT',
            date_ad=date_ad,
            branch=br,
            lines=lines,
            narration=narration,
            reference_doc=ref_doc,
            source_module=source_module,
            source_id=source_id,
            user=user,
            auto_post=True
        )

    post_customer_repayment_journal = post_customer_payment

    # =========================================================================
    # 4. SUPPLIER DEBT PAYOUT (READS SUB-LEDGER HISTORICAL ENTRY DATE)
    # =========================================================================
    @classmethod
    @transaction.atomic
    def post_supplier_payment(
        cls,
        ledger_entry: Optional[SupplierUdhaariLedger] = None,
        supplier: Optional[Supplier] = None,
        amount: Optional[Decimal] = None,
        payment_mode: str = 'CASH',
        ref_no: Optional[str] = None,
        branch: Optional[Branch] = None,
        user=None,
        **kwargs
    ) -> Optional[JournalEntry]:
        """
        Posts supplier debt settlement payout:
        - Reads the true historical payment date (entry_date) from SupplierUdhaariLedger.
        - Debit: Accounts Payable (2110 Supplier Sub-Ledger)
        - Credit: Resolved Payment Account (Cash, Bank, Wallet)
        """
        if ledger_entry:
            supp = ledger_entry.supplier
            amt = ledger_entry.amount
            mode = (ledger_entry.payment_mode or 'CASH').upper().strip()

            if mode in ['OTHER', 'ADJUSTMENT', 'NON_MONETARY']:
                return None

            br = ledger_entry.branch or Branch.get_default_main_branch()
            ref_doc = getattr(ledger_entry, 'reference_number', None) or f"SUP-PAY-{ledger_entry.id}"
            source_id = str(ledger_entry.id)
            user = user or getattr(ledger_entry, 'recorded_by', None)

            raw_date = getattr(ledger_entry, 'entry_date', None) or kwargs.get('date_ad')
            if not raw_date:
                raw_date = ledger_entry.created_at.date() if hasattr(ledger_entry.created_at, 'date') else ledger_entry.created_at
            date_ad = raw_date.date() if isinstance(raw_date, datetime) else raw_date
        else:
            supp = supplier
            amt = Decimal(str(amount or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            mode = (payment_mode or 'CASH').upper().strip()

            if mode in ['OTHER', 'ADJUSTMENT', 'NON_MONETARY']:
                return None

            br = branch or Branch.get_default_main_branch()
            ref_doc = ref_no or f"SUP-PAY-{supp.id}-{timezone.now().strftime('%Y%m%d%H%M%S')}"
            source_id = ref_doc
            raw_date = kwargs.get('date_ad') or timezone.now().date()
            date_ad = raw_date.date() if isinstance(raw_date, datetime) else raw_date

        if amt <= Decimal('0.00') or not supp:
            return None

        bs_y, bs_m, _ = NepaliCalendar.ad_to_bs(date_ad)
        fy_name = NepaliCalendar.get_fiscal_year(bs_y, bs_m)
        locked_fy = AccountingFiscalYear.objects.filter(name=fy_name, is_closed=True).first()
        if locked_fy:
            active_open_fy = AccountingFiscalYear.objects.filter(is_closed=False).order_by('-start_date_ad').first()
            open_name = active_open_fy.name if active_open_fy else "the active fiscal year (2083/84)"
            raise ValidationError(
                f"Supplier Payout Posting Rejected: Transaction date {date_ad} belongs to Fiscal Year {fy_name}, "
                f"which is audited and closed. Only payouts within {open_name} are permitted."
            )

        source_module = 'SUPPLIER_PAYMENT'
        duplicate_filter = Q(source_module=source_module, source_id=source_id, status='POSTED')
        duplicate_filter |= Q(voucher_type='PAYMENT', reference_document=ref_doc, status='POSTED')
        existing = JournalEntry.objects.filter(duplicate_filter).first()
        if existing:
            return existing

        ap_acc = cls.get_or_create_control_account(
            br, 'ACCOUNTS_PAYABLE', '2110', 'Accounts Payable (Trade Creditors)', 'LIABILITY', 'CREDIT'
        )
        src_acc = cls.resolve_payment_account(br, mode, for_party='SUPPLIER')

        lines = [
            {
                'account': ap_acc,
                'debit': amt,
                'credit': Decimal('0.00'),
                'supplier': supp,
                'narration': f"Payout settlement to {supp.company_name}"
            },
            {
                'account': src_acc,
                'debit': Decimal('0.00'),
                'credit': amt,
                'supplier': supp,
                'narration': f"Disbursement via {mode} (Ref: {ref_doc})"
            }
        ]

        narration = f"Supplier debt payout: {supp.company_name} (Rs. {amt:.2f})"
        return JournalEngine.create_balanced_entry(
            voucher_type='PAYMENT',
            date_ad=date_ad,
            branch=br,
            lines=lines,
            narration=narration,
            reference_doc=ref_doc,
            source_module=source_module,
            source_id=source_id,
            user=user,
            auto_post=True
        )

    post_supplier_payout_journal = post_supplier_payment

    # =========================================================================
    # 5. CUSTOMER SALES RETURN POSTING (REVERSES OUTPUT VAT 2210 WITH LINKAGE)
    # =========================================================================
    @classmethod
    @transaction.atomic
    def post_sales_return(cls, sales_return: SalesReturn, user=None, **kwargs) -> Optional[JournalEntry]:
        """
        Posts customer return or exchange credit note:
        - Strictly binds date_ad to sales_return.return_date_ad
        - Obtains the exact Output VAT reversal amount from verified header and line-item snapshots.
        - Does NOT silently post a zero VAT reversal when underlying return lines contain a non-zero VAT reversal.
        - Debits Sales Returns & Deductions (4020) for the pre-tax base.
        - Debits Output VAT 13% (2210) strictly for the verified VAT reversal amount, maintaining
          the bidirectional document audit link to the sales return and original sales invoice.
        - Debits Inventory Asset (1310 working) OR Quarantine Asset (1330 defective) for COGS.
        - Credits Cash/Bank/Wallet (refund) OR Accounts Receivable (1210 store credit).
        - Credits COGS (5110 reversal).
        """
        source_module = 'SALES_RETURN'
        source_id = str(sales_return.id)

        duplicate_filter = Q(source_module=source_module, source_id=source_id, status='POSTED')
        duplicate_filter |= Q(voucher_type='CREDIT_NOTE', reference_document=sales_return.return_number, status='POSTED')
        existing = JournalEntry.objects.filter(duplicate_filter).first()
        if existing:
            return existing

        branch = sales_return.branch
        raw_date = sales_return.return_date_ad or timezone.now().date()
        date_ad = raw_date.date() if isinstance(raw_date, datetime) else raw_date

        lines: List[Dict[str, Any]] = []

        ret_rev_acc = cls.get_or_create_control_account(
            branch, 'SALES_RETURN', '4020', 'Sales Returns & Deductions', 'REVENUE', 'DEBIT'
        )
        output_vat_acc = cls.get_or_create_control_account(
            branch, 'OUTPUT_VAT', '2210', 'Output VAT 13%', 'LIABILITY', 'CREDIT'
        )
        ar_acc = cls.get_or_create_control_account(
            branch, 'ACCOUNTS_RECEIVABLE', '1210', 'Accounts Receivable (Trade Debtors)', 'ASSET', 'DEBIT'
        )
        inv_asset_acc = cls.get_or_create_control_account(
            branch, 'INVENTORY_ASSET', '1310', 'Merchandise Inventory Asset', 'ASSET', 'DEBIT'
        )
        quar_asset_acc = cls.get_or_create_control_account(
            branch, 'DEFECTIVE_INVENTORY_ASSET', '1330', 'Quarantined Defective Inventory Asset', 'ASSET', 'DEBIT'
        )
        cogs_acc = cls.get_or_create_control_account(
            branch, 'COGS', '5110', 'Cost of Goods Sold (COGS)', 'DIRECT_EXPENSE', 'DEBIT'
        )

        # -------------------------------------------------------------
        # 1. VERIFY EXACT VAT REVERSAL AMOUNT & PRE-TAX RETURN BASE
        # -------------------------------------------------------------
        # Calculate aggregate VAT reversal from line-item snapshots
        items_vat_sum = Decimal('0.00')
        if sales_return.items.exists():
            for item in sales_return.items.all():
                line_vat_rev = item.vat_reversal_amount
                if line_vat_rev is None or line_vat_rev <= Decimal('0.00'):
                    # Fallback check on item level if tax rate > 0
                    if getattr(item, 'vat_rate', Decimal('0.00')) > Decimal('0.00') and item.refund_amount > Decimal('0.00'):
                        divisor = Decimal('1.00') + (item.vat_rate / Decimal('100.00'))
                        taxable_part = (item.refund_amount / divisor).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                        line_vat_rev = item.refund_amount - taxable_part
                    else:
                        line_vat_rev = Decimal('0.00')
                items_vat_sum += (line_vat_rev or Decimal('0.00'))

        items_vat_sum = items_vat_sum.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        header_vat_amt = (sales_return.vat_amount or Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        # Authoritatively resolve VAT reversal: prefer non-zero item snapshot sum over zero/incorrect header
        if items_vat_sum > Decimal('0.00'):
            vat_reversal_amt = items_vat_sum
        elif header_vat_amt > Decimal('0.00'):
            vat_reversal_amt = header_vat_amt
        elif hasattr(sales_return, 'total_vat_amount') and sales_return.total_vat_amount > Decimal('0.00'):
            vat_reversal_amt = sales_return.total_vat_amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        else:
            vat_reversal_amt = Decimal('0.00')

        total_refund_amt = Decimal(str(sales_return.total_refund_amount or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        orig_bill_no = sales_return.original_estimate.estimate_number if sales_return.original_estimate else 'N/A'
        cust_name = sales_return.customer.name if sales_return.customer else (sales_return.original_estimate.recipient_display_name if sales_return.original_estimate else 'Walk-in')
        cust_pan = (
            sales_return.customer.pan_number if sales_return.customer and sales_return.customer.pan_number else (
                sales_return.original_estimate.customer_pan if sales_return.original_estimate and sales_return.original_estimate.customer_pan else ''
            )
        )
        cust_pan_str = f", PAN: {cust_pan}" if cust_pan else ""

        if vat_reversal_amt > Decimal('0.00'):
            pre_tax_return_base = max(Decimal('0.00'), total_refund_amt - vat_reversal_amt).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            lines.append({
                'account': ret_rev_acc,
                'debit': pre_tax_return_base,
                'credit': Decimal('0.00'),
                'customer': sales_return.customer,
                'narration': (
                    f"Sales return merchandise base reversal [Return: {sales_return.return_number}] "
                    f"on Bill {orig_bill_no}"
                )
            })

            vat_narration = (
                f"13% Output VAT Reversal [Return: {sales_return.return_number}] "
                f"(Original Bill: {orig_bill_no}, "
                f"Customer: {cust_name}{cust_pan_str}, "
                f"Taxable Base: Rs. {pre_tax_return_base:,.2f})"
            )

            lines.append({
                'account': output_vat_acc,
                'debit': vat_reversal_amt,
                'credit': Decimal('0.00'),
                'customer': sales_return.customer,
                'narration': vat_narration
            })
        else:
            lines.append({
                'account': ret_rev_acc,
                'debit': total_refund_amt,
                'credit': Decimal('0.00'),
                'customer': sales_return.customer,
                'narration': f"Sales return voucher {sales_return.return_number} on Bill {orig_bill_no}"
            })

        # -------------------------------------------------------------
        # 2. CREDIT: Refund Settlement (Cash, Store Credit, or Udhaari Deduction)
        # -------------------------------------------------------------
        refund_mode = str(sales_return.refund_mode or '').upper()
        if refund_mode in ['CREDIT', 'STORE_CREDIT', 'UDHAARI', 'CREDIT_NOTE']:
            lines.append({
                'account': ar_acc,
                'debit': Decimal('0.00'),
                'credit': total_refund_amt,
                'customer': sales_return.customer,
                'narration': f"Store credit / Udhaari adjustment on return {sales_return.return_number} (Bill: {orig_bill_no})"
            })
        else:
            refund_acc = cls.resolve_payment_account(branch, refund_mode, for_party='CUSTOMER')
            lines.append({
                'account': refund_acc,
                'debit': Decimal('0.00'),
                'credit': total_refund_amt,
                'customer': sales_return.customer,
                'narration': f"Refund ({sales_return.refund_mode}) issued to customer on return {sales_return.return_number} (Bill: {orig_bill_no})"
            })

        # -------------------------------------------------------------
        # 3. INVENTORY & COGS REVERSAL
        # -------------------------------------------------------------
        total_restocked_cogs = Decimal('0.00')
        total_quarantined_cogs = Decimal('0.00')

        for item in sales_return.items.select_related('estimate_item', 'product'):
            unit_cost = item.estimate_item.cost_price or item.product.purchase_price or Decimal('0.00')
            line_cost = (unit_cost * item.base_unit_quantity).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            if getattr(item, 'is_defective', False):
                total_quarantined_cogs += line_cost
            else:
                total_restocked_cogs += line_cost

        if total_restocked_cogs > Decimal('0.00'):
            lines.append({
                'account': inv_asset_acc,
                'debit': total_restocked_cogs,
                'credit': Decimal('0.00'),
                'narration': f"Restock sellable returned goods at landed cost [Return: {sales_return.return_number}]"
            })
            lines.append({
                'account': cogs_acc,
                'debit': Decimal('0.00'),
                'credit': total_restocked_cogs,
                'narration': f"COGS reversal on restocked return items [Return: {sales_return.return_number}]"
            })

        if total_quarantined_cogs > Decimal('0.00'):
            lines.append({
                'account': quar_asset_acc,
                'debit': total_quarantined_cogs,
                'credit': Decimal('0.00'),
                'narration': f"Route defective returned items to quarantine asset [Return: {sales_return.return_number}]"
            })
            lines.append({
                'account': cogs_acc,
                'debit': Decimal('0.00'),
                'credit': total_quarantined_cogs,
                'narration': f"COGS reversal for defective items quarantined for RMA [Return: {sales_return.return_number}]"
            })

        # -------------------------------------------------------------
        # 4. PENNY ROUNDING RESIDUAL RECONCILIATION
        # -------------------------------------------------------------
        sum_dr = sum(l['debit'] for l in lines)
        sum_cr = sum(l['credit'] for l in lines)
        diff = sum_dr - sum_cr
        if Decimal('0.00') < abs(diff) <= Decimal('0.05'):
            for l in lines:
                if l['account'] == ret_rev_acc and l['debit'] > Decimal('0.00'):
                    l['debit'] -= diff
                    break

        narration = f"Sales Return Credit Note {sales_return.return_number} (Ref Bill: {orig_bill_no}, Customer: {cust_name})"
        return JournalEngine.create_balanced_entry(
            voucher_type='CREDIT_NOTE',
            date_ad=date_ad,
            branch=branch,
            lines=lines,
            narration=narration,
            reference_doc=sales_return.return_number,
            source_module=source_module,
            source_id=source_id,
            user=user or getattr(sales_return, 'processed_by', None),
            auto_post=True
        )

    # =========================================================================
    # 6. COMMERCIAL PURCHASE RETURN (DEBIT NOTE) POSTING (REVERSES INPUT VAT 1410)
    # =========================================================================
    @classmethod
    @transaction.atomic
    def post_purchase_return(cls, purchase_return: PurchaseReturn, user=None, **kwargs) -> Optional[JournalEntry]:
        """
        Posts commercial purchase return / debit note to supplier:
        - Strictly binds date_ad to purchase_return.return_date
        - Debit: Accounts Payable (2110) OR Cash in Hand (if cash refund received)
        - Credit: Merchandise Inventory Asset (1310 at purchase value)
        - Credit: Input VAT 13% (1410 reversal if tax bill by exact debit note tax amount),
          explicitly linked with supplier, PAN, Debit Note number, and original bill reference.
        """
        source_module = 'PURCHASE_RETURN'
        source_id = str(purchase_return.id)

        duplicate_filter = Q(source_module=source_module, source_id=source_id, status='POSTED')
        duplicate_filter |= Q(voucher_type='DEBIT_NOTE', reference_document=purchase_return.return_number, status='POSTED')
        existing = JournalEntry.objects.filter(duplicate_filter).first()
        if existing:
            return existing

        branch = purchase_return.branch
        raw_date = purchase_return.return_date or timezone.now().date()
        date_ad = raw_date.date() if isinstance(raw_date, datetime) else raw_date

        lines: List[Dict[str, Any]] = []

        ap_acc = cls.get_or_create_control_account(
            branch, 'ACCOUNTS_PAYABLE', '2110', 'Accounts Payable (Trade Creditors)', 'LIABILITY', 'CREDIT'
        )
        cash_acc = cls.get_or_create_control_account(
            branch, 'CASH', '1110', 'Cash in Hand (Main Drawer)', 'ASSET', 'DEBIT'
        )
        inv_asset_acc = cls.get_or_create_control_account(
            branch, 'INVENTORY_ASSET', '1310', 'Merchandise Inventory Asset', 'ASSET', 'DEBIT'
        )
        input_vat_acc = cls.get_or_create_control_account(
            branch, 'INPUT_VAT', '1410', 'Input VAT 13%', 'ASSET', 'DEBIT'
        )

        refund_mode = str(purchase_return.refund_mode or '').upper()
        net_refund_val = purchase_return.net_refund_amount or Decimal('0.00')

        # 1. DEBIT: Settlement Mode (AP Reduction or Cash Refund)
        if refund_mode in ['CASH_REFUND', 'CASH']:
            lines.append({
                'account': cash_acc,
                'debit': net_refund_val,
                'credit': Decimal('0.00'),
                'supplier': purchase_return.supplier,
                'narration': f"Cash refund received on Debit Note {purchase_return.return_number}"
            })
        else:
            lines.append({
                'account': ap_acc,
                'debit': net_refund_val,
                'credit': Decimal('0.00'),
                'supplier': purchase_return.supplier,
                'narration': f"Accounts Payable reduced on Debit Note {purchase_return.return_number}"
            })

        # 2. CREDIT: Merchandise Inventory Asset
        lines.append({
            'account': inv_asset_acc,
            'debit': Decimal('0.00'),
            'credit': purchase_return.total_return_amount or Decimal('0.00'),
            'supplier': purchase_return.supplier,
            'narration': f"Merchandise inventory returned to vendor {purchase_return.supplier.company_name}"
        })

        # 3. CREDIT: Input VAT 13% Reversal (AUDIT-LINKED TRACEABILITY)
        tax_amt = purchase_return.tax_amount or Decimal('0.00')
        if tax_amt <= Decimal('0.00') and purchase_return.items.exists():
            tax_amt = sum((item.tax_amount or Decimal('0.00') for item in purchase_return.items.all()), Decimal('0.00'))

        if tax_amt > Decimal('0.00'):
            supp_pan_str = f", PAN: {purchase_return.supplier.pan_number}" if purchase_return.supplier and purchase_return.supplier.pan_number else ""
            orig_ref_str = (
                f", Ref Bill: {purchase_return.original_bill_reference}" if purchase_return.original_bill_reference else (
                    f", GRN: {purchase_return.original_grn.grn_number}" if purchase_return.original_grn else ""
                )
            )
            vat_narration = (
                f"13% Input VAT Reversal [Debit Note: {purchase_return.return_number}] "
                f"(Supplier: {purchase_return.supplier.company_name}{supp_pan_str}{orig_ref_str}, "
                f"Taxable Base: Rs. {purchase_return.total_return_amount:,.2f})"
            )

            lines.append({
                'account': input_vat_acc,
                'debit': Decimal('0.00'),
                'credit': tax_amt,
                'supplier': purchase_return.supplier,
                'narration': vat_narration
            })

        # Residual Penny Reconciler
        sum_dr = sum(l['debit'] for l in lines)
        sum_cr = sum(l['credit'] for l in lines)
        diff = sum_dr - sum_cr
        if Decimal('0.00') < abs(diff) <= Decimal('0.05'):
            for l in lines:
                if l['account'] in [ap_acc, cash_acc] and l['debit'] > Decimal('0.00'):
                    l['debit'] -= diff
                    break

        narration = f"Purchase Return / Debit Note {purchase_return.return_number} to {purchase_return.supplier.company_name}"
        return JournalEngine.create_balanced_entry(
            voucher_type='DEBIT_NOTE',
            date_ad=date_ad,
            branch=branch,
            lines=lines,
            narration=narration,
            reference_doc=purchase_return.return_number,
            source_module=source_module,
            source_id=source_id,
            user=user or purchase_return.processed_by,
            auto_post=True
        )

    post_purchase_return_journal = post_purchase_return

    # =========================================================================
    # 7. INVENTORY DAMAGE & SHRINKAGE WRITE-OFF
    # =========================================================================
    @classmethod
    @transaction.atomic
    def post_inventory_shrinkage(
        cls,
        branch: Branch,
        product,
        quantity: Decimal,
        unit_cost: Decimal,
        reason: str,
        reference_id: Optional[str] = None,
        user=None,
        **kwargs
    ) -> Optional[JournalEntry]:
        """
        Posts stock damage, breakage, or loss write-offs:
        - Debit: Inventory Shrinkage & Loss (6160)
        - Credit: Merchandise Inventory Asset (1310)
        """
        total_loss = (quantity * unit_cost).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        if total_loss <= Decimal('0.00'):
            return None

        source_module = 'INVENTORY_SHRINKAGE'
        source_id = str(reference_id or f"SHRINK-{product.id}-{timezone.now().strftime('%Y%m%d%H%M%S')}")
        ref_doc = f"WRITEOFF-{product.id}"

        duplicate_filter = Q(source_module=source_module, source_id=source_id, status='POSTED')
        existing = JournalEntry.objects.filter(duplicate_filter).first()
        if existing:
            return existing

        lines: List[Dict[str, Any]] = []
        shrinkage_acc = cls.get_or_create_control_account(
            branch, 'INVENTORY_SHRINKAGE', '6160', 'Inventory Shrinkage, Breakage & Loss', 'INDIRECT_EXPENSE', 'DEBIT'
        )
        inv_asset_acc = cls.get_or_create_control_account(
            branch, 'INVENTORY_ASSET', '1310', 'Merchandise Inventory Asset', 'ASSET', 'DEBIT'
        )

        lines.append({
            'account': shrinkage_acc,
            'debit': total_loss,
            'credit': Decimal('0.00'),
            'narration': f"Stock damage write-off: {product.name} x {quantity} ({reason})"
        })
        lines.append({
            'account': inv_asset_acc,
            'debit': Decimal('0.00'),
            'credit': total_loss,
            'narration': f"Relieve inventory asset for damaged item: {product.name}"
        })

        narration = f"Stock Damage / Shrinkage Write-off: {product.name} x {quantity} (Loss: Rs. {total_loss:.2f})"
        return JournalEngine.create_balanced_entry(
            voucher_type='JOURNAL',
            date_ad=timezone.now().date(),
            branch=branch,
            lines=lines,
            narration=narration,
            reference_doc=ref_doc,
            source_module=source_module,
            source_id=source_id,
            user=user,
            auto_post=True
        )

    # =========================================================================
    # 8. TRADE-IN BUY-BACK ACQUISITION POSTING
    # =========================================================================
    @classmethod
    @transaction.atomic
    def post_trade_in_acquisition(
        cls,
        trade_in_voucher,
        item_instance=None,
        user=None,
        **kwargs
    ) -> Optional[JournalEntry]:
        """
        Creates a balanced double-entry voucher for a pre-owned handset trade-in intake:
        - Debit: Merchandise Inventory Asset (Account 1310) at buy-back payout cost.
        - Credit: Trade-In Buy-Back Clearing / Payable (Account 2150).
        """
        payout_val = Decimal(str(trade_in_voucher.final_trade_in_value or 0)).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP
        )
        if payout_val <= Decimal('0.00'):
            return None

        source_module = 'TRADE_IN'
        source_id = str(trade_in_voucher.id)

        duplicate_filter = Q(source_module=source_module, source_id=source_id, status='POSTED')
        duplicate_filter |= Q(voucher_type='JOURNAL', reference_document=trade_in_voucher.voucher_number, status='POSTED')
        existing = JournalEntry.objects.filter(duplicate_filter).first()
        if existing:
            return existing

        branch = trade_in_voucher.branch
        raw_date = getattr(trade_in_voucher, 'intake_date_ad', None) or timezone.now().date()
        date_ad = raw_date.date() if isinstance(raw_date, datetime) else raw_date

        inv_asset_acc = cls.get_or_create_control_account(
            branch, 'INVENTORY_ASSET', '1310', 'Merchandise Inventory Asset', 'ASSET', 'DEBIT'
        )
        trade_in_clearing_acc = cls.get_or_create_control_account(
            branch, 'TRADE_IN_CLEARING', '2150', 'Trade-In Buy-Back Clearing / Payable', 'LIABILITY', 'CREDIT'
        )

        narration = (
            f"Pre-Owned Buy-Back Acquisition: {trade_in_voucher.brand_name} {trade_in_voucher.model_name} "
            f"(IMEI: {trade_in_voucher.imei_1}) - Voucher {trade_in_voucher.voucher_number}"
        )

        lines = [
            {
                'account': inv_asset_acc,
                'debit': payout_val,
                'credit': Decimal('0.00'),
                'customer': getattr(trade_in_voucher, 'customer', None),
                'narration': narration
            },
            {
                'account': trade_in_clearing_acc,
                'debit': Decimal('0.00'),
                'credit': payout_val,
                'customer': getattr(trade_in_voucher, 'customer', None),
                'narration': f"Trade-In buyback payable recorded for {trade_in_voucher.voucher_number}"
            }
        ]

        return JournalEngine.create_balanced_entry(
            voucher_type='JOURNAL',
            date_ad=date_ad,
            branch=branch,
            lines=lines,
            narration=narration,
            reference_doc=trade_in_voucher.voucher_number,
            source_module=source_module,
            source_id=source_id,
            user=user or getattr(trade_in_voucher, 'cashier', None),
            auto_post=True
        )

    # =========================================================================
    # 9. DIGITAL GATEWAY BATCH SETTLEMENT
    # =========================================================================
    @classmethod
    @transaction.atomic
    def post_gateway_settlement(
        cls,
        branch: Branch,
        gateway: str,
        gross_amount: Decimal,
        commission_fee: Decimal,
        net_amount: Optional[Decimal] = None,
        settlement_date: Optional[date] = None,
        reference_number: Optional[str] = None,
        settlement_batch_id: Optional[str] = None,
        user=None,
        **kwargs
    ) -> Optional[JournalEntry]:
        """
        Posts daily/batch clearing settlement for digital gateways and POS card processors:
        - Debit: Primary Bank Account (1120 Net payout deposited)
        - Debit: Payment Gateway Commission / MDR Expense (6190 fee deducted)
        - Credit: Digital Gateway Clearing Account (1130 FonePay, 1140 eSewa, 1150 Khalti, 1160 Card)
        """
        gross = Decimal(str(gross_amount)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        fee = Decimal(str(commission_fee)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        net = (gross - fee).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) if net_amount is None else Decimal(str(net_amount)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        if gross <= Decimal('0.00'):
            raise ValidationError("Gross settlement amount must be greater than zero.")
        if fee < Decimal('0.00'):
            raise ValidationError("Commission fee cannot be negative.")
        if net + fee != gross:
            raise ValidationError(f"Settlement imbalance: Net (Rs. {net:.2f}) + Fee (Rs. {fee:.2f}) != Gross (Rs. {gross:.2f})")

        source_module = 'GATEWAY_SETTLEMENT'
        source_id = str(settlement_batch_id or reference_number or f"SETTLE-{gateway.upper()}-{timezone.now().strftime('%Y%m%d%H%M%S')}")
        ref_doc = reference_number or source_id

        duplicate_filter = Q(source_module=source_module, source_id=source_id, status='POSTED')
        existing = JournalEntry.objects.filter(duplicate_filter).first()
        if existing:
            return existing

        settlement_date = settlement_date or timezone.now().date()
        if isinstance(settlement_date, datetime):
            settlement_date = settlement_date.date()

        lines: List[Dict[str, Any]] = []

        bank_acc = cls.get_or_create_control_account(
            branch, 'BANK', '1120', 'Primary Bank Current Account', 'ASSET', 'DEBIT'
        )
        lines.append({
            'account': bank_acc,
            'debit': net,
            'credit': Decimal('0.00'),
            'narration': f"{gateway.upper()} batch settlement net deposit (Ref: {ref_doc})"
        })

        if fee > Decimal('0.00'):
            fee_acc = cls.get_or_create_control_account(
                branch, 'PAYMENT_GATEWAY_FEE', '6190', 'Payment Gateway & Bank Merchant Fees (MDR)', 'INDIRECT_EXPENSE', 'DEBIT'
            )
            lines.append({
                'account': fee_acc,
                'debit': fee,
                'credit': Decimal('0.00'),
                'narration': f"{gateway.upper()} MDR commission fee deducted (Ref: {ref_doc})"
            })

        clearing_acc = cls.resolve_payment_account(branch, gateway, for_party='CUSTOMER')
        lines.append({
            'account': clearing_acc,
            'debit': Decimal('0.00'),
            'credit': gross,
            'narration': f"{gateway.upper()} gross collection batch clearing (Ref: {ref_doc})"
        })

        narration = (
            f"{gateway.upper()} Gateway Settlement: Gross Rs. {gross:.2f}, Fee Rs. {fee:.2f}, "
            f"Net Rs. {net:.2f} credited to Bank (Batch: {source_id})"
        )

        return JournalEngine.create_balanced_entry(
            voucher_type='JOURNAL',
            date_ad=settlement_date,
            branch=branch,
            lines=lines,
            narration=narration,
            reference_doc=ref_doc,
            source_module=source_module,
            source_id=source_id,
            user=user,
            auto_post=True
        )

    # =========================================================================
    # TRACE ANNOTATIONS
    # =========================================================================
    @classmethod
    def _enrich_voucher_payment_narrations(
        cls,
        estimate: SalesEstimate,
        payment_transactions: List[SalesPaymentTransaction],
        result_voucher=None
    ) -> None:
        """
        Annotates journal line items with external payment trace identifiers safely.
        """
        voucher = result_voucher if isinstance(result_voucher, JournalEntry) else None
        if not voucher:
            voucher = JournalEntry.objects.filter(
                voucher_type='SALES',
                reference_document=estimate.estimate_number
            ).order_by('-created_at').first()

        if not voucher:
            return

        unmatched_refs = [
            tx for tx in payment_transactions
            if tx.transaction_ref and tx.amount > Decimal('0.00')
        ]

        if not unmatched_refs:
            return

        items = list(voucher.items.filter(debit_amount__gt=Decimal('0.00')).select_related('account'))

        for tx in unmatched_refs:
            tx_ref = tx.transaction_ref
            mode_upper = tx.payment_mode.upper()
            matched_item = None

            for itm in items:
                acct_name = (itm.account.name or '').upper() if itm.account else ''
                line_narr = (getattr(itm, 'line_narration', '') or '').upper()
                if itm.debit_amount == tx.amount and (mode_upper in acct_name or mode_upper in line_narr):
                    matched_item = itm
                    break

            if not matched_item:
                for itm in items:
                    if itm.debit_amount == tx.amount:
                        matched_item = itm
                        break

            if not matched_item:
                for itm in items:
                    acct_name = (itm.account.name or '').upper() if itm.account else ''
                    if mode_upper in acct_name:
                        matched_item = itm
                        break

            if matched_item:
                current_narr = getattr(matched_item, 'line_narration', '') or ''
                if tx_ref not in current_narr:
                    ref_tag = f"[{tx.payment_mode} Ref: {tx_ref}]"
                    if current_narr:
                        matched_item.line_narration = f"{current_narr} {ref_tag}"[:255]
                    else:
                        matched_item.line_narration = f"Receipt via {tx.payment_mode} {ref_tag} for {estimate.estimate_number}"[:255]
                    matched_item.save(update_fields=['line_narration'])
                items.remove(matched_item)

# =============================================================================
# MODULE-LEVEL CONVENIENCE BRIDGES (ZERO-IMPORT FAILURE GUARANTEE)
# =============================================================================
post_sales_estimate = AutoPostingService.post_sales_estimate
post_grn_receipt = AutoPostingService.post_grn_receipt
post_grn_journal = AutoPostingService.post_grn_receipt
post_customer_payment = AutoPostingService.post_customer_payment
post_customer_repayment_journal = AutoPostingService.post_customer_payment
post_supplier_payment = AutoPostingService.post_supplier_payment
post_supplier_payout_journal = AutoPostingService.post_supplier_payment
post_sales_return = AutoPostingService.post_sales_return
post_purchase_return = AutoPostingService.post_purchase_return
post_purchase_return_journal = AutoPostingService.post_purchase_return
post_inventory_shrinkage = AutoPostingService.post_inventory_shrinkage
post_trade_in_acquisition = AutoPostingService.post_trade_in_acquisition
post_gateway_settlement = AutoPostingService.post_gateway_settlement