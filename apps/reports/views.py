"""
Business Analytics, Valuation, Audit & Reporting Views.

Capabilities:
1. Core Inventory & Ledgers:
   - StockSummaryReportView (21-Column Historical Ledger)
   - StockDetailReportView (10-Column Bin Card)
   - CurrentStockReportView (Live Warehouse Snapshot)
   - LowStockReportView & OutOfStockReportView
   - InventoryValuationReportView
2. Serialized Handset & IMEI Intelligence:
   - IMEIStockReportView (Live IMEI 1/2 Registry & NTA MDMS)
   - IMEILifecycleReportView (Forensic Single-IMEI Trace)
   - TradeInInventoryReportView (Pre-Owned Handset Margins)
3. Movements, Logistics, Quality & RMA:
   - StockTransferReportView (Inter-Branch Transfers)
   - StockAdjustmentReportView (Shrinkage & Count Corrections)
   - DamagedStockReportView (Quarantined Defective Parts)
   - VendorRMAReportView (Distributor Warranty Claims)
4. Aging & Velocity:
   - StockAgingReportView (4-Bracket Aging)
   - SlowMovingStockReportView (Dormant Shelf Analysis)
   - BatchStockReportView (FIFO Inward Batches)
5. Purchase Domain Suite:
   - PurchaseRegisterReportView (Commercial Purchase Book)
   - SupplierPurchaseReportView (Supplier Turnover & Spend)
   - ProductPurchaseReportView (Product-Wise Rates & Inward)
   - SupplierOutstandingReportView (Supplier Udhaari Payables)
   - CancelledPurchasesReportView (Forensic Audit of Voided Inward GRNs - Part B)
6. Sales, Turnover & Commercial Intelligence Suite:
   - SalesSummaryReportView (Date-Wise Turnover & Collections)
   - ItemWiseSalesReportView (Line-Item Breakdown & Deductions)
   - ProductWiseSalesReportView (Product Volume & Unit Margins)
   - BrandWiseSalesReportView (Brand Turnover & Share %)
   - CategoryWiseSalesReportView (Departmental Revenue Breakdown)
   - SoldIMEIRegistryReportView (Sold Handsets & Active Warranties)
   - SalespersonSalesReportView (Counter Staff Volume & Performance)
   - CashierSalesReportView (Tender Reconciliation & Collections)
   - PaymentMethodSalesReportView (Cash, FonePay, eSewa, Card Splits)
   - DiscountOverrideReportView (Supervisor Concession & Price Override Audit)
   - TradeInExchangeSalesReportView (Old Phone Exchange Deal Settlement)
   - GrossProfitMarginReportView (4-Tier Profitability Breakdown)
   - TopSellingSalesReportView (Top Performers by Volume & Revenue)
   - SlowSellingSalesReportView (Low Sales Velocity & Capital Stagnation)
   - CancelledSalesAuditReportView (Anti-Fraud Voided Invoices Audit - Part A)
7. Financials, Audit Logs & Universal CSV Exporters
"""

import io
import csv
from datetime import date, datetime, timedelta, time
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Any, List, Optional

from django.shortcuts import render, redirect, get_object_or_404
from django.views.generic import TemplateView, View, ListView
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.db.models import (
    Sum, Count, F, Q, DecimalField, Value, ExpressionWrapper, Avg, Case, When, Max, Min
)
from django.db.models.functions import Coalesce
from django.core.paginator import Paginator
from django.contrib import messages
from django.http import HttpResponse, JsonResponse
from django.utils import timezone

from apps.sales.models import (
    SalesEstimate, SalesEstimateItem, SalesPaymentTransaction, SalesReturn, SalesReturnItem, PhoneExchangeTradeIn
)
from apps.inventory.models import (
    BranchStock, Product, ProductCategory, ProductSubCategory, Brand,
    UnitOfMeasurement, ProductBatch, ItemInstance, StockMovementLog,
    VendorRMAClaim, VendorRMAClaimItem, DeviceComponentWarranty
)
from apps.branches.models import Branch, StockTransferRequest, StockTransferItem
from apps.customers.models import Customer
from apps.purchases.models import Supplier, GoodsReceivedNote, GRNItem, PurchaseReturn
from apps.repairs.models import RepairTicket, RepairReplacedPart
from apps.reports.models import (
    InventoryValuationSnapshot, ProductCostHistory, ScheduledReportLog
)
from apps.reports.exports import CSVExportEngine, sanitize_csv_row

# -----------------------------------------------------------------------------
# DOMAIN SERVICES IMPORT
# -----------------------------------------------------------------------------
# 1. Inventory, Movements & Warehouse Services
from apps.reports.services.stock_summary_service import StockSummaryService
from apps.reports.services.stock_detail_service import StockDetailService
from apps.reports.services.current_stock_service import CurrentStockService
from apps.reports.services.low_stock_service import LowStockService
from apps.reports.services.out_of_stock_service import OutOfStockService
from apps.reports.services.imei_stock_service import IMEIStockService
from apps.reports.services.imei_lifecycle_service import IMEILifecycleService
from apps.reports.services.stock_transfer_report_service import StockTransferReportService
from apps.reports.services.stock_adjustment_service import StockAdjustmentService
from apps.reports.services.damaged_stock_service import DamagedStockService
from apps.reports.services.vendor_rma_report_service import VendorRMAReportService
from apps.reports.services.stock_aging_service import StockAgingService
from apps.reports.services.slow_moving_service import SlowMovingStockService
from apps.reports.services.trade_in_inventory_service import TradeInInventoryService
from apps.reports.services.batch_stock_service import BatchStockService

# 2. Procurement Services
from apps.reports.services.purchase_register_service import PurchaseRegisterService
from apps.reports.services.supplier_purchase_service import SupplierPurchaseService
from apps.reports.services.product_purchase_service import ProductPurchaseService
from apps.reports.services.supplier_outstanding_service import SupplierOutstandingService
from apps.reports.services.cancelled_purchase_service import CancelledPurchaseService

# 3. Sales & Commercial Intelligence Services
from apps.reports.services.sales_summary_service import SalesSummaryService
from apps.reports.services.item_sales_service import ItemSalesService
from apps.reports.services.product_sales_service import ProductSalesService
from apps.reports.services.brand_sales_service import BrandSalesService
from apps.reports.services.category_sales_service import CategorySalesService
from apps.reports.services.imei_sales_service import IMEISalesService
from apps.reports.services.salesperson_sales_service import SalespersonSalesService
from apps.reports.services.cashier_sales_service import CashierSalesService
from apps.reports.services.payment_method_sales_service import PaymentMethodSalesService
from apps.reports.services.discount_override_sales_service import DiscountOverrideSalesService
from apps.reports.services.trade_in_sales_service import TradeInSalesService
from apps.reports.services.gross_profit_sales_service import GrossProfitSalesService
from apps.reports.services.top_slow_sales_service import TopSlowSalesService
from apps.reports.services.cancelled_sales_service import CancelledSalesService

from apps.core.models import SystemConfiguration, AuditLog
from apps.core.nepali_calendar import NepaliCalendar
from apps.core.utils.nepali_date_converter import ad_to_bs_string
from apps.users.models import User


# ==============================================================================
# BASE PERMISSION MIXIN FOR REPORTS
# ==============================================================================
class ReportAccessMixin(LoginRequiredMixin, UserPassesTestMixin):
    """
    Enforces role-based security across all reporting, inventory audit, and valuation screens.
    Strictly restricts access to Superusers, Shop Owners, Branch Managers, and Accountants.
    """
    def test_func(self):
        user = self.request.user
        return user.is_authenticated and (
            user.is_superuser or getattr(user, 'role', '') in ['OWNER', 'MANAGER', 'ACCOUNTANT']
        )

    def handle_no_permission(self):
        messages.error(
            self.request,
            "Permission Denied: Access to business intelligence, inventory audits, and financial reports is restricted to Store Owners, Managers, and Accountants."
        )
        return redirect('core:dashboard')


# ==============================================================================
# SALES REPORT 1: SALES SUMMARY REPORT (DATE-WISE ROLLUP)
# ==============================================================================
class SalesSummaryReportView(ReportAccessMixin, View):
    """
    Date-wise sales rollup report reconciling invoices, units, gross billing,
    item discounts, bill discounts, net turnover, COGS, gross profit, and tender breakdowns.
    """
    template_name = 'reports/sales_summary.html'

    def get(self, request, *args, **kwargs):
        active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()

        filters = {
            'start_date': request.GET.get('start_date', ''),
            'end_date': request.GET.get('end_date', ''),
            'branch_id': request.GET.get('branch', '') or (active_branch.id if not request.user.is_superuser else ''),
            'payment_status': request.GET.get('payment_status', '').strip(),
        }

        data = SalesSummaryService.get_sales_summary_data(filters=filters, user=request.user)
        records = data['records']
        totals = data['totals']

        export_mode = request.GET.get('export', '').strip().lower()
        if export_mode == 'csv':
            response = HttpResponse(content_type='text/csv; charset=utf-8')
            response['Content-Disposition'] = f'attachment; filename="Sales_Summary_{data["start_date"]}_{data["end_date"]}.csv"'
            writer = csv.writer(response)
            writer.writerow([
                'Date (AD)', 'Date (BS)', 'Invoices', 'Units Sold', 'Gross Subtotal',
                'Item Discounts', 'Bill Discounts', 'Total Discounts', 'Trade-In Credit',
                'Taxable Base', 'Non-Taxable Base', 'VAT', 'Net Grand Total', 'COGS',
                'Gross Profit', 'Margin %', 'Paid Amount', 'Due Udhaari',
                'Cash', 'FonePay', 'eSewa', 'Khalti', 'Card', 'Bank', 'Credit'
            ])
            for r in records:
                writer.writerow(sanitize_csv_row([
                    r['date_ad_str'], r['date_bs'], r['invoice_count'], f"{r['units_sold']:.1f}",
                    f"{r['gross_subtotal']:.2f}", f"{r['item_discount_sum']:.2f}", f"{r['bill_discount_sum']:.2f}",
                    f"{r['total_discount_sum']:.2f}", f"{r['trade_in_credit_sum']:.2f}", f"{r['taxable_amount']:.2f}",
                    f"{r['non_taxable_amount']:.2f}", f"{r['vat_amount']:.2f}", f"{r['grand_total']:.2f}",
                    f"{r['cogs_amount']:.2f}", f"{r['gross_profit']:.2f}", f"{r['margin_percent']:.1f}%",
                    f"{r['paid_amount']:.2f}", f"{r['due_amount']:.2f}", f"{r['cash_collected']:.2f}",
                    f"{r['fonepay_collected']:.2f}", f"{r['esewa_collected']:.2f}", f"{r['khalti_collected']:.2f}",
                    f"{r['card_collected']:.2f}", f"{r['bank_collected']:.2f}", f"{r['credit_authorized']:.2f}"
                ]))
            writer.writerow(sanitize_csv_row([
                'TOTALS', '', totals.get('total_invoices', 0), f"{totals.get('total_units_sold', 0):.1f}",
                f"{totals.get('total_gross', 0):.2f}", f"{totals.get('total_item_disc', 0):.2f}",
                f"{totals.get('total_bill_disc', 0):.2f}", f"{totals.get('total_sales_disc', 0):.2f}",
                f"{totals.get('total_trade_in', 0):.2f}", f"{totals.get('total_taxable', 0):.2f}",
                f"{totals.get('total_non_taxable', 0):.2f}", f"{totals.get('total_vat', 0):.2f}",
                f"{totals.get('total_net_turnover', 0):.2f}", f"{totals.get('total_cogs', 0):.2f}",
                f"{totals.get('total_profit', 0):.2f}", f"{totals.get('overall_margin_pct', 0):.1f}%",
                f"{totals.get('total_paid', 0):.2f}", f"{totals.get('total_due', 0):.2f}",
                f"{totals.get('total_cash', 0):.2f}", f"{totals.get('total_fonepay', 0):.2f}",
                f"{totals.get('total_esewa', 0):.2f}", f"{totals.get('total_khalti', 0):.2f}",
                f"{totals.get('total_card', 0):.2f}", f"{totals.get('total_bank', 0):.2f}",
                f"{totals.get('total_credit', 0):.2f}"
            ]))
            return response

        paginator = Paginator(records, 50)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        context = {
            'records': page_obj,
            'page_obj': page_obj,
            'is_paginated': page_obj.has_other_pages(),
            'totals': totals,
            'branches': data['branches'],
            'selected_branch': data['selected_branch'],
            'filters': filters,
            'start_date': data['start_date'],
            'end_date': data['end_date'],
            'start_date_bs': data['start_date_bs'],
            'end_date_bs': data['end_date_bs'],
        }
        return render(request, self.template_name, context)


# ==============================================================================
# SALES REPORT 2: ITEM-WISE DETAILED SALES REPORT
# ==============================================================================
class ItemWiseSalesReportView(ReportAccessMixin, View):
    """
    Detailed line-item sales report exposing pure item discounts (AMOUNT / PERCENTAGE),
    allocated bill discounts, price override concessions, COGS, and unit margins.
    """
    template_name = 'reports/item_sales.html'

    def get(self, request, *args, **kwargs):
        active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()

        filters = {
            'start_date': request.GET.get('start_date', ''),
            'end_date': request.GET.get('end_date', ''),
            'branch_id': request.GET.get('branch', '') or (active_branch.id if not request.user.is_superuser else ''),
            'category_id': request.GET.get('category', ''),
            'brand_id': request.GET.get('brand', ''),
            'salesperson_id': request.GET.get('salesperson', ''),
            'cashier_id': request.GET.get('cashier', ''),
            'discount_filter': request.GET.get('discount_filter', '').strip(),
            'serialized_only': request.GET.get('serialized_only', '').strip(),
            'q': request.GET.get('q', '').strip(),
        }

        data = ItemSalesService.get_item_sales_data(filters=filters, user=request.user)
        records = data['records']
        totals = data['totals']

        export_mode = request.GET.get('export', '').strip().lower()
        if export_mode == 'csv':
            response = HttpResponse(content_type='text/csv; charset=utf-8')
            response['Content-Disposition'] = f'attachment; filename="Item_Wise_Sales_{data["start_date"]}_{data["end_date"]}.csv"'
            writer = csv.writer(response)
            writer.writerow([
                'Estimate No', 'Date (AD)', 'Date (BS)', 'Branch', 'Customer', 'Product',
                'SKU', 'Barcode', 'IMEI 1', 'IMEI 2', 'Qty', 'Unit', 'Official Price',
                'Selling Price', 'Price Override', 'Disc Type', 'Disc Input', 'Item Discount',
                'Effective %', 'Allocated Bill Disc', 'Total Line Disc', 'VAT', 'Net Total',
                'Unit Cost', 'COGS', 'Gross Profit', 'Margin %', 'Cashier', 'Salesperson', 'Reason'
            ])
            for r in records:
                writer.writerow(sanitize_csv_row([
                    r['estimate_number'], r['date_ad_str'], r['date_bs'], r['branch_code'],
                    r['customer_name'], r['product_name'], r['sku'], r['barcode'],
                    r['imei_number'], r['secondary_imei'], f"{r['quantity']:.1f}", r['unit_code'],
                    f"{r['official_unit_price']:.2f}", f"{r['unit_price']:.2f}", f"{r['price_override_amount']:.2f}",
                    r['discount_type'], f"{r['discount_input_value']:.2f}", f"{r['item_discount_amount']:.2f}",
                    f"{r['effective_discount_percent']:.1f}%", f"{r['allocated_bill_discount']:.2f}",
                    f"{r['total_line_discount']:.2f}", f"{r['tax_amount']:.2f}", f"{r['line_total']:.2f}",
                    f"{r['unit_cost']:.2f}", f"{r['line_cogs']:.2f}", f"{r['line_profit']:.2f}",
                    f"{r['line_margin_percent']:.1f}%", r['cashier_username'], r['salesperson_username'], r['discount_reason']
                ]))
            writer.writerow(sanitize_csv_row([
                'TOTALS', '', '', '', '', f"{totals.get('records_count', 0)} Lines", '', '', '', '',
                f"{totals.get('total_units', 0):.1f}", '', '', f"{totals.get('total_gross_subtotal', 0):.2f}",
                f"{totals.get('total_price_overrides', 0):.2f}", '', '', f"{totals.get('total_item_discounts', 0):.2f}",
                '', f"{totals.get('total_bill_discounts', 0):.2f}", f"{totals.get('total_concessions', 0):.2f}",
                f"{totals.get('total_vat', 0):.2f}", f"{totals.get('total_net', 0):.2f}", '',
                f"{totals.get('total_cogs', 0):.2f}", f"{totals.get('total_profit', 0):.2f}",
                f"{totals.get('overall_margin_pct', 0):.1f}%", '', '', ''
            ]))
            return response

        paginator = Paginator(records, 50)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        context = {
            'records': page_obj,
            'page_obj': page_obj,
            'is_paginated': page_obj.has_other_pages(),
            'totals': totals,
            'branches': data['branches'],
            'categories': data['categories'],
            'brands': data['brands'],
            'staff_members': data['staff_members'],
            'selected_branch': data['selected_branch'],
            'filters': filters,
            'start_date': data['start_date'],
            'end_date': data['end_date'],
            'start_date_bs': data['start_date_bs'],
            'end_date_bs': data['end_date_bs'],
        }
        return render(request, self.template_name, context)


# ==============================================================================
# SALES REPORT 3: PRODUCT-WISE SALES REPORT
# ==============================================================================
class ProductWiseSalesReportView(ReportAccessMixin, View):
    """Product-level volume, net revenue, margins, and on-hand stock report."""
    template_name = 'reports/product_sales.html'

    def get(self, request, *args, **kwargs):
        active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()

        filters = {
            'start_date': request.GET.get('start_date', ''),
            'end_date': request.GET.get('end_date', ''),
            'branch_id': request.GET.get('branch', '') or (active_branch.id if not request.user.is_superuser else ''),
            'category_id': request.GET.get('category', ''),
            'brand_id': request.GET.get('brand', ''),
            'tracking_type': request.GET.get('tracking_type', '').strip(),
            'q': request.GET.get('q', '').strip(),
        }

        data = ProductSalesService.get_product_sales_data(filters=filters, user=request.user)
        records = data['records']
        totals = data['totals']

        export_mode = request.GET.get('export', '').strip().lower()
        if export_mode == 'csv':
            response = HttpResponse(content_type='text/csv; charset=utf-8')
            response['Content-Disposition'] = f'attachment; filename="Product_Sales_{data["start_date"]}_{data["end_date"]}.csv"'
            writer = csv.writer(response)
            writer.writerow([
                'SKU', 'Barcode', 'Product Name', 'Category', 'Brand', 'Specs',
                'Type', 'Invoices', 'Qty Sold', 'Unit', 'Avg Price', 'Gross Rev',
                'Discounts', 'Net Rev', 'COGS', 'Gross Profit', 'Margin %', 'Stock On Hand'
            ])
            for r in records:
                writer.writerow(sanitize_csv_row([
                    r['sku'], r['barcode'], r['name'], r['category_name'], r['brand_name'],
                    r['variant_specs'], 'IMEI' if r['is_serialized'] else 'Bulk',
                    r['invoices_count'], f"{r['quantity_sold']:.1f}", r['unit_code'],
                    f"{r['avg_realized_price']:.2f}", f"{r['gross_revenue']:.2f}",
                    f"{r['total_discounts']:.2f}", f"{r['net_revenue']:.2f}",
                    f"{r['cogs_total']:.2f}", f"{r['gross_profit']:.2f}",
                    f"{r['margin_percent']:.1f}%", f"{r['available_stock']:.1f}"
                ]))
            writer.writerow(sanitize_csv_row([
                'TOTALS', '', f"{totals.get('products_count', 0)} Products", '', '', '', '', '',
                f"{totals.get('total_units_sold', 0):.1f}", '', '', f"{totals.get('total_gross_rev', 0):.2f}",
                f"{totals.get('total_disc_deducted', 0):.2f}", f"{totals.get('total_net_rev', 0):.2f}",
                f"{totals.get('total_cogs_sum', 0):.2f}", f"{totals.get('total_profit_sum', 0):.2f}",
                f"{totals.get('overall_margin_pct', 0):.1f}%", ''
            ]))
            return response

        paginator = Paginator(records, 50)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        context = {
            'records': page_obj,
            'page_obj': page_obj,
            'is_paginated': page_obj.has_other_pages(),
            'totals': totals,
            'branches': data['branches'],
            'categories': data['categories'],
            'brands': data['brands'],
            'selected_branch': data['selected_branch'],
            'filters': filters,
            'start_date': data['start_date'],
            'end_date': data['end_date'],
            'start_date_bs': data['start_date_bs'],
            'end_date_bs': data['end_date_bs'],
        }
        return render(request, self.template_name, context)


# ==============================================================================
# SALES REPORT 4: BRAND-WISE SALES REPORT
# ==============================================================================
class BrandWiseSalesReportView(ReportAccessMixin, View):
    """Turnover, volume (handset vs accessory), and revenue share % by Brand."""
    template_name = 'reports/brand_sales.html'

    def get(self, request, *args, **kwargs):
        active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()

        filters = {
            'start_date': request.GET.get('start_date', ''),
            'end_date': request.GET.get('end_date', ''),
            'branch_id': request.GET.get('branch', '') or (active_branch.id if not request.user.is_superuser else ''),
            'brand_id': request.GET.get('brand', ''),
            'q': request.GET.get('q', '').strip(),
        }

        data = BrandSalesService.get_brand_sales_data(filters=filters, user=request.user)
        records = data['records']
        totals = data['totals']

        export_mode = request.GET.get('export', '').strip().lower()
        if export_mode == 'csv':
            response = HttpResponse(content_type='text/csv; charset=utf-8')
            response['Content-Disposition'] = f'attachment; filename="Brand_Sales_{data["start_date"]}_{data["end_date"]}.csv"'
            writer = csv.writer(response)
            writer.writerow([
                'Brand Name', 'Invoices', 'Handsets Sold', 'Accessories Sold', 'Total Units Sold',
                'Gross Sales', 'Discounts', 'Net Turnover', 'COGS', 'Gross Profit', 'Margin %', 'Share %'
            ])
            for r in records:
                writer.writerow(sanitize_csv_row([
                    r['brand_name'], r['invoices_count'], f"{r['handset_units_sold']:.1f}",
                    f"{r['accessory_units_sold']:.1f}", f"{r['total_units_sold']:.1f}",
                    f"{r['gross_sales']:.2f}", f"{r['total_discounts']:.2f}", f"{r['net_turnover']:.2f}",
                    f"{r['total_cogs']:.2f}", f"{r['gross_profit']:.2f}", f"{r['margin_percent']:.1f}%",
                    f"{r['turnover_share_percent']:.1f}%"
                ]))
            writer.writerow(sanitize_csv_row([
                'TOTALS', '', f"{totals.get('total_handsets', 0):.1f}", f"{totals.get('total_accessories', 0):.1f}",
                f"{totals.get('total_units_sold', 0):.1f}", f"{totals.get('total_gross', 0):.2f}",
                f"{totals.get('total_discounts', 0):.2f}", f"{totals.get('total_net', 0):.2f}",
                f"{totals.get('total_cogs', 0):.2f}", f"{totals.get('total_profit', 0):.2f}",
                f"{totals.get('overall_margin_pct', 0):.1f}%", '100.0%'
            ]))
            return response

        paginator = Paginator(records, 50)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        context = {
            'records': page_obj,
            'page_obj': page_obj,
            'is_paginated': page_obj.has_other_pages(),
            'totals': totals,
            'branches': data['branches'],
            'brands': data['brands'],
            'selected_branch': data['selected_branch'],
            'filters': filters,
            'start_date': data['start_date'],
            'end_date': data['end_date'],
            'start_date_bs': data['start_date_bs'],
            'end_date_bs': data['end_date_bs'],
        }
        return render(request, self.template_name, context)


# ==============================================================================
# SALES REPORT 5: CATEGORY-WISE SALES REPORT
# ==============================================================================
class CategoryWiseSalesReportView(ReportAccessMixin, View):
    """Departmental turnover, lines count, COGS, and contribution % by Category."""
    template_name = 'reports/category_sales.html'

    def get(self, request, *args, **kwargs):
        active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()

        filters = {
            'start_date': request.GET.get('start_date', ''),
            'end_date': request.GET.get('end_date', ''),
            'branch_id': request.GET.get('branch', '') or (active_branch.id if not request.user.is_superuser else ''),
            'category_id': request.GET.get('category', ''),
            'q': request.GET.get('q', '').strip(),
        }

        data = CategorySalesService.get_category_sales_data(filters=filters, user=request.user)
        records = data['records']
        totals = data['totals']

        export_mode = request.GET.get('export', '').strip().lower()
        if export_mode == 'csv':
            response = HttpResponse(content_type='text/csv; charset=utf-8')
            response['Content-Disposition'] = f'attachment; filename="Category_Sales_{data["start_date"]}_{data["end_date"]}.csv"'
            writer = csv.writer(response)
            writer.writerow([
                'Category Code', 'Category Name', 'Invoices', 'Lines Billed', 'Units Sold',
                'Gross Sales', 'Discounts', 'Net Turnover', 'COGS', 'Gross Profit', 'Margin %', 'Contribution %'
            ])
            for r in records:
                writer.writerow(sanitize_csv_row([
                    r['category_code'], r['category_name'], r['invoices_count'], r['lines_count'],
                    f"{r['total_units_sold']:.1f}", f"{r['gross_sales']:.2f}", f"{r['total_discounts']:.2f}",
                    f"{r['net_turnover']:.2f}", f"{r['total_cogs']:.2f}", f"{r['gross_profit']:.2f}",
                    f"{r['margin_percent']:.1f}%", f"{r['contribution_percent']:.1f}%"
                ]))
            writer.writerow(sanitize_csv_row([
                'TOTALS', f"{totals.get('categories_count', 0)} Categories", '', totals.get('total_lines_count', 0),
                f"{totals.get('total_units_sold', 0):.1f}", f"{totals.get('total_gross', 0):.2f}",
                f"{totals.get('total_discounts', 0):.2f}", f"{totals.get('total_net', 0):.2f}",
                f"{totals.get('total_cogs', 0):.2f}", f"{totals.get('total_profit', 0):.2f}",
                f"{totals.get('overall_margin_pct', 0):.1f}%", '100.0%'
            ]))
            return response

        paginator = Paginator(records, 50)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        context = {
            'records': page_obj,
            'page_obj': page_obj,
            'is_paginated': page_obj.has_other_pages(),
            'totals': totals,
            'branches': data['branches'],
            'categories': data['categories'],
            'selected_branch': data['selected_branch'],
            'filters': filters,
            'start_date': data['start_date'],
            'end_date': data['end_date'],
            'start_date_bs': data['start_date_bs'],
            'end_date_bs': data['end_date_bs'],
        }
        return render(request, self.template_name, context)


# ==============================================================================
# SALES REPORT 6: SOLD IMEI & HANDSET REGISTRY REPORT
# ==============================================================================
class SoldIMEIRegistryReportView(ReportAccessMixin, View):
    """Forensic audit of sold handsets, IMEI 1 & 2, customer details, MDMS, and warranties."""
    template_name = 'reports/sold_imei_registry.html'

    def get(self, request, *args, **kwargs):
        active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()

        filters = {
            'start_date': request.GET.get('start_date', ''),
            'end_date': request.GET.get('end_date', ''),
            'branch_id': request.GET.get('branch', '') or (active_branch.id if not request.user.is_superuser else ''),
            'brand_id': request.GET.get('brand', ''),
            'condition': request.GET.get('condition', '').strip(),
            'mdms_status': request.GET.get('mdms_status', '').strip(),
            'q': request.GET.get('q', '').strip(),
        }

        data = IMEISalesService.get_imei_sales_data(filters=filters, user=request.user)
        records = data['records']
        totals = data['totals']

        export_mode = request.GET.get('export', '').strip().lower()
        if export_mode == 'csv':
            response = HttpResponse(content_type='text/csv; charset=utf-8')
            response['Content-Disposition'] = f'attachment; filename="Sold_IMEI_Registry_{data["start_date"]}_{data["end_date"]}.csv"'
            writer = csv.writer(response)
            writer.writerow([
                'Estimate No', 'Date (AD)', 'Date (BS)', 'Customer Name', 'Phone',
                'Handset Model', 'Brand', 'Specs', 'IMEI 1', 'IMEI 2', 'S/N',
                'Condition', 'MDMS Status', 'Landed Cost', 'Sold Price', 'Gross Profit',
                'Margin %', 'Warranty Terms', 'Warranty Expiry', 'Active Warranty?'
            ])
            for r in records:
                writer.writerow(sanitize_csv_row([
                    r['estimate_number'], r['date_ad_str'], r['date_bs_str'], r['customer_name'],
                    r['customer_phone'], r['product_name'], r['brand_name'], r['specs'],
                    r['imei_1'], r['imei_2'], r['serial_number'], r['condition'],
                    r['mdms_label'], f"{r['landed_cost']:.2f}", f"{r['sold_price']:.2f}",
                    f"{r['gross_profit']:.2f}", f"{r['margin_percent']:.1f}%", r['warranty_terms'],
                    r['warranty_expiry_str'], 'Yes' if r['is_warranty_active'] else 'No'
                ]))
            writer.writerow(sanitize_csv_row([
                'TOTALS', '', '', '', '', f"{totals.get('total_handsets_count', 0)} Handsets", '', '', '', '', '', '', '',
                f"{totals.get('total_cost_amount', 0):.2f}", f"{totals.get('total_sold_amount', 0):.2f}",
                f"{totals.get('total_profit_amount', 0):.2f}", f"{totals.get('overall_margin_pct', 0):.1f}%", '', '', ''
            ]))
            return response

        paginator = Paginator(records, 50)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        context = {
            'records': page_obj,
            'page_obj': page_obj,
            'is_paginated': page_obj.has_other_pages(),
            'totals': totals,
            'branches': data['branches'],
            'brands': data['brands'],
            'conditions': data['conditions'],
            'selected_branch': data['selected_branch'],
            'filters': filters,
            'start_date': data['start_date'],
            'end_date': data['end_date'],
            'start_date_bs': data['start_date_bs'],
            'end_date_bs': data['end_date_bs'],
        }
        return render(request, self.template_name, context)


# ==============================================================================
# SALES REPORT 7: SALESPERSON PERFORMANCE REPORT
# ==============================================================================
class SalespersonSalesReportView(ReportAccessMixin, View):
    """Counter staff productivity: phone volume, accessory units, turnover, and average ticket size."""
    template_name = 'reports/salesperson_sales.html'

    def get(self, request, *args, **kwargs):
        active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()

        filters = {
            'start_date': request.GET.get('start_date', ''),
            'end_date': request.GET.get('end_date', ''),
            'branch_id': request.GET.get('branch', '') or (active_branch.id if not request.user.is_superuser else ''),
            'salesperson_id': request.GET.get('salesperson', ''),
            'q': request.GET.get('q', '').strip(),
        }

        data = SalespersonSalesService.get_salesperson_sales_data(filters=filters, user=request.user)
        records = data['records']
        totals = data['totals']

        export_mode = request.GET.get('export', '').strip().lower()
        if export_mode == 'csv':
            response = HttpResponse(content_type='text/csv; charset=utf-8')
            response['Content-Disposition'] = f'attachment; filename="Salesperson_Sales_{data["start_date"]}_{data["end_date"]}.csv"'
            writer = csv.writer(response)
            writer.writerow([
                'Staff Name', 'Username', 'Role', 'Branch', 'Invoices', 'Phone Units',
                'Accessory Units', 'Total Units', 'Gross Sales', 'Discounts Given',
                'Net Turnover', 'COGS', 'Gross Profit', 'Margin %', 'Avg Ticket Size'
            ])
            for r in records:
                writer.writerow(sanitize_csv_row([
                    r['name'], r['username'], r['role'], r['branch_name'], r['invoices_count'],
                    f"{r['phone_units_sold']:.0f}", f"{r['accessory_units_sold']:.1f}", f"{r['total_units_sold']:.1f}",
                    f"{r['gross_sales']:.2f}", f"{r['total_discounts']:.2f}", f"{r['net_turnover']:.2f}",
                    f"{r['total_cogs']:.2f}", f"{r['gross_profit']:.2f}", f"{r['margin_percent']:.1f}%",
                    f"{r['avg_basket_size']:.2f}"
                ]))
            writer.writerow(sanitize_csv_row([
                'TOTALS', f"{totals.get('salespeople_count', 0)} Staff", '', '', totals.get('total_invoices', 0),
                f"{totals.get('total_phones_sold', 0):.0f}", f"{totals.get('total_accessories_sold', 0):.1f}",
                f"{totals.get('total_units_sold', 0):.1f}", f"{totals.get('total_gross_sales', 0):.2f}",
                f"{totals.get('total_discounts_given', 0):.2f}", f"{totals.get('total_net_turnover', 0):.2f}",
                f"{totals.get('total_cogs', 0):.2f}", f"{totals.get('total_gross_profit', 0):.2f}",
                f"{totals.get('overall_margin_pct', 0):.1f}%", f"{totals.get('overall_avg_basket', 0):.2f}"
            ]))
            return response

        paginator = Paginator(records, 50)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        context = {
            'records': page_obj,
            'page_obj': page_obj,
            'is_paginated': page_obj.has_other_pages(),
            'totals': totals,
            'branches': data['branches'],
            'salespeople': data['salespeople'],
            'selected_branch': data['selected_branch'],
            'filters': filters,
            'start_date': data['start_date'],
            'end_date': data['end_date'],
            'start_date_bs': data['start_date_bs'],
            'end_date_bs': data['end_date_bs'],
        }
        return render(request, self.template_name, context)


# ==============================================================================
# SALES REPORT 8: CASHIER-WISE COLLECTION REPORT
# ==============================================================================
class CashierSalesReportView(ReportAccessMixin, View):
    """Cashier accountability, tender reconciliation, digital collections, and return refunds."""
    template_name = 'reports/cashier_sales.html'

    def get(self, request, *args, **kwargs):
        active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()

        filters = {
            'start_date': request.GET.get('start_date', ''),
            'end_date': request.GET.get('end_date', ''),
            'branch_id': request.GET.get('branch', '') or (active_branch.id if not request.user.is_superuser else ''),
            'cashier_id': request.GET.get('cashier', ''),
            'q': request.GET.get('q', '').strip(),
        }

        data = CashierSalesService.get_cashier_sales_data(filters=filters, user=request.user)
        records = data['records']
        totals = data['totals']

        export_mode = request.GET.get('export', '').strip().lower()
        if export_mode == 'csv':
            response = HttpResponse(content_type='text/csv; charset=utf-8')
            response['Content-Disposition'] = f'attachment; filename="Cashier_Collections_{data["start_date"]}_{data["end_date"]}.csv"'
            writer = csv.writer(response)
            writer.writerow([
                'Cashier Name', 'Username', 'Branch', 'Invoices', 'Net Turnover',
                'Net Cash Retained', 'Change Given', 'FonePay QR', 'eSewa', 'Khalti',
                'Card Swipe', 'Bank Transfer', 'Credit Authorized', 'Returns Count', 'Refunds Issued'
            ])
            for r in records:
                writer.writerow(sanitize_csv_row([
                    r['name'], r['username'], r['branch_name'], r['invoices_count'],
                    f"{r['net_turnover']:.2f}", f"{r['cash_net_retained']:.2f}", f"{r['change_given']:.2f}",
                    f"{r['fonepay_collected']:.2f}", f"{r['esewa_collected']:.2f}", f"{r['khalti_collected']:.2f}",
                    f"{r['card_collected']:.2f}", f"{r['bank_collected']:.2f}", f"{r['credit_authorized']:.2f}",
                    r['returns_count'], f"{r['total_refunds_issued']:.2f}"
                ]))
            writer.writerow(sanitize_csv_row([
                'TOTALS', f"{totals.get('cashiers_count', 0)} Cashiers", '', totals.get('total_invoices', 0),
                f"{totals.get('total_net_turnover', 0):.2f}", f"{totals.get('total_cash_net', 0):.2f}",
                f"{totals.get('total_change_given', 0):.2f}", f"{totals.get('total_fonepay', 0):.2f}",
                f"{totals.get('total_esewa', 0):.2f}", f"{totals.get('total_khalti', 0):.2f}",
                f"{totals.get('total_card', 0):.2f}", f"{totals.get('total_bank', 0):.2f}",
                f"{totals.get('total_credit', 0):.2f}", '', f"{totals.get('total_refunds_issued', 0):.2f}"
            ]))
            return response

        paginator = Paginator(records, 50)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        context = {
            'records': page_obj,
            'page_obj': page_obj,
            'is_paginated': page_obj.has_other_pages(),
            'totals': totals,
            'branches': data['branches'],
            'cashiers': data['cashiers'],
            'selected_branch': data['selected_branch'],
            'filters': filters,
            'start_date': data['start_date'],
            'end_date': data['end_date'],
            'start_date_bs': data['start_date_bs'],
            'end_date_bs': data['end_date_bs'],
        }
        return render(request, self.template_name, context)


# ==============================================================================
# SALES REPORT 9: PAYMENT METHOD-WISE REPORT
# ==============================================================================
class PaymentMethodSalesReportView(ReportAccessMixin, View):
    """Reconciles collections across Cash, FonePay, eSewa, Khalti, Card, Bank, and Udhaari."""
    template_name = 'reports/payment_method_sales.html'

    def get(self, request, *args, **kwargs):
        active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()

        filters = {
            'start_date': request.GET.get('start_date', ''),
            'end_date': request.GET.get('end_date', ''),
            'branch_id': request.GET.get('branch', '') or (active_branch.id if not request.user.is_superuser else ''),
            'payment_mode': request.GET.get('payment_mode', '').strip(),
        }

        data = PaymentMethodSalesService.get_payment_method_data(filters=filters, user=request.user)
        records = data['records']
        totals = data['totals']

        export_mode = request.GET.get('export', '').strip().lower()
        if export_mode == 'csv':
            response = HttpResponse(content_type='text/csv; charset=utf-8')
            response['Content-Disposition'] = f'attachment; filename="Payment_Methods_Report_{data["start_date"]}_{data["end_date"]}.csv"'
            writer = csv.writer(response)
            writer.writerow([
                'Payment Channel', 'Transactions', 'Bills Billed',
                'Gross Tendered', 'Cash Change Deducted', 'Net Inflow Total', 'Collection Share %'
            ])
            for r in records:
                writer.writerow(sanitize_csv_row([
                    r['mode_display'], r['transaction_count'], r['invoice_count'],
                    f"{r['gross_amount']:.2f}", f"{r['change_deducted']:.2f}",
                    f"{r['net_amount']:.2f}", f"{r['share_percent']:.1f}%"
                ]))
            writer.writerow(sanitize_csv_row([
                'TOTALS', totals.get('total_transactions', 0), '',
                f"{totals.get('total_tendered_gross', 0):.2f}",
                f"{totals.get('total_change_given', 0):.2f}",
                f"{totals.get('total_net_collections', 0):.2f}", '100.0%'
            ]))
            return response

        context = {
            'records': records,
            'totals': totals,
            'branches': data['branches'],
            'selected_branch': data['selected_branch'],
            'filters': filters,
            'start_date': data['start_date'],
            'end_date': data['end_date'],
            'start_date_bs': data['start_date_bs'],
            'end_date_bs': data['end_date_bs'],
        }
        return render(request, self.template_name, context)


# ==============================================================================
# SALES REPORT 10: DISCOUNT & PRICE OVERRIDE REPORT
# ==============================================================================
class DiscountOverrideReportView(ReportAccessMixin, View):
    """Audits cashier concessions: Amount discounts, % discounts, catalog price overrides, and manager PINs."""
    template_name = 'reports/discount_override_sales.html'

    def get(self, request, *args, **kwargs):
        active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()

        filters = {
            'start_date': request.GET.get('start_date', ''),
            'end_date': request.GET.get('end_date', ''),
            'branch_id': request.GET.get('branch', '') or (active_branch.id if not request.user.is_superuser else ''),
            'concession_type': request.GET.get('concession_type', '').strip(),
            'manager_id': request.GET.get('manager_id', '') or request.GET.get('manager', ''),
            'reason': request.GET.get('reason', '').strip(),
            'q': request.GET.get('q', '').strip(),
        }

        data = DiscountOverrideSalesService.get_discount_override_data(filters=filters, user=request.user)
        records = data['records']
        totals = data['totals']

        export_mode = request.GET.get('export', '').strip().lower()
        if export_mode == 'csv':
            response = HttpResponse(content_type='text/csv; charset=utf-8')
            response['Content-Disposition'] = f'attachment; filename="Discount_Overrides_{data["start_date"]}_{data["end_date"]}.csv"'
            writer = csv.writer(response)
            writer.writerow([
                'Estimate No', 'Date (AD)', 'Date (BS)', 'Branch', 'Customer', 'Product Name',
                'SKU', 'IMEI', 'Qty', 'Official Price', 'Sold Price', 'Price Override Concession',
                'Discount Type', 'Discount Input', 'Item Discount Amount', 'Effective %',
                'Allocated Bill Disc', 'Total Concession', 'Manager Approved By', 'Reason'
            ])
            for r in records:
                writer.writerow(sanitize_csv_row([
                    r['estimate_number'], r['date_ad_str'], r['date_bs_str'], r['branch_code'],
                    r['customer_name'], r['product_name'], r['sku'], r['imei_number'],
                    f"{r['quantity']:.1f}", f"{r['official_unit_price']:.2f}", f"{r['unit_price']:.2f}",
                    f"{r['price_override_amount']:.2f}", r['discount_type'], f"{r['discount_input_value']:.2f}",
                    f"{r['item_discount_amount']:.2f}", f"{r['effective_discount_percent']:.1f}%",
                    f"{r['allocated_bill_discount']:.2f}", f"{r['total_line_concession']:.2f}",
                    r['manager_override_by'], r['discount_reason']
                ]))
            writer.writerow(sanitize_csv_row([
                'TOTALS', '', '', '', '', f"{totals.get('total_lines', 0)} Lines", '', '', '', '', '',
                f"{totals.get('total_price_overrides', 0):.2f}", '', '',
                f"{totals.get('total_item_disc_amount_type', 0) + totals.get('total_item_disc_percent_type', 0):.2f}",
                '', f"{totals.get('total_bill_disc_alloc', 0):.2f}",
                f"{totals.get('total_concessions_granted', 0):.2f}", '', ''
            ]))
            return response

        paginator = Paginator(records, 50)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        context = {
            'records': page_obj,
            'page_obj': page_obj,
            'is_paginated': page_obj.has_other_pages(),
            'totals': totals,
            'branches': data['branches'],
            'managers': data['managers'],
            'selected_branch': data['selected_branch'],
            'filters': filters,
            'start_date': data['start_date'],
            'end_date': data['end_date'],
            'start_date_bs': data['start_date_bs'],
            'end_date_bs': data['end_date_bs'],
        }
        return render(request, self.template_name, context)


# ==============================================================================
# SALES REPORT 11: TRADE-IN EXCHANGE SALES REPORT
# ==============================================================================
class TradeInExchangeSalesReportView(ReportAccessMixin, View):
    """Reconciles combined deals where customer traded in old phone to buy a new smartphone."""
    template_name = 'reports/trade_in_sales.html'

    def get(self, request, *args, **kwargs):
        active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()

        filters = {
            'start_date': request.GET.get('start_date', ''),
            'end_date': request.GET.get('end_date', ''),
            'branch_id': request.GET.get('branch', '') or (active_branch.id if not request.user.is_superuser else ''),
            'q': request.GET.get('q', '').strip(),
        }

        data = TradeInSalesService.get_trade_in_sales_data(filters=filters, user=request.user)
        records = data['records']
        totals = data['totals']

        export_mode = request.GET.get('export', '').strip().lower()
        if export_mode == 'csv':
            response = HttpResponse(content_type='text/csv; charset=utf-8')
            response['Content-Disposition'] = f'attachment; filename="Trade_In_Deals_{data["start_date"]}_{data["end_date"]}.csv"'
            writer = csv.writer(response)
            writer.writerow([
                'Bill No', 'Voucher No', 'Date (AD)', 'Date (BS)', 'Branch', 'Customer',
                'New Phone Sold', 'New Phone IMEI', 'New Phone Price', 'Traded Old Phone',
                'Old Phone IMEI', 'Condition Grade', 'NTA MDMS', 'Trade-In Credit',
                'Cash Top-Up Paid', 'Due Balance', 'Deal Gross Profit', 'Police KYC'
            ])
            for r in records:
                writer.writerow(sanitize_csv_row([
                    r['estimate_number'], r['voucher_number'], r['date_ad_str'], r['date_bs_str'],
                    r['branch_code'], r['customer_name'], r['new_phone_name'], r['new_phone_imei'],
                    f"{r['new_phone_price']:.2f}", r['old_phone_model'], r['old_phone_imei'],
                    r['old_condition'], r['old_mdms'], f"{r['trade_in_credit']:.2f}",
                    f"{r['cash_topup_paid']:.2f}", f"{r['due_balance']:.2f}",
                    f"{r['deal_profit']:.2f}", 'Signed' if r['undertaking_signed'] else 'Pending'
                ]))
            writer.writerow(sanitize_csv_row([
                'TOTALS', f"{totals.get('total_deals_count', 0)} Deals", '', '', '', '', '', '',
                f"{totals.get('total_new_phones_value', 0):.2f}", '', '', '', '',
                f"{totals.get('total_trade_in_credits', 0):.2f}",
                f"{totals.get('total_cash_topup_collected', 0):.2f}", '',
                f"{totals.get('total_combined_profit', 0):.2f}", ''
            ]))
            return response

        paginator = Paginator(records, 50)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        context = {
            'records': page_obj,
            'page_obj': page_obj,
            'is_paginated': page_obj.has_other_pages(),
            'totals': totals,
            'branches': data['branches'],
            'selected_branch': data['selected_branch'],
            'filters': filters,
            'start_date': data['start_date'],
            'end_date': data['end_date'],
            'start_date_bs': data['start_date_bs'],
            'end_date_bs': data['end_date_bs'],
        }
        return render(request, self.template_name, context)


# ==============================================================================
# SALES REPORT 12: GROSS PROFIT & MARGIN REALIZATION REPORT
# ==============================================================================
class GrossProfitMarginReportView(ReportAccessMixin, View):
    """Reconciles net revenue against true landed COGS and classifies items into 4 Margin Tiers."""
    template_name = 'reports/gross_profit_sales.html'

    def get(self, request, *args, **kwargs):
        active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()

        filters = {
            'start_date': request.GET.get('start_date', ''),
            'end_date': request.GET.get('end_date', ''),
            'branch_id': request.GET.get('branch', '') or (active_branch.id if not request.user.is_superuser else ''),
            'category_id': request.GET.get('category', ''),
            'brand_id': request.GET.get('brand', ''),
            'margin_tier': request.GET.get('margin_tier', '').strip(),
            'q': request.GET.get('q', '').strip(),
        }

        data = GrossProfitSalesService.get_gross_profit_data(filters=filters, user=request.user)
        records = data['records']
        totals = data['totals']

        export_mode = request.GET.get('export', '').strip().lower()
        if export_mode == 'csv':
            response = HttpResponse(content_type='text/csv; charset=utf-8')
            response['Content-Disposition'] = f'attachment; filename="Gross_Profit_Margins_{data["start_date"]}_{data["end_date"]}.csv"'
            writer = csv.writer(response)
            writer.writerow([
                'Product SKU', 'Product Name', 'Category', 'Brand', 'Tracking',
                'Qty Sold', 'Gross Revenue', 'Discounts Given', 'Net Revenue',
                'COGS (Landed)', 'Gross Profit', 'Margin %', 'Margin Bracket', 'Current MRP'
            ])
            for r in records:
                writer.writerow(sanitize_csv_row([
                    r['sku'], r['name'], r['category_name'], r['brand_name'],
                    'IMEI' if r['is_serialized'] else 'Bulk', f"{r['quantity_sold']:.1f}",
                    f"{r['gross_revenue']:.2f}", f"{r['discounts_given']:.2f}", f"{r['net_revenue']:.2f}",
                    f"{r['cogs_total']:.2f}", f"{r['gross_profit']:.2f}", f"{r['margin_percent']:.1f}%",
                    r['tier_label'], f"{r['current_mrp']:.2f}"
                ]))
            writer.writerow(sanitize_csv_row([
                'TOTALS', f"{totals.get('products_count', 0)} Products", '', '', '',
                f"{totals.get('total_units_sold', 0):.1f}", f"{totals.get('total_gross_rev', 0):.2f}",
                '', f"{totals.get('total_net_rev', 0):.2f}", f"{totals.get('total_cogs_sum', 0):.2f}",
                f"{totals.get('total_profit_sum', 0):.2f}", f"{totals.get('overall_margin_pct', 0):.1f}%", '', ''
            ]))
            return response

        paginator = Paginator(records, 50)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        context = {
            'records': page_obj,
            'page_obj': page_obj,
            'is_paginated': page_obj.has_other_pages(),
            'totals': totals,
            'branches': data['branches'],
            'categories': data['categories'],
            'brands': data['brands'],
            'selected_branch': data['selected_branch'],
            'filters': filters,
            'start_date': data['start_date'],
            'end_date': data['end_date'],
            'start_date_bs': data['start_date_bs'],
            'end_date_bs': data['end_date_bs'],
        }
        return render(request, self.template_name, context)


# ==============================================================================
# SALES REPORT 13: TOP-SELLING PRODUCTS REPORT
# ==============================================================================
class TopSellingSalesReportView(ReportAccessMixin, View):
    """Ranks fastest-moving products by volume (quantity) and revenue with live shelf stock."""
    template_name = 'reports/top_selling_sales.html'

    def get(self, request, *args, **kwargs):
        active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()

        filters = {
            'start_date': request.GET.get('start_date', ''),
            'end_date': request.GET.get('end_date', ''),
            'branch_id': request.GET.get('branch', '') or (active_branch.id if not request.user.is_superuser else ''),
            'category_id': request.GET.get('category', ''),
            'brand_id': request.GET.get('brand', ''),
            'top_limit': request.GET.get('limit', 20),
            'rank_by': request.GET.get('rank_by', 'revenue').strip().lower(),
            'q': request.GET.get('q', '').strip(),
        }

        data = TopSlowSalesService.get_sales_velocity_data(filters=filters, user=request.user)
        rank_by = filters['rank_by']
        records = data['top_by_revenue'] if rank_by == 'revenue' else data['top_by_volume']
        totals = data['totals']

        export_mode = request.GET.get('export', '').strip().lower()
        if export_mode == 'csv':
            response = HttpResponse(content_type='text/csv; charset=utf-8')
            response['Content-Disposition'] = f'attachment; filename="Top_Sellers_{rank_by}_{data["start_date"]}_{data["end_date"]}.csv"'
            writer = csv.writer(response)
            writer.writerow([
                'Rank', 'SKU', 'Product Name', 'Category', 'Brand', 'Specs',
                'Qty Sold', 'Unit', 'Avg Price', 'Net Revenue', 'COGS',
                'Gross Profit', 'Margin %', 'Revenue Share %', 'Current Stock'
            ])
            for idx, r in enumerate(records, start=1):
                writer.writerow(sanitize_csv_row([
                    idx, r['sku'], r['name'], r['category_name'], r['brand_name'],
                    r['variant_specs'], f"{r['quantity_sold']:.1f}", r['unit_code'],
                    f"{r['avg_selling_price']:.2f}", f"{r['net_revenue']:.2f}",
                    f"{r['cogs_total']:.2f}", f"{r['gross_profit']:.2f}",
                    f"{r['margin_percent']:.1f}%", f"{r['revenue_share_percent']:.1f}%",
                    f"{r['current_stock_on_hand']:.1f}"
                ]))
            return response

        paginator = Paginator(records, 50)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        context = {
            'records': page_obj,
            'page_obj': page_obj,
            'is_paginated': page_obj.has_other_pages(),
            'top_by_volume': data['top_by_volume'],
            'top_by_revenue': data['top_by_revenue'],
            'rank_by': rank_by,
            'totals': totals,
            'branches': data['branches'],
            'categories': data['categories'],
            'brands': data['brands'],
            'selected_branch': data['selected_branch'],
            'filters': filters,
            'start_date': data['start_date'],
            'end_date': data['end_date'],
            'start_date_bs': data['start_date_bs'],
            'end_date_bs': data['end_date_bs'],
        }
        return render(request, self.template_name, context)


# ==============================================================================
# SALES REPORT 14: SLOW-SELLING PRODUCTS & DORMANT STOCK REPORT
# ==============================================================================
class SlowSellingSalesReportView(ReportAccessMixin, View):
    """In-stock products with zero or <= threshold sales over the period with tied-up capital analysis."""
    template_name = 'reports/slow_selling_sales.html'

    def get(self, request, *args, **kwargs):
        active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()

        filters = {
            'start_date': request.GET.get('start_date', ''),
            'end_date': request.GET.get('end_date', ''),
            'branch_id': request.GET.get('branch', '') or (active_branch.id if not request.user.is_superuser else ''),
            'category_id': request.GET.get('category', ''),
            'brand_id': request.GET.get('brand', ''),
            'slow_threshold': request.GET.get('threshold', 2.0),
            'q': request.GET.get('q', '').strip(),
        }

        data = TopSlowSalesService.get_sales_velocity_data(filters=filters, user=request.user)
        records = data['slow_selling']
        totals = data['totals']

        export_mode = request.GET.get('export', '').strip().lower()
        if export_mode == 'csv':
            response = HttpResponse(content_type='text/csv; charset=utf-8')
            response['Content-Disposition'] = f'attachment; filename="Slow_Selling_Products_{data["start_date"]}_{data["end_date"]}.csv"'
            writer = csv.writer(response)
            writer.writerow([
                'SKU', 'Product Name', 'Category', 'Brand', 'Specs',
                'Stock On Hand', 'Unit', 'Qty Sold in Period', 'Period Net Revenue',
                'Cost Rate', 'Selling MRP', 'Tied Capital (Cost)', 'Potential Retail Value', 'Recommended Action'
            ])
            for r in records:
                writer.writerow(sanitize_csv_row([
                    r['sku'], r['name'], r['category_name'], r['brand_name'],
                    r['variant_specs'], f"{r['on_hand_stock']:.1f}", r['unit_code'],
                    f"{r['quantity_sold_in_period']:.1f}", f"{r['net_revenue_in_period']:.2f}",
                    f"{r['cost_price']:.2f}", f"{r['selling_price']:.2f}",
                    f"{r['tied_capital_cost']:.2f}", f"{r['potential_retail_value']:.2f}",
                    r['recommendation']
                ]))
            writer.writerow(sanitize_csv_row([
                'TOTALS', f"{totals.get('slow_selling_count', 0)} Slow Items", '', '', '',
                f"{totals.get('total_slow_units_on_shelf', 0):.1f}", '', '', '', '', '',
                f"{totals.get('total_dormant_capital', 0):.2f}", '', ''
            ]))
            return response

        paginator = Paginator(records, 50)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        context = {
            'records': page_obj,
            'page_obj': page_obj,
            'is_paginated': page_obj.has_other_pages(),
            'totals': totals,
            'branches': data['branches'],
            'categories': data['categories'],
            'brands': data['brands'],
            'selected_branch': data['selected_branch'],
            'filters': filters,
            'start_date': data['start_date'],
            'end_date': data['end_date'],
            'start_date_bs': data['start_date_bs'],
            'end_date_bs': data['end_date_bs'],
        }
        return render(request, self.template_name, context)


# ==============================================================================
# SALES REPORT 15: CANCELLED & VOIDED SALES AUDIT REPORT (PART A)
# ==============================================================================
class CancelledSalesAuditReportView(ReportAccessMixin, View):
    """
    Forensic anti-fraud audit log of cancelled sales estimates (Part A):
    - Dual calendar support: Gregorian AD and Nepali Bikram Sambat (BS).
    - Customer search across Name, Phone, PAN, and IMEI.
    - Captures originating cashier, cancelling supervisor, reason, reversed amounts, and physical stock reversal status.
    - Exports a clean, audit-ready spreadsheet named 'Cancelled_Sales_Report.csv'.
    """
    template_name = 'reports/cancelled_sales_audit.html'

    def get(self, request, *args, **kwargs):
        active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()

        start_date_ad = request.GET.get('start_date', '').strip()
        end_date_ad = request.GET.get('end_date', '').strip()
        start_date_bs = request.GET.get('start_date_bs', '').strip()
        end_date_bs = request.GET.get('end_date_bs', '').strip()

        # Seamless BS to AD conversion if BS dates were submitted without AD parameters
        if not start_date_ad and start_date_bs:
            try:
                parts = [int(p) for p in start_date_bs.split('-')]
                if len(parts) == 3:
                    start_date_ad = NepaliCalendar.bs_to_ad(parts[0], parts[1], parts[2]).strftime('%Y-%m-%d')
            except Exception:
                pass

        if not end_date_ad and end_date_bs:
            try:
                parts = [int(p) for p in end_date_bs.split('-')]
                if len(parts) == 3:
                    end_date_ad = NepaliCalendar.bs_to_ad(parts[0], parts[1], parts[2]).strftime('%Y-%m-%d')
            except Exception:
                pass

        filters = {
            'start_date': start_date_ad,
            'end_date': end_date_ad,
            'start_date_bs': start_date_bs,
            'end_date_bs': end_date_bs,
            'date_basis': request.GET.get('date_basis', 'bill_date').strip(),
            'branch_id': request.GET.get('branch', '') or (active_branch.id if not request.user.is_superuser else ''),
            'cashier_id': request.GET.get('cashier', ''),
            'reason': request.GET.get('reason', '').strip(),
            'q': request.GET.get('q', '').strip(),
        }

        data = CancelledSalesService.get_cancelled_sales_data(filters=filters, user=request.user)
        records = data['records']
        totals = data['totals']

        export_mode = request.GET.get('export', '').strip().lower()
        if export_mode == 'csv':
            if hasattr(CSVExportEngine, 'export_cancelled_sales_csv'):
                return CSVExportEngine.export_cancelled_sales_csv(
                    records=records,
                    totals=totals,
                    filename="Cancelled_Sales_Report.csv"
                )
            else:
                response = HttpResponse(content_type='text/csv; charset=utf-8')
                response['Content-Disposition'] = 'attachment; filename="Cancelled_Sales_Report.csv"'
                writer = csv.writer(response)
                writer.writerow([
                    'Estimate No', 'Bill Date (AD)', 'Bill Date (BS)', 'Cancellation Timestamp',
                    'Branch Code', 'Branch Name', 'Customer Name', 'Customer Phone', 'Customer PAN',
                    'Cashier', 'Salesperson', 'Cancelled By User', 'IP Address', 'Cancellation Reason',
                    'Gross Subtotal (NPR)', 'Discounts Voided (NPR)', 'Tax / VAT Voided (NPR)',
                    'Voided Grand Total (NPR)', 'Paid Cash Reversed (NPR)', 'Due Udhaari Reversed (NPR)',
                    'Stock Reversal Executed?'
                ])
                for r in records:
                    writer.writerow(sanitize_csv_row([
                        r['estimate_number'], r['bill_date_ad'], r['bill_date_bs'], r['cancel_date_ad_str'],
                        r['branch_code'], r['branch_name'], r['customer_name'], r['customer_phone'], r.get('customer_pan', '-'),
                        r['cashier'], r['salesperson'], r['cancelled_by'], r['cancel_ip'], r['cancellation_reason'],
                        f"{r['subtotal']:.2f}", f"{r['total_discounts']:.2f}", f"{r['vat_amount']:.2f}",
                        f"{r['grand_total']:.2f}", f"{r['paid_amount_reversed']:.2f}",
                        f"{r['due_amount_reversed']:.2f}", 'Yes (Confirmed Reverted)' if r['is_stock_reverted'] else 'Pending Verification'
                    ]))
                writer.writerow(sanitize_csv_row([
                    'TOTALS', '', '', '', '', '', f"{totals.get('total_voided_bills', len(records))} Voided Invoices", '', '',
                    '', '', '', '', '',
                    f"{totals.get('total_subtotal', 0):.2f}", f"{totals.get('total_discounts_voided', 0):.2f}",
                    f"{totals.get('total_vat_voided', 0):.2f}", f"{totals.get('total_voided_amount', 0):.2f}",
                    f"{totals.get('total_paid_reversed', 0):.2f}", f"{totals.get('total_due_reversed', 0):.2f}", ''
                ]))
                return response

        paginator = Paginator(records, 50)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        context = {
            'records': page_obj,
            'page_obj': page_obj,
            'is_paginated': page_obj.has_other_pages(),
            'totals': totals,
            'branches': data['branches'],
            'cashiers': data['cashiers'],
            'selected_branch': data['selected_branch'],
            'filters': filters,
            'start_date': data['start_date'],
            'end_date': data['end_date'],
            'start_date_bs': data['start_date_bs'],
            'end_date_bs': data['end_date_bs'],
            'date_basis': data.get('date_basis', 'bill_date'),
        }
        return render(request, self.template_name, context)


# ==============================================================================
# REPORT 16: CORE INVENTORY & LEDGER (SUMMARY, DETAIL, SNAPSHOTS)
# ==============================================================================
class StockSummaryReportView(ReportAccessMixin, View):
    template_name = 'reports/stock_summary.html'
    print_template_name = 'reports/stock_summary_print.html'

    def get(self, request, *args, **kwargs):
        active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()

        filters = {
            'start_date': request.GET.get('start_date', ''),
            'end_date': request.GET.get('end_date', ''),
            'branch_id': request.GET.get('branch', '') or (active_branch.id if not request.user.is_superuser else ''),
            'warehouse': request.GET.get('warehouse', '').strip(),
            'category_id': request.GET.get('category', ''),
            'subcategory_id': request.GET.get('subcategory', ''),
            'brand_id': request.GET.get('brand', ''),
            'product_id': request.GET.get('product', ''),
            'sku': request.GET.get('sku', '').strip(),
            'stock_status': request.GET.get('stock_status', '').strip(),
            'is_serialized': request.GET.get('is_serialized', '').strip(),
            'q': request.GET.get('q', '').strip(),
        }

        result = StockSummaryService.get_stock_summary(filters=filters, user=request.user)
        rows = result['rows']
        summary_totals = result['summary_totals']
        start_date = result['start_date']
        end_date = result['end_date']
        start_date_bs = result['start_date_bs']
        end_date_bs = result['end_date_bs']

        date_range_label = f"{start_date} to {end_date} ({start_date_bs} to {end_date_bs} BS)"

        selected_branch = None
        if filters['branch_id']:
            selected_branch = Branch.objects.filter(id=filters['branch_id']).first()
        if not selected_branch:
            selected_branch = active_branch

        export_mode = request.GET.get('export', '').strip().lower()

        if export_mode == 'csv':
            filename = f"Stock_Summary_{start_date}_{end_date}.csv"
            return CSVExportEngine.export_stock_summary_csv(
                report_data=rows,
                summary_totals=summary_totals,
                filename=filename
            )

        elif export_mode == 'excel':
            filename = f"Stock_Summary_{start_date}_{end_date}.xlsx"
            return CSVExportEngine.export_stock_summary_excel(
                report_data=rows,
                summary_totals=summary_totals,
                date_range_label=date_range_label,
                filename=filename
            )

        elif export_mode == 'pdf':
            filename = f"Stock_Summary_{start_date}_{end_date}.pdf"
            return CSVExportEngine.export_stock_summary_pdf(
                report_data=rows,
                summary_totals=summary_totals,
                date_range_label=date_range_label,
                branch_name=selected_branch.name if selected_branch else "All Outlets",
                filename=filename
            )

        if request.GET.get('format', '').strip().lower() == 'print':
            return render(request, self.print_template_name, {
                'records': rows,
                'summary_totals': summary_totals,
                'start_date': start_date,
                'end_date': end_date,
                'start_date_bs': start_date_bs,
                'end_date_bs': end_date_bs,
                'selected_branch': selected_branch,
                'filters': filters,
                'generated_at': timezone.now(),
            })

        page_num = request.GET.get('page', 1)
        paginator = Paginator(rows, 50)
        page_obj = paginator.get_page(page_num)

        context = {
            'records': page_obj,
            'page_obj': page_obj,
            'is_paginated': page_obj.has_other_pages(),
            'summary_totals': summary_totals,
            'start_date': start_date.strftime('%Y-%m-%d'),
            'end_date': end_date.strftime('%Y-%m-%d'),
            'start_date_bs': start_date_bs,
            'end_date_bs': end_date_bs,
            'selected_branch': selected_branch,
            'filters': filters,
            'branches': Branch.objects.filter(is_active=True).order_by('-is_main_branch', 'name'),
            'categories': ProductCategory.objects.filter(is_active=True).order_by('name'),
            'subcategories': ProductSubCategory.objects.all().order_by('name'),
            'brands': Brand.objects.all().order_by('name'),
            'products_list': Product.objects.filter(is_active=True).order_by('name')[:100],
        }

        return render(request, self.template_name, context)


class StockDetailReportView(ReportAccessMixin, View):
    template_name = 'reports/stock_detail.html'
    print_template_name = 'reports/stock_detail_print.html'

    def get(self, request, *args, **kwargs):
        active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()

        filters = {
            'product_id': request.GET.get('product_id') or request.GET.get('product', ''),
            'branch_id': request.GET.get('branch', '') or (active_branch.id if not request.user.is_superuser else ''),
            'start_date': request.GET.get('start_date', ''),
            'end_date': request.GET.get('end_date', ''),
            'sku': request.GET.get('sku', '').strip(),
            'q': request.GET.get('q', '').strip(),
        }

        data = StockDetailService.get_stock_detail(filters=filters, user=request.user)
        rows = data['rows']
        product = data['product']
        opening_balance = data['opening_balance']
        closing_balance = data['closing_balance']
        total_in = data['total_in']
        total_out = data['total_out']
        start_date = data['start_date']
        end_date = data['end_date']
        start_date_bs = data['start_date_bs']
        end_date_bs = data['end_date_bs']
        selected_branch = data['selected_branch'] or active_branch

        date_range_label = f"{start_date} to {end_date} ({start_date_bs} to {end_date_bs} BS)"

        export_mode = request.GET.get('export', '').strip().lower()

        if export_mode == 'csv' and product:
            filename = f"Stock_Detail_{product.sku}_{start_date}_{end_date}.csv"
            return CSVExportEngine.export_stock_detail_csv(
                product=product,
                report_data=rows,
                opening_balance=opening_balance,
                closing_balance=closing_balance,
                total_in=total_in,
                total_out=total_out,
                date_range_label=date_range_label,
                filename=filename
            )

        elif export_mode == 'excel' and product:
            return CSVExportEngine.export_stock_detail_excel(
                product=product,
                report_data=rows,
                opening_balance=opening_balance,
                closing_balance=closing_balance,
                total_in=total_in,
                total_out=total_out,
                date_range_label=date_range_label,
                branch_name=selected_branch.name
            )

        if request.GET.get('format', '').strip().lower() == 'print':
            return render(request, self.print_template_name, {
                'product': product,
                'records': rows,
                'opening_balance': opening_balance,
                'closing_balance': closing_balance,
                'total_in': total_in,
                'total_out': total_out,
                'start_date': start_date,
                'end_date': end_date,
                'start_date_bs': start_date_bs,
                'end_date_bs': end_date_bs,
                'selected_branch': selected_branch,
                'generated_at': timezone.now(),
            })

        page_num = request.GET.get('page', 1)
        paginator = Paginator(rows, 50)
        page_obj = paginator.get_page(page_num)

        all_products = Product.objects.filter(is_active=True).select_related('brand').order_by('name')[:150]

        context = {
            'product': product,
            'records': page_obj,
            'page_obj': page_obj,
            'is_paginated': page_obj.has_other_pages(),
            'opening_balance': opening_balance,
            'closing_balance': closing_balance,
            'total_in': total_in,
            'total_out': total_out,
            'start_date': start_date.strftime('%Y-%m-%d'),
            'end_date': end_date.strftime('%Y-%m-%d'),
            'start_date_bs': start_date_bs,
            'end_date_bs': end_date_bs,
            'selected_branch': selected_branch,
            'filters': filters,
            'all_products': all_products,
            'branches': Branch.objects.filter(is_active=True).order_by('-is_main_branch', 'name'),
        }

        return render(request, self.template_name, context)


class CurrentStockReportView(ReportAccessMixin, View):
    template_name = 'reports/current_stock.html'

    def get(self, request, *args, **kwargs):
        active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()

        filters = {
            'branch': request.GET.get('branch', '') or (active_branch.id if not request.user.is_superuser else ''),
            'category': request.GET.get('category', ''),
            'brand': request.GET.get('brand', ''),
            'status': request.GET.get('status', '').strip().lower(),
            'q': request.GET.get('q', '').strip(),
        }

        data = CurrentStockService.get_current_stock_data(filters=filters, user=request.user)
        stocks = data['queryset']
        totals = data['totals']

        selected_branch = Branch.objects.filter(id=filters['branch']).first() if filters['branch'] else active_branch

        if request.GET.get('export', '').strip().lower() == 'csv':
            return CSVExportEngine.export_stock_valuation_csv(branch=selected_branch)

        paginator = Paginator(stocks, 50)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        context = {
            'stocks': page_obj,
            'page_obj': page_obj,
            'is_paginated': page_obj.has_other_pages(),
            'totals': totals,
            'branches': Branch.objects.filter(is_active=True).order_by('-is_main_branch', 'name'),
            'categories': ProductCategory.objects.filter(is_active=True).order_by('name'),
            'brands': Brand.objects.all().order_by('name'),
            'filters': filters,
            'selected_branch': selected_branch,
        }
        return render(request, self.template_name, context)


class LowStockReportView(ReportAccessMixin, View):
    template_name = 'reports/low_stock.html'

    def get(self, request, *args, **kwargs):
        active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()

        filters = {
            'branch': request.GET.get('branch', '') or (active_branch.id if not request.user.is_superuser else ''),
            'category': request.GET.get('category', ''),
            'brand': request.GET.get('brand', ''),
            'q': request.GET.get('q', '').strip(),
        }

        data = LowStockService.get_low_stock_data(filters=filters, user=request.user)
        items_data = data['records']

        if request.GET.get('export', '').strip().lower() == 'csv':
            return CSVExportEngine.export_low_stock_csv(items_data=items_data)

        paginator = Paginator(items_data, 50)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        selected_branch = Branch.objects.filter(id=filters['branch']).first() if filters['branch'] else active_branch

        context = {
            'records': page_obj,
            'page_obj': page_obj,
            'is_paginated': page_obj.has_other_pages(),
            'total_low_stock_items': data['total_low_stock_items'],
            'total_reorder_units': data['total_reorder_units'],
            'total_reorder_cost': data['total_reorder_cost'],
            'branches': Branch.objects.filter(is_active=True).order_by('-is_main_branch', 'name'),
            'categories': ProductCategory.objects.filter(is_active=True).order_by('name'),
            'brands': Brand.objects.all().order_by('name'),
            'filters': filters,
            'selected_branch': selected_branch,
        }
        return render(request, self.template_name, context)


class OutOfStockReportView(ReportAccessMixin, View):
    template_name = 'reports/out_of_stock.html'

    def get(self, request, *args, **kwargs):
        active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()

        filters = {
            'branch': request.GET.get('branch', '') or (active_branch.id if not request.user.is_superuser else ''),
            'category': request.GET.get('category', ''),
            'brand': request.GET.get('brand', ''),
            'q': request.GET.get('q', '').strip(),
        }

        data = OutOfStockService.get_out_of_stock_data(filters=filters, user=request.user)
        items_data = data['records']

        if request.GET.get('export', '').strip().lower() == 'csv':
            return CSVExportEngine.export_out_of_stock_csv(items_data=items_data)

        paginator = Paginator(items_data, 50)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        selected_branch = Branch.objects.filter(id=filters['branch']).first() if filters['branch'] else active_branch

        context = {
            'records': page_obj,
            'page_obj': page_obj,
            'is_paginated': page_obj.has_other_pages(),
            'total_zero_stock_items': data['total_zero_stock_items'],
            'total_phones_out': data['total_phones_out'],
            'potential_lost_revenue': data['potential_lost_revenue'],
            'longest_depleted_days': data['longest_depleted_days'],
            'branches': Branch.objects.filter(is_active=True).order_by('-is_main_branch', 'name'),
            'categories': ProductCategory.objects.filter(is_active=True).order_by('name'),
            'brands': Brand.objects.all().order_by('name'),
            'filters': filters,
            'selected_branch': selected_branch,
        }
        return render(request, self.template_name, context)


class IMEIStockReportView(ReportAccessMixin, View):
    template_name = 'reports/imei_stock.html'

    def get(self, request, *args, **kwargs):
        active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()

        filters = {
            'branch': request.GET.get('branch', '') or (active_branch.id if not request.user.is_superuser else ''),
            'brand': request.GET.get('brand', ''),
            'mdms_status': request.GET.get('mdms_status', '').strip(),
            'imei2_status': request.GET.get('imei2_status', '').strip(),
            'condition': request.GET.get('condition', '').strip(),
            'q': request.GET.get('q', '').strip(),
        }

        result = IMEIStockService.get_imei_stock_data(filters=filters, user=request.user)
        queryset = result['queryset']
        totals = result['totals']

        if request.GET.get('export', '').strip().lower() == 'csv':
            return CSVExportEngine.export_imei_stock_csv(queryset=queryset)

        paginator = Paginator(queryset, 50)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        selected_branch = Branch.objects.filter(id=filters['branch']).first() if filters['branch'] else active_branch

        context = {
            'records': page_obj,
            'page_obj': page_obj,
            'is_paginated': page_obj.has_other_pages(),
            'totals': totals,
            'branches': result['branches'],
            'brands': result['brands'],
            'conditions': result['conditions'],
            'filters': filters,
            'selected_branch': selected_branch,
        }
        return render(request, self.template_name, context)


class IMEILifecycleReportView(ReportAccessMixin, View):
    template_name = 'reports/imei_lifecycle.html'

    def get(self, request, *args, **kwargs):
        search_imei = request.GET.get('imei', '').strip() or request.GET.get('q', '').strip()
        data = IMEILifecycleService.get_lifecycle_data(search_query=search_imei, user=request.user)

        context = {
            'query': data['query'],
            'device': data['device'],
            'timeline': data['timeline'],
            'total_events_count': data['total_events_count'],
            'is_found': data['is_found'],
        }
        return render(request, self.template_name, context)


class StockTransferReportView(ReportAccessMixin, View):
    template_name = 'reports/stock_transfer_report.html'

    def get(self, request, *args, **kwargs):
        filters = {
            'source_branch': request.GET.get('source_branch', ''),
            'destination_branch': request.GET.get('destination_branch', ''),
            'status': request.GET.get('status', '').strip(),
            'start_date': request.GET.get('start_date', ''),
            'end_date': request.GET.get('end_date', ''),
            'q': request.GET.get('q', '').strip(),
        }

        result = StockTransferReportService.get_transfer_report(filters=filters, user=request.user)
        records = result['records']
        queryset = result['queryset']
        totals = result['totals']

        if request.GET.get('export', '').strip().lower() == 'csv':
            return CSVExportEngine.export_stock_transfers_csv(queryset=queryset)

        paginator = Paginator(records, 30)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        context = {
            'records': page_obj,
            'page_obj': page_obj,
            'is_paginated': page_obj.has_other_pages(),
            'totals': totals,
            'branches': result['branches'],
            'statuses': result['statuses'],
            'filters': filters,
            'start_date': result['start_date'],
            'end_date': result['end_date'],
        }
        return render(request, self.template_name, context)


class StockAdjustmentReportView(ReportAccessMixin, View):
    template_name = 'reports/stock_adjustment_report.html'

    def get(self, request, *args, **kwargs):
        active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()

        filters = {
            'branch': request.GET.get('branch', '') or (active_branch.id if not request.user.is_superuser else ''),
            'type': request.GET.get('type', '').strip(),
            'category': request.GET.get('category', ''),
            'brand': request.GET.get('brand', ''),
            'start_date': request.GET.get('start_date', ''),
            'end_date': request.GET.get('end_date', ''),
            'q': request.GET.get('q', '').strip(),
        }

        result = StockAdjustmentService.get_adjustment_data(filters=filters, user=request.user)
        records = result['records']
        queryset = result['queryset']
        totals = result['totals']

        if request.GET.get('export', '').strip().lower() == 'csv':
            return CSVExportEngine.export_stock_adjustments_csv(queryset)

        paginator = Paginator(records, 50)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        context = {
            'records': page_obj,
            'page_obj': page_obj,
            'is_paginated': page_obj.has_other_pages(),
            'totals': totals,
            'total_added_qty': totals['total_added_qty'],
            'total_deducted_qty': totals['total_deducted_qty'],
            'net_adjustment_qty': totals['net_adjustment_qty'],
            'total_loss_value': totals['total_loss_value'],
            'total_added_value': totals['total_added_value'],
            'net_financial_impact': totals['net_financial_impact'],
            'total_events': totals['total_events'],
            'branches': result['branches'],
            'categories': result['categories'],
            'brands': result['brands'],
            'filters': filters,
            'start_date': result['start_date'],
            'end_date': result['end_date'],
            'start_date_bs': result['start_date_bs'],
            'end_date_bs': result['end_date_bs'],
            'selected_branch': result['selected_branch'],
        }
        return render(request, self.template_name, context)


class StockAgingReportView(ReportAccessMixin, View):
    template_name = 'reports/stock_aging.html'

    def get(self, request, *args, **kwargs):
        active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()

        filters = {
            'branch': request.GET.get('branch', '') or (active_branch.id if not request.user.is_superuser else ''),
            'category': request.GET.get('category', ''),
            'brand': request.GET.get('brand', ''),
            'bracket': request.GET.get('bracket', '').strip(),
            'item_type': request.GET.get('item_type', '').strip(),
            'q': request.GET.get('q', '').strip(),
        }

        data = StockAgingService.get_stock_aging_data(filters=filters, user=request.user)
        records = data['records']
        totals = data['totals']

        if request.GET.get('export', '').strip().lower() == 'csv':
            return CSVExportEngine.export_stock_aging_csv(records, totals)

        paginator = Paginator(records, 50)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        context = {
            'records': page_obj,
            'page_obj': page_obj,
            'is_paginated': page_obj.has_other_pages(),
            'totals': totals,
            'branches': data['branches'],
            'categories': data['categories'],
            'brands': data['brands'],
            'selected_branch': data['selected_branch'],
            'filters': filters,
        }
        return render(request, self.template_name, context)


class SlowMovingStockReportView(ReportAccessMixin, View):
    template_name = 'reports/slow_moving.html'

    def get(self, request, *args, **kwargs):
        active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()

        filters = {
            'branch': request.GET.get('branch', '') or (active_branch.id if not request.user.is_superuser else ''),
            'category': request.GET.get('category', ''),
            'brand': request.GET.get('brand', ''),
            'days': request.GET.get('days', 60),
            'inactivity_type': request.GET.get('inactivity_type', ''),
            'q': request.GET.get('q', '').strip(),
        }

        data = SlowMovingStockService.get_slow_moving_data(filters=filters, user=request.user)
        records = data['records']
        totals = data['totals']

        paginator = Paginator(records, 50)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        context = {
            'records': page_obj,
            'page_obj': page_obj,
            'is_paginated': page_obj.has_other_pages(),
            'totals': totals,
            'days_threshold': data['days_threshold'],
            'branches': data['branches'],
            'categories': data['categories'],
            'brands': data['brands'],
            'selected_branch': data['selected_branch'],
            'filters': filters,
        }
        return render(request, self.template_name, context)


class TradeInInventoryReportView(ReportAccessMixin, View):
    template_name = 'reports/trade_in_inventory.html'

    def get(self, request, *args, **kwargs):
        active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()

        filters = {
            'branch': request.GET.get('branch', '') or (active_branch.id if not request.user.is_superuser else ''),
            'status': request.GET.get('status', '').strip(),
            'grade': request.GET.get('grade', '') or request.GET.get('condition_grade', ''),
            'mdms_status': request.GET.get('mdms_status', '').strip(),
            'undertaking': request.GET.get('undertaking', '').strip(),
            'q': request.GET.get('q', '').strip(),
        }

        data = TradeInInventoryService.get_trade_in_inventory_data(filters=filters, user=request.user)
        records = data['records']
        totals = data['totals']

        if request.GET.get('export', '').strip().lower() == 'csv':
            return CSVExportEngine.export_trade_in_inventory_csv(records, totals)

        paginator = Paginator(records, 50)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        context = {
            'records': page_obj,
            'page_obj': page_obj,
            'is_paginated': page_obj.has_other_pages(),
            'totals': totals,
            'branches': data['branches'],
            'conditions': data['conditions'],
            'mdms_statuses': data['mdms_statuses'],
            'selected_branch': data['selected_branch'],
            'filters': filters,
        }
        return render(request, self.template_name, context)


class BatchStockReportView(ReportAccessMixin, View):
    template_name = 'reports/batch_stock.html'

    def get(self, request, *args, **kwargs):
        active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()

        filters = {
            'branch': request.GET.get('branch', '') or (active_branch.id if not request.user.is_superuser else ''),
            'category': request.GET.get('category', ''),
            'brand': request.GET.get('brand', ''),
            'status': request.GET.get('status', 'active'),
            'q': request.GET.get('q', '').strip(),
        }

        data = BatchStockService.get_batch_stock_data(filters=filters, user=request.user)
        records = data['records']
        totals = data['totals']

        paginator = Paginator(records, 50)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        context = {
            'records': page_obj,
            'page_obj': page_obj,
            'is_paginated': page_obj.has_other_pages(),
            'totals': totals,
            'branches': data['branches'],
            'categories': data['categories'],
            'brands': data['brands'],
            'selected_branch': data['selected_branch'],
            'filters': filters,
        }
        return render(request, self.template_name, context)


class DamagedStockReportView(ReportAccessMixin, View):
    template_name = 'reports/damaged_stock.html'

    def get(self, request, *args, **kwargs):
        active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()

        filters = {
            'branch': request.GET.get('branch', '') or (active_branch.id if not request.user.is_superuser else ''),
            'category': request.GET.get('category', ''),
            'brand': request.GET.get('brand', ''),
            'q': request.GET.get('q', '').strip(),
        }

        result = DamagedStockService.get_damaged_stock_data(filters=filters, user=request.user)

        context = {
            'quarantined_stocks': result['quarantined_stocks'],
            'defective_parts': result['defective_parts'],
            'defective_handsets': result['defective_handsets'],
            'totals': result['totals'],
            'total_quarantined_units': result['total_quarantined_units'],
            'total_quarantined_cost': result['total_quarantined_cost'],
            'grand_total_locked_capital': result['grand_total_locked_capital'],
            'branches': result['branches'],
            'categories': result['categories'],
            'brands': result['brands'],
            'filters': filters,
            'selected_branch': result['selected_branch'],
        }
        return render(request, self.template_name, context)


class VendorRMAReportView(ReportAccessMixin, View):
    template_name = 'reports/vendor_rma_report.html'

    def get(self, request, *args, **kwargs):
        active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()

        filters = {
            'supplier': request.GET.get('supplier', ''),
            'branch': request.GET.get('branch', '') or (active_branch.id if not request.user.is_superuser else ''),
            'status': request.GET.get('status', '').strip(),
            'resolution': request.GET.get('resolution', '').strip(),
            'start_date': request.GET.get('start_date', ''),
            'end_date': request.GET.get('end_date', ''),
            'q': request.GET.get('q', '').strip(),
        }

        result = VendorRMAReportService.get_vendor_rma_data(filters=filters, user=request.user)
        records = result['records']
        totals = result['totals']

        paginator = Paginator(records, 30)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        context = {
            'records': page_obj,
            'page_obj': page_obj,
            'is_paginated': page_obj.has_other_pages(),
            'totals': totals,
            'suppliers': result['suppliers'],
            'branches': result['branches'],
            'statuses': result['statuses'],
            'resolutions': result['resolutions'],
            'filters': filters,
            'start_date': result['start_date'],
            'end_date': result['end_date'],
            'start_date_bs': result['start_date_bs'],
            'end_date_bs': result['end_date_bs'],
            'selected_branch': result['selected_branch'],
        }
        return render(request, self.template_name, context)


# ==============================================================================
# REPORT 17: PURCHASE DOMAIN REPORTING SUITE
# ==============================================================================
class PurchaseRegisterReportView(ReportAccessMixin, View):
    template_name = 'reports/purchase_register.html'
    fallback_template_name = 'taxation/purchase_vat_register.html'

    def get(self, request, *args, **kwargs):
        active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()

        filters = {
            'start_date': request.GET.get('start_date', ''),
            'end_date': request.GET.get('end_date', ''),
            'branch_id': request.GET.get('branch', '') or (active_branch.id if not request.user.is_superuser else ''),
            'supplier_id': request.GET.get('supplier', ''),
            'tax_mode': request.GET.get('tax_mode', '').strip().lower(),
            'q': request.GET.get('q', '').strip(),
        }

        data = PurchaseRegisterService.get_purchase_register_data(filters=filters, user=request.user)
        records = data['records']
        totals = data['totals']

        export_mode = request.GET.get('export', '').strip().lower()
        if export_mode == 'csv':
            if hasattr(CSVExportEngine, 'export_purchase_register_csv'):
                return CSVExportEngine.export_purchase_register_csv(records, totals)

        paginator = Paginator(records, 40)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        context = {
            'records': page_obj,
            'page_obj': page_obj,
            'is_paginated': page_obj.has_other_pages(),
            'totals': totals,
            'branches': data['branches'],
            'suppliers': data['suppliers'],
            'selected_branch': data['selected_branch'],
            'filters': filters,
            'start_date': data['start_date'],
            'end_date': data['end_date'],
            'start_date_bs': data['start_date_bs'],
            'end_date_bs': data['end_date_bs'],
        }

        try:
            return render(request, self.template_name, context)
        except Exception:
            return render(request, self.fallback_template_name, context)


class SupplierPurchaseReportView(ReportAccessMixin, View):
    template_name = 'reports/supplier_purchase_report.html'

    def get(self, request, *args, **kwargs):
        active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()

        filters = {
            'start_date': request.GET.get('start_date', ''),
            'end_date': request.GET.get('end_date', ''),
            'branch_id': request.GET.get('branch', '') or (active_branch.id if not request.user.is_superuser else ''),
            'supplier_id': request.GET.get('supplier', ''),
            'q': request.GET.get('q', '').strip(),
        }

        data = SupplierPurchaseService.get_supplier_purchase_data(filters=filters, user=request.user)
        records = data['records']
        totals = data['totals']

        export_mode = request.GET.get('export', '').strip().lower()
        if export_mode == 'csv':
            if hasattr(CSVExportEngine, 'export_supplier_purchases_csv'):
                return CSVExportEngine.export_supplier_purchases_csv(records, totals)

        paginator = Paginator(records, 40)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        context = {
            'records': page_obj,
            'page_obj': page_obj,
            'is_paginated': page_obj.has_other_pages(),
            'totals': totals,
            'branches': data['branches'],
            'suppliers': data['suppliers'],
            'selected_branch': data['selected_branch'],
            'filters': filters,
            'start_date': data['start_date'],
            'end_date': data['end_date'],
            'start_date_bs': data['start_date_bs'],
            'end_date_bs': data['end_date_bs'],
        }
        return render(request, self.template_name, context)


class ProductPurchaseReportView(ReportAccessMixin, View):
    template_name = 'reports/product_purchase_report.html'

    def get(self, request, *args, **kwargs):
        active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()

        filters = {
            'start_date': request.GET.get('start_date', ''),
            'end_date': request.GET.get('end_date', ''),
            'branch_id': request.GET.get('branch', '') or (active_branch.id if not request.user.is_superuser else ''),
            'category_id': request.GET.get('category', ''),
            'brand_id': request.GET.get('brand', ''),
            'supplier_id': request.GET.get('supplier', ''),
            'tracking_type': request.GET.get('tracking_type', '').strip(),
            'q': request.GET.get('q', '').strip(),
        }

        data = ProductPurchaseService.get_product_purchase_data(filters=filters, user=request.user)
        records = data['records']
        totals = data['totals']

        export_mode = request.GET.get('export', '').strip().lower()
        if export_mode == 'csv':
            if hasattr(CSVExportEngine, 'export_product_purchases_csv'):
                return CSVExportEngine.export_product_purchases_csv(records, totals)

        paginator = Paginator(records, 40)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        context = {
            'records': page_obj,
            'page_obj': page_obj,
            'is_paginated': page_obj.has_other_pages(),
            'totals': totals,
            'branches': data['branches'],
            'categories': data['categories'],
            'brands': data['brands'],
            'suppliers': data['suppliers'],
            'selected_branch': data['selected_branch'],
            'filters': filters,
            'start_date': data['start_date'],
            'end_date': data['end_date'],
            'start_date_bs': data['start_date_bs'],
            'end_date_bs': data['end_date_bs'],
        }
        return render(request, self.template_name, context)


class SupplierOutstandingReportView(ReportAccessMixin, View):
    template_name = 'reports/supplier_outstanding_report.html'

    def get(self, request, *args, **kwargs):
        active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()

        filters = {
            'start_date': request.GET.get('start_date', ''),
            'end_date': request.GET.get('end_date', ''),
            'branch_id': request.GET.get('branch', '') or (active_branch.id if not request.user.is_superuser else ''),
            'supplier_type': request.GET.get('supplier_type', '').strip(),
            'status': request.GET.get('status', 'due').strip(),
            'q': request.GET.get('q', '').strip(),
        }

        data = SupplierOutstandingService.get_supplier_outstanding_data(filters=filters, user=request.user)
        records = data['records']
        totals = data['totals']

        export_mode = request.GET.get('export', '').strip().lower()
        if export_mode == 'csv':
            if hasattr(CSVExportEngine, 'export_supplier_outstanding_csv'):
                return CSVExportEngine.export_supplier_outstanding_csv(records, totals)

        paginator = Paginator(records, 40)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        context = {
            'records': page_obj,
            'page_obj': page_obj,
            'is_paginated': page_obj.has_other_pages(),
            'totals': totals,
            'branches': data['branches'],
            'supplier_types': data['supplier_types'],
            'selected_branch': data['selected_branch'],
            'filters': filters,
            'start_date': data['start_date'],
            'end_date': data['end_date'],
            'start_date_bs': data['start_date_bs'],
            'end_date_bs': data['end_date_bs'],
        }
        return render(request, self.template_name, context)


# ==============================================================================
# REPORT 17B: CANCELLED & VOIDED PURCHASES AUDIT REPORT (PART B - NEW)
# ==============================================================================
class CancelledPurchasesReportView(ReportAccessMixin, View):
    """
    Forensic anti-fraud audit log of cancelled and voided purchase GRNs (Part B):
    - Dual calendar support: Gregorian AD and Nepali Bikram Sambat (BS).
    - Supplier and Consignment search across Supplier Bill No, GRN, Vendor PAN, and IMEI.
    - Captures original receiving user, cancelling supervisor, reason, reversed AP/Cash, and physical stock reversal status.
    - Exports a clean, audit-ready spreadsheet named 'Cancelled_Purchases_Report.csv'.
    """
    template_name = 'reports/cancelled_purchases_audit.html'

    def get(self, request, *args, **kwargs):
        active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()

        start_date_ad = request.GET.get('start_date', '').strip()
        end_date_ad = request.GET.get('end_date', '').strip()
        start_date_bs = request.GET.get('start_date_bs', '').strip()
        end_date_bs = request.GET.get('end_date_bs', '').strip()

        # Seamless BS to AD conversion if BS dates were submitted without AD parameters
        if not start_date_ad and start_date_bs:
            try:
                parts = [int(p) for p in start_date_bs.split('-')]
                if len(parts) == 3:
                    start_date_ad = NepaliCalendar.bs_to_ad(parts[0], parts[1], parts[2]).strftime('%Y-%m-%d')
            except Exception:
                pass

        if not end_date_ad and end_date_bs:
            try:
                parts = [int(p) for p in end_date_bs.split('-')]
                if len(parts) == 3:
                    end_date_ad = NepaliCalendar.bs_to_ad(parts[0], parts[1], parts[2]).strftime('%Y-%m-%d')
            except Exception:
                pass

        filters = {
            'start_date': start_date_ad,
            'end_date': end_date_ad,
            'start_date_bs': start_date_bs,
            'end_date_bs': end_date_bs,
            'date_basis': request.GET.get('date_basis', 'bill_date').strip(),
            'branch_id': request.GET.get('branch', '') or (active_branch.id if not request.user.is_superuser else ''),
            'supplier_id': request.GET.get('supplier', ''),
            'reason': request.GET.get('reason', '').strip(),
            'q': request.GET.get('q', '').strip(),
        }

        data = CancelledPurchaseService.get_cancelled_purchase_data(filters=filters, user=request.user)
        records = data['records']
        totals = data['totals']

        # Dedicated clean CSV export named 'Cancelled_Purchases_Report.csv'
        export_mode = request.GET.get('export', '').strip().lower()
        if export_mode == 'csv':
            response = HttpResponse(content_type='text/csv; charset=utf-8')
            response['Content-Disposition'] = 'attachment; filename="Cancelled_Purchases_Report.csv"'
            writer = csv.writer(response)
            writer.writerow([
                'GRN Number', 'Supplier Bill Ref', 'Bill Date (AD)', 'Bill Date (BS)', 'Cancellation Timestamp',
                'Branch Code', 'Branch Name', 'Supplier Name', 'Supplier PAN', 'Supplier Phone',
                'Received By', 'Cancelled By User', 'IP Address', 'Cancellation Reason',
                'Gross Amount (NPR)', 'Trade Discount (NPR)', 'Input VAT (NPR)',
                'Net Invoice Total (NPR)', 'Paid Cash Reversed (NPR)', 'Due AP Reversed (NPR)',
                'Stock Reversal Executed?'
            ])
            for r in records:
                writer.writerow(sanitize_csv_row([
                    r['grn_number'], r['supplier_bill_no'], r['bill_date_ad'], r['bill_date_bs'], r['cancel_date_ad_str'],
                    r['branch_code'], r['branch_name'], r['supplier_name'], r['supplier_pan'], r['supplier_phone'],
                    r['received_by'], r['cancelled_by'], r['cancel_ip'], r['cancellation_reason'],
                    f"{r['gross_amount']:.2f}", f"{r['discount_amount']:.2f}", f"{r['vat_amount']:.2f}",
                    f"{r['net_total_amount']:.2f}", f"{r['paid_amount_reversed']:.2f}",
                    f"{r['due_amount_reversed']:.2f}", 'Yes (Confirmed Reverted)' if r['is_stock_reverted'] else 'Pending Verification'
                ]))
            writer.writerow(sanitize_csv_row([
                'TOTALS', '', '', '', '', '', f"{totals.get('total_cancelled_bills', len(records))} Voided GRNs", '', '', '', '', '', '', '',
                f"{totals.get('total_gross_amount', 0):.2f}", f"{totals.get('total_discount_amount', 0):.2f}",
                f"{totals.get('total_vat_amount', 0):.2f}", f"{totals.get('total_cancelled_amount', 0):.2f}",
                f"{totals.get('total_paid_reversed', 0):.2f}", f"{totals.get('total_due_reversed', 0):.2f}", ''
            ]))
            return response

        paginator = Paginator(records, 50)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        context = {
            'records': page_obj,
            'page_obj': page_obj,
            'is_paginated': page_obj.has_other_pages(),
            'totals': totals,
            'branches': data['branches'],
            'suppliers': data['suppliers'],
            'selected_branch': data['selected_branch'],
            'filters': filters,
            'start_date': data['start_date'],
            'end_date': data['end_date'],
            'start_date_bs': data['start_date_bs'],
            'end_date_bs': data['end_date_bs'],
            'date_basis': data.get('date_basis', 'bill_date'),
        }
        return render(request, self.template_name, context)


# ==============================================================================
# REPORT 18: FINANCIALS & HISTORICAL AUDITS
# ==============================================================================
class DailySalesReportView(ReportAccessMixin, TemplateView):
    template_name = 'reports/daily_sales.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        branch = getattr(self.request, 'active_branch', None)

        start_date_str = self.request.GET.get('start_date', '').strip()
        end_date_str = self.request.GET.get('end_date', '').strip()
        single_date_str = self.request.GET.get('date', '').strip()
        compare_period = self.request.GET.get('compare', 'false').lower() == 'true'

        today = date.today()
        if start_date_str and end_date_str:
            try:
                start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
                end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
            except ValueError:
                start_date, end_date = today, today
        elif single_date_str:
            try:
                start_date = datetime.strptime(single_date_str, '%Y-%m-%d').date()
                end_date = start_date
            except ValueError:
                start_date, end_date = today, today
        else:
            start_date, end_date = today, today

        if start_date > end_date:
            start_date, end_date = end_date, start_date

        qs = SalesEstimate.objects.filter(
            bill_date_ad__gte=start_date,
            bill_date_ad__lte=end_date,
            status__in=['COMPLETED', 'PARTIALLY_RETURNED']
        )

        selected_branch_id = self.request.GET.get('branch', '').strip()
        if selected_branch_id and (self.request.user.is_superuser or getattr(self.request.user, 'role', '') == 'OWNER'):
            qs = qs.filter(branch_id=selected_branch_id)
        elif branch and not self.request.user.is_superuser:
            qs = qs.filter(branch=branch)

        totals = qs.aggregate(
            total_sales=Coalesce(Sum('grand_total'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_subtotal=Coalesce(Sum('subtotal'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_item_disc=Coalesce(Sum('item_discount_total'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_bill_disc=Coalesce(Sum('bill_discount_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_trade_in_credits=Coalesce(Sum('trade_in_discount_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_taxable=Coalesce(Sum('taxable_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_non_taxable=Coalesce(Sum('non_taxable_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_vat=Coalesce(Sum('vat_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_cogs=Coalesce(Sum('total_cost_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_profit=Coalesce(Sum('total_gross_profit'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_cash=Coalesce(Sum('paid_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_due=Coalesce(Sum('due_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            bill_count=Count('id')
        )

        returns_qs = SalesReturn.objects.filter(original_estimate__in=qs)
        total_refunds = returns_qs.aggregate(
            total_refund=Coalesce(Sum('total_refund_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2)))
        )['total_refund']

        gross_sales = totals['total_sales']
        net_sales = max(Decimal('0.00'), gross_sales - total_refunds)
        total_vat = totals['total_vat']
        net_cogs = totals['total_cogs']

        net_merchandise_revenue = max(Decimal('0.00'), net_sales - total_vat + totals['total_trade_in_credits'])
        realized_profit = max(Decimal('0.00'), net_merchandise_revenue - net_cogs)
        margin_percent = (
            ((realized_profit / net_merchandise_revenue) * Decimal('100.00')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
            if net_merchandise_revenue > Decimal('0.00') else Decimal('0.0')
        )

        comparison_data = None
        if compare_period:
            period_length = (end_date - start_date).days + 1
            prev_end = start_date - timedelta(days=1)
            prev_start = prev_end - timedelta(days=period_length - 1)

            prev_qs = SalesEstimate.objects.filter(
                bill_date_ad__gte=prev_start,
                bill_date_ad__lte=prev_end,
                status__in=['COMPLETED', 'PARTIALLY_RETURNED']
            )
            if selected_branch_id and (self.request.user.is_superuser or getattr(self.request.user, 'role', '') == 'OWNER'):
                prev_qs = prev_qs.filter(branch_id=selected_branch_id)
            elif branch and not self.request.user.is_superuser:
                prev_qs = prev_qs.filter(branch=branch)

            prev_totals = prev_qs.aggregate(
                total_sales=Coalesce(Sum('grand_total'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
                bill_count=Count('id')
            )
            prev_returns = SalesReturn.objects.filter(original_estimate__in=prev_qs).aggregate(
                total_refund=Coalesce(Sum('total_refund_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2)))
            )['total_refund']

            prev_net_sales = max(Decimal('0.00'), prev_totals['total_sales'] - prev_returns)
            prev_bill_count = prev_totals['bill_count']

            if prev_net_sales > Decimal('0.00'):
                growth_pct = (((net_sales - prev_net_sales) / prev_net_sales) * Decimal('100.00')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
            else:
                growth_pct = Decimal('100.0') if net_sales > Decimal('0.00') else Decimal('0.0')

            comparison_data = {
                'prev_start': prev_start.strftime('%Y-%m-%d'),
                'prev_end': prev_end.strftime('%Y-%m-%d'),
                'prev_sales': prev_net_sales,
                'prev_bill_count': prev_bill_count,
                'growth_pct': growth_pct,
            }

        page_num = self.request.GET.get('page', 1)
        paginator = Paginator(
            qs.select_related('customer', 'cashier', 'salesperson', 'branch', 'manager_override_by').order_by('-created_at'),
            50
        )
        page_obj = paginator.get_page(page_num)

        totals_summary = {
            'sales': net_sales,
            'gross_sales': gross_sales,
            'total_refunds': total_refunds,
            'subtotal': totals['total_subtotal'],
            'item_discount_total': totals['total_item_disc'],
            'bill_discount_total': totals['total_bill_disc'],
            'total_sales_discounts': totals['total_item_disc'] + totals['total_bill_disc'],
            'total_trade_in_credits': totals['total_trade_in_credits'],
            'taxable': totals['total_taxable'],
            'non_taxable': totals['total_non_taxable'],
            'vat': total_vat,
            'net_revenue': net_merchandise_revenue,
            'cogs': net_cogs,
            'gross_profit': realized_profit,
            'margin_percent': margin_percent,
            'cash': totals['total_cash'],
            'due': totals['total_due'],
            'count': totals['bill_count'],
        }

        context.update({
            'estimates': page_obj,
            'page_obj': page_obj,
            'is_paginated': page_obj.has_other_pages(),
            'totals': totals_summary,
            'start_date': start_date.strftime('%Y-%m-%d'),
            'end_date': end_date.strftime('%Y-%m-%d'),
            'start_date_bs': ad_to_bs_string(start_date, lang='en'),
            'end_date_bs': ad_to_bs_string(end_date, lang='en'),
            'is_single_day': start_date == end_date,
            'compare_period': compare_period,
            'comparison_data': comparison_data,
            'branches': Branch.objects.filter(is_active=True).order_by('name'),
        })
        return context


class InventoryValuationReportView(ReportAccessMixin, ListView):
    model = BranchStock
    template_name = 'reports/inventory_valuation.html'
    context_object_name = 'stocks'
    paginate_by = 50

    def get_queryset(self):
        branch = getattr(self.request, 'active_branch', None)
        qs = BranchStock.objects.select_related(
            'product', 'product__category', 'product__brand', 'product__base_unit', 'branch'
        ).filter(quantity__gt=Decimal('0.000'))

        if branch and not self.request.user.is_superuser:
            qs = qs.filter(branch=branch)

        q = self.request.GET.get('q', '').strip()
        cat_id = self.request.GET.get('category', '').strip()
        if q:
            qs = qs.filter(
                Q(product__name__icontains=q) |
                Q(product__sku__icontains=q) |
                Q(product__barcode__icontains=q)
            )
        if cat_id and cat_id != 'all':
            qs = qs.filter(product__category_id=cat_id)

        cost_expression = ExpressionWrapper(
            F('quantity') * F('product__purchase_price'),
            output_field=DecimalField(max_digits=18, decimal_places=2)
        )
        retail_expression = ExpressionWrapper(
            F('quantity') * F('product__selling_price'),
            output_field=DecimalField(max_digits=18, decimal_places=2)
        )

        return qs.annotate(
            cost_valuation=cost_expression,
            retail_valuation=retail_expression
        ).order_by('product__name')

    def post(self, request, *args, **kwargs):
        branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()
        today = timezone.now().date()

        base_qs = BranchStock.objects.select_related('product').filter(quantity__gt=Decimal('0.000'))
        if branch and not request.user.is_superuser:
            base_qs = base_qs.filter(branch=branch)

        cost_expression = ExpressionWrapper(
            F('quantity') * F('product__purchase_price'),
            output_field=DecimalField(max_digits=18, decimal_places=2)
        )
        retail_expression = ExpressionWrapper(
            F('quantity') * F('product__selling_price'),
            output_field=DecimalField(max_digits=18, decimal_places=2)
        )

        summary = base_qs.aggregate(
            total_units=Coalesce(Sum('quantity'), Value(Decimal('0.000'), output_field=DecimalField(max_digits=18, decimal_places=3))),
            total_cost=Coalesce(Sum(cost_expression), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_retail=Coalesce(Sum(retail_expression), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2)))
        )

        total_cost_val = summary['total_cost']
        total_retail_val = summary['total_retail']
        projected_margin = max(Decimal('0.00'), total_retail_val - total_cost_val)

        snapshot = InventoryValuationSnapshot.objects.create(
            branch=branch,
            snapshot_date=today,
            snapshot_date_bs=ad_to_bs_string(today, lang='en'),
            total_units_count=summary['total_units'],
            total_cost_valuation=total_cost_val,
            total_retail_valuation=total_retail_val,
            projected_margin=projected_margin,
            generated_by=request.user,
            notes=f"Audit valuation snapshot locked on {today} by {request.user.username}"
        )

        AuditLog.objects.create(
            user=request.user,
            branch=branch,
            action_type='CREATE',
            module='InventoryValuationSnapshot',
            object_repr=f"Valuation Snapshot for {today}",
            details={
                'cost_valuation': str(total_cost_val),
                'retail_valuation': str(total_retail_val),
                'total_units': str(summary['total_units']),
                'projected_margin': str(projected_margin)
            }
        )

        messages.success(request, f"Valuation audit snapshot locked successfully for {today} (Rs. {total_cost_val:,.2f} Landed Cost).")
        return redirect('reports:inventory_valuation')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        branch = getattr(self.request, 'active_branch', None)

        base_qs = BranchStock.objects.select_related('product').filter(quantity__gt=Decimal('0.000'))
        if branch and not self.request.user.is_superuser:
            base_qs = base_qs.filter(branch=branch)

        cost_expression = ExpressionWrapper(
            F('quantity') * F('product__purchase_price'),
            output_field=DecimalField(max_digits=18, decimal_places=2)
        )
        retail_expression = ExpressionWrapper(
            F('quantity') * F('product__selling_price'),
            output_field=DecimalField(max_digits=18, decimal_places=2)
        )

        summary = base_qs.aggregate(
            total_units=Coalesce(Sum('quantity'), Value(Decimal('0.000'), output_field=DecimalField(max_digits=18, decimal_places=3))),
            total_cost=Coalesce(Sum(cost_expression), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_retail=Coalesce(Sum(retail_expression), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2)))
        )

        total_cost_val = summary['total_cost']
        total_retail_val = summary['total_retail']
        projected_profit = max(Decimal('0.00'), total_retail_val - total_cost_val)
        avg_margin_pct = (
            ((projected_profit / total_retail_val) * Decimal('100.00')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
            if total_retail_val > Decimal('0.00') else Decimal('0.0')
        )

        context.update({
            'categories': ProductCategory.objects.filter(is_active=True).order_by('name'),
            'total_units_count': summary['total_units'],
            'total_cost': total_cost_val,
            'total_retail': total_retail_val,
            'projected_profit': projected_profit,
            'average_margin_percent': avg_margin_pct,
            'selected_branch': branch,
        })
        return context


class CustomerUdhaariReportView(ReportAccessMixin, TemplateView):
    template_name = 'reports/customer_udhaari.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        debt_customers = Customer.objects.filter(
            current_credit_balance__gt=Decimal('0.00')
        ).order_by('-current_credit_balance')

        total_debt = debt_customers.aggregate(
            sum_debt=Coalesce(Sum('current_credit_balance'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2)))
        )['sum_debt']

        context.update({
            'customers': debt_customers,
            'total_outstanding': total_debt,
        })
        return context


class ProductPriceHistoryReportView(ReportAccessMixin, ListView):
    model = ProductCostHistory
    template_name = 'reports/price_history.html'
    context_object_name = 'history_records'
    paginate_by = 40

    def get_queryset(self):
        qs = ProductCostHistory.objects.select_related(
            'product', 'product__category', 'product__brand', 'product__base_unit', 'changed_by'
        )

        q = self.request.GET.get('q', '').strip()
        cat_id = self.request.GET.get('category', '').strip()
        brand_id = self.request.GET.get('brand', '').strip()
        start_date = self.request.GET.get('start_date', '').strip()
        end_date = self.request.GET.get('end_date', '').strip()

        if q:
            qs = qs.filter(
                Q(product__name__icontains=q) |
                Q(product__sku__icontains=q) |
                Q(product__barcode__icontains=q) |
                Q(source_reference__icontains=q) |
                Q(remarks__icontains=q)
            )

        if cat_id and cat_id != 'all':
            qs = qs.filter(product__category_id=cat_id)

        if brand_id and brand_id != 'all':
            qs = qs.filter(product__brand_id=brand_id)

        if start_date:
            qs = qs.filter(date_effective__gte=start_date)

        if end_date:
            qs = qs.filter(date_effective__lte=end_date)

        return qs.order_by('-date_effective', '-created_at')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        base_qs = self.get_queryset()

        total_changes = base_qs.count()
        cost_increases = base_qs.filter(new_cost_price__gt=F('old_cost_price')).count()
        cost_decreases = base_qs.filter(new_cost_price__lt=F('old_cost_price')).count()
        distinct_products = base_qs.values('product_id').distinct().count()

        context.update({
            'total_changes': total_changes,
            'cost_increases': cost_increases,
            'cost_decreases': cost_decreases,
            'distinct_products': distinct_products,
            'categories': ProductCategory.objects.filter(is_active=True).order_by('name'),
            'brands': Brand.objects.all().order_by('name'),
            'filters': self.request.GET,
            'start_date': self.request.GET.get('start_date', ''),
            'end_date': self.request.GET.get('end_date', ''),
        })
        return context


# ==============================================================================
# UNIVERSAL CSV EXPORTER DISPATCHER
# ==============================================================================
class ExportReportCSVView(ReportAccessMixin, View):
    """
    Central export dispatcher routing CSV downloads across all report domains.
    Hardened against CSV Formula Injection (CWE-1236).
    """
    def get(self, request, report_type):
        active_branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()

        if report_type in ['sales', 'sales_items', 'sales-items', 'sales_itemized', 'sales-itemized']:
            start_str = request.GET.get('start_date', '').strip()
            end_str = request.GET.get('end_date', '').strip()
            qs = SalesEstimate.objects.filter(status__in=['COMPLETED', 'PARTIALLY_RETURNED'])

            if active_branch and not request.user.is_superuser:
                qs = qs.filter(branch=active_branch)

            if start_str and end_str:
                qs = qs.filter(bill_date_ad__gte=start_str, bill_date_ad__lte=end_str)

            if 'item' in report_type:
                return CSVExportEngine.export_sales_items_csv(qs)
            return CSVExportEngine.export_sales_csv(qs)

        elif report_type in ['cancelled_sales', 'cancelled-sales', 'cancelled_bills', 'cancelled-bills']:
            filters = {
                'start_date': request.GET.get('start_date', ''),
                'end_date': request.GET.get('end_date', ''),
                'branch_id': request.GET.get('branch', '') or (active_branch.id if not request.user.is_superuser else ''),
                'cashier_id': request.GET.get('cashier', ''),
                'reason': request.GET.get('reason', '').strip(),
                'q': request.GET.get('q', '').strip(),
                'date_basis': request.GET.get('date_basis', 'bill_date').strip(),
            }
            res = CancelledSalesService.get_cancelled_sales_data(filters=filters, user=request.user)
            if hasattr(CSVExportEngine, 'export_cancelled_sales_csv'):
                return CSVExportEngine.export_cancelled_sales_csv(
                    records=res['records'],
                    totals=res['totals'],
                    filename="Cancelled_Sales_Report.csv"
                )

        elif report_type in ['cancelled_purchases', 'cancelled-purchases', 'cancelled_grn', 'cancelled-grn']:
            filters = {
                'start_date': request.GET.get('start_date', ''),
                'end_date': request.GET.get('end_date', ''),
                'branch_id': request.GET.get('branch', '') or (active_branch.id if not request.user.is_superuser else ''),
                'supplier_id': request.GET.get('supplier', ''),
                'reason': request.GET.get('reason', '').strip(),
                'q': request.GET.get('q', '').strip(),
                'date_basis': request.GET.get('date_basis', 'bill_date').strip(),
            }
            res = CancelledPurchaseService.get_cancelled_purchase_data(filters=filters, user=request.user)
            records = res['records']
            totals = res['totals']
            response = HttpResponse(content_type='text/csv; charset=utf-8')
            response['Content-Disposition'] = 'attachment; filename="Cancelled_Purchases_Report.csv"'
            writer = csv.writer(response)
            writer.writerow([
                'GRN Number', 'Supplier Bill Ref', 'Bill Date (AD)', 'Bill Date (BS)', 'Cancellation Timestamp',
                'Branch Code', 'Branch Name', 'Supplier Name', 'Supplier PAN', 'Supplier Phone',
                'Received By', 'Cancelled By User', 'IP Address', 'Cancellation Reason',
                'Gross Amount (NPR)', 'Trade Discount (NPR)', 'Input VAT (NPR)',
                'Net Invoice Total (NPR)', 'Paid Cash Reversed (NPR)', 'Due AP Reversed (NPR)',
                'Stock Reversal Executed?'
            ])
            for r in records:
                writer.writerow(sanitize_csv_row([
                    r['grn_number'], r['supplier_bill_no'], r['bill_date_ad'], r['bill_date_bs'], r['cancel_date_ad_str'],
                    r['branch_code'], r['branch_name'], r['supplier_name'], r['supplier_pan'], r['supplier_phone'],
                    r['received_by'], r['cancelled_by'], r['cancel_ip'], r['cancellation_reason'],
                    f"{r['gross_amount']:.2f}", f"{r['discount_amount']:.2f}", f"{r['vat_amount']:.2f}",
                    f"{r['net_total_amount']:.2f}", f"{r['paid_amount_reversed']:.2f}",
                    f"{r['due_amount_reversed']:.2f}", 'Yes (Confirmed Reverted)' if r['is_stock_reverted'] else 'Pending Verification'
                ]))
            writer.writerow(sanitize_csv_row([
                'TOTALS', '', '', '', '', '', f"{totals.get('total_cancelled_bills', len(records))} Voided GRNs", '', '', '', '', '', '', '',
                f"{totals.get('total_gross_amount', 0):.2f}", f"{totals.get('total_discount_amount', 0):.2f}",
                f"{totals.get('total_vat_amount', 0):.2f}", f"{totals.get('total_cancelled_amount', 0):.2f}",
                f"{totals.get('total_paid_reversed', 0):.2f}", f"{totals.get('total_due_reversed', 0):.2f}", ''
            ]))
            return response

        elif report_type == 'imei_stock':
            filters = {
                'branch': request.GET.get('branch', '') or (active_branch.id if not request.user.is_superuser else ''),
                'brand': request.GET.get('brand', ''),
                'mdms_status': request.GET.get('mdms_status', '').strip(),
                'imei2_status': request.GET.get('imei2_status', '').strip(),
                'condition': request.GET.get('condition', '').strip(),
                'q': request.GET.get('q', '').strip(),
            }
            res = IMEIStockService.get_imei_stock_data(filters=filters, user=request.user)
            return CSVExportEngine.export_imei_stock_csv(queryset=res['queryset'])

        elif report_type in ['transfers', 'stock_transfers']:
            filters = {
                'source_branch': request.GET.get('source_branch', ''),
                'destination_branch': request.GET.get('destination_branch', ''),
                'status': request.GET.get('status', '').strip(),
                'start_date': request.GET.get('start_date', ''),
                'end_date': request.GET.get('end_date', ''),
                'q': request.GET.get('q', '').strip(),
            }
            res = StockTransferReportService.get_transfer_report(filters=filters, user=request.user)
            return CSVExportEngine.export_stock_transfers_csv(queryset=res['queryset'])

        elif report_type == 'low_stock':
            filters = {
                'branch': request.GET.get('branch', '') or (active_branch.id if not request.user.is_superuser else ''),
                'category': request.GET.get('category', ''),
                'brand': request.GET.get('brand', ''),
                'q': request.GET.get('q', '').strip(),
            }
            res = LowStockService.get_low_stock_data(filters=filters, user=request.user)
            return CSVExportEngine.export_low_stock_csv(items_data=res['records'])

        elif report_type == 'out_of_stock':
            filters = {
                'branch': request.GET.get('branch', '') or (active_branch.id if not request.user.is_superuser else ''),
                'category': request.GET.get('category', ''),
                'brand': request.GET.get('brand', ''),
                'q': request.GET.get('q', '').strip(),
            }
            res = OutOfStockService.get_out_of_stock_data(filters=filters, user=request.user)
            return CSVExportEngine.export_out_of_stock_csv(items_data=res['records'])

        elif report_type == 'adjustments':
            filters = {
                'branch': request.GET.get('branch', '') or (active_branch.id if not request.user.is_superuser else ''),
                'type': request.GET.get('type', '').strip(),
                'category': request.GET.get('category', ''),
                'brand': request.GET.get('brand', ''),
                'start_date': request.GET.get('start_date', ''),
                'end_date': request.GET.get('end_date', ''),
                'q': request.GET.get('q', '').strip(),
            }
            res = StockAdjustmentService.get_adjustment_data(filters=filters, user=request.user)
            return CSVExportEngine.export_stock_adjustments_csv(res['queryset'])

        elif report_type == 'aging':
            filters = {
                'branch': request.GET.get('branch', '') or (active_branch.id if not request.user.is_superuser else ''),
                'category': request.GET.get('category', ''),
                'brand': request.GET.get('brand', ''),
                'bracket': request.GET.get('bracket', '').strip(),
                'item_type': request.GET.get('item_type', '').strip(),
                'q': request.GET.get('q', '').strip(),
            }
            res = StockAgingService.get_stock_aging_data(filters=filters, user=request.user)
            return CSVExportEngine.export_stock_aging_csv(res['records'], res['totals'])

        elif report_type == 'trade_in':
            filters = {
                'branch': request.GET.get('branch', '') or (active_branch.id if not request.user.is_superuser else ''),
                'status': request.GET.get('status', '').strip(),
                'grade': request.GET.get('grade', '') or request.GET.get('condition_grade', ''),
                'mdms_status': request.GET.get('mdms_status', '').strip(),
                'undertaking': request.GET.get('undertaking', '').strip(),
                'q': request.GET.get('q', '').strip(),
            }
            res = TradeInInventoryService.get_trade_in_inventory_data(filters=filters, user=request.user)
            return CSVExportEngine.export_trade_in_inventory_csv(res['records'], res['totals'])

        elif report_type == 'valuation':
            return CSVExportEngine.export_stock_valuation_csv(branch=active_branch)

        elif report_type == 'udhaari':
            return CSVExportEngine.export_customer_udhaari_csv()

        elif report_type == 'price_history':
            return CSVExportEngine.export_price_history_csv()

        elif report_type in ['purchase_register', 'purchase-register', 'purchase_book']:
            filters = {
                'start_date': request.GET.get('start_date', ''),
                'end_date': request.GET.get('end_date', ''),
                'branch_id': request.GET.get('branch', '') or (active_branch.id if not request.user.is_superuser else ''),
                'supplier_id': request.GET.get('supplier', ''),
                'tax_mode': request.GET.get('tax_mode', '').strip(),
                'q': request.GET.get('q', '').strip(),
            }
            res = PurchaseRegisterService.get_purchase_register_data(filters=filters, user=request.user)
            if hasattr(CSVExportEngine, 'export_purchase_register_csv'):
                return CSVExportEngine.export_purchase_register_csv(res['records'], res['totals'])

        elif report_type in ['supplier_purchases', 'supplier-purchases', 'supplier_purchase']:
            filters = {
                'start_date': request.GET.get('start_date', ''),
                'end_date': request.GET.get('end_date', ''),
                'branch_id': request.GET.get('branch', '') or (active_branch.id if not request.user.is_superuser else ''),
                'supplier_id': request.GET.get('supplier', ''),
                'q': request.GET.get('q', '').strip(),
            }
            res = SupplierPurchaseService.get_supplier_purchase_data(filters=filters, user=request.user)
            if hasattr(CSVExportEngine, 'export_supplier_purchases_csv'):
                return CSVExportEngine.export_supplier_purchases_csv(res['records'], res['totals'])

        elif report_type in ['product_purchases', 'product-purchases', 'product_purchase']:
            filters = {
                'start_date': request.GET.get('start_date', ''),
                'end_date': request.GET.get('end_date', ''),
                'branch_id': request.GET.get('branch', '') or (active_branch.id if not request.user.is_superuser else ''),
                'category_id': request.GET.get('category', ''),
                'brand_id': request.GET.get('brand', ''),
                'supplier_id': request.GET.get('supplier', ''),
                'tracking_type': request.GET.get('tracking_type', '').strip(),
                'q': request.GET.get('q', '').strip(),
            }
            res = ProductPurchaseService.get_product_purchase_data(filters=filters, user=request.user)
            if hasattr(CSVExportEngine, 'export_product_purchases_csv'):
                return CSVExportEngine.export_product_purchases_csv(res['records'], res['totals'])

        elif report_type in ['supplier_outstanding', 'supplier-outstanding', 'supplier_payables']:
            filters = {
                'start_date': request.GET.get('start_date', ''),
                'end_date': request.GET.get('end_date', ''),
                'branch_id': request.GET.get('branch', '') or (active_branch.id if not request.user.is_superuser else ''),
                'supplier_type': request.GET.get('supplier_type', '').strip(),
                'status': request.GET.get('status', 'due').strip(),
                'q': request.GET.get('q', '').strip(),
            }
            res = SupplierOutstandingService.get_supplier_outstanding_data(filters=filters, user=request.user)
            if hasattr(CSVExportEngine, 'export_supplier_outstanding_csv'):
                return CSVExportEngine.export_supplier_outstanding_csv(res['records'], res['totals'])

        return redirect('reports:daily_sales')