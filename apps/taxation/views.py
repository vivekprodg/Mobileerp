"""
Taxation & Proforma Register Overview Views (Annex 5 Sales Book & Annex 7 Purchase Book).

Capabilities:
1. Multi-Calendar Date & Fiscal Year Engine:
   - Connects to the core NepaliCalendar engine for Bikram Sambat (BS) date calculations.
   - Allows accountants to toggle between viewing a single Nepali month (e.g., Ashadh 2080, Shrawan 2081)
     or an entire Nepali Fiscal Year (आर्थिक वर्ष, e.g., 2080/81, 2081/82).
   - Accurately resolves Shrawan 1 of the base BS year through the dynamic variable last day of Ashadh
     (31 or 32 days) of the next BS year to obtain exact Gregorian AD date boundaries.
   - Supports custom date range queries in both Gregorian AD and Nepali BS.
2. TaxDashboardView:
   - Displays period VAT summaries for output tax (sales) and input tax (purchases) along with net assessment.
   - Supports switching period view mode ('month' vs. 'fiscal_year' vs. 'custom').
3. SalesRegisterReportView:
   - Generates the Internal Sales Register (बिक्री खाता - Annex 5 format) for any selected BS month or full Fiscal Year.
   - Reconciles taxable base, non-taxable base, output VAT, and credit adjustments from customer sales returns.
   - Offers 1-click CSV export with formula injection protection.
4. PurchaseRegisterReportView:
   - Generates the Commercial Purchase Register (खरिद खाता - Annex 7 format) for any selected BS month or full Fiscal Year.
   - Retrieves full commercial metrics: gross subtotal, trade discounts, input VAT, shipping/customs overheads,
     total landed inventory costs, net payable amounts, spot cash paid, and supplier ledger debt balances.
   - Offers 1-click CSV and styled Excel export options.
"""

import csv
import re
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Any, List, Tuple, Optional

from django.shortcuts import render
from django.views.generic import TemplateView
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.http import HttpResponse
from django.utils import timezone

from apps.taxation.reports import TaxationReportGenerator
from apps.taxation.ird_sync import NonIRDDisclaimerEngine
from apps.branches.models import Branch
from apps.core.models import SystemConfiguration
from apps.core.nepali_calendar import NepaliCalendar
from apps.core.utils.nepali_date_converter import ad_to_bs_string
from apps.reports.exports import CSVExportEngine, sanitize_csv_row


# ==============================================================================
# PERIOD & FISCAL YEAR RESOLUTION HELPERS
# ==============================================================================

def get_available_fiscal_years() -> List[str]:
    """
    Returns standard list of relevant Nepali Fiscal Years for selection dropdowns.
    e.g., ['2082/83', '2081/82', '2080/81', '2079/80', '2078/79']
    """
    today_ad = timezone.now().date()
    current_bs_y, current_bs_m, _ = NepaliCalendar.ad_to_bs(today_ad)
    current_fy = NepaliCalendar.get_fiscal_year(current_bs_y, current_bs_m)

    base_years = [2078, 2079, 2080, 2081, 2082, 2083]
    fys = [f"{y}/{str(y + 1)[-2:]}" for y in base_years]
    if current_fy not in fys:
        fys.append(current_fy)
    fys.sort(reverse=True)
    return fys


def get_available_nepali_months() -> List[Dict[str, Any]]:
    """
    Returns the 12 Bikram Sambat months with English and Nepali Devanagari labels.
    """
    return [
        {
            'num': i + 1,
            'name_en': NepaliCalendar.NEPALI_MONTH_NAMES_EN[i],
            'name_np': NepaliCalendar.NEPALI_MONTH_NAMES_NP[i]
        }
        for i in range(12)
    ]


def parse_flexible_date(val_str: str) -> Optional[Tuple[date, str]]:
    """
    Accepts arbitrary date strings in either Gregorian AD or Nepali BS (delimited by - or /)
    and returns a clean (ad_date_object, bs_date_string) tuple.
    """
    if not val_str or not str(val_str).strip():
        return None

    clean = re.sub(r'[^\d]', '-', str(val_str).strip())
    parts = [int(p) for p in clean.split('-') if p]
    if len(parts) != 3:
        return None

    # Format: YYYY-MM-DD in BS (e.g., 2080-04-01)
    if 2000 <= parts[0] <= 2095:
        bs_y, bs_m, bs_d = parts[0], parts[1], parts[2]
        bs_m = max(1, min(12, bs_m))
        max_days = NepaliCalendar.get_days_in_month(bs_y, bs_m)
        bs_d = max(1, min(max_days, bs_d))
        ad_date = NepaliCalendar.bs_to_ad(bs_y, bs_m, bs_d)
        bs_str = f"{bs_y:04d}-{bs_m:02d}-{bs_d:02d}"
        return ad_date, bs_str

    # Format: DD-MM-YYYY in BS (e.g., 01-04-2080)
    elif 2000 <= parts[2] <= 2095:
        bs_y, bs_m, bs_d = parts[2], parts[1], parts[0]
        bs_m = max(1, min(12, bs_m))
        max_days = NepaliCalendar.get_days_in_month(bs_y, bs_m)
        bs_d = max(1, min(max_days, bs_d))
        ad_date = NepaliCalendar.bs_to_ad(bs_y, bs_m, bs_d)
        bs_str = f"{bs_y:04d}-{bs_m:02d}-{bs_d:02d}"
        return ad_date, bs_str

    # Format: YYYY-MM-DD in AD (e.g., 2023-07-17)
    elif 1970 <= parts[0] <= 2050:
        try:
            ad_date = date(parts[0], parts[1], parts[2])
            y, m, d = NepaliCalendar.ad_to_bs(ad_date)
            bs_str = NepaliCalendar.format_bs(y, m, d, lang='en')
            return ad_date, bs_str
        except (ValueError, TypeError):
            return None

    return None


def resolve_taxation_period(raw_params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Central date & fiscal period resolution engine for taxation registers.

    Capabilities:
    1. View Mode Toggling:
       - 'fiscal_year': Full Nepali Fiscal Year (Shrawan 1 of Year A to Ashadh 31/32 of Year B).
       - 'month': Single Bikram Sambat Month (1st of month to exact last day of month).
       - 'custom': Custom start and end date inputs (AD or BS).
    2. Auto-Detection:
       - If 'view_mode' or 'period_type' is not explicitly passed, detects based on submitted inputs.
       - Defaults gracefully to the active BS month up to today.
    """
    today_ad = timezone.now().date()
    today_bs_y, today_bs_m, today_bs_d = NepaliCalendar.ad_to_bs(today_ad)
    current_fy = NepaliCalendar.get_fiscal_year(today_bs_y, today_bs_m)

    view_mode = str(raw_params.get('view_mode') or raw_params.get('period_type') or '').strip().lower()
    fy_param = str(raw_params.get('fiscal_year') or raw_params.get('fy') or '').strip()
    bs_year_param = str(raw_params.get('bs_year') or '').strip()
    bs_month_param = str(raw_params.get('bs_month') or '').strip()
    start_param = str(raw_params.get('start_date') or raw_params.get('start_date_bs') or '').strip()
    end_param = str(raw_params.get('end_date') or raw_params.get('end_date_bs') or '').strip()

    # Determine view mode if not explicitly stated
    if not view_mode:
        if fy_param and not bs_month_param and not start_param and not end_param:
            view_mode = 'fiscal_year'
        elif start_param and end_param and not fy_param and not bs_month_param:
            view_mode = 'custom'
        else:
            view_mode = 'month'

    # Mode 1: Full Nepali Fiscal Year
    if view_mode == 'fiscal_year':
        target_fy = fy_param if fy_param and fy_param.lower() not in ['all', 'none', ''] else current_fy
        clean_fy = target_fy.replace('-', '/').strip()

        try:
            start_date_ad, end_date_ad, start_date_bs, end_date_bs = NepaliCalendar.get_fiscal_year_range(clean_fy)
            parts = clean_fy.split('/')
            normalized_fy = f"{int(parts[0])}/{str(int(parts[0]) + 1)[-2:]}"
        except Exception:
            clean_fy = current_fy
            start_date_ad, end_date_ad, start_date_bs, end_date_bs = NepaliCalendar.get_fiscal_year_range(clean_fy)
            normalized_fy = clean_fy

        period_label = f"Fiscal Year {normalized_fy} ({start_date_bs} to {end_date_bs} BS)"
        target_bs_year = int(normalized_fy.split('/')[0])
        target_bs_month = None
        bs_month_name_en = "Full Fiscal Year"
        bs_month_name_np = "सम्पूर्ण आर्थिक वर्ष"

    # Mode 2: Custom Date Range (AD or BS)
    elif view_mode == 'custom':
        start_date_ad = None
        end_date_ad = None
        start_date_bs = ""
        end_date_bs = ""

        if start_param:
            parsed = parse_flexible_date(start_param)
            if parsed:
                start_date_ad, start_date_bs = parsed

        if end_param:
            parsed = parse_flexible_date(end_param)
            if parsed:
                end_date_ad, end_date_bs = parsed

        if not start_date_ad or not end_date_ad:
            start_date_ad = NepaliCalendar.bs_to_ad(today_bs_y, today_bs_m, 1)
            end_date_ad = today_ad
            start_y, start_m, start_d = NepaliCalendar.ad_to_bs(start_date_ad)
            end_y, end_m, end_d = NepaliCalendar.ad_to_bs(end_date_ad)
            start_date_bs = NepaliCalendar.format_bs(start_y, start_m, start_d, lang='en')
            end_date_bs = NepaliCalendar.format_bs(end_y, end_m, end_d, lang='en')

        if start_date_ad > end_date_ad:
            start_date_ad, end_date_ad = end_date_ad, start_date_ad
            start_date_bs, end_date_bs = end_date_bs, start_date_bs

        s_y, s_m, _ = NepaliCalendar.ad_to_bs(start_date_ad)
        normalized_fy = NepaliCalendar.get_fiscal_year(s_y, s_m)
        target_bs_year = s_y
        target_bs_month = s_m
        bs_month_name_en = NepaliCalendar.NEPALI_MONTH_NAMES_EN[s_m - 1]
        bs_month_name_np = NepaliCalendar.NEPALI_MONTH_NAMES_NP[s_m - 1]
        period_label = f"Custom: {start_date_bs} to {end_date_bs} BS ({start_date_ad} to {end_date_ad})"

    # Mode 3: Single Nepali Bikram Sambat Month (Default)
    else:
        view_mode = 'month'
        try:
            target_bs_year = int(bs_year_param) if bs_year_param else today_bs_y
        except (ValueError, TypeError):
            target_bs_year = today_bs_y

        try:
            target_bs_month = int(bs_month_param) if bs_month_param else today_bs_m
        except (ValueError, TypeError):
            target_bs_month = today_bs_m

        target_bs_month = max(1, min(12, target_bs_month))
        target_bs_year = max(2000, min(2090, target_bs_year))

        start_date_ad, end_date_ad, start_date_bs, end_date_bs = NepaliCalendar.get_bs_month_range(
            target_bs_year, target_bs_month
        )
        normalized_fy = NepaliCalendar.get_fiscal_year(target_bs_year, target_bs_month)
        bs_month_name_en = NepaliCalendar.NEPALI_MONTH_NAMES_EN[target_bs_month - 1]
        bs_month_name_np = NepaliCalendar.NEPALI_MONTH_NAMES_NP[target_bs_month - 1]
        period_label = f"{bs_month_name_en} {target_bs_year} BS ({start_date_bs} to {end_date_bs} BS)"

    return {
        'view_mode': view_mode,
        'period_type': view_mode,
        'is_fiscal_year_mode': (view_mode == 'fiscal_year'),
        'is_month_mode': (view_mode == 'month'),
        'is_custom_mode': (view_mode == 'custom'),
        'fiscal_year': normalized_fy,
        'available_fiscal_years': get_available_fiscal_years(),
        'bs_year': target_bs_year,
        'available_bs_years': [2083, 2082, 2081, 2080, 2079, 2078],
        'bs_month': target_bs_month,
        'bs_month_name_en': bs_month_name_en,
        'bs_month_name_np': bs_month_name_np,
        'nepali_months': get_available_nepali_months(),
        'start_date': start_date_ad,
        'end_date': end_date_ad,
        'start_date_str': start_date_ad.strftime('%Y-%m-%d'),
        'end_date_str': end_date_ad.strftime('%Y-%m-%d'),
        'start_date_bs': start_date_bs,
        'end_date_bs': end_date_bs,
        'period_start_ad': start_date_ad,
        'period_end_ad': end_date_ad,
        'period_label': period_label,
    }


# ==============================================================================
# 1. TAX DASHBOARD VIEW
# ==============================================================================

class TaxDashboardView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    """
    Taxation & VAT Overview Dashboard.
    Supports seamless toggling between a single BS month (e.g. Ashadh)
    and an entire Nepali Fiscal Year (e.g. 2080/81, 2081/82).
    """
    template_name = 'taxation/vat_overview.html'

    def test_func(self):
        return self.request.user.is_superuser or getattr(self.request.user, 'role', '') in [
            'OWNER', 'ACCOUNTANT', 'MANAGER'
        ]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # 1. Resolve Active Branch Scope
        active_branch = getattr(self.request, 'active_branch', None) or Branch.get_default_main_branch()
        branch_id = self.request.GET.get('branch')
        if branch_id and str(branch_id).strip() not in ['', 'all', 'None']:
            b_match = Branch.objects.filter(id=branch_id, is_active=True).first()
            if b_match:
                active_branch = b_match

        # 2. Resolve Period (Month vs Full Fiscal Year vs Custom Range)
        period_ctx = resolve_taxation_period(self.request.GET)
        start_date = period_ctx['start_date']
        end_date = period_ctx['end_date']

        # 3. Initialize Fallback Summaries
        sales_summary = {
            'taxable': Decimal('0.00'),
            'non_taxable': Decimal('0.00'),
            'vat': Decimal('0.00'),
            'grand_total': Decimal('0.00'),
            'total_refunds': Decimal('0.00')
        }
        purchase_summary = {
            'gross': Decimal('0.00'),
            'discount': Decimal('0.00'),
            'vat': Decimal('0.00'),
            'freight': Decimal('0.00'),
            'customs': Decimal('0.00'),
            'handling': Decimal('0.00'),
            'overheads': Decimal('0.00'),
            'landed': Decimal('0.00'),
            'net': Decimal('0.00'),
            'paid': Decimal('0.00'),
            'due': Decimal('0.00')
        }
        net_vat = Decimal('0.00')

        # 4. Generate Reports
        if active_branch:
            sales_data = TaxationReportGenerator.generate_sales_book(active_branch, start_date, end_date)
            purchase_data = TaxationReportGenerator.generate_purchase_book(active_branch, start_date, end_date)

            sales_summary = sales_data.get('totals', sales_summary)
            purchase_summary = purchase_data.get('totals', purchase_summary)
            net_vat = sales_summary.get('vat', Decimal('0.00')) - purchase_summary.get('vat', Decimal('0.00'))

        # 5. Populate Context
        context.update({
            'branch': active_branch,
            'branches': Branch.objects.filter(is_active=True).order_by('-is_main_branch', 'name'),
            'sales_summary': sales_summary,
            'purchase_summary': purchase_summary,
            'net_vat': net_vat,
            'config': SystemConfiguration.get_solo(),
            **period_ctx,
        })
        return NonIRDDisclaimerEngine.inject_disclaimer(context)


# ==============================================================================
# 2. INTERNAL SALES REGISTER VIEW (ANNEX 5 FORMAT)
# ==============================================================================

class SalesRegisterReportView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    """
    Internal Sales Register (बिक्री खाता - Annex 5 format).
    Enables toggling between a single BS month or an entire Nepali Fiscal Year,
    accurately deducting credit adjustments from customer returns.
    """
    template_name = 'taxation/sales_vat_register.html'

    def test_func(self):
        return self.request.user.is_superuser or getattr(self.request.user, 'role', '') in [
            'OWNER', 'ACCOUNTANT', 'MANAGER'
        ]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # 1. Resolve Branch Scope
        active_branch = getattr(self.request, 'active_branch', None) or Branch.get_default_main_branch()
        branch_id = self.request.GET.get('branch')
        if branch_id and str(branch_id).strip() not in ['', 'all', 'None']:
            b_match = Branch.objects.filter(id=branch_id, is_active=True).first()
            if b_match:
                active_branch = b_match

        # 2. Resolve Period (Month vs Full Fiscal Year vs Custom Range)
        period_ctx = resolve_taxation_period(self.request.GET)
        start_date = period_ctx['start_date']
        end_date = period_ctx['end_date']

        report_data = {
            'records': [],
            'is_vat_shop': (SystemConfiguration.get_solo().tax_system_mode == 'VAT'),
            'totals': {
                'taxable': Decimal('0.00'),
                'non_taxable': Decimal('0.00'),
                'vat': Decimal('0.00'),
                'grand_total': Decimal('0.00'),
                'total_refunds': Decimal('0.00'),
            }
        }

        if active_branch:
            report_data = TaxationReportGenerator.generate_sales_book(active_branch, start_date, end_date)

        # 3. Handle CSV Export if requested
        if self.request.GET.get('export', '').strip().lower() == 'csv':
            response = HttpResponse(content_type='text/csv; charset=utf-8')
            filename = f"Sales_Register_Annex5_{period_ctx['start_date_str']}_{period_ctx['end_date_str']}.csv"
            response['Content-Disposition'] = f'attachment; filename="{filename}"'

            writer = csv.writer(response)
            writer.writerow([
                'Bill Date (AD)', 'Bill Date (BS)', 'Estimate Slip No', 'Customer Name',
                'Customer Phone', 'Customer PAN', 'Taxable Base (NPR)', 'Non-Taxable Base (NPR)',
                'Tax / VAT Amount (NPR)', 'Payable Grand Total (NPR)'
            ])

            for row in report_data.get('records', []):
                writer.writerow(sanitize_csv_row([
                    row.bill_date_ad,
                    row.bill_date_bs or ad_to_bs_string(row.bill_date_ad, lang='en'),
                    row.estimate_number,
                    row.recipient_display_name,
                    row.customer_phone_manual or (row.customer.phone_number if row.customer else ''),
                    row.customer_pan or (row.customer.pan_number if row.customer and row.customer.pan_number else ''),
                    f"{row.taxable_amount:.2f}",
                    f"{row.non_taxable_amount:.2f}",
                    f"{row.vat_amount:.2f}",
                    f"{row.grand_total:.2f}"
                ]))

            t = report_data.get('totals', {})
            writer.writerow(sanitize_csv_row([
                'TOTALS', '', f"{len(report_data.get('records', []))} Invoices", '', '', '',
                f"{t.get('taxable', Decimal('0.00')):.2f}",
                f"{t.get('non_taxable', Decimal('0.00')):.2f}",
                f"{t.get('vat', Decimal('0.00')):.2f}",
                f"{t.get('grand_total', Decimal('0.00')):.2f}"
            ]))
            return response

        context.update({
            'branch': active_branch,
            'branches': Branch.objects.filter(is_active=True).order_by('-is_main_branch', 'name'),
            'report': report_data,
            'records': report_data.get('records', []),
            'totals': report_data.get('totals', {}),
            'config': SystemConfiguration.get_solo(),
            **period_ctx,
        })
        return NonIRDDisclaimerEngine.inject_disclaimer(context)


# ==============================================================================
# 3. COMMERCIAL PURCHASE REGISTER VIEW (ANNEX 7 FORMAT)
# ==============================================================================

class PurchaseRegisterReportView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    """
    Commercial Purchase Register (खरिद खाता - Annex 7 format).
    Enables inspecting inward purchases across any single BS month or full Fiscal Year
    with complete trade discounts, input VAT, shipping overheads, landed costs, and supplier payables.
    """
    template_name = 'taxation/purchase_vat_register.html'

    def test_func(self):
        return self.request.user.is_superuser or getattr(self.request.user, 'role', '') in [
            'OWNER', 'ACCOUNTANT', 'MANAGER'
        ]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # 1. Resolve Branch Scope
        active_branch = getattr(self.request, 'active_branch', None) or Branch.get_default_main_branch()
        branch_id = self.request.GET.get('branch')
        if branch_id and str(branch_id).strip() not in ['', 'all', 'None']:
            b_match = Branch.objects.filter(id=branch_id, is_active=True).first()
            if b_match:
                active_branch = b_match

        # 2. Resolve Period (Month vs Full Fiscal Year vs Custom Range)
        period_ctx = resolve_taxation_period(self.request.GET)
        start_date = period_ctx['start_date']
        end_date = period_ctx['end_date']

        report_data = {
            'records': [],
            'totals': {
                'gross': Decimal('0.00'),
                'discount': Decimal('0.00'),
                'vat': Decimal('0.00'),
                'freight': Decimal('0.00'),
                'customs': Decimal('0.00'),
                'handling': Decimal('0.00'),
                'overheads': Decimal('0.00'),
                'landed': Decimal('0.00'),
                'net': Decimal('0.00'),
                'paid': Decimal('0.00'),
                'due': Decimal('0.00'),
            }
        }

        if active_branch:
            report_data = TaxationReportGenerator.generate_purchase_book(active_branch, start_date, end_date)

        # Build dual-compatible totals dictionary supporting both key naming conventions
        raw_totals = report_data.get('totals', {})
        totals_context = {
            **raw_totals,
            'total_gross': raw_totals.get('gross', Decimal('0.00')),
            'total_discount': raw_totals.get('discount', Decimal('0.00')),
            'total_vat': raw_totals.get('vat', Decimal('0.00')),
            'total_freight': raw_totals.get('freight', Decimal('0.00')),
            'total_customs': raw_totals.get('customs', Decimal('0.00')),
            'total_handling': raw_totals.get('handling', Decimal('0.00')),
            'total_overheads': raw_totals.get('overheads', Decimal('0.00')),
            'total_landed': raw_totals.get('landed', Decimal('0.00')),
            'total_net': raw_totals.get('net', Decimal('0.00')),
            'total_paid': raw_totals.get('paid', Decimal('0.00')),
            'total_due': raw_totals.get('due', Decimal('0.00')),
        }

        report_data['totals'].update(totals_context)
        records_context = report_data.get('records', [])

        # 3. Handle File Exports
        export_mode = self.request.GET.get('export', '').strip().lower()
        if export_mode == 'csv':
            return CSVExportEngine.export_purchase_register_csv(
                records=records_context,
                totals=totals_context,
                filename=f"Purchase_Register_Annex7_{period_ctx['start_date_str']}_{period_ctx['end_date_str']}.csv"
            )
        elif export_mode == 'excel':
            return CSVExportEngine.export_purchase_register_excel(
                records=records_context,
                totals=totals_context,
                date_range_label=period_ctx['period_label'],
                branch_name=active_branch.name if active_branch else "All Outlets",
                filename=f"Purchase_Register_Annex7_{period_ctx['start_date_str']}_{period_ctx['end_date_str']}.xlsx"
            )

        context.update({
            'report': report_data,
            'totals': totals_context,
            'records': records_context,
            'config': SystemConfiguration.get_solo(),
            'branch': active_branch,
            'branches': Branch.objects.filter(is_active=True).order_by('-is_main_branch', 'name'),
            **period_ctx,
        })
        return NonIRDDisclaimerEngine.inject_disclaimer(context)