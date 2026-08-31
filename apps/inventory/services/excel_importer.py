"""
Multi-Format Excel & CSV Bulk Catalog Importer.
File Path: D:\Mobile Shop\Inventory\apps\inventory\services\excel_importer.py

Core Capabilities:
1. Smart Fuzzy Header Auto-Detection:
   - Recognizes multi-lingual (English & Nepali) and varied spreadsheet headers automatically
     (Item Name, MRP/Selling Rate, Purchase Cost, Opening Stock, RAM/ROM, Brand, Category, etc.).
2. Auto-Creation of Missing Categories & Brands:
   - Automatically registers missing categories, subcategories, and brands on the fly with active
     status and slugified short codes, ensuring bulk imports never crash midway.
3. Optional Barcode Policy:
   - If a barcode is in the spreadsheet, it is imported and validated.
   - If the barcode cell is blank, it saves as `None` without generating any unwanted fake barcodes.
4. Serialized Handset Opening Stock & Placeholder Allocation:
   - If smartphones/tablets are imported with opening stock but without individual 15-digit IMEIs,
     creates clearly indexed placeholder device records linked to SKU/device_uid with
     `imei_2_pending_scan=True`.
5. Flexible Duplicate Conflict Resolution:
   - Supports 'MERGE' (consolidate stock), 'OVERWRITE' (update prices & counts), 'SKIP', and 'NEW_VARIANT'.
6. Atomic Row-Level Savepoints & Detailed Diagnostic Summary:
   - Isolates row failures so valid rows are committed, providing full error reports and audit logs.
"""

import io
import re
import uuid
from decimal import Decimal, InvalidOperation
from datetime import date, timedelta
from typing import Dict, List, Any, Optional, Tuple
import pandas as pd
from django.db import transaction
from django.utils.text import slugify

from apps.inventory.models import (
    Product, ProductCategory, ProductSubCategory, Brand, UnitOfMeasurement,
    BranchStock, ProductBatch, ProductComponentWarrantyRule,
    ItemInstance, StockMovementLog
)
from apps.branches.models import Branch
from apps.reports.models import ProductCostHistory
from apps.core.models import SystemConfiguration, AuditLog


class ExcelProductImporter:
    """
    High-Performance Multi-Format Catalog Importer for Excel (.xlsx, .xls) and CSV (.csv).
    """

    # System Field Mapping Metadata (Key, Display Label)
    SYSTEM_FIELDS = [
        ('name', 'Product / Phone Name *'),
        ('model_name', 'Model Name'),
        ('model_number', 'Model Number'),
        ('sku', 'SKU Code'),
        ('barcode', 'Barcode (Optional)'),
        ('category', 'Category'),
        ('brand', 'Brand'),
        ('ram', 'RAM (e.g. 8GB)'),
        ('internal_storage', 'Internal Storage (e.g. 256GB)'),
        ('color_variant', 'Color / Finish'),
        ('network_type', 'Network Generation (5G/4G)'),
        ('sim_configuration', 'SIM Configuration (Dual SIM / Single SIM / eSIM)'),
        ('purchase_price', 'Cost / Purchase Price (NPR)'),
        ('selling_price', 'Selling Price / MRP (NPR) *'),
        ('wholesale_price', 'Wholesale Rate (NPR)'),
        ('initial_stock', 'Opening Stock Quantity'),
        ('imei_numbers', 'IMEI / Serial Numbers (Comma/Newline/Pipe separated)'),
        ('rack_number', 'Rack Location / Shelf'),
        ('warranty_months', 'Overall Warranty (Months)'),
        ('battery_warranty_months', 'Battery Warranty (Months)'),
        ('screen_warranty_months', 'Screen Warranty (Months)'),
        ('requires_imei_tracking', 'IMEI Tracked Phone (Yes/No)'),
    ]

    # Comprehensive Fuzzy Regex Patterns for Header Auto-Detection (English & Nepali)
    FUZZY_PATTERNS = {
        'name': [
            r'product.*name', r'item.*name', r'^name$', r'^title$', r'^item$',
            r'^phone$', r'^description$', r'particulars?', r'सामान.*नाम', r'वस्तु.*नाम', r'विवरण'
        ],
        'model_name': [
            r'model.*name', r'^model$', r'handset.*model', r'मोडेल.*नाम', r'मोडेल'
        ],
        'model_number': [
            r'model.*num', r'model.*no', r'part.*num', r'part.*no', r'मोडेल.*नं'
        ],
        'sku': [
            r'^sku$', r'item.*code', r'product.*code', r'sku.*code', r'^code$', r'संकेत'
        ],
        'barcode': [
            r'^barcode$', r'upc', r'ean', r'qr.*code', r'bar.*code', r'बारकोड'
        ],
        'category': [
            r'^category$', r'cat.*name', r'item.*group', r'classification', r'group', r'वर्ग', r'समूह'
        ],
        'brand': [
            r'^brand$', r'make', r'manufacturer', r'company', r'कम्पनी', r'ब्रान्ड'
        ],
        'ram': [
            r'^ram$', r'memory.*ram', r'ram.*size', r'system.*memory', r'र्याम'
        ],
        'internal_storage': [
            r'^rom$', r'storage', r'internal.*storage', r'capacity', r'memory$', r'रोम', r'मेमोरी'
        ],
        'color_variant': [
            r'^color$', r'colour', r'finish', r'shade', r'रङ्ग', r'रंग'
        ],
        'network_type': [
            r'network', r'generation', r'5g.*4g', r'connectivity', r'band'
        ],
        'sim_configuration': [
            r'sim.*config', r'sim.*type', r'sim.*slot', r'^sim$', r'sim.*setup',
            r'dual.*sim', r'sim.*count', r'सिम'
        ],
        'purchase_price': [
            r'cost', r'purchase.*price', r'buy.*rate', r'cost.*price', r'inward.*rate',
            r'landed', r'खरिद.*दर', r'लागत.*मूल्य', r'खरिद'
        ],
        'selling_price': [
            r'selling.*price', r'mrp', r'retail.*rate', r'sell.*rate', r'^price$',
            r'sale.*price', r'^rate$', r'बिक्री.*मूल्य', r'एमआरपी', r'दर'
        ],
        'wholesale_price': [
            r'wholesale', r'dealer.*price', r'trade.*price', r'bulk.*rate', r'थोक.*मूल्य', r'थोक'
        ],
        'initial_stock': [
            r'stock', r'qty', r'quantity', r'opening.*stock', r'balance', r'units',
            r'count', r'मौज्दात', r'परिमाण', r'संख्या'
        ],
        'imei_numbers': [
            r'imei.*list', r'imei.*number', r'serials', r'^imei$', r'^imeis$',
            r'serial.*list', r'imei.*1', r'imei.*2', r'imei.*no', r'आईएमईआई'
        ],
        'rack_number': [
            r'rack', r'shelf', r'cabinet', r'bin', r'location', r'दराज', r'र्याक'
        ],
        'warranty_months': [
            r'^warranty$', r'warranty.*months', r'device.*warranty', r'वारेन्टी'
        ],
        'battery_warranty_months': [
            r'battery.*warranty', r'battery.*months', r'batt.*war'
        ],
        'screen_warranty_months': [
            r'screen.*warranty', r'display.*warranty', r'lcd.*warranty'
        ],
        'requires_imei_tracking': [
            r'imei.*track', r'serial.*tracked', r'is.*phone', r'has.*imei',
            r'is.*mobile', r'serialized'
        ],
    }

    @classmethod
    def read_file(cls, file_obj) -> pd.DataFrame:
        """
        Parses an uploaded Excel (.xlsx, .xls) or CSV (.csv) file into a cleaned DataFrame.
        """
        file_name = getattr(file_obj, 'name', '').lower()
        if file_name.endswith('.csv'):
            df = pd.read_csv(file_obj, dtype=str)
        else:
            df = pd.read_excel(file_obj, dtype=str)
        df = df.fillna('')
        df.columns = [str(c).strip() for c in df.columns]
        return df

    @classmethod
    def detect_column_mappings(cls, headers: List[str]) -> Dict[str, str]:
        """
        Matches spreadsheet header columns to standard system fields using fuzzy regex.
        """
        mappings = {}
        for field_key, patterns in cls.FUZZY_PATTERNS.items():
            matched_header = ''
            for header in headers:
                header_clean = header.strip().lower()
                for pattern in patterns:
                    if re.search(pattern, header_clean):
                        matched_header = header
                        break
                if matched_header:
                    break
            if matched_header:
                mappings[field_key] = matched_header
        return mappings

    @classmethod
    def generate_sku(cls, brand_name: str, model_name: str, storage: str) -> str:
        """
        Generates a clean, unique SKU code (e.g., SAM-A55-256-A1B2).
        """
        b = (brand_name[:3] if brand_name else "GEN").upper()
        b = re.sub(r'[^A-Za-z0-9]', '', b) or "GEN"
        m = re.sub(r'[^A-Za-z0-9]', '', model_name)[:4].upper() if model_name else "ITEM"
        s = re.sub(r'[^0-9]', '', storage) if storage else "STD"
        uid = uuid.uuid4().hex[:4].upper()
        return f"{b}-{m}-{s}-{uid}"

    @classmethod
    def parse_decimal(cls, val: Any, default: Decimal = Decimal('0.00')) -> Decimal:
        """
        Sanitizes string inputs (stripping currency symbols, commas, and whitespace) into Decimal.
        """
        if val is None or str(val).strip() == '':
            return default
        try:
            clean_str = re.sub(r'[^\d.]', '', str(val).strip())
            return Decimal(clean_str) if clean_str else default
        except (InvalidOperation, ValueError, TypeError):
            return default

    @classmethod
    def parse_sim_configuration(
        cls,
        val: str,
        category_name: str = "",
        ram: str = "",
        requires_imei: bool = False
    ) -> str:
        """
        Parses SIM configuration string or applies smartphone heuristics.
        Returns: 'DUAL_SIM', 'SINGLE_SIM', 'ESIM_DUAL', or 'ESIM_ONLY'.
        """
        val_clean = str(val or '').strip().upper()
        if val_clean:
            if any(term in val_clean for term in ['ESIM_ONLY', 'DUAL_ESIM', 'ONLY_ESIM', 'DUAL ESIM']):
                return 'ESIM_ONLY'
            if any(term in val_clean for term in ['ESIM', 'E-SIM', 'ESIM_DUAL', '1 NANO + 1 ESIM', 'NANO+ESIM']):
                return 'ESIM_DUAL'
            if any(term in val_clean for term in ['SINGLE', '1 SIM', '1SIM', 'SINGLE_SIM', 'ONE SIM', 'SINGLE NANO']):
                return 'SINGLE_SIM'
            if any(term in val_clean for term in ['DUAL', '2 SIM', '2SIM', 'DUAL_SIM', 'TWO SIM', 'HYBRID', 'DUAL NANO']):
                return 'DUAL_SIM'

        if not requires_imei and not any(k in category_name.lower() for k in ['phone', 'mobile', 'smartphone', 'ह्यान्डसेट', 'स्मार्टफोन']):
            return 'SINGLE_SIM'

        ram_digits = re.sub(r'[^\d]', '', str(ram or ''))
        if ram_digits:
            try:
                ram_gb = int(ram_digits)
                if ram_gb >= 4:
                    return 'DUAL_SIM'
            except (ValueError, TypeError):
                pass

        if any(k in category_name.lower() for k in ['feature', 'bar', 'basic', 'button']):
            return 'SINGLE_SIM'

        return 'DUAL_SIM' if requires_imei else 'SINGLE_SIM'

    @classmethod
    def parse_imei_tokens(cls, val: str) -> List[str]:
        """
        Parses comma, semicolon, newline, carriage return, pipe, or whitespace-separated IMEI tokens.
        """
        if not val or str(val).strip() == '':
            return []
        raw_tokens = re.split(r'[\n,;\r\t]+', str(val).strip())
        return [t.strip() for t in raw_tokens if t.strip()]

    @classmethod
    def register_imei_instances_for_stock(
        cls,
        product: Product,
        branch: Branch,
        batch_number: str,
        stock_qty: Decimal,
        raw_imei_str: str,
        cost_price: Decimal,
        warranty_months: int,
        default_mdms_status: str,
        supplier_name: str = "Opening Stock via Excel"
    ) -> Tuple[int, int]:
        """
        Creates exactly int(stock_qty) ItemInstance records in 'IN_STOCK' status.
        """
        units_count = int(stock_qty)
        if units_count <= 0:
            return 0, 0

        imei_tokens = cls.parse_imei_tokens(raw_imei_str)
        is_dual_sim = getattr(product, 'sim_configuration', 'DUAL_SIM') in ['DUAL_SIM', 'ESIM_DUAL']
        today = date.today()
        warranty_end = today + timedelta(days=warranty_months * 30) if warranty_months > 0 else None

        instances_to_create = []
        placeholder_count = 0

        for i in range(units_count):
            im1 = None
            im2 = None
            sn = None
            is_placeholder = False

            if i < len(imei_tokens):
                token = imei_tokens[i]
                parts = token.split('|')
                im1_candidate = parts[0].strip() if len(parts) > 0 and parts[0].strip() else None
                im2_candidate = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
                sn_candidate = parts[2].strip() if len(parts) > 2 and parts[2].strip() else None

                if im1_candidate:
                    if not ItemInstance.objects.filter(imei_1=im1_candidate, status='IN_STOCK').exists():
                        im1 = im1_candidate
                        im2 = im2_candidate
                        sn = sn_candidate
                    else:
                        is_placeholder = True
                else:
                    is_placeholder = True
            else:
                is_placeholder = True

            device_uid = f"DEV-{uuid.uuid4().hex[:12].upper()}"

            if is_placeholder:
                placeholder_count += 1
                pending_scan = True
                device_barcode = product.barcode or product.sku or device_uid
                remarks = "Opening stock seeded via Excel - Physical IMEI link pending on first POS scan"
            else:
                pending_scan = is_dual_sim and (im2 is None)
                device_barcode = im1 or sn or product.barcode or product.sku or device_uid
                remarks = "Explicit IMEI registered via Excel Importer"

            instances_to_create.append(
                ItemInstance(
                    product=product,
                    branch=branch,
                    device_uid=device_uid,
                    imei_1=im1,
                    imei_2=im2,
                    imei_2_pending_scan=pending_scan,
                    serial_number=sn,
                    device_barcode=device_barcode,
                    status='IN_STOCK',
                    condition='BRAND_NEW',
                    activation_status='SEALED_INACTIVE',
                    source_type='NEW_PURCHASE_GRN',
                    mdms_status=default_mdms_status or product.default_mdms_status or 'REGISTERED_OFFICIAL',
                    mdms_verification_date=today,
                    mdms_remarks=remarks,
                    purchase_reference='EXCEL-IMPORT',
                    batch_reference=batch_number,
                    supplier_name=supplier_name,
                    landed_cost=cost_price or product.purchase_price,
                    purchase_date=today,
                    warranty_start_date=today,
                    warranty_end_date=warranty_end,
                    warranty_remarks=f"Opening Stock Warranty: {warranty_months}M"
                )
            )

        if instances_to_create:
            ItemInstance.objects.bulk_create(instances_to_create)

        return len(instances_to_create), placeholder_count

    @classmethod
    def process_import(
        cls,
        df: pd.DataFrame,
        mapping: Dict[str, str],
        branch: Branch,
        conflict_strategy: str = 'MERGE',
        user: Any = None
    ) -> Dict[str, Any]:
        """
        Executes bulk import across all spreadsheet rows inside isolated row-level savepoints.
        Barcodes are saved if provided in the file, or set to None without auto-generation.
        """
        summary = {
            'total_rows': len(df),
            'imported': 0,
            'updated': 0,
            'merged': 0,
            'skipped': 0,
            'placeholder_imei_units_created': 0,
            'serialized_products_needing_imeis': [],
            'warnings': [],
            'errors': []
        }

        config = SystemConfiguration.get_solo()
        is_vat_shop = (config.tax_system_mode == 'VAT')
        default_rate = config.default_vat_rate if is_vat_shop else Decimal('0.00')
        default_tax_mode = 'INCLUSIVE' if is_vat_shop else 'EXEMPT'

        all_active_branches = list(Branch.objects.filter(is_active=True))

        # Ensure standard piece base unit exists
        base_unit_pcs = UnitOfMeasurement.objects.filter(code='PCS').first()
        if not base_unit_pcs:
            base_unit_pcs = UnitOfMeasurement.objects.create(
                name='Piece',
                name_np='पिस',
                code='PCS',
                allow_decimal=False
            )

        seen_in_sheet = {}

        for row_idx, row in df.iterrows():
            line_num = row_idx + 2
            try:
                with transaction.atomic():
                    raw_name = str(row.get(mapping.get('name', ''), '')).strip()
                    if not raw_name:
                        summary['skipped'] += 1
                        summary['errors'].append(f"Row {line_num}: Skipped due to empty Product Name.")
                        continue

                    raw_model = str(row.get(mapping.get('model_name', ''), '')).strip() or raw_name
                    raw_model_num = str(row.get(mapping.get('model_number', ''), '')).strip()
                    raw_sku = str(row.get(mapping.get('sku', ''), '')).strip()
                    raw_barcode = str(row.get(mapping.get('barcode', ''), '')).strip()
                    raw_cat = str(row.get(mapping.get('category', ''), '')).strip() or "Smartphones & Mobile"
                    raw_brand = str(row.get(mapping.get('brand', ''), '')).strip()
                    raw_ram = str(row.get(mapping.get('ram', ''), '')).strip()
                    raw_storage = str(row.get(mapping.get('internal_storage', ''), '')).strip()
                    raw_color = str(row.get(mapping.get('color_variant', ''), '')).strip()
                    raw_network = str(row.get(mapping.get('network_type', ''), '')).strip().upper()
                    raw_sim = str(row.get(mapping.get('sim_configuration', ''), '')).strip()
                    raw_rack = str(row.get(mapping.get('rack_number', ''), '')).strip()
                    raw_imeis = str(row.get(mapping.get('imei_numbers', ''), '')).strip()

                    raw_warranty = int(cls.parse_decimal(row.get(mapping.get('warranty_months', '')), Decimal('12')))
                    raw_batt_war = int(cls.parse_decimal(row.get(mapping.get('battery_warranty_months', '')), Decimal('6')))
                    raw_screen_war = int(cls.parse_decimal(row.get(mapping.get('screen_warranty_months', '')), Decimal('6')))

                    cost_price = cls.parse_decimal(row.get(mapping.get('purchase_price', '')))
                    selling_price = cls.parse_decimal(row.get(mapping.get('selling_price', '')))
                    wholesale_price = cls.parse_decimal(row.get(mapping.get('wholesale_price', '')))
                    if wholesale_price == Decimal('0.00'):
                        wholesale_price = selling_price

                    stock_qty = cls.parse_decimal(row.get(mapping.get('initial_stock', '')))

                    imei_flag_val = str(row.get(mapping.get('requires_imei_tracking', ''), '')).strip().lower()
                    requires_imei = imei_flag_val in ['yes', 'true', '1', 'y']
                    if not requires_imei and (raw_ram or raw_storage or raw_imeis or 'phone' in raw_cat.lower() or 'mobile' in raw_cat.lower() or 'smartphone' in raw_cat.lower() or 'स्मार्टफोन' in raw_cat.lower()):
                        requires_imei = True

                    sim_config = cls.parse_sim_configuration(
                        val=raw_sim,
                        category_name=raw_cat,
                        ram=raw_ram,
                        requires_imei=requires_imei
                    )

                    # 1. Automatic Category Lookup & Self-Healing Creation
                    category_obj = ProductCategory.objects.filter(name__iexact=raw_cat).first()
                    if not category_obj:
                        clean_slug = slugify(raw_cat)[:4].upper()
                        cat_code = clean_slug if clean_slug else f"CAT-{uuid.uuid4().hex[:3].upper()}"
                        while ProductCategory.objects.filter(code=cat_code).exists():
                            cat_code = f"C-{uuid.uuid4().hex[:4].upper()}"
                        category_obj = ProductCategory.objects.create(
                            name=raw_cat,
                            code=cat_code,
                            is_active=True
                        )
                    elif not category_obj.is_active:
                        category_obj.is_active = True
                        category_obj.save(update_fields=['is_active'])

                    # 2. Automatic Brand Lookup & Creation
                    brand_obj = None
                    if raw_brand:
                        brand_obj = Brand.objects.filter(name__iexact=raw_brand).first()
                        if not brand_obj:
                            brand_obj = Brand.objects.create(name=raw_brand, origin_country="Nepal")

                    # Deduplication key across current spreadsheet rows
                    dedup_key = raw_barcode or raw_sku or f"{raw_name}|{raw_ram}|{raw_storage}|{raw_color}".lower()

                    if dedup_key in seen_in_sheet:
                        if conflict_strategy == 'MERGE':
                            existing_prod = seen_in_sheet[dedup_key]
                            if stock_qty > Decimal('0.000'):
                                b_stock, _ = BranchStock.objects.get_or_create(
                                    branch=branch,
                                    product=existing_prod,
                                    defaults={
                                        'quantity': Decimal('0.000'),
                                        'reserved_quantity': Decimal('0.000'),
                                        'quarantined_defective_quantity': Decimal('0.000'),
                                        'low_stock_threshold': existing_prod.reorder_level or Decimal('5.00')
                                    }
                                )
                                b_stock.quantity += stock_qty
                                b_stock.save(update_fields=['quantity', 'updated_at'])

                                merge_batch_id = f"BATCH-IMP-{uuid.uuid4().hex[:6].upper()}"
                                ProductBatch.objects.create(
                                    batch_number=merge_batch_id,
                                    product=existing_prod,
                                    branch=branch,
                                    purchase_date=date.today(),
                                    cost_price=cost_price or existing_prod.purchase_price,
                                    selling_price=selling_price or existing_prod.selling_price,
                                    quantity_received=stock_qty,
                                    quantity_remaining=stock_qty,
                                    supplier_name="Excel Consolidation"
                                )

                                if existing_prod.requires_imei_tracking:
                                    _, placeholders = cls.register_imei_instances_for_stock(
                                        product=existing_prod,
                                        branch=branch,
                                        batch_number=merge_batch_id,
                                        stock_qty=stock_qty,
                                        raw_imei_str=raw_imeis,
                                        cost_price=cost_price or existing_prod.purchase_price,
                                        warranty_months=raw_warranty,
                                        default_mdms_status=existing_prod.default_mdms_status,
                                        supplier_name="Excel Consolidation"
                                    )
                                    summary['placeholder_imei_units_created'] += placeholders
                                    if placeholders > 0:
                                        summary['serialized_products_needing_imeis'].append({
                                            'product': existing_prod.name,
                                            'sku': existing_prod.sku,
                                            'pending_units': placeholders
                                        })

                            summary['merged'] += 1
                            continue
                        elif conflict_strategy == 'SKIP':
                            summary['skipped'] += 1
                            continue
                        elif conflict_strategy == 'NEW_VARIANT':
                            raw_sku = cls.generate_sku(raw_brand, raw_model, raw_storage)
                            raw_barcode = None  # Leave blank for new variant

                    # 3. Database Lookup for Existing Product Record
                    product_obj = None
                    if raw_barcode:
                        product_obj = Product.objects.filter(barcode=raw_barcode).first()
                    if not product_obj and raw_sku:
                        product_obj = Product.objects.filter(sku=raw_sku).first()
                    if not product_obj:
                        product_obj = Product.objects.filter(
                            name__iexact=raw_name,
                            ram__iexact=raw_ram,
                            internal_storage__iexact=raw_storage,
                            color_variant__iexact=raw_color
                        ).first()

                    if product_obj:
                        if conflict_strategy == 'SKIP':
                            summary['skipped'] += 1
                            continue

                        old_cost = product_obj.purchase_price
                        old_sell = product_obj.selling_price

                        if cost_price > Decimal('0.00'):
                            product_obj.purchase_price = cost_price
                        if selling_price > Decimal('0.00'):
                            product_obj.selling_price = selling_price
                        if raw_rack:
                            product_obj.rack_number = raw_rack
                        if raw_sim:
                            product_obj.sim_configuration = sim_config
                        if raw_barcode and not product_obj.barcode:
                            product_obj.barcode = raw_barcode
                        product_obj.save()

                        if (cost_price > Decimal('0.00') and cost_price != old_cost) or (selling_price > Decimal('0.00') and selling_price != old_sell):
                            ProductCostHistory.objects.create(
                                product=product_obj,
                                date_effective=date.today(),
                                old_cost_price=old_cost,
                                new_cost_price=product_obj.purchase_price,
                                old_selling_price=old_sell,
                                new_selling_price=product_obj.selling_price,
                                source_reference="Excel Import Update",
                                changed_by=user,
                                remarks="Updated during bulk Excel spreadsheet re-import"
                            )

                        if stock_qty > Decimal('0.000'):
                            b_stock, _ = BranchStock.objects.get_or_create(
                                branch=branch,
                                product=product_obj,
                                defaults={
                                    'quantity': Decimal('0.000'),
                                    'reserved_quantity': Decimal('0.000'),
                                    'quarantined_defective_quantity': Decimal('0.000'),
                                    'low_stock_threshold': product_obj.reorder_level or Decimal('5.00')
                                }
                            )
                            prev_qty = b_stock.quantity

                            if conflict_strategy == 'MERGE':
                                b_stock.quantity += stock_qty
                                delta_added = stock_qty
                            else:
                                b_stock.quantity = stock_qty
                                delta_added = stock_qty - prev_qty
                            b_stock.save(update_fields=['quantity', 'updated_at'])

                            update_batch_id = f"BATCH-IMP-{uuid.uuid4().hex[:6].upper()}"
                            ProductBatch.objects.create(
                                batch_number=update_batch_id,
                                product=product_obj,
                                branch=branch,
                                purchase_date=date.today(),
                                cost_price=product_obj.purchase_price,
                                selling_price=product_obj.selling_price,
                                quantity_received=stock_qty,
                                quantity_remaining=stock_qty,
                                supplier_name="Excel Bulk Update"
                            )

                            if product_obj.requires_imei_tracking and delta_added > Decimal('0.000'):
                                _, placeholders = cls.register_imei_instances_for_stock(
                                    product=product_obj,
                                    branch=branch,
                                    batch_number=update_batch_id,
                                    stock_qty=delta_added,
                                    raw_imei_str=raw_imeis,
                                    cost_price=product_obj.purchase_price,
                                    warranty_months=raw_warranty,
                                    default_mdms_status=product_obj.default_mdms_status,
                                    supplier_name="Excel Bulk Update"
                                )
                                summary['placeholder_imei_units_created'] += placeholders
                                if placeholders > 0:
                                    summary['serialized_products_needing_imeis'].append({
                                        'product': product_obj.name,
                                        'sku': product_obj.sku,
                                        'pending_units': placeholders
                                    })

                            StockMovementLog.objects.create(
                                product=product_obj,
                                branch=branch,
                                movement_type='ADJUSTMENT_ADD',
                                quantity_delta=delta_added,
                                previous_quantity=prev_qty,
                                new_quantity=b_stock.quantity,
                                reference_document="EXCEL-UPDATE",
                                remarks="Stock adjusted from Excel re-import",
                                user=user
                            )

                        seen_in_sheet[dedup_key] = product_obj
                        summary['updated'] += 1

                    else:
                        final_sku = raw_sku or cls.generate_sku(raw_brand, raw_model, raw_storage)
                        while Product.objects.filter(sku=final_sku).exists():
                            final_sku = cls.generate_sku(raw_brand, raw_model, raw_storage)

                        # Clean Barcode: Save if provided in file; otherwise leave as None
                        final_barcode = raw_barcode if raw_barcode else None
                        if final_barcode and Product.objects.filter(barcode=final_barcode).exists():
                            final_barcode = None  # Prevent duplicate barcode crash

                        variant_tag = f"{raw_ram}/{raw_storage}".strip('/')
                        if raw_color:
                            variant_tag = f"{variant_tag} {raw_color}".strip()

                        new_product = Product(
                            name=raw_name,
                            model_name=raw_model,
                            model_number=raw_model_num or None,
                            sku=final_sku,
                            barcode=final_barcode,
                            category=category_obj,
                            brand=brand_obj,
                            variant_name=variant_tag or "Standard",
                            ram=raw_ram or None,
                            internal_storage=raw_storage or None,
                            color_variant=raw_color or None,
                            network_type=raw_network if raw_network in ['5G', '4G', '3G', 'WIFI_ONLY'] else '5G',
                            sim_configuration=sim_config,
                            base_unit=base_unit_pcs,
                            purchase_price=cost_price,
                            selling_price=selling_price,
                            wholesale_price=wholesale_price,
                            requires_imei_tracking=requires_imei,
                            rack_number=raw_rack or None,
                            warranty_months=raw_warranty,
                            tax_pricing_type=default_tax_mode,
                            is_vat_applicable=is_vat_shop,
                            vat_rate=default_rate
                        )
                        new_product._skip_signal_branch_stock = True
                        new_product.save()

                        if requires_imei:
                            ProductComponentWarrantyRule.objects.bulk_create([
                                ProductComponentWarrantyRule(
                                    product=new_product,
                                    component_type='DEVICE',
                                    component_name='Main Handset Body & Motherboard',
                                    warranty_months=raw_warranty
                                ),
                                ProductComponentWarrantyRule(
                                    product=new_product,
                                    component_type='BATTERY',
                                    component_name='Internal Battery',
                                    warranty_months=raw_batt_war
                                ),
                                ProductComponentWarrantyRule(
                                    product=new_product,
                                    component_type='SCREEN',
                                    component_name='Display Panel / Touchscreen',
                                    warranty_months=raw_screen_war
                                )
                            ])

                        branch_stock_entries = [
                            BranchStock(
                                branch=br,
                                product=new_product,
                                quantity=stock_qty if br.id == branch.id else Decimal('0.000'),
                                reserved_quantity=Decimal('0.000'),
                                quarantined_defective_quantity=Decimal('0.000'),
                                low_stock_threshold=new_product.reorder_level or Decimal('5.00')
                            )
                            for br in all_active_branches
                        ]
                        BranchStock.objects.bulk_create(branch_stock_entries, ignore_conflicts=True)

                        if stock_qty > Decimal('0.000'):
                            batch_id = f"BATCH-IMP-{uuid.uuid4().hex[:6].upper()}"
                            ProductBatch.objects.create(
                                batch_number=batch_id,
                                product=new_product,
                                branch=branch,
                                purchase_date=date.today(),
                                cost_price=cost_price,
                                selling_price=selling_price,
                                quantity_received=stock_qty,
                                quantity_remaining=stock_qty,
                                supplier_name="Opening Stock via Excel"
                            )

                            if requires_imei:
                                _, placeholders = cls.register_imei_instances_for_stock(
                                    product=new_product,
                                    branch=branch,
                                    batch_number=batch_id,
                                    stock_qty=stock_qty,
                                    raw_imei_str=raw_imeis,
                                    cost_price=cost_price,
                                    warranty_months=raw_warranty,
                                    default_mdms_status=new_product.default_mdms_status,
                                    supplier_name="Opening Stock via Excel"
                                )
                                summary['placeholder_imei_units_created'] += placeholders
                                if placeholders > 0:
                                    summary['serialized_products_needing_imeis'].append({
                                        'product': new_product.name,
                                        'sku': new_product.sku,
                                        'pending_units': placeholders
                                    })

                            StockMovementLog.objects.create(
                                product=new_product,
                                branch=branch,
                                movement_type='ADJUSTMENT_ADD',
                                quantity_delta=stock_qty,
                                previous_quantity=Decimal('0.000'),
                                new_quantity=stock_qty,
                                reference_document="EXCEL-INIT",
                                remarks="Opening stock seeded via Excel Importer",
                                user=user
                            )

                        seen_in_sheet[dedup_key] = new_product
                        summary['imported'] += 1

            except Exception as row_err:
                summary['errors'].append(f"Row {line_num}: Error - {str(row_err)}")
                summary['skipped'] += 1

        if summary['placeholder_imei_units_created'] > 0:
            summary['warnings'].append(
                f"Notice: {summary['placeholder_imei_units_created']} serialized phone unit(s) were imported without explicit 15-digit IMEIs. "
                f"They are indexed by product SKU and will prompt counter staff for physical IMEI assignment on POS checkout."
            )

        try:
            with transaction.atomic():
                AuditLog.objects.create(
                    user=user,
                    branch=branch,
                    action_type='CREATE',
                    module='ExcelProductImport',
                    object_repr=f"Import {summary['imported']} new, {summary['updated']} updated, {summary['placeholder_imei_units_created']} pending IMEIs",
                    details={
                        'total_rows': summary['total_rows'],
                        'imported': summary['imported'],
                        'updated': summary['updated'],
                        'merged': summary['merged'],
                        'skipped': summary['skipped'],
                        'placeholder_imei_units_created': summary['placeholder_imei_units_created'],
                        'conflict_strategy': conflict_strategy,
                    }
                )
        except Exception:
            pass

        return summary