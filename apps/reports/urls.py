"""
Report URLs Configuration.

Exposes clean, intuitive URL endpoints under the 'reports:' namespace for:
1. Sales & Commercial Turnover Intelligence Suite (15 Specialized Reports)
2. Core Historical Inventory & Stock Ledgers (Summary, Bin Card, Snapshots, Valuations)
3. Serialized Smartphone & IMEI Intelligence (Live Stock, Single-IMEI Lifecycle, Trade-In)
4. Logistics, Movements & Quality Assurance (Transfers, Adjustments, Damaged, Vendor RMA)
5. Stock Intelligence & Aging (FIFO Batches, 4-Bucket Aging, Dead/Slow Moving)
6. Purchase Domain Procurement Suite (Purchase Register, Supplier/Product Outlay, Payables, Cancelled Purchases)
7. Financial, Debt & Price Audit History (Daily Sales, Customer Udhaari, Cost Fluctuation)
8. Universal CSV Export Dispatcher (Formula Injection Hardened)
"""

from django.urls import path
from apps.reports.views import (
    # -------------------------------------------------------------------------
    # 1. SALES & COMMERCIAL TURNOVER INTELLIGENCE REPORTS
    # -------------------------------------------------------------------------
    SalesSummaryReportView,
    ItemWiseSalesReportView,
    ProductWiseSalesReportView,
    BrandWiseSalesReportView,
    CategoryWiseSalesReportView,
    SoldIMEIRegistryReportView,
    SalespersonSalesReportView,
    CashierSalesReportView,
    PaymentMethodSalesReportView,
    DiscountOverrideReportView,
    TradeInExchangeSalesReportView,
    GrossProfitMarginReportView,
    TopSellingSalesReportView,
    SlowSellingSalesReportView,
    CancelledSalesAuditReportView,

    # -------------------------------------------------------------------------
    # 2. CORE HISTORICAL INVENTORY & LEDGER REPORTS
    # -------------------------------------------------------------------------
    StockSummaryReportView,
    StockDetailReportView,
    CurrentStockReportView,
    LowStockReportView,
    OutOfStockReportView,
    InventoryValuationReportView,

    # -------------------------------------------------------------------------
    # 3. SERIALIZED SMARTPHONE & IMEI INTELLIGENCE REPORTS
    # -------------------------------------------------------------------------
    IMEIStockReportView,
    IMEILifecycleReportView,
    TradeInInventoryReportView,

    # -------------------------------------------------------------------------
    # 4. MOVEMENTS, LOGISTICS & QUALITY ASSURANCE REPORTS
    # -------------------------------------------------------------------------
    StockTransferReportView,
    StockAdjustmentReportView,
    DamagedStockReportView,
    VendorRMAReportView,

    # -------------------------------------------------------------------------
    # 5. STOCK AGING & INVENTORY INTELLIGENCE REPORTS
    # -------------------------------------------------------------------------
    StockAgingReportView,
    SlowMovingStockReportView,
    BatchStockReportView,

    # -------------------------------------------------------------------------
    # 6. PURCHASE DOMAIN REPORTING SUITE
    # -------------------------------------------------------------------------
    PurchaseRegisterReportView,
    SupplierPurchaseReportView,
    ProductPurchaseReportView,
    SupplierOutstandingReportView,
    CancelledPurchasesReportView,

    # -------------------------------------------------------------------------
    # 7. FINANCIAL, LEDGER & AUDIT REPORTS
    # -------------------------------------------------------------------------
    DailySalesReportView,
    CustomerUdhaariReportView,
    ProductPriceHistoryReportView,

    # -------------------------------------------------------------------------
    # 8. UNIVERSAL CSV EXPORT DISPATCHER
    # -------------------------------------------------------------------------
    ExportReportCSVView,
)

app_name = 'reports'

urlpatterns = [
    # =========================================================================
    # 1. SALES & COMMERCIAL TURNOVER INTELLIGENCE REPORTS (PART A)
    # =========================================================================
    # Date-Wise Sales Summary Rollup (Turnover, Discounts, Collections, Gross Profit)
    path('sales-summary/', SalesSummaryReportView.as_view(), name='sales_summary'),

    # Line-Item Level Detailed Sales (Gross, Item/Bill Concessions, VAT, COGS, Net Profit)
    path('item-sales/', ItemWiseSalesReportView.as_view(), name='item_sales'),

    # Product-Level Volume, Unit Margins, and On-Hand Counter Inventory
    path('product-sales/', ProductWiseSalesReportView.as_view(), name='product_sales'),

    # Brand-Wise Volume (Handsets vs Accessories), Net Sales, and Turnover Share %
    path('brand-sales/', BrandWiseSalesReportView.as_view(), name='brand_sales'),

    # Departmental Revenue, Lines Billed, and Margin Contribution % by Category
    path('category-sales/', CategoryWiseSalesReportView.as_view(), name='category_sales'),

    # Sold Handset Registry (Dual-IMEI, Customer Ownership, MDMS, and Active Warranties)
    path('sold-imei/', SoldIMEIRegistryReportView.as_view(), name='sold_imei'),

    # Counter Staff & Sales Representative Volume, Average Basket Size, and Turnover
    path('salesperson-sales/', SalespersonSalesReportView.as_view(), name='salesperson_sales'),

    # Cashier Accountability, Tender Reconciliation, Drawer Float, and Return Refunds
    path('cashier-sales/', CashierSalesReportView.as_view(), name='cashier_sales'),

    # Tender Channel Collections (Cash, FonePay, eSewa, Khalti, Card, Bank, Udhaari)
    path('payment-methods/', PaymentMethodSalesReportView.as_view(), name='payment_methods'),

    # Discount & Supervisor Price Override Concession Audit (Amount vs %, Manager PIN)
    path('discounts-overrides/', DiscountOverrideReportView.as_view(), name='discount_overrides'),

    # Old Phone Trade-In / Exchange Settlement (New Phone Sold vs Old Phone Credit)
    path('trade-in-sales/', TradeInExchangeSalesReportView.as_view(), name='trade_in_sales'),

    # Gross Profit & 4-Tier Margin Realization (High, Medium, Slim, Negative)
    path('gross-profit/', GrossProfitMarginReportView.as_view(), name='gross_profit'),

    # Top-Selling Products Ranked by Volume (Quantity Sold) & Revenue
    path('top-selling/', TopSellingSalesReportView.as_view(), name='top_selling'),

    # Slow-Moving / Dormant Sales Velocity with Tied-Up Capital Calculations
    path('slow-selling/', SlowSellingSalesReportView.as_view(), name='slow_selling'),

    # Anti-Fraud Voided & Cancelled Sales Invoices Audit with Stock Reversal Confirmation (Part A)
    path('cancelled-sales/', CancelledSalesAuditReportView.as_view(), name='cancelled_sales'),

    # =========================================================================
    # 2. CORE HISTORICAL INVENTORY & LEDGER REPORTS
    # =========================================================================
    # 21-Column Historical Stock Summary Report (Opening, Movements, Closing, Valuations)
    path('stock-summary/', StockSummaryReportView.as_view(), name='stock_summary'),

    # 10-Column Item-Specific Stock Detail Running Ledger (Bin Card / वस्तुको मौज्दात खाता)
    path('stock-detail/', StockDetailReportView.as_view(), name='stock_detail'),

    # Real-Time Current Stock Balance Snapshot (Live Warehouse & Showcase Balances)
    path('current-stock/', CurrentStockReportView.as_view(), name='current_stock'),

    # Low Stock & Automated Reorder Alert Intelligence
    path('low-stock/', LowStockReportView.as_view(), name='low_stock'),

    # Out of Stock / Zero Inventory Critical Report with Depletion Duration
    path('out-of-stock/', OutOfStockReportView.as_view(), name='out_of_stock'),

    # Inventory Asset Valuation & Historical Audit Snapshots (Landed Cost vs Retail MRP)
    path('inventory-valuation/', InventoryValuationReportView.as_view(), name='inventory_valuation'),

    # =========================================================================
    # 3. SERIALIZED SMARTPHONE & IMEI INTELLIGENCE REPORTS
    # =========================================================================
    # Live Active IMEI 1 & IMEI 2 Stock Registry with NTA MDMS Compliance Status
    path('imei-stock/', IMEIStockReportView.as_view(), name='imei_stock'),

    # Forensic Single-IMEI Lifecycle Journey & Audit History (Cradle-to-Grave Timeline)
    path('imei-lifecycle/', IMEILifecycleReportView.as_view(), name='imei_lifecycle'),

    # Pre-Owned / Second-Hand Traded-in Phone Inventory, Condition Grades & Margins
    path('trade-in-inventory/', TradeInInventoryReportView.as_view(), name='trade_in_inventory'),

    # =========================================================================
    # 4. MOVEMENTS, LOGISTICS & QUALITY ASSURANCE REPORTS
    # =========================================================================
    # Inter-Branch Stock Transfers & Consignment Transit Tracking (Dispatched vs Received)
    path('stock-transfers/', StockTransferReportView.as_view(), name='stock_transfers'),

    # Manual Stock Adjustments, Count Corrections & Damage Write-Offs
    path('stock-adjustments/', StockAdjustmentReportView.as_view(), name='stock_adjustments'),

    # Quarantined Damaged & Defective Stock Report (Pending Vendor RMA Claim)
    path('damaged-stock/', DamagedStockReportView.as_view(), name='damaged_stock'),

    # Vendor RMA & Distributor Warranty Claim Resolutions (Replacements vs Credit Notes)
    path('vendor-rma/', VendorRMAReportView.as_view(), name='vendor_rma'),

    # =========================================================================
    # 5. STOCK AGING & INVENTORY INTELLIGENCE REPORTS
    # =========================================================================
    # Stock Aging Analysis across 4 Brackets (0-30, 31-60, 61-90, 90+ Days)
    path('stock-aging/', StockAgingReportView.as_view(), name='stock_aging'),

    # Dead & Slow-Moving Stock Analysis (No Sales in 30, 60, 90, 120, 180 Days)
    path('slow-moving/', SlowMovingStockReportView.as_view(), name='slow_moving'),

    # Batch-Wise FIFO Inward Stock Report (Accessories & Spare Parts)
    path('batch-stock/', BatchStockReportView.as_view(), name='batch_stock'),

    # =========================================================================
    # 6. PURCHASE DOMAIN REPORTING SUITE (PART B)
    # =========================================================================
    # Commercial Purchase Register (Inward Purchase Book / Annex 7 Format)
    path('purchase-register/', PurchaseRegisterReportView.as_view(), name='purchase_register'),

    # Supplier-Wise Purchase Turnover, Net Spend & Returns Summary
    path('supplier-purchases/', SupplierPurchaseReportView.as_view(), name='supplier_purchases'),

    # Product-Wise & Handset Purchase Volume, Rates & Supplier Sources
    path('product-purchases/', ProductPurchaseReportView.as_view(), name='product_purchases'),

    # Supplier Outstanding (Udhaari) & Accounts Payable Aging Report
    path('supplier-outstanding/', SupplierOutstandingReportView.as_view(), name='supplier_outstanding'),

    # Forensic Anti-Fraud Audit of Cancelled & Voided Purchase Bills (GRN Voiding - Part B)
    path('cancelled-purchases/', CancelledPurchasesReportView.as_view(), name='cancelled_purchases'),

    # =========================================================================
    # 7. FINANCIAL, LEDGER & AUDIT REPORTS
    # =========================================================================
    # Daily Sales Turnover, Returns Deductions, Landed COGS & Gross Profit Realization
    path('daily-sales/', DailySalesReportView.as_view(), name='daily_sales'),

    # Customer Udhaari (Credit) Debt Book & Aging
    path('customer-udhaari/', CustomerUdhaariReportView.as_view(), name='customer_udhaari'),

    # Product Cost & Counter MRP Price Fluctuation Audit Log
    path('price-history/', ProductPriceHistoryReportView.as_view(), name='price_history'),

    # =========================================================================
    # 8. UNIVERSAL CSV EXPORT DISPATCHER
    # =========================================================================
    path('export/<str:report_type>/', ExportReportCSVView.as_view(), name='export_csv'),
]