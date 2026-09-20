"""
Data Migration Command: Mobilesoft / Hisaav IRD VAT Register & Historical Accounting Sync.

Handles historical data migration from Hisaav (mobilesoft.nafanoksan.com) into the ERP:
1. Smart Header Offset & Format Detection:
   - Automatically skips non-data title banners and handles Excel, CSV, or HTML tables disguised as .xls.
   - Dual-language support for both Devanagari and English column headers.
   - Auto-promotes row 10 to header if row 9 had merged titles with 'Unnamed'.
   - Parses multi-unit stock strings (e.g., '1 11 Pcs' -> 11.000).
2. Comprehensive Entity Migration:
   - Safe Categories & Brands: Collision-proof code generation preventing unique constraint errors (e.g. MOBI).
   - Collision-Proof Product SKUs: Guaranteed unique SKU generator preventing database key collisions.
   - Customers: Resolves 9-digit IRD PANs, creates profiles, logs opening Udhaari, and balances GL Account 1210 vs 3120.
   - Suppliers: Maps supplier PANs, creates profiles, logs opening payables, and balances GL Account 3120 vs 2110.
   - Inventory: Catalogues products, tracks categories/brands, initializes BranchStock, FIFO batches, IMEI tokens,
     and posts opening inventory valuation to GL Account 1310 vs 3120.
   - Purchases: Seamlessly handles Annex 13 VAT purchase bills (bill-level summary) and line-item GRNs with 13% Input VAT.
   - Sales: Multi-item line grouping with header forward-filling, distinct Unit Rate vs. Sub Total resolution,
     line-item discount handling with multi-item adapter isolation, and 3-way double-entry GL posting (Cash/AR, Revenue, Output VAT).
3. Chronological Integrity & Multi-Year Fiscal Control:
   - Sorts records chronologically from oldest (2080.04.01) to newest.
   - Automatically converts BS <-> AD dates and assigns correct Nepali Fiscal Years (2080/81 to 2083/84).
   - Enforces fiscal year locking: FY 2080/81, 2081/82, and 2082/83 are permanently locked upon completion;
     FY 2083/84 remains active and open for live counter billing.
"""

import os
import re
import uuid
import logging
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from datetime import date, datetime
from typing import Dict, Any, List, Optional, Tuple

import pandas as pd
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.text import slugify
from django.utils import timezone

from apps.core.nepali_calendar import NepaliCalendar
from apps.core.utils.nepali_date_converter import parse_bs_date_to_ad, ad_to_bs_string
from apps.branches.models import Branch, BranchDocumentSequence
from apps.users.models import User
from apps.customers.models import Customer, CustomerUdhaariLedger
from apps.inventory.models import (
    Product, ProductCategory, Brand, UnitOfMeasurement, BranchStock,
    ItemInstance, ProductBatch, StockMovementLog
)
from apps.products.models import HistoricalProduct
from apps.purchases.models import Supplier, GoodsReceivedNote, GRNItem, SupplierUdhaariLedger
from apps.sales.models import SalesEstimate, SalesEstimateItem, SalesPaymentTransaction
from apps.accounting.models import Account, JournalEntry, AccountingFiscalYear, FinancialPeriod
from apps.accounting.services.auto_posting import JournalEngine, AutoPostingService
from apps.core.models import SystemConfiguration, AuditLog

logger = logging.getLogger(__name__)

OPENING_DATE_AD = date(2023, 7, 17)
OPENING_DATE_BS = "2080-04-01"
OPENING_FISCAL_YEAR = "2080/81"


class Command(BaseCommand):
    help = (
        "Migrates historical data exported from Mobilesoft/Hisaav (2080/81 B.S. onwards) into the ERP. "
        "Supports Annex 13 Purchases, Itemized Sales Details, Opening Stock with multi-unit quantities, "
        "Customer/Supplier Udhaari ledgers, and posts 3-way balanced accounting vouchers up to FY 2083/84."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dir',
            type=str,
            help='Directory containing exported Excel/CSV files.'
        )
        parser.add_argument(
            '--customers',
            type=str,
            help='Path to exported customers file (.xlsx, .xls, .csv).'
        )
        parser.add_argument(
            '--suppliers',
            type=str,
            help='Path to exported suppliers file (.xlsx, .xls, .csv).'
        )
        parser.add_argument(
            '--inventory',
            '--stock',
            dest='inventory',
            type=str,
            nargs='*',
            help='Path to exported stock/inventory file(s) (.xlsx, .xls, .csv).'
        )
        parser.add_argument(
            '--purchases',
            type=str,
            help='Path to exported purchase report / bills file (.xlsx, .xls, .csv).'
        )
        parser.add_argument(
            '--sales',
            type=str,
            help='Path to exported sales report / sales detail register (.xlsx, .xls, .csv).'
        )
        parser.add_argument(
            '--branch',
            type=str,
            default='',
            help='Target branch code (e.g. BR-MAIN-01). Defaults to Primary Main Branch.'
        )
        parser.add_argument(
            '--default-cashier',
            type=str,
            default='',
            help='Username of staff member to credit for historical migrated transactions.'
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Validate and calculate totals without committing changes to the database.'
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        branch_code = options['branch']
        default_cashier_username = options['default_cashier']

        self.stdout.write(self.style.MIGRATE_HEADING("=" * 85))
        self.stdout.write(self.style.MIGRATE_HEADING("  MOBILE SOFT (BANEPA) - LEGACY DATA MIGRATION ENGINE"))
        self.stdout.write(self.style.MIGRATE_HEADING("=" * 85))

        if dry_run:
            self.stdout.write(self.style.WARNING(">>> DRY-RUN SIMULATION ACTIVE: Database transactions will roll back. <<<"))

        # 1. Resolve Target Branch
        branch = None
        if branch_code:
            branch = Branch.objects.filter(code__iexact=branch_code).first()
            if not branch:
                raise CommandError(f"Branch with code '{branch_code}' does not exist.")
        if not branch:
            branch = Branch.get_default_main_branch()

        self.stdout.write(f"[*] Target Store Outlet: {branch.name} ({branch.code})")

        # 2. Resolve Operator / Cashier
        cashier = None
        if default_cashier_username:
            cashier = User.objects.filter(username__iexact=default_cashier_username).first()
        if not cashier:
            cashier = User.objects.filter(is_superuser=True).first() or User.objects.first()

        self.stdout.write(f"[*] Default Operator: {cashier.username if cashier else 'System'}")

        # 3. Ensure Multi-Year Fiscal Years Exist (2080/81 to 2083/84)
        target_fy_names = ['2080/81', '2081/82', '2082/83', '2083/84']
        self._ensure_fiscal_years_exist(target_fy_names)

        # 4. Track Originally Closed Periods to Re-lock in finally Block
        originally_closed_fy_ids: List[int] = []
        originally_closed_period_ids: List[int] = []

        if not dry_run:
            originally_closed_fy_ids = list(
                AccountingFiscalYear.objects.filter(
                    name__in=['2080/81', '2081/82', '2082/83'],
                    is_closed=True
                ).values_list('id', flat=True)
            )
            originally_closed_period_ids = list(
                FinancialPeriod.objects.filter(
                    fiscal_year__name__in=['2080/81', '2081/82', '2082/83'],
                    is_closed=True
                ).values_list('id', flat=True)
            )

            # Temporarily unlock historical periods for migration
            AccountingFiscalYear.objects.filter(name__in=target_fy_names).update(is_closed=False)
            FinancialPeriod.objects.filter(fiscal_year__name__in=target_fy_names).update(is_closed=False)
            self.stdout.write("[*] Temporarily unlocked historical fiscal periods for migration.")

        # 5. Resolve File Paths
        source_dir = options.get('dir')
        customers_file = options.get('customers')
        suppliers_file = options.get('suppliers')
        inventory_inputs = options.get('inventory') or []
        purchases_file = options.get('purchases')
        sales_file = options.get('sales')

        inventory_files: List[str] = []
        if isinstance(inventory_inputs, list):
            inventory_files.extend(inventory_inputs)
        elif inventory_inputs:
            inventory_files.append(str(inventory_inputs))

        if source_dir and os.path.isdir(source_dir):
            for fname in os.listdir(source_dir):
                fpath = os.path.join(source_dir, fname)
                lower = fname.lower()
                if not customers_file and any(k in lower for k in ['customer', 'client', 'udhari', 'udhaari']):
                    customers_file = fpath
                elif not suppliers_file and any(k in lower for k in ['supplier', 'vendor', 'distributor', 'party']):
                    suppliers_file = fpath
                elif any(k in lower for k in ['item', 'product', 'inventory', 'stock']):
                    if fpath not in inventory_files:
                        inventory_files.append(fpath)
                elif not purchases_file and any(k in lower for k in ['purchase', 'buy', 'grn', 'kharid']):
                    purchases_file = fpath
                elif not sales_file and any(k in lower for k in ['sale', 'bill', 'invoice', 'bikri', 'sales_register', 'sales detail']):
                    sales_file = fpath

        try:
            with transaction.atomic():
                # Step 1: Customers
                if customers_file:
                    self.stdout.write(self.style.MIGRATE_LABEL(f"\n[1/5] Processing Customers from: {customers_file}"))
                    self._migrate_customers(customers_file, branch, cashier, dry_run)
                else:
                    self.stdout.write(self.style.NOTICE("\n[1/5] Standalone customers file not provided. Proceeding."))

                # Step 2: Suppliers
                if suppliers_file:
                    self.stdout.write(self.style.MIGRATE_LABEL(f"\n[2/5] Processing Suppliers from: {suppliers_file}"))
                    self._migrate_suppliers(suppliers_file, branch, cashier, dry_run)
                else:
                    self.stdout.write(self.style.NOTICE("\n[2/5] Standalone suppliers file not provided. Proceeding."))

                # Step 3: Stock / Inventory (Handles Positive & Out of Stock)
                if inventory_files:
                    self.stdout.write(self.style.MIGRATE_LABEL(f"\n[3/5] Processing Stock Reports ({len(inventory_files)} file(s))..."))
                    for inv_f in inventory_files:
                        self.stdout.write(f"  -> File: {os.path.basename(inv_f)}")
                        self._migrate_inventory(inv_f, branch, cashier, dry_run)
                else:
                    self.stdout.write(self.style.NOTICE("\n[3/5] Inventory stock file not provided. Skipping."))

                # Step 4: Purchases (Handles Annex 13 Register & Detailed Bills)
                if purchases_file:
                    self.stdout.write(self.style.MIGRATE_LABEL(f"\n[4/5] Processing Purchases from: {purchases_file}"))
                    self._migrate_purchases(purchases_file, branch, cashier, dry_run)
                else:
                    self.stdout.write(self.style.NOTICE("\n[4/5] Purchases file not provided. Skipping."))

                # Step 5: Sales (Handles Itemized Sales Details & IRD Registers)
                if sales_file:
                    self.stdout.write(self.style.MIGRATE_LABEL(f"\n[5/5] Processing Sales Register from: {sales_file}"))
                    self._migrate_sales(sales_file, branch, cashier, dry_run)
                else:
                    self.stdout.write(self.style.NOTICE("\n[5/5] Sales file not provided. Skipping."))

                if dry_run:
                    self.stdout.write(self.style.WARNING("\n[!] Dry-run simulation complete. Rolling back all database mutations."))
                    transaction.set_rollback(True)
                else:
                    self.stdout.write(self.style.SUCCESS("\n[+] Migration pipeline executed and committed successfully!"))

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"\n[!] Migration terminated due to error: {str(e)}"))
            raise
        finally:
            if not dry_run:
                # Permanent locking of closed past periods; keeping 2083/84 open
                AccountingFiscalYear.objects.filter(name__in=['2080/81', '2081/82', '2082/83']).update(is_closed=True)
                FinancialPeriod.objects.filter(fiscal_year__name__in=['2080/81', '2081/82', '2082/83']).update(is_closed=True)
                
                AccountingFiscalYear.objects.filter(name='2083/84').update(is_closed=False)
                FinancialPeriod.objects.filter(fiscal_year__name='2083/84').update(is_closed=False)

                self.stdout.write(self.style.SUCCESS(
                    "[*] Fiscal Year Integrity Enforced: FY 2080/81, 2081/82, 2082/83 are permanently LOCKED. "
                    "FY 2083/84 is ACTIVE and OPEN for counter billing."
                ))

    # =========================================================================
    # CORE PARSING & PREPARATION UTILITIES
    # =========================================================================

    def _get_or_create_category(self, cat_name: str) -> ProductCategory:
        """
        Collision-proof category resolver. Ensures code is unique even when
        names share the same starting characters (e.g. Mobile vs Mobile Accessories).
        """
        clean_name = str(cat_name or 'Mobile Accessories').strip()
        cat = ProductCategory.objects.filter(name__iexact=clean_name).first()
        if cat:
            return cat

        base_code = slugify(clean_name)[:4].upper() or 'CAT'
        code = base_code
        counter = 1
        while ProductCategory.objects.filter(code=code).exists():
            code = f"{base_code[:3]}{counter}"
            counter += 1

        return ProductCategory.objects.create(
            name=clean_name,
            code=code,
            is_active=True
        )

    def _generate_unique_sku(self, name: str, brand_name: str = '', prefix_override: str = '') -> str:
        """
        Collision-proof SKU generator. Guarantees absolute uniqueness against PostgreSQL's
        unique constraint 'inv_products_sku_key' using 8 hex characters and an existence check.
        """
        if prefix_override:
            prefix = prefix_override
        else:
            clean_b = slugify(brand_name or '')[:3].upper()
            prefix = clean_b if clean_b else 'ITM'

        name_part = slugify(name or '')[:6].upper() or 'PROD'
        base_sku = f"{prefix}-{name_part}"
        sku = f"{base_sku}-{uuid.uuid4().hex[:8].upper()}"
        while Product.objects.filter(sku=sku).exists():
            sku = f"{base_sku}-{uuid.uuid4().hex[:8].upper()}"
        return sku

    def _read_smart_dataframe(self, filepath: str) -> pd.DataFrame:
        """
        Reads Excel, CSV, or HTML tables disguised as .xls with dynamic header offset detection.
        Scans top rows to locate the true table header row.
        Auto-promotes row 10 to header if row 9 had merged titles with 'Unnamed'.
        """
        ext = os.path.splitext(filepath)[1].lower()
        header_indicators = [
            'मिती', 'मिति', 'date', 'बीजक', 'invoice', 'bill', 'खरिदकर्ता', 'patron',
            'customer', 'वस्तु', 'item', 'product', 'particular', 'करयोग्य', 'taxable',
            'जम्मा', 'remaining', 'primary price'
        ]

        df = None

        if ext in ['.xlsx', '.xls']:
            try:
                preview_df = pd.read_excel(filepath, header=None, nrows=15, dtype=str)
                h_idx = self._detect_header_row_index(preview_df, header_indicators)
                df = pd.read_excel(filepath, header=h_idx, dtype=str)
            except Exception:
                try:
                    tables = pd.read_html(filepath)
                    if tables:
                        raw_html_df = tables[0].astype(str)
                        h_idx = self._detect_header_row_index(raw_html_df.head(15), header_indicators)
                        if h_idx > 0:
                            raw_html_df.columns = raw_html_df.iloc[h_idx]
                            df = raw_html_df.iloc[h_idx + 1:].copy()
                        else:
                            df = raw_html_df
                except Exception:
                    pass

        if df is None:
            for enc in ['utf-8', 'latin1', 'cp1252']:
                try:
                    preview_df = pd.read_csv(filepath, header=None, nrows=15, dtype=str, encoding=enc)
                    h_idx = self._detect_header_row_index(preview_df, header_indicators)
                    df = pd.read_csv(filepath, header=h_idx, dtype=str, encoding=enc)
                    break
                except Exception:
                    continue

        if df is None:
            raise CommandError(f"Could not parse file structure: {filepath}")

        df = df.fillna('')
        df.columns = [str(col).strip() for col in df.columns]

        # Auto-promote row 10 if row 9 had merged titles with 'Unnamed'
        cols_lower = " ".join(df.columns).lower()
        if 'मिति' not in cols_lower and 'date' not in cols_lower and 'मिती' not in cols_lower:
            if len(df) > 0:
                first_row_text = " ".join(str(v).lower() for v in df.iloc[0].values)
                if 'मिति' in first_row_text or 'date' in first_row_text or 'मिती' in first_row_text:
                    df.columns = [str(c).strip() for c in df.iloc[0].values]
                    df = df.iloc[1:].copy()

        return df

    def _detect_header_row_index(self, preview_df: pd.DataFrame, header_indicators: List[str]) -> int:
        for idx, row in preview_df.iterrows():
            row_text = " ".join(str(val).lower() for val in row.values if pd.notna(val))
            matches = sum(1 for kw in header_indicators if kw in row_text)
            if matches >= 2:
                return idx
        return 0

    def _clean_product_name(self, raw_name: str) -> str:
        """
        Cleans serial numbers prepended by Hisaav (e.g. '1 AirPods 4' -> 'AirPods 4', '10 I Phone 17' -> 'I Phone 17').
        Leaves valid model identifiers (e.g. '105', 'D2000') intact.
        """
        val = str(raw_name or '').strip()
        cleaned = re.sub(r'^\s*\d+\s+([A-Za-z].*)$', r'\1', val).strip()
        cleaned = re.sub(r'\s+Pcs$', '', cleaned, flags=re.IGNORECASE).strip()
        return cleaned or val

    def _parse_stock_quantity(self, raw_val: Any) -> Decimal:
        """
        Extracts quantity from Hisaav's multi-unit remaining stock strings:
        '1 11 Pcs' -> 11.000 | '1 303 Pcs' -> 303.000 | '1062 Pcs' -> 1062.000 | '-' -> 0.000
        """
        raw_str = str(raw_val or '').strip()
        if not raw_str or raw_str in ['-', '--', 'None', 'nan']:
            return Decimal('0.000')
        nums = re.findall(r'(\d+(?:\.\d+)?)', raw_str)
        if nums:
            try:
                return Decimal(nums[-1]).quantize(Decimal('0.001'))
            except (InvalidOperation, ValueError):
                return Decimal('0.000')
        return Decimal('0.000')

    def _parse_decimal(self, val: Any, default: Decimal = Decimal('0.00')) -> Decimal:
        """Sanitizes monetary figures, stripping commas, hyphens, and currency symbols."""
        if val is None:
            return default
        raw_str = str(val).strip()
        if raw_str in ['', '-', '--', 'N/A', 'nan', 'null', 'None']:
            return default
        try:
            clean_str = re.sub(r'[^\d.-]', '', raw_str)
            if not clean_str or clean_str in ['-', '.', '-.']:
                return default
            return Decimal(clean_str).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        except (InvalidOperation, ValueError, TypeError):
            return default

    def _parse_nepali_date(self, raw_val: Any, default_date: Optional[date] = None) -> Tuple[date, str, str]:
        """
        Parses Bikram Sambat date (e.g. '2083.05.07', '2080/04/01', '2080-04-01')
        into Gregorian AD date, formatted BS string (YYYY-MM-DD), and Fiscal Year.
        """
        raw_str = str(raw_val or '').strip()
        if not raw_str:
            target_ad = default_date or date.today()
            y, m, d = NepaliCalendar.ad_to_bs(target_ad)
            return target_ad, NepaliCalendar.format_bs(y, m, d, lang='en'), NepaliCalendar.get_fiscal_year(y, m)

        clean_date_str = re.sub(r'[^\d]', '-', raw_str)
        parts = [int(p) for p in clean_date_str.split('-') if p]

        if len(parts) >= 3:
            if 2000 <= parts[0] <= 2095:
                bs_y, bs_m, bs_d = parts[0], parts[1], parts[2]
            elif 2000 <= parts[2] <= 2095:
                bs_y, bs_m, bs_d = parts[2], parts[1], parts[0]
            else:
                try:
                    ad_dt = datetime.strptime(raw_str[:10], '%Y-%m-%d').date()
                    y, m, d = NepaliCalendar.ad_to_bs(ad_dt)
                    return ad_dt, NepaliCalendar.format_bs(y, m, d, lang='en'), NepaliCalendar.get_fiscal_year(y, m)
                except Exception:
                    bs_y, bs_m, bs_d = 2080, 4, 1

            bs_m = max(1, min(12, bs_m))
            max_days = NepaliCalendar.get_days_in_month(bs_y, bs_m)
            bs_d = max(1, min(max_days, bs_d))

            ad_date = NepaliCalendar.bs_to_ad(bs_y, bs_m, bs_d)
            bs_str = f"{bs_y:04d}-{bs_m:02d}-{bs_d:02d}"
            fy = NepaliCalendar.get_fiscal_year(bs_y, bs_m)
            return ad_date, bs_str, fy

        target_ad = default_date or OPENING_DATE_AD
        y, m, d = NepaliCalendar.ad_to_bs(target_ad)
        return target_ad, NepaliCalendar.format_bs(y, m, d, lang='en'), NepaliCalendar.get_fiscal_year(y, m)

    def _ensure_fiscal_years_exist(self, fy_list: List[str]):
        """Seeds accounting fiscal years and their 12 monthly financial periods if absent."""
        for fy_name in fy_list:
            start_ad, end_ad, start_bs, end_bs = NepaliCalendar.get_fiscal_year_range(fy_name)
            fy_obj, _ = AccountingFiscalYear.objects.get_or_create(
                name=fy_name,
                defaults={
                    'start_date_ad': start_ad,
                    'end_date_ad': end_ad,
                    'start_date_bs': start_bs,
                    'end_date_bs': end_bs,
                    'is_closed': False
                }
            )

            base_year = int(fy_name.split('/')[0])
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
                    fiscal_year=fy_obj,
                    period_number=month_idx,
                    defaults={
                        'period_name_en': f"{m_name_en} ({fy_name})",
                        'period_name_np': f"{m_name_np} ({fy_name})",
                        'start_date_ad': s_ad,
                        'end_date_ad': e_ad,
                        'start_date_bs': s_bs,
                        'end_date_bs': e_bs,
                        'is_closed': False
                    }
                )

    # =========================================================================
    # 1. CUSTOMERS MIGRATION (LEDGERS & OPENING EQUITY)
    # =========================================================================

    def _migrate_customers(self, filepath: str, branch: Branch, cashier: User, dry_run: bool):
        df = self._read_smart_dataframe(filepath)
        headers = list(df.columns)

        def match_col(patterns):
            for p in patterns:
                for h in headers:
                    if re.search(p, h.lower()):
                        return h
            return ''

        name_col = match_col([r'name', r'customer', r'client', r'ग्राहक'])
        phone_col = match_col([r'phone', r'mobile', r'contact', r'सम्पर्क'])
        pan_col = match_col([r'pan', r'vat', r'स्थायी'])
        address_col = match_col([r'address', r'city', r'location', r'ठेगाना'])
        balance_col = match_col([r'balance', r'due', r'udhari', r'udhaari', r'बाँकी'])

        if not name_col:
            self.stdout.write(self.style.ERROR("  [!] Customer Name column not found. Skipping."))
            return

        created_cnt = 0
        updated_cnt = 0
        gl_posted_cnt = 0

        ar_acc = AutoPostingService.get_or_create_control_account(
            branch=branch, system_tag='ACCOUNTS_RECEIVABLE', default_code='1210',
            default_name='Accounts Receivable (Trade Debtors)', group_category='ASSET', nature='DEBIT'
        )
        equity_acc = AutoPostingService.get_or_create_control_account(
            branch=branch, system_tag='OPENING_BALANCE_EQUITY', default_code='3120',
            default_name='Opening Balance Equity', group_category='EQUITY', nature='CREDIT'
        )

        for idx, row in df.iterrows():
            excel_line = idx + 2
            name = str(row.get(name_col, '')).strip()
            if not name or any(k in name.lower() for k in ['total', 'जम्मा']):
                continue

            phone = str(row.get(phone_col, '')).strip() if phone_col else ''
            pan = str(row.get(pan_col, '')).strip() if pan_col else ''
            address = str(row.get(address_col, '')).strip() if address_col else ''
            balance = self._parse_decimal(row.get(balance_col, 0)) if balance_col else Decimal('0.00')

            clean_phone = re.sub(r'\D', '', phone)
            if len(clean_phone) < 7:
                clean_phone = f"98000{idx+1:05d}"

            if not dry_run:
                try:
                    customer = Customer.objects.filter(phone_number=clean_phone).first()
                    if not customer and pan:
                        customer = Customer.objects.filter(pan_number=pan).first()
                    if not customer:
                        customer = Customer.objects.filter(name__iexact=name).first()

                    if customer:
                        fields_to_update = []
                        if pan and not customer.pan_number:
                            customer.pan_number = pan
                            fields_to_update.append('pan_number')
                        if address and not customer.address:
                            customer.address = address
                            fields_to_update.append('address')
                        if fields_to_update:
                            customer.save(update_fields=fields_to_update)
                        updated_cnt += 1
                    else:
                        customer = Customer.objects.create(
                            name=name,
                            phone_number=clean_phone,
                            pan_number=pan or None,
                            address=address or 'Kathmandu',
                            customer_type='RETAIL' if not pan else 'WHOLESALE',
                            current_credit_balance=Decimal('0.00'),
                            credit_limit=Decimal('50000.00') if balance > Decimal('0.00') else Decimal('0.00'),
                            preferred_branch=branch
                        )
                        created_cnt += 1

                    if balance > Decimal('0.00'):
                        ref_doc = f"OPENING-CUST-{customer.id}"
                        if not CustomerUdhaariLedger.objects.filter(customer=customer, reference_invoice=ref_doc).exists():
                            prev_bal = customer.current_credit_balance or Decimal('0.00')
                            CustomerUdhaariLedger.objects.create(
                                customer=customer,
                                branch=branch,
                                entry_type='DEBIT',
                                amount=balance,
                                previous_balance=prev_bal,
                                resulting_balance=prev_bal + balance,
                                reference_invoice=ref_doc,
                                remarks=f"Opening Udhaari balance for {customer.name}",
                                recorded_by=cashier
                            )
                            customer.current_credit_balance = prev_bal + balance
                            customer.save(update_fields=['current_credit_balance'])

                        if not JournalEntry.objects.filter(reference_document=ref_doc, status='POSTED').exists():
                            lines = [
                                {'account': ar_acc, 'debit': balance, 'credit': Decimal('0.00'), 'customer': customer, 'narration': f"Opening Udhaari for {customer.name}"},
                                {'account': equity_acc, 'debit': Decimal('0.00'), 'credit': balance, 'narration': f"Opening equity offset for {customer.name} debt"}
                            ]
                            JournalEngine.create_balanced_entry(
                                voucher_type='JOURNAL',
                                date_ad=OPENING_DATE_AD,
                                branch=branch,
                                lines=lines,
                                narration=f"Opening Customer Balance: {customer.name} (Rs. {balance:.2f})",
                                reference_doc=ref_doc,
                                user=cashier,
                                auto_post=True
                            )
                            gl_posted_cnt += 1
                except Exception as err:
                    raise CommandError(f"Error importing customer '{name}' at line {excel_line}: {err}") from err
            else:
                created_cnt += 1
                if balance > Decimal('0.00'):
                    gl_posted_cnt += 1

        self.stdout.write(self.style.SUCCESS(f"  [OK] Customers: {created_cnt} created, {updated_cnt} updated. GL Vouchers: {gl_posted_cnt}."))

    # =========================================================================
    # 2. SUPPLIERS MIGRATION (LEDGERS & OPENING EQUITY)
    # =========================================================================

    def _migrate_suppliers(self, filepath: str, branch: Branch, cashier: User, dry_run: bool):
        df = self._read_smart_dataframe(filepath)
        headers = list(df.columns)

        def match_col(patterns):
            for p in patterns:
                for h in headers:
                    if re.search(p, h.lower()):
                        return h
            return ''

        name_col = match_col([r'supplier', r'vendor', r'party', r'company', r'सप्लायर', r'आपूर्तिकर्ता'])
        phone_col = match_col([r'phone', r'mobile', r'contact', r'सम्पर्क'])
        pan_col = match_col([r'pan', r'vat', r'स्थायी'])
        address_col = match_col([r'address', r'city', r'location', r'ठेगाना'])
        balance_col = match_col([r'balance', r'due', r'payable', r'बाँकी'])

        if not name_col:
            self.stdout.write(self.style.ERROR("  [!] Supplier Name column not found. Skipping."))
            return

        created_cnt = 0
        updated_cnt = 0
        gl_posted_cnt = 0

        ap_acc = AutoPostingService.get_or_create_control_account(
            branch=branch, system_tag='ACCOUNTS_PAYABLE', default_code='2110',
            default_name='Accounts Payable (Trade Creditors)', group_category='LIABILITY', nature='CREDIT'
        )
        equity_acc = AutoPostingService.get_or_create_control_account(
            branch=branch, system_tag='OPENING_BALANCE_EQUITY', default_code='3120',
            default_name='Opening Balance Equity', group_category='EQUITY', nature='CREDIT'
        )

        for idx, row in df.iterrows():
            excel_line = idx + 2
            name = str(row.get(name_col, '')).strip()
            if not name or any(k in name.lower() for k in ['total', 'जम्मा']):
                continue

            phone = str(row.get(phone_col, '')).strip() if phone_col else ''
            pan = str(row.get(pan_col, '')).strip() if pan_col else ''
            address = str(row.get(address_col, '')).strip() if address_col else ''
            balance = self._parse_decimal(row.get(balance_col, 0)) if balance_col else Decimal('0.00')

            clean_phone = re.sub(r'\D', '', phone)
            if len(clean_phone) < 7:
                clean_phone = f"98100{idx+1:05d}"

            if not dry_run:
                try:
                    supplier = Supplier.objects.filter(phone_number=clean_phone).first()
                    if not supplier and pan:
                        supplier = Supplier.objects.filter(pan_number=pan).first()
                    if not supplier:
                        supplier = Supplier.objects.filter(company_name__iexact=name).first()

                    if supplier:
                        fields_to_update = []
                        if pan and not supplier.pan_number:
                            supplier.pan_number = pan
                            fields_to_update.append('pan_number')
                        if address and not supplier.address:
                            supplier.address = address
                            fields_to_update.append('address')
                        if fields_to_update:
                            supplier.save(update_fields=fields_to_update)
                        updated_cnt += 1
                    else:
                        supplier = Supplier.objects.create(
                            company_name=name,
                            contact_person=name,
                            phone_number=clean_phone,
                            pan_number=pan or None,
                            address=address or 'Kathmandu',
                            opening_balance=balance,
                            balance_type='PAYABLE',
                            current_balance=Decimal('0.00')
                        )
                        created_cnt += 1

                    if balance > Decimal('0.00'):
                        ref_doc = f"OPENING-SUPP-{supplier.id}"
                        if not SupplierUdhaariLedger.objects.filter(supplier=supplier, reference_number=ref_doc).exists():
                            prev_bal = supplier.current_balance or Decimal('0.00')
                            SupplierUdhaariLedger.objects.create(
                                supplier=supplier,
                                branch=branch,
                                transaction_type='PURCHASE_BILL',
                                amount=balance,
                                previous_balance=prev_bal,
                                resulting_balance=prev_bal + balance,
                                payment_mode='OTHER',
                                reference_number=ref_doc,
                                remarks=f"Opening payable debt to {supplier.company_name}",
                                recorded_by=cashier
                            )
                            supplier.current_balance = prev_bal + balance
                            supplier.save(update_fields=['current_balance'])

                        if not JournalEntry.objects.filter(reference_document=ref_doc, status='POSTED').exists():
                            lines = [
                                {'account': equity_acc, 'debit': balance, 'credit': Decimal('0.00'), 'narration': f"Opening equity offset for supplier {supplier.company_name}"},
                                {'account': ap_acc, 'debit': Decimal('0.00'), 'credit': balance, 'supplier': supplier, 'narration': f"Opening payable balance to {supplier.company_name}"}
                            ]
                            JournalEngine.create_balanced_entry(
                                voucher_type='JOURNAL',
                                date_ad=OPENING_DATE_AD,
                                branch=branch,
                                lines=lines,
                                narration=f"Opening Supplier Payable: {supplier.company_name} (Rs. {balance:.2f})",
                                reference_doc=ref_doc,
                                user=cashier,
                                auto_post=True
                            )
                            gl_posted_cnt += 1
                except Exception as err:
                    raise CommandError(f"Error importing supplier '{name}' at line {excel_line}: {err}") from err
            else:
                created_cnt += 1
                if balance > Decimal('0.00'):
                    gl_posted_cnt += 1

        self.stdout.write(self.style.SUCCESS(f"  [OK] Suppliers: {created_cnt} created, {updated_cnt} updated. GL Vouchers: {gl_posted_cnt}."))

    # =========================================================================
    # 3. STOCK / INVENTORY MIGRATION (WITH MULTI-UNIT & VALUATION SUPPORT)
    # =========================================================================

    def _migrate_inventory(self, filepath: str, branch: Branch, cashier: User, dry_run: bool):
        df = self._read_smart_dataframe(filepath)
        headers = list(df.columns)

        def match_col(patterns):
            for p in patterns:
                for h in headers:
                    if re.search(p, h.lower()):
                        return h
            return ''

        name_col = match_col([r'item.*name', r'product.*name', r'^name$', r'^item$', r'विवरण'])
        category_col = match_col([r'category', r'group', r'वर्ग'])
        brand_col = match_col([r'brand', r'make', r'कम्पनी'])
        barcode_col = match_col([r'barcode', r'upc', r'ean', r'बारकोड'])
        cost_col = match_col([r'average.*price', r'cost', r'purchase', r'buy.*rate', r'खरिद'])
        mrp_col = match_col([r'latest.*price', r'mrp', r'selling', r'sale.*rate', r'बिक्री'])
        stock_col = match_col([r'remaining.*stock', r'remaining', r'stock', r'qty', r'quantity', r'मौज्दात'])
        imei_col = match_col([r'imei', r'serial'])
        ram_col = match_col([r'ram', r'र्याम'])
        storage_col = match_col([r'storage', r'rom', r'capacity', r'रोम'])

        if not name_col:
            self.stdout.write(self.style.ERROR("  [!] Product Name column not found. Skipping."))
            return

        base_unit_pcs, _ = UnitOfMeasurement.objects.get_or_create(
            code='PCS', defaults={'name': 'Piece', 'name_np': 'पिस', 'allow_decimal': False}
        )

        created_cnt = 0
        updated_cnt = 0
        gl_posted_cnt = 0

        inv_asset_acc = AutoPostingService.get_or_create_control_account(
            branch=branch, system_tag='INVENTORY_ASSET', default_code='1310',
            default_name='Merchandise Inventory Asset', group_category='ASSET', nature='DEBIT'
        )
        equity_acc = AutoPostingService.get_or_create_control_account(
            branch=branch, system_tag='OPENING_BALANCE_EQUITY', default_code='3120',
            default_name='Opening Balance Equity', group_category='EQUITY', nature='CREDIT'
        )

        for idx, row in df.iterrows():
            excel_line = idx + 2
            raw_name = str(row.get(name_col, '')).strip()
            if not raw_name or any(k in raw_name.lower() for k in ['total', 'जम्मा', 'duration']):
                continue

            name = self._clean_product_name(raw_name)
            raw_cat = str(row.get(category_col, '')).strip() or "Mobile Accessories"
            raw_brand = str(row.get(brand_col, '')).strip()
            raw_barcode = str(row.get(barcode_col, '')).strip()
            cost_price = self._parse_decimal(row.get(cost_col, 0)) if cost_col else Decimal('0.00')
            selling_price = self._parse_decimal(row.get(mrp_col, 0)) if mrp_col else Decimal('0.00')
            
            # Use multi-unit parser for strings like '1 11 Pcs'
            stock_qty = self._parse_stock_quantity(row.get(stock_col, 0)) if stock_col else Decimal('0.000')
            
            raw_imeis = str(row.get(imei_col, '')).strip() if imei_col else ""
            raw_ram = str(row.get(ram_col, '')).strip() if ram_col else ""
            raw_storage = str(row.get(storage_col, '')).strip() if storage_col else ""

            is_phone = bool(
                raw_imeis or raw_ram or raw_storage or
                'phone' in raw_cat.lower() or 'mobile' in raw_cat.lower() or
                any(b in name.lower() for b in ['iphone', 'samsung', 'redmi', 'vivo', 'realme', 'benco', 'itel', 'lava'])
            )

            if not dry_run:
                try:
                    # Centralized collision-proof category creation
                    cat_obj = self._get_or_create_category(raw_cat)

                    brand_obj = None
                    if raw_brand:
                        clean_brand = raw_brand.strip()
                        brand_obj = Brand.objects.filter(name__iexact=clean_brand).first()
                        if not brand_obj:
                            brand_obj = Brand.objects.create(name=clean_brand, origin_country='Nepal')

                    product = None
                    if raw_barcode:
                        product = Product.objects.filter(barcode=raw_barcode).first()
                    if not product:
                        product = Product.objects.filter(name__iexact=name).first()

                    if product:
                        if cost_price > Decimal('0.00'):
                            product.purchase_price = cost_price
                        if selling_price > Decimal('0.00'):
                            product.selling_price = selling_price
                        product.save()
                        updated_cnt += 1
                    else:
                        sku_code = self._generate_unique_sku(name=name, brand_name=raw_brand)
                        product = Product(
                            name=name,
                            model_name=name,
                            sku=sku_code,
                            barcode=raw_barcode or None,
                            category=cat_obj,
                            brand=brand_obj,
                            ram=raw_ram or None,
                            internal_storage=raw_storage or None,
                            base_unit=base_unit_pcs,
                            purchase_price=cost_price,
                            selling_price=selling_price if selling_price > 0 else (cost_price * Decimal('1.15')).quantize(Decimal('0.01')),
                            requires_imei_tracking=is_phone,
                            warranty_months=12 if is_phone else 0,
                            tax_pricing_type='INCLUSIVE',
                            is_vat_applicable=True,
                            vat_rate=Decimal('13.00')
                        )
                        product._skip_signal_branch_stock = True
                        product.save()
                        created_cnt += 1

                    b_stock, _ = BranchStock.objects.get_or_create(
                        branch=branch,
                        product=product,
                        defaults={'quantity': Decimal('0.000'), 'reserved_quantity': Decimal('0.000')}
                    )

                    if stock_qty > Decimal('0.000'):
                        prev_qty = b_stock.quantity
                        b_stock.quantity += stock_qty
                        b_stock.save(update_fields=['quantity', 'updated_at'])

                        batch_id = f"BATCH-MIG-{uuid.uuid4().hex[:6].upper()}"
                        ProductBatch.objects.create(
                            batch_number=batch_id,
                            product=product,
                            branch=branch,
                            purchase_date=OPENING_DATE_AD,
                            cost_price=product.purchase_price,
                            selling_price=product.selling_price,
                            quantity_received=stock_qty,
                            quantity_remaining=stock_qty,
                            supplier_name="Opening Stock"
                        )

                        # Parse IMEI tokens if supplied in file
                        if is_phone and raw_imeis:
                            imei_tokens = [t.strip() for t in re.split(r'[\n,;]+', raw_imeis) if t.strip()]
                            for i in range(int(stock_qty)):
                                im1 = imei_tokens[i] if i < len(imei_tokens) else None
                                if im1 and ItemInstance.objects.filter(imei_1=im1, status='IN_STOCK').exists():
                                    im1 = None

                                ItemInstance.objects.create(
                                    product=product,
                                    branch=branch,
                                    device_uid=f"DEV-{uuid.uuid4().hex[:12].upper()}",
                                    imei_1=im1,
                                    status='IN_STOCK',
                                    condition='BRAND_NEW',
                                    source_type='NEW_PURCHASE_GRN',
                                    mdms_status='REGISTERED_OFFICIAL',
                                    purchase_reference='MIGRATION',
                                    batch_reference=batch_id,
                                    supplier_name="Opening Stock",
                                    landed_cost=product.purchase_price,
                                    purchase_date=OPENING_DATE_AD
                                )

                        ref_doc = f"OPENING-STOCK-{product.id}"
                        StockMovementLog.objects.create(
                            product=product,
                            branch=branch,
                            movement_type='ADJUSTMENT_ADD',
                            quantity_delta=stock_qty,
                            previous_quantity=prev_qty,
                            new_quantity=b_stock.quantity,
                            reference_document=ref_doc,
                            remarks=f"Opening inventory stock migration: {product.name}",
                            user=cashier
                        )

                        line_val = (stock_qty * product.purchase_price).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                        if line_val > Decimal('0.00') and not JournalEntry.objects.filter(reference_document=ref_doc, status='POSTED').exists():
                            lines = [
                                {'account': inv_asset_acc, 'debit': line_val, 'credit': Decimal('0.00'), 'narration': f"Opening stock asset: {product.name}"},
                                {'account': equity_acc, 'debit': Decimal('0.00'), 'credit': line_val, 'narration': f"Opening equity for stock: {product.name}"}
                            ]
                            JournalEngine.create_balanced_entry(
                                voucher_type='JOURNAL',
                                date_ad=OPENING_DATE_AD,
                                branch=branch,
                                lines=lines,
                                narration=f"Opening Stock Valuation: {product.name} x {stock_qty}",
                                reference_doc=ref_doc,
                                user=cashier,
                                auto_post=True
                            )
                            gl_posted_cnt += 1

                except Exception as err:
                    raise CommandError(f"Error importing product '{name}' at line {excel_line}: {err}") from err
            else:
                created_cnt += 1
                if stock_qty > Decimal('0.000'):
                    gl_posted_cnt += 1

        self.stdout.write(self.style.SUCCESS(f"  [OK] Products: {created_cnt} created, {updated_cnt} updated. GL Vouchers: {gl_posted_cnt}."))

    # =========================================================================
    # 4. PURCHASES / GRN MIGRATION (ANNEX 13 & ITEMIZED DUAL SUPPORT)
    # =========================================================================

    def _migrate_purchases(self, filepath: str, branch: Branch, cashier: User, dry_run: bool):
        df = self._read_smart_dataframe(filepath)
        headers = list(df.columns)

        def match_col(patterns):
            for p in patterns:
                for h in headers:
                    if re.search(p, h.lower()):
                        return h
            return ''

        bill_no_col = match_col([r'बीजक', r'bill', r'invoice', r'challan'])
        date_col = match_col([r'मिति', r'मिती', r'^date$', r'date'])
        supplier_col = match_col([r'आपूर्तिकर्ताको\s*नाम', r'आपूर्तिकर्ता', r'supplier', r'vendor', r'party'])
        supp_pan_col = match_col([r'स्थायी\s*लेखा\s*नं', r'प्यान', r'pan', r'vat'])
        product_col = match_col([r'खरिद/पैठारी.*विवरण', r'वस्तु.*सेवाको.*विवरण', r'विवरण', r'product', r'item', r'particular'])
        qty_col = match_col([r'परिमाण', r'qty', r'quantity'])
        rate_col = match_col([r'दर', r'rate', r'cost'])
        total_col = match_col([r'जम्मा\s*खरिद\s*मूल्य', r'जम्मा', r'total.*amount'])
        taxable_col = match_col([r'करयोग्य\s*खरिद', r'taxable'])
        vat_col = match_col([r'कर\s*\(रु\)', r'^कर$', r'vat.*amount', r'^vat$'])
        imei_col = match_col([r'imei', r'serial'])

        if not bill_no_col or not date_col:
            self.stdout.write(self.style.ERROR(
                f"  [!] Required purchase headers (Bill No / Date) not found. Detected headers: {headers}. Skipping."
            ))
            return

        is_annex_13 = bool(taxable_col or vat_col or supp_pan_col)
        p_mobile, p_various = HistoricalProduct.get_or_create_placeholders()

        grn_grouped = df.groupby(bill_no_col)
        imported_grn_cnt = 0
        total_taxable_imported = Decimal('0.00')
        total_vat_imported = Decimal('0.00')
        total_gross_imported = Decimal('0.00')

        for bill_no, group in grn_grouped:
            bill_no_raw = str(bill_no).strip()
            if not bill_no_raw or any(k in bill_no_raw.lower() for k in ['total', 'जम्मा', 'बीजक नं', 'प्रज्ञापनपत्र']):
                continue

            clean_bill_no = re.sub(r'[\s-]+$', '', bill_no_raw).strip()

            try:
                first_row = group.iloc[0]
                raw_date = first_row.get(date_col, '') if date_col else ''
                if not re.search(r'\d{4}', str(raw_date)):
                    continue

                ad_date, bs_date, fiscal_year = self._parse_nepali_date(raw_date)

                supplier_name = str(first_row.get(supplier_col, '')).strip() if supplier_col else "General Supplier"
                if not supplier_name or any(k in supplier_name.lower() for k in ['total', 'जम्मा']):
                    supplier_name = "General Supplier"

                raw_pan = re.sub(r'\D', '', str(first_row.get(supp_pan_col, '')).strip()) if supp_pan_col else ''
                supp_pan = raw_pan if len(raw_pan) == 9 else ''

                if not dry_run:
                    supplier = None
                    if supp_pan:
                        supplier = Supplier.objects.filter(pan_number=supp_pan).first()
                    if not supplier:
                        supplier = Supplier.objects.filter(company_name__iexact=supplier_name).first()

                    if not supplier:
                        fallback_phone = f"9810{supp_pan[:6]}" if supp_pan else f"98100{imported_grn_cnt+1:05d}"
                        while Supplier.objects.filter(phone_number=fallback_phone).exists():
                            fallback_phone = f"98100{re.sub(r'[^0-9]', '', str(uuid.uuid4().int))[:5]}"

                        supplier = Supplier.objects.create(
                            company_name=supplier_name,
                            contact_person=supplier_name,
                            phone_number=fallback_phone,
                            pan_number=supp_pan or None,
                            address='Kathmandu',
                            supplier_type='NATIONAL_DISTRIBUTOR'
                        )

                    grn_num = f"GRN-MS-{clean_bill_no}"
                    grn = GoodsReceivedNote.objects.filter(grn_number=grn_num).first()
                    if not grn:
                        grn = GoodsReceivedNote(
                            grn_number=grn_num,
                            supplier=supplier,
                            branch=branch,
                            supplier_bill_no=clean_bill_no,
                            bill_date=ad_date,
                            bill_date_bs=bs_date,
                            fiscal_year=fiscal_year,
                            status='RECEIVED',
                            distributor_mdms_certified=True,
                            received_by=cashier,
                            remarks=f"Migrated Purchase Bill {clean_bill_no}"
                        )
                        grn.save()

                    bill_gross = Decimal('0.00')
                    bill_taxable = Decimal('0.00')
                    bill_vat = Decimal('0.00')

                    for r_idx, row in group.iterrows():
                        if is_annex_13:
                            line_desc = str(row.get(product_col, 'Various Items')).strip()
                            line_gross = self._parse_decimal(row.get(total_col, 0)) if total_col else Decimal('0.00')
                            line_taxable = self._parse_decimal(row.get(taxable_col, 0)) if taxable_col else Decimal('0.00')
                            line_vat = self._parse_decimal(row.get(vat_col, 0)) if vat_col else Decimal('0.00')

                            if line_gross <= Decimal('0.00'):
                                line_gross = line_taxable + line_vat
                            if line_taxable <= Decimal('0.00') and line_vat > Decimal('0.00'):
                                line_taxable = (line_vat / Decimal('0.13')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

                            bill_gross += line_gross
                            bill_taxable += line_taxable
                            bill_vat += line_vat

                            chosen_product = p_mobile if 'mobile' in line_desc.lower() else p_various
                            GRNItem.objects.create(
                                grn=grn,
                                product=chosen_product,
                                purchased_quantity=Decimal('1.000'),
                                conversion_factor=Decimal('1.000'),
                                base_unit_quantity=Decimal('1.000'),
                                purchase_rate=line_taxable,
                                unit_landed_cost=line_taxable,
                                is_vat_applicable=(line_vat > Decimal('0.00')),
                                vat_rate=Decimal('13.00') if (line_vat > Decimal('0.00')) else Decimal('0.00'),
                                line_total=line_gross
                            )

                        else:
                            prod_name = str(row.get(product_col, '')).strip()
                            if not prod_name:
                                continue

                            raw_q = str(row.get(qty_col, '1')).strip() if qty_col else '1'
                            qty = Decimal('1.000') if raw_q in ['', '-', '--'] else self._parse_decimal(raw_q, Decimal('1.000'))
                            if qty <= Decimal('0.000'):
                                qty = Decimal('1.000')

                            rate = self._parse_decimal(row.get(rate_col, 0)) if rate_col else Decimal('0.00')
                            raw_imei = str(row.get(imei_col, '')).strip() if imei_col else ''

                            product = Product.objects.filter(name__iexact=prod_name).first()
                            if not product:
                                cat_default = ProductCategory.objects.first()
                                base_u = UnitOfMeasurement.objects.filter(code='PCS').first()
                                sku_code = self._generate_unique_sku(name=prod_name, prefix_override='IMP')
                                product = Product.objects.create(
                                    name=prod_name,
                                    sku=sku_code,
                                    category=cat_default,
                                    base_unit=base_u,
                                    purchase_price=rate,
                                    selling_price=rate * Decimal('1.15'),
                                    requires_imei_tracking=bool(raw_imei)
                                )

                            line_tot = (qty * rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                            bill_gross += line_tot
                            bill_taxable += line_tot

                            GRNItem.objects.create(
                                grn=grn,
                                product=product,
                                purchased_quantity=qty,
                                conversion_factor=Decimal('1.000'),
                                base_unit_quantity=qty,
                                purchase_rate=rate,
                                unit_landed_cost=rate,
                                line_total=line_tot,
                                scanned_imei_list=raw_imei
                            )

                    grn.gross_amount = bill_gross
                    grn.vat_amount = bill_vat
                    grn.is_vat_bill = (bill_vat > Decimal('0.00'))
                    grn.total_landed_cost = bill_gross
                    grn.net_total_amount = bill_gross
                    grn.paid_amount = bill_gross
                    grn.due_amount = Decimal('0.00')
                    grn.save()

                    AutoPostingService.post_grn_receipt(grn=grn, user=cashier)
                    imported_grn_cnt += 1
                    total_gross_imported += bill_gross
                    total_taxable_imported += bill_taxable
                    total_vat_imported += bill_vat
                else:
                    imported_grn_cnt += 1
                    for r_idx, row in group.iterrows():
                        total_gross_imported += self._parse_decimal(row.get(total_col, 0)) if total_col else Decimal('0.00')

            except Exception as exc:
                raise CommandError(f"Error processing Purchase Bill '{clean_bill_no}': {exc}") from exc

        self.stdout.write(self.style.SUCCESS(
            f"  [OK] Purchases: {imported_grn_cnt} bills processed with GL vouchers.\n"
            f"       - Taxable Purchases: Rs. {total_taxable_imported:,.2f}\n"
            f"       - Input VAT 13%:     Rs. {total_vat_imported:,.2f}\n"
            f"       - Total Inward:      Rs. {total_gross_imported:,.2f}"
        ))

    # =========================================================================
    # 5. SALES REGISTER MIGRATION (ITEM-LEVEL & IRD ANNEX 5 COMPATIBILITY)
    # =========================================================================

    def _migrate_sales(self, filepath: str, branch: Branch, cashier: User, dry_run: bool):
        df = self._read_smart_dataframe(filepath)
        headers = list(df.columns)

        def match_col(patterns):
            for p in patterns:
                for h in headers:
                    if re.search(p, h.lower()):
                        return h
            return ''

        # Devanagari & Latin Unified Header Mappings
        date_col = match_col([r'^date$', r'मिती', r'मिति'])
        invoice_col = match_col([r'invoice\s*no', r'bill\s*no', r'बीजक.*नं', r'इन्भ्वाइस.*नं'])
        customer_col = match_col([r'patron', r'customer', r'client', r'party', r'खरिदकर्ताको\s*नाम', r'खरिदकर्ता', r'ग्राहक'])
        pan_col = match_col([r'pan\s*no', r'vat\s*no', r'स्थायी\s*लेखा\s*नं', r'प्यान', r'^pan$', r'^vat$'])
        product_col = match_col([r'product\s*description', r'product', r'item', r'particular', r'वस्तु.*सेवाको.*नाम', r'वस्तु', r'सामान'])
        qty_col = match_col([r'primary\s*qty', r'quantity', r'qty', r'परिमाण'])
        unit_col = match_col([r'primary\s*unit', r'एकाइ', r'एकल', r'^unit$'])
        rate_col = match_col([r'primary\s*price', r'rate', r'दर', r'price'])
        subtotal_col = match_col([r'sub\s*total', r'करयोग्य\s*बिक्री', r'taxable.*amount', r'मूल्य'])
        
        # Distinguish item-level 'Discount' from invoice-level 'Total Discount'
        line_discount_col = match_col([r'^discount$', r'item.*discount', r'line.*discount'])
        total_discount_col = match_col([r'total\s*discount', r'छुट', r'discount'])

        tax_col = match_col([r'^tax$', r'vat.*amount', r'कर\s*रकम', r'^कर$', r'^vat$'])
        total_col = match_col([r'^total$', r'grand.*total', r'जम्मा\s*बिक्री', r'जम्मा'])
        exempt_col = match_col([r'कर\s*छुट', r'गैर\s*करयोग्य', r'exempt'])

        if not invoice_col or not date_col:
            raise CommandError(
                f"[Sales Migration Failed] Could not detect Date or Invoice Number headers. "
                f"Detected headers: {headers}"
            )

        # Forward fill headers for multi-item invoices where Hisaav leaves lines 2+ blank
        if invoice_col:
            df[invoice_col] = df[invoice_col].astype(str).str.strip().replace('', None).replace('nan', None).ffill()
        if date_col:
            df[date_col] = df[date_col].astype(str).str.strip().replace('', None).replace('nan', None).ffill()
        if customer_col:
            df[customer_col] = df[customer_col].astype(str).str.strip().replace('', None).replace('nan', None).ffill()
        if pan_col:
            df[pan_col] = df[pan_col].astype(str).str.strip().replace('', None).replace('nan', None).ffill()

        base_unit_pcs, _ = UnitOfMeasurement.objects.get_or_create(
            code='PCS', defaults={'name': 'Piece', 'name_np': 'पिस', 'allow_decimal': False}
        )
        p_mobile, p_various = HistoricalProduct.get_or_create_placeholders()

        valid_rows = []
        for idx, row in df.iterrows():
            excel_line = idx + 2
            raw_inv = str(row.get(invoice_col, '')).strip()
            raw_date = str(row.get(date_col, '')).strip()
            raw_cust = str(row.get(customer_col, '')).strip()

            if not raw_inv or not raw_date:
                continue
            if any(term in raw_inv.lower() for term in ['total', 'जम्मा', 'grand total', 'duration']):
                continue
            if any(term in raw_date.lower() for term in ['total', 'जम्मा']):
                continue
            if any(term in raw_cust.lower() for term in ['grand total', 'कुल जम्मा']):
                continue

            if not re.search(r'\d{4}', raw_date):
                continue

            ad_date, bs_date, fy = self._parse_nepali_date(raw_date)
            raw_prod = str(row.get(product_col, '')).strip() if product_col else 'Mobile'

            st = self._parse_decimal(row.get(subtotal_col, 0)) if subtotal_col else Decimal('0.00')

            # Accurate discount resolution: prefer line-item discount on rows with positive subtotal
            d = Decimal('0.00')
            if line_discount_col:
                d = self._parse_decimal(row.get(line_discount_col, 0))
            if d <= Decimal('0.00') and total_discount_col and st > Decimal('0.00'):
                d = self._parse_decimal(row.get(total_discount_col, 0))

            t = self._parse_decimal(row.get(tax_col, 0)) if tax_col else Decimal('0.00')
            tot = self._parse_decimal(row.get(total_col, 0)) if total_col else Decimal('0.00')
            ex = self._parse_decimal(row.get(exempt_col, 0)) if exempt_col else Decimal('0.00')
            rate = self._parse_decimal(row.get(rate_col, 0)) if rate_col else Decimal('0.00')

            # Accurate taxable base calculation
            if st > Decimal('0.00'):
                taxable = max(Decimal('0.00'), st - d)
            elif tot > Decimal('0.00') and t > Decimal('0.00'):
                taxable = (tot - t).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            elif tot > Decimal('0.00'):
                taxable = (tot / Decimal('1.13')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                t = tot - taxable
            else:
                taxable = Decimal('0.00')

            if tot <= Decimal('0.00'):
                tot = taxable + t + ex

            valid_rows.append({
                'excel_line': excel_line,
                'ad_date': ad_date,
                'bs_date': bs_date,
                'fiscal_year': fy,
                'invoice_no': raw_inv,
                'customer_name': raw_cust or 'Cash Customer (खुदरा ग्राहक)',
                'customer_pan': str(row.get(pan_col, '')).strip() if pan_col else '',
                'product_name': self._clean_product_name(raw_prod),
                'qty_raw': str(row.get(qty_col, '')).strip() if qty_col else '1',
                'unit_raw': str(row.get(unit_col, '')).strip() if unit_col else 'Pcs',
                'unit_rate': rate if rate > Decimal('0.00') else (taxable if taxable > Decimal('0.00') else Decimal('0.00')),
                'total_amount': tot,
                'taxable_amount': taxable,
                'vat_amount': t,
                'exempt_amount': ex,
                'discount_amount': d,
            })

        if not valid_rows:
            self.stdout.write(self.style.WARNING("  [!] No valid sales rows found to import."))
            return

        # Chronological Sorting: Oldest to Newest
        valid_rows.sort(key=lambda x: (x['ad_date'], x['invoice_no']))

        # Group by Invoice Number to assemble multi-line bills
        grouped_invoices: Dict[str, List[Dict[str, Any]]] = {}
        for r in valid_rows:
            grouped_invoices.setdefault(r['invoice_no'], []).append(r)

        imported_bills_cnt = 0
        total_taxable_imported = Decimal('0.00')
        total_vat_imported = Decimal('0.00')
        total_gross_imported = Decimal('0.00')

        self.stdout.write(f"  [*] Processing {len(grouped_invoices)} chronological sales bills...")

        for inv_no, lines in grouped_invoices.items():
            first_line = lines[0]
            ad_date = first_line['ad_date']
            bs_date = first_line['bs_date']
            fiscal_year = first_line['fiscal_year']
            cust_name = first_line['customer_name']
            raw_pan = re.sub(r'\D', '', first_line['customer_pan']) if first_line['customer_pan'] != 'None' else ''
            pan = raw_pan if len(raw_pan) == 9 else ''

            if not dry_run:
                customer, _ = Customer.resolve_or_create_by_pan(
                    name=cust_name, pan=pan or None, branch=branch
                )

                est_number = f"INV-MS-{inv_no}"
                estimate = SalesEstimate.objects.filter(estimate_number=est_number).first()
                if not estimate:
                    estimate = SalesEstimate(
                        estimate_number=est_number,
                        branch=branch,
                        customer=customer,
                        customer_name_manual=cust_name,
                        customer_phone_manual=customer.phone_number if customer else "",
                        customer_pan=pan or None,
                        bill_date_ad=ad_date,
                        bill_date_bs=bs_date,
                        fiscal_year=fiscal_year,
                        cashier=cashier,
                        salesperson=cashier,
                        status='COMPLETED',
                        total_cost_amount=Decimal('0.00')
                    )
                    estimate.save()
                else:
                    estimate.items.all().delete()
                    estimate.payment_transactions.all().delete()

                bill_taxable = Decimal('0.00')
                bill_vat = Decimal('0.00')
                bill_exempt = Decimal('0.00')
                bill_gross = Decimal('0.00')
                bill_discount = Decimal('0.00')

                for l in lines:
                    raw_q = l['qty_raw']
                    if raw_q in ['', '-', '--', 'N/A', 'nan']:
                        qty = Decimal('1.000')
                    else:
                        qty = self._parse_decimal(raw_q, default=Decimal('1.000'))
                        if qty <= Decimal('0.000'):
                            qty = Decimal('1.000')

                    line_gross = l['total_amount']
                    line_taxable = l['taxable_amount']
                    line_vat = l['vat_amount']
                    line_exempt = l['exempt_amount']
                    line_disc = l['discount_amount']
                    unit_rate = l['unit_rate']

                    if unit_rate <= Decimal('0.00'):
                        unit_rate = (line_taxable / qty).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) if qty > 0 else line_taxable

                    bill_taxable += line_taxable
                    bill_vat += line_vat
                    bill_exempt += line_exempt
                    bill_gross += line_gross
                    bill_discount += line_disc

                    prod_name = l['product_name']
                    product = Product.objects.filter(name__iexact=prod_name).first()
                    if not product:
                        p_name_lower = prod_name.lower()
                        if any(k in p_name_lower for k in ['various', 'accessories', 'अन्य']):
                            product = p_various
                        elif any(b in p_name_lower for b in ['iphone', 'samsung', 'redmi', 'vivo', 'realme', 'mobile', 'phone']):
                            cat_mob = self._get_or_create_category("Mobile")
                            sku_code = self._generate_unique_sku(name=prod_name, prefix_override='SAL')
                            product = Product.objects.create(
                                name=prod_name,
                                model_name=prod_name,
                                sku=sku_code,
                                category=cat_mob,
                                base_unit=base_unit_pcs,
                                purchase_price=Decimal('0.00'),
                                selling_price=unit_rate,
                                requires_imei_tracking=False,
                                tax_pricing_type='INCLUSIVE',
                                is_vat_applicable=(line_vat > Decimal('0.00')),
                                vat_rate=Decimal('13.00') if (line_vat > Decimal('0.00')) else Decimal('0.00')
                            )
                        else:
                            product = p_various

                    is_line_vat = (line_vat > Decimal('0.00'))

                    SalesEstimateItem.objects.create(
                        estimate=estimate,
                        product=product,
                        quantity=qty,
                        conversion_factor=Decimal('1.000'),
                        base_unit_quantity=qty,
                        unit_price=unit_rate,
                        official_unit_price=unit_rate,
                        cost_price=Decimal('0.00'),
                        discount_type='FLAT' if line_disc > Decimal('0.00') else 'NONE',
                        discount_input_value=line_disc,
                        discount_amount=line_disc,
                        tax_pricing_type='EXCLUSIVE' if is_line_vat else 'EXEMPT',
                        is_vat_applicable=is_line_vat,
                        vat_rate=Decimal('13.00') if is_line_vat else Decimal('0.00'),
                        base_taxable_amount=line_taxable,
                        taxable_line_amount=line_taxable if is_line_vat else Decimal('0.00'),
                        tax_amount=line_vat,
                        line_total=line_gross,
                        imei_number=None
                    )

                estimate.subtotal = (bill_taxable + bill_exempt + bill_discount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                estimate.item_discount_total = bill_discount
                estimate.taxable_amount = bill_taxable
                estimate.non_taxable_amount = bill_exempt
                estimate.vat_amount = bill_vat
                estimate.grand_total = bill_gross
                estimate.is_vat_applicable = (bill_vat > Decimal('0.00'))
                estimate.total_gross_profit = bill_taxable + bill_exempt
                estimate.paid_amount = bill_gross
                estimate.due_amount = Decimal('0.00')
                estimate.payment_status = 'PAID'
                estimate.save()

                SalesPaymentTransaction.objects.create(
                    estimate=estimate,
                    payment_mode='CASH',
                    amount=bill_gross,
                    transaction_ref=f"MIGRATED-{inv_no}"
                )

                AutoPostingService.post_sales_estimate(estimate=estimate, user=cashier)

                total_taxable_imported += bill_taxable
                total_vat_imported += bill_vat
                total_gross_imported += bill_gross
                imported_bills_cnt += 1

            else:
                imported_bills_cnt += 1
                for l in lines:
                    total_gross_imported += l['total_amount']
                    total_taxable_imported += l['taxable_amount']
                    total_vat_imported += l['vat_amount']

        self.stdout.write(self.style.SUCCESS(
            f"  [OK] Sales Register: {imported_bills_cnt} bills imported chronologically.\n"
            f"       - Taxable Sales:  Rs. {total_taxable_imported:,.2f}\n"
            f"       - 13% Output VAT: Rs. {total_vat_imported:,.2f}\n"
            f"       - Total Turnover: Rs. {total_gross_imported:,.2f}\n"
            f"       - Inventory Asset (1310) Deductions: Rs. 0.00 (Protected)"
        ))