"""
Central VAT & Tax Assessment Ledger Service.

The single authoritative source of truth across the entire ERP for:
1. Sales VAT (Annex 5 Sales Book & Outward Billing)
2. Purchase VAT (Annex 7 Purchase Book & Inward Consignments)
3. Sales Return VAT (Credit Notes & Output Tax Reversals)
4. Purchase Return VAT (Debit Notes & Input Tax Reversals)
5. VAT Transaction Classification (13% Standard, 0% Non-Taxable / Exempt, Exclusive vs Inclusive)
6. Hierarchical Multi-Tier Drill-Down:
   - 6 Months ➔ Month ➔ Day ➔ Invoices / Returns / GRNs / Debit Notes ➔ Line Items ➔ GL Journal
   - Sales Invoice Drilldown (Invoice ➔ Items ➔ GL Account 2210)
   - Inward GRN Drilldown (GRN ➔ Items ➔ GL Account 1410)
   - Sales Return Drilldown (Sales Return ➔ Credit Note ➔ GL Journal Entry ➔ Journal Items ➔ Output VAT 2210 Reversal, with Original Invoice as reference)
   - Purchase Return Drilldown (Purchase Return ➔ Debit Note ➔ GL Journal Entry ➔ Journal Items ➔ Input VAT 1410 Reversal, with Original GRN as reference)
7. Net VAT Assessment & Multi-Period Summaries (Daily, Monthly, 6-Month, Fiscal Year)
8. 6-Month Period-Over-Period Comparison (Current 6 Months vs. Previous 6 Months with Difference & % Variance)
9. General Ledger Parity Reconciliation:
   - Total-Level Reconciliation (VAT Registers vs GL Accounts 2210 & 1410)
   - Full Transaction-Level VAT ↔ GL Reconciliation (Expected VAT → GL VAT → Difference → Matched/Unmatched)
   - Identifies: Missing GL entries, Orphan/Manual GL entries, Duplicate postings, Amount/Date/Branch/Reference mismatches, and Return Header vs Item differences
   - Does NOT trust account totals alone: Matches using source_module, source_id, reference_document, voucher_type, branch, and date

Auditing Invariants:
- Historical Invoices are matched by original sale date across COMPLETED, PARTIALLY_RETURNED,
  and RETURNED statuses so that later returns never remove the original sale from historical registers.
- Sales Returns and Purchase Returns are recorded as separate reversal events on their actual return dates.
- Net tax figures are never artificially clamped to zero (no max(0) zero-clamping), allowing valid
  negative tax credit movements and audit traceability across all periods.
"""

import logging
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Dict, Any, List, Optional, Tuple, Union, Set

from django.db.models import Sum, Q, F, DecimalField, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.sales.models import SalesEstimate, SalesEstimateItem, SalesReturn, SalesReturnItem
from apps.purchases.models import GoodsReceivedNote, GRNItem, PurchaseReturn, PurchaseReturnItem
from apps.branches.models import Branch
from apps.accounting.models import JournalEntry, JournalItem, Account
from apps.core.models import SystemConfiguration
from apps.core.nepali_calendar import NepaliCalendar

logger = logging.getLogger(__name__)


class VATLedgerService:
    """
    Unified VAT calculation and reconciliation engine.
    Ensures Dashboard, Daily Ledger, Monthly Reports, 6-Month Summaries, Fiscal Year Audits,
    and CSV/Excel Exports compute tax consistently.
    """

    STANDARD_VAT_RATE = Decimal('13.00')
    ZERO_VAT_RATE = Decimal('0.00')

    # Statuses that represent valid completed sales for historical tax registers
    VALID_SALES_STATUSES = ['COMPLETED', 'PARTIALLY_RETURNED', 'RETURNED']

    # =========================================================================
    # 1. VAT TRANSACTION CLASSIFICATION & LINE ITEM EXTRACTION
    # =========================================================================
    @classmethod
    def classify_vat_rate(cls, rate_val: Any) -> Decimal:
        """
        Normalizes any tax rate input to either 13.00% or 0.00% (Exempt).
        """
        if rate_val is None:
            return cls.ZERO_VAT_RATE
        try:
            val = Decimal(str(rate_val)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            return cls.STANDARD_VAT_RATE if val > Decimal('0.00') else cls.ZERO_VAT_RATE
        except (InvalidOperation, ValueError, TypeError):
            return cls.ZERO_VAT_RATE

    @classmethod
    def extract_sales_item_vat_detail(
        cls,
        item: SalesEstimateItem,
        is_vat_shop: bool = True
    ) -> Dict[str, Any]:
        """
        Extracts the immutable line-level VAT snapshot for a SalesEstimateItem.
        Guarantees that historical product master changes never alter historical records.
        """
        qty = item.quantity or Decimal('1.000')
        taxable_base = item.base_taxable_amount or item.taxable_line_amount or Decimal('0.00')
        vat_rate = item.vat_rate if (item.is_vat_applicable and item.vat_rate is not None) else Decimal('0.00')
        vat_amt = item.tax_amount or Decimal('0.00')
        line_tot = item.line_total or Decimal('0.00')

        if not is_vat_shop:
            taxable_base = Decimal('0.00')
            vat_amt = Decimal('0.00')
            vat_rate = Decimal('0.00')
            non_taxable_base = line_tot
        else:
            is_taxable = bool(item.is_vat_applicable and vat_rate > Decimal('0.00'))
            non_taxable_base = Decimal('0.00') if is_taxable else line_tot

        return {
            'item_id': item.id,
            'product_id': item.product_id,
            'product_name': item.product.name if item.product else (item.device_condition or 'Merchandise Item'),
            'product_sku': item.product.sku if item.product else '',
            'quantity': qty,
            'unit_price': item.unit_price or Decimal('0.00'),
            'discount_amount': item.discount_amount or Decimal('0.00'),
            'is_vat_applicable': bool(is_vat_shop and item.is_vat_applicable and vat_rate > Decimal('0.00')),
            'tax_pricing_type': item.tax_pricing_type or 'INCLUSIVE',
            'vat_rate': vat_rate,
            'taxable_amount': taxable_base,
            'non_taxable_amount': non_taxable_base,
            'vat_amount': vat_amt,
            'line_total': line_tot,
            'imei_number': item.imei_number or '',
            'secondary_imei': item.secondary_imei or '',
        }

    @classmethod
    def extract_sales_return_item_vat_detail(
        cls,
        r_item: SalesReturnItem,
        is_vat_shop: bool = True
    ) -> Dict[str, Any]:
        """
        Extracts line-level VAT reversal details for a SalesReturnItem.
        1. Uses stored return VAT snapshots first (taxable_return_amount, vat_reversal_amount, non_taxable_return_amount).
        2. For missing legacy snapshots, uses the reliable calculation based on the
           original invoice item's stored VAT figures and returned quantity.
        """
        refund_amt = (r_item.refund_amount or Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        if not is_vat_shop:
            return {
                'item_id': r_item.id,
                'product_id': r_item.product_id,
                'product_name': r_item.product.name if r_item.product else 'Returned Item',
                'return_quantity': r_item.return_quantity or Decimal('1.000'),
                'refund_amount': refund_amt,
                'vat_rate': Decimal('0.00'),
                'taxable_amount': Decimal('0.00'),
                'vat_amount': Decimal('0.00'),
                'non_taxable_amount': refund_amt,
                'is_defective': getattr(r_item, 'is_defective', False),
                'defect_reason': getattr(r_item, 'defect_reason', '') or '',
                'returned_imei': getattr(r_item, 'returned_imei', '') or '',
            }

        # 1. Use stored return VAT snapshots first
        has_snapshot = (
            (r_item.taxable_return_amount or Decimal('0.00')) > Decimal('0.00') or
            (r_item.vat_reversal_amount or Decimal('0.00')) > Decimal('0.00') or
            (r_item.non_taxable_return_amount or Decimal('0.00')) > Decimal('0.00')
        )
        if has_snapshot:
            return {
                'item_id': r_item.id,
                'product_id': r_item.product_id,
                'product_name': r_item.product.name if r_item.product else 'Returned Item',
                'return_quantity': r_item.return_quantity or Decimal('1.000'),
                'refund_amount': refund_amt,
                'vat_rate': r_item.vat_rate or Decimal('0.00'),
                'taxable_amount': r_item.taxable_return_amount or Decimal('0.00'),
                'vat_amount': r_item.vat_reversal_amount or Decimal('0.00'),
                'non_taxable_amount': r_item.non_taxable_return_amount or Decimal('0.00'),
                'is_defective': getattr(r_item, 'is_defective', False),
                'defect_reason': getattr(r_item, 'defect_reason', '') or '',
                'returned_imei': getattr(r_item, 'returned_imei', '') or '',
            }

        # 2. For missing legacy snapshots, use the reliable calculation based on the
        # original invoice item's stored VAT figures and returned quantity
        taxable_base = Decimal('0.00')
        vat_amt = Decimal('0.00')
        non_taxable = refund_amt
        vat_rate = Decimal('0.00')

        est_item = getattr(r_item, 'estimate_item', None)
        if est_item:
            orig_qty = est_item.quantity if (est_item.quantity and est_item.quantity > Decimal('0.000')) else Decimal('1.000')
            return_qty = r_item.return_quantity if (r_item.return_quantity and r_item.return_quantity > Decimal('0.000')) else Decimal('1.000')

            orig_base_taxable = (est_item.base_taxable_amount or est_item.taxable_line_amount or Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            orig_vat = (est_item.tax_amount or Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            orig_total = (est_item.line_total or Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            orig_vat_rate = (est_item.vat_rate or Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            is_taxable = bool(
                est_item.is_vat_applicable and 
                orig_vat_rate > Decimal('0.00') and 
                (orig_vat > Decimal('0.00') or orig_base_taxable > Decimal('0.00'))
            )

            # Defensive recovery for legacy invoices where VAT rate was set but base/tax were not snapshotted
            if not is_taxable and est_item.is_vat_applicable and orig_vat_rate > Decimal('0.00') and orig_total > Decimal('0.00'):
                divisor = Decimal('1.00') + (orig_vat_rate / Decimal('100.00'))
                orig_base_taxable = (orig_total / divisor).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                orig_vat = (orig_total - orig_base_taxable).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                is_taxable = (orig_vat > Decimal('0.00'))

            if is_taxable:
                vat_rate = orig_vat_rate
                ratio = min(Decimal('1.000000'), return_qty / orig_qty) if orig_qty > Decimal('0.000') else Decimal('1.000000')
                vat_amt = (orig_vat * ratio).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                taxable_base = max(Decimal('0.00'), refund_amt - vat_amt)
                non_taxable = Decimal('0.00')
            else:
                vat_rate = Decimal('0.00')
                taxable_base = Decimal('0.00')
                vat_amt = Decimal('0.00')
                non_taxable = refund_amt

        return {
            'item_id': r_item.id,
            'product_id': r_item.product_id,
            'product_name': r_item.product.name if r_item.product else 'Returned Item',
            'return_quantity': r_item.return_quantity or Decimal('1.000'),
            'refund_amount': refund_amt,
            'vat_rate': vat_rate,
            'taxable_amount': taxable_base,
            'vat_amount': vat_amt,
            'non_taxable_amount': non_taxable,
            'is_defective': getattr(r_item, 'is_defective', False),
            'defect_reason': getattr(r_item, 'defect_reason', '') or '',
            'returned_imei': getattr(r_item, 'returned_imei', '') or '',
        }

    @classmethod
    def extract_grn_item_vat_detail(cls, item: GRNItem) -> Dict[str, Any]:
        """
        Extracts line-level inward VAT details for a GRNItem.
        """
        qty = item.purchased_quantity or Decimal('1.000')
        rate = item.purchase_rate or Decimal('0.00')
        vat_rate = item.vat_rate if (item.is_vat_applicable and item.vat_rate is not None) else Decimal('0.00')
        vat_amt = item.tax_amount or Decimal('0.00')
        is_taxable = bool(item.is_vat_applicable and vat_rate > Decimal('0.00'))

        taxable_base = item.line_total if is_taxable else Decimal('0.00')
        non_taxable_base = item.line_total if not is_taxable else Decimal('0.00')

        return {
            'item_id': item.id,
            'product_id': item.product_id,
            'product_name': item.product.name if item.product else 'Purchased Item',
            'supplier_item_code': item.supplier_item_code or '',
            'batch_number': item.batch_number or '',
            'quantity': qty,
            'purchase_rate': rate,
            'item_discount': item.item_discount_amount or Decimal('0.00'),
            'is_vat_applicable': is_taxable,
            'vat_rate': vat_rate,
            'taxable_amount': taxable_base,
            'non_taxable_amount': non_taxable_base,
            'vat_amount': vat_amt,
            'unit_landed_cost': item.unit_landed_cost or Decimal('0.00'),
            'line_total': (item.line_total or Decimal('0.00')) + vat_amt,
        }

    @classmethod
    def extract_purchase_return_item_vat_detail(cls, item: PurchaseReturnItem) -> Dict[str, Any]:
        """
        Extracts line-level debit note VAT reversal details for a PurchaseReturnItem.
        """
        qty = item.returned_quantity or Decimal('1.000')
        rate = item.purchase_rate or Decimal('0.00')
        tax_rate = item.tax_rate or Decimal('0.00')
        vat_amt = item.tax_amount or Decimal('0.00')
        gross = (qty * rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        is_taxable = bool(tax_rate > Decimal('0.00'))

        taxable_base = gross if is_taxable else Decimal('0.00')
        non_taxable_base = gross if not is_taxable else Decimal('0.00')

        return {
            'item_id': item.id,
            'product_id': item.product_id,
            'product_name': item.product.name if item.product else 'Debit Note Item',
            'returned_quantity': qty,
            'purchase_rate': rate,
            'tax_rate': tax_rate,
            'taxable_amount': taxable_base,
            'non_taxable_amount': non_taxable_base,
            'vat_amount': vat_amt,
            'line_total': item.line_total or (gross + vat_amt),
            'returned_imei_list': item.returned_imei_list or '',
        }

    # =========================================================================
    # 2. SALES VAT AGGREGATION (ANNEX 5 / SALES BOOK)
    # =========================================================================
    @classmethod
    def get_sales_vat_summary(
        cls,
        branch: Optional[Branch],
        start_date: date,
        end_date: date,
        include_drilldown: bool = True
    ) -> Dict[str, Any]:
        """
        Compiles the authoritative Annex 5 Sales Book:
        1. Selects sales invoices using original sale date across COMPLETED,
           PARTIALLY_RETURNED, and RETURNED statuses.
        2. Selects customer sales returns strictly by return_date_ad as separate reversal events.
        3. Separates Gross Sales VAT and Sales Return VAT Reversals without zero-clamping.
        4. Exposes the Credit Note and GL Journal Entry linkage for returns.
        """
        config = SystemConfiguration.get_solo()
        is_vat_shop = (config.tax_system_mode == 'VAT')

        sales_filter: Dict[str, Any] = {
            'bill_date_ad__gte': start_date,
            'bill_date_ad__lte': end_date,
            'status__in': cls.VALID_SALES_STATUSES
        }
        if branch:
            sales_filter['branch'] = branch

        sales_qs = SalesEstimate.objects.filter(
            **sales_filter
        ).select_related(
            'customer', 'cashier', 'salesperson', 'branch'
        ).prefetch_related(
            'items__product', 'items__unit_conversion', 'returns__items__product'
        ).order_by('bill_date_ad', 'estimate_number')

        totals = sales_qs.aggregate(
            total_taxable=Coalesce(Sum('taxable_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_non_taxable=Coalesce(Sum('non_taxable_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_vat=Coalesce(Sum('vat_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_grand=Coalesce(Sum('grand_total'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_subtotal=Coalesce(Sum('subtotal'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2)))
        )

        taxable_sum = totals['total_taxable']
        non_taxable_sum = totals['total_non_taxable']
        vat_sum = totals['total_vat']
        grand_sum = totals['total_grand']

        returns_filter: Dict[str, Any] = {
            'return_date_ad__gte': start_date,
            'return_date_ad__lte': end_date
        }
        if branch:
            returns_filter['branch'] = branch

        period_returns_qs = SalesReturn.objects.filter(
            **returns_filter
        ).select_related(
            'customer', 'original_estimate', 'processed_by', 'branch'
        ).prefetch_related(
            'items__product', 'items__estimate_item'
        ).order_by('return_date_ad', 'return_number')

        total_refund_amount = period_returns_qs.aggregate(
            total_refund=Coalesce(Sum('total_refund_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2)))
        )['total_refund']

        return_taxable_deduct = Decimal('0.00')
        return_vat_deduct = Decimal('0.00')
        return_non_taxable_deduct = Decimal('0.00')

        ret_gl_map = {}
        if include_drilldown and period_returns_qs.exists():
            ret_doc_numbers = [ret.return_number for ret in period_returns_qs]
            ret_gl_map = cls._fetch_gl_journal_map(ret_doc_numbers, voucher_type='CREDIT_NOTE')
            missing_ret_docs = [d for d in ret_doc_numbers if d not in ret_gl_map]
            if missing_ret_docs:
                ret_gl_map.update(cls._fetch_gl_journal_map(missing_ret_docs))

        returns_breakdown = []
        for ret in period_returns_qs:
            ret_items_detail = []
            ret_taxable_sub = Decimal('0.00')
            ret_vat_sub = Decimal('0.00')
            ret_non_taxable_sub = Decimal('0.00')

            for r_item in ret.items.all():
                item_detail = cls.extract_sales_return_item_vat_detail(r_item, is_vat_shop)
                ret_items_detail.append(item_detail)
                ret_taxable_sub += item_detail['taxable_amount']
                ret_vat_sub += item_detail['vat_amount']
                ret_non_taxable_sub += item_detail['non_taxable_amount']

            return_taxable_deduct += ret_taxable_sub
            return_vat_deduct += ret_vat_sub
            return_non_taxable_deduct += ret_non_taxable_sub

            if include_drilldown:
                gl_info = ret_gl_map.get(ret.return_number)
                returns_breakdown.append({
                    'id': ret.id,
                    'return_number': ret.return_number,
                    'original_estimate_number': ret.original_estimate.estimate_number if ret.original_estimate else 'N/A',
                    'original_estimate_id': ret.original_estimate_id,
                    'original_invoice_number': ret.original_estimate.estimate_number if ret.original_estimate else 'N/A',
                    'customer_name': ret.customer.name if ret.customer else (ret.original_estimate.recipient_display_name if ret.original_estimate else 'Walk-in'),
                    'return_date_ad': ret.return_date_ad,
                    'return_date_bs': ret.return_date_bs or '',
                    'fiscal_year': ret.fiscal_year or '',
                    'refund_mode': ret.get_refund_mode_display(),
                    'reason': ret.reason or '',
                    'total_refund_amount': ret.total_refund_amount,
                    'taxable_amount': ret_taxable_sub,
                    'vat_amount': ret_vat_sub,
                    'non_taxable_amount': ret_non_taxable_sub,
                    'credit_note_number': gl_info.get('voucher_number') if gl_info else None,
                    'gl_entry': gl_info,
                    'items': ret_items_detail
                })

        return_taxable_deduct = return_taxable_deduct.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        return_vat_deduct = return_vat_deduct.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        return_non_taxable_deduct = return_non_taxable_deduct.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        if is_vat_shop:
            net_taxable = (taxable_sum - return_taxable_deduct).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            net_vat = (vat_sum - return_vat_deduct).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            net_non_taxable = (non_taxable_sum - return_non_taxable_deduct).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            net_grand = (grand_sum - total_refund_amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        else:
            net_taxable = Decimal('0.00')
            net_vat = Decimal('0.00')
            net_grand = (grand_sum - total_refund_amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            net_non_taxable = net_grand

        document_breakdown = []
        if include_drilldown:
            doc_numbers = [est.estimate_number for est in sales_qs]
            gl_map = cls._fetch_gl_journal_map(doc_numbers, voucher_type='SALES')

            for est in sales_qs:
                items_detail = [
                    cls.extract_sales_item_vat_detail(item, is_vat_shop)
                    for item in est.items.all()
                ]

                document_breakdown.append({
                    'id': est.id,
                    'estimate_number': est.estimate_number,
                    'bill_type': getattr(est, 'bill_type', 'SALES'),
                    'bill_type_display': est.get_bill_type_display() if hasattr(est, 'get_bill_type_display') else 'Sales Invoice',
                    'bill_date_ad': est.bill_date_ad,
                    'bill_date_bs': est.bill_date_bs or '',
                    'fiscal_year': est.fiscal_year or '',
                    'customer_id': est.customer_id,
                    'customer_name': est.recipient_display_name,
                    'customer_phone': est.customer_phone_manual or (est.customer.phone_number if est.customer else ''),
                    'customer_pan': est.customer_pan or (est.customer.pan_number if est.customer and est.customer.pan_number else ''),
                    'subtotal': est.subtotal,
                    'discount_total': est.total_sales_discount,
                    'taxable_amount': est.taxable_amount if is_vat_shop else Decimal('0.00'),
                    'non_taxable_amount': est.non_taxable_amount if is_vat_shop else est.grand_total,
                    'vat_amount': est.vat_amount if is_vat_shop else Decimal('0.00'),
                    'grand_total': est.grand_total,
                    'paid_amount': est.paid_amount,
                    'due_amount': est.due_amount,
                    'status': est.status,
                    'status_display': est.get_status_display(),
                    'gl_entry': gl_map.get(est.estimate_number),
                    'items': items_detail
                })

        return {
            'records': sales_qs,
            'returns': period_returns_qs,
            'is_vat_shop': is_vat_shop,
            'document_breakdown': document_breakdown,
            'returns_breakdown': returns_breakdown,
            'totals': {
                'taxable': net_taxable,
                'non_taxable': net_non_taxable,
                'vat': net_vat,
                'grand_total': net_grand,
                'gross_taxable_before_returns': taxable_sum,
                'gross_vat_before_returns': vat_sum,
                'sales_output_vat': vat_sum,
                'sales_return_vat_reversal': return_vat_deduct,
                'net_output_vat': net_vat,
                'total_refunds': total_refund_amount,
                'return_taxable_deduct': return_taxable_deduct,
                'return_vat_deduct': return_vat_deduct,
                'return_non_taxable_deduct': return_non_taxable_deduct,
            }
        }

    # =========================================================================
    # 3. PURCHASE VAT AGGREGATION (ANNEX 7 / PURCHASE BOOK)
    # =========================================================================
    @classmethod
    def get_purchase_vat_summary(
        cls,
        branch: Optional[Branch],
        start_date: date,
        end_date: date,
        include_drilldown: bool = True
    ) -> Dict[str, Any]:
        """
        Compiles the authoritative Annex 7 Purchase Register:
        1. Selects inward GRNs verified within the date period.
        2. Selects confirmed supplier debit notes on return_date.
        3. Separates Input VAT from GRNs and Input VAT Reversals from Debit Notes.
        """
        grn_filter: Dict[str, Any] = {
            'bill_date__gte': start_date,
            'bill_date__lte': end_date,
            'status': 'RECEIVED'
        }
        if branch:
            grn_filter['branch'] = branch

        grn_qs = GoodsReceivedNote.objects.filter(
            **grn_filter
        ).select_related(
            'supplier', 'branch', 'received_by'
        ).prefetch_related(
            'items__product', 'items__unit_conversion'
        ).order_by('bill_date', 'grn_number')

        totals = grn_qs.aggregate(
            total_gross=Coalesce(Sum('gross_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_discount=Coalesce(Sum('discount_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_taxable=Coalesce(Sum('taxable_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_vat=Coalesce(Sum('vat_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_freight=Coalesce(Sum('extra_freight_charge'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_customs=Coalesce(Sum('customs_import_charge'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_handling=Coalesce(Sum('other_handling_charge'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_insurance=Coalesce(Sum('insurance_charge'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_other_ovh=Coalesce(Sum('other_overheads_charge'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_landed=Coalesce(Sum('total_landed_cost'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_net=Coalesce(Sum('net_total_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_paid=Coalesce(Sum('paid_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_due=Coalesce(Sum('due_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2)))
        )

        total_overheads = (
            totals['total_freight'] + totals['total_customs'] +
            totals['total_handling'] + totals['total_insurance'] + totals['total_other_ovh']
        ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        pret_filter: Dict[str, Any] = {
            'return_date__gte': start_date,
            'return_date__lte': end_date,
            'status': 'CONFIRMED'
        }
        if branch:
            pret_filter['branch'] = branch

        purchase_returns_qs = PurchaseReturn.objects.filter(
            **pret_filter
        ).select_related(
            'supplier', 'branch', 'processed_by', 'original_grn'
        ).prefetch_related(
            'items__product'
        ).order_by('return_date', 'return_number')

        pret_totals = purchase_returns_qs.aggregate(
            total_return_taxable=Coalesce(Sum('total_return_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_return_vat=Coalesce(Sum('tax_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_return_refund=Coalesce(Sum('net_refund_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2)))
        )

        net_purchase_taxable = (totals['total_taxable'] - pret_totals['total_return_taxable']).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        net_purchase_vat = (totals['total_vat'] - pret_totals['total_return_vat']).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        net_purchase_net = (totals['total_net'] - pret_totals['total_return_refund']).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        net_purchase_gross = (totals['total_gross'] - pret_totals['total_return_taxable']).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        net_purchase_landed = (totals['total_landed'] - pret_totals['total_return_taxable']).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        document_breakdown = []
        returns_breakdown = []

        if include_drilldown:
            grn_doc_numbers = [g.grn_number for g in grn_qs]
            grn_gl_map = cls._fetch_gl_journal_map(grn_doc_numbers, voucher_type='PURCHASE')

            for grn in grn_qs:
                items_detail = [
                    cls.extract_grn_item_vat_detail(item)
                    for item in grn.items.all()
                ]

                document_breakdown.append({
                    'id': grn.id,
                    'grn_number': grn.grn_number,
                    'supplier_bill_no': grn.supplier_bill_no,
                    'challan_no': grn.challan_no or '',
                    'supplier_id': grn.supplier_id,
                    'supplier_name': grn.supplier.company_name,
                    'supplier_pan': grn.supplier.pan_number or grn.supplier.vat_number or '',
                    'bill_date': grn.bill_date,
                    'bill_date_bs': grn.bill_date_bs or '',
                    'fiscal_year': grn.fiscal_year or '',
                    'vat_handling_mode': grn.vat_handling_mode,
                    'is_vat_bill': grn.is_vat_bill,
                    'gross_amount': grn.gross_amount,
                    'discount_amount': grn.discount_amount,
                    'taxable_amount': grn.taxable_amount,
                    'vat_amount': grn.vat_amount,
                    'overheads': grn.overhead_total,
                    'total_landed_cost': grn.total_landed_cost,
                    'net_total_amount': grn.net_total_amount,
                    'paid_amount': grn.paid_amount,
                    'due_amount': grn.due_amount,
                    'status': grn.status,
                    'gl_entry': grn_gl_map.get(grn.grn_number),
                    'items': items_detail
                })

            pret_doc_numbers = [pr.return_number for pr in purchase_returns_qs]
            pret_gl_map = cls._fetch_gl_journal_map(pret_doc_numbers, voucher_type='DEBIT_NOTE')

            for pret in purchase_returns_qs:
                items_detail = [
                    cls.extract_purchase_return_item_vat_detail(item)
                    for item in pret.items.all()
                ]

                returns_breakdown.append({
                    'id': pret.id,
                    'return_number': pret.return_number,
                    'original_bill_reference': pret.original_bill_reference or (pret.original_grn.grn_number if pret.original_grn else ''),
                    'original_grn_id': pret.original_grn_id,
                    'supplier_id': pret.supplier_id,
                    'supplier_name': pret.supplier.company_name,
                    'supplier_pan': pret.supplier.pan_number or '',
                    'return_date': pret.return_date,
                    'return_date_bs': pret.return_date_bs or '',
                    'fiscal_year': pret.fiscal_year or '',
                    'refund_mode': pret.get_refund_mode_display(),
                    'total_return_amount': pret.total_return_amount,
                    'tax_amount': pret.tax_amount,
                    'net_refund_amount': pret.net_refund_amount,
                    'debit_note_number': pret_gl_map.get(pret.return_number, {}).get('voucher_number') if pret_gl_map.get(pret.return_number) else None,
                    'gl_entry': pret_gl_map.get(pret.return_number),
                    'items': items_detail
                })

        return {
            'records': grn_qs,
            'purchase_returns': purchase_returns_qs,
            'document_breakdown': document_breakdown,
            'returns_breakdown': returns_breakdown,
            'totals': {
                'gross': net_purchase_gross,
                'discount': totals['total_discount'],
                'taxable': net_purchase_taxable,
                'vat': net_purchase_vat,
                'freight': totals['total_freight'],
                'customs': totals['total_customs'],
                'handling': totals['total_handling'],
                'overheads': total_overheads,
                'landed': net_purchase_landed,
                'net': net_purchase_net,
                'paid': totals['total_paid'],
                'due': totals['total_due'],
                'returns_taxable': pret_totals['total_return_taxable'],
                'returns_vat': pret_totals['total_return_vat'],
                'returns_total': pret_totals['total_return_refund'],
                'gross_vat_before_returns': totals['total_vat'],
                'gross_taxable_before_returns': totals['total_taxable'],
                'purchase_input_vat': totals['total_vat'],
                'purchase_return_vat_reversal': pret_totals['total_return_vat'],
                'net_input_vat': net_purchase_vat,
            }
        }

    # =========================================================================
    # 4. CHRONOLOGICAL DAY-WISE COMBINED VAT LEDGER
    # =========================================================================
    @classmethod
    def get_daily_vat_ledger(
        cls,
        branch: Optional[Branch],
        start_date: date,
        end_date: date,
        include_drilldown: bool = True
    ) -> Dict[str, Any]:
        """
        Generates the chronological Day-Wise Combined VAT & Tax Assessment Ledger.
        """
        config = SystemConfiguration.get_solo()
        is_vat_shop = (config.tax_system_mode == 'VAT')

        sales_filter: Dict[str, Any] = {
            'bill_date_ad__gte': start_date,
            'bill_date_ad__lte': end_date,
            'status__in': cls.VALID_SALES_STATUSES
        }
        if branch:
            sales_filter['branch'] = branch

        sales_qs = SalesEstimate.objects.filter(
            **sales_filter
        ).select_related(
            'customer', 'cashier', 'branch'
        ).prefetch_related(
            'items__product', 'items__unit_conversion'
        ).order_by('bill_date_ad', 'estimate_number')

        sales_returns_filter: Dict[str, Any] = {
            'return_date_ad__gte': start_date,
            'return_date_ad__lte': end_date
        }
        if branch:
            sales_returns_filter['branch'] = branch

        sales_returns_qs = SalesReturn.objects.filter(
            **sales_returns_filter
        ).select_related(
            'customer', 'original_estimate'
        ).prefetch_related(
            'items__product', 'items__estimate_item'
        ).order_by('return_date_ad', 'return_number')

        grn_filter: Dict[str, Any] = {
            'bill_date__gte': start_date,
            'bill_date__lte': end_date,
            'status': 'RECEIVED'
        }
        if branch:
            grn_filter['branch'] = branch

        grn_qs = GoodsReceivedNote.objects.filter(
            **grn_filter
        ).select_related(
            'supplier', 'branch'
        ).prefetch_related(
            'items__product'
        ).order_by('bill_date', 'grn_number')

        pret_filter: Dict[str, Any] = {
            'return_date__gte': start_date,
            'return_date__lte': end_date,
            'status': 'CONFIRMED'
        }
        if branch:
            pret_filter['branch'] = branch

        purchase_returns_qs = PurchaseReturn.objects.filter(
            **pret_filter
        ).select_related(
            'supplier', 'branch', 'original_grn'
        ).prefetch_related(
            'items__product'
        ).order_by('return_date', 'return_number')

        all_doc_numbers = []
        if include_drilldown:
            all_doc_numbers.extend([est.estimate_number for est in sales_qs])
            all_doc_numbers.extend([ret.return_number for ret in sales_returns_qs])
            all_doc_numbers.extend([grn.grn_number for grn in grn_qs])
            all_doc_numbers.extend([pr.return_number for pr in purchase_returns_qs])

        gl_map = cls._fetch_gl_journal_map(all_doc_numbers) if include_drilldown else {}

        daily_sales_bucket: Dict[date, Dict[str, Any]] = {}
        for est in sales_qs:
            d = est.bill_date_ad
            if d not in daily_sales_bucket:
                daily_sales_bucket[d] = {
                    'taxable': Decimal('0.00'), 'non_taxable': Decimal('0.00'),
                    'vat': Decimal('0.00'), 'grand_total': Decimal('0.00'), 'invoices': []
                }

            daily_sales_bucket[d]['taxable'] += est.taxable_amount
            daily_sales_bucket[d]['non_taxable'] += est.non_taxable_amount
            daily_sales_bucket[d]['vat'] += est.vat_amount
            daily_sales_bucket[d]['grand_total'] += est.grand_total

            if include_drilldown:
                items_detail = [
                    cls.extract_sales_item_vat_detail(item, is_vat_shop)
                    for item in est.items.all()
                ]

                daily_sales_bucket[d]['invoices'].append({
                    'id': est.id,
                    'estimate_number': est.estimate_number,
                    'bill_type': getattr(est, 'bill_type', 'SALES'),
                    'customer_name': est.recipient_display_name,
                    'customer_pan': est.customer_pan or (est.customer.pan_number if est.customer and est.customer.pan_number else ''),
                    'taxable_amount': est.taxable_amount if is_vat_shop else Decimal('0.00'),
                    'non_taxable_amount': est.non_taxable_amount if is_vat_shop else est.grand_total,
                    'vat_amount': est.vat_amount if is_vat_shop else Decimal('0.00'),
                    'grand_total': est.grand_total,
                    'status': est.status,
                    'status_display': est.get_status_display(),
                    'gl_entry': gl_map.get(est.estimate_number),
                    'items': items_detail
                })

        daily_returns_bucket: Dict[date, Dict[str, Any]] = {}
        for ret in sales_returns_qs:
            d = ret.return_date_ad
            if d not in daily_returns_bucket:
                daily_returns_bucket[d] = {
                    'taxable_deduct': Decimal('0.00'), 'vat_deduct': Decimal('0.00'),
                    'non_taxable_deduct': Decimal('0.00'), 'refund_total': Decimal('0.00'), 'returns': []
                }

            daily_returns_bucket[d]['refund_total'] += ret.total_refund_amount

            ret_items_detail = []
            ret_taxable_sub = Decimal('0.00')
            ret_vat_sub = Decimal('0.00')
            ret_non_taxable_sub = Decimal('0.00')

            for r_item in ret.items.all():
                item_detail = cls.extract_sales_return_item_vat_detail(r_item, is_vat_shop)
                ret_items_detail.append(item_detail)
                ret_taxable_sub += item_detail['taxable_amount']
                ret_vat_sub += item_detail['vat_amount']
                ret_non_taxable_sub += item_detail['non_taxable_amount']

            daily_returns_bucket[d]['taxable_deduct'] += ret_taxable_sub
            daily_returns_bucket[d]['vat_deduct'] += ret_vat_sub
            daily_returns_bucket[d]['non_taxable_deduct'] += ret_non_taxable_sub

            if include_drilldown:
                ret_gl_info = gl_map.get(ret.return_number)
                daily_returns_bucket[d]['returns'].append({
                    'id': ret.id,
                    'return_number': ret.return_number,
                    'original_estimate_number': ret.original_estimate.estimate_number if ret.original_estimate else 'N/A',
                    'original_estimate_id': ret.original_estimate_id,
                    'original_invoice_number': ret.original_estimate.estimate_number if ret.original_estimate else 'N/A',
                    'customer_name': ret.customer.name if ret.customer else (ret.original_estimate.recipient_display_name if ret.original_estimate else 'Walk-in'),
                    'refund_mode': ret.get_refund_mode_display(),
                    'total_refund_amount': ret.total_refund_amount,
                    'taxable_amount': ret_taxable_sub,
                    'vat_amount': ret_vat_sub,
                    'credit_note_number': ret_gl_info.get('voucher_number') if ret_gl_info else None,
                    'gl_entry': ret_gl_info,
                    'items': ret_items_detail
                })

        daily_grn_bucket: Dict[date, Dict[str, Any]] = {}
        for grn in grn_qs:
            d = grn.bill_date
            if d not in daily_grn_bucket:
                daily_grn_bucket[d] = {
                    'taxable': Decimal('0.00'), 'gross': Decimal('0.00'),
                    'vat': Decimal('0.00'), 'net_total': Decimal('0.00'), 'grns': []
                }

            daily_grn_bucket[d]['taxable'] += grn.taxable_amount
            daily_grn_bucket[d]['gross'] += grn.gross_amount
            daily_grn_bucket[d]['vat'] += grn.vat_amount
            daily_grn_bucket[d]['net_total'] += grn.net_total_amount

            if include_drilldown:
                items_detail = [
                    cls.extract_grn_item_vat_detail(item)
                    for item in grn.items.all()
                ]

                daily_grn_bucket[d]['grns'].append({
                    'id': grn.id,
                    'grn_number': grn.grn_number,
                    'supplier_bill_no': grn.supplier_bill_no,
                    'supplier_name': grn.supplier.company_name,
                    'supplier_pan': grn.supplier.pan_number or '',
                    'taxable_amount': grn.taxable_amount,
                    'vat_amount': grn.vat_amount,
                    'net_total_amount': grn.net_total_amount,
                    'gl_entry': gl_map.get(grn.grn_number),
                    'items': items_detail
                })

        daily_pret_bucket: Dict[date, Dict[str, Any]] = {}
        for pret in purchase_returns_qs:
            d = pret.return_date
            if d not in daily_pret_bucket:
                daily_pret_bucket[d] = {
                    'taxable_deduct': Decimal('0.00'), 'vat_deduct': Decimal('0.00'),
                    'refund_total': Decimal('0.00'), 'debit_notes': []
                }

            daily_pret_bucket[d]['taxable_deduct'] += pret.total_return_amount
            daily_pret_bucket[d]['vat_deduct'] += pret.tax_amount
            daily_pret_bucket[d]['refund_total'] += pret.net_refund_amount

            if include_drilldown:
                items_detail = [
                    cls.extract_purchase_return_item_vat_detail(item)
                    for item in pret.items.all()
                ]

                daily_pret_bucket[d]['debit_notes'].append({
                    'id': pret.id,
                    'return_number': pret.return_number,
                    'original_bill_reference': pret.original_bill_reference or '',
                    'supplier_name': pret.supplier.company_name,
                    'supplier_pan': pret.supplier.pan_number or '',
                    'total_return_amount': pret.total_return_amount,
                    'tax_amount': pret.tax_amount,
                    'net_refund_amount': pret.net_refund_amount,
                    'debit_note_number': gl_map.get(pret.return_number, {}).get('voucher_number') if gl_map.get(pret.return_number) else None,
                    'gl_entry': gl_map.get(pret.return_number),
                    'items': items_detail
                })

        days_count = (end_date - start_date).days + 1
        daily_rows = []
        cumulative_balance = Decimal('0.00')

        period_sales_taxable = Decimal('0.00')
        period_sales_non_taxable = Decimal('0.00')
        period_sales_vat = Decimal('0.00')
        period_sales_output_vat = Decimal('0.00')
        period_sales_return_vat = Decimal('0.00')
        period_sales_grand = Decimal('0.00')

        period_purchase_taxable = Decimal('0.00')
        period_purchase_vat = Decimal('0.00')
        period_purchase_input_vat = Decimal('0.00')
        period_purchase_return_vat = Decimal('0.00')
        period_purchase_net = Decimal('0.00')

        period_net_vat = Decimal('0.00')
        active_tax_days_count = 0

        current_day = start_date
        while current_day <= end_date:
            try:
                bs_y, bs_m, bs_d = NepaliCalendar.ad_to_bs(current_day)
                date_bs_str = f"{bs_y:04d}-{bs_m:02d}-{bs_d:02d}"
                bs_month_name = NepaliCalendar.NEPALI_MONTH_NAMES_EN[bs_m - 1]
            except Exception:
                date_bs_str = ""
                bs_month_name = ""

            s_data = daily_sales_bucket.get(current_day, {
                'taxable': Decimal('0.00'), 'non_taxable': Decimal('0.00'),
                'vat': Decimal('0.00'), 'grand_total': Decimal('0.00'), 'invoices': []
            })
            ret_data = daily_returns_bucket.get(current_day, {
                'taxable_deduct': Decimal('0.00'), 'vat_deduct': Decimal('0.00'),
                'non_taxable_deduct': Decimal('0.00'), 'refund_total': Decimal('0.00'), 'returns': []
            })

            day_sales_vat = s_data['vat']
            day_sales_return_vat = ret_data['vat_deduct']

            if is_vat_shop:
                net_day_sales_taxable = (s_data['taxable'] - ret_data['taxable_deduct']).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                net_day_sales_vat = (day_sales_vat - day_sales_return_vat).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                net_day_sales_non_taxable = (s_data['non_taxable'] - ret_data['non_taxable_deduct']).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                net_day_sales_grand = (s_data['grand_total'] - ret_data['refund_total']).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            else:
                net_day_sales_taxable = Decimal('0.00')
                net_day_sales_vat = Decimal('0.00')
                net_day_sales_grand = (s_data['grand_total'] - ret_data['refund_total']).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                net_day_sales_non_taxable = net_day_sales_grand

            p_data = daily_grn_bucket.get(current_day, {
                'taxable': Decimal('0.00'), 'gross': Decimal('0.00'),
                'vat': Decimal('0.00'), 'net_total': Decimal('0.00'), 'grns': []
            })
            pret_data = daily_pret_bucket.get(current_day, {
                'taxable_deduct': Decimal('0.00'), 'vat_deduct': Decimal('0.00'),
                'refund_total': Decimal('0.00'), 'debit_notes': []
            })

            day_purchase_vat = p_data['vat']
            day_purchase_return_vat = pret_data['vat_deduct']

            net_day_purchase_taxable = (p_data['taxable'] - pret_data['taxable_deduct']).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            net_day_purchase_vat = (day_purchase_vat - day_purchase_return_vat).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            net_day_purchase_net = (p_data['net_total'] - pret_data['refund_total']).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            daily_net_vat = (net_day_sales_vat - net_day_purchase_vat).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            cumulative_balance = (cumulative_balance + daily_net_vat).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            has_activity = (
                len(s_data['invoices']) > 0 or
                len(p_data['grns']) > 0 or
                len(ret_data['returns']) > 0 or
                len(pret_data['debit_notes']) > 0 or
                s_data['grand_total'] != Decimal('0.00') or
                ret_data['refund_total'] != Decimal('0.00') or
                p_data['net_total'] != Decimal('0.00') or
                pret_data['refund_total'] != Decimal('0.00')
            )
            if has_activity:
                active_tax_days_count += 1

            row_data = {
                'date_ad': current_day,
                'date_ad_str': current_day.strftime('%Y-%m-%d'),
                'date_bs': date_bs_str,
                'bs_month_name': bs_month_name,
                'day_name': current_day.strftime('%a'),
                'sales_taxable': net_day_sales_taxable,
                'sales_non_taxable': net_day_sales_non_taxable,
                'sales_vat': net_day_sales_vat,
                'sales_output_vat': day_sales_vat,
                'sales_return_vat': day_sales_return_vat,
                'sales_grand_total': net_day_sales_grand,
                'sales_count': len(s_data['invoices']),
                'sales_return_count': len(ret_data['returns']),
                'purchase_taxable': net_day_purchase_taxable,
                'purchase_vat': net_day_purchase_vat,
                'purchase_input_vat': day_purchase_vat,
                'purchase_return_vat': day_purchase_return_vat,
                'purchase_net_total': net_day_purchase_net,
                'purchase_count': len(p_data['grns']),
                'purchase_return_count': len(pret_data['debit_notes']),
                'daily_net_vat': daily_net_vat,
                'is_daily_payable': (daily_net_vat > Decimal('0.00')),
                'is_daily_credit': (daily_net_vat < Decimal('0.00')),
                'cumulative_balance': cumulative_balance,
                'is_cumulative_payable': (cumulative_balance > Decimal('0.00')),
                'is_cumulative_credit': (cumulative_balance < Decimal('0.00')),
                'has_activity': has_activity,
                'sales_invoices': s_data['invoices'] if include_drilldown else [],
                'sales_returns': ret_data['returns'] if include_drilldown else [],
                'purchase_grns': p_data['grns'] if include_drilldown else [],
                'purchase_returns': pret_data['debit_notes'] if include_drilldown else [],
            }
            daily_rows.append(row_data)

            period_sales_taxable += net_day_sales_taxable
            period_sales_non_taxable += net_day_sales_non_taxable
            period_sales_vat += net_day_sales_vat
            period_sales_output_vat += day_sales_vat
            period_sales_return_vat += day_sales_return_vat
            period_sales_grand += net_day_sales_grand

            period_purchase_taxable += net_day_purchase_taxable
            period_purchase_vat += net_day_purchase_vat
            period_purchase_input_vat += day_purchase_vat
            period_purchase_return_vat += day_purchase_return_vat
            period_purchase_net += net_day_purchase_net

            period_net_vat += daily_net_vat
            current_day += timedelta(days=1)

        return {
            'daily_rows': daily_rows,
            'is_vat_shop': is_vat_shop,
            'total_days_in_period': days_count,
            'active_tax_days_count': active_tax_days_count,
            'totals': {
                'sales_taxable': period_sales_taxable,
                'sales_non_taxable': period_sales_non_taxable,
                'sales_vat': period_sales_vat,
                'sales_output_vat': period_sales_output_vat,
                'sales_return_vat': period_sales_return_vat,
                'sales_grand_total': period_sales_grand,
                'purchase_taxable': period_purchase_taxable,
                'purchase_vat': period_purchase_vat,
                'purchase_input_vat': period_purchase_input_vat,
                'purchase_return_vat': period_purchase_return_vat,
                'purchase_net_total': period_purchase_net,
                'net_vat': period_net_vat,
                'final_cumulative_balance': cumulative_balance,
                'is_net_payable': (period_net_vat > Decimal('0.00')),
                'is_net_credit': (period_net_vat < Decimal('0.00')),
            }
        }

    # -------------------------------------------------------------------------
    # BS CALENDAR MONTH HELPERS FOR CONSECUTIVE 6-MONTH CALCULATION
    # -------------------------------------------------------------------------
    @staticmethod
    def _get_previous_bs_month(bs_year: int, bs_month: int) -> Tuple[int, int]:
        """Returns the immediately preceding (bs_year, bs_month) in Bikram Sambat."""
        if bs_month == 1:
            return bs_year - 1, 12
        return bs_year, bs_month - 1

    @staticmethod
    def _get_next_bs_month(bs_year: int, bs_month: int) -> Tuple[int, int]:
        """Returns the immediately succeeding (bs_year, bs_month) in Bikram Sambat."""
        if bs_month == 12:
            return bs_year + 1, 1
        return bs_year, bs_month + 1

    @classmethod
    def _resolve_six_month_tuples(
        cls,
        end_bs_year: Optional[int] = None,
        end_bs_month: Optional[int] = None,
        start_bs_year: Optional[int] = None,
        start_bs_month: Optional[int] = None,
        fiscal_year_name: Optional[str] = None,
        half: Optional[int] = None
    ) -> Tuple[List[Tuple[int, int]], str]:
        """
        Resolves 6 consecutive Bikram Sambat months in chronological order.
        Supports:
          1. Fiscal Year Half (H1: Shrawan-Poush vs H2: Magh-Ashadh)
          2. Specific End Month (steps back 5 months)
          3. Specific Start Month (steps forward 5 months)
          4. Default Fallback (active half of current fiscal year)
        """
        if fiscal_year_name and half in [1, 2]:
            clean_fy = fiscal_year_name.replace('-', '/').strip()
            base_year = int(clean_fy.split('/')[0])
            if half == 1:
                month_tuples = [(base_year, m) for m in range(4, 10)]
                label = f"FY {clean_fy} H1 (Shrawan - Poush {base_year} BS)"
            else:
                month_tuples = [
                    (base_year, 10), (base_year, 11), (base_year, 12),
                    (base_year + 1, 1), (base_year + 1, 2), (base_year + 1, 3)
                ]
                label = f"FY {clean_fy} H2 (Magh {base_year} - Ashadh {base_year + 1} BS)"
            return month_tuples, label

        if start_bs_year and start_bs_month:
            curr_y, curr_m = start_bs_year, start_bs_month
            month_tuples = [(curr_y, curr_m)]
            for _ in range(5):
                curr_y, curr_m = cls._get_next_bs_month(curr_y, curr_m)
                month_tuples.append((curr_y, curr_m))
            m1_name = NepaliCalendar.NEPALI_MONTH_NAMES_EN[month_tuples[0][1] - 1]
            m6_name = NepaliCalendar.NEPALI_MONTH_NAMES_EN[month_tuples[-1][1] - 1]
            label = f"{m1_name} {month_tuples[0][0]} - {m6_name} {month_tuples[-1][0]} BS (6 Months)"
            return month_tuples, label

        if end_bs_year and end_bs_month:
            curr_y, curr_m = end_bs_year, end_bs_month
            rev_tuples = [(curr_y, curr_m)]
            for _ in range(5):
                curr_y, curr_m = cls._get_previous_bs_month(curr_y, curr_m)
                rev_tuples.append((curr_y, curr_m))
            month_tuples = list(reversed(rev_tuples))
            m1_name = NepaliCalendar.NEPALI_MONTH_NAMES_EN[month_tuples[0][1] - 1]
            m6_name = NepaliCalendar.NEPALI_MONTH_NAMES_EN[month_tuples[-1][1] - 1]
            label = f"{m1_name} {month_tuples[0][0]} - {m6_name} {month_tuples[-1][0]} BS (6 Months)"
            return month_tuples, label

        today_ad = timezone.now().date()
        today_bs_y, today_bs_m, _ = NepaliCalendar.ad_to_bs(today_ad)
        current_fy = NepaliCalendar.get_fiscal_year(today_bs_y, today_bs_m)
        base_year = int(current_fy.split('/')[0])

        if 4 <= today_bs_m <= 9:
            month_tuples = [(base_year, m) for m in range(4, 10)]
            label = f"FY {current_fy} H1 (Shrawan - Poush {base_year} BS)"
        else:
            month_tuples = [
                (base_year, 10), (base_year, 11), (base_year, 12),
                (base_year + 1, 1), (base_year + 1, 2), (base_year + 1, 3)
            ]
            label = f"FY {current_fy} H2 (Magh {base_year} - Ashadh {base_year + 1} BS)"

        return month_tuples, label

    # =========================================================================
    # 5. MONTHLY, 6-MONTH & FISCAL YEAR VAT SUMMARIES
    # =========================================================================
    @classmethod
    def get_monthly_vat_summary(
        cls,
        branch: Optional[Branch],
        bs_year: int,
        bs_month: int,
        include_drilldown: bool = False
    ) -> Dict[str, Any]:
        """
        Computes the authoritative monthly VAT summary for a single Bikram Sambat month.
        Obtains Gregorian AD date boundaries for (bs_year, bs_month) using NepaliCalendar,
        aggregates sales and purchase VAT using existing methods, and calculates:
          - gross Output VAT (sales)
          - sales-return VAT reversal
          - net Output VAT
          - gross Input VAT (purchases)
          - purchase-return VAT reversal
          - net Input VAT
          - net VAT assessment (net Output - net Input)
        Never forces negative tax adjustments to zero.
        """
        bs_month = max(1, min(12, int(bs_month)))
        bs_year = int(bs_year)

        start_ad, end_ad, start_bs, end_bs = NepaliCalendar.get_bs_month_range(bs_year, bs_month)
        fy = NepaliCalendar.get_fiscal_year(bs_year, bs_month)
        m_name_en = NepaliCalendar.NEPALI_MONTH_NAMES_EN[bs_month - 1]
        m_name_np = NepaliCalendar.NEPALI_MONTH_NAMES_NP[bs_month - 1]

        sales = cls.get_sales_vat_summary(branch, start_ad, end_ad, include_drilldown=include_drilldown)
        purchases = cls.get_purchase_vat_summary(branch, start_ad, end_ad, include_drilldown=include_drilldown)

        gross_output_vat = sales['totals'].get('gross_vat_before_returns', Decimal('0.00'))
        sales_return_vat = sales['totals'].get('return_vat_deduct', Decimal('0.00'))
        net_output_vat = sales['totals']['vat']

        gross_input_vat = purchases['totals'].get('gross_vat_before_returns', Decimal('0.00'))
        purchase_return_vat = purchases['totals'].get('returns_vat', Decimal('0.00'))
        net_input_vat = purchases['totals']['vat']

        net_vat = (net_output_vat - net_input_vat).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        return {
            'period_type': 'MONTH',
            'bs_year': bs_year,
            'bs_month': bs_month,
            'month_name_en': m_name_en,
            'month_name_np': m_name_np,
            'month_label': f"{m_name_en} {bs_year}",
            'month_label_np': f"{m_name_np} {bs_year}",
            'fiscal_year': fy,
            'start_date_ad': start_ad,
            'end_date_ad': end_ad,
            'start_date_bs': start_bs,
            'end_date_bs': end_bs,
            'sales_totals': sales['totals'],
            'purchase_totals': purchases['totals'],
            'gross_output_vat': gross_output_vat,
            'sales_return_vat_reversal': sales_return_vat,
            'sales_return_vat': sales_return_vat,
            'output_vat': gross_output_vat,
            'net_output_vat': net_output_vat,
            'net_output': net_output_vat,
            'gross_input_vat': gross_input_vat,
            'purchase_return_vat_reversal': purchase_return_vat,
            'purchase_return_vat': purchase_return_vat,
            'input_vat': gross_input_vat,
            'net_input_vat': net_input_vat,
            'net_input': net_input_vat,
            'net_vat': net_vat,
            'is_net_payable': (net_vat > Decimal('0.00')),
            'is_net_credit': (net_vat < Decimal('0.00')),
            'document_breakdown': sales.get('document_breakdown', []),
            'sales_document_breakdown': sales.get('document_breakdown', []),
            'returns_breakdown': sales.get('returns_breakdown', []),
            'sales_returns_breakdown': sales.get('returns_breakdown', []),
            'purchase_document_breakdown': purchases.get('document_breakdown', []),
            'purchase_returns_breakdown': purchases.get('returns_breakdown', []),
        }

    # -------------------------------------------------------------------------
    # 6-MONTH VAT REPORTING ENGINE (REQUIREMENT 16)
    # -------------------------------------------------------------------------
    @classmethod
    def get_six_month_vat_summary(
        cls,
        branch: Optional[Branch],
        end_bs_year: Optional[int] = None,
        end_bs_month: Optional[int] = None,
        start_bs_year: Optional[int] = None,
        start_bs_month: Optional[int] = None,
        fiscal_year_name: Optional[str] = None,
        half: Optional[int] = None,
        include_comparison: bool = True
    ) -> Dict[str, Any]:
        """
        Authoritative 6-Month VAT Reporting Engine.
        Computes 6 consecutive Bikram Sambat months breakdown:
          Month | Output VAT | Sales Return VAT | Net Output | Input VAT | Purchase Return VAT | Net Input | Net VAT
        
        Guarantees Full Drilldown Chain:
          6 Months ➔ Month ➔ Day ➔ Invoice / Return / GRN / Debit Note ➔ Line Item ➔ GL Journal
        """
        month_tuples, period_label = cls._resolve_six_month_tuples(
            end_bs_year=end_bs_year,
            end_bs_month=end_bs_month,
            start_bs_year=start_bs_year,
            start_bs_month=start_bs_month,
            fiscal_year_name=fiscal_year_name,
            half=half
        )

        monthly_breakdown: List[Dict[str, Any]] = []
        cumulative_balance = Decimal('0.00')

        for bs_y, bs_m in month_tuples:
            m_summary = cls.get_monthly_vat_summary(branch, bs_y, bs_m)
            cumulative_balance += m_summary['net_vat']

            gross_out = m_summary.get('gross_output_vat', Decimal('0.00'))
            sales_ret = m_summary.get('sales_return_vat_reversal', Decimal('0.00'))
            net_out = m_summary.get('net_output_vat', gross_out - sales_ret)

            gross_in = m_summary.get('gross_input_vat', Decimal('0.00'))
            purch_ret = m_summary.get('purchase_return_vat_reversal', Decimal('0.00'))
            net_in = m_summary.get('net_input_vat', gross_in - purch_ret)

            net_tax = m_summary.get('net_vat', net_out - net_in)

            m_summary.update({
                'month_label': f"{m_summary['month_name_en']} {bs_y}",
                'month_label_np': f"{m_summary['month_name_np']} {bs_y}",
                'output_vat': gross_out,
                'gross_output_vat': gross_out,
                'sales_return_vat': sales_ret,
                'sales_return_vat_reversal': sales_ret,
                'net_output': net_out,
                'net_output_vat': net_out,
                'input_vat': gross_in,
                'gross_input_vat': gross_in,
                'purchase_return_vat': purch_ret,
                'purchase_return_vat_reversal': purch_ret,
                'net_input': net_in,
                'net_input_vat': net_in,
                'net_vat': net_tax,
                'cumulative_balance': cumulative_balance,
                'drilldown_urls': {
                    'daily_report': f"/taxation/daily-report/?bs_year={bs_y}&bs_month={bs_m}",
                    'sales_register': f"/taxation/sales-register/?bs_year={bs_y}&bs_month={bs_m}",
                    'purchase_register': f"/taxation/purchase-register/?bs_year={bs_y}&bs_month={bs_m}",
                },
                'drilldown_params': {
                    'bs_year': bs_y,
                    'bs_month': bs_m,
                    'start_date_bs': m_summary['start_date_bs'],
                    'end_date_bs': m_summary['end_date_bs'],
                    'start_date': m_summary['start_date_ad'].isoformat(),
                    'end_date': m_summary['end_date_ad'].isoformat(),
                    'fiscal_year': m_summary['fiscal_year'],
                }
            })
            monthly_breakdown.append(m_summary)

        start_date_ad = monthly_breakdown[0]['start_date_ad']
        end_date_ad = monthly_breakdown[-1]['end_date_ad']
        start_date_bs = monthly_breakdown[0]['start_date_bs']
        end_date_bs = monthly_breakdown[-1]['end_date_bs']

        sales = cls.get_sales_vat_summary(branch, start_date_ad, end_date_ad, include_drilldown=False)
        purchases = cls.get_purchase_vat_summary(branch, start_date_ad, end_date_ad, include_drilldown=False)

        total_gross_out = sales['totals'].get('gross_vat_before_returns', Decimal('0.00'))
        total_sales_ret = sales['totals'].get('return_vat_deduct', Decimal('0.00'))
        total_net_out = sales['totals']['vat']

        total_gross_in = purchases['totals'].get('gross_vat_before_returns', Decimal('0.00'))
        total_purch_ret = purchases['totals'].get('returns_vat', Decimal('0.00'))
        total_net_in = purchases['totals']['vat']

        total_net_tax = (total_net_out - total_net_in).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        summary_result = {
            'period_type': 'SIX_MONTH',
            'period_label': period_label,
            'start_date_ad': start_date_ad,
            'end_date_ad': end_date_ad,
            'start_date_bs': start_date_bs,
            'end_date_bs': end_date_bs,
            'month_tuples': month_tuples,
            'monthly_breakdown': monthly_breakdown,
            'sales_totals': sales['totals'],
            'purchase_totals': purchases['totals'],
            'output_vat': total_gross_out,
            'gross_output_vat': total_gross_out,
            'sales_return_vat': total_sales_ret,
            'sales_return_vat_reversal': total_sales_ret,
            'net_output': total_net_out,
            'net_output_vat': total_net_out,
            'input_vat': total_gross_in,
            'gross_input_vat': total_gross_in,
            'purchase_return_vat': total_purch_ret,
            'purchase_return_vat_reversal': total_purch_ret,
            'net_input': total_net_in,
            'net_input_vat': total_net_in,
            'net_vat': total_net_tax,
            'is_net_payable': (total_net_tax > Decimal('0.00')),
            'is_net_credit': (total_net_tax < Decimal('0.00')),
            'final_cumulative_balance': cumulative_balance,
        }

        if include_comparison:
            prev_end_y, prev_end_m = cls._get_previous_bs_month(month_tuples[0][0], month_tuples[0][1])
            summary_result['comparison'] = cls.get_six_month_vat_comparison(
                branch=branch,
                end_bs_year=prev_end_y,
                end_bs_month=prev_end_m,
                include_comparison=False
            )

        return summary_result

    # -------------------------------------------------------------------------
    # 6-MONTH PERIOD-OVER-PERIOD COMPARISON ENGINE (REQUIREMENT 17)
    # -------------------------------------------------------------------------
    @classmethod
    def get_six_month_vat_comparison(
        cls,
        branch: Optional[Branch],
        end_bs_year: Optional[int] = None,
        end_bs_month: Optional[int] = None,
        start_bs_year: Optional[int] = None,
        start_bs_month: Optional[int] = None,
        fiscal_year_name: Optional[str] = None,
        half: Optional[int] = None,
        include_comparison: bool = False
    ) -> Dict[str, Any]:
        """
        Authoritative 6-Month Period-Over-Period VAT Comparison Engine.
        Compares:
          Current 6 Months versus Previous 6 Months
        for:
          1. Output VAT
          2. Sales Return VAT
          3. Net Output VAT
          4. Input VAT
          5. Purchase Return VAT
          6. Net Input VAT
          7. Net VAT Payable / Credit
        """
        current_summary = cls.get_six_month_vat_summary(
            branch=branch,
            end_bs_year=end_bs_year,
            end_bs_month=end_bs_month,
            start_bs_year=start_bs_year,
            start_bs_month=start_bs_month,
            fiscal_year_name=fiscal_year_name,
            half=half,
            include_comparison=False
        )
        current_tuples = current_summary['month_tuples']

        prev_end_y, prev_end_m = cls._get_previous_bs_month(current_tuples[0][0], current_tuples[0][1])
        previous_summary = cls.get_six_month_vat_summary(
            branch=branch,
            end_bs_year=prev_end_y,
            end_bs_month=prev_end_m,
            include_comparison=False
        )

        def _calc_variance_metric(
            metric_key: str,
            label_en: str,
            label_np: str,
            curr_val: Decimal,
            prev_val: Decimal
        ) -> Dict[str, Any]:
            diff = (curr_val - prev_val).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            
            if prev_val != Decimal('0.00'):
                pct = ((diff / abs(prev_val)) * Decimal('100.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                pct_display = f"{'+' if pct > 0 else ''}{pct:.2f}%"
            else:
                if curr_val == Decimal('0.00'):
                    pct = Decimal('0.00')
                    pct_display = "0.00%"
                else:
                    pct = None
                    pct_display = "+100.00%" if curr_val > Decimal('0.00') else "-100.00%"

            if diff > Decimal('0.00'):
                trend = 'INCREASE'
            elif diff < Decimal('0.00'):
                trend = 'DECREASE'
            else:
                trend = 'UNCHANGED'

            return {
                'metric': metric_key,
                'label': label_en,
                'label_np': label_np,
                'current_value': curr_val,
                'previous_value': prev_val,
                'current_display': f"Rs. {curr_val:,.2f}",
                'previous_display': f"Rs. {prev_val:,.2f}",
                'difference': diff,
                'difference_display': f"{'+' if diff > 0 else ''}Rs. {diff:,.2f}",
                'percentage_difference': pct,
                'percentage_difference_display': pct_display,
                'trend': trend,
            }

        m1 = _calc_variance_metric('output_vat', 'Output VAT', 'बिक्री भ्याट', current_summary['gross_output_vat'], previous_summary['gross_output_vat'])
        m2 = _calc_variance_metric('sales_return_vat', 'Sales Return VAT', 'बिक्री फिर्ता भ्याट', current_summary['sales_return_vat'], previous_summary['sales_return_vat'])
        m3 = _calc_variance_metric('net_output_vat', 'Net Output VAT', 'खुद बिक्री भ्याट', current_summary['net_output_vat'], previous_summary['net_output_vat'])
        m4 = _calc_variance_metric('input_vat', 'Input VAT', 'खरिद भ्याट', current_summary['gross_input_vat'], previous_summary['gross_input_vat'])
        m5 = _calc_variance_metric('purchase_return_vat', 'Purchase Return VAT', 'खरिद फिर्ता भ्याट', current_summary['purchase_return_vat'], previous_summary['purchase_return_vat'])
        m6 = _calc_variance_metric('net_input_vat', 'Net Input VAT', 'खुद खरिद भ्याट', current_summary['net_input_vat'], previous_summary['net_input_vat'])
        m7 = _calc_variance_metric('net_vat', 'Net VAT Payable / Credit', 'खुद भ्याट दायित्व / कट्टी', current_summary['net_vat'], previous_summary['net_vat'])

        m7['is_current_payable'] = current_summary['is_net_payable']
        m7['is_previous_payable'] = previous_summary['is_net_payable']

        comparison_rows = [m1, m2, m3, m4, m5, m6, m7]
        metrics_dict = {
            'output_vat': m1,
            'sales_return_vat': m2,
            'net_output_vat': m3,
            'input_vat': m4,
            'purchase_return_vat': m5,
            'net_input_vat': m6,
            'net_vat': m7,
        }

        return {
            'current_period_label': current_summary['period_label'],
            'previous_period_label': previous_summary['period_label'],
            'current_start_bs': current_summary['start_date_bs'],
            'current_end_bs': current_summary['end_date_bs'],
            'previous_start_bs': previous_summary['start_date_bs'],
            'previous_end_bs': previous_summary['end_date_bs'],
            'current_start_ad': current_summary['start_date_ad'],
            'current_end_ad': current_summary['end_date_ad'],
            'previous_start_ad': previous_summary['start_date_ad'],
            'previous_end_ad': previous_summary['end_date_ad'],
            'metrics': metrics_dict,
            'comparison_rows': comparison_rows,
            'current_summary': current_summary,
            'previous_summary': previous_summary,
        }

    # -------------------------------------------------------------------------
    # FISCAL YEAR SUMMARY (ANNUAL RECONCILIATION)
    # -------------------------------------------------------------------------
    @classmethod
    def get_fiscal_year_vat_summary(
        cls,
        branch: Optional[Branch],
        fiscal_year_name: str
    ) -> Dict[str, Any]:
        """
        Calculates the VAT summary for an entire Nepali Fiscal Year (Shrawan 1 - Ashadh 31/32).
        Includes a month-by-month reconciliation array.
        """
        clean_fy = fiscal_year_name.replace('-', '/').strip()
        start_ad, end_ad, start_bs, end_bs = NepaliCalendar.get_fiscal_year_range(clean_fy)

        sales = cls.get_sales_vat_summary(branch, start_ad, end_ad, include_drilldown=False)
        purchases = cls.get_purchase_vat_summary(branch, start_ad, end_ad, include_drilldown=False)

        output_vat = sales['totals']['vat']
        input_vat = purchases['totals']['vat']
        net_vat = (output_vat - input_vat).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        base_year = int(clean_fy.split('/')[0])
        monthly_breakdown = []
        cumulative_balance = Decimal('0.00')

        for month_idx in range(1, 13):
            if month_idx <= 9:
                bs_y = base_year
                bs_m = month_idx + 3  # 1->Shrawan (4) ... 9->Chaitra (12)
            else:
                bs_y = base_year + 1
                bs_m = month_idx - 9  # 10->Baishakh (1) ... 12->Ashadh (3)

            m_summary = cls.get_monthly_vat_summary(branch, bs_y, bs_m)
            cumulative_balance += m_summary['net_vat']
            m_summary['cumulative_balance'] = cumulative_balance
            monthly_breakdown.append(m_summary)

        return {
            'period_type': 'FISCAL_YEAR',
            'fiscal_year': clean_fy,
            'start_date_ad': start_ad,
            'end_date_ad': end_ad,
            'start_date_bs': start_bs,
            'end_date_bs': end_bs,
            'sales_totals': sales['totals'],
            'purchase_totals': purchases['totals'],
            'gross_output_vat': sales['totals'].get('gross_vat_before_returns', Decimal('0.00')),
            'sales_return_vat_reversal': sales['totals'].get('return_vat_deduct', Decimal('0.00')),
            'output_vat': output_vat,
            'net_output_vat': output_vat,
            'gross_input_vat': purchases['totals'].get('gross_vat_before_returns', Decimal('0.00')),
            'purchase_return_vat_reversal': purchases['totals'].get('returns_vat', Decimal('0.00')),
            'input_vat': input_vat,
            'net_input_vat': input_vat,
            'net_vat': net_vat,
            'is_net_payable': (net_vat > Decimal('0.00')),
            'is_net_credit': (net_vat < Decimal('0.00')),
            'monthly_breakdown': monthly_breakdown,
        }

    # =========================================================================
    # 6. HIERARCHICAL DRILL-DOWN RESOLVERS
    # =========================================================================
    @classmethod
    def get_day_vat_drilldown(cls, branch: Optional[Branch], target_date: date) -> Dict[str, Any]:
        """
        Single-day drill-down: retrieves all sales invoices, GRNs, and returns for target_date.
        """
        ledger = cls.get_daily_vat_ledger(branch, target_date, target_date, include_drilldown=True)
        rows = ledger.get('daily_rows', [])
        return rows[0] if rows else {}

    @classmethod
    def get_invoice_vat_drilldown(cls, invoice_id_or_number: Union[int, str]) -> Dict[str, Any]:
        """
        Single-document drill-down:
        Sales Invoice ➔ Item-by-item VAT Snapshots ➔ Sales Returns ➔ General Ledger Entry (Account 2210).
        """
        config = SystemConfiguration.get_solo()
        is_vat_shop = (config.tax_system_mode == 'VAT')

        if isinstance(invoice_id_or_number, int) or str(invoice_id_or_number).isdigit():
            est = SalesEstimate.objects.filter(id=int(invoice_id_or_number)).first()
        else:
            est = SalesEstimate.objects.filter(estimate_number__iexact=str(invoice_id_or_number).strip()).first()

        if not est:
            return {'status': 'NOT_FOUND', 'message': f'Invoice {invoice_id_or_number} not found.'}

        gl_entry = cls._fetch_gl_journal_map([est.estimate_number], voucher_type='SALES').get(est.estimate_number)

        items_breakdown = [
            cls.extract_sales_item_vat_detail(item, is_vat_shop)
            for item in est.items.select_related('product', 'unit_conversion').all()
        ]

        returns_breakdown = []
        for ret in est.returns.select_related('processed_by').prefetch_related('items__product').all():
            for r_item in ret.items.all():
                returns_breakdown.append(cls.extract_sales_return_item_vat_detail(r_item, is_vat_shop))

        return {
            'status': 'SUCCESS',
            'document_type': 'SALES_INVOICE',
            'id': est.id,
            'estimate_number': est.estimate_number,
            'bill_type': getattr(est, 'bill_type', 'SALES'),
            'bill_type_display': est.get_bill_type_display() if hasattr(est, 'get_bill_type_display') else 'Sales Invoice',
            'bill_date_ad': est.bill_date_ad,
            'bill_date_bs': est.bill_date_bs or '',
            'fiscal_year': est.fiscal_year or '',
            'customer_name': est.recipient_display_name,
            'customer_pan': est.customer_pan,
            'subtotal': est.subtotal,
            'discount_amount': est.total_sales_discount,
            'taxable_amount': est.taxable_amount if is_vat_shop else Decimal('0.00'),
            'non_taxable_amount': est.non_taxable_amount if is_vat_shop else est.grand_total,
            'vat_amount': est.vat_amount if is_vat_shop else Decimal('0.00'),
            'grand_total': est.grand_total,
            'invoice_status': est.status,
            'invoice_status_display': est.get_status_display(),
            'items': items_breakdown,
            'returns': returns_breakdown,
            'gl_entry': gl_entry,
        }

    @classmethod
    def get_grn_vat_drilldown(cls, grn_id_or_number: Union[int, str]) -> Dict[str, Any]:
        """
        Single-document drill-down:
        Inward GRN ➔ Item-by-item VAT Snapshots ➔ General Ledger Entry (Account 1410).
        """
        if isinstance(grn_id_or_number, int) or str(grn_id_or_number).isdigit():
            grn = GoodsReceivedNote.objects.filter(id=int(grn_id_or_number)).first()
        else:
            grn = GoodsReceivedNote.objects.filter(grn_number__iexact=str(grn_id_or_number).strip()).first()

        if not grn:
            return {'status': 'NOT_FOUND', 'message': f'GRN {grn_id_or_number} not found.'}

        gl_entry = cls._fetch_gl_journal_map([grn.grn_number], voucher_type='PURCHASE').get(grn.grn_number)

        items_breakdown = [
            cls.extract_grn_item_vat_detail(item)
            for item in grn.items.select_related('product', 'unit_conversion').all()
        ]

        returns_breakdown = []
        for pret in grn.purchase_returns.select_related('processed_by').prefetch_related('items__product').all():
            for r_item in pret.items.all():
                returns_breakdown.append(cls.extract_purchase_return_item_vat_detail(r_item))

        return {
            'status': 'SUCCESS',
            'document_type': 'GOODS_RECEIVED_NOTE',
            'id': grn.id,
            'grn_number': grn.grn_number,
            'supplier_bill_no': grn.supplier_bill_no,
            'challan_no': grn.challan_no or '',
            'bill_date': grn.bill_date,
            'bill_date_bs': grn.bill_date_bs or '',
            'fiscal_year': grn.fiscal_year or '',
            'supplier_name': grn.supplier.company_name,
            'supplier_pan': grn.supplier.pan_number or '',
            'gross_amount': grn.gross_amount,
            'discount_amount': grn.discount_amount,
            'taxable_amount': grn.taxable_amount,
            'vat_amount': grn.vat_amount,
            'total_landed_cost': grn.total_landed_cost,
            'net_total_amount': grn.net_total_amount,
            'status': grn.status,
            'status_display': grn.get_status_display(),
            'items': items_breakdown,
            'returns': returns_breakdown,
            'gl_entry': gl_entry,
        }

    @classmethod
    def get_sales_return_vat_drilldown(cls, return_id_or_number: Union[int, str]) -> Dict[str, Any]:
        """
        Complete Single-Document Drill-Down Resolver for Customer Sales Returns / Credit Notes.
        Calculates item-level VAT snapshots and ensures return totals accurately reflect the lines.
        """
        config = SystemConfiguration.get_solo()
        is_vat_shop = (config.tax_system_mode == 'VAT')

        clean_ref = str(return_id_or_number or '').strip()
        if not clean_ref:
            return {'status': 'NOT_FOUND', 'message': 'No sales return identifier provided.'}

        ret_qs = SalesReturn.objects.select_related(
            'customer', 'original_estimate', 'branch', 'processed_by'
        ).prefetch_related(
            'items__product', 'items__estimate_item'
        )

        ret = None
        if isinstance(return_id_or_number, int) or clean_ref.isdigit():
            ret = ret_qs.filter(id=int(clean_ref)).first()

        if not ret:
            ret = ret_qs.filter(return_number__iexact=clean_ref).first()

        if not ret:
            return {'status': 'NOT_FOUND', 'message': f'Sales Return voucher {return_id_or_number} not found.'}

        je_obj = None

        je_obj = JournalEntry.objects.filter(
            source_module='SALES_RETURN',
            source_id=str(ret.id),
            status='POSTED'
        ).prefetch_related('items__account', 'items__customer', 'items__supplier').first()

        if not je_obj:
            je_obj = JournalEntry.objects.filter(
                reference_document__iexact=ret.return_number,
                voucher_type='CREDIT_NOTE',
                status='POSTED'
            ).prefetch_related('items__account', 'items__customer', 'items__supplier').first()

        if not je_obj:
            je_obj = JournalEntry.objects.filter(
                reference_document__iexact=ret.return_number,
                status='POSTED'
            ).prefetch_related('items__account', 'items__customer', 'items__supplier').first()

        if not je_obj:
            je_obj = JournalEntry.objects.filter(
                voucher_number__iexact=ret.return_number,
                status='POSTED'
            ).prefetch_related('items__account', 'items__customer', 'items__supplier').first()

        if not je_obj:
            je_obj = JournalEntry.objects.filter(
                narration__icontains=ret.return_number,
                voucher_type='CREDIT_NOTE',
                status='POSTED'
            ).prefetch_related('items__account', 'items__customer', 'items__supplier').first()

        journal_items_list = []
        gl_account_2210_detail = None
        gl_entry_summary = None
        credit_note_summary = None

        if je_obj:
            gl_entry_summary = {
                'id': je_obj.id,
                'voucher_number': je_obj.voucher_number,
                'voucher_type': je_obj.voucher_type,
                'voucher_type_display': je_obj.get_voucher_type_display(),
                'entry_date': je_obj.entry_date,
                'entry_date_bs': je_obj.entry_date_bs or '',
                'fiscal_year': je_obj.fiscal_year or '',
                'total_debit': je_obj.total_debit,
                'total_credit': je_obj.total_credit,
                'reference_document': je_obj.reference_document or ret.return_number,
                'narration': je_obj.narration or '',
                'status': je_obj.status,
                'status_display': je_obj.get_status_display(),
                'source_module': getattr(je_obj, 'source_module', 'SALES_RETURN'),
                'source_id': getattr(je_obj, 'source_id', str(ret.id)),
            }

            credit_note_summary = {
                'id': je_obj.id,
                'voucher_number': je_obj.voucher_number,
                'credit_note_number': je_obj.voucher_number,
                'voucher_type': je_obj.voucher_type,
                'voucher_type_display': je_obj.get_voucher_type_display(),
                'entry_date': je_obj.entry_date,
                'entry_date_bs': je_obj.entry_date_bs or '',
                'fiscal_year': je_obj.fiscal_year or '',
                'total_amount': je_obj.total_debit,
                'reference_document': je_obj.reference_document or ret.return_number,
                'narration': je_obj.narration or '',
                'status': je_obj.status,
                'status_display': je_obj.get_status_display(),
            }

            for itm in je_obj.items.all():
                is_2210 = bool(
                    itm.account.code == '2210' or
                    itm.account.code.startswith('2210-') or
                    itm.account.system_tag == 'OUTPUT_VAT'
                )
                item_dict = {
                    'id': itm.id,
                    'account_id': itm.account_id,
                    'account_code': itm.account.code,
                    'account_name': itm.account.name,
                    'debit_amount': itm.debit_amount,
                    'credit_amount': itm.credit_amount,
                    'line_narration': itm.line_narration or '',
                    'customer_name': itm.customer.name if itm.customer else '',
                    'supplier_name': itm.supplier.company_name if itm.supplier else '',
                    'is_output_vat_2210': is_2210,
                    'is_output_vat': is_2210,
                }
                journal_items_list.append(item_dict)

                if is_2210 and not gl_account_2210_detail:
                    gl_account_2210_detail = {
                        'account_code': itm.account.code,
                        'account_name': itm.account.name,
                        'debit_amount': itm.debit_amount,
                        'credit_amount': itm.credit_amount,
                        'vat_reversal_amount': itm.debit_amount,
                        'line_narration': itm.line_narration or '',
                        'is_reversal': True,
                    }

        items_breakdown = [
            cls.extract_sales_return_item_vat_detail(item, is_vat_shop)
            for item in ret.items.all()
        ]

        taxable_sum = sum((i['taxable_amount'] for i in items_breakdown), Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        vat_sum = sum((i['vat_amount'] for i in items_breakdown), Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        non_taxable_sum = sum((i['non_taxable_amount'] for i in items_breakdown), Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        final_taxable = taxable_sum if (taxable_sum > Decimal('0.00') or (ret.taxable_amount or Decimal('0.00')) == Decimal('0.00')) else (ret.taxable_amount or Decimal('0.00'))
        final_vat = vat_sum if (vat_sum > Decimal('0.00') or (ret.vat_amount or Decimal('0.00')) == Decimal('0.00')) else (ret.vat_amount or Decimal('0.00'))
        final_non_taxable = non_taxable_sum if (non_taxable_sum > Decimal('0.00') or (ret.non_taxable_amount or Decimal('0.00')) == Decimal('0.00')) else (ret.non_taxable_amount or Decimal('0.00'))

        orig_est = ret.original_estimate
        original_invoice_detail = None
        if orig_est:
            original_invoice_detail = {
                'id': orig_est.id,
                'estimate_number': orig_est.estimate_number,
                'invoice_number': orig_est.estimate_number,
                'bill_type': getattr(orig_est, 'bill_type', 'SALES'),
                'bill_type_display': orig_est.get_bill_type_display() if hasattr(orig_est, 'get_bill_type_display') else 'Sales Invoice',
                'bill_date_ad': orig_est.bill_date_ad,
                'bill_date_bs': orig_est.bill_date_bs or '',
                'fiscal_year': orig_est.fiscal_year or '',
                'customer_name': orig_est.recipient_display_name,
                'customer_phone': orig_est.customer_phone_manual or (orig_est.customer.phone_number if orig_est.customer else ''),
                'customer_pan': orig_est.customer_pan or (orig_est.customer.pan_number if orig_est.customer and orig_est.customer.pan_number else ''),
                'subtotal': orig_est.subtotal,
                'discount_amount': orig_est.total_sales_discount,
                'taxable_amount': orig_est.taxable_amount if is_vat_shop else Decimal('0.00'),
                'non_taxable_amount': orig_est.non_taxable_amount if is_vat_shop else orig_est.grand_total,
                'vat_amount': orig_est.vat_amount if is_vat_shop else Decimal('0.00'),
                'grand_total': orig_est.grand_total,
                'paid_amount': orig_est.paid_amount,
                'due_amount': orig_est.due_amount,
                'status': orig_est.status,
                'status_display': orig_est.get_status_display(),
            }

        customer_name = ret.customer.name if ret.customer else (orig_est.recipient_display_name if orig_est else 'Walk-in')
        customer_phone = ret.customer.phone_number if ret.customer and ret.customer.phone_number else (orig_est.customer_phone_manual if orig_est else '')
        customer_pan = ret.customer.pan_number if ret.customer and ret.customer.pan_number else (orig_est.customer_pan if orig_est else '')

        return {
            'status': 'SUCCESS',
            'document_type': 'SALES_RETURN',
            'id': ret.id,
            'sales_return_id': ret.id,
            'return_number': ret.return_number,
            'sales_return_number': ret.return_number,
            'return_date_ad': ret.return_date_ad,
            'return_date_bs': ret.return_date_bs or '',
            'fiscal_year': ret.fiscal_year or '',
            'customer_id': ret.customer_id,
            'customer_name': customer_name,
            'customer_phone': customer_phone,
            'customer_pan': customer_pan,
            'refund_mode': ret.refund_mode,
            'refund_mode_display': ret.get_refund_mode_display(),
            'reason': ret.reason or '',
            'technician_notes': ret.technician_notes or '',
            'total_refund_amount': ret.total_refund_amount,
            'taxable_amount': final_taxable,
            'taxable_reversal_amount': final_taxable,
            'vat_amount': final_vat,
            'vat_reversal_amount': final_vat,
            'non_taxable_amount': final_non_taxable,
            'items': items_breakdown,
            'original_estimate_id': ret.original_estimate_id,
            'original_estimate_number': orig_est.estimate_number if orig_est else 'N/A',
            'original_invoice_id': ret.original_estimate_id,
            'original_invoice_number': orig_est.estimate_number if orig_est else 'N/A',
            'original_invoice_reference': orig_est.estimate_number if orig_est else 'N/A',
            'original_invoice_bill_type': getattr(orig_est, 'bill_type', 'SALES') if orig_est else '',
            'original_invoice_bill_type_display': orig_est.get_bill_type_display() if (orig_est and hasattr(orig_est, 'get_bill_type_display')) else 'Sales Invoice',
            'original_invoice_date_ad': orig_est.bill_date_ad if orig_est else None,
            'original_invoice_date_bs': orig_est.bill_date_bs if orig_est else '',
            'original_invoice_fiscal_year': orig_est.fiscal_year if orig_est else '',
            'original_invoice_grand_total': orig_est.grand_total if orig_est else Decimal('0.00'),
            'original_invoice': original_invoice_detail,
            'credit_note_number': je_obj.voucher_number if je_obj else None,
            'credit_note': credit_note_summary,
            'gl_entry': gl_entry_summary,
            'gl_journal_entry': gl_entry_summary,
            'journal_items': journal_items_list,
            'gl_account_2210': gl_account_2210_detail,
            'output_vat_2210': gl_account_2210_detail,
            'audit_chain': {
                'sales_return_number': ret.return_number,
                'credit_note_number': je_obj.voucher_number if je_obj else 'N/A',
                'gl_journal_voucher': je_obj.voucher_number if je_obj else 'N/A',
                'gl_journal_id': je_obj.id if je_obj else None,
                'journal_items_count': len(journal_items_list),
                'output_vat_account': gl_account_2210_detail['account_code'] if gl_account_2210_detail else '2210',
                'output_vat_reversal_amount': gl_account_2210_detail['debit_amount'] if gl_account_2210_detail else final_vat,
                'original_invoice_number': orig_est.estimate_number if orig_est else 'N/A',
                'original_invoice_id': ret.original_estimate_id,
                'is_gl_linked': bool(je_obj is not None),
                'is_vat_2210_linked': bool(gl_account_2210_detail is not None),
            }
        }

    @classmethod
    def get_purchase_return_vat_drilldown(cls, return_id_or_number: Union[int, str]) -> Dict[str, Any]:
        """
        Complete Single-Document Drill-Down Resolver for Supplier Purchase Returns / Debit Notes.
        """
        clean_ref = str(return_id_or_number or '').strip()
        if not clean_ref:
            return {'status': 'NOT_FOUND', 'message': 'No purchase return identifier provided.'}

        pret_qs = PurchaseReturn.objects.select_related(
            'supplier', 'original_grn', 'branch', 'processed_by'
        ).prefetch_related(
            'items__product', 'items__unit_conversion'
        )

        pret = None
        if isinstance(return_id_or_number, int) or clean_ref.isdigit():
            pret = pret_qs.filter(id=int(clean_ref)).first()

        if not pret:
            pret = pret_qs.filter(return_number__iexact=clean_ref).first()

        if not pret:
            return {'status': 'NOT_FOUND', 'message': f'Purchase Return / Debit Note {return_id_or_number} not found.'}

        je_obj = None

        je_obj = JournalEntry.objects.filter(
            source_module='PURCHASE_RETURN',
            source_id=str(pret.id),
            status='POSTED'
        ).prefetch_related('items__account', 'items__customer', 'items__supplier').first()

        if not je_obj:
            je_obj = JournalEntry.objects.filter(
                reference_document__iexact=pret.return_number,
                voucher_type='DEBIT_NOTE',
                status='POSTED'
            ).prefetch_related('items__account', 'items__customer', 'items__supplier').first()

        if not je_obj:
            je_obj = JournalEntry.objects.filter(
                reference_document__iexact=pret.return_number,
                status='POSTED'
            ).prefetch_related('items__account', 'items__customer', 'items__supplier').first()

        if not je_obj:
            je_obj = JournalEntry.objects.filter(
                voucher_number__iexact=pret.return_number,
                status='POSTED'
            ).prefetch_related('items__account', 'items__customer', 'items__supplier').first()

        if not je_obj:
            je_obj = JournalEntry.objects.filter(
                narration__icontains=pret.return_number,
                voucher_type='DEBIT_NOTE',
                status='POSTED'
            ).prefetch_related('items__account', 'items__customer', 'items__supplier').first()

        journal_items_list = []
        gl_account_1410_detail = None
        gl_entry_summary = None
        debit_note_summary = None

        if je_obj:
            gl_entry_summary = {
                'id': je_obj.id,
                'voucher_number': je_obj.voucher_number,
                'voucher_type': je_obj.voucher_type,
                'voucher_type_display': je_obj.get_voucher_type_display(),
                'entry_date': je_obj.entry_date,
                'entry_date_bs': je_obj.entry_date_bs or '',
                'fiscal_year': je_obj.fiscal_year or '',
                'total_debit': je_obj.total_debit,
                'total_credit': je_obj.total_credit,
                'reference_document': je_obj.reference_document or pret.return_number,
                'narration': je_obj.narration or '',
                'status': je_obj.status,
                'status_display': je_obj.get_status_display(),
                'source_module': getattr(je_obj, 'source_module', 'PURCHASE_RETURN'),
                'source_id': getattr(je_obj, 'source_id', str(pret.id)),
            }

            debit_note_summary = {
                'id': je_obj.id,
                'voucher_number': je_obj.voucher_number,
                'debit_note_number': je_obj.voucher_number,
                'voucher_type': je_obj.voucher_type,
                'voucher_type_display': je_obj.get_voucher_type_display(),
                'entry_date': je_obj.entry_date,
                'entry_date_bs': je_obj.entry_date_bs or '',
                'fiscal_year': je_obj.fiscal_year or '',
                'total_amount': je_obj.total_debit,
                'reference_document': je_obj.reference_document or pret.return_number,
                'narration': je_obj.narration or '',
                'status': je_obj.status,
                'status_display': je_obj.get_status_display(),
            }

            for itm in je_obj.items.all():
                is_1410 = bool(
                    itm.account.code == '1410' or
                    itm.account.code.startswith('1410-') or
                    itm.account.system_tag == 'INPUT_VAT'
                )
                item_dict = {
                    'id': itm.id,
                    'account_id': itm.account_id,
                    'account_code': itm.account.code,
                    'account_name': itm.account.name,
                    'debit_amount': itm.debit_amount,
                    'credit_amount': itm.credit_amount,
                    'line_narration': itm.line_narration or '',
                    'customer_name': itm.customer.name if itm.customer else '',
                    'supplier_name': itm.supplier.company_name if itm.supplier else '',
                    'is_input_vat_1410': is_1410,
                    'is_input_vat': is_1410,
                }
                journal_items_list.append(item_dict)

                if is_1410 and not gl_account_1410_detail:
                    gl_account_1410_detail = {
                        'account_code': itm.account.code,
                        'account_name': itm.account.name,
                        'debit_amount': itm.debit_amount,
                        'credit_amount': itm.credit_amount,
                        'vat_reversal_amount': itm.credit_amount,
                        'line_narration': itm.line_narration or '',
                        'is_reversal': True,
                    }

        items_breakdown = [
            cls.extract_purchase_return_item_vat_detail(item)
            for item in pret.items.all()
        ]

        taxable_sum = sum((i['taxable_amount'] for i in items_breakdown), Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        vat_sum = sum((i['vat_amount'] for i in items_breakdown), Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        final_taxable = pret.total_return_amount if (pret.total_return_amount or Decimal('0.00')) > Decimal('0.00') else taxable_sum
        final_vat = pret.tax_amount if (pret.tax_amount or Decimal('0.00')) > Decimal('0.00') else vat_sum

        orig_grn = pret.original_grn
        original_grn_detail = None
        if orig_grn:
            original_grn_detail = {
                'id': orig_grn.id,
                'grn_number': orig_grn.grn_number,
                'supplier_bill_no': orig_grn.supplier_bill_no,
                'challan_no': orig_grn.challan_no or '',
                'bill_date': orig_grn.bill_date,
                'bill_date_bs': orig_grn.bill_date_bs or '',
                'fiscal_year': orig_grn.fiscal_year or '',
                'gross_amount': orig_grn.gross_amount,
                'taxable_amount': orig_grn.taxable_amount,
                'vat_amount': orig_grn.vat_amount,
                'net_total_amount': orig_grn.net_total_amount,
                'status': orig_grn.status,
                'status_display': orig_grn.get_status_display(),
            }

        return {
            'status': 'SUCCESS',
            'document_type': 'PURCHASE_RETURN',
            'id': pret.id,
            'purchase_return_id': pret.id,
            'return_number': pret.return_number,
            'purchase_return_number': pret.return_number,
            'return_date': pret.return_date,
            'return_date_bs': pret.return_date_bs or '',
            'fiscal_year': pret.fiscal_year or '',
            'supplier_id': pret.supplier_id,
            'supplier_name': pret.supplier.company_name,
            'supplier_code': pret.supplier.code if pret.supplier else '',
            'supplier_pan': pret.supplier.pan_number or pret.supplier.vat_number or '',
            'supplier_phone': pret.supplier.phone_number if pret.supplier else '',
            'refund_mode': pret.refund_mode,
            'refund_mode_display': pret.get_refund_mode_display(),
            'remarks': pret.remarks or '',
            'total_return_amount': final_taxable,
            'taxable_amount': final_taxable,
            'tax_amount': final_vat,
            'vat_amount': final_vat,
            'vat_reversal_amount': final_vat,
            'net_refund_amount': pret.net_refund_amount,
            'return_status': pret.status,
            'status': pret.status,
            'status_display': pret.get_status_display(),
            'items': items_breakdown,
            'original_grn_id': pret.original_grn_id,
            'original_grn_number': orig_grn.grn_number if orig_grn else 'N/A',
            'original_bill_reference': pret.original_bill_reference or (orig_grn.supplier_bill_no if orig_grn else ''),
            'original_grn_date': orig_grn.bill_date if orig_grn else None,
            'original_grn_date_bs': orig_grn.bill_date_bs if orig_grn else '',
            'original_grn': original_grn_detail,
            'debit_note_number': je_obj.voucher_number if je_obj else None,
            'debit_note': debit_note_summary,
            'gl_entry': gl_entry_summary,
            'gl_journal_entry': gl_entry_summary,
            'journal_items': journal_items_list,
            'gl_account_1410': gl_account_1410_detail,
            'input_vat_1410': gl_account_1410_detail,
            'audit_chain': {
                'purchase_return_number': pret.return_number,
                'debit_note_number': je_obj.voucher_number if je_obj else 'N/A',
                'gl_journal_voucher': je_obj.voucher_number if je_obj else 'N/A',
                'gl_journal_id': je_obj.id if je_obj else None,
                'journal_items_count': len(journal_items_list),
                'input_vat_account': gl_account_1410_detail['account_code'] if gl_account_1410_detail else '1410',
                'input_vat_reversal_amount': gl_account_1410_detail['credit_amount'] if gl_account_1410_detail else final_vat,
                'original_grn_number': orig_grn.grn_number if orig_grn else 'N/A',
                'original_grn_id': pret.original_grn_id,
                'is_gl_linked': bool(je_obj is not None),
                'is_vat_1410_linked': bool(gl_account_1410_detail is not None),
            }
        }

    # =========================================================================
    # 7. GENERAL LEDGER PARITY RECONCILIATION
    #    (TOTAL-LEVEL & COMPREHENSIVE TRANSACTION-LEVEL AUDIT)
    # =========================================================================
    @classmethod
    def reconcile_vat_with_general_ledger(
        cls,
        branch: Optional[Branch],
        start_date: date,
        end_date: date,
        include_transaction_reconciliation: bool = True
    ) -> Dict[str, Any]:
        """
        Performs comprehensive double-entry audit:
        1. Total-Level Reconciliation:
           - Output VAT (Register Annex 5 Net) vs. General Ledger Account 2210 (Credits - Debits).
           - Input VAT (Register Annex 7 Net) vs. General Ledger Account 1410 (Debits - Credits).
        2. Transaction-Level Reconciliation:
           - Checks every single Sales Invoice, Sales Return, GRN, and Purchase Return transaction.
           - Compares Expected VAT with GL VAT and reports exact differences.
           - Matches documents using multi-attribute reliable identifiers:
             source_module, source_id, reference_document, voucher_type, branch, and date.
           - Flags and isolates:
             * Missing GL entries
             * Orphan / unlinked manual GL entries on VAT accounts
             * Duplicate GL postings
             * Amount mismatches
             * Date mismatches
             * Branch mismatches
             * Reference mismatches
             * Differences between return-item VAT totals and the return header
        """
        sales = cls.get_sales_vat_summary(branch, start_date, end_date, include_drilldown=False)
        purchases = cls.get_purchase_vat_summary(branch, start_date, end_date, include_drilldown=False)

        register_output_vat = sales['totals']['vat']
        register_input_vat = purchases['totals']['vat']

        # ---------------------------------------------------------------------
        # A. TOTAL-LEVEL RECONCILIATION (ACCOUNTS 2210 & 1410)
        # ---------------------------------------------------------------------
        output_vat_items = JournalItem.objects.filter(
            Q(account__code='2210') | Q(account__code__startswith='2210-') | Q(account__system_tag='OUTPUT_VAT'),
            journal_entry__status='POSTED',
            journal_entry__entry_date__gte=start_date,
            journal_entry__entry_date__lte=end_date
        )
        if branch:
            output_vat_items = output_vat_items.filter(journal_entry__branch=branch)

        gl_output_agg = output_vat_items.aggregate(
            cr=Coalesce(Sum('credit_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            dr=Coalesce(Sum('debit_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2)))
        )
        gl_output_vat = (gl_output_agg['cr'] - gl_output_agg['dr']).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        input_vat_items = JournalItem.objects.filter(
            Q(account__code='1410') | Q(account__code__startswith='1410-') | Q(account__system_tag='INPUT_VAT'),
            journal_entry__status='POSTED',
            journal_entry__entry_date__gte=start_date,
            journal_entry__entry_date__lte=end_date
        )
        if branch:
            input_vat_items = input_vat_items.filter(journal_entry__branch=branch)

        gl_input_agg = input_vat_items.aggregate(
            dr=Coalesce(Sum('debit_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            cr=Coalesce(Sum('credit_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2)))
        )
        gl_input_vat = (gl_input_agg['dr'] - gl_input_agg['cr']).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        output_variance = abs(register_output_vat - gl_output_vat).quantize(Decimal('0.01'))
        input_variance = abs(register_input_vat - gl_input_vat).quantize(Decimal('0.01'))

        is_totals_reconciled = (output_variance == Decimal('0.00') and input_variance == Decimal('0.00'))

        # ---------------------------------------------------------------------
        # B. FULL TRANSACTION-LEVEL VAT ↔ GL RECONCILIATION
        # ---------------------------------------------------------------------
        tx_recon = None
        has_tx_discrepancies = False

        if include_transaction_reconciliation:
            tx_recon = cls.reconcile_transactions_vat_with_gl(branch, start_date, end_date)
            has_tx_discrepancies = bool(tx_recon.get('discrepancies') and len(tx_recon['discrepancies']) > 0)

        is_fully_reconciled = is_totals_reconciled and not has_tx_discrepancies

        return {
            'branch': branch,
            'start_date': start_date,
            'end_date': end_date,
            'is_reconciled': is_fully_reconciled,
            'is_totals_reconciled': is_totals_reconciled,
            'is_transactions_reconciled': not has_tx_discrepancies,
            'output_vat': {
                'register_amount': register_output_vat,
                'gl_amount': gl_output_vat,
                'variance': output_variance,
                'is_matched': (output_variance == Decimal('0.00')),
            },
            'input_vat': {
                'register_amount': register_input_vat,
                'gl_amount': gl_input_vat,
                'variance': input_variance,
                'is_matched': (input_variance == Decimal('0.00')),
            },
            'net_vat': {
                'register_net': (register_output_vat - register_input_vat).quantize(Decimal('0.01')),
                'gl_net': (gl_output_vat - gl_input_vat).quantize(Decimal('0.01')),
            },
            'transaction_reconciliation': tx_recon
        }

    # =========================================================================
    # 8. TRANSACTION-LEVEL VAT ↔ GL RECONCILIATION ENGINE
    # =========================================================================
    @classmethod
    def reconcile_transactions_vat_with_gl(
        cls,
        branch: Optional[Branch],
        start_date: date,
        end_date: date
    ) -> Dict[str, Any]:
        """
        Detailed Transaction-Level Reconciliation.
        Examines every single VAT transaction across 4 registers:
          1. Sales Invoices (Annex 5)
          2. Sales Returns / Credit Notes
          3. Goods Received Notes (Annex 7)
          4. Purchase Returns / Debit Notes
        Calculates expected Sales Return VAT reversal from the corrected return-item
        figures rather than blindly trusting a zero or incorrect header.
        Detects differences between return-item VAT totals and the return header.
        """
        config = SystemConfiguration.get_solo()
        is_vat_shop = (config.tax_system_mode == 'VAT')

        discrepancies: List[Dict[str, Any]] = []

        # -------------------------------------------------------------
        # 1. LOAD MOVEMENTS FROM REGISTERS
        # -------------------------------------------------------------
        sales_filter: Dict[str, Any] = {
            'bill_date_ad__gte': start_date,
            'bill_date_ad__lte': end_date,
            'status__in': cls.VALID_SALES_STATUSES,
        }
        if branch:
            sales_filter['branch'] = branch

        sales_qs = list(
            SalesEstimate.objects.filter(**sales_filter)
            .select_related('customer', 'branch')
            .prefetch_related('items__product')
            .order_by('bill_date_ad', 'estimate_number')
        )

        sales_return_filter: Dict[str, Any] = {
            'return_date_ad__gte': start_date,
            'return_date_ad__lte': end_date,
        }
        if branch:
            sales_return_filter['branch'] = branch

        sales_returns_qs = list(
            SalesReturn.objects.filter(**sales_return_filter)
            .select_related('customer', 'original_estimate', 'branch')
            .prefetch_related('items__product', 'items__estimate_item')
            .order_by('return_date_ad', 'return_number')
        )

        grn_filter: Dict[str, Any] = {
            'bill_date__gte': start_date,
            'bill_date__lte': end_date,
            'status': 'RECEIVED',
        }
        if branch:
            grn_filter['branch'] = branch

        grn_qs = list(
            GoodsReceivedNote.objects.filter(**grn_filter)
            .select_related('supplier', 'branch')
            .prefetch_related('items__product')
            .order_by('bill_date', 'grn_number')
        )

        pret_filter: Dict[str, Any] = {
            'return_date__gte': start_date,
            'return_date__lte': end_date,
            'status': 'CONFIRMED',
        }
        if branch:
            pret_filter['branch'] = branch

        purchase_returns_qs = list(
            PurchaseReturn.objects.filter(**pret_filter)
            .select_related('supplier', 'original_grn', 'branch')
            .prefetch_related('items__product')
            .order_by('return_date', 'return_number')
        )

        # -------------------------------------------------------------
        # 2. COLLECT DOCUMENT REFERENCES FOR FAST GL MATCHING
        # -------------------------------------------------------------
        all_doc_refs = set()
        all_doc_refs.update(est.estimate_number for est in sales_qs if est.estimate_number)
        all_doc_refs.update(ret.return_number for ret in sales_returns_qs if ret.return_number)
        all_doc_refs.update(grn.grn_number for grn in grn_qs if grn.grn_number)
        all_doc_refs.update(pret.return_number for pret in purchase_returns_qs if pret.return_number)

        all_doc_ids = set()
        all_doc_ids.update(str(est.id) for est in sales_qs)
        all_doc_ids.update(str(ret.id) for ret in sales_returns_qs)
        all_doc_ids.update(str(grn.id) for grn in grn_qs)
        all_doc_ids.update(str(pret.id) for pret in purchase_returns_qs)

        gl_query_filter = Q(
            status='POSTED',
            entry_date__gte=start_date,
            entry_date__lte=end_date,
        )
        if branch:
            gl_query_filter &= Q(branch=branch)

        ref_link_filter = Q(status='POSTED') & (
            Q(reference_document__in=all_doc_refs) |
            Q(source_id__in=all_doc_ids) |
            Q(voucher_number__in=all_doc_refs)
        )

        all_je_qs = JournalEntry.objects.filter(
            gl_query_filter | ref_link_filter
        ).select_related('branch').prefetch_related(
            'items__account'
        ).distinct()

        je_cache: Dict[int, JournalEntry] = {}
        je_vat_data: Dict[int, Dict[str, Any]] = {}
        by_source_id: Dict[Tuple[str, str], List[int]] = {}
        by_reference_doc: Dict[str, List[int]] = {}
        by_voucher_num: Dict[str, List[int]] = {}

        for je in all_je_qs:
            je_cache[je.id] = je

            net_2210_cr = Decimal('0.00')
            net_2210_dr = Decimal('0.00')
            net_1410_dr = Decimal('0.00')
            net_1410_cr = Decimal('0.00')

            has_vat_line = False

            for itm in je.items.all():
                code = itm.account.code or ''
                tag = itm.account.system_tag or ''

                if code == '2210' or code.startswith('2210-') or tag == 'OUTPUT_VAT':
                    net_2210_cr += itm.credit_amount
                    net_2210_dr += itm.debit_amount
                    has_vat_line = True

                elif code == '1410' or code.startswith('1410-') or tag == 'INPUT_VAT':
                    net_1410_dr += itm.debit_amount
                    net_1410_cr += itm.credit_amount
                    has_vat_line = True

            je_vat_data[je.id] = {
                'has_vat_line': has_vat_line,
                'output_vat_cr': (net_2210_cr - net_2210_dr).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
                'output_vat_reversal_dr': (net_2210_dr - net_2210_cr).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
                'input_vat_dr': (net_1410_dr - net_1410_cr).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
                'input_vat_reversal_cr': (net_1410_cr - net_1410_dr).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
            }

            if je.source_module and je.source_id:
                key = (je.source_module.strip().upper(), str(je.source_id).strip())
                by_source_id.setdefault(key, []).append(je.id)

            if je.reference_document:
                ref_key = je.reference_document.strip().upper()
                by_reference_doc.setdefault(ref_key, []).append(je.id)

            if je.voucher_number:
                v_key = je.voucher_number.strip().upper()
                by_voucher_num.setdefault(v_key, []).append(je.id)

        claimed_je_ids: Set[int] = set()

        def _find_candidate_jes(source_modules: List[str], source_id: str, ref_doc: str, voucher_type: Optional[str] = None) -> List[JournalEntry]:
            candidate_ids = []

            for sm in source_modules:
                sm_key = (sm.strip().upper(), str(source_id).strip())
                if sm_key in by_source_id:
                    candidate_ids.extend(by_source_id[sm_key])

            ref_clean = (ref_doc or '').strip().upper()
            if ref_clean and ref_clean in by_reference_doc:
                candidate_ids.extend(by_reference_doc[ref_clean])

            if ref_clean and ref_clean in by_voucher_num:
                candidate_ids.extend(by_voucher_num[ref_clean])

            seen = set()
            unique_ids = []
            for cid in candidate_ids:
                if cid not in seen and cid in je_cache:
                    seen.add(cid)
                    unique_ids.append(cid)

            if voucher_type:
                typed = [cid for cid in unique_ids if je_cache[cid].voucher_type == voucher_type]
                if typed:
                    return [je_cache[cid] for cid in typed]

            return [je_cache[cid] for cid in unique_ids]

        # -------------------------------------------------------------
        # 3. RECONCILE SALES INVOICES (ANNEX 5)
        # -------------------------------------------------------------
        reconciled_sales = []
        for est in sales_qs:
            sales_items_detail = [
                cls.extract_sales_item_vat_detail(item, is_vat_shop)
                for item in est.items.all()
            ]
            sales_items_vat_total = sum((i['vat_amount'] for i in sales_items_detail), Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            sales_header_vat = (est.vat_amount or Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            expected_vat = sales_items_vat_total if (sales_items_vat_total > Decimal('0.00') or sales_header_vat == Decimal('0.00')) else sales_header_vat

            item_discrepancies = []
            is_matched = False
            gl_vat = Decimal('0.00')
            primary_je = None

            if sales_header_vat != sales_items_vat_total:
                item_discrepancies.append('HEADER_ITEM_VAT_MISMATCH')
                discrepancies.append({
                    'type': 'HEADER_ITEM_VAT_MISMATCH',
                    'severity': 'MEDIUM',
                    'document_type': 'SALES_INVOICE',
                    'document_number': est.estimate_number,
                    'document_date': str(est.bill_date_ad),
                    'message': f"Sales Invoice {est.estimate_number} header VAT (Rs. {sales_header_vat:,.2f}) does not match line-items VAT total (Rs. {sales_items_vat_total:,.2f}).",
                    'expected': sales_items_vat_total,
                    'actual': sales_header_vat,
                    'difference': abs(sales_items_vat_total - sales_header_vat),
                })

            candidates = _find_candidate_jes(['POS_SALE', 'SALES'], str(est.id), est.estimate_number, 'SALES')

            if not candidates:
                if expected_vat > Decimal('0.00'):
                    item_discrepancies.append('MISSING_GL_ENTRY')
                    discrepancies.append({
                        'type': 'MISSING_GL_ENTRY',
                        'severity': 'HIGH',
                        'document_type': 'SALES_INVOICE',
                        'document_number': est.estimate_number,
                        'document_date': str(est.bill_date_ad),
                        'message': f"Sales Invoice {est.estimate_number} has Expected VAT of Rs. {expected_vat:,.2f} but no posted GL journal entry exists.",
                        'expected': expected_vat,
                        'actual': Decimal('0.00'),
                        'difference': expected_vat,
                    })
                else:
                    if not item_discrepancies:
                        is_matched = True

            elif len(candidates) > 1:
                item_discrepancies.append('DUPLICATE_GL_POSTING')
                v_nums = [c.voucher_number for c in candidates]
                actual_gl_sum = sum(je_vat_data[c.id]['output_vat_cr'] for c in candidates)
                discrepancies.append({
                    'type': 'DUPLICATE_GL_POSTING',
                    'severity': 'HIGH',
                    'document_type': 'SALES_INVOICE',
                    'document_number': est.estimate_number,
                    'document_date': str(est.bill_date_ad),
                    'message': f"Sales Invoice {est.estimate_number} is linked to {len(candidates)} duplicate posted GL entries: {', '.join(v_nums)}.",
                    'expected': expected_vat,
                    'actual': actual_gl_sum,
                    'difference': abs(expected_vat - actual_gl_sum),
                })
                primary_je = candidates[0]
                gl_vat = actual_gl_sum
                for c in candidates:
                    claimed_je_ids.add(c.id)

            else:
                primary_je = candidates[0]
                claimed_je_ids.add(primary_je.id)
                gl_vat = je_vat_data[primary_je.id]['output_vat_cr']

                if gl_vat != expected_vat:
                    item_discrepancies.append('AMOUNT_MISMATCH')
                    discrepancies.append({
                        'type': 'AMOUNT_MISMATCH',
                        'severity': 'HIGH',
                        'document_type': 'SALES_INVOICE',
                        'document_number': est.estimate_number,
                        'document_date': str(est.bill_date_ad),
                        'message': f"Sales Invoice {est.estimate_number} VAT mismatch: Expected Rs. {expected_vat:,.2f} vs GL Voucher {primary_je.voucher_number} Output VAT Rs. {gl_vat:,.2f}.",
                        'expected': expected_vat,
                        'actual': gl_vat,
                        'difference': abs(expected_vat - gl_vat),
                    })

                if primary_je.entry_date != est.bill_date_ad:
                    item_discrepancies.append('DATE_MISMATCH')
                    discrepancies.append({
                        'type': 'DATE_MISMATCH',
                        'severity': 'MEDIUM',
                        'document_type': 'SALES_INVOICE',
                        'document_number': est.estimate_number,
                        'document_date': str(est.bill_date_ad),
                        'message': f"Sales Invoice {est.estimate_number} date ({est.bill_date_ad}) differs from GL Voucher {primary_je.voucher_number} entry date ({primary_je.entry_date}).",
                        'expected': str(est.bill_date_ad),
                        'actual': str(primary_je.entry_date),
                        'difference': None,
                    })

                if est.branch_id and primary_je.branch_id and primary_je.branch_id != est.branch_id:
                    item_discrepancies.append('BRANCH_MISMATCH')
                    discrepancies.append({
                        'type': 'BRANCH_MISMATCH',
                        'severity': 'MEDIUM',
                        'document_type': 'SALES_INVOICE',
                        'document_number': est.estimate_number,
                        'document_date': str(est.bill_date_ad),
                        'message': f"Sales Invoice {est.estimate_number} branch ({est.branch.code}) differs from GL Voucher {primary_je.voucher_number} branch ({primary_je.branch.code if primary_je.branch else 'HQ'}).",
                        'expected': est.branch.code,
                        'actual': primary_je.branch.code if primary_je.branch else 'HQ',
                        'difference': None,
                    })

                if primary_je.reference_document and primary_je.reference_document.strip().upper() != est.estimate_number.strip().upper():
                    item_discrepancies.append('REFERENCE_MISMATCH')
                    discrepancies.append({
                        'type': 'REFERENCE_MISMATCH',
                        'severity': 'MEDIUM',
                        'document_type': 'SALES_INVOICE',
                        'document_number': est.estimate_number,
                        'document_date': str(est.bill_date_ad),
                        'message': f"Sales Invoice {est.estimate_number} reference document mismatch: Expected '{est.estimate_number}' but GL Voucher {primary_je.voucher_number} reference is '{primary_je.reference_document}'.",
                        'expected': est.estimate_number,
                        'actual': primary_je.reference_document,
                        'difference': None,
                    })

                if not item_discrepancies:
                    is_matched = True

            diff_amt = abs(expected_vat - gl_vat).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            reconciled_sales.append({
                'document_type': 'SALES_INVOICE',
                'document_id': est.id,
                'document_number': est.estimate_number,
                'document_date': est.bill_date_ad,
                'document_date_bs': est.bill_date_bs or '',
                'party_name': est.recipient_display_name,
                'party_pan': est.customer_pan or (est.customer.pan_number if est.customer and est.customer.pan_number else ''),
                'expected_vat': expected_vat,
                'gl_vat': gl_vat,
                'difference': diff_amt,
                'is_matched': is_matched,
                'status': 'MATCHED' if is_matched else 'DISCREPANCY',
                'gl_voucher_number': primary_je.voucher_number if primary_je else None,
                'gl_voucher_id': primary_je.id if primary_je else None,
                'gl_entry_date': primary_je.entry_date if primary_je else None,
                'discrepancy_reasons': item_discrepancies,
            })

        # -------------------------------------------------------------
        # 4. RECONCILE SALES RETURNS / CREDIT NOTES
        #    (CALCULATES EXPECTED VAT FROM CORRECTED RETURN-ITEM FIGURES)
        # -------------------------------------------------------------
        reconciled_sales_returns = []
        for ret in sales_returns_qs:
            # 1. Calculate return items detail using the corrected extract_sales_return_item_vat_detail
            items_detail = [
                cls.extract_sales_return_item_vat_detail(item, is_vat_shop)
                for item in ret.items.all()
            ]
            items_vat_total = sum((i['vat_amount'] for i in items_detail), Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            items_taxable_total = sum((i['taxable_amount'] for i in items_detail), Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            header_vat = (ret.vat_amount or Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            # 2. Expected VAT reversal from corrected return-item figures rather than blindly trusting a zero or incorrect header
            expected_vat_rev = items_vat_total if (items_vat_total > Decimal('0.00') or header_vat == Decimal('0.00')) else header_vat

            item_discrepancies = []
            is_matched = False
            gl_vat_rev = Decimal('0.00')
            primary_je = None

            # 3. Detect and report differences between return-item VAT totals and the return header
            if header_vat != items_vat_total:
                item_discrepancies.append('HEADER_ITEM_VAT_MISMATCH')
                discrepancies.append({
                    'type': 'HEADER_ITEM_VAT_MISMATCH',
                    'severity': 'MEDIUM',
                    'document_type': 'SALES_RETURN',
                    'document_number': ret.return_number,
                    'document_date': str(ret.return_date_ad),
                    'message': f"Sales Return {ret.return_number} header VAT (Rs. {header_vat:,.2f}) does not match line-items VAT reversal total (Rs. {items_vat_total:,.2f}).",
                    'expected': items_vat_total,
                    'actual': header_vat,
                    'difference': abs(items_vat_total - header_vat),
                })

            candidates = _find_candidate_jes(['SALES_RETURN'], str(ret.id), ret.return_number, 'CREDIT_NOTE')

            if not candidates:
                if expected_vat_rev > Decimal('0.00'):
                    item_discrepancies.append('MISSING_GL_ENTRY')
                    discrepancies.append({
                        'type': 'MISSING_GL_ENTRY',
                        'severity': 'HIGH',
                        'document_type': 'SALES_RETURN',
                        'document_number': ret.return_number,
                        'document_date': str(ret.return_date_ad),
                        'message': f"Sales Return {ret.return_number} has Expected VAT Reversal of Rs. {expected_vat_rev:,.2f} but no posted Credit Note GL entry exists.",
                        'expected': expected_vat_rev,
                        'actual': Decimal('0.00'),
                        'difference': expected_vat_rev,
                    })
                else:
                    if not item_discrepancies:
                        is_matched = True

            elif len(candidates) > 1:
                item_discrepancies.append('DUPLICATE_GL_POSTING')
                v_nums = [c.voucher_number for c in candidates]
                actual_gl_sum = sum(je_vat_data[c.id]['output_vat_reversal_dr'] for c in candidates)
                discrepancies.append({
                    'type': 'DUPLICATE_GL_POSTING',
                    'severity': 'HIGH',
                    'document_type': 'SALES_RETURN',
                    'document_number': ret.return_number,
                    'document_date': str(ret.return_date_ad),
                    'message': f"Sales Return {ret.return_number} is linked to {len(candidates)} duplicate posted GL entries: {', '.join(v_nums)}.",
                    'expected': expected_vat_rev,
                    'actual': actual_gl_sum,
                    'difference': abs(expected_vat_rev - actual_gl_sum),
                })
                primary_je = candidates[0]
                gl_vat_rev = actual_gl_sum
                for c in candidates:
                    claimed_je_ids.add(c.id)

            else:
                primary_je = candidates[0]
                claimed_je_ids.add(primary_je.id)
                # Compare expected VAT reversal with the actual debit to Output VAT account 2210
                gl_vat_rev = je_vat_data[primary_je.id]['output_vat_reversal_dr']

                if gl_vat_rev != expected_vat_rev:
                    item_discrepancies.append('INCORRECT_VAT_REVERSAL')
                    discrepancies.append({
                        'type': 'INCORRECT_VAT_REVERSAL',
                        'severity': 'HIGH',
                        'document_type': 'SALES_RETURN',
                        'document_number': ret.return_number,
                        'document_date': str(ret.return_date_ad),
                        'message': f"Sales Return {ret.return_number} Output VAT reversal mismatch: Expected Rs. {expected_vat_rev:,.2f} vs GL Voucher {primary_je.voucher_number} Output VAT 2210 Debit Rs. {gl_vat_rev:,.2f}.",
                        'expected': expected_vat_rev,
                        'actual': gl_vat_rev,
                        'difference': abs(expected_vat_rev - gl_vat_rev),
                    })

                if primary_je.entry_date != ret.return_date_ad:
                    item_discrepancies.append('DATE_MISMATCH')
                    discrepancies.append({
                        'type': 'DATE_MISMATCH',
                        'severity': 'MEDIUM',
                        'document_type': 'SALES_RETURN',
                        'document_number': ret.return_number,
                        'document_date': str(ret.return_date_ad),
                        'message': f"Sales Return {ret.return_number} date ({ret.return_date_ad}) differs from GL Voucher {primary_je.voucher_number} entry date ({primary_je.entry_date}).",
                        'expected': str(ret.return_date_ad),
                        'actual': str(primary_je.entry_date),
                        'difference': None,
                    })

                if ret.branch_id and primary_je.branch_id and primary_je.branch_id != ret.branch_id:
                    item_discrepancies.append('BRANCH_MISMATCH')
                    discrepancies.append({
                        'type': 'BRANCH_MISMATCH',
                        'severity': 'MEDIUM',
                        'document_type': 'SALES_RETURN',
                        'document_number': ret.return_number,
                        'document_date': str(ret.return_date_ad),
                        'message': f"Sales Return {ret.return_number} branch ({ret.branch.code}) differs from GL Voucher {primary_je.voucher_number} branch ({primary_je.branch.code if primary_je.branch else 'HQ'}).",
                        'expected': ret.branch.code,
                        'actual': primary_je.branch.code if primary_je.branch else 'HQ',
                        'difference': None,
                    })

                if primary_je.reference_document and primary_je.reference_document.strip().upper() != ret.return_number.strip().upper():
                    item_discrepancies.append('REFERENCE_MISMATCH')
                    discrepancies.append({
                        'type': 'REFERENCE_MISMATCH',
                        'severity': 'MEDIUM',
                        'document_type': 'SALES_RETURN',
                        'document_number': ret.return_number,
                        'document_date': str(ret.return_date_ad),
                        'message': f"Sales Return {ret.return_number} reference document mismatch: Expected '{ret.return_number}' but GL Voucher {primary_je.voucher_number} reference is '{primary_je.reference_document}'.",
                        'expected': ret.return_number,
                        'actual': primary_je.reference_document,
                        'difference': None,
                    })

                if not item_discrepancies:
                    is_matched = True

            diff_amt = abs(expected_vat_rev - gl_vat_rev).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            reconciled_sales_returns.append({
                'document_type': 'SALES_RETURN',
                'document_id': ret.id,
                'document_number': ret.return_number,
                'original_invoice_number': ret.original_estimate.estimate_number if ret.original_estimate else 'N/A',
                'document_date': ret.return_date_ad,
                'document_date_bs': ret.return_date_bs or '',
                'party_name': ret.customer.name if ret.customer else (ret.original_estimate.recipient_display_name if ret.original_estimate else 'Walk-in'),
                'expected_vat': expected_vat_rev,
                'gl_vat': gl_vat_rev,
                'difference': diff_amt,
                'is_matched': is_matched,
                'status': 'MATCHED' if is_matched else 'DISCREPANCY',
                'credit_note_number': primary_je.voucher_number if primary_je else None,
                'gl_voucher_number': primary_je.voucher_number if primary_je else None,
                'gl_voucher_id': primary_je.id if primary_je else None,
                'gl_entry_date': primary_je.entry_date if primary_je else None,
                'discrepancy_reasons': item_discrepancies,
            })

        # -------------------------------------------------------------
        # 5. RECONCILE INWARD GRNS (ANNEX 7)
        # -------------------------------------------------------------
        reconciled_grns = []
        for grn in grn_qs:
            grn_items_detail = [
                cls.extract_grn_item_vat_detail(item)
                for item in grn.items.all()
            ]
            grn_items_vat_total = sum((i['vat_amount'] for i in grn_items_detail), Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            grn_header_vat = (grn.vat_amount or Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            expected_input_vat = grn_items_vat_total if (grn_items_vat_total > Decimal('0.00') or grn_header_vat == Decimal('0.00')) else grn_header_vat

            item_discrepancies = []
            is_matched = False
            gl_input_vat = Decimal('0.00')
            primary_je = None

            if grn_header_vat != grn_items_vat_total:
                item_discrepancies.append('HEADER_ITEM_VAT_MISMATCH')
                discrepancies.append({
                    'type': 'HEADER_ITEM_VAT_MISMATCH',
                    'severity': 'MEDIUM',
                    'document_type': 'GOODS_RECEIVED_NOTE',
                    'document_number': grn.grn_number,
                    'supplier_bill_no': grn.supplier_bill_no,
                    'document_date': str(grn.bill_date),
                    'message': f"GRN {grn.grn_number} header VAT (Rs. {grn_header_vat:,.2f}) does not match line-items VAT total (Rs. {grn_items_vat_total:,.2f}).",
                    'expected': grn_items_vat_total,
                    'actual': grn_header_vat,
                    'difference': abs(grn_items_vat_total - grn_header_vat),
                })

            candidates = _find_candidate_jes(['PURCHASE_GRN', 'PURCHASE'], str(grn.id), grn.grn_number, 'PURCHASE')

            if not candidates:
                if expected_input_vat > Decimal('0.00'):
                    item_discrepancies.append('MISSING_GL_ENTRY')
                    discrepancies.append({
                        'type': 'MISSING_GL_ENTRY',
                        'severity': 'HIGH',
                        'document_type': 'GOODS_RECEIVED_NOTE',
                        'document_number': grn.grn_number,
                        'supplier_bill_no': grn.supplier_bill_no,
                        'document_date': str(grn.bill_date),
                        'message': f"GRN {grn.grn_number} (Bill: {grn.supplier_bill_no}) has Expected Input VAT of Rs. {expected_input_vat:,.2f} but no posted GL entry exists.",
                        'expected': expected_input_vat,
                        'actual': Decimal('0.00'),
                        'difference': expected_input_vat,
                    })
                else:
                    if not item_discrepancies:
                        is_matched = True

            elif len(candidates) > 1:
                item_discrepancies.append('DUPLICATE_GL_POSTING')
                v_nums = [c.voucher_number for c in candidates]
                actual_gl_sum = sum(je_vat_data[c.id]['input_vat_dr'] for c in candidates)
                discrepancies.append({
                    'type': 'DUPLICATE_GL_POSTING',
                    'severity': 'HIGH',
                    'document_type': 'GOODS_RECEIVED_NOTE',
                    'document_number': grn.grn_number,
                    'document_date': str(grn.bill_date),
                    'message': f"GRN {grn.grn_number} is linked to {len(candidates)} duplicate posted GL entries: {', '.join(v_nums)}.",
                    'expected': expected_input_vat,
                    'actual': actual_gl_sum,
                    'difference': abs(expected_input_vat - actual_gl_sum),
                })
                primary_je = candidates[0]
                gl_input_vat = actual_gl_sum
                for c in candidates:
                    claimed_je_ids.add(c.id)

            else:
                primary_je = candidates[0]
                claimed_je_ids.add(primary_je.id)
                gl_input_vat = je_vat_data[primary_je.id]['input_vat_dr']

                if gl_input_vat != expected_input_vat:
                    item_discrepancies.append('AMOUNT_MISMATCH')
                    discrepancies.append({
                        'type': 'AMOUNT_MISMATCH',
                        'severity': 'HIGH',
                        'document_type': 'GOODS_RECEIVED_NOTE',
                        'document_number': grn.grn_number,
                        'document_date': str(grn.bill_date),
                        'message': f"GRN {grn.grn_number} Input VAT mismatch: Expected Rs. {expected_input_vat:,.2f} vs GL Voucher {primary_je.voucher_number} Input VAT Rs. {gl_input_vat:,.2f}.",
                        'expected': expected_input_vat,
                        'actual': gl_input_vat,
                        'difference': abs(expected_input_vat - gl_input_vat),
                    })

                if primary_je.entry_date != grn.bill_date:
                    item_discrepancies.append('DATE_MISMATCH')
                    discrepancies.append({
                        'type': 'DATE_MISMATCH',
                        'severity': 'MEDIUM',
                        'document_type': 'GOODS_RECEIVED_NOTE',
                        'document_number': grn.grn_number,
                        'document_date': str(grn.bill_date),
                        'message': f"GRN {grn.grn_number} date ({grn.bill_date}) differs from GL Voucher {primary_je.voucher_number} entry date ({primary_je.entry_date}).",
                        'expected': str(grn.bill_date),
                        'actual': str(primary_je.entry_date),
                        'difference': None,
                    })

                if grn.branch_id and primary_je.branch_id and primary_je.branch_id != grn.branch_id:
                    item_discrepancies.append('BRANCH_MISMATCH')
                    discrepancies.append({
                        'type': 'BRANCH_MISMATCH',
                        'severity': 'MEDIUM',
                        'document_type': 'GOODS_RECEIVED_NOTE',
                        'document_number': grn.grn_number,
                        'document_date': str(grn.bill_date),
                        'message': f"GRN {grn.grn_number} branch ({grn.branch.code}) differs from GL Voucher {primary_je.voucher_number} branch ({primary_je.branch.code if primary_je.branch else 'HQ'}).",
                        'expected': grn.branch.code,
                        'actual': primary_je.branch.code if primary_je.branch else 'HQ',
                        'difference': None,
                    })

                if primary_je.reference_document and primary_je.reference_document.strip().upper() != grn.grn_number.strip().upper():
                    item_discrepancies.append('REFERENCE_MISMATCH')
                    discrepancies.append({
                        'type': 'REFERENCE_MISMATCH',
                        'severity': 'MEDIUM',
                        'document_type': 'GOODS_RECEIVED_NOTE',
                        'document_number': grn.grn_number,
                        'document_date': str(grn.bill_date),
                        'message': f"GRN {grn.grn_number} reference document mismatch: Expected '{grn.grn_number}' but GL Voucher {primary_je.voucher_number} reference is '{primary_je.reference_document}'.",
                        'expected': grn.grn_number,
                        'actual': primary_je.reference_document,
                        'difference': None,
                    })

                if not item_discrepancies:
                    is_matched = True

            diff_amt = abs(expected_input_vat - gl_input_vat).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            reconciled_grns.append({
                'document_type': 'GOODS_RECEIVED_NOTE',
                'document_id': grn.id,
                'document_number': grn.grn_number,
                'supplier_bill_no': grn.supplier_bill_no,
                'challan_no': grn.challan_no or '',
                'document_date': grn.bill_date,
                'document_date_bs': grn.bill_date_bs or '',
                'party_name': grn.supplier.company_name,
                'party_pan': grn.supplier.pan_number or grn.supplier.vat_number or '',
                'expected_vat': expected_input_vat,
                'gl_vat': gl_input_vat,
                'difference': diff_amt,
                'is_matched': is_matched,
                'status': 'MATCHED' if is_matched else 'DISCREPANCY',
                'gl_voucher_number': primary_je.voucher_number if primary_je else None,
                'gl_voucher_id': primary_je.id if primary_je else None,
                'gl_entry_date': primary_je.entry_date if primary_je else None,
                'discrepancy_reasons': item_discrepancies,
            })

        # -------------------------------------------------------------
        # 6. RECONCILE PURCHASE RETURNS / DEBIT NOTES
        # -------------------------------------------------------------
        reconciled_purchase_returns = []
        for pret in purchase_returns_qs:
            pret_items_detail = [
                cls.extract_purchase_return_item_vat_detail(item)
                for item in pret.items.all()
            ]
            pret_items_vat_total = sum((i['vat_amount'] for i in pret_items_detail), Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            pret_header_vat = (pret.tax_amount or Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            expected_tax_rev = pret_items_vat_total if (pret_items_vat_total > Decimal('0.00') or pret_header_vat == Decimal('0.00')) else pret_header_vat

            item_discrepancies = []
            is_matched = False
            gl_tax_rev = Decimal('0.00')
            primary_je = None

            if pret_header_vat != pret_items_vat_total:
                item_discrepancies.append('HEADER_ITEM_VAT_MISMATCH')
                discrepancies.append({
                    'type': 'HEADER_ITEM_VAT_MISMATCH',
                    'severity': 'MEDIUM',
                    'document_type': 'PURCHASE_RETURN',
                    'document_number': pret.return_number,
                    'document_date': str(pret.return_date),
                    'message': f"Purchase Return {pret.return_number} header VAT (Rs. {pret_header_vat:,.2f}) does not match line-items tax total (Rs. {pret_items_vat_total:,.2f}).",
                    'expected': pret_items_vat_total,
                    'actual': pret_header_vat,
                    'difference': abs(pret_items_vat_total - pret_header_vat),
                })

            candidates = _find_candidate_jes(['PURCHASE_RETURN'], str(pret.id), pret.return_number, 'DEBIT_NOTE')

            if not candidates:
                if expected_tax_rev > Decimal('0.00'):
                    item_discrepancies.append('MISSING_GL_ENTRY')
                    discrepancies.append({
                        'type': 'MISSING_GL_ENTRY',
                        'severity': 'HIGH',
                        'document_type': 'PURCHASE_RETURN',
                        'document_number': pret.return_number,
                        'document_date': str(pret.return_date),
                        'message': f"Purchase Return {pret.return_number} has Expected Input VAT Reversal of Rs. {expected_tax_rev:,.2f} but no posted Debit Note GL entry exists.",
                        'expected': expected_tax_rev,
                        'actual': Decimal('0.00'),
                        'difference': expected_tax_rev,
                    })
                else:
                    if not item_discrepancies:
                        is_matched = True

            elif len(candidates) > 1:
                item_discrepancies.append('DUPLICATE_GL_POSTING')
                v_nums = [c.voucher_number for c in candidates]
                actual_gl_sum = sum(je_vat_data[c.id]['input_vat_reversal_cr'] for c in candidates)
                discrepancies.append({
                    'type': 'DUPLICATE_GL_POSTING',
                    'severity': 'HIGH',
                    'document_type': 'PURCHASE_RETURN',
                    'document_number': pret.return_number,
                    'document_date': str(pret.return_date),
                    'message': f"Purchase Return {pret.return_number} is linked to {len(candidates)} duplicate posted GL entries: {', '.join(v_nums)}.",
                    'expected': expected_tax_rev,
                    'actual': actual_gl_sum,
                    'difference': abs(expected_tax_rev - actual_gl_sum),
                })
                primary_je = candidates[0]
                gl_tax_rev = actual_gl_sum
                for c in candidates:
                    claimed_je_ids.add(c.id)

            else:
                primary_je = candidates[0]
                claimed_je_ids.add(primary_je.id)
                gl_tax_rev = je_vat_data[primary_je.id]['input_vat_reversal_cr']

                if gl_tax_rev != expected_tax_rev:
                    item_discrepancies.append('INCORRECT_VAT_REVERSAL')
                    discrepancies.append({
                        'type': 'INCORRECT_VAT_REVERSAL',
                        'severity': 'HIGH',
                        'document_type': 'PURCHASE_RETURN',
                        'document_number': pret.return_number,
                        'document_date': str(pret.return_date),
                        'message': f"Purchase Return {pret.return_number} Input VAT Reversal mismatch: Expected Rs. {expected_tax_rev:,.2f} vs GL Voucher {primary_je.voucher_number} Reversal Rs. {gl_tax_rev:,.2f}.",
                        'expected': expected_tax_rev,
                        'actual': gl_tax_rev,
                        'difference': abs(expected_tax_rev - gl_tax_rev),
                    })

                if primary_je.entry_date != pret.return_date:
                    item_discrepancies.append('DATE_MISMATCH')
                    discrepancies.append({
                        'type': 'DATE_MISMATCH',
                        'severity': 'MEDIUM',
                        'document_type': 'PURCHASE_RETURN',
                        'document_number': pret.return_number,
                        'document_date': str(pret.return_date),
                        'message': f"Purchase Return {pret.return_number} date ({pret.return_date}) differs from GL Voucher {primary_je.voucher_number} entry date ({primary_je.entry_date}).",
                        'expected': str(pret.return_date),
                        'actual': str(primary_je.entry_date),
                        'difference': None,
                    })

                if pret.branch_id and primary_je.branch_id and primary_je.branch_id != pret.branch_id:
                    item_discrepancies.append('BRANCH_MISMATCH')
                    discrepancies.append({
                        'type': 'BRANCH_MISMATCH',
                        'severity': 'MEDIUM',
                        'document_type': 'PURCHASE_RETURN',
                        'document_number': pret.return_number,
                        'document_date': str(pret.return_date),
                        'message': f"Purchase Return {pret.return_number} branch ({pret.branch.code}) differs from GL Voucher {primary_je.voucher_number} branch ({primary_je.branch.code if primary_je.branch else 'HQ'}).",
                        'expected': pret.branch.code,
                        'actual': primary_je.branch.code if primary_je.branch else 'HQ',
                        'difference': None,
                    })

                if primary_je.reference_document and primary_je.reference_document.strip().upper() != pret.return_number.strip().upper():
                    item_discrepancies.append('REFERENCE_MISMATCH')
                    discrepancies.append({
                        'type': 'REFERENCE_MISMATCH',
                        'severity': 'MEDIUM',
                        'document_type': 'PURCHASE_RETURN',
                        'document_number': pret.return_number,
                        'document_date': str(pret.return_date),
                        'message': f"Purchase Return {pret.return_number} reference document mismatch: Expected '{pret.return_number}' but GL Voucher {primary_je.voucher_number} reference is '{primary_je.reference_document}'.",
                        'expected': pret.return_number,
                        'actual': primary_je.reference_document,
                        'difference': None,
                    })

                if not item_discrepancies:
                    is_matched = True

            diff_amt = abs(expected_tax_rev - gl_tax_rev).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            reconciled_purchase_returns.append({
                'document_type': 'PURCHASE_RETURN',
                'document_id': pret.id,
                'document_number': pret.return_number,
                'original_bill_reference': pret.original_bill_reference or (pret.original_grn.grn_number if pret.original_grn else ''),
                'document_date': pret.return_date,
                'document_date_bs': pret.return_date_bs or '',
                'party_name': pret.supplier.company_name,
                'expected_vat': expected_tax_rev,
                'gl_vat': gl_tax_rev,
                'difference': diff_amt,
                'is_matched': is_matched,
                'status': 'MATCHED' if is_matched else 'DISCREPANCY',
                'debit_note_number': primary_je.voucher_number if primary_je else None,
                'gl_voucher_number': primary_je.voucher_number if primary_je else None,
                'gl_voucher_id': primary_je.id if primary_je else None,
                'gl_entry_date': primary_je.entry_date if primary_je else None,
                'discrepancy_reasons': item_discrepancies,
            })

        # -------------------------------------------------------------
        # 7. ISOLATE ORPHAN / MANUAL UNLINKED GL POSTINGS
        # -------------------------------------------------------------
        orphan_gl_entries = []
        for je_id, je in je_cache.items():
            if je.entry_date >= start_date and je.entry_date <= end_date:
                data = je_vat_data.get(je_id, {})
                if data.get('has_vat_line'):
                    if je_id not in claimed_je_ids:
                        output_vat_impact = data['output_vat_cr']
                        input_vat_impact = data['input_vat_dr']

                        entry_info = {
                            'id': je.id,
                            'voucher_number': je.voucher_number,
                            'voucher_type': je.voucher_type,
                            'voucher_type_display': je.get_voucher_type_display(),
                            'entry_date': str(je.entry_date),
                            'entry_date_bs': je.entry_date_bs or '',
                            'branch_name': je.branch.name if je.branch else 'HQ / Consolidated',
                            'branch_code': je.branch.code if je.branch else 'HQ',
                            'source_module': getattr(je, 'source_module', 'MANUAL'),
                            'source_id': getattr(je, 'source_id', ''),
                            'reference_document': je.reference_document or '',
                            'narration': je.narration or '',
                            'output_vat_impact': output_vat_impact,
                            'input_vat_impact': input_vat_impact,
                            'is_manual': getattr(je, 'source_module', 'MANUAL') == 'MANUAL',
                        }
                        orphan_gl_entries.append(entry_info)

                        discrepancies.append({
                            'type': 'ORPHAN_GL_ENTRY',
                            'severity': 'HIGH',
                            'document_type': 'MANUAL_OR_ORPHAN_GL',
                            'document_number': je.voucher_number,
                            'document_date': str(je.entry_date),
                            'message': f"GL Voucher {je.voucher_number} affects VAT accounts (Output: Rs. {output_vat_impact:,.2f}, Input: Rs. {input_vat_impact:,.2f}) but has NO matching source invoice or GRN in the VAT registers.",
                            'expected': Decimal('0.00'),
                            'actual': (output_vat_impact if output_vat_impact != Decimal('0.00') else input_vat_impact),
                            'difference': abs(output_vat_impact if output_vat_impact != Decimal('0.00') else input_vat_impact),
                        })

        total_tx_checked = len(reconciled_sales) + len(reconciled_sales_returns) + len(reconciled_grns) + len(reconciled_purchase_returns)
        total_matched = (
            sum(1 for x in reconciled_sales if x['is_matched']) +
            sum(1 for x in reconciled_sales_returns if x['is_matched']) +
            sum(1 for x in reconciled_grns if x['is_matched']) +
            sum(1 for x in reconciled_purchase_returns if x['is_matched'])
        )
        total_unmatched = total_tx_checked - total_matched

        return {
            'summary': {
                'total_transactions_checked': total_tx_checked,
                'total_matched': total_matched,
                'total_unmatched': total_unmatched,
                'discrepancies_count': len(discrepancies),
                'sales_invoices': {
                    'total': len(reconciled_sales),
                    'matched': sum(1 for x in reconciled_sales if x['is_matched']),
                    'unmatched': sum(1 for x in reconciled_sales if not x['is_matched']),
                },
                'sales_returns': {
                    'total': len(reconciled_sales_returns),
                    'matched': sum(1 for x in reconciled_sales_returns if x['is_matched']),
                    'unmatched': sum(1 for x in reconciled_sales_returns if not x['is_matched']),
                },
                'grn_purchases': {
                    'total': len(reconciled_grns),
                    'matched': sum(1 for x in reconciled_grns if x['is_matched']),
                    'unmatched': sum(1 for x in reconciled_grns if not x['is_matched']),
                },
                'purchase_returns': {
                    'total': len(reconciled_purchase_returns),
                    'matched': sum(1 for x in reconciled_purchase_returns if x['is_matched']),
                    'unmatched': sum(1 for x in reconciled_purchase_returns if not x['is_matched']),
                },
                'orphan_gl_entries_count': len(orphan_gl_entries),
            },
            'sales_invoices': reconciled_sales,
            'sales_returns': reconciled_sales_returns,
            'grn_purchases': reconciled_grns,
            'purchase_returns': reconciled_purchase_returns,
            'orphan_gl_entries': orphan_gl_entries,
            'discrepancies': discrepancies,
        }

    # =========================================================================
    # INTERNAL GL JOURNAL HELPER
    # =========================================================================
    @staticmethod
    def _fetch_gl_journal_map(doc_numbers: List[str], voucher_type: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        """
        Fetches posted General Ledger journal vouchers matching the given document numbers.
        Returns a dictionary: {document_number: {'voucher_number': ..., 'id': ..., 'total_debit': ...}}
        """
        if not doc_numbers:
            return {}

        clean_docs = [str(d).strip() for d in doc_numbers if d and str(d).strip()]
        if not clean_docs:
            return {}

        qs = JournalEntry.objects.filter(
            reference_document__in=clean_docs,
            status='POSTED'
        ).only('id', 'voucher_number', 'voucher_type', 'reference_document', 'entry_date', 'fiscal_year', 'total_debit', 'total_credit', 'narration')

        if voucher_type:
            qs = qs.filter(voucher_type=voucher_type)

        gl_map = {}
        for je in qs:
            gl_map[je.reference_document] = {
                'id': je.id,
                'voucher_number': je.voucher_number,
                'voucher_type': je.voucher_type,
                'voucher_type_display': je.get_voucher_type_display(),
                'entry_date': je.entry_date,
                'fiscal_year': je.fiscal_year,
                'total_debit': je.total_debit,
                'total_credit': je.total_credit,
                'narration': je.narration or '',
            }

        return gl_map