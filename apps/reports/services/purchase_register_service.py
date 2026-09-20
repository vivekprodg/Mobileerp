"""
Commercial Purchase Register (Inward Purchase Book) Service.

Capabilities:
1. Multi-Calendar Date & Fiscal Year Engine:
   - Supports filtering by official Nepali Fiscal Year (आर्थिक वर्ष, e.g. '2080/81', '2081/82').
   - Resolves Shrawan 1 through the exact dynamic last day of Ashadh (accounting for 30, 31, or 32 days).
   - Accurately converts Nepali BS date inputs to Gregorian AD dates for database querying.
2. Generates the complete chronological Inward Purchase Register for all verified GRN vouchers.
3. Extracts complete commercial transaction fields:
   - Bill date in Gregorian AD and Nepali Bikram Sambat (BS).
   - GRN number and supplier invoice / challan reference number.
   - Supplier identification (code, name, PAN/VAT registration, mobile).
   - Inward invoice type (13% VAT Bill versus PAN / Non-VAT Challan).
   - Gross merchandise subtotal before trade discounts.
   - Trade discount received from supplier.
   - Input VAT amount claimed.
   - Allocated shipping overheads (freight, customs, handling charges).
   - Total landed acquisition cost (true inventory asset value).
   - Final net payable amount to distributor.
   - Payment tendered on delivery versus due balance added to supplier credit.
4. Computes exact summary totals across all columns for screen view, CSV, Excel, and A4 PDF rendering.
"""

import re
from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime
from typing import Dict, Any, List, Optional, Tuple

from django.db.models import Q, Sum, Count, F, DecimalField, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.purchases.models import GoodsReceivedNote, Supplier
from apps.branches.models import Branch
from apps.core.nepali_calendar import NepaliCalendar
from apps.core.utils.nepali_date_converter import ad_to_bs_string


class PurchaseRegisterService:
    """
    Business logic service for Report 1: Purchase Register (Inward Purchase Book).
    """

    @classmethod
    def get_available_fiscal_years(cls) -> List[str]:
        """Returns standard list of relevant Nepali Fiscal Years for selection dropdowns."""
        today = timezone.now().date()
        current_bs_y, current_bs_m, _ = NepaliCalendar.ad_to_bs(today)
        current_fy = NepaliCalendar.get_fiscal_year(current_bs_y, current_bs_m)

        base_years = [2079, 2080, 2081, 2082, 2083]
        fys = [f"{y}/{str(y + 1)[-2:]}" for y in base_years]
        if current_fy not in fys:
            fys.append(current_fy)
        fys.sort(reverse=True)
        return fys

    @classmethod
    def _parse_flexible_date(cls, val_str: str) -> Optional[Tuple[date, str]]:
        """
        Accepts arbitrary date strings in either Gregorian AD or Nepali BS (delimited by - or /)
        and returns a clean (ad_date, bs_date_string) tuple.
        """
        if not val_str:
            return None
        clean = re.sub(r'[^\d]', '-', str(val_str).strip())
        parts = [int(p) for p in clean.split('-') if p]
        if len(parts) != 3:
            return None

        # Format: YYYY-MM-DD in BS (e.g. 2080-04-01)
        if 2000 <= parts[0] <= 2095:
            bs_y, bs_m, bs_d = parts[0], parts[1], parts[2]
            bs_m = max(1, min(12, bs_m))
            max_days = NepaliCalendar.get_days_in_month(bs_y, bs_m)
            bs_d = max(1, min(max_days, bs_d))
            ad_date = NepaliCalendar.bs_to_ad(bs_y, bs_m, bs_d)
            bs_str = f"{bs_y:04d}-{bs_m:02d}-{bs_d:02d}"
            return ad_date, bs_str

        # Format: DD-MM-YYYY in BS (e.g. 01-04-2080)
        elif 2000 <= parts[2] <= 2095:
            bs_y, bs_m, bs_d = parts[2], parts[1], parts[0]
            bs_m = max(1, min(12, bs_m))
            max_days = NepaliCalendar.get_days_in_month(bs_y, bs_m)
            bs_d = max(1, min(max_days, bs_d))
            ad_date = NepaliCalendar.bs_to_ad(bs_y, bs_m, bs_d)
            bs_str = f"{bs_y:04d}-{bs_m:02d}-{bs_d:02d}"
            return ad_date, bs_str

        # Format: YYYY-MM-DD in AD (e.g. 2023-07-17)
        elif 1970 <= parts[0] <= 2050:
            try:
                ad_date = date(parts[0], parts[1], parts[2])
                y, m, d = NepaliCalendar.ad_to_bs(ad_date)
                bs_str = NepaliCalendar.format_bs(y, m, d, lang='en')
                return ad_date, bs_str
            except (ValueError, TypeError):
                return None

        return None

    @classmethod
    def resolve_date_range(cls, raw_params: Dict[str, Any]) -> Tuple[date, date, str, str, str]:
        """
        Resolves query dates and Nepali Fiscal Year with multi-format support:
        1. Fiscal Year parameter (e.g. '2080/81', '2081/82', '2080-81').
           Accurately resolves Shrawan 1 to Ashadh 31/32 (variable month length).
        2. Direct Nepali BS dates ('YYYY-MM-DD').
        3. Gregorian AD dates ('YYYY-MM-DD').
        4. Default fallback: 1st of the current BS month up to today.

        Returns:
            Tuple[date, date, str, str, str]:
            (start_date_ad, end_date_ad, start_date_bs, end_date_bs, fiscal_year)
        """
        today_ad = timezone.now().date()
        today_bs_y, today_bs_m, today_bs_d = NepaliCalendar.ad_to_bs(today_ad)

        fy_param = str(raw_params.get('fiscal_year') or raw_params.get('fy') or '').strip()
        start_param = str(raw_params.get('start_date') or raw_params.get('start_date_bs') or '').strip()
        end_param = str(raw_params.get('end_date') or raw_params.get('end_date_bs') or '').strip()

        # Case 1: Fiscal Year preset selected without explicit overriding date range
        if fy_param and fy_param.lower() not in ['all', 'none', '']:
            clean_fy = fy_param.replace('-', '/').strip()
            if not start_param and not end_param:
                try:
                    start_ad, end_ad, start_bs, end_bs = NepaliCalendar.get_fiscal_year_range(clean_fy)
                    parts = clean_fy.split('/')
                    norm_fy = f"{int(parts[0])}/{str(int(parts[0]) + 1)[-2:]}"
                    return start_ad, end_ad, start_bs, end_bs, norm_fy
                except Exception:
                    pass

        # Case 2: Parse custom start and end date inputs (supporting both AD and BS formats)
        start_date = None
        end_date = None
        start_date_bs = ""
        end_date_bs = ""

        if start_param:
            parsed_start = cls._parse_flexible_date(start_param)
            if parsed_start:
                start_date, start_date_bs = parsed_start

        if end_param:
            parsed_end = cls._parse_flexible_date(end_param)
            if parsed_end:
                end_date, end_date_bs = parsed_end

        # Case 3: If fiscal_year was chosen along with custom dates within it
        if fy_param and fy_param.lower() not in ['all', 'none', '']:
            clean_fy = fy_param.replace('-', '/').strip()
            try:
                fy_start_ad, fy_end_ad, fy_start_bs, fy_end_bs = NepaliCalendar.get_fiscal_year_range(clean_fy)
                parts = clean_fy.split('/')
                resolved_fy = f"{int(parts[0])}/{str(int(parts[0]) + 1)[-2:]}"
                start_date = start_date or fy_start_ad
                end_date = end_date or fy_end_ad
                start_date_bs = start_date_bs or fy_start_bs
                end_date_bs = end_date_bs or fy_end_bs
                if start_date > end_date:
                    start_date, end_date = end_date, start_date
                    start_date_bs, end_date_bs = end_date_bs, start_date_bs
                return start_date, end_date, start_date_bs, end_date_bs, resolved_fy
            except Exception:
                pass

        # Case 4: Fallback to current ongoing BS month up to today
        if not start_date or not end_date:
            start_of_bs_month = NepaliCalendar.bs_to_ad(today_bs_y, today_bs_m, 1)
            start_date = start_date or start_of_bs_month
            end_date = end_date or today_ad

        if start_date > end_date:
            start_date, end_date = end_date, start_date

        start_y, start_m, start_d = NepaliCalendar.ad_to_bs(start_date)
        end_y, end_m, end_d = NepaliCalendar.ad_to_bs(end_date)

        start_date_bs = NepaliCalendar.format_bs(start_y, start_m, start_d, lang='en')
        end_date_bs = NepaliCalendar.format_bs(end_y, end_m, end_d, lang='en')
        resolved_fy = NepaliCalendar.get_fiscal_year(start_y, start_m)

        return start_date, end_date, start_date_bs, end_date_bs, resolved_fy

    @classmethod
    def get_purchase_register_data(
        cls,
        filters: Dict[str, Any],
        user=None
    ) -> Dict[str, Any]:
        """
        Compiles the full inward purchase register entries with overheads, taxes, and payment status.
        """
        active_branch = getattr(user, 'assigned_branch', None) if user and not user.is_superuser else None

        start_date, end_date, start_date_bs, end_date_bs, fiscal_year = cls.resolve_date_range(filters)
        branch_id = filters.get('branch_id') or filters.get('branch', '')
        supplier_id = filters.get('supplier_id') or filters.get('supplier', '')
        tax_mode = str(filters.get('tax_mode', '') or '').strip().lower()
        search_query = str(filters.get('q', '') or '').strip()

        # 1. Base Query: Only verified Goods Received Notes
        grn_qs = GoodsReceivedNote.objects.select_related(
            'supplier',
            'branch',
            'received_by',
            'purchase_order'
        ).prefetch_related('items__product').filter(
            status='RECEIVED',
            bill_date__gte=start_date,
            bill_date__lte=end_date
        )

        # 2. Apply Scoping & Filters
        if branch_id and str(branch_id).strip() not in ['', 'all', 'None']:
            grn_qs = grn_qs.filter(branch_id=branch_id)
        elif active_branch and not (user.is_superuser or getattr(user, 'role', '') == 'OWNER'):
            grn_qs = grn_qs.filter(branch=active_branch)

        if supplier_id and str(supplier_id).strip() not in ['', 'all', 'None']:
            grn_qs = grn_qs.filter(supplier_id=supplier_id)

        if tax_mode == 'vat':
            grn_qs = grn_qs.filter(is_vat_bill=True)
        elif tax_mode in ['pan', 'non_vat']:
            grn_qs = grn_qs.filter(is_vat_bill=False)

        if search_query:
            grn_qs = grn_qs.filter(
                Q(grn_number__icontains=search_query) |
                Q(supplier_bill_no__icontains=search_query) |
                Q(supplier__company_name__icontains=search_query) |
                Q(supplier_product_code__icontains=search_query) |
                Q(mdms_tax_invoice_ref__icontains=search_query)
            )

        # 3. Compile Sequential Register Entries
        records: List[Dict[str, Any]] = []

        total_gross = Decimal('0.00')
        total_discount = Decimal('0.00')
        total_vat = Decimal('0.00')
        total_freight = Decimal('0.00')
        total_customs = Decimal('0.00')
        total_handling = Decimal('0.00')
        total_overheads = Decimal('0.00')
        total_landed = Decimal('0.00')
        total_net = Decimal('0.00')
        total_paid = Decimal('0.00')
        total_due = Decimal('0.00')

        ordered_grns = grn_qs.order_by('bill_date', 'grn_number')

        for idx, grn in enumerate(ordered_grns, start=1):
            gross = grn.gross_amount or Decimal('0.00')
            disc = grn.discount_amount or Decimal('0.00')
            vat = grn.vat_amount or Decimal('0.00')
            freight = grn.extra_freight_charge or Decimal('0.00')
            customs = grn.customs_import_charge or Decimal('0.00')
            handling = grn.other_handling_charge or Decimal('0.00')
            overheads = freight + customs + handling
            landed = grn.total_landed_cost or Decimal('0.00')
            net_amt = grn.net_total_amount or Decimal('0.00')
            paid = grn.paid_amount or Decimal('0.00')
            due = grn.due_amount or Decimal('0.00')

            date_bs = grn.bill_date_bs or ad_to_bs_string(grn.bill_date, lang='en')

            total_gross += gross
            total_discount += disc
            total_vat += vat
            total_freight += freight
            total_customs += customs
            total_handling += handling
            total_overheads += overheads
            total_landed += landed
            total_net += net_amt
            total_paid += paid
            total_due += due

            records.append({
                'sn': idx,
                'grn_id': grn.id,
                'grn_number': grn.grn_number,
                'bill_date': grn.bill_date,
                'bill_date_str': grn.bill_date.strftime('%Y-%m-%d'),
                'bill_date_bs': date_bs,
                'fiscal_year': grn.fiscal_year or fiscal_year,
                'supplier_bill_no': grn.supplier_bill_no,
                'supplier_name': grn.supplier.company_name,
                'supplier_code': grn.supplier.code,
                'supplier_phone': grn.supplier.phone_number,
                'supplier_pan': grn.supplier.pan_number or grn.supplier.vat_number or '-',
                'branch_code': grn.branch.code,
                'branch_name': grn.branch.name,
                'is_vat_bill': grn.is_vat_bill,
                'distributor_mdms_certified': grn.distributor_mdms_certified,
                'mdms_ref': grn.mdms_tax_invoice_ref or '-',
                'gross_amount': gross,
                'discount_amount': disc,
                'vat_amount': vat,
                'freight_charge': freight,
                'customs_charge': customs,
                'handling_charge': handling,
                'overhead_total': overheads,
                'total_landed_cost': landed,
                'net_total_amount': net_amt,
                'paid_amount': paid,
                'due_amount': due,
                'items_count': grn.items.count(),
                'received_by': grn.received_by.username if grn.received_by else 'Staff',
            })

        totals = {
            'records_count': len(records),
            'total_gross': total_gross,
            'total_discount': total_discount,
            'total_vat': total_vat,
            'total_freight': total_freight,
            'total_customs': total_customs,
            'total_handling': total_handling,
            'total_overheads': total_overheads,
            'total_landed': total_landed,
            'total_net': total_net,
            'total_paid': total_paid,
            'total_due': total_due,
        }

        selected_branch = None
        if branch_id and branch_id != 'all':
            selected_branch = Branch.objects.filter(id=branch_id).first()
        elif active_branch:
            selected_branch = active_branch

        return {
            'records': records,
            'totals': totals,
            'branches': Branch.objects.filter(is_active=True).order_by('-is_main_branch', 'name'),
            'suppliers': Supplier.objects.filter(is_active=True).order_by('company_name'),
            'selected_branch': selected_branch,
            'filters': filters,
            'start_date': start_date.strftime('%Y-%m-%d'),
            'end_date': end_date.strftime('%Y-%m-%d'),
            'start_date_bs': start_date_bs,
            'end_date_bs': end_date_bs,
            'fiscal_year': fiscal_year,
            'available_fiscal_years': cls.get_available_fiscal_years(),
        }