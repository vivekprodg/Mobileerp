import csv
import io
import json
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Any, List, Tuple, Optional, Union

from django.shortcuts import render, get_object_or_404, redirect
from django.views.generic import TemplateView, View
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.http import HttpResponse, JsonResponse, HttpResponsePermanentRedirect
from django.utils import timezone
from django.urls import reverse
from django.core.serializers.json import DjangoJSONEncoder

from apps.taxation.reports import TaxationReportGenerator
from apps.taxation.ird_sync import NonIRDDisclaimerEngine
from apps.taxation.services.vat_ledger_service import VATLedgerService
from apps.branches.models import Branch
from apps.sales.models import SalesEstimate, SalesReturn, SalesReturnItem
from apps.purchases.models import GoodsReceivedNote, PurchaseReturn, PurchaseReturnItem
from apps.core.models import SystemConfiguration
from apps.core.nepali_calendar import NepaliCalendar
from apps.core.utils.nepali_date_converter import ad_to_bs_string
from apps.reports.exports import CSVExportEngine, sanitize_csv_row


class SafeTaxJSONEncoder(DjangoJSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return str(obj.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
        if isinstance(obj, (date, datetime)):
            return obj.isoformat()
        return super().default(obj)


def get_available_fiscal_years() -> List[str]:
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
    return [
        {
            'num': i + 1,
            'name_en': NepaliCalendar.NEPALI_MONTH_NAMES_EN[i],
            'name_np': NepaliCalendar.NEPALI_MONTH_NAMES_NP[i]
        }
        for i in range(12)
    ]


def parse_flexible_date(val_str: str) -> Optional[Tuple[date, str]]:
    if not val_str or not str(val_str).strip():
        return None

    clean = re.sub(r'[^\d]', '-', str(val_str).strip())
    parts = [int(p) for p in clean.split('-') if p]
    if len(parts) != 3:
        return None

    if 2000 <= parts[0] <= 2095:
        bs_y, bs_m, bs_d = parts[0], parts[1], parts[2]
        bs_m = max(1, min(12, bs_m))
        max_days = NepaliCalendar.get_days_in_month(bs_y, bs_m)
        bs_d = max(1, min(max_days, bs_d))
        ad_date = NepaliCalendar.bs_to_ad(bs_y, bs_m, bs_d)
        bs_str = f"{bs_y:04d}-{bs_m:02d}-{bs_d:02d}"
        return ad_date, bs_str

    elif 2000 <= parts[2] <= 2095:
        bs_y, bs_m, bs_d = parts[2], parts[1], parts[0]
        bs_m = max(1, min(12, bs_m))
        max_days = NepaliCalendar.get_days_in_month(bs_y, bs_m)
        bs_d = max(1, min(max_days, bs_d))
        ad_date = NepaliCalendar.bs_to_ad(bs_y, bs_m, bs_d)
        bs_str = f"{bs_y:04d}-{bs_m:02d}-{bs_d:02d}"
        return ad_date, bs_str

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
    today_ad = timezone.now().date()
    today_bs_y, today_bs_m, today_bs_d = NepaliCalendar.ad_to_bs(today_ad)
    current_fy = NepaliCalendar.get_fiscal_year(today_bs_y, today_bs_m)

    view_mode = str(raw_params.get('view_mode') or raw_params.get('period_type') or '').strip().lower()
    fy_param = str(raw_params.get('fiscal_year') or raw_params.get('fy') or '').strip()
    bs_year_param = str(raw_params.get('bs_year') or '').strip()
    bs_month_param = str(raw_params.get('bs_month') or '').strip()
    start_param = str(raw_params.get('start_date') or raw_params.get('start_date_bs') or '').strip()
    end_param = str(raw_params.get('end_date') or raw_params.get('end_date_bs') or '').strip()

    if not view_mode:
        if fy_param and not bs_month_param and not start_param and not end_param:
            view_mode = 'fiscal_year'
        elif start_param and end_param and not fy_param and not bs_month_param:
            view_mode = 'custom'
        else:
            view_mode = 'month'

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


class TaxationAccessMixin(LoginRequiredMixin, UserPassesTestMixin):
    def test_func(self):
        user = self.request.user
        return user.is_authenticated and (
            user.is_superuser or getattr(user, 'role', '') in ['OWNER', 'ACCOUNTANT', 'MANAGER']
        )


class TaxDashboardView(TaxationAccessMixin, TemplateView):
    template_name = 'taxation/vat_overview.html'

    def get(self, request, *args, **kwargs):
        active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()
        branch_id = request.GET.get('branch')
        if branch_id and str(branch_id).strip() not in ['', 'all', 'None']:
            b_match = Branch.objects.filter(id=branch_id, is_active=True).first()
            if b_match:
                active_branch = b_match

        period_ctx = resolve_taxation_period(request.GET)
        start_date = period_ctx['start_date']
        end_date = period_ctx['end_date']
        bs_year = period_ctx['bs_year']
        bs_month = period_ctx['bs_month']
        fiscal_year = period_ctx['fiscal_year']

        cockpit_data = VATLedgerService.get_unified_vat_cockpit(
            branch=active_branch,
            start_date=start_date,
            end_date=end_date,
            bs_year=bs_year,
            bs_month=bs_month,
            fiscal_year_name=fiscal_year,
            include_drilldown=True
        )

        export_mode = request.GET.get('export', '').strip().lower()
        if export_mode == 'csv':
            return self.export_daily_ledger_csv(cockpit_data['daily_ledger'], period_ctx)
        elif export_mode == 'excel':
            return self.export_daily_ledger_excel(cockpit_data['daily_ledger'], period_ctx, active_branch)

        context = self.get_context_data(
            active_branch=active_branch,
            period_ctx=period_ctx,
            cockpit_data=cockpit_data
        )
        return self.render_to_response(NonIRDDisclaimerEngine.inject_disclaimer(context))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        active_branch = kwargs.get('active_branch')
        period_ctx = kwargs.get('period_ctx') or resolve_taxation_period(self.request.GET)
        cockpit_data = kwargs.get('cockpit_data') or {}

        config = SystemConfiguration.get_solo()
        is_vat_mode = (config.tax_system_mode == 'VAT')

        sync_query_dict = self.request.GET.copy()
        if active_branch and 'branch' not in sync_query_dict:
            sync_query_dict['branch'] = str(active_branch.id)
        if period_ctx.get('bs_year') and 'bs_year' not in sync_query_dict:
            sync_query_dict['bs_year'] = str(period_ctx['bs_year'])
        if period_ctx.get('bs_month') and 'bs_month' not in sync_query_dict:
            sync_query_dict['bs_month'] = str(period_ctx['bs_month'])

        encoded_sync_query = sync_query_dict.urlencode()

        context.update({
            'branch': active_branch,
            'branches': Branch.objects.filter(is_active=True).order_by('-is_main_branch', 'name'),
            'sales_summary': cockpit_data.get('sales_summary', {}),
            'purchase_summary': cockpit_data.get('purchase_summary', {}),
            'net_vat': cockpit_data.get('net_vat', Decimal('0.00')),
            'is_net_payable': cockpit_data.get('is_net_payable', False),
            'is_net_credit': cockpit_data.get('is_net_credit', False),
            'final_cumulative_balance': cockpit_data.get('final_cumulative_balance', Decimal('0.00')),
            'daily_ledger': cockpit_data.get('daily_ledger', {}),
            'daily_rows': cockpit_data.get('daily_rows', []),
            'totals': cockpit_data.get('daily_ledger', {}).get('totals', {}),
            'total_days_in_period': cockpit_data.get('total_days_in_period', 0),
            'active_tax_days_count': cockpit_data.get('active_tax_days_count', 0),
            'six_month_summary': cockpit_data.get('six_month_summary', {}),
            'six_month_rows': cockpit_data.get('six_month_rows', []),
            'six_month_totals': cockpit_data.get('six_month_totals', {}),
            'comparison': cockpit_data.get('comparison', {}),
            'comparison_rows': cockpit_data.get('comparison_rows', []),
            'config': config,
            'SYS_CONFIG': config,
            'IS_VAT_MODE': is_vat_mode,
            'SHOP_TAX_MODE': config.tax_system_mode,
            'DEFAULT_TAX_RATE': str(config.default_vat_rate) if is_vat_mode else '0.00',
            'drilldown_urls': {
                'sales_register': f"/taxation/sales-register/?{encoded_sync_query}",
                'purchase_register': f"/taxation/purchase-register/?{encoded_sync_query}",
                'daily_vat_report': f"/taxation/?{encoded_sync_query}",
                'six_month_report': f"/taxation/six-month-report/?{encoded_sync_query}",
                'reconciliation': f"/taxation/reconciliation/?{encoded_sync_query}",
            },
            **period_ctx,
        })
        return context

    def export_daily_ledger_csv(self, report_data: dict, period_ctx: dict) -> HttpResponse:
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        filename = f"Day_Wise_VAT_Ledger_{period_ctx['start_date_str']}_{period_ctx['end_date_str']}.csv"
        response['Content-Disposition'] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        writer.writerow([
            'Nepali Date (BS)', 'Date (AD)', 'Day',
            'Sales Taxable (NPR)', 'Output VAT Collected (NPR)', 'Sales Grand Total (NPR)',
            'Purchase Taxable (NPR)', 'Input VAT Paid (NPR)', 'Purchase Net Total (NPR)',
            'Daily Net VAT (NPR)', 'Daily Tax Status', 'Running Cumulative Balance (NPR)'
        ])

        for row in report_data.get('daily_rows', []):
            status_text = "Payable" if row['is_daily_payable'] else ("Credit" if row['is_daily_credit'] else "Balanced")
            writer.writerow(sanitize_csv_row([
                row['date_bs'],
                row['date_ad_str'],
                row['day_name'],
                f"{row['sales_taxable']:.2f}",
                f"{row['sales_vat']:.2f}",
                f"{row['sales_grand_total']:.2f}",
                f"{row['purchase_taxable']:.2f}",
                f"{row['purchase_vat']:.2f}",
                f"{row['purchase_net_total']:.2f}",
                f"{row['daily_net_vat']:.2f}",
                status_text,
                f"{row['cumulative_balance']:.2f}"
            ]))

        t = report_data.get('totals', {})
        writer.writerow(sanitize_csv_row([
            'GRAND TOTALS', '', f"{report_data.get('active_tax_days_count', 0)} Active Days",
            f"{t.get('sales_taxable', Decimal('0.00')):.2f}",
            f"{t.get('sales_vat', Decimal('0.00')):.2f}",
            f"{t.get('sales_grand_total', Decimal('0.00')):.2f}",
            f"{t.get('purchase_taxable', Decimal('0.00')):.2f}",
            f"{t.get('purchase_vat', Decimal('0.00')):.2f}",
            f"{t.get('purchase_net_total', Decimal('0.00')):.2f}",
            f"{t.get('net_vat', Decimal('0.00')):.2f}",
            "Payable" if t.get('is_net_payable') else "Credit",
            f"{t.get('final_cumulative_balance', Decimal('0.00')):.2f}"
        ]))
        return response

    def export_daily_ledger_excel(self, report_data: dict, period_ctx: dict, branch: Optional[Branch]) -> HttpResponse:
        try:
            import openpyxl
            from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
            from openpyxl.utils import get_column_letter

            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "Day-Wise VAT Ledger"
            ws.views.sheetView[0].showGridLines = True

            header_fill = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
            total_fill = PatternFill(start_color="F1F5F9", end_color="F1F5F9", fill_type="solid")
            white_bold = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
            title_font = Font(name="Calibri", size=14, bold=True, color="0F172A")
            regular_font = Font(name="Calibri", size=10)
            bold_font = Font(name="Calibri", size=10, bold=True)

            thin_border = Border(
                left=Side(style='thin', color='E2E8F0'),
                right=Side(style='thin', color='E2E8F0'),
                top=Side(style='thin', color='E2E8F0'),
                bottom=Side(style='thin', color='E2E8F0')
            )

            branch_name = branch.name if branch else "All Store Outlets"
            ws.merge_cells('A1:L1')
            ws['A1'] = f"{branch_name.upper()} - DAY-WISE VAT & TAX ASSESSMENT LEDGER"
            ws['A1'].font = title_font
            ws['A1'].alignment = Alignment(horizontal="center", vertical="center")
            ws.row_dimensions[1].height = 24

            ws.merge_cells('A2:L2')
            ws['A2'] = f"Period Scope: {period_ctx['period_label']} | AD: {period_ctx['start_date_str']} to {period_ctx['end_date_str']}"
            ws['A2'].font = Font(name="Calibri", size=10, italic=True, color="64748B")
            ws['A2'].alignment = Alignment(horizontal="center", vertical="center")

            headers = [
                'Nepali Date (BS)', 'Date (AD)', 'Day',
                'Sales Taxable (NPR)', 'Output VAT (NPR)', 'Sales Total (NPR)',
                'Purchases Taxable (NPR)', 'Input VAT (NPR)', 'Purchases Total (NPR)',
                'Daily Net VAT (NPR)', 'Tax State', 'Running Balance (NPR)'
            ]

            ws.row_dimensions[4].height = 22
            for col_idx, header in enumerate(headers, 1):
                cell = ws.cell(row=4, column=col_idx, value=header)
                cell.font = white_bold
                cell.fill = header_fill
                cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

            current_row = 5
            for row in report_data.get('daily_rows', []):
                ws.row_dimensions[current_row].height = 18
                ws.cell(row=current_row, column=1, value=row['date_bs']).alignment = Alignment(horizontal="center")
                ws.cell(row=current_row, column=2, value=row['date_ad_str']).alignment = Alignment(horizontal="center")
                ws.cell(row=current_row, column=3, value=row['day_name']).alignment = Alignment(horizontal="center")

                ws.cell(row=current_row, column=4, value=float(row['sales_taxable']))
                ws.cell(row=current_row, column=5, value=float(row['sales_vat']))
                ws.cell(row=current_row, column=6, value=float(row['sales_grand_total']))
                ws.cell(row=current_row, column=7, value=float(row['purchase_taxable']))
                ws.cell(row=current_row, column=8, value=float(row['purchase_vat']))
                ws.cell(row=current_row, column=9, value=float(row['purchase_net_total']))
                ws.cell(row=current_row, column=10, value=float(row['daily_net_vat']))

                status_text = "Payable" if row['is_daily_payable'] else ("Credit" if row['is_daily_credit'] else "Balanced")
                c11 = ws.cell(row=current_row, column=11, value=status_text)
                c11.alignment = Alignment(horizontal="center")

                ws.cell(row=current_row, column=12, value=float(row['cumulative_balance']))

                for col_idx in [4, 5, 6, 7, 8, 9, 10, 12]:
                    cell = ws.cell(row=current_row, column=col_idx)
                    cell.number_format = '#,##0.00'
                    cell.font = regular_font

                for c_idx in range(1, 13):
                    ws.cell(row=current_row, column=c_idx).border = thin_border

                current_row += 1

            t = report_data.get('totals', {})
            ws.row_dimensions[current_row].height = 22
            ws.cell(row=current_row, column=1, value="GRAND TOTALS").font = bold_font
            ws.cell(row=current_row, column=2, value="")
            ws.cell(row=current_row, column=3, value=f"{report_data.get('active_tax_days_count', 0)} Active Days").font = bold_font

            ws.cell(row=current_row, column=4, value=float(t.get('sales_taxable', 0)))
            ws.cell(row=current_row, column=5, value=float(t.get('sales_vat', 0)))
            ws.cell(row=current_row, column=6, value=float(t.get('sales_grand_total', 0)))
            ws.cell(row=current_row, column=7, value=float(t.get('purchase_taxable', 0)))
            ws.cell(row=current_row, column=8, value=float(t.get('purchase_vat', 0)))
            ws.cell(row=current_row, column=9, value=float(t.get('purchase_net_total', 0)))
            ws.cell(row=current_row, column=10, value=float(t.get('net_vat', 0)))
            ws.cell(row=current_row, column=11, value="Payable" if t.get('is_net_payable') else "Credit")
            ws.cell(row=current_row, column=12, value=float(t.get('final_cumulative_balance', 0)))

            for col_idx in range(1, 13):
                cell = ws.cell(row=current_row, column=col_idx)
                cell.fill = total_fill
                cell.font = bold_font
                cell.border = thin_border
                if col_idx in [4, 5, 6, 7, 8, 9, 10, 12]:
                    cell.number_format = '#,##0.00'

            for col in ws.columns:
                max_len = 0
                col_letter = get_column_letter(col[0].column)
                for cell in col:
                    if cell.row > 2 and cell.value:
                        max_len = max(max_len, len(str(cell.value)))
                ws.column_dimensions[col_letter].width = max(max_len + 4, 12)

            buffer = io.BytesIO()
            wb.save(buffer)
            buffer.seek(0)

            response = HttpResponse(
                buffer.getvalue(),
                content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            )
            filename = f"Day_Wise_VAT_Ledger_{period_ctx['start_date_str']}_{period_ctx['end_date_str']}.xlsx"
            response['Content-Disposition'] = f'attachment; filename="{filename}"'
            return response

        except ImportError:
            return self.export_daily_ledger_csv(report_data, period_ctx)


class DailyVatReportView(TaxationAccessMixin, View):
    def get(self, request, *args, **kwargs):
        drilldown_target = request.GET.get('drilldown', '').strip().lower()
        if drilldown_target:
            active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()
            branch_id = request.GET.get('branch')
            if branch_id and str(branch_id).strip() not in ['', 'all', 'None']:
                b_match = Branch.objects.filter(id=branch_id, is_active=True).first()
                if b_match:
                    active_branch = b_match

            return self._handle_drilldown_request(request, active_branch, drilldown_target)

        if request.GET.get('format', '').strip().lower() == 'json' or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()
            period_ctx = resolve_taxation_period(request.GET)
            report_data = TaxationReportGenerator.generate_daily_vat_ledger(
                active_branch, period_ctx['start_date'], period_ctx['end_date'], include_drilldown=True
            )
            return JsonResponse({'status': 'success', 'data': report_data}, encoder=SafeTaxJSONEncoder)

        query_string = request.META.get('QUERY_STRING', '')
        target_url = reverse('taxation:dashboard')
        if query_string:
            target_url = f"{target_url}?{query_string}"

        return redirect(target_url)

    def _handle_drilldown_request(self, request, branch: Branch, target: str) -> JsonResponse:
        if target == 'day':
            date_str = request.GET.get('date', '').strip()
            parsed = parse_flexible_date(date_str)
            target_date = parsed[0] if parsed else timezone.now().date()
            day_data = TaxationReportGenerator.get_date_vat_drilldown(branch, target_date)
            return JsonResponse({'status': 'success', 'data': day_data}, encoder=SafeTaxJSONEncoder)

        elif target in ['invoice', 'sales']:
            inv_id = request.GET.get('id', '').strip()
            inv_data = TaxationReportGenerator.get_invoice_vat_drilldown(inv_id)
            return JsonResponse(inv_data, encoder=SafeTaxJSONEncoder)

        elif target in ['grn', 'purchase']:
            grn_id = request.GET.get('id', '').strip()
            grn_data = TaxationReportGenerator.get_grn_vat_drilldown(grn_id)
            return JsonResponse(grn_data, encoder=SafeTaxJSONEncoder)

        elif target in ['sales_return', 'credit_note', 'sale_return', 'return']:
            ret_id = request.GET.get('id') or request.GET.get('ref', '')
            data = VatDrilldownAPIView.get_sales_return_vat_drilldown(ret_id)
            return JsonResponse(data, encoder=SafeTaxJSONEncoder)

        elif target in ['purchase_return', 'debit_note', 'pret']:
            pret_id = request.GET.get('id') or request.GET.get('ref', '')
            data = VatDrilldownAPIView.get_purchase_return_vat_drilldown(pret_id)
            return JsonResponse(data, encoder=SafeTaxJSONEncoder)

        return JsonResponse({'status': 'error', 'message': f'Unknown drill-down target: {target}'}, status=400)


class SalesRegisterReportView(TaxationAccessMixin, TemplateView):
    template_name = 'taxation/sales_vat_register.html'

    def get(self, request, *args, **kwargs):
        active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()
        branch_id = request.GET.get('branch')
        if branch_id and str(branch_id).strip() not in ['', 'all', 'None']:
            b_match = Branch.objects.filter(id=branch_id, is_active=True).first()
            if b_match:
                active_branch = b_match

        config = SystemConfiguration.get_solo()
        is_vat_mode = (config.tax_system_mode == 'VAT')

        period_ctx = resolve_taxation_period(request.GET)
        start_date = period_ctx['start_date']
        end_date = period_ctx['end_date']

        report_data = {
            'records': [],
            'document_breakdown': [],
            'returns_breakdown': [],
            'is_vat_shop': is_vat_mode,
            'totals': {
                'taxable': Decimal('0.00'), 'non_taxable': Decimal('0.00'),
                'vat': Decimal('0.00'), 'grand_total': Decimal('0.00'), 'total_refunds': Decimal('0.00'),
            }
        }

        if active_branch:
            report_data = TaxationReportGenerator.generate_sales_book(
                active_branch, start_date, end_date, include_drilldown=True
            )

        if request.GET.get('format', '').strip().lower() == 'json' or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'status': 'success',
                'period': period_ctx['period_label'],
                'branch': active_branch.name if active_branch else '',
                'totals': report_data.get('totals', {}),
                'document_breakdown': report_data.get('document_breakdown', []),
                'returns_breakdown': report_data.get('returns_breakdown', []),
            }, encoder=SafeTaxJSONEncoder)

        if request.GET.get('export', '').strip().lower() == 'csv':
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

        context = self.get_context_data(
            branch=active_branch,
            branches=Branch.objects.filter(is_active=True).order_by('-is_main_branch', 'name'),
            report=report_data,
            records=report_data.get('records', []),
            document_breakdown=report_data.get('document_breakdown', []),
            returns_breakdown=report_data.get('returns_breakdown', []),
            totals=report_data.get('totals', {}),
            config=config,
            SYS_CONFIG=config,
            IS_VAT_MODE=is_vat_mode,
            SHOP_TAX_MODE=config.tax_system_mode,
            DEFAULT_TAX_RATE=str(config.default_vat_rate) if is_vat_mode else '0.00',
            **period_ctx,
        )
        return self.render_to_response(NonIRDDisclaimerEngine.inject_disclaimer(context))


class PurchaseRegisterReportView(TaxationAccessMixin, TemplateView):
    template_name = 'taxation/purchase_vat_register.html'

    def get(self, request, *args, **kwargs):
        active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()
        branch_id = request.GET.get('branch')
        if branch_id and str(branch_id).strip() not in ['', 'all', 'None']:
            b_match = Branch.objects.filter(id=branch_id, is_active=True).first()
            if b_match:
                active_branch = b_match

        config = SystemConfiguration.get_solo()
        is_vat_mode = (config.tax_system_mode == 'VAT')

        period_ctx = resolve_taxation_period(request.GET)
        start_date = period_ctx['start_date']
        end_date = period_ctx['end_date']

        report_data = {
            'records': [],
            'document_breakdown': [],
            'returns_breakdown': [],
            'totals': {
                'gross': Decimal('0.00'), 'discount': Decimal('0.00'), 'taxable': Decimal('0.00'),
                'vat': Decimal('0.00'), 'freight': Decimal('0.00'), 'customs': Decimal('0.00'),
                'handling': Decimal('0.00'), 'overheads': Decimal('0.00'), 'landed': Decimal('0.00'),
                'net': Decimal('0.00'), 'paid': Decimal('0.00'), 'due': Decimal('0.00'),
            }
        }

        if active_branch:
            report_data = TaxationReportGenerator.generate_purchase_book(
                active_branch, start_date, end_date, include_drilldown=True
            )

        raw_totals = report_data.get('totals', {})
        totals_context = {
            **raw_totals,
            'total_gross': raw_totals.get('gross', Decimal('0.00')),
            'total_discount': raw_totals.get('discount', Decimal('0.00')),
            'total_taxable': raw_totals.get('taxable', Decimal('0.00')),
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

        if request.GET.get('format', '').strip().lower() == 'json' or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'status': 'success',
                'period': period_ctx['period_label'],
                'branch': active_branch.name if active_branch else '',
                'totals': totals_context,
                'document_breakdown': report_data.get('document_breakdown', []),
                'returns_breakdown': report_data.get('returns_breakdown', []),
            }, encoder=SafeTaxJSONEncoder)

        export_mode = request.GET.get('export', '').strip().lower()
        if export_mode == 'csv':
            return CSVExportEngine.export_purchase_register_csv(
                records=report_data.get('records', []),
                totals=totals_context,
                filename=f"Purchase_Register_Annex7_{period_ctx['start_date_str']}_{period_ctx['end_date_str']}.csv"
            )
        elif export_mode == 'excel':
            return CSVExportEngine.export_purchase_register_excel(
                records=report_data.get('records', []),
                totals=totals_context,
                date_range_label=period_ctx['period_label'],
                branch_name=active_branch.name if active_branch else "All Outlets",
                filename=f"Purchase_Register_Annex7_{period_ctx['start_date_str']}_{period_ctx['end_date_str']}.xlsx"
            )

        context = self.get_context_data(
            report=report_data,
            totals=totals_context,
            records=report_data.get('records', []),
            document_breakdown=report_data.get('document_breakdown', []),
            returns_breakdown=report_data.get('returns_breakdown', []),
            config=config,
            SYS_CONFIG=config,
            IS_VAT_MODE=is_vat_mode,
            SHOP_TAX_MODE=config.tax_system_mode,
            DEFAULT_TAX_RATE=str(config.default_vat_rate) if is_vat_mode else '0.00',
            branch=active_branch,
            branches=Branch.objects.filter(is_active=True).order_by('-is_main_branch', 'name'),
            **period_ctx,
        )
        return self.render_to_response(NonIRDDisclaimerEngine.inject_disclaimer(context))


class SixMonthVatReportView(TaxationAccessMixin, TemplateView):
    template_name = 'taxation/six_month_vat_report.html'

    def get(self, request, *args, **kwargs):
        active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()
        branch_id = request.GET.get('branch')
        if branch_id and str(branch_id).strip() not in ['', 'all', 'None']:
            b_match = Branch.objects.filter(id=branch_id, is_active=True).first()
            if b_match:
                active_branch = b_match

        config = SystemConfiguration.get_solo()
        is_vat_mode = (config.tax_system_mode == 'VAT')

        def _parse_int_param(param_name: str) -> Optional[int]:
            val = request.GET.get(param_name, '').strip()
            if val and val.isdigit():
                return int(val)
            return None

        start_bs_year = _parse_int_param('start_bs_year')
        start_bs_month = _parse_int_param('start_bs_month')
        end_bs_year = _parse_int_param('end_bs_year')
        end_bs_month = _parse_int_param('end_bs_month')

        if not end_bs_year and not start_bs_year:
            fallback_year = _parse_int_param('bs_year')
            fallback_month = _parse_int_param('bs_month')
            if fallback_year and fallback_month:
                end_bs_year = fallback_year
                end_bs_month = fallback_month

        fiscal_year = request.GET.get('fiscal_year', '').strip() or None
        half = _parse_int_param('half')

        report_data = TaxationReportGenerator.generate_six_month_vat_summary(
            branch=active_branch,
            end_bs_year=end_bs_year,
            end_bs_month=end_bs_month,
            start_bs_year=start_bs_year,
            start_bs_month=start_bs_month,
            fiscal_year_name=fiscal_year,
            half=half,
            include_comparison=True
        )

        if request.GET.get('format', '').strip().lower() == 'json' or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'status': 'success',
                'branch': active_branch.name if active_branch else '',
                'data': report_data
            }, encoder=SafeTaxJSONEncoder)

        export_mode = request.GET.get('export', '').strip().lower()
        if export_mode == 'csv':
            return self.export_csv(report_data)
        elif export_mode == 'excel':
            return self.export_excel(report_data, active_branch)

        comparison_dict = report_data.get('comparison', {})
        context = self.get_context_data(
            branch=active_branch,
            branches=Branch.objects.filter(is_active=True).order_by('-is_main_branch', 'name'),
            report=report_data,
            monthly_breakdown=report_data.get('monthly_breakdown', []),
            comparison=comparison_dict,
            comparison_rows=comparison_dict.get('comparison_rows', []),
            totals={
                'output_vat': report_data.get('output_vat', Decimal('0.00')),
                'gross_output_vat': report_data.get('gross_output_vat', Decimal('0.00')),
                'sales_return_vat': report_data.get('sales_return_vat', Decimal('0.00')),
                'net_output_vat': report_data.get('net_output_vat', Decimal('0.00')),
                'input_vat': report_data.get('input_vat', Decimal('0.00')),
                'gross_input_vat': report_data.get('gross_input_vat', Decimal('0.00')),
                'purchase_return_vat': report_data.get('purchase_return_vat', Decimal('0.00')),
                'net_input_vat': report_data.get('net_input_vat', Decimal('0.00')),
                'net_vat': report_data.get('net_vat', Decimal('0.00')),
                'is_net_payable': report_data.get('is_net_payable', False),
                'is_net_credit': report_data.get('is_net_credit', False),
                'final_cumulative_balance': report_data.get('final_cumulative_balance', Decimal('0.00')),
            },
            period_label=report_data.get('period_label', ''),
            start_date_bs=report_data.get('start_date_bs', ''),
            end_date_bs=report_data.get('end_date_bs', ''),
            start_date=report_data.get('start_date_ad'),
            end_date=report_data.get('end_date_ad'),
            available_fiscal_years=get_available_fiscal_years(),
            nepali_months=get_available_nepali_months(),
            available_bs_years=[2083, 2082, 2081, 2080, 2079, 2078],
            config=config,
            SYS_CONFIG=config,
            IS_VAT_MODE=is_vat_mode,
            SHOP_TAX_MODE=config.tax_system_mode,
            DEFAULT_TAX_RATE=str(config.default_vat_rate) if is_vat_mode else '0.00',
            selected_start_bs_year=start_bs_year,
            selected_start_bs_month=start_bs_month,
            selected_end_bs_year=end_bs_year,
            selected_end_bs_month=end_bs_month,
            selected_fiscal_year=fiscal_year,
            selected_half=half,
            drilldown_urls={
                'vat_overview': '/taxation/',
                'daily_report': '/taxation/',
                'sales_register': '/taxation/sales-register/',
                'purchase_register': '/taxation/purchase-register/',
                'reconciliation': '/taxation/reconciliation/',
            }
        )
        return self.render_to_response(NonIRDDisclaimerEngine.inject_disclaimer(context))

    def export_csv(self, report_data: dict) -> HttpResponse:
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        start_bs = report_data.get('start_date_bs', '').replace('-', '_')
        end_bs = report_data.get('end_date_bs', '').replace('-', '_')
        filename = f"Six_Month_VAT_Summary_{start_bs}_to_{end_bs}.csv"
        response['Content-Disposition'] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        writer.writerow(['SIX-MONTH CONSECUTIVE VAT SUMMARY'])
        writer.writerow(['Period:', report_data.get('period_label', '')])
        writer.writerow(['BS Range:', f"{report_data.get('start_date_bs')} to {report_data.get('end_date_bs')}"])
        writer.writerow(['AD Range:', f"{report_data.get('start_date_ad')} to {report_data.get('end_date_ad')}"])
        writer.writerow([])

        writer.writerow([
            'BS Month', 'Date Range (BS)', 'Date Range (AD)',
            'Gross Output VAT (NPR)', 'Sales Return VAT (NPR)', 'Net Output VAT (NPR)',
            'Gross Input VAT (NPR)', 'Purchase Return VAT (NPR)', 'Net Input VAT (NPR)',
            'Net VAT Balance (NPR)', 'Tax Status', 'Cumulative Running Balance (NPR)'
        ])

        for row in report_data.get('monthly_breakdown', []):
            tax_state = "Payable" if row['net_vat'] > Decimal('0.00') else ("Credit" if row['net_vat'] < Decimal('0.00') else "Balanced")
            writer.writerow(sanitize_csv_row([
                row.get('month_label'),
                f"{row.get('start_date_bs')} to {row.get('end_date_bs')}",
                f"{row.get('start_date_ad')} to {row.get('end_date_ad')}",
                f"{row.get('gross_output_vat', Decimal('0.00')):.2f}",
                f"{row.get('sales_return_vat_reversal', Decimal('0.00')):.2f}",
                f"{row.get('net_output_vat', Decimal('0.00')):.2f}",
                f"{row.get('gross_input_vat', Decimal('0.00')):.2f}",
                f"{row.get('purchase_return_vat_reversal', Decimal('0.00')):.2f}",
                f"{row.get('net_input_vat', Decimal('0.00')):.2f}",
                f"{row.get('net_vat', Decimal('0.00')):.2f}",
                tax_state,
                f"{row.get('cumulative_balance', Decimal('0.00')):.2f}"
            ]))

        writer.writerow(sanitize_csv_row([
            '6-MONTH TOTALS', '', '',
            f"{report_data.get('gross_output_vat', Decimal('0.00')):.2f}",
            f"{report_data.get('sales_return_vat', Decimal('0.00')):.2f}",
            f"{report_data.get('net_output_vat', Decimal('0.00')):.2f}",
            f"{report_data.get('gross_input_vat', Decimal('0.00')):.2f}",
            f"{report_data.get('purchase_return_vat', Decimal('0.00')):.2f}",
            f"{report_data.get('net_input_vat', Decimal('0.00')):.2f}",
            f"{report_data.get('net_vat', Decimal('0.00')):.2f}",
            "Payable" if report_data.get('is_net_payable') else ("Credit" if report_data.get('is_net_credit') else "Balanced"),
            f"{report_data.get('final_cumulative_balance', Decimal('0.00')):.2f}"
        ]))

        comparison = report_data.get('comparison', {})
        if comparison and comparison.get('comparison_rows'):
            writer.writerow([])
            writer.writerow(['PERIOD-OVER-PERIOD COMPARISON (Current 6 Months vs. Previous 6 Months)'])
            writer.writerow([
                'Tax Dimension / Metric',
                f"Current 6M ({comparison.get('current_period_label')})",
                f"Previous 6M ({comparison.get('previous_period_label')})",
                'Difference (NPR)', 'Percentage Change', 'Trend'
            ])
            for comp_row in comparison.get('comparison_rows', []):
                writer.writerow(sanitize_csv_row([
                    comp_row.get('label'),
                    f"{comp_row.get('current_value', Decimal('0.00')):.2f}",
                    f"{comp_row.get('previous_value', Decimal('0.00')):.2f}",
                    f"{comp_row.get('difference', Decimal('0.00')):.2f}",
                    comp_row.get('percentage_difference_display', ''),
                    comp_row.get('trend', '')
                ]))

        return response

    def export_excel(self, report_data: dict, branch: Optional[Branch]) -> HttpResponse:
        try:
            import openpyxl
            from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
            from openpyxl.utils import get_column_letter

            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "6-Month VAT Summary"
            ws.views.sheetView[0].showGridLines = True

            header_fill = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
            total_fill = PatternFill(start_color="F1F5F9", end_color="F1F5F9", fill_type="solid")
            comp_hdr_fill = PatternFill(start_color="0F766E", end_color="0F766E", fill_type="solid")
            white_bold = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
            title_font = Font(name="Calibri", size=14, bold=True, color="0F172A")
            regular_font = Font(name="Calibri", size=10)
            bold_font = Font(name="Calibri", size=10, bold=True)

            thin_border = Border(
                left=Side(style='thin', color='E2E8F0'),
                right=Side(style='thin', color='E2E8F0'),
                top=Side(style='thin', color='E2E8F0'),
                bottom=Side(style='thin', color='E2E8F0')
            )

            branch_name = branch.name if branch else "All Store Outlets"
            ws.merge_cells('A1:L1')
            ws['A1'] = f"{branch_name.upper()} - 6-MONTH CONSECUTIVE VAT SUMMARY"
            ws['A1'].font = title_font
            ws['A1'].alignment = Alignment(horizontal="center", vertical="center")
            ws.row_dimensions[1].height = 24

            ws.merge_cells('A2:L2')
            ws['A2'] = f"Period: {report_data.get('period_label')} | BS: {report_data.get('start_date_bs')} to {report_data.get('end_date_bs')} | AD: {report_data.get('start_date_ad')} to {report_data.get('end_date_ad')}"
            ws['A2'].font = Font(name="Calibri", size=10, italic=True, color="64748B")
            ws['A2'].alignment = Alignment(horizontal="center", vertical="center")

            headers = [
                'BS Month', 'Date Range (BS)', 'Date Range (AD)',
                'Gross Output VAT (NPR)', 'Sales Return VAT (NPR)', 'Net Output VAT (NPR)',
                'Gross Input VAT (NPR)', 'Purchase Return VAT (NPR)', 'Net Input VAT (NPR)',
                'Net VAT (NPR)', 'Tax Status', 'Cumulative Balance (NPR)'
            ]

            ws.row_dimensions[4].height = 22
            for col_idx, header in enumerate(headers, 1):
                cell = ws.cell(row=4, column=col_idx, value=header)
                cell.font = white_bold
                cell.fill = header_fill
                cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

            current_row = 5
            for row in report_data.get('monthly_breakdown', []):
                ws.row_dimensions[current_row].height = 18
                ws.cell(row=current_row, column=1, value=row.get('month_label')).alignment = Alignment(horizontal="left")
                ws.cell(row=current_row, column=2, value=f"{row.get('start_date_bs')} - {row.get('end_date_bs')}").alignment = Alignment(horizontal="center")
                ws.cell(row=current_row, column=3, value=f"{row.get('start_date_ad')} - {row.get('end_date_ad')}").alignment = Alignment(horizontal="center")

                ws.cell(row=current_row, column=4, value=float(row.get('gross_output_vat', 0)))
                ws.cell(row=current_row, column=5, value=float(row.get('sales_return_vat_reversal', 0)))
                ws.cell(row=current_row, column=6, value=float(row.get('net_output_vat', 0)))
                ws.cell(row=current_row, column=7, value=float(row.get('gross_input_vat', 0)))
                ws.cell(row=current_row, column=8, value=float(row.get('purchase_return_vat_reversal', 0)))
                ws.cell(row=current_row, column=9, value=float(row.get('net_input_vat', 0)))
                ws.cell(row=current_row, column=10, value=float(row.get('net_vat', 0)))

                tax_state = "Payable" if row['net_vat'] > Decimal('0.00') else ("Credit" if row['net_vat'] < Decimal('0.00') else "Balanced")
                ws.cell(row=current_row, column=11, value=tax_state).alignment = Alignment(horizontal="center")
                ws.cell(row=current_row, column=12, value=float(row.get('cumulative_balance', 0)))

                for col_idx in [4, 5, 6, 7, 8, 9, 10, 12]:
                    c = ws.cell(row=current_row, column=col_idx)
                    c.number_format = '#,##0.00'
                    c.font = regular_font

                for c_idx in range(1, 13):
                    ws.cell(row=current_row, column=c_idx).border = thin_border

                current_row += 1

            ws.row_dimensions[current_row].height = 22
            ws.cell(row=current_row, column=1, value="6-MONTH TOTALS").font = bold_font
            ws.cell(row=current_row, column=2, value="")
            ws.cell(row=current_row, column=3, value="")

            ws.cell(row=current_row, column=4, value=float(report_data.get('gross_output_vat', 0)))
            ws.cell(row=current_row, column=5, value=float(report_data.get('sales_return_vat', 0)))
            ws.cell(row=current_row, column=6, value=float(report_data.get('net_output_vat', 0)))
            ws.cell(row=current_row, column=7, value=float(report_data.get('gross_input_vat', 0)))
            ws.cell(row=current_row, column=8, value=float(report_data.get('purchase_return_vat', 0)))
            ws.cell(row=current_row, column=9, value=float(report_data.get('net_input_vat', 0)))
            ws.cell(row=current_row, column=10, value=float(report_data.get('net_vat', 0)))

            total_state = "Payable" if report_data.get('is_net_payable') else ("Credit" if report_data.get('is_net_credit') else "Balanced")
            ws.cell(row=current_row, column=11, value=total_state).alignment = Alignment(horizontal="center")
            ws.cell(row=current_row, column=12, value=float(report_data.get('final_cumulative_balance', 0)))

            for col_idx in range(1, 13):
                c = ws.cell(row=current_row, column=col_idx)
                c.fill = total_fill
                c.font = bold_font
                c.border = thin_border
                if col_idx in [4, 5, 6, 7, 8, 9, 10, 12]:
                    c.number_format = '#,##0.00'

            current_row += 2

            comparison = report_data.get('comparison', {})
            if comparison and comparison.get('comparison_rows'):
                ws.cell(row=current_row, column=1, value="PERIOD-OVER-PERIOD COMPARISON (Current 6M vs. Previous 6M)").font = bold_font
                current_row += 1

                comp_headers = [
                    'Metric', 'Current 6 Months (NPR)', 'Previous 6 Months (NPR)',
                    'Difference (NPR)', '% Change', 'Trend'
                ]
                for col_idx, ch in enumerate(comp_headers, 1):
                    cell = ws.cell(row=current_row, column=col_idx, value=ch)
                    cell.font = white_bold
                    cell.fill = comp_hdr_fill
                    cell.alignment = Alignment(horizontal="center", vertical="center")

                current_row += 1

                for crow in comparison.get('comparison_rows', []):
                    ws.cell(row=current_row, column=1, value=crow.get('label')).alignment = Alignment(horizontal="left")
                    c2 = ws.cell(row=current_row, column=2, value=float(crow.get('current_value', 0)))
                    c2.number_format = '#,##0.00'
                    c3 = ws.cell(row=current_row, column=3, value=float(crow.get('previous_value', 0)))
                    c3.number_format = '#,##0.00'
                    c4 = ws.cell(row=current_row, column=4, value=float(crow.get('difference', 0)))
                    c4.number_format = '#,##0.00'
                    ws.cell(row=current_row, column=5, value=crow.get('percentage_difference_display', '')).alignment = Alignment(horizontal="right")
                    ws.cell(row=current_row, column=6, value=crow.get('trend', '')).alignment = Alignment(horizontal="center")

                    for col_idx in range(1, 7):
                        ws.cell(row=current_row, column=col_idx).border = thin_border
                        ws.cell(row=current_row, column=col_idx).font = regular_font

                    current_row += 1

            for col in ws.columns:
                max_len = 0
                col_letter = get_column_letter(col[0].column)
                for cell in col:
                    if cell.row > 2 and cell.value:
                        max_len = max(max_len, len(str(cell.value)))
                ws.column_dimensions[col_letter].width = max(max_len + 4, 13)

            buffer = io.BytesIO()
            wb.save(buffer)
            buffer.seek(0)

            start_bs = report_data.get('start_date_bs', '').replace('-', '_')
            end_bs = report_data.get('end_date_bs', '').replace('-', '_')
            filename = f"Six_Month_VAT_Summary_{start_bs}_to_{end_bs}.xlsx"

            response = HttpResponse(
                buffer.getvalue(),
                content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            )
            response['Content-Disposition'] = f'attachment; filename="{filename}"'
            return response

        except ImportError:
            return self.export_csv(report_data)


class VatGlReconciliationView(TaxationAccessMixin, TemplateView):
    template_name = 'taxation/vat_gl_reconciliation.html'

    def get(self, request, *args, **kwargs):
        active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()
        branch_id = request.GET.get('branch')
        if branch_id and str(branch_id).strip() not in ['', 'all', 'None']:
            b_match = Branch.objects.filter(id=branch_id, is_active=True).first()
            if b_match:
                active_branch = b_match

        config = SystemConfiguration.get_solo()
        is_vat_mode = (config.tax_system_mode == 'VAT')

        period_ctx = resolve_taxation_period(request.GET)
        start_date = period_ctx['start_date']
        end_date = period_ctx['end_date']

        recon_data = TaxationReportGenerator.reconcile_vat_with_general_ledger(
            branch=active_branch,
            start_date=start_date,
            end_date=end_date,
            include_transaction_reconciliation=True
        )

        if request.GET.get('format', '').strip().lower() == 'json' or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'status': 'success',
                'period': period_ctx['period_label'],
                'branch': active_branch.name if active_branch else '',
                'reconciliation': recon_data
            }, encoder=SafeTaxJSONEncoder)

        if request.GET.get('export', '').strip().lower() == 'csv':
            return self.export_csv(recon_data, period_ctx, active_branch)

        output_vat_info = recon_data.get('output_vat', {})
        input_vat_info = recon_data.get('input_vat', {})
        net_vat_info = recon_data.get('net_vat', {})
        tx_recon = recon_data.get('transaction_reconciliation', {}) or {}
        summary_info = tx_recon.get('summary', {}) or {}
        discrepancies = tx_recon.get('discrepancies', []) or []

        register_net = net_vat_info.get('register_net', Decimal('0.00'))
        gl_net = net_vat_info.get('gl_net', Decimal('0.00'))
        net_variance = abs(register_net - gl_net).quantize(Decimal('0.01'))

        context = self.get_context_data(
            branch=active_branch,
            branches=Branch.objects.filter(is_active=True).order_by('-is_main_branch', 'name'),
            recon=recon_data,
            output_vat=output_vat_info,
            input_vat=input_vat_info,
            net_vat=net_vat_info,
            net_variance=net_variance,
            is_reconciled=recon_data.get('is_reconciled', False),
            is_totals_reconciled=recon_data.get('is_totals_reconciled', False),
            is_transactions_reconciled=recon_data.get('is_transactions_reconciled', False),
            transaction_reconciliation=tx_recon,
            summary=summary_info,
            discrepancies=discrepancies,
            sales_invoices=tx_recon.get('sales_invoices', []),
            sales_returns=tx_recon.get('sales_returns', []),
            grn_purchases=tx_recon.get('grn_purchases', []),
            purchase_returns=tx_recon.get('purchase_returns', []),
            orphan_gl_entries=tx_recon.get('orphan_gl_entries', []),
            config=config,
            SYS_CONFIG=config,
            IS_VAT_MODE=is_vat_mode,
            SHOP_TAX_MODE=config.tax_system_mode,
            DEFAULT_TAX_RATE=str(config.default_vat_rate) if is_vat_mode else '0.00',
            drilldown_urls={
                'vat_overview': '/taxation/',
                'daily_report': '/taxation/',
                'sales_register': '/taxation/sales-register/',
                'purchase_register': '/taxation/purchase-register/',
                'six_month_report': '/taxation/six-month-report/',
                'reconciliation': '/taxation/reconciliation/',
            },
            **period_ctx,
        )
        return self.render_to_response(NonIRDDisclaimerEngine.inject_disclaimer(context))

    def export_csv(self, recon_data: dict, period_ctx: dict, branch: Optional[Branch]) -> HttpResponse:
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        start_str = period_ctx['start_date_str'].replace('-', '_')
        end_str = period_ctx['end_date_str'].replace('-', '_')
        filename = f"VAT_GL_Reconciliation_{start_str}_to_{end_str}.csv"
        response['Content-Disposition'] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        writer.writerow(['VAT REGISTERS TO GENERAL LEDGER AUDIT RECONCILIATION REPORT'])
        writer.writerow(['Period:', period_ctx['period_label']])
        writer.writerow(['Branch:', branch.name if branch else "Consolidated / All Outlets"])
        writer.writerow(['Overall Status:', "RECONCILED (0.00 Variance)" if recon_data.get('is_reconciled') else "DISCREPANCIES DETECTED"])
        writer.writerow([])

        out_vat = recon_data.get('output_vat', {})
        in_vat = recon_data.get('input_vat', {})
        net_vat = recon_data.get('net_vat', {})

        writer.writerow([
            'Metric', 'VAT Register Net (NPR)', 'GL Account Balance (NPR)', 'Variance (NPR)', 'Status'
        ])
        writer.writerow(sanitize_csv_row([
            'Output VAT (Sales & Returns - GL 2210)',
            f"{out_vat.get('register_amount', Decimal('0.00')):.2f}",
            f"{out_vat.get('gl_amount', Decimal('0.00')):.2f}",
            f"{out_vat.get('variance', Decimal('0.00')):.2f}",
            'MATCHED' if out_vat.get('is_matched') else 'DISCREPANCY'
        ]))
        writer.writerow(sanitize_csv_row([
            'Input VAT (Purchases & Returns - GL 1410)',
            f"{in_vat.get('register_amount', Decimal('0.00')):.2f}",
            f"{in_vat.get('gl_amount', Decimal('0.00')):.2f}",
            f"{in_vat.get('variance', Decimal('0.00')):.2f}",
            'MATCHED' if in_vat.get('is_matched') else 'DISCREPANCY'
        ]))
        net_var = abs(net_vat.get('register_net', Decimal('0.00')) - net_vat.get('gl_net', Decimal('0.00')))
        writer.writerow(sanitize_csv_row([
            'Net VAT Assessment (Output - Input)',
            f"{net_vat.get('register_net', Decimal('0.00')):.2f}",
            f"{net_vat.get('gl_net', Decimal('0.00')):.2f}",
            f"{net_var:.2f}",
            'MATCHED' if net_var == Decimal('0.00') else 'DISCREPANCY'
        ]))
        writer.writerow([])

        tx_recon = recon_data.get('transaction_reconciliation', {}) or {}
        discrepancies = tx_recon.get('discrepancies', []) or []
        writer.writerow([f"AUDIT DISCREPANCIES ({len(discrepancies)} Found)"])
        writer.writerow([
            'Discrepancy Type', 'Severity', 'Document Type', 'Document Number',
            'Date', 'Expected VAT', 'GL VAT', 'Difference', 'Audit Findings / Message'
        ])
        for d in discrepancies:
            exp_val = f"{d.get('expected'):.2f}" if isinstance(d.get('expected'), Decimal) else str(d.get('expected', ''))
            act_val = f"{d.get('actual'):.2f}" if isinstance(d.get('actual'), Decimal) else str(d.get('actual', ''))
            diff_val = f"{d.get('difference'):.2f}" if isinstance(d.get('difference'), Decimal) else str(d.get('difference', ''))
            writer.writerow(sanitize_csv_row([
                d.get('type'),
                d.get('severity'),
                d.get('document_type'),
                d.get('document_number'),
                d.get('document_date'),
                exp_val,
                act_val,
                diff_val,
                d.get('message')
            ]))

        return response


VATReconciliationView = VatGlReconciliationView


class VatDrilldownAPIView(TaxationAccessMixin, View):
    def get(self, request, *args, **kwargs):
        target = request.GET.get('target', '').strip().lower()
        branch_id = request.GET.get('branch')

        branch = None
        if branch_id and branch_id.isdigit():
            branch = Branch.objects.filter(id=int(branch_id), is_active=True).first()
        if not branch:
            branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()

        if target == 'day':
            date_str = request.GET.get('date', '').strip()
            parsed = parse_flexible_date(date_str)
            target_date = parsed[0] if parsed else timezone.now().date()

            day_data = TaxationReportGenerator.get_date_vat_drilldown(branch, target_date)
            return JsonResponse({
                'status': 'success',
                'target': 'day',
                'branch_id': branch.id,
                'branch_name': branch.name,
                'data': day_data
            }, encoder=SafeTaxJSONEncoder)

        elif target in ['invoice', 'sales']:
            inv_ref = request.GET.get('id') or request.GET.get('ref', '')
            if not inv_ref:
                return JsonResponse({'status': 'error', 'message': 'Invoice reference parameter (id or ref) is required.'}, status=400)

            data = TaxationReportGenerator.get_invoice_vat_drilldown(inv_ref)
            return JsonResponse(data, encoder=SafeTaxJSONEncoder)

        elif target in ['grn', 'purchase']:
            grn_ref = request.GET.get('id') or request.GET.get('ref', '')
            if not grn_ref:
                return JsonResponse({'status': 'error', 'message': 'GRN reference parameter (id or ref) is required.'}, status=400)

            data = TaxationReportGenerator.get_grn_vat_drilldown(grn_ref)
            return JsonResponse(data, encoder=SafeTaxJSONEncoder)

        elif target in ['sales_return', 'credit_note', 'sale_return', 'return']:
            ret_ref = request.GET.get('id') or request.GET.get('ref', '')
            if not ret_ref:
                return JsonResponse({'status': 'error', 'message': 'Sales Return reference parameter (id or ref) is required.'}, status=400)

            data = self.get_sales_return_vat_drilldown(ret_ref)
            return JsonResponse(data, encoder=SafeTaxJSONEncoder)

        elif target in ['purchase_return', 'debit_note', 'pret']:
            pret_ref = request.GET.get('id') or request.GET.get('ref', '')
            if not pret_ref:
                return JsonResponse({'status': 'error', 'message': 'Purchase Return reference parameter (id or ref) is required.'}, status=400)

            data = self.get_purchase_return_vat_drilldown(pret_ref)
            return JsonResponse(data, encoder=SafeTaxJSONEncoder)

        return JsonResponse({
            'status': 'error',
            'message': 'Invalid drill-down target. Supported targets: "day", "invoice", "grn", "sales_return", "purchase_return".'
        }, status=400)

    @staticmethod
    def get_sales_return_vat_drilldown(return_id_or_number: Union[int, str]) -> Dict[str, Any]:
        config = SystemConfiguration.get_solo()
        is_vat_shop = (config.tax_system_mode == 'VAT')

        clean_ref = str(return_id_or_number or '').strip()
        if not clean_ref:
            return {'status': 'NOT_FOUND', 'message': 'No return identifier provided.'}

        if isinstance(return_id_or_number, int) or clean_ref.isdigit():
            ret = SalesReturn.objects.filter(id=int(clean_ref)).first()
        else:
            ret = SalesReturn.objects.filter(return_number__iexact=clean_ref).first()

        if not ret:
            return {'status': 'NOT_FOUND', 'message': f'Sales Return voucher {return_id_or_number} not found.'}

        gl_entry = VATLedgerService._fetch_gl_journal_map([ret.return_number]).get(ret.return_number)

        items_breakdown = [
            VATLedgerService.extract_sales_return_item_vat_detail(item, is_vat_shop)
            for item in ret.items.select_related('product', 'estimate_item').all()
        ]

        taxable_sum = sum((i['taxable_amount'] for i in items_breakdown), Decimal('0.00'))
        vat_sum = sum((i['vat_amount'] for i in items_breakdown), Decimal('0.00'))
        non_taxable_sum = sum((i['non_taxable_amount'] for i in items_breakdown), Decimal('0.00'))

        final_taxable = ret.taxable_amount if (ret.taxable_amount or Decimal('0.00')) > Decimal('0.00') else taxable_sum
        final_vat = ret.vat_amount if (ret.vat_amount or Decimal('0.00')) > Decimal('0.00') else vat_sum
        final_non_taxable = ret.non_taxable_amount if (ret.non_taxable_amount or Decimal('0.00')) > Decimal('0.00') else non_taxable_sum

        return {
            'status': 'SUCCESS',
            'document_type': 'SALES_RETURN',
            'id': ret.id,
            'return_number': ret.return_number,
            'original_estimate_id': ret.original_estimate_id,
            'original_estimate_number': ret.original_estimate.estimate_number if ret.original_estimate else 'N/A',
            'return_date_ad': ret.return_date_ad,
            'return_date_bs': ret.return_date_bs or '',
            'fiscal_year': ret.fiscal_year or '',
            'customer_id': ret.customer_id,
            'customer_name': ret.customer.name if ret.customer else (ret.original_estimate.recipient_display_name if ret.original_estimate else 'Walk-in'),
            'customer_pan': ret.customer.pan_number if ret.customer and ret.customer.pan_number else (ret.original_estimate.customer_pan if ret.original_estimate else ''),
            'refund_mode': ret.refund_mode,
            'refund_mode_display': ret.get_refund_mode_display(),
            'reason': ret.reason or '',
            'technician_notes': ret.technician_notes or '',
            'total_refund_amount': ret.total_refund_amount,
            'taxable_amount': final_taxable,
            'vat_amount': final_vat,
            'non_taxable_amount': final_non_taxable,
            'items': items_breakdown,
            'gl_entry': gl_entry,
        }

    @staticmethod
    def get_purchase_return_vat_drilldown(return_id_or_number: Union[int, str]) -> Dict[str, Any]:
        clean_ref = str(return_id_or_number or '').strip()
        if not clean_ref:
            return {'status': 'NOT_FOUND', 'message': 'No debit note identifier provided.'}

        if isinstance(return_id_or_number, int) or clean_ref.isdigit():
            pret = PurchaseReturn.objects.filter(id=int(clean_ref)).first()
        else:
            pret = PurchaseReturn.objects.filter(return_number__iexact=clean_ref).first()

        if not pret:
            return {'status': 'NOT_FOUND', 'message': f'Purchase Return / Debit Note {return_id_or_number} not found.'}

        gl_entry = VATLedgerService._fetch_gl_journal_map([pret.return_number]).get(pret.return_number)

        items_breakdown = [
            VATLedgerService.extract_purchase_return_item_vat_detail(item)
            for item in pret.items.select_related('product', 'unit_conversion').all()
        ]

        taxable_sum = sum((i['taxable_amount'] for i in items_breakdown), Decimal('0.00'))
        vat_sum = sum((i['vat_amount'] for i in items_breakdown), Decimal('0.00'))

        final_taxable = pret.total_return_amount if (pret.total_return_amount or Decimal('0.00')) > Decimal('0.00') else taxable_sum
        final_vat = pret.tax_amount if (pret.tax_amount or Decimal('0.00')) > Decimal('0.00') else vat_sum

        return {
            'status': 'SUCCESS',
            'document_type': 'PURCHASE_RETURN',
            'id': pret.id,
            'return_number': pret.return_number,
            'original_grn_id': pret.original_grn_id,
            'original_bill_reference': pret.original_bill_reference or (pret.original_grn.grn_number if pret.original_grn else ''),
            'return_date': pret.return_date,
            'return_date_bs': pret.return_date_bs or '',
            'fiscal_year': pret.fiscal_year or '',
            'supplier_id': pret.supplier_id,
            'supplier_name': pret.supplier.company_name,
            'supplier_pan': pret.supplier.pan_number or pret.supplier.vat_number or '',
            'refund_mode': pret.refund_mode,
            'refund_mode_display': pret.get_refund_mode_display(),
            'remarks': pret.remarks or '',
            'total_return_amount': final_taxable,
            'taxable_amount': final_taxable,
            'tax_amount': final_vat,
            'vat_amount': final_vat,
            'net_refund_amount': pret.net_refund_amount,
            'status': pret.status,
            'status_display': pret.get_status_display(),
            'items': items_breakdown,
            'gl_entry': gl_entry,
        }


class InvoiceVatDrilldownView(TaxationAccessMixin, View):
    def get(self, request, pk_or_number, *args, **kwargs):
        data = TaxationReportGenerator.get_invoice_vat_drilldown(pk_or_number)
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.GET.get('format') == 'json':
            return JsonResponse(data, encoder=SafeTaxJSONEncoder)

        context = {
            'data': data,
            'config': SystemConfiguration.get_solo(),
            'SYS_CONFIG': SystemConfiguration.get_solo(),
        }
        return render(request, 'taxation/partials/_invoice_vat_modal.html', context)


class GRNVatDrilldownView(TaxationAccessMixin, View):
    def get(self, request, pk_or_number, *args, **kwargs):
        data = TaxationReportGenerator.get_grn_vat_drilldown(pk_or_number)
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.GET.get('format') == 'json':
            return JsonResponse(data, encoder=SafeTaxJSONEncoder)

        context = {
            'data': data,
            'config': SystemConfiguration.get_solo(),
            'SYS_CONFIG': SystemConfiguration.get_solo(),
        }
        return render(request, 'taxation/partials/_grn_vat_modal.html', context)


class SalesReturnVatDrilldownView(TaxationAccessMixin, View):
    def get(self, request, pk_or_number, *args, **kwargs):
        data = VatDrilldownAPIView.get_sales_return_vat_drilldown(pk_or_number)
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.GET.get('format') == 'json':
            return JsonResponse(data, encoder=SafeTaxJSONEncoder)

        context = {
            'data': data,
            'config': SystemConfiguration.get_solo(),
            'SYS_CONFIG': SystemConfiguration.get_solo(),
        }
        return render(request, 'taxation/partials/_sales_return_vat_modal.html', context)


class PurchaseReturnVatDrilldownView(TaxationAccessMixin, View):
    def get(self, request, pk_or_number, *args, **kwargs):
        data = VatDrilldownAPIView.get_purchase_return_vat_drilldown(pk_or_number)
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.GET.get('format') == 'json':
            return JsonResponse(data, encoder=SafeTaxJSONEncoder)

        context = {
            'data': data,
            'config': SystemConfiguration.get_solo(),
            'SYS_CONFIG': SystemConfiguration.get_solo(),
        }
        return render(request, 'taxation/partials/_purchase_return_vat_modal.html', context)