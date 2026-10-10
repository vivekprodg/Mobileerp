"""
Historical Sales Return VAT Snapshot Repair & Backfill Command.

Path:
D:\Mobile Shop\Inventory\apps\sales\management\commands\backfill_sales_return_vat_snapshots.py

Capabilities:
1. Scans all historical SalesReturn and SalesReturnItem records in the database.
2. Identifies items with missing, zero, or drifted VAT snapshots:
   - Items where original sales invoice line had 13% VAT, but return line has 0.00 VAT reversal.
   - Items where taxable, non-taxable, or VAT reversal figures do not sum to refund_amount.
   - Parent SalesReturn headers whose totals disagree with the sum of their return items.
3. Reconstructs correct values from the original invoice item's stored VAT figures
   (base_taxable_amount, tax_amount, line_total, vat_rate) and returned quantities.
4. Accurately handles multiple partial returns in chronological order with exact
   penny rounding and residual allocation.
5. Updates child line-item fields:
   - vat_rate
   - taxable_return_amount
   - non_taxable_return_amount
   - vat_reversal_amount
6. Recalculates parent SalesReturn header totals:
   - taxable_amount
   - non_taxable_amount
   - vat_amount
   - total_refund_amount
7. Invariant Safety:
   - Does NOT change refund_amount, return_quantity, inventory counters, return dates,
     original invoices, or posted general ledger vouchers.
8. Includes --dry-run (default) and --commit modes, reporting detailed audit metrics.

Usage:
    python manage.py backfill_sales_return_vat_snapshots
    python manage.py backfill_sales_return_vat_snapshots --commit
    python manage.py backfill_sales_return_vat_snapshots --voucher=RET-00000001 --commit
    python manage.py backfill_sales_return_vat_snapshots --branch=BR-MAIN-01
"""

from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Any, List, Optional
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Sum, Q
from django.utils import timezone

from apps.sales.models import SalesReturn, SalesReturnItem, SalesEstimateItem
from apps.branches.models import Branch


class Command(BaseCommand):
    help = (
        "Audits and repairs historical Sales Return VAT snapshots and header totals "
        "using original invoice item stored figures without altering refund amounts, stock, or GL entries."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--commit',
            action='store_true',
            help='Genuinely applies and commits the corrected VAT snapshots to the database. Defaults to dry-run.'
        )
        parser.add_argument(
            '--voucher',
            type=str,
            default='',
            help='Filter audit/repair to a specific return voucher number (e.g. RET-00000001).'
        )
        parser.add_argument(
            '--branch',
            type=str,
            default='',
            help='Filter audit/repair by branch code (e.g. BR-MAIN-01).'
        )

    def handle(self, *args, **options):
        commit_mode = options['commit']
        voucher_filter = options['voucher'].strip()
        branch_filter = options['branch'].strip()

        self.stdout.write(self.style.MIGRATE_HEADING("=" * 90))
        self.stdout.write(self.style.MIGRATE_HEADING("  SALES RETURN VAT SNAPSHOT REPAIR & RECONCILIATION AUDIT"))
        self.stdout.write(self.style.MIGRATE_HEADING("=" * 90))

        if commit_mode:
            self.stdout.write(self.style.WARNING(">>> LIVE COMMIT MODE: Corrected VAT snapshots will be written to DB. <<<\n"))
        else:
            self.stdout.write(self.style.NOTICE(">>> PREVIEW ONLY (DRY-RUN): Pass --commit to write changes to DB. <<<\n"))

        # Base QuerySet
        returns_qs = SalesReturn.objects.select_related(
            'original_estimate', 'branch', 'customer'
        ).prefetch_related(
            'items__estimate_item', 'items__product'
        ).order_by('return_date_ad', 'created_at', 'id')

        if voucher_filter:
            returns_qs = returns_qs.filter(return_number__iexact=voucher_filter)
            self.stdout.write(f"Filter Voucher: {voucher_filter}\n")

        if branch_filter:
            branch_obj = Branch.objects.filter(code__iexact=branch_filter).first()
            if not branch_obj:
                self.stdout.write(self.style.ERROR(f"Branch code '{branch_filter}' not found."))
                return
            returns_qs = returns_qs.filter(branch=branch_obj)
            self.stdout.write(f"Filter Branch: {branch_obj.name} ({branch_obj.code})\n")

        total_returns_checked = 0
        total_items_checked = 0
        total_items_repaired = 0
        total_headers_repaired = 0
        total_skipped = 0
        total_vat_discrepancy_repaired = Decimal('0.00')
        unrepairable_records: List[Dict[str, Any]] = []

        # Track historical cumulative returns grouped by original estimate_item to handle partial returns chronologically
        cumulative_item_returns = defaultdict(lambda: {
            'qty_returned': Decimal('0.000'),
            'taxable_reversed': Decimal('0.00'),
            'vat_reversed': Decimal('0.00'),
            'non_taxable_reversed': Decimal('0.00'),
            'refund_reversed': Decimal('0.00'),
        })

        self.stdout.write(
            f"  {'Voucher No':<16} | {'Item Product':<26} | {'Refund (NPR)':>13} | "
            f"{'Old VAT Rev':>12} | {'New VAT Rev':>12} | {'Status':^10}"
        )
        self.stdout.write("  " + "-" * 102)

        try:
            with transaction.atomic():
                for sales_return in returns_qs:
                    total_returns_checked += 1
                    header_needs_update = False
                    return_items = list(sales_return.items.select_related('estimate_item', 'product').order_by('id'))

                    new_header_taxable = Decimal('0.00')
                    new_header_non_taxable = Decimal('0.00')
                    new_header_vat = Decimal('0.00')
                    new_header_refund = Decimal('0.00')

                    for r_item in return_items:
                        total_items_checked += 1
                        est_item: Optional[SalesEstimateItem] = getattr(r_item, 'estimate_item', None)

                        if not est_item:
                            unrepairable_records.append({
                                'voucher': sales_return.return_number,
                                'item_id': r_item.id,
                                'reason': f"Missing linked estimate_item for product '{r_item.product.name if r_item.product else 'Unknown'}'"
                            })
                            total_skipped += 1
                            # Preserve whatever was on the item
                            new_header_taxable += (r_item.taxable_return_amount or Decimal('0.00'))
                            new_header_non_taxable += (r_item.non_taxable_return_amount or Decimal('0.00'))
                            new_header_vat += (r_item.vat_reversal_amount or Decimal('0.00'))
                            new_header_refund += (r_item.refund_amount or Decimal('0.00'))
                            continue

                        # -------------------------------------------------------------
                        # 1. READ ORIGINAL INVOICE ITEM STORED FIGURES
                        # -------------------------------------------------------------
                        orig_qty = est_item.quantity if (est_item.quantity and est_item.quantity > Decimal('0.000')) else Decimal('1.000')
                        return_qty = r_item.return_quantity if (r_item.return_quantity and r_item.return_quantity > Decimal('0.000')) else Decimal('1.000')
                        refund_val = (r_item.refund_amount or Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

                        orig_base_taxable = (est_item.base_taxable_amount or est_item.taxable_line_amount or Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                        orig_vat = (est_item.tax_amount or Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                        orig_total = (est_item.line_total or Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                        orig_vat_rate = (est_item.vat_rate or Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

                        # Determine whether the original sales line was subject to VAT
                        is_original_taxable = bool(
                            est_item.is_vat_applicable and
                            orig_vat_rate > Decimal('0.00') and
                            (orig_vat > Decimal('0.00') or orig_base_taxable > Decimal('0.00'))
                        )

                        # Defensive recovery for legacy invoices where VAT rate was saved but line tax wasn't snapshotted
                        if not is_original_taxable and est_item.is_vat_applicable and orig_vat_rate > Decimal('0.00') and orig_total > Decimal('0.00'):
                            divisor = Decimal('1.00') + (orig_vat_rate / Decimal('100.00'))
                            orig_base_taxable = (orig_total / divisor).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                            orig_vat = (orig_total - orig_base_taxable).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                            is_original_taxable = (orig_vat > Decimal('0.00'))

                        # Historical tracking on this estimate item across multiple returns
                        prior_state = cumulative_item_returns[est_item.id]
                        prior_qty = prior_state['qty_returned']
                        prior_taxable = prior_state['taxable_reversed']
                        prior_vat = prior_state['vat_reversed']
                        prior_non_taxable = prior_state['non_taxable_reversed']

                        is_final_return = bool(prior_qty + return_qty >= orig_qty)
                        ratio = min(Decimal('1.000000'), return_qty / orig_qty) if orig_qty > Decimal('0.000') else Decimal('1.000000')

                        # -------------------------------------------------------------
                        # 2. CALCULATE EXACT CORRECT REVERSAL VALUES
                        # -------------------------------------------------------------
                        if is_original_taxable:
                            correct_vat_rate = orig_vat_rate
                            rem_vat = max(Decimal('0.00'), orig_vat - prior_vat)

                            if is_final_return:
                                correct_vat_reversal = rem_vat
                            else:
                                correct_vat_reversal = min(rem_vat, (orig_vat * ratio).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))

                            # Taxable base reversed is the pre-tax portion of the refund: refund_amount - vat_reversal
                            correct_taxable_return = max(Decimal('0.00'), refund_val - correct_vat_reversal).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                            correct_non_taxable_return = Decimal('0.00')
                        else:
                            correct_vat_rate = Decimal('0.00')
                            correct_taxable_return = Decimal('0.00')
                            correct_vat_reversal = Decimal('0.00')
                            correct_non_taxable_return = refund_val

                        # Update cumulative state for subsequent partial returns of the same estimate item
                        prior_state['qty_returned'] += return_qty
                        prior_state['taxable_reversed'] += correct_taxable_return
                        prior_state['vat_reversed'] += correct_vat_reversal
                        prior_state['non_taxable_reversed'] += correct_non_taxable_return
                        prior_state['refund_reversed'] += refund_val

                        # Accumulate for parent header totals
                        new_header_taxable += correct_taxable_return
                        new_header_non_taxable += correct_non_taxable_return
                        new_header_vat += correct_vat_reversal
                        new_header_refund += refund_val

                        # -------------------------------------------------------------
                        # 3. COMPARE WITH EXISTING ITEM SNAPSHOTS
                        # -------------------------------------------------------------
                        current_vat_rate = (r_item.vat_rate or Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                        current_taxable = (r_item.taxable_return_amount or Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                        current_non_taxable = (r_item.non_taxable_return_amount or Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                        current_vat_reversal = (r_item.vat_reversal_amount or Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

                        needs_item_repair = (
                            current_vat_rate != correct_vat_rate or
                            current_taxable != correct_taxable_return or
                            current_non_taxable != correct_non_taxable_return or
                            current_vat_reversal != correct_vat_reversal
                        )

                        prod_title = r_item.product.name[:24] if r_item.product else 'Item'

                        if needs_item_repair:
                            total_items_repaired += 1
                            header_needs_update = True
                            vat_diff = abs(correct_vat_reversal - current_vat_reversal)
                            total_vat_discrepancy_repaired += vat_diff

                            status_lbl = "REPAIRED" if commit_mode else "DRIFT"
                            style_fn = self.style.SUCCESS if commit_mode else self.style.WARNING

                            self.stdout.write(style_fn(
                                f"  {sales_return.return_number:<16} | {prod_title:<26} | {refund_val:>13,.2f} | "
                                f"{current_vat_reversal:>12,.2f} | {correct_vat_reversal:>12,.2f} | {status_lbl:^10}"
                            ))

                            if commit_mode:
                                r_item.vat_rate = correct_vat_rate
                                r_item.taxable_return_amount = correct_taxable_return
                                r_item.non_taxable_return_amount = correct_non_taxable_return
                                r_item.vat_reversal_amount = correct_vat_reversal
                                r_item.save(update_fields=[
                                    'vat_rate', 'taxable_return_amount',
                                    'non_taxable_return_amount', 'vat_reversal_amount', 'updated_at'
                                ])

                    # -------------------------------------------------------------
                    # 4. RECONCILE PARENT SALES RETURN HEADER TOTALS
                    # -------------------------------------------------------------
                    curr_h_taxable = (sales_return.taxable_amount or Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                    curr_h_non_taxable = (sales_return.non_taxable_amount or Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                    curr_h_vat = (sales_return.vat_amount or Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                    curr_h_refund = (sales_return.total_refund_amount or Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

                    new_h_taxable = new_header_taxable.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                    new_h_non_taxable = new_header_non_taxable.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                    new_h_vat = new_header_vat.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                    new_h_refund = new_header_refund.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

                    header_drift = (
                        curr_h_taxable != new_h_taxable or
                        curr_h_non_taxable != new_h_non_taxable or
                        curr_h_vat != new_h_vat or
                        curr_h_refund != new_h_refund
                    )

                    if header_drift or header_needs_update:
                        total_headers_repaired += 1
                        if commit_mode:
                            sales_return.taxable_amount = new_h_taxable
                            sales_return.non_taxable_amount = new_h_non_taxable
                            sales_return.vat_amount = new_h_vat
                            sales_return.total_refund_amount = new_h_refund
                            sales_return.save(update_fields=[
                                'taxable_amount', 'non_taxable_amount',
                                'vat_amount', 'total_refund_amount', 'updated_at'
                            ])

                if not commit_mode:
                    transaction.set_rollback(True)

        except Exception as e:
            self.stderr.write(self.style.ERROR(f"\n[FATAL ERROR] Backfill aborted: {str(e)}"))
            raise

        # ---------------------------------------------------------------------
        # FINAL AUDIT SUMMARY REPORT
        # ---------------------------------------------------------------------
        self.stdout.write(self.style.MIGRATE_HEADING("\n" + "=" * 90))
        self.stdout.write(self.style.MIGRATE_HEADING("  SALES RETURN VAT BACKFILL AUDIT SUMMARY REPORT"))
        self.stdout.write(self.style.MIGRATE_HEADING("=" * 90))

        self.stdout.write(f"  • Return Vouchers Checked     : {total_returns_checked}")
        self.stdout.write(f"  • Return Items Inspected      : {total_items_checked}")
        self.stdout.write(f"  • Return Items Repaired       : {total_items_repaired}")
        self.stdout.write(f"  • Return Headers Synchronized : {total_headers_repaired}")
        self.stdout.write(f"  • Net VAT Discrepancy Adjusted: Rs. {total_vat_discrepancy_repaired:,.2f}")
        self.stdout.write(f"  • Records Skipped / Incomplete: {total_skipped}")

        if unrepairable_records:
            self.stdout.write(self.style.WARNING("\n[!] Unrepairable / Skipped Records:"))
            for item in unrepairable_records[:10]:
                self.stdout.write(self.style.WARNING(f"    - Voucher {item['voucher']} (Item #{item['item_id']}): {item['reason']}"))

        if total_items_repaired > 0 or total_headers_repaired > 0:
            if commit_mode:
                self.stdout.write(self.style.SUCCESS(
                    f"\n[+] Successfully repaired {total_items_repaired} return item(s) and synchronized "
                    f"{total_headers_repaired} return voucher header(s) in database!"
                ))
            else:
                self.stdout.write(self.style.WARNING(
                    f"\n[!] Detected {total_items_repaired} drifted return item(s) and {total_headers_repaired} "
                    f"desynchronized header(s). Run with '--commit' to persist corrections."
                ))
        else:
            self.stdout.write(self.style.SUCCESS(
                "\n[+] All Sales Return VAT snapshots and header totals match original invoice figures with 0.00 variance. Database healthy!"
            ))