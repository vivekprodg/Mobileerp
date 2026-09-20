"""
Report Domain Business Logic Services Package.
File Path: apps/reports/services/__init__.py

Exposes specialized calculation engines for:
1. Core Historical Inventory & Ledger (Summary, Bin Card, Snapshots, Low/Zero Stock, Valuation)
2. Serialized Phone & IMEI Intelligence (Live IMEI, Single-IMEI Lifecycle, Pre-Owned Trade-In)
3. Stock Movements, Logistics & Claims (Transfers, Adjustments, Damaged Quarantine, Vendor RMA)
4. Inventory Aging & FIFO Batches (Stock Aging, Slow-Moving, Batch Stock)
5. Comprehensive Purchase Reports (Purchase Register, Supplier Purchases, Product Purchases, Supplier Outstanding)
6. Sales & Turnover Intelligence Suite (Sales Summary, Detailed Items, Product Sales, Brand, Category,
   Sold IMEIs, Salesperson, Cashier, Tender Modes, Discount Overrides, Trade-In Settlement,
   Gross Profit & Margin Tiers, Top/Slow Velocity, and Cancelled Void Audit)
"""

# -----------------------------------------------------------------------------
# 1. CORE HISTORICAL STOCK & LEDGER SERVICES
# -----------------------------------------------------------------------------
from apps.reports.services.stock_summary_service import StockSummaryService
from apps.reports.services.stock_detail_service import StockDetailService
from apps.reports.services.current_stock_service import CurrentStockService
from apps.reports.services.low_stock_service import LowStockService
from apps.reports.services.out_of_stock_service import OutOfStockService

# -----------------------------------------------------------------------------
# 2. SERIALIZED SMARTPHONE & IMEI INTELLIGENCE SERVICES
# -----------------------------------------------------------------------------
from apps.reports.services.imei_stock_service import IMEIStockService
from apps.reports.services.imei_lifecycle_service import IMEILifecycleService
from apps.reports.services.trade_in_inventory_service import TradeInInventoryService

# -----------------------------------------------------------------------------
# 3. MOVEMENTS, LOGISTICS, QUALITY & RMA SERVICES
# -----------------------------------------------------------------------------
from apps.reports.services.stock_transfer_report_service import StockTransferReportService
from apps.reports.services.stock_adjustment_service import StockAdjustmentService
from apps.reports.services.damaged_stock_service import DamagedStockService
from apps.reports.services.vendor_rma_report_service import VendorRMAReportService

# -----------------------------------------------------------------------------
# 4. STOCK INTELLIGENCE, AGING & FIFO BATCHES
# -----------------------------------------------------------------------------
from apps.reports.services.stock_aging_service import StockAgingService
from apps.reports.services.slow_moving_service import SlowMovingStockService
from apps.reports.services.batch_stock_service import BatchStockService

# -----------------------------------------------------------------------------
# 5. PURCHASE DOMAIN REPORTING SERVICES
# -----------------------------------------------------------------------------
from apps.reports.services.purchase_register_service import PurchaseRegisterService
from apps.reports.services.supplier_purchase_service import SupplierPurchaseService
from apps.reports.services.product_purchase_service import ProductPurchaseService
from apps.reports.services.supplier_outstanding_service import SupplierOutstandingService

# -----------------------------------------------------------------------------
# 6. SALES & TURNOVER INTELLIGENCE SERVICES
# -----------------------------------------------------------------------------
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

__all__ = [
    # 1. Core Stock
    'StockSummaryService',
    'StockDetailService',
    'CurrentStockService',
    'LowStockService',
    'OutOfStockService',

    # 2. IMEI & Trade-In
    'IMEIStockService',
    'IMEILifecycleService',
    'TradeInInventoryService',

    # 3. Movements & RMA
    'StockTransferReportService',
    'StockAdjustmentService',
    'DamagedStockService',
    'VendorRMAReportService',

    # 4. Aging & Batches
    'StockAgingService',
    'SlowMovingStockService',
    'BatchStockService',

    # 5. Purchases Suite
    'PurchaseRegisterService',
    'SupplierPurchaseService',
    'ProductPurchaseService',
    'SupplierOutstandingService',

    # 6. Sales & Turnover Suite
    'SalesSummaryService',
    'ItemSalesService',
    'ProductSalesService',
    'BrandSalesService',
    'CategorySalesService',
    'IMEISalesService',
    'SalespersonSalesService',
    'CashierSalesService',
    'PaymentMethodSalesService',
    'DiscountOverrideSalesService',
    'TradeInSalesService',
    'GrossProfitSalesService',
    'TopSlowSalesService',
    'CancelledSalesService',
]