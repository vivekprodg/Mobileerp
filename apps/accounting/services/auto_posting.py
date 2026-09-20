"""
Double-Entry Journal Posting Engine & Automated Operational Dispatcher.

Features:
1. Three-Way Sales Tax Split:
   - Dr: Cash / Bank / Wallets or Customer Accounts Receivable (Gross Total).
   - Cr: Sales Revenue Account (Taxable Base + Non-Taxable / Exempt Base).
   - Cr: Output VAT 13% Account (Output VAT Collected).
2. Historical Backdating Integrity:
   - Accurately timestamps historical vouchers with the converted Gregorian date of the bill
     (e.g., 2080.04.01 -> 2023-07-17), locking them to the appropriate historical Nepali Fiscal Year.
3. Balance Sheet Asset Protection:
   - Completely skips COGS and Inventory Asset (Account 1310) credits when total_cost_amount is 0.00,
     preventing artificial inventory asset deficits during historical migrations.
4. Mathematical Equality Enforcement:
   - Reconciles minor 1-paisa rounding variances and enforces Sum(Debits) == Sum(Credits).
5. Omnichannel Payment Ledger Routing:
   - Cash (1110), Bank (1120), FonePay (1130), eSewa (1140), Khalti (1150), Card POS (1160), AR (1210).
6. Operational Dispatchers:
   - Sales POS Checkouts, Purchase GRNs, Customer Udhaari Repayments, Supplier Payouts,
     Sales Returns (Credit Notes), Purchase Returns (Debit Notes), Stock Damage Write-Offs,
     and Digital Gateway Batch Settlements.
"""

import uuid
from decimal import Decimal, ROUND_HALF_UP
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
        Example: JV-BR01-000001, SV-BR01-000045, PV-BR01-000012
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
            raise ValidationError(
                f"Financial posting rejected: Nepali Fiscal Year {fiscal_year_str} is audited and closed."
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
        acc = Account.objects.filter(
            system_tag=system_tag
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
    # 1. POS SALES CHECKOUT POSTING (3-WAY VAT SPLIT & HISTORICAL BACKDATING)
    # =========================================================================
    @classmethod
    @transaction.atomic
    def post_sales_estimate(cls, estimate: SalesEstimate, user=None, **kwargs) -> Optional[JournalEntry]:
        """
        Creates a balanced double-entry voucher for a finalized Sales POS Invoice.
        
        THREE-WAY TAX SPLIT & BACKDATING RULES:
        - Dr: Payment Modes (Cash, FonePay, eSewa, Bank) or Accounts Receivable = Gross Total (जम्मा बिक्री)
        - Dr: Sales Discount Allowed (if concessions were given)
        - Cr: Sales Revenue (4110) = Pre-Tax Base (Taxable Base करयोग्य बिक्री + Exempt Base कर छुट)
        - Cr: Output VAT 13% (2210) = Output Tax (कर रकम)
        - Date: Backdated strictly to estimate.bill_date_ad (matches 2080.04.01 to 2081.03.31).
        - COGS / Inventory Asset: Skipped if estimate.total_cost_amount == 0.00, keeping Account 1310 safe.
        """
        source_module = 'SALES'
        source_id = str(estimate.id)

        duplicate_filter = Q(source_module=source_module, source_id=source_id, status='POSTED')
        duplicate_filter |= Q(voucher_type='SALES', reference_document=estimate.estimate_number, status='POSTED')
        existing = JournalEntry.objects.filter(duplicate_filter).first()
        if existing:
            return existing

        branch = estimate.branch
        # Strictly backdate voucher to the bill's historical Gregorian date
        date_ad = estimate.bill_date_ad or timezone.now().date()
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

        # ---------------------------------------------------------------------
        # 1. DEBIT: Payment Settlements & Customer Udhaari
        # ---------------------------------------------------------------------
        payments = estimate.payment_transactions.all()
        has_recorded_payments = False

        for p in payments:
            if p.amount <= Decimal('0.00'):
                continue
            has_recorded_payments = True
            target_acc = cls.resolve_payment_account(branch, p.payment_mode, for_party='CUSTOMER')
            display_mode = p.get_payment_mode_display() if hasattr(p, 'get_payment_mode_display') else p.payment_mode
            ref = getattr(p, 'transaction_ref', '') or '-'
            lines.append({
                'account': target_acc,
                'debit': p.amount,
                'credit': Decimal('0.00'),
                'customer': estimate.customer,
                'narration': f"Sale collection ({display_mode}) - Ref: {ref}"
            })

        # Fallback if no payment transactions exist on historical import
        if not has_recorded_payments:
            if estimate.paid_amount > Decimal('0.00'):
                lines.append({
                    'account': cash_acc,
                    'debit': estimate.paid_amount,
                    'credit': Decimal('0.00'),
                    'customer': estimate.customer,
                    'narration': f"Cash sale on {estimate.estimate_number}"
                })

        # Change returned to customer (Credit Cash)
        if estimate.change_returned > Decimal('0.00'):
            lines.append({
                'account': cash_acc,
                'debit': Decimal('0.00'),
                'credit': estimate.change_returned,
                'narration': f"Change returned to customer on bill {estimate.estimate_number}"
            })

        # Customer Udhaari / Due Amount (Debit AR 1210)
        if estimate.due_amount > Decimal('0.00'):
            lines.append({
                'account': ar_acc,
                'debit': estimate.due_amount,
                'credit': Decimal('0.00'),
                'customer': estimate.customer,
                'narration': f"Customer Udhaari on estimate {estimate.estimate_number}"
            })

        # Old Phone Trade-In Credit (Debit Inventory Asset 1310)
        if estimate.has_trade_in_exchange and estimate.trade_in_discount_amount > Decimal('0.00'):
            lines.append({
                'account': inv_asset_acc,
                'debit': estimate.trade_in_discount_amount,
                'credit': Decimal('0.00'),
                'customer': estimate.customer,
                'narration': f"Trade-in handset buyback credit from voucher {estimate.trade_in_voucher_reference}"
            })

        # ---------------------------------------------------------------------
        # 2. DEBIT: Merchandise Sales Discounts Allowed (Concessions)
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
        # 3. CREDIT: Sales Revenue & 13% Output VAT (Three-Way Split)
        # ---------------------------------------------------------------------
        if estimate.is_vat_applicable and estimate.vat_amount > Decimal('0.00'):
            # Pre-Tax Revenue Base = Taxable Amount + Non-Taxable / Exempt Amount
            pre_tax_base = (estimate.taxable_amount + estimate.non_taxable_amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            
            if pre_tax_base <= Decimal('0.00'):
                pre_tax_base = max(Decimal('0.00'), estimate.subtotal - estimate.vat_amount)

            # If merchandise discount was debited above, revenue credit represents gross pre-discount base
            revenue_credit = (pre_tax_base + total_disc).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            lines.append({
                'account': rev_acc,
                'debit': Decimal('0.00'),
                'credit': revenue_credit,
                'narration': f"Sales revenue (Taxable Base) on {estimate.estimate_number}"
            })

            # Output VAT 13%
            vat_acc = cls.get_or_create_control_account(
                branch, 'OUTPUT_VAT', '2210', 'Output VAT 13%', 'LIABILITY', 'CREDIT'
            )
            lines.append({
                'account': vat_acc,
                'debit': Decimal('0.00'),
                'credit': estimate.vat_amount,
                'narration': f"13% Output VAT collected on {estimate.estimate_number}"
            })
        else:
            # 0% VAT / PAN Mode
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
        # 4. PENNY ROUNDING RESIDUAL RECONCILIATION
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
        # 5. COGS & INVENTORY ASSET RELIEF (PERPETUAL METHOD)
        # ---------------------------------------------------------------------
        # Completely skipped when total_cost_amount == 0.00 (protecting Account 1310 during migration)
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
    # 2. INWARD GRN PROCUREMENT POSTING
    # =========================================================================
    @classmethod
    @transaction.atomic
    def post_grn_receipt(cls, grn: GoodsReceivedNote, user=None, **kwargs) -> Optional[JournalEntry]:
        """
        Creates a balanced double-entry voucher for an approved Goods Received Note (GRN).
        - Debit: Merchandise Inventory Asset (1310 at landed cost)
        - Debit: Input VAT 13% (1410 if inward tax invoice)
        - Credit: Resolved Payment Account (for spot cash/bank/wallet paid on delivery)
        - Credit: Accounts Payable (2110 Supplier Udhaari for remaining due balance)
        """
        source_module = 'PURCHASE'
        source_id = str(grn.id)

        duplicate_filter = Q(source_module=source_module, source_id=source_id, status='POSTED')
        duplicate_filter |= Q(voucher_type='PURCHASE', reference_document=grn.grn_number, status='POSTED')
        existing = JournalEntry.objects.filter(duplicate_filter).first()
        if existing:
            return existing

        branch = grn.branch
        date_ad = grn.bill_date or timezone.now().date()
        lines: List[Dict[str, Any]] = []

        inv_asset_acc = cls.get_or_create_control_account(
            branch, 'INVENTORY_ASSET', '1310', 'Merchandise Inventory Asset', 'ASSET', 'DEBIT'
        )
        ap_acc = cls.get_or_create_control_account(
            branch, 'ACCOUNTS_PAYABLE', '2110', 'Accounts Payable (Trade Creditors)', 'LIABILITY', 'CREDIT'
        )

        lines.append({
            'account': inv_asset_acc,
            'debit': grn.total_landed_cost,
            'credit': Decimal('0.00'),
            'supplier': grn.supplier,
            'narration': f"Stock received at landed cost under GRN {grn.grn_number} (Inv: {grn.supplier_bill_no})"
        })

        if grn.is_vat_bill and grn.vat_amount > Decimal('0.00'):
            input_vat_acc = cls.get_or_create_control_account(
                branch, 'INPUT_VAT', '1410', 'Input VAT 13%', 'ASSET', 'DEBIT'
            )
            lines.append({
                'account': input_vat_acc,
                'debit': grn.vat_amount,
                'credit': Decimal('0.00'),
                'supplier': grn.supplier,
                'narration': f"Input VAT claimed on supplier invoice {grn.supplier_bill_no}"
            })

        if grn.paid_amount > Decimal('0.00'):
            payment_mode = getattr(grn, 'payment_mode', 'CASH') or 'CASH'
            spot_acc = cls.resolve_payment_account(branch, payment_mode, for_party='SUPPLIER')
            lines.append({
                'account': spot_acc,
                'debit': Decimal('0.00'),
                'credit': grn.paid_amount,
                'supplier': grn.supplier,
                'narration': f"Spot payment made to {grn.supplier.company_name} on GRN {grn.grn_number} via {payment_mode}"
            })

        if grn.due_amount > Decimal('0.00'):
            lines.append({
                'account': ap_acc,
                'debit': Decimal('0.00'),
                'credit': grn.due_amount,
                'supplier': grn.supplier,
                'narration': f"Payable debt to {grn.supplier.company_name} on bill {grn.supplier_bill_no}"
            })

        narration = f"Inward GRN stock procurement from {grn.supplier.company_name} (Challan: {grn.supplier_bill_no})"
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

    # Alias for backward compatibility
    post_grn_journal = post_grn_receipt

    # =========================================================================
    # 3. CUSTOMER DEBT REPAYMENT (UDHAARI SETTLEMENT)
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
        Posts customer Udhaari debt collection into double-entry accounts:
        - Debit: Resolved Payment Account (Cash, FonePay, eSewa, Khalti, Bank)
        - Credit: Accounts Receivable (1210 Customer Sub-Ledger)
        """
        if ledger_entry:
            cust = ledger_entry.customer
            amt = ledger_entry.amount
            mode = ledger_entry.payment_mode or 'CASH'
            br = ledger_entry.branch or getattr(cust, 'preferred_branch', None) or Branch.get_default_main_branch()
            ref_doc = getattr(ledger_entry, 'reference_invoice', None) or f"UDH-CUST-{ledger_entry.id}"
            source_id = str(ledger_entry.id)
            date_ad = ledger_entry.created_at.date() if hasattr(ledger_entry.created_at, 'date') else ledger_entry.created_at
            user = user or getattr(ledger_entry, 'recorded_by', None)
        else:
            cust = customer
            amt = Decimal(str(amount or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            mode = payment_mode
            br = branch or getattr(cust, 'preferred_branch', None) or Branch.get_default_main_branch()
            ref_doc = reference or f"UDH-CUST-{cust.id}-{timezone.now().strftime('%Y%m%d%H%M%S')}"
            source_id = ref_doc
            date_ad = timezone.now().date()

        if amt <= Decimal('0.00') or not cust:
            return None

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

    # Alias for backward compatibility
    post_customer_repayment_journal = post_customer_payment

    # =========================================================================
    # 4. SUPPLIER DEBT PAYOUT
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
        - Debit: Accounts Payable (2110 Supplier Sub-Ledger)
        - Credit: Resolved Payment Account (Cash, Bank, Wallet)
        """
        if ledger_entry:
            supp = ledger_entry.supplier
            amt = ledger_entry.amount
            mode = ledger_entry.payment_mode or 'CASH'
            br = ledger_entry.branch or Branch.get_default_main_branch()
            ref_doc = getattr(ledger_entry, 'reference_number', None) or f"SUP-PAY-{ledger_entry.id}"
            source_id = str(ledger_entry.id)
            date_ad = ledger_entry.created_at.date() if hasattr(ledger_entry.created_at, 'date') else ledger_entry.created_at
            user = user or getattr(ledger_entry, 'recorded_by', None)
        else:
            supp = supplier
            amt = Decimal(str(amount or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            mode = payment_mode
            br = branch or Branch.get_default_main_branch()
            ref_doc = ref_no or f"SUP-PAY-{supp.id}-{timezone.now().strftime('%Y%m%d%H%M%S')}"
            source_id = ref_doc
            date_ad = timezone.now().date()

        if amt <= Decimal('0.00') or not supp:
            return None

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

    # Alias for backward compatibility
    post_supplier_payout_journal = post_supplier_payment

    # =========================================================================
    # 5. CUSTOMER SALES RETURN POSTING
    # =========================================================================
    @classmethod
    @transaction.atomic
    def post_sales_return(cls, sales_return: SalesReturn, user=None, **kwargs) -> Optional[JournalEntry]:
        """
        Posts customer return or exchange credit note:
        - Debit: Sales Returns & Deductions (4020)
        - Debit: Inventory Asset (1310 working) OR Quarantine Asset (1330 defective)
        - Credit: Cash/Bank/Wallet (refund) OR Accounts Receivable (1210 store credit)
        - Credit: COGS (5110 reversal)
        """
        source_module = 'SALES_RETURN'
        source_id = str(sales_return.id)

        duplicate_filter = Q(source_module=source_module, source_id=source_id, status='POSTED')
        duplicate_filter |= Q(voucher_type='CREDIT_NOTE', reference_document=sales_return.return_number, status='POSTED')
        existing = JournalEntry.objects.filter(duplicate_filter).first()
        if existing:
            return existing

        branch = sales_return.branch
        date_ad = sales_return.return_date_ad or timezone.now().date()
        lines: List[Dict[str, Any]] = []

        ret_rev_acc = cls.get_or_create_control_account(
            branch, 'SALES_RETURN', '4020', 'Sales Returns & Deductions', 'REVENUE', 'DEBIT'
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

        lines.append({
            'account': ret_rev_acc,
            'debit': sales_return.total_refund_amount,
            'credit': Decimal('0.00'),
            'customer': sales_return.customer,
            'narration': f"Sales return voucher {sales_return.return_number} on bill {sales_return.original_estimate.estimate_number}"
        })

        refund_mode = str(sales_return.refund_mode or '').upper()
        if refund_mode in ['CREDIT', 'STORE_CREDIT', 'UDHAARI', 'CREDIT_NOTE']:
            lines.append({
                'account': ar_acc,
                'debit': Decimal('0.00'),
                'credit': sales_return.total_refund_amount,
                'customer': sales_return.customer,
                'narration': f"Store credit / Udhaari adjustment on return {sales_return.return_number}"
            })
        else:
            refund_acc = cls.resolve_payment_account(branch, refund_mode, for_party='CUSTOMER')
            lines.append({
                'account': refund_acc,
                'debit': Decimal('0.00'),
                'credit': sales_return.total_refund_amount,
                'narration': f"Refund ({sales_return.refund_mode}) issued to customer on return {sales_return.return_number}"
            })

        total_restocked_cogs = Decimal('0.00')
        total_quarantined_cogs = Decimal('0.00')

        for item in sales_return.items.select_related('estimate_item', 'product'):
            unit_cost = item.estimate_item.cost_price or item.product.purchase_price
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
                'narration': "Restock sellable returned goods at landed cost"
            })
            lines.append({
                'account': cogs_acc,
                'debit': Decimal('0.00'),
                'credit': total_restocked_cogs,
                'narration': "COGS reversal on restocked return items"
            })

        if total_quarantined_cogs > Decimal('0.00'):
            lines.append({
                'account': quar_asset_acc,
                'debit': total_quarantined_cogs,
                'credit': Decimal('0.00'),
                'narration': "Route defective returned items to quarantine asset"
            })
            lines.append({
                'account': cogs_acc,
                'debit': Decimal('0.00'),
                'credit': total_quarantined_cogs,
                'narration': "COGS reversal for defective items quarantined for RMA"
            })

        narration = f"Sales Return Voucher {sales_return.return_number} (Ref Bill: {sales_return.original_estimate.estimate_number})"
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
    # 6. COMMERCIAL PURCHASE RETURN (DEBIT NOTE) POSTING
    # =========================================================================
    @classmethod
    @transaction.atomic
    def post_purchase_return(cls, purchase_return: PurchaseReturn, user=None, **kwargs) -> Optional[JournalEntry]:
        """
        Posts commercial purchase return / debit note to supplier:
        - Debit: Accounts Payable (2110) OR Cash in Hand (if cash refund received)
        - Credit: Merchandise Inventory Asset (1310 at purchase value)
        - Credit: Input VAT 13% (1410 reversal if tax bill)
        """
        source_module = 'PURCHASE_RETURN'
        source_id = str(purchase_return.id)

        duplicate_filter = Q(source_module=source_module, source_id=source_id, status='POSTED')
        duplicate_filter |= Q(voucher_type='DEBIT_NOTE', reference_document=purchase_return.return_number, status='POSTED')
        existing = JournalEntry.objects.filter(duplicate_filter).first()
        if existing:
            return existing

        branch = purchase_return.branch
        date_ad = purchase_return.return_date or timezone.now().date()
        lines: List[Dict[str, Any]] = []

        ap_acc = cls.get_or_create_control_account(
            branch, 'ACCOUNTS_PAYABLE', '2110', 'Accounts Payable (Trade Creditors)', 'LIABILITY', 'CREDIT'
        )
        cash_acc = cls.get_or_create_control_account(
            branch, 'CASH', '1110', 'Cash in Hand', 'ASSET', 'DEBIT'
        )
        inv_asset_acc = cls.get_or_create_control_account(
            branch, 'INVENTORY_ASSET', '1310', 'Merchandise Inventory Asset', 'ASSET', 'DEBIT'
        )
        input_vat_acc = cls.get_or_create_control_account(
            branch, 'INPUT_VAT', '1410', 'Input VAT 13%', 'ASSET', 'DEBIT'
        )

        if purchase_return.refund_mode == 'CASH_REFUND':
            lines.append({
                'account': cash_acc,
                'debit': purchase_return.net_refund_amount,
                'credit': Decimal('0.00'),
                'supplier': purchase_return.supplier,
                'narration': f"Cash refund received on Debit Note {purchase_return.return_number}"
            })
        else:
            lines.append({
                'account': ap_acc,
                'debit': purchase_return.net_refund_amount,
                'credit': Decimal('0.00'),
                'supplier': purchase_return.supplier,
                'narration': f"Accounts Payable reduced on Debit Note {purchase_return.return_number}"
            })

        lines.append({
            'account': inv_asset_acc,
            'debit': Decimal('0.00'),
            'credit': purchase_return.total_return_amount,
            'supplier': purchase_return.supplier,
            'narration': f"Merchandise inventory returned to vendor {purchase_return.supplier.company_name}"
        })

        if purchase_return.tax_amount > Decimal('0.00'):
            lines.append({
                'account': input_vat_acc,
                'debit': Decimal('0.00'),
                'credit': purchase_return.tax_amount,
                'supplier': purchase_return.supplier,
                'narration': f"Input VAT claimed reversal on Debit Note {purchase_return.return_number}"
            })

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

    # Alias for backward compatibility
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
    # 8. DIGITAL GATEWAY BATCH SETTLEMENT
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
                branch, 'GATEWAY_COMMISSION', '6190', 'Payment Gateway & Bank Merchant Fees (MDR)', 'INDIRECT_EXPENSE', 'DEBIT'
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
post_gateway_settlement = AutoPostingService.post_gateway_settlement