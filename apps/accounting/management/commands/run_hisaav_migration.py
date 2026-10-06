"""
Legacy Hisaav Data Migration & Reconciliation Pipeline Command.

Executes legacy migration repair and reconciliation steps:
- Step 1: Purchase Bill Cancellations (deducts physical BranchStock and archives batches).
- Step 2: Inward Purchase Returns / Debit Notes (mapped to true schema fields).
- Step 3: Customer Sales Returns / Credit Notes (includes required foreign keys and line items).
- Step 4: GL Double-Entry Parity Audit & Fiscal Year Lock Enforcement.

SECURITY NOTICE:
This command includes an intentional production safety interlock. Because Hisaav data has
already been migrated to production, running live writes requires the explicit flag:
    --confirm-live-run
"""

import os
import openpyxl
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Sum, Q
from django.utils import timezone
from django.contrib.auth import get_user_model

from apps.accounting.models import (
    JournalEntry,
    JournalItem,
    Account,
    AccountingFiscalYear,
    FinancialPeriod
)
from apps.purchases.models import (
    GoodsReceivedNote,
    GRNItem,
    PurchaseReturn,
    PurchaseReturnItem,
    Supplier
)
from apps.sales.models import (
    SalesEstimate,
    SalesEstimateItem,
    SalesReturn,
    SalesReturnItem,
    Customer
)
from apps.inventory.models import (
    Product,
    ProductCategory,
    UnitOfMeasurement,
    BranchStock,
    StockMovementLog,
    ProductBatch,
    ItemInstance
)
from apps.branches.models import Branch
from apps.core.nepali_calendar import NepaliCalendar
from apps.core.utils.nepali_date_converter import parse_bs_date_components


class Command(BaseCommand):
    help = "Secured production pipeline for Hisaav Legacy Data Migration & Reconciliation."

    def add_arguments(self, parser):
        parser.add_argument(
            '--data-dir',
            type=str,
            default=r"C:\Users\Vivek Mani Upadhyaya\Downloads\Mobile soft Data",
            help="Path to folder containing legacy Hisaav Excel files."
        )
        parser.add_argument(
            '--step',
            type=int,
            choices=[1, 2, 3, 4],
            default=None,
            help="Execute a specific step (1=Cancellations, 2=Purchase Returns, 3=Sales Returns, 4=Reconcile & Lock). Default: all."
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help="Simulate execution in an atomic transaction without committing changes."
        )
        parser.add_argument(
            '--confirm-live-run',
            action='store_true',
            help="Explicit security confirmation required to commit legacy migration changes to the live database."
        )

    def handle(self, *args, **options):
        data_dir = options['data_dir']
        step_filter = options['step']
        dry_run = options['dry_run']
        confirm_live = options['confirm_live_run']

        self.stdout.write(self.style.SUCCESS("=" * 85))
        self.stdout.write(self.style.SUCCESS("  HISAAV LEGACY MIGRATION & AUDIT PIPELINE (SECURED)"))
        self.stdout.write(self.style.SUCCESS(f"  Data Directory : {data_dir}"))
        self.stdout.write(self.style.SUCCESS(f"  Execution Mode : {'DRY-RUN (SIMULATION)' if dry_run else 'LIVE COMMIT REQUESTED'}"))
        self.stdout.write(self.style.SUCCESS("=" * 85))

        # ---------------------------------------------------------------------
        # PRODUCTION SAFETY INTERLOCK
        # ---------------------------------------------------------------------
        if not dry_run and not confirm_live:
            self.stdout.write(self.style.ERROR(
                "\n[SAFETY LOCKOUT ACTIVE]\n"
                "Hisaav legacy data has already been migrated into GrowERP.\n"
                "Executing this migration command on a live database can modify stock counters,\n"
                "recalculate supplier balances, or duplicate accounting records.\n\n"
                "• To simulate without saving changes:  python manage.py run_hisaav_migration --dry-run\n"
                "• To genuinely execute live database writes: python manage.py run_hisaav_migration --confirm-live-run\n"
            ))
            return

        if not os.path.exists(data_dir):
            raise CommandError(f"Data directory does not exist: {data_dir}")

        branch = Branch.objects.filter(code='BR-MAIN-01').first() or Branch.objects.first()
        if not branch:
            raise CommandError("No Branch found in the database. Ensure basic branch setup exists.")

        User = get_user_model()
        admin_user = User.objects.filter(is_superuser=True).first() or User.objects.first()
        if not admin_user:
            raise CommandError("No active User found in database to assign as system auditor.")

        try:
            with transaction.atomic():
                # Step 1: Purchase Cancellations & Physical Stock Deductions
                if step_filter is None or step_filter == 1:
                    self.run_step_1_cancellations(data_dir, branch)

                # Step 2: Purchase Returns (Debit Notes)
                if step_filter is None or step_filter == 2:
                    self.run_step_2_purchase_returns(data_dir, branch, admin_user)

                # Step 3: Sales Returns (Credit Notes)
                if step_filter is None or step_filter == 3:
                    self.run_step_3_sales_returns(data_dir, branch, admin_user)

                # Step 4: Parity Audit & Fiscal Year Lock Enforcement
                if step_filter is None or step_filter == 4:
                    self.run_step_4_reconciliation_and_locks(admin_user)

                if dry_run:
                    self.stdout.write(self.style.WARNING("\n[DRY-RUN] Simulation completed successfully. Rolling back all database writes."))
                    transaction.set_rollback(True)
                else:
                    self.stdout.write(self.style.SUCCESS("\n[SUCCESS] Pipeline executed and committed to database successfully."))

        except Exception as e:
            self.stderr.write(self.style.ERROR(f"\n[FATAL ERROR] Pipeline aborted: {str(e)}"))
            raise

    # -------------------------------------------------------------------------
    # STEP 1: PURCHASE CANCELLATIONS & STOCK DEDUCTION
    # -------------------------------------------------------------------------
    def run_step_1_cancellations(self, data_dir, branch):
        self.stdout.write("\n" + "-" * 75)
        self.stdout.write(self.style.MIGRATE_HEADING("STEP 1: Processing Purchase Cancellations (Bill Cancel Report.xlsx)"))
        self.stdout.write("-" * 75)

        file_path = os.path.join(data_dir, "Bill Cancel Report.xlsx")
        if not os.path.exists(file_path):
            self.stdout.write(self.style.WARNING(f"File not found: {file_path}. Skipping Step 1."))
            return

        wb = openpyxl.load_workbook(file_path, data_only=True)
        ws = wb.active

        cancelled_bills = set()
        for r in range(8, ws.max_row + 1):
            bill_no = str(ws.cell(r, 2).value or '').strip()
            if bill_no and bill_no not in ['None', '', '-']:
                cancelled_bills.add(bill_no)

        self.stdout.write(f"Found {len(cancelled_bills)} cancellation entries in Excel.")

        cancelled_count = 0
        reversed_amount = Decimal('0.00')

        for bill_no in cancelled_bills:
            grns = GoodsReceivedNote.objects.filter(
                Q(supplier_bill_no=bill_no) | Q(grn_number=bill_no)
            ).select_related('supplier', 'branch')

            for grn in grns:
                if grn.status != 'CANCELLED':
                    grn.status = 'CANCELLED'
                    grn.due_amount = Decimal('0.00')
                    grn.save(update_fields=['status', 'due_amount', 'updated_at'])

                    # 1. Reverse physical stock counters for each item on this GRN
                    for item in grn.items.select_related('product').all():
                        qty = item.base_unit_quantity or item.purchased_quantity or Decimal('1.000')
                        if item.product and qty > Decimal('0.000'):
                            # Deduct from physical shelf stock counter
                            bs, _ = BranchStock.objects.get_or_create(
                                branch=grn.branch,
                                product=item.product,
                                defaults={'quantity': Decimal('0.000')}
                            )
                            bs.quantity -= qty
                            bs.save(update_fields=['quantity', 'updated_at'])

                            # Record audit log
                            StockMovementLog.objects.create(
                                product=item.product,
                                branch=grn.branch,
                                movement_type='ADJUSTMENT_SUB',
                                quantity_delta=-qty,
                                reference_document=f"VOID-{grn.grn_number}",
                                remarks=f"Migration stock reversal for cancelled purchase bill #{bill_no}"
                            )

                    # 2. Deplete any active FIFO product batches for this GRN
                    ProductBatch.objects.filter(
                        grn_reference=grn.grn_number,
                        branch=grn.branch
                    ).update(
                        quantity_remaining=Decimal('0.000'),
                        is_depleted=True,
                        updated_at=timezone.now()
                    )

                    # 3. Archive any unsold handset serial instances from this GRN
                    ItemInstance.objects.filter(
                        purchase_reference=grn.grn_number,
                        status='IN_STOCK'
                    ).update(
                        status='ARCHIVED',
                        updated_at=timezone.now()
                    )

                    # 4. Cancel associated journal entries
                    jes = JournalEntry.objects.filter(reference_document=grn.grn_number, status='POSTED')
                    for je in jes:
                        je.status = 'CANCELLED'
                        je.narration = f"[MIGRATION VOID] {je.narration}"
                        je.save(update_fields=['status', 'narration', 'updated_at'])

                    # 5. Re-reconcile supplier ledger balance
                    if grn.supplier:
                        grn.supplier.recalculate_balance_from_ledger(save=True)

                    cancelled_count += 1
                    reversed_amount += (grn.gross_amount or Decimal('0.00'))

        self.stdout.write(self.style.SUCCESS(
            f"Step 1 Complete: {cancelled_count} GRNs cancelled with physical stock deducted. "
            f"Gross Reversed: Rs. {reversed_amount:,.2f}"
        ))

    # -------------------------------------------------------------------------
    # STEP 2: PURCHASE RETURNS / DEBIT NOTES
    # -------------------------------------------------------------------------
    def run_step_2_purchase_returns(self, data_dir, branch, admin_user):
        self.stdout.write("\n" + "-" * 75)
        self.stdout.write(self.style.MIGRATE_HEADING("STEP 2: Processing Purchase Returns / Debit Notes"))
        self.stdout.write("-" * 75)

        file_path = os.path.join(data_dir, "Purchase Return Report9_18_2026.xlsx")
        if not os.path.exists(file_path):
            self.stdout.write(self.style.WARNING(f"File not found: {file_path}. Skipping Step 2."))
            return

        wb = openpyxl.load_workbook(file_path, data_only=True)
        ws = wb.active

        cash_account = (
            Account.objects.filter(Q(code='1110') | Q(code__startswith='1110-') | Q(system_tag='CASH'))
            .filter(Q(branch=branch) | Q(branch__isnull=True))
            .first()
        )
        inventory_account = (
            Account.objects.filter(Q(code='1310') | Q(code__startswith='1310-') | Q(system_tag='INVENTORY_ASSET'))
            .filter(Q(branch=branch) | Q(branch__isnull=True))
            .first()
        )

        category = ProductCategory.objects.first()
        base_unit = UnitOfMeasurement.objects.filter(code='PCS').first() or UnitOfMeasurement.objects.first()
        historical_product, _ = Product.objects.get_or_create(
            name="Mobile (Historical)",
            defaults={
                'sku': 'HIST-MOB-001',
                'is_active': True,
                'category': category,
                'base_unit': base_unit,
                'purchase_price': Decimal('0.00'),
                'selling_price': Decimal('0.00')
            }
        )

        return_rows = []
        for r in range(11, ws.max_row + 1):
            date_bs = str(ws.cell(r, 1).value or '').strip()
            sup_name = str(ws.cell(r, 4).value or '').strip()
            pan = str(ws.cell(r, 5).value or '').strip()
            gross_val = ws.cell(r, 9).value
            taxable_val = ws.cell(r, 11).value
            vat_val = ws.cell(r, 12).value

            if gross_val and sup_name and date_bs not in ['None', '', '-']:
                return_rows.append({
                    'date_bs': date_bs,
                    'supplier_name': sup_name,
                    'pan': pan,
                    'gross': Decimal(str(gross_val)),
                    'taxable': Decimal(str(taxable_val or gross_val)),
                    'vat': Decimal(str(vat_val or '0.00'))
                })

        created_dn_count = 0
        total_dn_amount = Decimal('0.00')

        for idx, item in enumerate(return_rows, start=1):
            dn_number = f"DN-BR-MAIN-01-{idx:06d}"
            pr_number = f"PR-BR-MAIN-01-{idx:06d}"

            supplier = (
                Supplier.objects.filter(pan_number=item['pan']).first()
                if item['pan'] else None
            ) or Supplier.objects.filter(company_name__icontains=item['supplier_name']).first()

            if not supplier:
                supplier = Supplier.objects.create(
                    company_name=item['supplier_name'],
                    pan_number=item['pan'] or None,
                    is_active=True
                )
            elif item['pan'] and not supplier.pan_number:
                supplier.pan_number = item['pan']
                supplier.save(update_fields=['pan_number'])

            # Convert B.S. Date to Gregorian A.D. Date
            parsed_ad_date = timezone.now().date()
            if item['date_bs']:
                try:
                    bs_y, bs_m, bs_d = parse_bs_date_components(item['date_bs'])
                    parsed_ad_date = NepaliCalendar.bs_to_ad(bs_y, bs_m, bs_d)
                except Exception:
                    parsed_ad_date = timezone.now().date()

            pr, created_pr = PurchaseReturn.objects.get_or_create(
                return_number=pr_number,
                defaults={
                    'branch': branch,
                    'supplier': supplier,
                    'status': 'CONFIRMED',
                    'refund_mode': 'CASH_REFUND',
                    'return_date': parsed_ad_date,
                    'return_date_bs': item['date_bs'],
                    'fiscal_year': '2083/84',
                    'total_return_amount': item['taxable'],
                    'tax_amount': item['vat'],
                    'net_refund_amount': item['gross'],
                    'processed_by': admin_user,
                    'remarks': f"Hisaav Purchase Return - {item['date_bs']}"
                }
            )

            if created_pr:
                tax_rate = (
                    (item['vat'] / item['taxable'] * Decimal('100.00')).quantize(Decimal('0.01'))
                    if item['taxable'] > Decimal('0.00') else Decimal('0.00')
                )
                PurchaseReturnItem.objects.get_or_create(
                    purchase_return=pr,
                    product=historical_product,
                    defaults={
                        'returned_quantity': Decimal('1.000'),
                        'conversion_factor': Decimal('1.000'),
                        'base_unit_quantity': Decimal('1.000'),
                        'purchase_rate': item['taxable'],
                        'tax_rate': tax_rate,
                        'tax_amount': item['vat'],
                        'line_total': item['gross'],
                        'return_reason': f"Hisaav Purchase Return - {item['date_bs']}"
                    }
                )

            # Debit Note Accounting Journal Voucher
            je, created_je = JournalEntry.objects.get_or_create(
                voucher_number=dn_number,
                defaults={
                    'voucher_type': 'DEBIT_NOTE',
                    'branch': branch,
                    'reference_document': pr_number,
                    'narration': f"Debit Note for Purchase Return {pr_number} - {item['supplier_name']}",
                    'entry_date': parsed_ad_date,
                    'entry_date_bs': item['date_bs'],
                    'fiscal_year': '2083/84',
                    'total_debit': item['gross'],
                    'total_credit': item['gross'],
                    'status': 'POSTED',
                    'created_by': admin_user,
                    'posted_by': admin_user,
                    'posted_at': timezone.now()
                }
            )

            if created_je:
                if cash_account and inventory_account:
                    JournalItem.objects.create(
                        journal_entry=je,
                        account=cash_account,
                        debit_amount=item['gross'],
                        credit_amount=Decimal('0.00'),
                        supplier=supplier,
                        line_narration=f"Cash refund from supplier {item['supplier_name']}"
                    )
                    JournalItem.objects.create(
                        journal_entry=je,
                        account=inventory_account,
                        debit_amount=Decimal('0.00'),
                        credit_amount=item['gross'],
                        supplier=supplier,
                        line_narration="Inventory reversal for purchase return"
                    )
                created_dn_count += 1
                total_dn_amount += item['gross']

        self.stdout.write(self.style.SUCCESS(
            f"Step 2 Complete: {created_dn_count} Debit Notes mapped and created. "
            f"Gross Reversal: Rs. {total_dn_amount:,.2f}"
        ))

    # -------------------------------------------------------------------------
    # STEP 3: SALES RETURNS / CREDIT NOTES
    # -------------------------------------------------------------------------
    def run_step_3_sales_returns(self, data_dir, branch, admin_user):
        self.stdout.write("\n" + "-" * 75)
        self.stdout.write(self.style.MIGRATE_HEADING("STEP 3: Processing Sales Returns / Credit Notes"))
        self.stdout.write("-" * 75)

        sr_number = "SR-BR-MAIN-01-000001"
        cn_number = "CN-BR-MAIN-01-000001"

        if SalesReturn.objects.filter(return_number=sr_number).exists():
            self.stdout.write(self.style.WARNING("Step 3 Sales Return already exists. Skipping duplicate."))
            return

        taxable = Decimal('156637.17')
        vat = Decimal('20362.83')
        gross = Decimal('177000.00')

        customer = Customer.objects.filter(pan_number='304427312').first() or Customer.objects.first()
        if not customer:
            customer = Customer.objects.create(
                name="Walk-in Customer (Legacy)",
                phone_number="9800000000",
                pan_number="304427312",
                is_active=True
            )

        product = Product.objects.filter(name__icontains='I Phone 16 Pro 256').first() or Product.objects.first()

        # Locate or initialize parent estimate
        estimate = SalesEstimate.objects.filter(grand_total=gross).first() or SalesEstimate.objects.first()
        if not estimate:
            estimate = SalesEstimate.objects.create(
                estimate_number="EST-MIG-HIST-001",
                branch=branch,
                customer=customer,
                cashier=admin_user,
                bill_date_ad=timezone.now().date(),
                bill_date_bs="2083-05-18",
                fiscal_year="2083/84",
                subtotal=taxable,
                taxable_amount=taxable,
                vat_amount=vat,
                grand_total=gross,
                paid_amount=gross,
                status='COMPLETED'
            )

        estimate_item = estimate.items.filter(product=product).first() or estimate.items.first()
        if not estimate_item:
            estimate_item = SalesEstimateItem.objects.create(
                estimate=estimate,
                product=product,
                quantity=Decimal('1.000'),
                conversion_factor=Decimal('1.000'),
                base_unit_quantity=Decimal('1.000'),
                unit_price=taxable,
                official_unit_price=taxable,
                line_total=gross,
                tax_amount=vat
            )

        sr = SalesReturn.objects.create(
            branch=branch,
            customer=customer,
            return_number=sr_number,
            original_estimate=estimate,
            return_date_ad=timezone.now().date(),
            return_date_bs="2083-05-18",
            fiscal_year="2083/84",
            refund_mode='CASH',
            total_refund_amount=gross,
            reason="Customer sales return: I Phone 16 Pro 256",
            processed_by=admin_user
        )

        SalesReturnItem.objects.create(
            sales_return=sr,
            estimate_item=estimate_item,
            product=product,
            return_quantity=Decimal('1.000'),
            base_unit_quantity=Decimal('1.000'),
            refund_amount=gross,
            restock_to_inventory=True,
            is_defective=False,
            defect_reason="Customer sales return: I Phone 16 Pro 256"
        )

        # Update physical stock counter
        bs, _ = BranchStock.objects.get_or_create(
            branch=branch,
            product=product,
            defaults={'quantity': Decimal('0.000')}
        )
        bs.quantity += Decimal('1.000')
        bs.save(update_fields=['quantity', 'updated_at'])

        StockMovementLog.objects.create(
            movement_type='SALE_RETURN',
            branch=branch,
            product=product,
            quantity_delta=Decimal('1.000'),
            reference_document=sr_number,
            remarks="Restocked 1 unit via Sales Return SR-BR-MAIN-01-000001"
        )

        sales_acc = (
            Account.objects.filter(Q(code='4110') | Q(code__startswith='4110-') | Q(system_tag='SALES_REVENUE'))
            .filter(Q(branch=branch) | Q(branch__isnull=True))
            .first()
        ) or Account.objects.filter(group__category='REVENUE').first()

        vat_acc = (
            Account.objects.filter(Q(code='2210') | Q(code__startswith='2210-') | Q(system_tag='OUTPUT_VAT'))
            .filter(Q(branch=branch) | Q(branch__isnull=True))
            .first()
        ) or Account.objects.filter(name__icontains='Output VAT').first()

        cash_acc = (
            Account.objects.filter(Q(code='1110') | Q(code__startswith='1110-') | Q(system_tag='CASH'))
            .filter(Q(branch=branch) | Q(branch__isnull=True))
            .first()
        ) or Account.objects.filter(group__category='ASSET', name__icontains='Cash').first()

        je = JournalEntry.objects.create(
            voucher_number=cn_number,
            voucher_type='CREDIT_NOTE',
            branch=branch,
            reference_document=sr_number,
            narration=f"Credit Note for Sales Return {sr_number}",
            entry_date=timezone.now().date(),
            entry_date_bs="2083-05-18",
            fiscal_year="2083/84",
            total_debit=gross,
            total_credit=gross,
            status='POSTED',
            created_by=admin_user,
            posted_by=admin_user,
            posted_at=timezone.now()
        )

        if sales_acc and vat_acc and cash_acc:
            JournalItem.objects.create(
                journal_entry=je,
                account=sales_acc,
                debit_amount=taxable,
                credit_amount=Decimal('0.00'),
                customer=customer,
                line_narration="Sales revenue reversal on customer return"
            )
            JournalItem.objects.create(
                journal_entry=je,
                account=vat_acc,
                debit_amount=vat,
                credit_amount=Decimal('0.00'),
                customer=customer,
                line_narration="Output VAT 13% reversal on customer return"
            )
            JournalItem.objects.create(
                journal_entry=je,
                account=cash_acc,
                debit_amount=Decimal('0.00'),
                credit_amount=gross,
                customer=customer,
                line_narration="Cash refund paid to customer"
            )

        self.stdout.write(self.style.SUCCESS(
            f"Step 3 Complete: Credit Note {cn_number} posted and 1 unit restocked. Gross Refund: Rs. {gross:,.2f}"
        ))

    # -------------------------------------------------------------------------
    # STEP 4: PARITY AUDIT & FISCAL LOCK ENFORCEMENT
    # -------------------------------------------------------------------------
    def run_step_4_reconciliation_and_locks(self, admin_user):
        self.stdout.write("\n" + "-" * 75)
        self.stdout.write(self.style.MIGRATE_HEADING("STEP 4: Parity Audit & Fiscal Lock Enforcement"))
        self.stdout.write("-" * 75)

        posted_items = JournalItem.objects.filter(journal_entry__status='POSTED')
        totals = posted_items.aggregate(total_dr=Sum('debit_amount'), total_cr=Sum('credit_amount'))
        total_dr = totals['total_dr'] or Decimal('0.00')
        total_cr = totals['total_cr'] or Decimal('0.00')
        diff = total_dr - total_cr

        self.stdout.write(f"GL Total Debits : Rs. {total_dr:,.2f}")
        self.stdout.write(f"GL Total Credits: Rs. {total_cr:,.2f}")
        self.stdout.write(f"Internal GL Diff: Rs. {diff:,.2f}")

        if diff != Decimal('0.00'):
            raise CommandError(f"CRITICAL GL IMBALANCE: Dr - Cr delta is Rs. {diff:,.2f}!")

        # Re-close historical fiscal years and their financial monthly periods
        for fy_name in ['2080/81', '2081/82', '2082/83']:
            AccountingFiscalYear.lock_year(fy_name, user=admin_user)
            self.stdout.write(f"Enforced Lock: FY {fy_name} -> is_closed=True")

        # Keep current active fiscal year open
        AccountingFiscalYear.unlock_year('2083/84')
        self.stdout.write("Enforced Active: FY 2083/84 -> is_closed=False")

        self.stdout.write(self.style.SUCCESS("Step 4 Complete: Parity verified (0.00 variance) and historical fiscal years locked."))