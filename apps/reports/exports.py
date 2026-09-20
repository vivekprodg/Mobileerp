"""
Business Intelligence, Financial, Inventory & Sales Report Exporter Engine.

Features:
1. Formula Injection Hardening (CWE-1236):
   - Sanitizes all exported string cells beginning with trigger characters ('=', '+', '-', '@', '\t', '\r')
     by prepending a single quote (') to prevent arbitrary code execution in downstream spreadsheet apps.
2. Dual-Mode Concession & Itemized Discount Clarity:
   - Preserves exact monetary distinctions between amount concessions, percentage discounts,
     catalog price overrides, and trade-in credits.
3. Multi-Format Rendering:
   - Sanitized CSV streams with cumulative summary totals rows.
   - Enterprise-styled Excel (.xlsx) workbooks via openpyxl with dark navy headers, auto-fit columns,
     formatted currency cells, and double-underline total footers.
   - Landscape A4 PDF export using ReportLab with exact column distribution.
4. Comprehensive Specialized Retail Mobile Exports:
   - 21-Column Stock Summary Report (CSV, Excel, PDF).
   - 10-Column Stock Detail Ledger (Bin Card - CSV, Excel).
   - Low Stock & Reorder Intelligence (CSV).
   - Out of Stock / Zero Inventory Critical Report (CSV).
   - Live IMEI & Serial Number Registry with Dual-SIM & NTA MDMS Status (CSV).
   - Inter-Branch Stock Transfers & In-Transit Consignments (CSV).
   - Manual Stock Adjustments, Count Corrections & Damage Write-Offs (CSV).
   - Stock Aging Analysis across age brackets (0-30, 31-60, 61-90, 90+ days) (CSV).
   - Pre-Owned / Trade-In Buy-Back Inventory with condition grades and profit margins (CSV).
   - Invoice-level and line-item level Sales and Concessions (CSV).
   - Point-in-time Inventory Valuation, Customer Udhaari (Credit), and Price Fluctuation logs (CSV).
5. Purchase Domain Reporting Suite:
   - Purchase Register (Inward Purchase Book - CSV & Styled Excel).
   - Supplier-Wise Purchase Turnover, Net Spend & Returns Summary (CSV).
   - Product-Wise & Handset Purchase Volume, Rates & Supplier Sources (CSV).
   - Supplier Outstanding (Udhaari) & Accounts Payable Aging (CSV).
6. Sales & Commercial Intelligence Reporting Suite (Newly Added):
   - Sales Summary Date-Wise Rollup (CSV & Styled Excel).
   - Product-Wise Sales Volume & Realized Margins (CSV).
   - Brand-Wise Sales Turnover & Share % (CSV).
   - Category-Wise Departmental Revenue & Contribution % (CSV).
   - Sold Handset & IMEI Registry with Active Warranty Status (CSV).
   - Salesperson Performance & Average Basket Size (CSV).
   - Cashier Collection & Tender Reconciliation (CSV).
   - Payment Method-Wise Inflow & Share % (CSV).
   - Discount & Supervisor Price Override Concession Audit (CSV).
   - Trade-In / Exchange Sales Settlement Report (CSV).
   - Gross Profit & 4-Tier Margin Realization (CSV).
   - Top-Selling Products Ranked by Volume or Revenue (CSV).
   - Cancelled & Voided Sales Invoices Forensic Audit (CSV).
"""

import io
import csv
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, List, Dict, Optional, Union

from django.http import HttpResponse
from django.db.models import Sum, Q, F, Case, When, DecimalField, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.sales.models import SalesEstimate, SalesEstimateItem
from apps.inventory.models import BranchStock, Product, ProductBatch, ItemInstance
from apps.customers.models import Customer
from apps.reports.models import ProductCostHistory
from apps.core.models import SystemConfiguration


def sanitize_csv_cell(val: Any) -> str:
    """
    Sanitizes values written to CSV to prevent CSV / Formula Injection attacks (CWE-1236).
    If a cell value begins with formula trigger characters ('=', '+', '-', '@', '\t', '\r'),
    it prepends a single quote (') so spreadsheet applications render it strictly as plain text.
    """
    if val is None:
        return ""

    val_str = str(val)
    stripped = val_str.lstrip()
    if stripped and stripped[0] in ('=', '+', '-', '@', '\t', '\r'):
        return f"'{val_str}"
    return val_str


def sanitize_csv_row(row: List[Any]) -> List[str]:
    """Applies CSV formula sanitization across all elements of a row."""
    return [sanitize_csv_cell(cell) for cell in row]


class CSVExportEngine:
    """
    High-Precision Business, Inventory, Sales, Tax, and Purchase Export Engine.
    Supports CSV (with formula injection protection), styled Excel (.xlsx),
    and landscape PDF generation across all operational reporting modules.
    """

    # =========================================================================
    # 1. REPORT 1: STOCK SUMMARY REPORT EXPORTERS (CSV, EXCEL, PDF)
    # =========================================================================
    @staticmethod
    def export_stock_summary_csv(
        report_data: List[Dict[str, Any]],
        summary_totals: Dict[str, Any],
        filename: str = "Stock_Summary_Report.csv"
    ) -> HttpResponse:
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        headers = [
            'SKU', 'Barcode', 'Product', 'Category', 'Brand', 'Branch',
            'Warehouse', 'Unit', 'Opening quantity', 'Inward quantity',
            'Outward quantity', 'Transfer in', 'Transfer out', 'Returns',
            'Adjustments', 'Reserved quantity', 'Defective quantity',
            'Available quantity', 'Closing quantity', 'Cost value (NPR)',
            'Retail value (NPR)'
        ]
        writer.writerow(headers)

        for r in report_data:
            row = [
                r['sku'],
                r['barcode'],
                r['product_name'],
                r['category_name'],
                r['brand_name'],
                r['branch_name'],
                r['warehouse'],
                r['unit_code'],
                f"{r['opening_qty']:.3f}",
                f"{r['inward_qty']:.3f}",
                f"{r['outward_qty']:.3f}",
                f"{r['transfer_in']:.3f}",
                f"{r['transfer_out']:.3f}",
                f"{r['returns']:.3f}",
                f"{r['adjustments']:.3f}",
                f"{r['reserved_qty']:.3f}",
                f"{r['defective_qty']:.3f}",
                f"{r['available_qty']:.3f}",
                f"{r['closing_qty']:.3f}",
                f"{r['cost_value']:.2f}",
                f"{r['retail_value']:.2f}",
            ]
            writer.writerow(sanitize_csv_row(row))

        totals_row = [
            'TOTALS', '', f"{summary_totals.get('records_count', 0)} Records", '', '', '', '', '',
            f"{summary_totals.get('total_opening_qty', 0):.3f}",
            f"{summary_totals.get('total_inward_qty', 0):.3f}",
            f"{summary_totals.get('total_outward_qty', 0):.3f}",
            f"{summary_totals.get('total_transfer_in', 0):.3f}",
            f"{summary_totals.get('total_transfer_out', 0):.3f}",
            f"{summary_totals.get('total_returns', 0):.3f}",
            f"{summary_totals.get('total_adjustments', 0):.3f}",
            f"{summary_totals.get('total_reserved_qty', 0):.3f}",
            f"{summary_totals.get('total_defective_qty', 0):.3f}",
            f"{summary_totals.get('total_available_qty', 0):.3f}",
            f"{summary_totals.get('total_closing_qty', 0):.3f}",
            f"{summary_totals.get('total_cost_value', 0):.2f}",
            f"{summary_totals.get('total_retail_value', 0):.2f}",
        ]
        writer.writerow(sanitize_csv_row(totals_row))

        return response

    @staticmethod
    def export_stock_summary_excel(
        report_data: List[Dict[str, Any]],
        summary_totals: Dict[str, Any],
        date_range_label: str = "",
        filename: str = "Stock_Summary_Report.xlsx"
    ) -> HttpResponse:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Stock Summary"
        ws.views.sheetView[0].showGridLines = True

        config = SystemConfiguration.get_solo()
        shop_title = config.company_name_en if config else "Smart Mobile & Optical Hub"

        ws.merge_cells("A1:U1")
        ws["A1"] = f"{shop_title.upper()} - STOCK SUMMARY REPORT"
        ws["A1"].font = Font(name="Calibri", size=14, bold=True, color="FFFFFF")
        ws["A1"].fill = PatternFill(start_color="0F172A", end_color="0F172A", fill_type="solid")
        ws["A1"].alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[1].height = 28

        ws.merge_cells("A2:U2")
        ws["A2"] = f"Period: {date_range_label} | Generated on: {timezone.now().strftime('%Y-%m-%d %H:%M')}"
        ws["A2"].font = Font(name="Calibri", size=10, italic=True, color="334155")
        ws["A2"].fill = PatternFill(start_color="F1F5F9", end_color="F1F5F9", fill_type="solid")
        ws["A2"].alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[2].height = 20

        headers = [
            'SKU', 'Barcode', 'Product', 'Category', 'Brand', 'Branch',
            'Warehouse', 'Unit', 'Opening Qty', 'Inward Qty',
            'Outward Qty', 'Transfer In', 'Transfer Out', 'Returns',
            'Adjustments', 'Reserved Qty', 'Defective Qty',
            'Available Qty', 'Closing Qty', 'Cost Value (NPR)',
            'Retail Value (NPR)'
        ]

        header_font = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
        header_fill = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
        thin_border = Border(
            left=Side(style='thin', color='CBD5E1'),
            right=Side(style='thin', color='CBD5E1'),
            top=Side(style='thin', color='CBD5E1'),
            bottom=Side(style='thin', color='CBD5E1')
        )

        ws.row_dimensions[4].height = 24
        for col_idx, h in enumerate(headers, 1):
            cell = ws.cell(row=4, column=col_idx, value=h)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = thin_border

        current_row = 5
        alt_fill = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")

        for r in report_data:
            ws.row_dimensions[current_row].height = 19
            is_alt = (current_row % 2 == 0)

            row_values = [
                r['sku'], r['barcode'], r['product_name'], r['category_name'],
                r['brand_name'], r['branch_name'], r['warehouse'], r['unit_code'],
                float(r['opening_qty']), float(r['inward_qty']), float(r['outward_qty']),
                float(r['transfer_in']), float(r['transfer_out']), float(r['returns']),
                float(r['adjustments']), float(r['reserved_qty']), float(r['defective_qty']),
                float(r['available_qty']), float(r['closing_qty']),
                float(r['cost_value']), float(r['retail_value'])
            ]

            for col_idx, val in enumerate(row_values, 1):
                cell = ws.cell(row=current_row, column=col_idx, value=val)
                cell.border = thin_border
                cell.font = Font(name="Calibri", size=9.5)
                if is_alt:
                    cell.fill = alt_fill

                if col_idx in [1, 2]:
                    cell.alignment = Alignment(horizontal="center")
                elif col_idx in [3, 4, 5, 6, 7]:
                    cell.alignment = Alignment(horizontal="left")
                elif col_idx == 8:
                    cell.alignment = Alignment(horizontal="center")
                elif 9 <= col_idx <= 19:
                    cell.number_format = '#,##0.00'
                    cell.alignment = Alignment(horizontal="right")
                elif col_idx in [20, 21]:
                    cell.number_format = '#,##0.00'
                    cell.alignment = Alignment(horizontal="right")
                    cell.font = Font(name="Calibri", size=9.5, bold=True)

            current_row += 1

        ws.row_dimensions[current_row].height = 24
        double_top_border = Border(
            left=Side(style='thin', color='CBD5E1'),
            right=Side(style='thin', color='CBD5E1'),
            top=Side(style='double', color='0F172A'),
            bottom=Side(style='double', color='0F172A')
        )
        totals_fill = PatternFill(start_color="F1F5F9", end_color="F1F5F9", fill_type="solid")

        totals_values = [
            'TOTALS', '', f"{summary_totals.get('records_count', 0)} Items", '', '', '', '', '',
            float(summary_totals.get('total_opening_qty', 0)),
            float(summary_totals.get('total_inward_qty', 0)),
            float(summary_totals.get('total_outward_qty', 0)),
            float(summary_totals.get('total_transfer_in', 0)),
            float(summary_totals.get('total_transfer_out', 0)),
            float(summary_totals.get('total_returns', 0)),
            float(summary_totals.get('total_adjustments', 0)),
            float(summary_totals.get('total_reserved_qty', 0)),
            float(summary_totals.get('total_defective_qty', 0)),
            float(summary_totals.get('total_available_qty', 0)),
            float(summary_totals.get('total_closing_qty', 0)),
            float(summary_totals.get('total_cost_value', 0)),
            float(summary_totals.get('total_retail_value', 0))
        ]

        for col_idx, val in enumerate(totals_values, 1):
            cell = ws.cell(row=current_row, column=col_idx, value=val)
            cell.border = double_top_border
            cell.fill = totals_fill
            cell.font = Font(name="Calibri", size=10, bold=True)
            if col_idx in [1, 2, 8]:
                cell.alignment = Alignment(horizontal="center", vertical="center")
            elif 3 <= col_idx <= 7:
                cell.alignment = Alignment(horizontal="left", vertical="center")
            elif 9 <= col_idx <= 21:
                cell.number_format = '#,##0.00'
                cell.alignment = Alignment(horizontal="right", vertical="center")

        for col in ws.columns:
            max_len = 0
            col_letter = get_column_letter(col[0].column)
            for cell in col:
                if cell.row in [1, 2]:
                    continue
                val_str = str(cell.value or '')
                max_len = max(max_len, len(val_str))
            ws.column_dimensions[col_letter].width = max(max_len + 3, 11)

        buffer = io.BytesIO()
        wb.save(buffer)
        buffer.seek(0)

        response = HttpResponse(
            buffer.getvalue(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response

    @staticmethod
    def export_stock_summary_pdf(
        report_data: List[Dict[str, Any]],
        summary_totals: Dict[str, Any],
        date_range_label: str = "",
        branch_name: str = "",
        filename: str = "Stock_Summary_Report.pdf"
    ) -> HttpResponse:
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib import colors

        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=landscape(A4),
            leftMargin=14,
            rightMargin=14,
            topMargin=14,
            bottomMargin=14
        )

        styles = getSampleStyleSheet()
        config = SystemConfiguration.get_solo()
        company_title = config.company_name_en if config else "Smart Mobile & Optical Hub"

        title_style = ParagraphStyle(
            name='RepTitle',
            parent=styles['Heading1'],
            fontName='Helvetica-Bold',
            fontSize=13,
            leading=15,
            alignment=1,
            textColor=colors.HexColor('#0F172A')
        )
        meta_style = ParagraphStyle(
            name='RepMeta',
            parent=styles['Normal'],
            fontName='Helvetica',
            fontSize=8,
            leading=10,
            alignment=1,
            textColor=colors.HexColor('#475569')
        )
        cell_style = ParagraphStyle(
            name='CellSmall',
            parent=styles['Normal'],
            fontName='Helvetica',
            fontSize=5.5,
            leading=6.5
        )
        cell_bold = ParagraphStyle(
            name='CellSmallBold',
            parent=styles['Normal'],
            fontName='Helvetica-Bold',
            fontSize=5.5,
            leading=6.5
        )

        elements = [
            Paragraph(f"{company_title.upper()} - STOCK SUMMARY REPORT", title_style),
            Paragraph(f"Outlet: {branch_name or 'All Stores'} | Scope: {date_range_label} | Generated: {timezone.now().strftime('%Y-%m-%d %H:%M')}", meta_style),
            Spacer(1, 8)
        ]

        table_data = [[
            'SKU', 'Barcode', 'Product', 'Cat', 'Brand', 'Branch', 'Wh/Rack', 'UOM',
            'Open', 'In', 'Out', 'Tr In', 'Tr Out', 'Ret', 'Adj',
            'Res', 'Def', 'Avail', 'Close', 'Cost Val', 'Retail Val'
        ]]

        for r in report_data:
            table_data.append([
                Paragraph(r['sku'][:12], cell_style),
                Paragraph(r['barcode'][:10], cell_style),
                Paragraph(r['product_name'][:18], cell_style),
                Paragraph(r['category_name'][:10], cell_style),
                Paragraph(r['brand_name'][:8], cell_style),
                Paragraph(r['branch_code'][:6], cell_style),
                Paragraph(r['warehouse'][:10], cell_style),
                Paragraph(r['unit_code'][:4], cell_style),
                f"{r['opening_qty']:.1f}",
                f"{r['inward_qty']:.1f}",
                f"{r['outward_qty']:.1f}",
                f"{r['transfer_in']:.1f}",
                f"{r['transfer_out']:.1f}",
                f"{r['returns']:.1f}",
                f"{r['adjustments']:.1f}",
                f"{r['reserved_qty']:.1f}",
                f"{r['defective_qty']:.1f}",
                f"{r['available_qty']:.1f}",
                f"{r['closing_qty']:.1f}",
                f"{r['cost_value']:.0f}",
                f"{r['retail_value']:.0f}"
            ])

        table_data.append([
            Paragraph('TOTALS', cell_bold), '', '', '', '', '', '', '',
            f"{summary_totals.get('total_opening_qty', 0):.1f}",
            f"{summary_totals.get('total_inward_qty', 0):.1f}",
            f"{summary_totals.get('total_outward_qty', 0):.1f}",
            f"{summary_totals.get('total_transfer_in', 0):.1f}",
            f"{summary_totals.get('total_transfer_out', 0):.1f}",
            f"{summary_totals.get('total_returns', 0):.1f}",
            f"{summary_totals.get('total_adjustments', 0):.1f}",
            f"{summary_totals.get('total_reserved_qty', 0):.1f}",
            f"{summary_totals.get('total_defective_qty', 0):.1f}",
            f"{summary_totals.get('total_available_qty', 0):.1f}",
            f"{summary_totals.get('total_closing_qty', 0):.1f}",
            f"{summary_totals.get('total_cost_value', 0):.0f}",
            f"{summary_totals.get('total_retail_value', 0):.0f}"
        ])

        col_widths = [
            42, 38, 70, 42, 34, 30, 40, 20,
            34, 32, 32, 32, 32, 28, 28,
            28, 28, 38, 38, 54, 54
        ]

        summary_table = Table(table_data, colWidths=col_widths, repeatRows=1)
        summary_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1E293B')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 6),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
            ('TOPPADDING', (0, 0), (-1, -1), 2),
            ('LEFTPADDING', (0, 0), (-1, -1), 1),
            ('RIGHTPADDING', (0, 0), (-1, -1), 1),
            ('GRID', (0, 0), (-1, -1), 0.4, colors.HexColor('#CBD5E1')),
            ('ROWBACKGROUNDS', (0, 1), (-1, -2), [colors.white, colors.HexColor('#F8FAFC')]),
            ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#E2E8F0')),
            ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
            ('ALIGN', (8, 1), (-1, -1), 'RIGHT'),
        ]))

        elements.append(summary_table)
        doc.build(elements)

        buffer.seek(0)
        response = HttpResponse(buffer.getvalue(), content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response

    # =========================================================================
    # 2. REPORT 2: STOCK DETAIL REPORT (BIN CARD / RUNNING LEDGER) EXPORTERS
    # =========================================================================
    @staticmethod
    def export_stock_detail_csv(
        product: Product,
        report_data: List[Dict[str, Any]],
        opening_balance: Decimal,
        closing_balance: Decimal,
        total_in: Decimal,
        total_out: Decimal,
        date_range_label: str = "",
        filename: Optional[str] = None
    ) -> HttpResponse:
        if not filename:
            filename = f"Stock_Detail_{product.sku}.csv"

        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        writer.writerow([
            'S.N.', 'Date', 'Invoice Type', 'Invoice number',
            'In Stock', 'Out Stock', 'Unit', 'Price', 'Balance', 'Remarks'
        ])

        unit_str = product.base_unit.code if product.base_unit else 'Pcs'

        writer.writerow(sanitize_csv_row([
            '', '', 'OPENING BALANCE', '-',
            '0.000', '0.000', unit_str, '0.00', f"{opening_balance:.3f}", 'Opening Stock Balance'
        ]))

        for r in report_data:
            date_display = f"{r['date_ad']} ({r['date_bs']} BS)"
            row_cells = [
                r['sn'],
                date_display,
                r['invoice_type'],
                r['invoice_number'],
                f"{r['in_stock']:.3f}",
                f"{r['out_stock']:.3f}",
                r['unit'],
                f"{r['price']:.2f}",
                f"{r['balance']:.3f}",
                r['remarks']
            ]
            writer.writerow(sanitize_csv_row(row_cells))

        writer.writerow(sanitize_csv_row([
            'TOTALS', '', '', f"{len(report_data)} Records",
            f"{total_in:.3f}", f"{total_out:.3f}", unit_str,
            '', f"{closing_balance:.3f}", 'Closing Stock Balance'
        ]))

        return response

    @staticmethod
    def export_stock_detail_excel(
        product: Product,
        report_data: List[Dict[str, Any]],
        opening_balance: Decimal,
        closing_balance: Decimal,
        total_in: Decimal,
        total_out: Decimal,
        date_range_label: str = "",
        branch_name: str = "",
        filename: Optional[str] = None
    ) -> HttpResponse:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter

        if not filename:
            filename = f"Stock_Detail_{product.sku}.xlsx"

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Stock Detail"
        ws.views.sheetView[0].showGridLines = True

        config = SystemConfiguration.get_solo()
        shop_title = config.company_name_en if config else "Smart Mobile & Optical Hub"

        ws.merge_cells("A1:J1")
        ws["A1"] = f"{shop_title.upper()} - STOCK DETAIL REPORT (BIN CARD)"
        ws["A1"].font = Font(name="Calibri", size=13, bold=True, color="FFFFFF")
        ws["A1"].fill = PatternFill(start_color="0F172A", end_color="0F172A", fill_type="solid")
        ws["A1"].alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[1].height = 26

        ws.merge_cells("A2:J2")
        ws["A2"] = (
            f"Product: {product.name} ({product.sku}) | Branch: {branch_name or 'All Outlets'} | "
            f"Scope: {date_range_label}"
        )
        ws["A2"].font = Font(name="Calibri", size=10, italic=True, color="334155")
        ws["A2"].fill = PatternFill(start_color="F1F5F9", end_color="F1F5F9", fill_type="solid")
        ws["A2"].alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[2].height = 20

        headers = [
            'S.N.', 'Date', 'Invoice Type', 'Invoice number',
            'In Stock', 'Out Stock', 'Unit', 'Price', 'Balance', 'Remarks'
        ]
        header_font = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
        header_fill = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
        thin_border = Border(
            left=Side(style='thin', color='CBD5E1'),
            right=Side(style='thin', color='CBD5E1'),
            top=Side(style='thin', color='CBD5E1'),
            bottom=Side(style='thin', color='CBD5E1')
        )

        ws.row_dimensions[4].height = 22
        for col_idx, h in enumerate(headers, 1):
            cell = ws.cell(row=4, column=col_idx, value=h)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border = thin_border

        curr_row = 5
        unit_code = product.base_unit.code if product.base_unit else 'Pcs'

        opening_vals = [
            '', '', 'OPENING BALANCE', '-',
            0.0, 0.0, unit_code, 0.0, float(opening_balance), 'Opening Stock Balance'
        ]
        for col_idx, v in enumerate(opening_vals, 1):
            cell = ws.cell(row=curr_row, column=col_idx, value=v)
            cell.border = thin_border
            cell.font = Font(name="Calibri", size=9.5, bold=True)
            cell.fill = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")
            if col_idx in [5, 6, 8, 9]:
                cell.number_format = '#,##0.00'
                cell.alignment = Alignment(horizontal="right")
            else:
                cell.alignment = Alignment(horizontal="center")
        curr_row += 1

        for r in report_data:
            ws.row_dimensions[curr_row].height = 19
            date_display = f"{r['date_ad']} ({r['date_bs']})"
            row_vals = [
                r['sn'],
                date_display,
                r['invoice_type'],
                r['invoice_number'],
                float(r['in_stock']),
                float(r['out_stock']),
                r['unit'],
                float(r['price']),
                float(r['balance']),
                r['remarks']
            ]
            for col_idx, v in enumerate(row_vals, 1):
                cell = ws.cell(row=curr_row, column=col_idx, value=v)
                cell.border = thin_border
                cell.font = Font(name="Calibri", size=9.5)
                if col_idx in [1, 2, 7]:
                    cell.alignment = Alignment(horizontal="center")
                elif col_idx in [3, 4, 10]:
                    cell.alignment = Alignment(horizontal="left")
                elif col_idx in [5, 6, 8, 9]:
                    cell.number_format = '#,##0.00'
                    cell.alignment = Alignment(horizontal="right")
            curr_row += 1

        double_border = Border(
            left=Side(style='thin', color='CBD5E1'),
            right=Side(style='thin', color='CBD5E1'),
            top=Side(style='double', color='0F172A'),
            bottom=Side(style='double', color='0F172A')
        )
        totals_vals = [
            'TOTALS', '', '', f"{len(report_data)} Records",
            float(total_in), float(total_out), unit_code,
            '', float(closing_balance), 'Closing Balance'
        ]
        for col_idx, v in enumerate(totals_vals, 1):
            cell = ws.cell(row=curr_row, column=col_idx, value=v)
            cell.border = double_border
            cell.font = Font(name="Calibri", size=10, bold=True)
            cell.fill = PatternFill(start_color="F1F5F9", end_color="F1F5F9", fill_type="solid")
            if col_idx in [5, 6, 9]:
                cell.number_format = '#,##0.00'
                cell.alignment = Alignment(horizontal="right")
            else:
                cell.alignment = Alignment(horizontal="center")

        for col in ws.columns:
            col_letter = get_column_letter(col[0].column)
            max_len = max(len(str(c.value or '')) for c in col if c.row > 2)
            ws.column_dimensions[col_letter].width = max(max_len + 3, 11)

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)

        response = HttpResponse(
            buf.getvalue(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response

    # =========================================================================
    # 3. SPECIALIZED RETAIL INVENTORY & OPERATIONAL EXPORTERS
    # =========================================================================
    @staticmethod
    def export_low_stock_csv(items_data: List[Dict[str, Any]], filename: str = "Low_Stock_Report.csv") -> HttpResponse:
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        headers = [
            'SKU', 'Product Name', 'Category', 'Brand', 'Branch', 'Rack Location',
            'Current Stock Qty', 'Unit', 'Reorder Threshold', 'Suggested Order Qty',
            'Unit Purchase Cost (NPR)', 'Selling Price (NPR)', 'Estimated Reorder Outlay (NPR)', 'Last Supplier'
        ]
        writer.writerow(headers)

        sum_reorder_qty = Decimal('0.000')
        sum_reorder_outlay = Decimal('0.00')

        for item in items_data:
            prod = item['product']
            branch = item['branch']
            curr_qty = item['current_qty']
            threshold = item['threshold']
            suggested_qty = item['suggested_reorder_qty']
            est_cost = item['estimated_reorder_cost']

            sum_reorder_qty += suggested_qty
            sum_reorder_outlay += est_cost

            row = [
                prod.sku,
                prod.name,
                prod.category.name if prod.category else 'General',
                prod.brand.name if prod.brand else '-',
                branch.name,
                item.get('rack_number', '-'),
                f"{curr_qty:.3f}",
                prod.base_unit.code if prod.base_unit else 'PCS',
                f"{threshold:.2f}",
                f"{suggested_qty:.3f}",
                f"{item['cost_price']:.2f}",
                f"{item['selling_price']:.2f}",
                f"{est_cost:.2f}",
                item.get('last_supplier', 'Direct Purchase')
            ]
            writer.writerow(sanitize_csv_row(row))

        totals_row = [
            'TOTALS', f"{len(items_data)} Items", '', '', '', '', '', '', '',
            f"{sum_reorder_qty:.3f}", '', '', f"{sum_reorder_outlay:.2f}", ''
        ]
        writer.writerow(sanitize_csv_row(totals_row))
        return response

    @staticmethod
    def export_out_of_stock_csv(items_data: List[Dict[str, Any]], filename: str = "Out_Of_Stock_Report.csv") -> HttpResponse:
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        headers = [
            'SKU', 'Product Name', 'Category', 'Brand', 'Branch', 'Rack Location',
            'Days Out of Stock', 'Last Sold Date', 'Purchase Cost (NPR)', 'Selling MRP (NPR)',
            'Tracking Type'
        ]
        writer.writerow(headers)

        for item in items_data:
            prod = item['product']
            branch = item['branch']
            days_out = item.get('days_out_of_stock')
            days_out_str = str(days_out) if days_out is not None else "Never Sold"
            last_sold_str = str(item.get('last_sold_date') or '-')
            tracking_label = "Serialized (IMEI)" if item.get('is_serialized') else "Standard Bulk"

            row = [
                prod.sku,
                prod.name,
                prod.category.name if prod.category else 'General',
                prod.brand.name if prod.brand else '-',
                branch.name,
                item.get('rack_number', '-'),
                days_out_str,
                last_sold_str,
                f"{item['cost_price']:.2f}",
                f"{item['selling_price']:.2f}",
                tracking_label
            ]
            writer.writerow(sanitize_csv_row(row))

        totals_row = ['TOTALS', f"{len(items_data)} Zero-Stock Items", '', '', '', '', '', '', '', '', '']
        writer.writerow(sanitize_csv_row(totals_row))
        return response

    @staticmethod
    def export_imei_stock_csv(queryset, filename: str = "Live_IMEI_Stock_Report.csv") -> HttpResponse:
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        headers = [
            'Device UID', 'Product / Handset Model', 'RAM / Storage Specs', 'Color',
            'Branch Outlet', 'Primary IMEI 1', 'Secondary IMEI 2', 'Dual-SIM Status',
            'Physical Condition Grade', 'NTA MDMS Status', 'Landed Cost (NPR)',
            'Inward GRN Reference', 'Purchase Date', 'Supplier'
        ]
        writer.writerow(headers)

        sum_landed_cost = Decimal('0.00')
        records_count = 0

        qs = queryset.select_related('product', 'product__brand', 'branch').order_by('product__name', '-purchase_date')

        for h in qs:
            records_count += 1
            cost = h.landed_cost or h.product.purchase_price or Decimal('0.00')
            sum_landed_cost += cost

            if h.imei_2:
                sim_status = "Captured"
            elif h.imei_2_pending_scan:
                sim_status = "Pending Scan at POS"
            else:
                sim_status = "Single SIM / N/A"

            spec_desc = f"{h.product.ram or ''}/{h.product.internal_storage or ''}".strip('/')

            row = [
                h.device_uid or '-',
                h.product.name,
                spec_desc or '-',
                h.product.color_variant or '-',
                h.branch.name,
                h.imei_1 or '-',
                h.imei_2 or '-',
                sim_status,
                h.get_condition_display(),
                h.get_mdms_status_display(),
                f"{cost:.2f}",
                h.purchase_reference or '-',
                str(h.purchase_date or '-'),
                h.supplier_name or '-'
            ]
            writer.writerow(sanitize_csv_row(row))

        totals_row = [
            'TOTALS', f"{records_count} Handsets", '', '', '', '', '', '', '', '',
            f"{sum_landed_cost:.2f}", '', '', ''
        ]
        writer.writerow(sanitize_csv_row(totals_row))
        return response

    @staticmethod
    def export_stock_transfers_csv(queryset, filename: str = "Stock_Transfers_Report.csv") -> HttpResponse:
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        headers = [
            'Transfer Requisition No', 'Source (Origin)', 'Destination (Target)',
            'Transfer Date', 'Consignment Status', 'Line Items Count',
            'Requested By', 'Dispatched By', 'Received By', 'Consignment Remarks'
        ]
        writer.writerow(headers)

        qs = queryset.select_related(
            'source_branch', 'destination_branch', 'requested_by', 'dispatched_by', 'received_by'
        ).prefetch_related('items')

        records_count = 0
        for trf in qs:
            records_count += 1
            row = [
                trf.transfer_no,
                f"{trf.source_branch.name} ({trf.source_branch.code})",
                f"{trf.destination_branch.name} ({trf.destination_branch.code})",
                str(trf.transfer_date),
                trf.get_status_display(),
                trf.items.count(),
                trf.requested_by.username if trf.requested_by else 'System',
                trf.dispatched_by.username if trf.dispatched_by else '-',
                trf.received_by.username if trf.received_by else '-',
                trf.notes or ''
            ]
            writer.writerow(sanitize_csv_row(row))

        totals_row = ['TOTALS', f"{records_count} Consignments", '', '', '', '', '', '', '', '']
        writer.writerow(sanitize_csv_row(totals_row))
        return response

    @staticmethod
    def export_stock_adjustments_csv(queryset, filename: str = "Stock_Adjustments_Report.csv") -> HttpResponse:
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        headers = [
            'Timestamp', 'Branch', 'Product Name', 'SKU', 'Adjustment Type',
            'Quantity Delta', 'Previous Quantity', 'New Quantity', 'Base Unit',
            'Reference Document', 'Reason / Remarks', 'Performed By'
        ]
        writer.writerow(headers)

        sum_delta = Decimal('0.000')
        records_count = 0

        qs = queryset.select_related('product', 'product__base_unit', 'branch', 'user').order_by('-created_at')

        for log in qs:
            records_count += 1
            delta = log.quantity_delta or Decimal('0.000')
            sum_delta += delta

            row = [
                log.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                log.branch.name if log.branch else '-',
                log.product.name,
                log.product.sku,
                log.get_movement_type_display(),
                f"{delta:+.3f}",
                f"{log.previous_quantity:.3f}",
                f"{log.new_quantity:.3f}",
                log.product.base_unit.code if log.product.base_unit else 'PCS',
                log.reference_document or '-',
                log.remarks or '',
                log.user.username if log.user else 'System'
            ]
            writer.writerow(sanitize_csv_row(row))

        totals_row = [
            'TOTALS', f"{records_count} Adjustment Events", '', '', '',
            f"{sum_delta:+.3f}", '', '', '', '', '', ''
        ]
        writer.writerow(sanitize_csv_row(totals_row))
        return response

    @staticmethod
    def export_stock_aging_csv(
        aging_rows: List[Dict[str, Any]],
        totals: Dict[str, Any],
        filename: str = "Stock_Aging_Analysis.csv"
    ) -> HttpResponse:
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        headers = [
            'Item Classification', 'Product / Device Name', 'IMEI / Serial / Batch ID',
            'Branch Outlet', 'Inward Date', 'Age (Days)', 'Aging Bracket',
            'Quantity', 'Unit', 'Landed Cost Value (NPR)'
        ]
        writer.writerow(headers)

        sum_qty = Decimal('0.000')
        sum_cost = Decimal('0.00')

        for r in aging_rows:
            age = r['age_days']
            if age <= 30:
                bracket = "0 - 30 Days (Fresh)"
            elif age <= 60:
                bracket = "31 - 60 Days (Normal)"
            elif age <= 90:
                bracket = "61 - 90 Days (Slow)"
            else:
                bracket = "90+ Days (Critical Aging)"

            qty = Decimal(str(r['quantity']))
            cost = Decimal(str(r['cost_value']))
            sum_qty += qty
            sum_cost += cost

            row = [
                r['item_type'],
                r['name'],
                r['identifier'],
                r['branch'],
                str(r['inward_date']),
                age,
                bracket,
                f"{qty:.3f}",
                r.get('unit_code', 'PCS'),
                f"{cost:.2f}"
            ]
            writer.writerow(sanitize_csv_row(row))

        totals_row = [
            'TOTALS', f"{len(aging_rows)} Inventory Batches/Units", '', '', '', '', '',
            f"{sum_qty:.3f}", '', f"{sum_cost:.2f}"
        ]
        writer.writerow(sanitize_csv_row(totals_row))
        return response

    @staticmethod
    def export_trade_in_inventory_csv(
        records_data: List[Dict[str, Any]],
        totals: Dict[str, Any],
        filename: str = "Trade_In_PreOwned_Inventory.csv"
    ) -> HttpResponse:
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        headers = [
            'Trade-In Voucher No', 'Handset Brand', 'Model Name', 'Storage / Specs',
            'Primary IMEI 1', 'Condition Grade', 'NTA MDMS Status', 'Customer Name',
            'Buy-Back Landed Payout (NPR)', 'Resale Selling Price (NPR)', 'Margin / Markup (NPR)',
            'Inventory Status', 'Police Undertaking Executed'
        ]
        writer.writerow(headers)

        sum_buyback = Decimal('0.00')
        sum_resale = Decimal('0.00')
        sum_margin = Decimal('0.00')

        for r in records_data:
            item = r['instance']
            voucher = r.get('voucher')
            buyback = Decimal(str(r['buyback_cost']))
            resale = Decimal(str(r['selling_price']))
            margin = Decimal(str(r['realized_margin']))

            sum_buyback += buyback
            sum_resale += resale
            sum_margin += margin

            row = [
                item.trade_in_voucher_reference or (voucher.voucher_number if voucher else '-'),
                voucher.brand_name if voucher else item.product.brand.name if item.product.brand else 'Pre-Owned',
                voucher.model_name if voucher else item.product.name,
                voucher.storage_capacity if voucher else item.product.internal_storage or '-',
                item.imei_1 or '-',
                item.get_condition_display(),
                item.get_mdms_status_display(),
                voucher.customer_name_manual if voucher else item.customer_name or 'Walk-in',
                f"{buyback:.2f}",
                f"{resale:.2f}",
                f"{margin:+.2f}",
                item.get_status_display(),
                "Yes (Police Compliant)" if r.get('has_undertaking') else "Pending / Missing"
            ]
            writer.writerow(sanitize_csv_row(row))

        totals_row = [
            'TOTALS', f"{len(records_data)} Devices", '', '', '', '', '', '',
            f"{sum_buyback:.2f}", f"{sum_resale:.2f}", f"{sum_margin:+.2f}", '', ''
        ]
        writer.writerow(sanitize_csv_row(totals_row))
        return response

    # =========================================================================
    # 4. PURCHASE DOMAIN SUITE EXPORTERS (REGISTER, SUPPLIER, PRODUCT, DEBT)
    # =========================================================================
    @staticmethod
    def export_purchase_register_csv(
        records: List[Dict[str, Any]],
        totals: Dict[str, Any],
        filename: str = "Purchase_Register_Book.csv"
    ) -> HttpResponse:
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        headers = [
            'S.N.', 'GRN Number', 'Supplier Bill / Invoice Ref', 'Bill Date (AD)', 'Bill Date (BS)',
            'Supplier Code', 'Supplier Name', 'Supplier Phone', 'Supplier PAN/VAT', 'Outlet Branch',
            'Inward Tax Type', 'MDMS Certified', 'Gross Merchandise (NPR)', 'Trade Discount (NPR)',
            'Input VAT (NPR)', 'Freight / Shipping (NPR)', 'Customs Duty (NPR)', 'Handling / Insurance (NPR)',
            'Total Overheads (NPR)', 'Total Landed Cost (COGS) (NPR)', 'Net Invoice Total (NPR)',
            'Paid Amount (NPR)', 'Due Added to Credit (NPR)', 'Line Items Count', 'Received By'
        ]
        writer.writerow(headers)

        for r in records:
            tax_tag = '13% VAT Bill' if r.get('is_vat_bill') else 'PAN / Non-VAT'
            mdms_tag = 'Yes (MDMS Certified)' if r.get('distributor_mdms_certified') else 'No'

            row = [
                r.get('sn', ''),
                r.get('grn_number', ''),
                r.get('supplier_bill_no', ''),
                r.get('bill_date_str', ''),
                r.get('bill_date_bs', ''),
                r.get('supplier_code', ''),
                r.get('supplier_name', ''),
                r.get('supplier_phone', ''),
                r.get('supplier_pan', ''),
                r.get('branch_code', ''),
                tax_tag,
                mdms_tag,
                f"{r.get('gross_amount', 0):.2f}",
                f"{r.get('discount_amount', 0):.2f}",
                f"{r.get('vat_amount', 0):.2f}",
                f"{r.get('freight_charge', 0):.2f}",
                f"{r.get('customs_charge', 0):.2f}",
                f"{r.get('handling_charge', 0):.2f}",
                f"{r.get('overhead_total', 0):.2f}",
                f"{r.get('total_landed_cost', 0):.2f}",
                f"{r.get('net_total_amount', 0):.2f}",
                f"{r.get('paid_amount', 0):.2f}",
                f"{r.get('due_amount', 0):.2f}",
                r.get('items_count', 0),
                r.get('received_by', '')
            ]
            writer.writerow(sanitize_csv_row(row))

        totals_row = [
            'TOTALS', f"{totals.get('records_count', len(records))} Inward GRNs", '', '', '', '', '', '', '', '', '', '',
            f"{totals.get('total_gross', 0):.2f}",
            f"{totals.get('total_discount', 0):.2f}",
            f"{totals.get('total_vat', 0):.2f}",
            f"{totals.get('total_freight', 0):.2f}",
            f"{totals.get('total_customs', 0):.2f}",
            f"{totals.get('total_handling', 0):.2f}",
            f"{totals.get('total_overheads', 0):.2f}",
            f"{totals.get('total_landed', 0):.2f}",
            f"{totals.get('total_net', 0):.2f}",
            f"{totals.get('total_paid', 0):.2f}",
            f"{totals.get('total_due', 0):.2f}",
            '', ''
        ]
        writer.writerow(sanitize_csv_row(totals_row))

        return response

    @staticmethod
    def export_purchase_register_excel(
        records: List[Dict[str, Any]],
        totals: Dict[str, Any],
        date_range_label: str = "",
        branch_name: str = "",
        filename: str = "Purchase_Register_Book.xlsx"
    ) -> HttpResponse:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Purchase Register"
        ws.views.sheetView[0].showGridLines = True

        config = SystemConfiguration.get_solo()
        shop_title = config.company_name_en if config else "Smart Mobile & Optical Hub"

        ws.merge_cells("A1:T1")
        ws["A1"] = f"{shop_title.upper()} - COMMERCIAL PURCHASE REGISTER (INWARD BOOK)"
        ws["A1"].font = Font(name="Calibri", size=14, bold=True, color="FFFFFF")
        ws["A1"].fill = PatternFill(start_color="0F172A", end_color="0F172A", fill_type="solid")
        ws["A1"].alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[1].height = 28

        ws.merge_cells("A2:T2")
        ws["A2"] = f"Outlet: {branch_name or 'All Stores'} | Scope: {date_range_label} | Generated on: {timezone.now().strftime('%Y-%m-%d %H:%M')}"
        ws["A2"].font = Font(name="Calibri", size=10, italic=True, color="334155")
        ws["A2"].fill = PatternFill(start_color="F1F5F9", end_color="F1F5F9", fill_type="solid")
        ws["A2"].alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[2].height = 20

        headers = [
            'S.N.', 'GRN Number', 'Supplier Bill Ref', 'Date (AD)', 'Date (BS)',
            'Supplier Code', 'Supplier Name', 'Supplier Phone', 'Supplier PAN/VAT', 'Branch',
            'Bill Tax Mode', 'Gross Subtotal', 'Trade Discount', 'Input VAT',
            'Overhead Charges', 'Total Landed Cost', 'Net Invoice Total', 'Paid Amount',
            'Due Balance', 'Received By'
        ]

        header_font = Font(name="Calibri", size=9.5, bold=True, color="FFFFFF")
        header_fill = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
        thin_border = Border(
            left=Side(style='thin', color='CBD5E1'),
            right=Side(style='thin', color='CBD5E1'),
            top=Side(style='thin', color='CBD5E1'),
            bottom=Side(style='thin', color='CBD5E1')
        )

        ws.row_dimensions[4].height = 24
        for col_idx, h in enumerate(headers, 1):
            cell = ws.cell(row=4, column=col_idx, value=h)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = thin_border

        current_row = 5
        alt_fill = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")

        for r in records:
            ws.row_dimensions[current_row].height = 19
            is_alt = (current_row % 2 == 0)

            row_values = [
                r.get('sn', ''),
                r.get('grn_number', ''),
                r.get('supplier_bill_no', ''),
                r.get('bill_date_str', ''),
                r.get('bill_date_bs', ''),
                r.get('supplier_code', ''),
                r.get('supplier_name', ''),
                r.get('supplier_phone', ''),
                r.get('supplier_pan', ''),
                r.get('branch_code', ''),
                '13% VAT' if r.get('is_vat_bill') else 'PAN / Non-VAT',
                float(r.get('gross_amount', 0)),
                float(r.get('discount_amount', 0)),
                float(r.get('vat_amount', 0)),
                float(r.get('overhead_total', 0)),
                float(r.get('total_landed_cost', 0)),
                float(r.get('net_total_amount', 0)),
                float(r.get('paid_amount', 0)),
                float(r.get('due_amount', 0)),
                r.get('received_by', '')
            ]

            for col_idx, val in enumerate(row_values, 1):
                cell = ws.cell(row=current_row, column=col_idx, value=val)
                cell.border = thin_border
                cell.font = Font(name="Calibri", size=9)
                if is_alt:
                    cell.fill = alt_fill

                if col_idx in [1, 2, 4, 5, 6, 8, 9, 10, 11, 20]:
                    cell.alignment = Alignment(horizontal="center")
                elif col_idx in [3, 7]:
                    cell.alignment = Alignment(horizontal="left")
                elif 12 <= col_idx <= 19:
                    cell.number_format = '#,##0.00'
                    cell.alignment = Alignment(horizontal="right")
                    if col_idx in [16, 17]:
                        cell.font = Font(name="Calibri", size=9, bold=True)

            current_row += 1

        ws.row_dimensions[current_row].height = 24
        double_top_border = Border(
            left=Side(style='thin', color='CBD5E1'),
            right=Side(style='thin', color='CBD5E1'),
            top=Side(style='double', color='0F172A'),
            bottom=Side(style='double', color='0F172A')
        )
        totals_fill = PatternFill(start_color="F1F5F9", end_color="F1F5F9", fill_type="solid")

        totals_values = [
            'TOTALS', '', f"{totals.get('records_count', len(records))} Inward GRNs", '', '',
            '', '', '', '', '', '',
            float(totals.get('total_gross', 0)),
            float(totals.get('total_discount', 0)),
            float(totals.get('total_vat', 0)),
            float(totals.get('total_overheads', 0)),
            float(totals.get('total_landed', 0)),
            float(totals.get('total_net', 0)),
            float(totals.get('total_paid', 0)),
            float(totals.get('total_due', 0)),
            ''
        ]

        for col_idx, val in enumerate(totals_values, 1):
            cell = ws.cell(row=current_row, column=col_idx, value=val)
            cell.border = double_top_border
            cell.fill = totals_fill
            cell.font = Font(name="Calibri", size=9.5, bold=True)
            if col_idx in [1, 2, 3]:
                cell.alignment = Alignment(horizontal="center", vertical="center")
            elif 12 <= col_idx <= 19:
                cell.number_format = '#,##0.00'
                cell.alignment = Alignment(horizontal="right", vertical="center")

        for col in ws.columns:
            col_letter = get_column_letter(col[0].column)
            max_len = 0
            for cell in col:
                if cell.row in [1, 2]:
                    continue
                max_len = max(max_len, len(str(cell.value or '')))
            ws.column_dimensions[col_letter].width = max(max_len + 3, 11)

        buffer = io.BytesIO()
        wb.save(buffer)
        buffer.seek(0)

        response = HttpResponse(
            buffer.getvalue(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response

    @staticmethod
    def export_supplier_purchases_csv(
        records: List[Dict[str, Any]],
        totals: Dict[str, Any],
        filename: str = "Supplier_Purchases_Report.csv"
    ) -> HttpResponse:
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        headers = [
            'Supplier Code', 'Supplier / Distributor Name', 'Contact Person', 'Primary Phone', 'PAN / VAT No',
            'Classification', 'Authorized Distributor', 'Total Bills / GRNs', 'Total Returns / Debit Notes',
            'Gross Purchases (NPR)', 'Trade Discounts (NPR)', 'Input VAT (NPR)', 'Freight / Customs Overheads (NPR)',
            'Total Landed Purchases (NPR)', 'Returns Amount (NPR)', 'Net Procurement Spend (NPR)',
            'Amount Paid (NPR)', 'Due Added to Credit (NPR)', 'Current Outstanding Balance (NPR)', 'Last Purchase Date'
        ]
        writer.writerow(headers)

        for r in records:
            auth_tag = 'Yes (Authorized)' if r.get('is_authorized_distributor') else 'Standard'
            last_date_str = r['last_purchase_date'].strftime('%Y-%m-%d') if r.get('last_purchase_date') else '-'

            row = [
                r.get('supplier_code', ''),
                r.get('company_name', ''),
                r.get('contact_person', ''),
                r.get('phone_number', ''),
                r.get('pan_number', '-'),
                r.get('supplier_type', ''),
                auth_tag,
                r.get('bills_count', 0),
                r.get('returns_count', 0),
                f"{r.get('gross_amount', 0):.2f}",
                f"{r.get('discount_amount', 0):.2f}",
                f"{r.get('vat_amount', 0):.2f}",
                f"{r.get('overhead_amount', 0):.2f}",
                f"{r.get('landed_purchases', 0):.2f}",
                f"{r.get('returns_amount', 0):.2f}",
                f"{r.get('net_procurement', 0):.2f}",
                f"{r.get('paid_amount', 0):.2f}",
                f"{r.get('due_amount', 0):.2f}",
                f"{r.get('current_outstanding', 0):.2f}",
                last_date_str
            ]
            writer.writerow(sanitize_csv_row(row))

        totals_row = [
            'TOTALS', f"{totals.get('total_suppliers_active', len(records))} Active Suppliers", '', '', '', '', '',
            totals.get('total_bills_count', 0),
            totals.get('total_returns_amount', 0),
            f"{totals.get('total_gross', 0):.2f}",
            f"{totals.get('total_discount', 0):.2f}",
            f"{totals.get('total_vat', 0):.2f}",
            f"{totals.get('total_overheads', 0):.2f}",
            f"{totals.get('total_landed', 0):.2f}",
            f"{totals.get('total_returns_amount', 0):.2f}",
            f"{totals.get('total_net_procurement', 0):.2f}",
            f"{totals.get('total_paid', 0):.2f}",
            f"{totals.get('total_due_added', 0):.2f}",
            f"{totals.get('total_current_debt', 0):.2f}",
            ''
        ]
        writer.writerow(sanitize_csv_row(totals_row))

        return response

    @staticmethod
    def export_product_purchases_csv(
        records: List[Dict[str, Any]],
        totals: Dict[str, Any],
        filename: str = "Product_Purchases_Report.csv"
    ) -> HttpResponse:
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        headers = [
            'Product SKU', 'Barcode', 'Product / Handset Model', 'Category', 'Brand', 'Variant Specs',
            'Tracking Type', 'Total Quantity Purchased', 'Base Unit', 'Net Purchase Spend (NPR)',
            'Weighted Avg Purchase Rate (NPR)', 'Latest Purchase Rate (NPR)', 'Current Counter MRP (NPR)',
            'Potential Markup (NPR)', 'Markup %', 'Primary Supplier', 'Inward GRN Count',
            'Last Inward Date (AD)', 'Last Inward Date (BS)'
        ]
        writer.writerow(headers)

        for r in records:
            tracking_str = 'Serialized (IMEI)' if r.get('is_serialized') else 'Standard Bulk'

            row = [
                r.get('sku', ''),
                r.get('barcode', ''),
                r.get('name', ''),
                r.get('category_name', ''),
                r.get('brand_name', ''),
                r.get('variant_specs', ''),
                tracking_str,
                f"{r.get('quantity_purchased', 0):.3f}",
                r.get('unit_code', 'PCS'),
                f"{r.get('total_spend', 0):.2f}",
                f"{r.get('weighted_avg_rate', 0):.2f}",
                f"{r.get('latest_purchase_rate', 0):.2f}",
                f"{r.get('current_mrp', 0):.2f}",
                f"{r.get('margin_potential', 0):.2f}",
                f"{r.get('markup_percentage', 0):.1f}%",
                r.get('primary_supplier', ''),
                r.get('grn_count', 1),
                r.get('last_purchase_date_str', '-'),
                r.get('last_purchase_date_bs', '-')
            ]
            writer.writerow(sanitize_csv_row(row))

        totals_row = [
            'TOTALS', f"{totals.get('distinct_products_count', len(records))} Products", '', '', '', '', '',
            f"{totals.get('total_units_purchased', 0):.3f}", '',
            f"{totals.get('total_procurement_spend', 0):.2f}",
            '', '', '', '', '', '', '', '', ''
        ]
        writer.writerow(sanitize_csv_row(totals_row))

        return response

    @staticmethod
    def export_supplier_outstanding_csv(
        records: List[Dict[str, Any]],
        totals: Dict[str, Any],
        filename: str = "Supplier_Outstanding_Payables.csv"
    ) -> HttpResponse:
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        headers = [
            'Supplier Code', 'Supplier / Distributor Name', 'Contact Person', 'Primary Mobile', 'PAN / VAT No',
            'Classification', 'Credit Terms (Days)', 'Credit Limit (NPR)', 'Opening Balance (NPR)',
            'Period Purchases (NPR)', 'Period Payments (NPR)', 'Period Returns (NPR)',
            'Current Outstanding Payable (NPR)', 'Overdue Status', 'Overdue Days',
            'Last Purchase Date (AD)', 'Last Purchase Date (BS)', 'Last Payment Date (AD)',
            'Bank Name', 'Bank Account Number', 'Account Holder Name', 'Bank Branch Location', 'Has Merchant QR'
        ]
        writer.writerow(headers)

        for r in records:
            overdue_tag = f"OVERDUE ({r.get('overdue_days')} Days)" if r.get('is_overdue') else 'Within Terms'
            qr_tag = 'Yes' if r.get('has_qr') else 'No'

            row = [
                r.get('supplier_code', ''),
                r.get('company_name', ''),
                r.get('contact_person', ''),
                r.get('phone_number', ''),
                r.get('pan_number', '-'),
                r.get('supplier_type', ''),
                r.get('credit_period_days', 30),
                f"{r.get('credit_limit', 0):.2f}",
                f"{r.get('opening_balance', 0):.2f}",
                f"{r.get('period_purchases', 0):.2f}",
                f"{r.get('period_payments', 0):.2f}",
                f"{r.get('period_returns', 0):.2f}",
                f"{r.get('current_balance', 0):.2f}",
                overdue_tag,
                r.get('overdue_days', 0),
                r.get('last_purchase_date_str', '-'),
                r.get('last_purchase_date_bs', '-'),
                r.get('last_payment_date_str', '-'),
                r.get('bank_name', '-'),
                r.get('bank_account_number', '-'),
                r.get('account_holder_name', '-'),
                r.get('bank_branch', '-'),
                qr_tag
            ]
            writer.writerow(sanitize_csv_row(row))

        totals_row = [
            'TOTALS', f"{totals.get('total_suppliers_count', len(records))} Suppliers", '', '', '', '', '', '', '',
            '', '', '',
            f"{totals.get('total_payable_debt', 0):.2f}",
            f"{totals.get('overdue_vendors_count', 0)} Overdue (Rs. {totals.get('overdue_debt_amount', 0):.2f})",
            '', '', '', '', '', '', '', '', ''
        ]
        writer.writerow(sanitize_csv_row(totals_row))

        return response

    # =========================================================================
    # 5. SALES & COMMERCIAL INTELLIGENCE EXPORTERS (NEWLY IMPLEMENTED)
    # =========================================================================

    @staticmethod
    def export_sales_summary_csv(
        records: List[Dict[str, Any]],
        totals: Dict[str, Any],
        filename: str = "Sales_Summary_Report.csv"
    ) -> HttpResponse:
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        headers = [
            'Date (AD)', 'Date (BS)', 'Invoices', 'Units Sold', 'Gross Subtotal (NPR)',
            'Item Discounts (NPR)', 'Bill Discounts (NPR)', 'Total Discounts (NPR)', 'Trade-In Credit (NPR)',
            'Taxable Base (NPR)', 'Non-Taxable Base (NPR)', 'Tax / VAT (NPR)', 'Net Grand Total (NPR)',
            'Cost of Goods Sold (COGS) (NPR)', 'Gross Profit Realized (NPR)', 'Gross Margin %',
            'Paid Amount (NPR)', 'Due Balance (Udhaari) (NPR)', 'Cash Inflow (NPR)',
            'FonePay QR (NPR)', 'eSewa (NPR)', 'Khalti (NPR)', 'Card Swipe (NPR)',
            'Bank Transfer (NPR)', 'Credit Authorized (NPR)'
        ]
        writer.writerow(headers)

        for r in records:
            row = [
                r.get('date_ad_str', ''),
                r.get('date_bs', ''),
                r.get('invoice_count', 0),
                f"{r.get('units_sold', 0):.3f}",
                f"{r.get('gross_subtotal', 0):.2f}",
                f"{r.get('item_discount_sum', 0):.2f}",
                f"{r.get('bill_discount_sum', 0):.2f}",
                f"{r.get('total_discount_sum', 0):.2f}",
                f"{r.get('trade_in_credit_sum', 0):.2f}",
                f"{r.get('taxable_amount', 0):.2f}",
                f"{r.get('non_taxable_amount', 0):.2f}",
                f"{r.get('vat_amount', 0):.2f}",
                f"{r.get('grand_total', 0):.2f}",
                f"{r.get('cogs_amount', 0):.2f}",
                f"{r.get('gross_profit', 0):.2f}",
                f"{r.get('margin_percent', 0):.1f}%",
                f"{r.get('paid_amount', 0):.2f}",
                f"{r.get('due_amount', 0):.2f}",
                f"{r.get('cash_collected', 0):.2f}",
                f"{r.get('fonepay_collected', 0):.2f}",
                f"{r.get('esewa_collected', 0):.2f}",
                f"{r.get('khalti_collected', 0):.2f}",
                f"{r.get('card_collected', 0):.2f}",
                f"{r.get('bank_collected', 0):.2f}",
                f"{r.get('credit_authorized', 0):.2f}"
            ]
            writer.writerow(sanitize_csv_row(row))

        totals_row = [
            'TOTALS', '',
            totals.get('total_invoices', 0),
            f"{totals.get('total_units_sold', 0):.3f}",
            f"{totals.get('total_gross', 0):.2f}",
            f"{totals.get('total_item_disc', 0):.2f}",
            f"{totals.get('total_bill_disc', 0):.2f}",
            f"{totals.get('total_sales_disc', 0):.2f}",
            f"{totals.get('total_trade_in', 0):.2f}",
            f"{totals.get('total_taxable', 0):.2f}",
            f"{totals.get('total_non_taxable', 0):.2f}",
            f"{totals.get('total_vat', 0):.2f}",
            f"{totals.get('total_net_turnover', 0):.2f}",
            f"{totals.get('total_cogs', 0):.2f}",
            f"{totals.get('total_profit', 0):.2f}",
            f"{totals.get('overall_margin_pct', 0):.1f}%",
            f"{totals.get('total_paid', 0):.2f}",
            f"{totals.get('total_due', 0):.2f}",
            f"{totals.get('total_cash', 0):.2f}",
            f"{totals.get('total_fonepay', 0):.2f}",
            f"{totals.get('total_esewa', 0):.2f}",
            f"{totals.get('total_khalti', 0):.2f}",
            f"{totals.get('total_card', 0):.2f}",
            f"{totals.get('total_bank', 0):.2f}",
            f"{totals.get('total_credit', 0):.2f}"
        ]
        writer.writerow(sanitize_csv_row(totals_row))

        return response

    @staticmethod
    def export_sales_summary_excel(
        records: List[Dict[str, Any]],
        totals: Dict[str, Any],
        date_range_label: str = "",
        filename: str = "Sales_Summary_Report.xlsx"
    ) -> HttpResponse:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Sales Summary"
        ws.views.sheetView[0].showGridLines = True

        config = SystemConfiguration.get_solo()
        shop_title = config.company_name_en if config else "Smart Mobile & Optical Hub"

        ws.merge_cells("A1:Y1")
        ws["A1"] = f"{shop_title.upper()} - SALES SUMMARY & REVENUE REPORT"
        ws["A1"].font = Font(name="Calibri", size=14, bold=True, color="FFFFFF")
        ws["A1"].fill = PatternFill(start_color="0F172A", end_color="0F172A", fill_type="solid")
        ws["A1"].alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[1].height = 28

        ws.merge_cells("A2:Y2")
        ws["A2"] = f"Period: {date_range_label} | Generated on: {timezone.now().strftime('%Y-%m-%d %H:%M')}"
        ws["A2"].font = Font(name="Calibri", size=10, italic=True, color="334155")
        ws["A2"].fill = PatternFill(start_color="F1F5F9", end_color="F1F5F9", fill_type="solid")
        ws["A2"].alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[2].height = 20

        headers = [
            'Date (AD)', 'Date (BS)', 'Invoices', 'Units Sold', 'Gross Subtotal',
            'Item Discounts', 'Bill Discounts', 'Total Discounts', 'Trade-In Credit',
            'Taxable Base', 'Non-Taxable Base', 'Tax / VAT', 'Grand Total', 'COGS',
            'Gross Profit', 'Margin %', 'Paid Amount', 'Due Udhaari',
            'Cash Inflow', 'FonePay QR', 'eSewa', 'Khalti', 'Card', 'Bank', 'Credit'
        ]

        header_font = Font(name="Calibri", size=9.5, bold=True, color="FFFFFF")
        header_fill = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
        thin_border = Border(
            left=Side(style='thin', color='CBD5E1'),
            right=Side(style='thin', color='CBD5E1'),
            top=Side(style='thin', color='CBD5E1'),
            bottom=Side(style='thin', color='CBD5E1')
        )

        ws.row_dimensions[4].height = 24
        for col_idx, h in enumerate(headers, 1):
            cell = ws.cell(row=4, column=col_idx, value=h)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = thin_border

        current_row = 5
        alt_fill = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")

        for r in records:
            ws.row_dimensions[current_row].height = 19
            is_alt = (current_row % 2 == 0)

            row_values = [
                r.get('date_ad_str', ''),
                r.get('date_bs', ''),
                int(r.get('invoice_count', 0)),
                float(r.get('units_sold', 0)),
                float(r.get('gross_subtotal', 0)),
                float(r.get('item_discount_sum', 0)),
                float(r.get('bill_discount_sum', 0)),
                float(r.get('total_discount_sum', 0)),
                float(r.get('trade_in_credit_sum', 0)),
                float(r.get('taxable_amount', 0)),
                float(r.get('non_taxable_amount', 0)),
                float(r.get('vat_amount', 0)),
                float(r.get('grand_total', 0)),
                float(r.get('cogs_amount', 0)),
                float(r.get('gross_profit', 0)),
                float(r.get('margin_percent', 0)) / 100.0,
                float(r.get('paid_amount', 0)),
                float(r.get('due_amount', 0)),
                float(r.get('cash_collected', 0)),
                float(r.get('fonepay_collected', 0)),
                float(r.get('esewa_collected', 0)),
                float(r.get('khalti_collected', 0)),
                float(r.get('card_collected', 0)),
                float(r.get('bank_collected', 0)),
                float(r.get('credit_authorized', 0))
            ]

            for col_idx, val in enumerate(row_values, 1):
                cell = ws.cell(row=current_row, column=col_idx, value=val)
                cell.border = thin_border
                cell.font = Font(name="Calibri", size=9)
                if is_alt:
                    cell.fill = alt_fill

                if col_idx in [1, 2]:
                    cell.alignment = Alignment(horizontal="center")
                elif col_idx in [3]:
                    cell.alignment = Alignment(horizontal="center")
                    cell.number_format = '#,##0'
                elif col_idx == 4:
                    cell.alignment = Alignment(horizontal="right")
                    cell.number_format = '#,##0.00'
                elif col_idx == 16:
                    cell.alignment = Alignment(horizontal="right")
                    cell.number_format = '0.0%'
                else:
                    cell.alignment = Alignment(horizontal="right")
                    cell.number_format = '#,##0.00'

            current_row += 1

        ws.row_dimensions[current_row].height = 24
        double_top_border = Border(
            left=Side(style='thin', color='CBD5E1'),
            right=Side(style='thin', color='CBD5E1'),
            top=Side(style='double', color='0F172A'),
            bottom=Side(style='double', color='0F172A')
        )
        totals_fill = PatternFill(start_color="F1F5F9", end_color="F1F5F9", fill_type="solid")

        totals_values = [
            'TOTALS', '',
            int(totals.get('total_invoices', 0)),
            float(totals.get('total_units_sold', 0)),
            float(totals.get('total_gross', 0)),
            float(totals.get('total_item_disc', 0)),
            float(totals.get('total_bill_disc', 0)),
            float(totals.get('total_sales_disc', 0)),
            float(totals.get('total_trade_in', 0)),
            float(totals.get('total_taxable', 0)),
            float(totals.get('total_non_taxable', 0)),
            float(totals.get('total_vat', 0)),
            float(totals.get('total_net_turnover', 0)),
            float(totals.get('total_cogs', 0)),
            float(totals.get('total_profit', 0)),
            float(totals.get('overall_margin_pct', 0)) / 100.0,
            float(totals.get('total_paid', 0)),
            float(totals.get('total_due', 0)),
            float(totals.get('total_cash', 0)),
            float(totals.get('total_fonepay', 0)),
            float(totals.get('total_esewa', 0)),
            float(totals.get('total_khalti', 0)),
            float(totals.get('total_card', 0)),
            float(totals.get('total_bank', 0)),
            float(totals.get('total_credit', 0))
        ]

        for col_idx, val in enumerate(totals_values, 1):
            cell = ws.cell(row=current_row, column=col_idx, value=val)
            cell.border = double_top_border
            cell.fill = totals_fill
            cell.font = Font(name="Calibri", size=9.5, bold=True)
            if col_idx in [1, 2]:
                cell.alignment = Alignment(horizontal="center", vertical="center")
            elif col_idx in [3]:
                cell.alignment = Alignment(horizontal="center", vertical="center")
                cell.number_format = '#,##0'
            elif col_idx == 16:
                cell.alignment = Alignment(horizontal="right", vertical="center")
                cell.number_format = '0.0%'
            else:
                cell.alignment = Alignment(horizontal="right", vertical="center")
                cell.number_format = '#,##0.00'

        for col in ws.columns:
            col_letter = get_column_letter(col[0].column)
            max_len = 0
            for cell in col:
                if cell.row in [1, 2]:
                    continue
                max_len = max(max_len, len(str(cell.value or '')))
            ws.column_dimensions[col_letter].width = max(max_len + 3, 11)

        buffer = io.BytesIO()
        wb.save(buffer)
        buffer.seek(0)

        response = HttpResponse(
            buffer.getvalue(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response

    @staticmethod
    def export_product_sales_csv(
        records: List[Dict[str, Any]],
        totals: Dict[str, Any],
        filename: str = "Product_Sales_Report.csv"
    ) -> HttpResponse:
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        headers = [
            'SKU', 'Barcode', 'Product Name', 'Category', 'Brand', 'Variant Specs',
            'Tracking Mode', 'Invoices Count', 'Quantity Sold', 'Unit', 'Avg Selling Price (NPR)',
            'Gross Revenue (NPR)', 'Discounts Deducted (NPR)', 'Net Revenue (NPR)',
            'COGS Landed Cost (NPR)', 'Gross Profit (NPR)', 'Gross Margin %', 'Stock On Hand'
        ]
        writer.writerow(headers)

        for r in records:
            tracking_label = 'Serialized (IMEI)' if r.get('is_serialized') else 'Standard Bulk'
            row = [
                r.get('sku', ''),
                r.get('barcode', ''),
                r.get('name', ''),
                r.get('category_name', ''),
                r.get('brand_name', ''),
                r.get('variant_specs', ''),
                tracking_label,
                r.get('invoices_count', 0),
                f"{r.get('quantity_sold', 0):.3f}",
                r.get('unit_code', 'PCS'),
                f"{r.get('avg_realized_price', 0):.2f}",
                f"{r.get('gross_revenue', 0):.2f}",
                f"{r.get('total_discounts', 0):.2f}",
                f"{r.get('net_revenue', 0):.2f}",
                f"{r.get('cogs_total', 0):.2f}",
                f"{r.get('gross_profit', 0):.2f}",
                f"{r.get('margin_percent', 0):.1f}%",
                f"{r.get('available_stock', 0):.1f}"
            ]
            writer.writerow(sanitize_csv_row(row))

        totals_row = [
            'TOTALS', '', f"{totals.get('products_count', len(records))} Products", '', '', '', '', '',
            f"{totals.get('total_units_sold', 0):.3f}", '', '',
            f"{totals.get('total_gross_rev', 0):.2f}",
            f"{totals.get('total_disc_deducted', 0):.2f}",
            f"{totals.get('total_net_rev', 0):.2f}",
            f"{totals.get('total_cogs_sum', 0):.2f}",
            f"{totals.get('total_profit_sum', 0):.2f}",
            f"{totals.get('overall_margin_pct', 0):.1f}%",
            ''
        ]
        writer.writerow(sanitize_csv_row(totals_row))

        return response

    @staticmethod
    def export_brand_sales_csv(
        records: List[Dict[str, Any]],
        totals: Dict[str, Any],
        filename: str = "Brand_Sales_Report.csv"
    ) -> HttpResponse:
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        headers = [
            'Brand Name', 'Invoices Count', 'Handset Units Sold (IMEI)', 'Accessory Units Sold',
            'Total Units Sold', 'Gross Sales (NPR)', 'Trade Discounts (NPR)', 'Net Turnover (NPR)',
            'Landed COGS (NPR)', 'Gross Profit (NPR)', 'Gross Margin %', 'Turnover Share %'
        ]
        writer.writerow(headers)

        for r in records:
            row = [
                r.get('brand_name', ''),
                r.get('invoices_count', 0),
                f"{r.get('handset_units_sold', 0):.3f}",
                f"{r.get('accessory_units_sold', 0):.3f}",
                f"{r.get('total_units_sold', 0):.3f}",
                f"{r.get('gross_sales', 0):.2f}",
                f"{r.get('total_discounts', 0):.2f}",
                f"{r.get('net_turnover', 0):.2f}",
                f"{r.get('total_cogs', 0):.2f}",
                f"{r.get('gross_profit', 0):.2f}",
                f"{r.get('margin_percent', 0):.1f}%",
                f"{r.get('turnover_share_percent', 0):.1f}%"
            ]
            writer.writerow(sanitize_csv_row(row))

        totals_row = [
            'TOTALS', '',
            f"{totals.get('total_handsets', 0):.3f}",
            f"{totals.get('total_accessories', 0):.3f}",
            f"{totals.get('total_units_sold', 0):.3f}",
            f"{totals.get('total_gross', 0):.2f}",
            f"{totals.get('total_discounts', 0):.2f}",
            f"{totals.get('total_net', 0):.2f}",
            f"{totals.get('total_cogs', 0):.2f}",
            f"{totals.get('total_profit', 0):.2f}",
            f"{totals.get('overall_margin_pct', 0):.1f}%",
            '100.0%'
        ]
        writer.writerow(sanitize_csv_row(totals_row))

        return response

    @staticmethod
    def export_category_sales_csv(
        records: List[Dict[str, Any]],
        totals: Dict[str, Any],
        filename: str = "Category_Sales_Report.csv"
    ) -> HttpResponse:
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        headers = [
            'Category Code', 'Category Name', 'Invoices Count', 'Line Items Billed',
            'Units Sold', 'Gross Sales (NPR)', 'Trade Discounts (NPR)', 'Net Turnover (NPR)',
            'Landed COGS (NPR)', 'Gross Profit (NPR)', 'Gross Margin %', 'Revenue Contribution %'
        ]
        writer.writerow(headers)

        for r in records:
            row = [
                r.get('category_code', ''),
                r.get('category_name', ''),
                r.get('invoices_count', 0),
                r.get('lines_count', 0),
                f"{r.get('total_units_sold', 0):.3f}",
                f"{r.get('gross_sales', 0):.2f}",
                f"{r.get('total_discounts', 0):.2f}",
                f"{r.get('net_turnover', 0):.2f}",
                f"{r.get('total_cogs', 0):.2f}",
                f"{r.get('gross_profit', 0):.2f}",
                f"{r.get('margin_percent', 0):.1f}%",
                f"{r.get('contribution_percent', 0):.1f}%"
            ]
            writer.writerow(sanitize_csv_row(row))

        totals_row = [
            'TOTALS', f"{totals.get('categories_count', len(records))} Categories", '',
            totals.get('total_lines_count', 0),
            f"{totals.get('total_units_sold', 0):.3f}",
            f"{totals.get('total_gross', 0):.2f}",
            f"{totals.get('total_discounts', 0):.2f}",
            f"{totals.get('total_net', 0):.2f}",
            f"{totals.get('total_cogs', 0):.2f}",
            f"{totals.get('total_profit', 0):.2f}",
            f"{totals.get('overall_margin_pct', 0):.1f}%",
            '100.0%'
        ]
        writer.writerow(sanitize_csv_row(totals_row))

        return response

    @staticmethod
    def export_sold_imei_csv(
        records: List[Dict[str, Any]],
        totals: Dict[str, Any],
        filename: str = "Sold_IMEI_Registry_Report.csv"
    ) -> HttpResponse:
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        headers = [
            'Estimate No', 'Sale Date (AD)', 'Sale Date (BS)', 'Customer Name', 'Customer Phone',
            'Product / Handset Model', 'Brand', 'RAM / Storage / Color', 'Primary IMEI 1', 'Secondary IMEI 2',
            'Serial Number', 'Device UID', 'Condition Grade', 'NTA MDMS Status', 'Landed Cost (NPR)',
            'Sold Price (NPR)', 'Gross Profit Realized (NPR)', 'Gross Margin %', 'Warranty Coverage Terms',
            'Warranty Expiry Date', 'Warranty Currently Active?', 'Outlet Branch', 'Sales Rep', 'Cashier'
        ]
        writer.writerow(headers)

        for r in records:
            row = [
                r.get('estimate_number', ''),
                r.get('date_ad_str', ''),
                r.get('date_bs_str', ''),
                r.get('customer_name', ''),
                r.get('customer_phone', ''),
                r.get('product_name', ''),
                r.get('brand_name', ''),
                r.get('specs', ''),
                r.get('imei_1', ''),
                r.get('imei_2', ''),
                r.get('serial_number', ''),
                r.get('device_uid', ''),
                r.get('condition', ''),
                r.get('mdms_label', ''),
                f"{r.get('landed_cost', 0):.2f}",
                f"{r.get('sold_price', 0):.2f}",
                f"{r.get('gross_profit', 0):.2f}",
                f"{r.get('margin_percent', 0):.1f}%",
                r.get('warranty_terms', ''),
                r.get('warranty_expiry_str', '-'),
                'Yes (Active)' if r.get('is_warranty_active') else 'Expired / Void',
                r.get('branch_code', ''),
                r.get('salesperson', ''),
                r.get('cashier', '')
            ]
            writer.writerow(sanitize_csv_row(row))

        totals_row = [
            'TOTALS', '', '', '', '', f"{totals.get('total_handsets_count', len(records))} Handsets Sold", '', '', '', '', '', '', '', '',
            f"{totals.get('total_cost_amount', 0):.2f}",
            f"{totals.get('total_sold_amount', 0):.2f}",
            f"{totals.get('total_profit_amount', 0):.2f}",
            f"{totals.get('overall_margin_pct', 0):.1f}%",
            f"{totals.get('active_warranty_count', 0)} Active Warranties",
            f"{totals.get('mdms_registered_count', 0)} MDMS Registered", '', '', '', ''
        ]
        writer.writerow(sanitize_csv_row(totals_row))

        return response

    @staticmethod
    def export_salesperson_sales_csv(
        records: List[Dict[str, Any]],
        totals: Dict[str, Any],
        filename: str = "Salesperson_Sales_Report.csv"
    ) -> HttpResponse:
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        headers = [
            'Sales Representative', 'Username', 'System Role', 'Assigned Branch', 'Invoices Count',
            'Phone Units Sold (IMEI)', 'Accessory Units Sold', 'Total Units Sold', 'Gross Sales (NPR)',
            'Discounts Given (NPR)', 'Net Turnover (NPR)', 'Landed COGS (NPR)', 'Gross Profit Contribution (NPR)',
            'Gross Margin %', 'Average Basket / Ticket Size (NPR)', 'Cash Collected (NPR)', 'Due Udhaari Added (NPR)'
        ]
        writer.writerow(headers)

        for r in records:
            row = [
                r.get('name', ''),
                r.get('username', ''),
                r.get('role', ''),
                r.get('branch_name', ''),
                r.get('invoices_count', 0),
                f"{r.get('phone_units_sold', 0):.0f}",
                f"{r.get('accessory_units_sold', 0):.3f}",
                f"{r.get('total_units_sold', 0):.3f}",
                f"{r.get('gross_sales', 0):.2f}",
                f"{r.get('total_discounts', 0):.2f}",
                f"{r.get('net_turnover', 0):.2f}",
                f"{r.get('total_cogs', 0):.2f}",
                f"{r.get('gross_profit', 0):.2f}",
                f"{r.get('margin_percent', 0):.1f}%",
                f"{r.get('avg_basket_size', 0):.2f}",
                f"{r.get('paid_amount', 0):.2f}",
                f"{r.get('due_amount', 0):.2f}"
            ]
            writer.writerow(sanitize_csv_row(row))

        totals_row = [
            'TOTALS', f"{totals.get('salespeople_count', len(records))} Staff", '', '',
            totals.get('total_invoices', 0),
            f"{totals.get('total_phones_sold', 0):.0f}",
            f"{totals.get('total_accessories_sold', 0):.3f}",
            f"{totals.get('total_units_sold', 0):.3f}",
            f"{totals.get('total_gross_sales', 0):.2f}",
            f"{totals.get('total_discounts_given', 0):.2f}",
            f"{totals.get('total_net_turnover', 0):.2f}",
            f"{totals.get('total_cogs', 0):.2f}",
            f"{totals.get('total_gross_profit', 0):.2f}",
            f"{totals.get('overall_margin_pct', 0):.1f}%",
            f"{totals.get('overall_avg_basket', 0):.2f}",
            f"{totals.get('total_paid', 0):.2f}",
            f"{totals.get('total_due', 0):.2f}"
        ]
        writer.writerow(sanitize_csv_row(totals_row))

        return response

    @staticmethod
    def export_cashier_sales_csv(
        records: List[Dict[str, Any]],
        totals: Dict[str, Any],
        filename: str = "Cashier_Collections_Report.csv"
    ) -> HttpResponse:
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        headers = [
            'Cashier Name', 'Username', 'Assigned Outlet', 'Invoices Count', 'Gross Subtotal (NPR)',
            'Discounts Given (NPR)', 'Net Turnover (NPR)', 'Net Cash Retained (NPR)', 'Cash Change Given (NPR)',
            'FonePay QR (NPR)', 'eSewa (NPR)', 'Khalti (NPR)', 'Digital Total (NPR)',
            'POS Card Swipes (NPR)', 'Bank Transfers (NPR)', 'Credit Authorized (Udhaari) (NPR)',
            'Customer Returns Handled', 'Cash Refunds Issued (NPR)', 'Total Refunds Issued (NPR)'
        ]
        writer.writerow(headers)

        for r in records:
            row = [
                r.get('name', ''),
                r.get('username', ''),
                r.get('branch_name', ''),
                r.get('invoices_count', 0),
                f"{r.get('gross_subtotal', 0):.2f}",
                f"{r.get('total_discounts', 0):.2f}",
                f"{r.get('net_turnover', 0):.2f}",
                f"{r.get('cash_net_retained', 0):.2f}",
                f"{r.get('change_given', 0):.2f}",
                f"{r.get('fonepay_collected', 0):.2f}",
                f"{r.get('esewa_collected', 0):.2f}",
                f"{r.get('khalti_collected', 0):.2f}",
                f"{r.get('digital_total', 0):.2f}",
                f"{r.get('card_collected', 0):.2f}",
                f"{r.get('bank_collected', 0):.2f}",
                f"{r.get('credit_authorized', 0):.2f}",
                r.get('returns_count', 0),
                f"{r.get('cash_refunds_issued', 0):.2f}",
                f"{r.get('total_refunds_issued', 0):.2f}"
            ]
            writer.writerow(sanitize_csv_row(row))

        totals_row = [
            'TOTALS', f"{totals.get('cashiers_count', len(records))} Cashiers", '',
            totals.get('total_invoices', 0),
            f"{totals.get('total_gross_subtotal', 0):.2f}",
            f"{totals.get('total_discounts_given', 0):.2f}",
            f"{totals.get('total_net_turnover', 0):.2f}",
            f"{totals.get('total_cash_net', 0):.2f}",
            f"{totals.get('total_change_given', 0):.2f}",
            f"{totals.get('total_fonepay', 0):.2f}",
            f"{totals.get('total_esewa', 0):.2f}",
            f"{totals.get('total_khalti', 0):.2f}",
            f"{totals.get('total_digital', 0):.2f}",
            f"{totals.get('total_card', 0):.2f}",
            f"{totals.get('total_bank', 0):.2f}",
            f"{totals.get('total_credit', 0):.2f}",
            '', '',
            f"{totals.get('total_refunds_issued', 0):.2f}"
        ]
        writer.writerow(sanitize_csv_row(totals_row))

        return response

    @staticmethod
    def export_payment_methods_csv(
        records: List[Dict[str, Any]],
        totals: Dict[str, Any],
        filename: str = "Payment_Methods_Report.csv"
    ) -> HttpResponse:
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        headers = [
            'Tender Channel / Method', 'Transactions Count', 'Bills Billed Count',
            'Gross Tendered Amount (NPR)', 'Cash Change Deducted (NPR)', 'Net Inflow Total (NPR)',
            'Collection Share %'
        ]
        writer.writerow(headers)

        for r in records:
            row = [
                r.get('mode_display', ''),
                r.get('transaction_count', 0),
                r.get('invoice_count', 0),
                f"{r.get('gross_amount', 0):.2f}",
                f"{r.get('change_deducted', 0):.2f}",
                f"{r.get('net_amount', 0):.2f}",
                f"{r.get('share_percent', 0):.1f}%"
            ]
            writer.writerow(sanitize_csv_row(row))

        totals_row = [
            'TOTALS',
            totals.get('total_transactions', 0),
            '',
            f"{totals.get('total_tendered_gross', 0):.2f}",
            f"{totals.get('total_change_given', 0):.2f}",
            f"{totals.get('total_net_collections', 0):.2f}",
            '100.0%'
        ]
        writer.writerow(sanitize_csv_row(totals_row))

        return response

    @staticmethod
    def export_discount_overrides_csv(
        records: List[Dict[str, Any]],
        totals: Dict[str, Any],
        filename: str = "Discount_Overrides_Report.csv"
    ) -> HttpResponse:
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        headers = [
            'Estimate No', 'Sale Date (AD)', 'Sale Date (BS)', 'Branch', 'Customer Name',
            'Product Name', 'SKU', 'IMEI / Serial', 'Quantity', 'Unit', 'Official Catalog Price (NPR)',
            'Submitted Unit Rate (NPR)', 'Line Gross Subtotal (NPR)', 'Price Override Concession (NPR)',
            'Item Discount Type', 'Discount Input Value', 'Actual Item Discount (NPR)',
            'Effective Discount %', 'Allocated Bill Discount (NPR)', 'Total Line Concession (NPR)',
            'Net Line Total (NPR)', 'Supervisor Manager Approver', 'Commercial Justification Reason', 'Cashier'
        ]
        writer.writerow(headers)

        for r in records:
            row = [
                r.get('estimate_number', ''),
                r.get('date_ad_str', ''),
                r.get('date_bs_str', ''),
                r.get('branch_code', ''),
                r.get('customer_name', ''),
                r.get('product_name', ''),
                r.get('sku', ''),
                r.get('imei_number', ''),
                f"{r.get('quantity', 0):.3f}",
                r.get('unit_code', 'PCS'),
                f"{r.get('official_unit_price', 0):.2f}",
                f"{r.get('unit_price', 0):.2f}",
                f"{r.get('line_gross', 0):.2f}",
                f"{r.get('price_override_amount', 0):.2f}",
                r.get('discount_type', ''),
                f"{r.get('discount_input_value', 0):.2f}",
                f"{r.get('item_discount_amount', 0):.2f}",
                f"{r.get('effective_discount_percent', 0):.1f}%",
                f"{r.get('allocated_bill_discount', 0):.2f}",
                f"{r.get('total_line_concession', 0):.2f}",
                f"{r.get('line_total', 0):.2f}",
                r.get('manager_override_by', ''),
                r.get('discount_reason', ''),
                r.get('cashier', '')
            ]
            writer.writerow(sanitize_csv_row(row))

        totals_row = [
            'TOTALS', '', '', '', '', f"{totals.get('total_lines', len(records))} Concession Lines", '', '', '', '', '', '', '',
            f"{totals.get('total_price_overrides', 0):.2f}", '', '',
            f"{totals.get('total_item_disc_amount_type', 0) + totals.get('total_item_disc_percent_type', 0):.2f}",
            '',
            f"{totals.get('total_bill_disc_alloc', 0):.2f}",
            f"{totals.get('total_concessions_granted', 0):.2f}",
            '', '', '', ''
        ]
        writer.writerow(sanitize_csv_row(totals_row))

        return response

    @staticmethod
    def export_trade_in_sales_csv(
        records: List[Dict[str, Any]],
        totals: Dict[str, Any],
        filename: str = "Trade_In_Sales_Settlement_Report.csv"
    ) -> HttpResponse:
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        headers = [
            'Sales Estimate No', 'Trade-In Voucher No', 'Date (AD)', 'Date (BS)', 'Branch', 'Customer Name',
            'Customer Phone', 'New Phone Sold', 'New Phone IMEI', 'New Phone Price (NPR)', 'New Phone Landed Cost (NPR)',
            'Traded Old Phone Model', 'Old Phone Storage & Specs', 'Old Phone IMEI', 'Old Condition Grade',
            'Old Phone NTA MDMS Status', 'Trade-In Credit Applied (NPR)', 'Cash Top-Up Paid (NPR)',
            'Due Udhaari Added (NPR)', 'Combined Deal Profit (NPR)', 'Police KYC Undertaking Signed?', 'Cashier'
        ]
        writer.writerow(headers)

        for r in records:
            row = [
                r.get('estimate_number', ''),
                r.get('voucher_number', ''),
                r.get('date_ad_str', ''),
                r.get('date_bs_str', ''),
                r.get('branch_code', ''),
                r.get('customer_name', ''),
                r.get('customer_phone', ''),
                r.get('new_phone_name', ''),
                r.get('new_phone_imei', ''),
                f"{r.get('new_phone_price', 0):.2f}",
                f"{r.get('new_phone_cost', 0):.2f}",
                r.get('old_phone_model', ''),
                r.get('old_phone_specs', ''),
                r.get('old_phone_imei', ''),
                r.get('old_condition', ''),
                r.get('old_mdms', ''),
                f"{r.get('trade_in_credit', 0):.2f}",
                f"{r.get('cash_topup_paid', 0):.2f}",
                f"{r.get('due_balance', 0):.2f}",
                f"{r.get('deal_profit', 0):.2f}",
                'Yes (Compliant)' if r.get('undertaking_signed') else 'Pending / Missing',
                r.get('cashier', '')
            ]
            writer.writerow(sanitize_csv_row(row))

        totals_row = [
            'TOTALS', f"{totals.get('total_deals_count', len(records))} Exchange Deals", '', '', '', '', '', '', '',
            f"{totals.get('total_new_phones_value', 0):.2f}", '', '', '', '', '', '',
            f"{totals.get('total_trade_in_credits', 0):.2f}",
            f"{totals.get('total_cash_topup_collected', 0):.2f}", '',
            f"{totals.get('total_combined_profit', 0):.2f}", '', ''
        ]
        writer.writerow(sanitize_csv_row(totals_row))

        return response

    @staticmethod
    def export_gross_profit_csv(
        records: List[Dict[str, Any]],
        totals: Dict[str, Any],
        filename: str = "Gross_Profit_Margins_Report.csv"
    ) -> HttpResponse:
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        headers = [
            'SKU', 'Product Name', 'Category', 'Brand', 'Unit', 'Tracking Mode',
            'Quantity Sold', 'Gross Revenue (NPR)', 'Discounts Deducted (NPR)', 'Net Revenue Excl. Tax (NPR)',
            'Landed COGS (NPR)', 'Gross Profit Realized (NPR)', 'Gross Margin %', 'Profitability Tier',
            'Current Counter MRP (NPR)'
        ]
        writer.writerow(headers)

        for r in records:
            tracking_label = 'Serialized (IMEI)' if r.get('is_serialized') else 'Standard Bulk'
            row = [
                r.get('sku', ''),
                r.get('name', ''),
                r.get('category_name', ''),
                r.get('brand_name', ''),
                r.get('unit_code', 'PCS'),
                tracking_label,
                f"{r.get('quantity_sold', 0):.3f}",
                f"{r.get('gross_revenue', 0):.2f}",
                f"{r.get('discounts_given', 0):.2f}",
                f"{r.get('net_revenue', 0):.2f}",
                f"{r.get('cogs_total', 0):.2f}",
                f"{r.get('gross_profit', 0):.2f}",
                f"{r.get('margin_percent', 0):.1f}%",
                r.get('tier_label', ''),
                f"{r.get('current_mrp', 0):.2f}"
            ]
            writer.writerow(sanitize_csv_row(row))

        totals_row = [
            'TOTALS', f"{totals.get('products_count', len(records))} Products", '', '', '', '',
            f"{totals.get('total_units_sold', 0):.3f}",
            f"{totals.get('total_gross_rev', 0):.2f}",
            '',
            f"{totals.get('total_net_rev', 0):.2f}",
            f"{totals.get('total_cogs_sum', 0):.2f}",
            f"{totals.get('total_profit_sum', 0):.2f}",
            f"{totals.get('overall_margin_pct', 0):.1f}%",
            '', ''
        ]
        writer.writerow(sanitize_csv_row(totals_row))

        return response

    @staticmethod
    def export_top_selling_csv(
        records: List[Dict[str, Any]],
        rank_by: str = "revenue",
        filename: Optional[str] = None
    ) -> HttpResponse:
        if not filename:
            filename = f"Top_Selling_Products_{rank_by}.csv"

        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        headers = [
            'Sales Rank', 'SKU', 'Barcode', 'Product Name', 'Category', 'Brand', 'Variant Specs',
            'Quantity Sold', 'Unit', 'Invoices Count', 'Average Realized Price (NPR)', 'Net Revenue (NPR)',
            'Landed COGS (NPR)', 'Gross Profit Realized (NPR)', 'Gross Margin %',
            'Revenue Share %', 'Active Counter Stock on Hand', 'Stock Level Status'
        ]
        writer.writerow(headers)

        for idx, r in enumerate(records, start=1):
            row = [
                idx,
                r.get('sku', ''),
                r.get('barcode', ''),
                r.get('name', ''),
                r.get('category_name', ''),
                r.get('brand_name', ''),
                r.get('variant_specs', ''),
                f"{r.get('quantity_sold', 0):.3f}",
                r.get('unit_code', 'PCS'),
                r.get('invoices_count', 0),
                f"{r.get('avg_selling_price', 0):.2f}",
                f"{r.get('net_revenue', 0):.2f}",
                f"{r.get('cogs_total', 0):.2f}",
                f"{r.get('gross_profit', 0):.2f}",
                f"{r.get('margin_percent', 0):.1f}%",
                f"{r.get('revenue_share_percent', 0):.1f}%",
                f"{r.get('current_stock_on_hand', 0):.1f}",
                r.get('stock_status', '')
            ]
            writer.writerow(sanitize_csv_row(row))

        return response

    @staticmethod
    def export_cancelled_sales_csv(
        records: List[Dict[str, Any]],
        totals: Dict[str, Any],
        filename: str = "Cancelled_Voided_Bills_Audit.csv"
    ) -> HttpResponse:
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        headers = [
            'Estimate No', 'Original Bill Date (AD)', 'Original Bill Date (BS)', 'Cancellation Timestamp',
            'Branch Code', 'Branch Name', 'Customer Name', 'Customer Phone', 'Billed Cashier', 'Sales Rep',
            'Voided / Cancelled By Supervisor', 'IP Address', 'Mandatory Cancellation Reason',
            'Gross Subtotal (NPR)', 'Discounts Voided (NPR)', 'Tax / VAT Voided (NPR)',
            'Voided Grand Total (NPR)', 'Paid Cash Reversed (NPR)', 'Due Udhaari Reversed (NPR)',
            'Stock Reversal Executed?'
        ]
        writer.writerow(headers)

        for r in records:
            row = [
                r.get('estimate_number', ''),
                r.get('bill_date_ad', ''),
                r.get('bill_date_bs', ''),
                r.get('cancel_date_ad_str', ''),
                r.get('branch_code', ''),
                r.get('branch_name', ''),
                r.get('customer_name', ''),
                r.get('customer_phone', ''),
                r.get('cashier', ''),
                r.get('salesperson', ''),
                r.get('cancelled_by', ''),
                r.get('cancel_ip', ''),
                r.get('cancellation_reason', ''),
                f"{r.get('subtotal', 0):.2f}",
                f"{r.get('total_discounts', 0):.2f}",
                f"{r.get('vat_amount', 0):.2f}",
                f"{r.get('grand_total', 0):.2f}",
                f"{r.get('paid_amount_reversed', 0):.2f}",
                f"{r.get('due_amount_reversed', 0):.2f}",
                'Yes (Confirmed Reverted)' if r.get('is_stock_reverted') else 'Pending Verification'
            ]
            writer.writerow(sanitize_csv_row(row))

        totals_row = [
            'TOTALS', '', '', '', '', '', f"{totals.get('total_voided_bills', len(records))} Voided Invoices", '', '', '', '', '', '',
            f"{totals.get('total_subtotal', 0):.2f}",
            f"{totals.get('total_discounts_voided', 0):.2f}",
            f"{totals.get('total_vat_voided', 0):.2f}",
            f"{totals.get('total_voided_amount', 0):.2f}",
            f"{totals.get('total_paid_reversed', 0):.2f}",
            f"{totals.get('total_due_reversed', 0):.2f}",
            ''
        ]
        writer.writerow(sanitize_csv_row(totals_row))

        return response

    # =========================================================================
    # 6. INVOICE-LEVEL SALES & CONCESSIONS DETAILED CSV EXPORTER
    # =========================================================================
    @staticmethod
    def export_sales_csv(queryset, filename: str = "Sales_Estimates_Detailed.csv") -> HttpResponse:
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        headers = [
            'Estimate No', 'Date (AD)', 'Date (BS)', 'Branch', 'Customer Name',
            'Customer Phone', 'Customer PAN', 'Cashier', 'Salesperson',
            'Manager Override / Approved By', 'Discount Reason', 'Gross Subtotal (NPR)',
            'Price Override Concession (NPR)', 'Item Discounts via Amount (NPR)',
            'Item Discounts via Percentage (NPR)', 'Total Item Discount (NPR)',
            'Bill Discount Type', 'Bill Discount Input Value', 'Bill Discount Amount (NPR)',
            'Bill Discount Effective %', 'Total Sales Discount (NPR)', 'Trade-In Credit (NPR)',
            'Taxable Base (NPR)', 'Non-Taxable Base (NPR)', 'Tax / VAT Amount (NPR)',
            'Grand Total (NPR)', 'Cost of Goods Sold (COGS) (NPR)',
            'Net Merchandise Revenue Excl. Tax (NPR)', 'Gross Profit Realized (NPR)',
            'Gross Margin %', 'Paid Amount (NPR)', 'Due Udhaari (NPR)',
            'Payment Status', 'Bill Status'
        ]
        writer.writerow(headers)

        sum_gross = Decimal('0.00')
        sum_price_override = Decimal('0.00')
        sum_item_disc_amount_type = Decimal('0.00')
        sum_item_disc_percent_type = Decimal('0.00')
        sum_item_disc_total = Decimal('0.00')
        sum_bill_disc = Decimal('0.00')
        sum_total_sales_disc = Decimal('0.00')
        sum_trade_in = Decimal('0.00')
        sum_taxable = Decimal('0.00')
        sum_non_taxable = Decimal('0.00')
        sum_vat = Decimal('0.00')
        sum_grand = Decimal('0.00')
        sum_cogs = Decimal('0.00')
        sum_net_rev = Decimal('0.00')
        sum_gross_profit = Decimal('0.00')
        sum_paid = Decimal('0.00')
        sum_due = Decimal('0.00')
        records_count = 0

        qs = queryset.select_related(
            'customer', 'branch', 'cashier', 'salesperson', 'manager_override_by'
        ).prefetch_related('items__product')

        for est in qs:
            records_count += 1
            gross = est.subtotal or Decimal('0.00')
            price_override = est.price_override_total
            trade_in = est.trade_in_credit
            taxable = est.taxable_amount or Decimal('0.00')
            non_taxable = est.non_taxable_amount or Decimal('0.00')
            vat = est.vat_amount or Decimal('0.00')
            grand = est.grand_total or Decimal('0.00')
            cogs = est.total_cost_amount or Decimal('0.00')

            net_rev = max(Decimal('0.00'), grand - vat + trade_in)
            gross_profit = (net_rev - cogs).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            margin_pct = (
                ((gross_profit / net_rev) * Decimal('100.00')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
                if net_rev > Decimal('0.00') else Decimal('0.0')
            )

            items = list(est.items.all())
            item_disc_amt_type = sum(
                (it.item_discount_amount for it in items if it.discount_type in ['AMOUNT', 'FIXED']),
                Decimal('0.00')
            )
            item_disc_pct_type = sum(
                (it.item_discount_amount for it in items if it.discount_type == 'PERCENTAGE' or (it.discount_type not in ['AMOUNT', 'FIXED'] and it.item_discount_amount > Decimal('0.00'))),
                Decimal('0.00')
            )
            item_disc_total = est.item_discount_total or (item_disc_amt_type + item_disc_pct_type)

            bill_disc_type_str = est.get_bill_discount_type_display() if hasattr(est, 'get_bill_discount_type_display') else est.bill_discount_type
            bill_disc_input_str = f"{est.bill_discount_input_value:.2f}" if est.bill_discount_input_value is not None else "0.00"
            bill_disc_amt = est.bill_discount_amount or Decimal('0.00')
            bill_disc_pct_str = f"{est.bill_discount_percent:.2f}%" if est.bill_discount_percent is not None else "0.00%"
            total_sales_disc = est.total_sales_discount

            sum_gross += gross
            sum_price_override += price_override
            sum_item_disc_amount_type += item_disc_amt_type
            sum_item_disc_percent_type += item_disc_pct_type
            sum_item_disc_total += item_disc_total
            sum_bill_disc += bill_disc_amt
            sum_total_sales_disc += total_sales_disc
            sum_trade_in += trade_in
            sum_taxable += taxable
            sum_non_taxable += non_taxable
            sum_vat += vat
            sum_grand += grand
            sum_cogs += cogs
            sum_net_rev += net_rev
            sum_gross_profit += gross_profit
            sum_paid += (est.paid_amount or Decimal('0.00'))
            sum_due += (est.due_amount or Decimal('0.00'))

            manager_override_str = est.manager_override_by.username if est.manager_override_by else ''
            discount_reason_str = est.discount_reason or ''

            raw_row = [
                est.estimate_number,
                est.bill_date_ad,
                est.bill_date_bs or '',
                est.branch.name,
                est.recipient_display_name,
                est.customer_phone_manual or (est.customer.phone_number if est.customer else ''),
                est.customer_pan or (est.customer.pan_number if est.customer and est.customer.pan_number else ''),
                est.cashier.username,
                est.salesperson.username if est.salesperson else est.cashier.username,
                manager_override_str,
                discount_reason_str,
                f"{gross:.2f}",
                f"{price_override:.2f}",
                f"{item_disc_amt_type:.2f}",
                f"{item_disc_pct_type:.2f}",
                f"{item_disc_total:.2f}",
                bill_disc_type_str,
                bill_disc_input_str,
                f"{bill_disc_amt:.2f}",
                bill_disc_pct_str,
                f"{total_sales_disc:.2f}",
                f"{trade_in:.2f}",
                f"{taxable:.2f}",
                f"{non_taxable:.2f}",
                f"{vat:.2f}",
                f"{grand:.2f}",
                f"{cogs:.2f}",
                f"{net_rev:.2f}",
                f"{gross_profit:.2f}",
                f"{margin_pct:.1f}%",
                f"{est.paid_amount:.2f}",
                f"{est.due_amount:.2f}",
                est.get_payment_status_display(),
                est.get_status_display()
            ]
            writer.writerow(sanitize_csv_row(raw_row))

        if records_count > 0:
            overall_margin_pct = (
                ((sum_gross_profit / sum_net_rev) * Decimal('100.00')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
                if sum_net_rev > Decimal('0.00') else Decimal('0.0')
            )
            totals_row = [
                'TOTALS', '', '', '', f"{records_count} Bills", '', '', '', '', '', '',
                f"{sum_gross:.2f}", f"{sum_price_override:.2f}",
                f"{sum_item_disc_amount_type:.2f}", f"{sum_item_disc_percent_type:.2f}",
                f"{sum_item_disc_total:.2f}", '', '', f"{sum_bill_disc:.2f}", '',
                f"{sum_total_sales_disc:.2f}", f"{sum_trade_in:.2f}", f"{sum_taxable:.2f}",
                f"{sum_non_taxable:.2f}", f"{sum_vat:.2f}", f"{sum_grand:.2f}",
                f"{sum_cogs:.2f}", f"{sum_net_rev:.2f}", f"{sum_gross_profit:.2f}",
                f"{overall_margin_pct:.1f}%", f"{sum_paid:.2f}", f"{sum_due:.2f}", '', ''
            ]
            writer.writerow(sanitize_csv_row(totals_row))

        return response

    # =========================================================================
    # 7. LINE-ITEM LEVEL DETAILED SALES LINES CSV EXPORTER
    # =========================================================================
    @staticmethod
    def export_sales_items_csv(queryset, filename: str = "Sales_Itemized_Lines.csv") -> HttpResponse:
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        headers = [
            'Estimate No', 'Date (AD)', 'Date (BS)', 'Branch', 'Customer Name',
            'Customer Phone', 'Customer PAN', 'Cashier', 'Salesperson', 'Product Name',
            'SKU', 'Barcode', 'IMEI 1 / Serial', 'IMEI 2', 'Quantity', 'Base Unit',
            'Official Unit Price (NPR)', 'Unit Selling Rate (NPR)', 'Line Gross Subtotal (NPR)',
            'Price Override Concession (NPR)', 'Item Discount Type', 'Discount Input Value',
            'Actual Item Discount Amount (NPR)', 'Effective Discount Percentage (%)',
            'Allocated Bill Discount (NPR)', 'Total Line Discount Concession (NPR)',
            'Tax Mode', 'Tax Rate (%)', 'Line Tax / VAT (NPR)', 'Net Line Total (NPR)',
            'Unit Cost Price (NPR)', 'Total Cost (COGS) (NPR)', 'Line Gross Profit (NPR)',
            'Line Gross Margin %', 'Warranty Terms', 'Manager Override / Approved By',
            'Commercial Discount Reason'
        ]
        writer.writerow(headers)

        if hasattr(queryset, 'model') and queryset.model == SalesEstimateItem:
            items_qs = queryset
        else:
            items_qs = SalesEstimateItem.objects.filter(estimate__in=queryset)

        items_qs = items_qs.select_related(
            'estimate', 'estimate__branch', 'estimate__customer',
            'estimate__cashier', 'estimate__salesperson', 'estimate__manager_override_by',
            'product', 'product__base_unit', 'item_instance'
        ).order_by('-estimate__bill_date_ad', 'estimate__estimate_number', 'id')

        sum_qty = Decimal('0.000')
        sum_gross = Decimal('0.00')
        sum_price_override = Decimal('0.00')
        sum_item_disc = Decimal('0.00')
        sum_alloc_bill_disc = Decimal('0.00')
        sum_total_disc = Decimal('0.00')
        sum_tax = Decimal('0.00')
        sum_net_total = Decimal('0.00')
        sum_cogs = Decimal('0.00')
        sum_profit = Decimal('0.00')
        records_count = 0

        for it in items_qs:
            records_count += 1
            est = it.estimate
            qty = it.quantity or Decimal('0.000')
            unit_price = it.unit_price or Decimal('0.00')
            official_price = it.official_unit_price or unit_price
            line_gross = (qty * unit_price).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            override_amt = it.price_override_amount or Decimal('0.00')

            raw_dtype = it.discount_type or 'NONE'
            if raw_dtype in ['AMOUNT', 'FIXED']:
                norm_dtype = 'AMOUNT'
            elif raw_dtype == 'PERCENTAGE':
                norm_dtype = 'PERCENTAGE'
            else:
                norm_dtype = 'NONE'

            disc_input_val = it.discount_input_value if it.discount_input_value is not None else Decimal('0.00')
            actual_item_disc = it.item_discount_amount if it.item_discount_amount is not None else Decimal('0.00')
            effective_disc_pct = it.effective_discount_percent if it.effective_discount_percent is not None else (it.discount_percent or Decimal('0.00'))
            alloc_bill_disc = it.allocated_bill_discount_amount or Decimal('0.00')
            total_line_disc = it.discount_amount or (actual_item_disc + alloc_bill_disc)

            line_tax = it.tax_amount or Decimal('0.00')
            net_total = it.line_total or Decimal('0.00')
            unit_cost = it.cost_price or Decimal('0.00')
            total_cogs = (unit_cost * it.base_unit_quantity).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            line_profit = it.line_gross_profit
            net_tax_base = max(Decimal('0.00'), net_total - line_tax)
            line_margin_pct = (
                ((line_profit / net_tax_base) * Decimal('100.00')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
                if net_tax_base > Decimal('0.00') else Decimal('0.0')
            )

            sum_qty += qty
            sum_gross += line_gross
            sum_price_override += override_amt
            sum_item_disc += actual_item_disc
            sum_alloc_bill_disc += alloc_bill_disc
            sum_total_disc += total_line_disc
            sum_tax += line_tax
            sum_net_total += net_total
            sum_cogs += total_cogs
            sum_profit += line_profit

            manager_override_str = est.manager_override_by.username if est.manager_override_by else ''
            discount_reason_str = est.discount_reason or ''

            raw_row = [
                est.estimate_number,
                est.bill_date_ad,
                est.bill_date_bs or '',
                est.branch.name,
                est.recipient_display_name,
                est.customer_phone_manual or (est.customer.phone_number if est.customer else ''),
                est.customer_pan or (est.customer.pan_number if est.customer and est.customer.pan_number else ''),
                est.cashier.username,
                est.salesperson.username if est.salesperson else est.cashier.username,
                it.product.name,
                it.product.sku,
                it.product.barcode or '',
                it.imei_number or it.serial_number or '',
                it.secondary_imei or '',
                f"{qty:.3f}",
                it.product.base_unit.code if it.product.base_unit else 'PCS',
                f"{official_price:.2f}",
                f"{unit_price:.2f}",
                f"{line_gross:.2f}",
                f"{override_amt:.2f}",
                norm_dtype,
                f"{disc_input_val:.2f}",
                f"{actual_item_disc:.2f}",
                f"{effective_disc_pct:.2f}%",
                f"{alloc_bill_disc:.2f}",
                f"{total_line_disc:.2f}",
                it.get_tax_pricing_type_display() if hasattr(it, 'get_tax_pricing_type_display') else it.tax_pricing_type,
                f"{it.vat_rate:.2f}",
                f"{line_tax:.2f}",
                f"{net_total:.2f}",
                f"{unit_cost:.2f}",
                f"{total_cogs:.2f}",
                f"{line_profit:.2f}",
                f"{line_margin_pct:.1f}%",
                it.warranty_terms or '',
                manager_override_str,
                discount_reason_str
            ]
            writer.writerow(sanitize_csv_row(raw_row))

        if records_count > 0:
            net_revenue_total = sum_net_total - sum_tax
            overall_margin_pct = (
                ((sum_profit / net_revenue_total) * Decimal('100.00')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
                if net_revenue_total > Decimal('0.00') else Decimal('0.0')
            )
            totals_row = [
                'TOTALS', '', '', '', f"{records_count} Lines", '', '', '', '', '', '', '', '', '',
                f"{sum_qty:.3f}", '', '', '', f"{sum_gross:.2f}", f"{sum_price_override:.2f}",
                '', '', f"{sum_item_disc:.2f}", '', f"{sum_alloc_bill_disc:.2f}", f"{sum_total_disc:.2f}",
                '', '', f"{sum_tax:.2f}", f"{sum_net_total:.2f}", '', f"{sum_cogs:.2f}",
                f"{sum_profit:.2f}", f"{overall_margin_pct:.1f}%", '', '', ''
            ]
            writer.writerow(sanitize_csv_row(totals_row))

        return response

    # =========================================================================
    # 8. INVENTORY VALUATION, CUSTOMER UDHAARI & PRICE HISTORY EXPORTERS
    # =========================================================================
    @staticmethod
    def export_stock_valuation_csv(branch=None, filename: str = "Stock_Valuation_Accurate.csv") -> HttpResponse:
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        headers = [
            'SKU', 'Barcode', 'Product / Handset Name', 'Category', 'Brand',
            'Branch', 'Stock Qty', 'Unit', 'Tracking Type', 'Tax Mode',
            'Active Selling MRP (NPR)', 'Total Inward Landed Cost Valuation (NPR)',
            'Total Retail Valuation (NPR)', 'Projected Realizable Profit (NPR)', 'Projected Margin %'
        ]
        writer.writerow(headers)

        stocks = BranchStock.objects.select_related(
            'product', 'product__category', 'product__brand', 'product__base_unit', 'branch'
        ).all()

        if branch:
            stocks = stocks.filter(branch=branch)

        for s in stocks:
            prod = s.product
            qty = s.quantity

            if prod.requires_imei_tracking or prod.requires_serial_tracking:
                unsold_instances = ItemInstance.objects.filter(product=prod, status='IN_STOCK')
                if branch:
                    unsold_instances = unsold_instances.filter(branch=branch)

                item_cost_sum = unsold_instances.aggregate(sum_cost=Sum('landed_cost'))['sum_cost'] or Decimal('0.00')
                instance_count = unsold_instances.count()
                if qty > instance_count:
                    item_cost_sum += ((qty - instance_count) * prod.purchase_price)

                cost_val = item_cost_sum
                retail_val = (qty * prod.selling_price).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                tracking_str = "IMEI Serialized"
            else:
                active_batches = ProductBatch.objects.filter(product=prod, is_depleted=False)
                if branch:
                    active_batches = active_batches.filter(branch=branch)

                batch_cost_sum = Decimal('0.00')
                for b in active_batches:
                    batch_cost_sum += (b.quantity_remaining * b.cost_price)

                cost_val = batch_cost_sum if batch_cost_sum > Decimal('0.00') else (qty * prod.purchase_price).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                retail_val = (qty * prod.selling_price).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                tracking_str = "Standard FIFO Batch"

            projected_profit = retail_val - cost_val
            margin_pct = (
                ((projected_profit / retail_val) * Decimal('100.00')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
                if retail_val > Decimal('0.00') else Decimal('0.0')
            )

            raw_row = [
                prod.sku, prod.barcode, prod.name,
                prod.category.name if prod.category else '',
                prod.brand.name if prod.brand else '',
                s.branch.name, f"{qty:.3f}",
                prod.base_unit.code if prod.base_unit else 'PCS',
                tracking_str,
                prod.get_tax_pricing_type_display() if hasattr(prod, 'get_tax_pricing_type_display') else prod.tax_pricing_type,
                f"{prod.selling_price:.2f}", f"{cost_val:.2f}", f"{retail_val:.2f}",
                f"{projected_profit:.2f}", f"{margin_pct:.1f}%"
            ]
            writer.writerow(sanitize_csv_row(raw_row))

        return response

    @staticmethod
    def export_customer_udhaari_csv(filename: str = "Customer_Udhaari_Book.csv") -> HttpResponse:
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        headers = [
            'Customer Name', 'Mobile Number', 'PAN Number', 'Customer Type',
            'Credit Limit (NPR)', 'Outstanding Udhaari Balance (NPR)',
            'Lifetime Spent (NPR)', 'Status'
        ]
        writer.writerow(headers)

        customers = Customer.objects.filter(current_credit_balance__gt=Decimal('0.00')).order_by('-current_credit_balance')
        for c in customers:
            raw_row = [
                c.name, c.phone_number, c.pan_number or '', c.get_customer_type_display(),
                f"{c.credit_limit:.2f}", f"{c.current_credit_balance:.2f}", f"{c.total_spent:.2f}",
                'Over Limit' if c.credit_limit > 0 and c.current_credit_balance > c.credit_limit else 'Active Due'
            ]
            writer.writerow(sanitize_csv_row(raw_row))

        return response

    @staticmethod
    def export_price_history_csv(filename: str = "Price_Fluctuation_History.csv") -> HttpResponse:
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        headers = [
            'Effective Date', 'Product Name', 'SKU', 'Old Cost Price (NPR)',
            'New Cost Price (NPR)', 'Cost Delta (NPR)', 'Old Selling MRP (NPR)',
            'New Selling MRP (NPR)', 'Source Reference', 'Changed By', 'Remarks'
        ]
        writer.writerow(headers)

        history = ProductCostHistory.objects.select_related('product', 'changed_by').all().order_by('-date_effective', '-created_at')
        for h in history:
            cost_diff = h.new_cost_price - h.old_cost_price
            raw_row = [
                h.date_effective, h.product.name, h.product.sku,
                f"{h.old_cost_price:.2f}", f"{h.new_cost_price:.2f}", f"{cost_diff:+.2f}",
                f"{h.old_selling_price:.2f}", f"{h.new_selling_price:.2f}",
                h.source_reference or '', h.changed_by.username if h.changed_by else 'System',
                h.remarks or ''
            ]
            writer.writerow(sanitize_csv_row(raw_row))

        return response