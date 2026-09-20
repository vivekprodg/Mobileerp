"""
Management Command: seed_chart_of_accounts
File Path: apps/accounting/management/commands/seed_chart_of_accounts.py

Initializes an industry-standard Chart of Accounts (COA) tailored specifically
for Nepal's retail and wholesale smartphone, electronics, and optical ecosystems.

Pre-populates:
1. Standard Account Groups (Assets, Liabilities, Equity, Revenue, Direct Costs, Operating Expenses)
2. All 34 master general ledger accounts with accurate Nepali translations and system tags:
   - Dedicated digital clearing accounts: FonePay (1130), eSewa (1140), Khalti (1150), Card POS (1160)
   - Fixed asset contra-accounts: Accumulated Depreciation (1520)
   - Liabilities & Equity: Bank Term Loans (2510), Owner Drawings (3130)
   - Operational revenue & expenses: Inventory Surplus (4030), Gateway MDR Fees (6190), Interest Expense (6210)
3. Binds control accounts directly into SystemConfiguration singleton for automated posting
4. Nepali Fiscal Years (2080/81, 2081/82, 2082/83) and their 12 monthly financial periods
"""

from decimal import Decimal
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.accounting.models import (
    AccountGroup, Account, AccountingFiscalYear, FinancialPeriod
)
from apps.branches.models import Branch
from apps.core.models import SystemConfiguration
from apps.core.nepali_calendar import NepaliCalendar


class Command(BaseCommand):
    help = "Seeds standard double-entry Chart of Accounts, fiscal years, and system ledger bindings."

    def add_arguments(self, parser):
        parser.add_argument(
            '--branch',
            type=str,
            help='Specific branch code to bind accounts to (optional; defaults to organization-wide / main branch).'
        )
        parser.add_argument(
            '--reset',
            action='store_true',
            help='Wipes and re-creates non-transacted accounts (use with caution).'
        )

    @transaction.atomic
    def handle(self, *args, **options):
        self.stdout.write(self.style.NOTICE("Initializing Nepal Standard Chart of Accounts..."))

        branch_code = options.get('branch')
        target_branch = None
        if branch_code:
            target_branch = Branch.objects.filter(code=branch_code).first()
            if not target_branch:
                self.stdout.write(self.style.ERROR(f"Branch with code '{branch_code}' not found."))
                return
        else:
            target_branch = Branch.objects.filter(is_main_branch=True).first() or Branch.objects.first()

        # =========================================================================
        # 1. ROOT & SUB-ACCOUNT GROUPS
        # =========================================================================
        self.stdout.write("--> Creating Account Groups...")

        groups_data = [
            # 1000 Assets
            {'code': '1000', 'name': 'Assets', 'name_np': 'सम्पत्ति', 'category': 'ASSET', 'nature': 'DEBIT', 'parent': None},
            {'code': '1100', 'name': 'Current Assets', 'name_np': 'चालु सम्पत्ति', 'category': 'ASSET', 'nature': 'DEBIT', 'parent': '1000'},
            {'code': '1500', 'name': 'Fixed / Non-Current Assets', 'name_np': 'स्थिर सम्पत्ति', 'category': 'ASSET', 'nature': 'DEBIT', 'parent': '1000'},

            # 2000 Liabilities
            {'code': '2000', 'name': 'Liabilities', 'name_np': 'दायित्व', 'category': 'LIABILITY', 'nature': 'CREDIT', 'parent': None},
            {'code': '2100', 'name': 'Current Liabilities', 'name_np': 'चालु दायित्व', 'category': 'LIABILITY', 'nature': 'CREDIT', 'parent': '2000'},
            {'code': '2500', 'name': 'Long-Term Liabilities', 'name_np': 'दीर्घकालीन दायित्व', 'category': 'LIABILITY', 'nature': 'CREDIT', 'parent': '2000'},

            # 3000 Equity
            {'code': '3000', 'name': 'Equity & Capital', 'name_np': 'पुँजी तथा कोष', 'category': 'EQUITY', 'nature': 'CREDIT', 'parent': None},
            {'code': '3100', 'name': 'Proprietor Capital & Reserves', 'name_np': 'साहुको पुँजी कोष', 'category': 'EQUITY', 'nature': 'CREDIT', 'parent': '3000'},

            # 4000 Revenue
            {'code': '4000', 'name': 'Operating Revenue', 'name_np': 'सञ्चालन आम्दानी', 'category': 'REVENUE', 'nature': 'CREDIT', 'parent': None},
            {'code': '4100', 'name': 'Merchandise Sales Revenue', 'name_np': 'सामान बिक्री आम्दानी', 'category': 'REVENUE', 'nature': 'CREDIT', 'parent': '4000'},
            {'code': '4200', 'name': 'Service & Workshop Revenue', 'name_np': 'मर्मत सेवा आम्दानी', 'category': 'REVENUE', 'nature': 'CREDIT', 'parent': '4000'},

            # 5000 Direct Costs / COGS
            {'code': '5000', 'name': 'Direct Costs (COGS)', 'name_np': 'प्रत्यक्ष लागत (बिक्री खर्च)', 'category': 'DIRECT_EXPENSE', 'nature': 'DEBIT', 'parent': None},
            {'code': '5100', 'name': 'Cost of Goods Sold', 'name_np': 'बिक्री भएको सामानको लागत', 'category': 'DIRECT_EXPENSE', 'nature': 'DEBIT', 'parent': '5000'},

            # 6000 Indirect Expenses
            {'code': '6000', 'name': 'Indirect Operating Expenses', 'name_np': 'अप्रत्यक्ष सञ्चालन खर्च', 'category': 'INDIRECT_EXPENSE', 'nature': 'DEBIT', 'parent': None},
            {'code': '6100', 'name': 'Store Overheads & Administration', 'name_np': 'पसल तथा प्रशासनिक खर्च', 'category': 'INDIRECT_EXPENSE', 'nature': 'DEBIT', 'parent': '6000'},
        ]

        created_groups = {}
        for g in groups_data:
            parent_obj = created_groups.get(g['parent']) if g['parent'] else None
            group_obj, _ = AccountGroup.objects.update_or_create(
                code=g['code'],
                defaults={
                    'name': g['name'],
                    'name_np': g['name_np'],
                    'category': g['category'],
                    'nature': g['nature'],
                    'parent': parent_obj,
                    'is_system_reserved': True
                }
            )
            created_groups[g['code']] = group_obj

        # =========================================================================
        # 2. MASTER GENERAL LEDGER ACCOUNTS (ALL 34 STANDARD ACCOUNTS)
        # =========================================================================
        self.stdout.write("--> Creating Master General Ledger Accounts...")

        accounts_data = [
            # --- 1000 Assets (Current Assets: 1100) ---
            {
                'code': '1110',
                'name': 'Cash in Hand (Main Drawer)',
                'name_np': 'नगद मौज्दात (मुख्य काउन्टर)',
                'group': '1100',
                'system_tag': 'CASH',
                'nature': 'DEBIT',
                'description': 'Physical cash float in POS counter drawer.'
            },
            {
                'code': '1120',
                'name': 'Primary Bank Current Account',
                'name_np': 'मुख्य बैंक चालु खाता',
                'group': '1100',
                'system_tag': 'BANK',
                'nature': 'DEBIT',
                'description': 'Commercial bank current account for ConnectIPS, Cheques, and direct wire.'
            },
            {
                'code': '1130',
                'name': 'FonePay / QR Settlement Clearing',
                'name_np': 'फोनपे क्युआर हिसाब',
                'group': '1100',
                'system_tag': 'FONEPAY',
                'nature': 'DEBIT',
                'description': 'Clearing ledger for merchant dynamic FonePay QR customer payments.'
            },
            {
                'code': '1140',
                'name': 'eSewa / Digital Wallet Clearing',
                'name_np': 'ईसेवा तथा डिजिटल वालेट हिसाब',
                'group': '1100',
                'system_tag': 'ESEWA',
                'nature': 'DEBIT',
                'description': 'Clearing ledger for eSewa digital wallet customer settlements.'
            },
            {
                'code': '1150',
                'name': 'Khalti / Digital Wallet Clearing',
                'name_np': 'खल्ती तथा डिजिटल वालेट हिसाब',
                'group': '1100',
                'system_tag': 'KHALTI',
                'nature': 'DEBIT',
                'description': 'Clearing ledger for Khalti merchant wallet payments.'
            },
            {
                'code': '1160',
                'name': 'POS Card Settlement Clearing',
                'name_np': 'कार्ड भुक्तानी हिसाब',
                'group': '1100',
                'system_tag': 'CARD_CLEARING',
                'nature': 'DEBIT',
                'description': 'Clearing ledger for counter credit/debit card POS swipe transactions.'
            },
            {
                'code': '1210',
                'name': 'Accounts Receivable (Trade Debtors / Customer Udhaari)',
                'name_np': 'ग्राहक उधारो हिसाब (आसामी)',
                'group': '1100',
                'system_tag': 'ACCOUNTS_RECEIVABLE',
                'nature': 'DEBIT',
                'description': 'Customer credit ledger tracking outstanding Udhaari balances.'
            },
            {
                'code': '1310',
                'name': 'Merchandise Inventory (Smartphones & Accessories at Landed Cost)',
                'name_np': 'सामान मौज्दात (मोबाइल तथा सामान)',
                'group': '1100',
                'system_tag': 'INVENTORY_ASSET',
                'nature': 'DEBIT',
                'description': 'Landed acquisition cost of all active in-stock smartphones and accessories.'
            },
            {
                'code': '1320',
                'name': 'Spare Parts Inventory',
                'name_np': 'स्पेयर पार्ट्स मौज्दात',
                'group': '1100',
                'system_tag': 'INVENTORY_ASSET',
                'nature': 'DEBIT',
                'description': 'LCD displays, batteries, flex cables, and workshop repair components.'
            },
            {
                'code': '1330',
                'name': 'Quarantined Defective Inventory (Waiting RMA)',
                'name_np': 'खराब सामान मौज्दात (RMA पर्खाइमा)',
                'group': '1100',
                'system_tag': 'DEFECTIVE_INVENTORY_ASSET',
                'nature': 'DEBIT',
                'description': 'Returned defective devices awaiting vendor credit or replacement.'
            },
            {
                'code': '1410',
                'name': 'Input VAT 13%',
                'name_np': 'खरिद भ्याट (१३%)',
                'group': '1100',
                'system_tag': 'INPUT_VAT',
                'nature': 'DEBIT',
                'description': 'Tax claimable on official supplier tax purchases (Annex 7).'
            },

            # --- 1500 Fixed Assets ---
            {
                'code': '1510',
                'name': 'Store Fixtures, Showcases & Lab Tools',
                'name_np': 'पसल फर्निचर तथा मर्मत औजार',
                'group': '1500',
                'system_tag': 'NONE',
                'nature': 'DEBIT',
                'description': 'Glass showcases, display racks, ultrasonic cleaners, and SMD rework stations.'
            },
            {
                'code': '1520',
                'name': 'Accumulated Depreciation - Store Fixtures & Tools',
                'name_np': 'सञ्चित ह्रास कट्टी (फर्निचर तथा औजार)',
                'group': '1500',
                'system_tag': 'ACCUMULATED_DEPRECIATION',
                'nature': 'CREDIT',
                'description': 'Contra-asset account tracking cumulative depreciation on fixtures, tools, and displays.'
            },

            # --- 2000 Liabilities (Current: 2100, Long-Term: 2500) ---
            {
                'code': '2110',
                'name': 'Accounts Payable (Trade Creditors / Supplier Udhaari)',
                'name_np': 'साहु उधारो हिसाब (आपूर्तिकर्ता दायित्व)',
                'group': '2100',
                'system_tag': 'ACCOUNTS_PAYABLE',
                'nature': 'CREDIT',
                'description': 'Supplier Udhaari owed to national importers and distributors.'
            },
            {
                'code': '2210',
                'name': 'Output VAT 13%',
                'name_np': 'बिक्री भ्याट (१३%)',
                'group': '2100',
                'system_tag': 'OUTPUT_VAT',
                'nature': 'CREDIT',
                'description': 'VAT collected from taxable retail & wholesale customer sales.'
            },
            {
                'code': '2310',
                'name': 'Customer Store Credit Deposits',
                'name_np': 'ग्राहक अग्रिम तथा स्टोर क्रेडिट',
                'group': '2100',
                'system_tag': 'NONE',
                'nature': 'CREDIT',
                'description': 'Unsettled customer exchange credits and return balances.'
            },
            {
                'code': '2410',
                'name': 'Accrued Expenses Payable',
                'name_np': 'तिर्न बाँकी सञ्चालन खर्च',
                'group': '2100',
                'system_tag': 'NONE',
                'nature': 'CREDIT',
                'description': 'Accrued shop rent, electricity, and unpaid staff payroll.'
            },
            {
                'code': '2510',
                'name': 'Bank Term Loans & Borrowings',
                'name_np': 'बैंक आवधिक ऋण तथा दायित्व',
                'group': '2500',
                'system_tag': 'LOAN_PRINCIPAL',
                'nature': 'CREDIT',
                'description': 'Bank commercial term loans, working capital loans, and formal borrowed obligations.'
            },

            # --- 3000 Equity (3100) ---
            {
                'code': '3110',
                'name': 'Owner Capital',
                'name_np': 'साहुको लगानी पुँजी',
                'group': '3100',
                'system_tag': 'NONE',
                'nature': 'CREDIT',
                'description': 'Net initial and ongoing capital injected by store owner.'
            },
            {
                'code': '3120',
                'name': 'Opening Balance Equity',
                'name_np': 'प्रारम्भिक सन्तुलन पुँजी',
                'group': '3100',
                'system_tag': 'OPENING_BALANCE_EQUITY',
                'nature': 'CREDIT',
                'description': 'Balancing equity offset used during data migration of legacy stock and udhaari.'
            },
            {
                'code': '3130',
                'name': 'Proprietor Capital Drawings',
                'name_np': 'साहुको व्यक्तिगत खर्च (ड्रइङ)',
                'group': '3100',
                'system_tag': 'OWNER_DRAWINGS',
                'nature': 'DEBIT',
                'description': 'Owner withdrawals for personal use reducing proprietor equity.'
            },
            {
                'code': '3210',
                'name': 'Retained Earnings',
                'name_np': 'सञ्चित नाफा नोक्सान हिसाब',
                'group': '3100',
                'system_tag': 'RETAINED_EARNINGS',
                'nature': 'CREDIT',
                'description': 'Cumulative net profit or loss carried forward from previous fiscal years.'
            },

            # --- 4000 Revenue (4000, 4100 & 4200) ---
            {
                'code': '4030',
                'name': 'Inventory Audit Surplus & Stock Gain',
                'name_np': 'स्टक गणना बढी आम्दानी (स्टक गेन)',
                'group': '4000',
                'system_tag': 'INVENTORY_SURPLUS',
                'nature': 'CREDIT',
                'description': 'Physical inventory audit surplus and stock count gain reconciliation.'
            },
            {
                'code': '4110',
                'name': 'Handset Sales Revenue',
                'name_np': 'मोबाइल बिक्री आम्दानी',
                'group': '4100',
                'system_tag': 'SALES_REVENUE',
                'nature': 'CREDIT',
                'description': 'Turnover realized from brand-new and pre-owned smartphone sales.'
            },
            {
                'code': '4120',
                'name': 'Accessories Sales Revenue',
                'name_np': 'एक्सेसोरिज तथा सामान बिक्री',
                'group': '4100',
                'system_tag': 'SALES_REVENUE',
                'nature': 'CREDIT',
                'description': 'Sales of tempered glasses, chargers, smartwatches, and cases.'
            },
            {
                'code': '4210',
                'name': 'Service & Repair Labor Revenue',
                'name_np': 'मर्मत सेवा तथा ज्याला आम्दानी',
                'group': '4200',
                'system_tag': 'REPAIR_SERVICE_INCOME',
                'nature': 'CREDIT',
                'description': 'Technician service labor charges for hardware and software repairs.'
            },
            {
                'code': '4310',
                'name': 'Trade-In Buy-Back Margin Income',
                'name_np': 'पुरानो फोन साटासाट नाफा',
                'group': '4100',
                'system_tag': 'NONE',
                'nature': 'CREDIT',
                'description': 'Margin gained between customer buyback valuation and resale value.'
            },

            # --- 5000 Direct Costs (5100) ---
            {
                'code': '5110',
                'name': 'Cost of Goods Sold - Handsets',
                'name_np': 'मोबाइल खरिद लागत (COGS)',
                'group': '5100',
                'system_tag': 'COGS',
                'nature': 'DEBIT',
                'description': 'Landed acquisition cost of all sold mobile phones.'
            },
            {
                'code': '5120',
                'name': 'Cost of Goods Sold - Accessories',
                'name_np': 'एक्सेसोरिज खरिद लागत (COGS)',
                'group': '5100',
                'system_tag': 'COGS',
                'nature': 'DEBIT',
                'description': 'Cost of sold chargers, covers, tempered glasses, and cables.'
            },
            {
                'code': '5130',
                'name': 'Repair Spare Parts Consumed',
                'name_np': 'मर्मतमा प्रयोग भएको पार्टपुर्जा लागत',
                'group': '5100',
                'system_tag': 'REPAIR_PARTS_COGS',
                'nature': 'DEBIT',
                'description': 'Cost of screens, batteries, and ICs used in customer repairs.'
            },
            {
                'code': '5210',
                'name': 'Freight, Customs & Inward Shipping',
                'name_np': 'ढुवानी तथा भन्सार महसुल',
                'group': '5100',
                'system_tag': 'NONE',
                'nature': 'DEBIT',
                'description': 'Direct courier and transport costs associated with inventory intake.'
            },

            # --- 6000 Operating Expenses (6100) ---
            {
                'code': '6110',
                'name': 'Shop Space Rent',
                'name_np': 'पसल कोठा भाडा',
                'group': '6100',
                'system_tag': 'NONE',
                'nature': 'DEBIT',
                'description': 'Monthly premises rent paid to store landlord.'
            },
            {
                'code': '6120',
                'name': 'Electricity, Water & Utilities',
                'name_np': 'बिजुली तथा पानी महसुल',
                'group': '6100',
                'system_tag': 'NONE',
                'nature': 'DEBIT',
                'description': 'NEA electricity charges, drinking water, and municipal waste fees.'
            },
            {
                'code': '6130',
                'name': 'Staff Payroll & Commissions',
                'name_np': 'कर्मचारी तलब तथा कमिसन',
                'group': '6100',
                'system_tag': 'NONE',
                'nature': 'DEBIT',
                'description': 'Monthly cashier salaries and technician repair commission splits.'
            },
            {
                'code': '6140',
                'name': 'Staff Refreshments / Tea & Snacks',
                'name_np': 'चिया तथा खाजा खर्च',
                'group': '6100',
                'system_tag': 'NONE',
                'nature': 'DEBIT',
                'description': 'Daily tea, coffee, and refreshments for shop staff and VIP clients.'
            },
            {
                'code': '6150',
                'name': 'Internet & Communications',
                'name_np': 'इन्टरनेट तथा फोन महसुल',
                'group': '6100',
                'system_tag': 'NONE',
                'nature': 'DEBIT',
                'description': 'Fiber broadband, landline, and POS SMS gateway costs.'
            },
            {
                'code': '6160',
                'name': 'Inventory Shrinkage, Breakage & Loss',
                'name_np': 'सामान टुटफुट तथा नोक्सानी',
                'group': '6100',
                'system_tag': 'INVENTORY_SHRINKAGE',
                'nature': 'DEBIT',
                'description': 'Write-off expense for broken tempered glass, damaged phones, or missing stock.'
            },
            {
                'code': '6170',
                'name': 'POS Sales Discounts Allowed',
                'name_np': 'बिक्रीमा दिइएको छुट (कन्सेसन)',
                'group': '6100',
                'system_tag': 'SALES_DISCOUNT',
                'nature': 'DEBIT',
                'description': 'Merchandise concessions and price reductions granted on estimate slips.'
            },
            {
                'code': '6180',
                'name': 'Depreciation on Shop Showcases & Fixtures',
                'name_np': 'फर्निचर तथा औजार ह्रास कट्टी',
                'group': '6100',
                'system_tag': 'NONE',
                'nature': 'DEBIT',
                'description': 'Periodic depreciation write-down on physical furniture and displays.'
            },
            {
                'code': '6190',
                'name': 'Payment Gateway & Bank Merchant Fees (MDR)',
                'name_np': 'डिजिटल भुक्तानी सेवा शुल्क (MDR)',
                'group': '6100',
                'system_tag': 'GATEWAY_COMMISSION',
                'nature': 'DEBIT',
                'description': 'Merchant Discount Rate (MDR) fees deducted by FonePay, eSewa, Khalti, and POS card acquirers.'
            },
            {
                'code': '6210',
                'name': 'Bank Loan Interest & Financial Charges',
                'name_np': 'बैंक ऋण ब्याज तथा वित्तीय खर्च',
                'group': '6100',
                'system_tag': 'INTEREST_EXPENSE',
                'nature': 'DEBIT',
                'description': 'Monthly interest payments, loan processing fees, and financial banking charges.'
            },
        ]

        account_model_fields = {f.name for f in Account._meta.get_fields()}
        seeded_accounts = {}

        for acc in accounts_data:
            group_obj = created_groups[acc['group']]
            defaults = {
                'name': acc['name'],
                'name_np': acc['name_np'],
                'group': group_obj,
                'branch': target_branch,
                'system_tag': acc['system_tag'],
                'is_system_reserved': True,
                'description': acc['description']
            }

            if 'nature' in account_model_fields:
                defaults['nature'] = acc.get('nature', group_obj.nature)
            if 'is_debit_nature' in account_model_fields:
                acc_nature = acc.get('nature', group_obj.nature)
                defaults['is_debit_nature'] = (acc_nature == 'DEBIT')

            account_obj, created = Account.objects.update_or_create(
                code=acc['code'],
                defaults=defaults
            )
            seeded_accounts[acc['code']] = account_obj

        # =========================================================================
        # 3. BIND CONTROL ACCOUNTS TO SYSTEM CONFIGURATION
        # =========================================================================
        self.stdout.write("--> Binding Control Accounts to System Configuration...")

        config = SystemConfiguration.get_solo()

        # Primary baseline bindings
        config.default_cash_account = seeded_accounts.get('1110')
        config.default_bank_account = seeded_accounts.get('1120')
        config.default_receivable_account = seeded_accounts.get('1210')
        config.default_inventory_asset_account = seeded_accounts.get('1310')
        config.default_vat_input_account = seeded_accounts.get('1410')
        config.default_payable_account = seeded_accounts.get('2110')
        config.default_vat_output_account = seeded_accounts.get('2210')
        config.default_sales_revenue_account = seeded_accounts.get('4110')
        config.default_cogs_account = seeded_accounts.get('5110')
        config.default_shrinkage_account = seeded_accounts.get('6160')
        config.default_discount_expense_account = seeded_accounts.get('6170')

        # Extended digital clearing & operational accounts bindings
        extended_config_map = {
            'default_fonepay_account': '1130',
            'default_fonepay_clearing_account': '1130',
            'default_esewa_account': '1140',
            'default_esewa_clearing_account': '1140',
            'default_khalti_account': '1150',
            'default_khalti_clearing_account': '1150',
            'default_card_clearing_account': '1160',
            'default_card_account': '1160',
            'default_accumulated_depreciation_account': '1520',
            'default_loan_account': '2510',
            'default_term_loan_account': '2510',
            'default_borrowing_account': '2510',
            'default_owner_drawings_account': '3130',
            'default_drawings_account': '3130',
            'default_inventory_surplus_account': '4030',
            'default_stock_gain_account': '4030',
            'default_gateway_fee_account': '6190',
            'default_gateway_commission_account': '6190',
            'default_payment_gateway_fee_account': '6190',
            'default_interest_expense_account': '6210',
            'default_bank_interest_account': '6210',
        }

        config_model_fields = {f.name for f in config._meta.get_fields()}
        for field_name, acc_code in extended_config_map.items():
            if field_name in config_model_fields and acc_code in seeded_accounts:
                setattr(config, field_name, seeded_accounts[acc_code])

        config.save()

        # =========================================================================
        # 4. INITIALIZE NEPALI FISCAL YEARS (2080/81, 2081/82, 2082/83)
        # =========================================================================
        self.stdout.write("--> Initializing Nepali Fiscal Years and Monthly Periods...")

        fiscal_years = ['2080/81', '2081/82', '2082/83']
        for fy_name in fiscal_years:
            start_ad, end_ad, start_bs, end_bs = NepaliCalendar.get_fiscal_year_range(fy_name)
            fy_obj, _ = AccountingFiscalYear.objects.update_or_create(
                name=fy_name,
                defaults={
                    'start_date_ad': start_ad,
                    'end_date_ad': end_ad,
                    'start_date_bs': start_bs,
                    'end_date_bs': end_bs,
                    'is_closed': (fy_name == '2080/81')  # 2080/81 is closed historical year
                }
            )

            # Generate 12 BS Monthly Periods (Shrawan = 1 ... Ashadh = 12)
            base_year = int(fy_name.split('/')[0])
            for month_idx in range(1, 13):
                # Nepali FY begins Shrawan (Month 4) of base_year
                if month_idx <= 9:
                    bs_y = base_year
                    bs_m = month_idx + 3  # 1->4 (Shrawan), 9->12 (Chaitra)
                else:
                    bs_y = base_year + 1
                    bs_m = month_idx - 9  # 10->1 (Baishakh), 12->3 (Ashadh)

                s_ad, e_ad, s_bs, e_bs = NepaliCalendar.get_bs_month_range(bs_y, bs_m)
                m_name_en = NepaliCalendar.NEPALI_MONTH_NAMES_EN[bs_m - 1]
                m_name_np = NepaliCalendar.NEPALI_MONTH_NAMES_NP[bs_m - 1]

                FinancialPeriod.objects.update_or_create(
                    fiscal_year=fy_obj,
                    period_number=month_idx,
                    defaults={
                        'period_name_en': f"{m_name_en} ({fy_name})",
                        'period_name_np': f"{m_name_np} ({fy_name})",
                        'start_date_ad': s_ad,
                        'end_date_ad': e_ad,
                        'start_date_bs': s_bs,
                        'end_date_bs': e_bs,
                        'is_closed': (fy_name == '2080/81')
                    }
                )

        self.stdout.write(self.style.SUCCESS(
            f"Successfully seeded {len(seeded_accounts)} Chart of Accounts ledgers and bound 3 Fiscal Years."
        ))