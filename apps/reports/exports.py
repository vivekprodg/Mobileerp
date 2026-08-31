import csv
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, List
from django.http import HttpResponse
from django.db.models import Sum

from apps.sales.models import SalesEstimate
from apps.inventory.models import BranchStock, Product, ProductBatch, ItemInstance
from apps.customers.models import Customer
from apps.reports.models import ProductCostHistory


def sanitize_csv_cell(val: Any) -> str:
    """
    Sanitizes values written to CSV to prevent CSV / Formula Injection attacks (CWE-1236).
    If a cell value begins with formula characters ('=', '+', '-', '@', '\t', '\r'),
    it prepends a single quote (') so spreadsheet applications (Excel, LibreOffice)
    render it strictly as inert text rather than executing dynamic formulas or DDE commands.
    """
    if val is None:
        return ""

    val_str = str(val)
    # Check for formula prefixes after stripping leading whitespace
    stripped = val_str.lstrip()
    if stripped and stripped[0] in ('=', '+', '-', '@', '\t', '\r'):
        return f"'{val_str}"
    return val_str


def sanitize_csv_row(row: List[Any]) -> List[str]:
    """Applies CSV formula sanitization across all elements of a row."""
    return [sanitize_csv_cell(cell) for cell in row]


class CSVExportEngine:
    """
    High-Precision Business, Inventory, and Tax CSV Export Engine.
    Generates downloadable reports with hardened formula-injection protection,
    separate columns for Gross Amount, Item Discounts, Taxable Base, Output VAT,
    Landed Cost (COGS), Net Revenue, and Realized Gross Profit.
    """

    @staticmethod
    def export_sales_csv(queryset, filename: str = "Sales_Estimates_Detailed.csv") -> HttpResponse:
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        headers = [
            'Estimate No',
            'Date (AD)',
            'Date (BS)',
            'Branch',
            'Customer Name',
            'Customer Phone',
            'Customer PAN',
            'Cashier',
            'Salesperson',
            'Gross Subtotal (NPR)',
            'Total Discount (NPR)',
            'Taxable Base (NPR)',
            'Non-Taxable Base (NPR)',
            'Tax / VAT Amount (NPR)',
            'Grand Total (NPR)',
            'Cost of Goods Sold (COGS) (NPR)',
            'Net Revenue Excl. Tax (NPR)',
            'Gross Profit Realized (NPR)',
            'Gross Margin %',
            'Paid Amount (NPR)',
            'Due Udhaari (NPR)',
            'Payment Status',
            'Bill Status'
        ]
        writer.writerow(headers)

        for est in queryset.select_related('customer', 'branch', 'cashier', 'salesperson'):
            gross = est.subtotal
            disc = est.item_discount_total + est.bill_discount_amount
            taxable = est.taxable_amount
            non_taxable = est.non_taxable_amount
            vat = est.vat_amount
            grand = est.grand_total
            cogs = est.total_cost_amount

            # Net Revenue = Grand Total minus VAT (for VAT bills) or Grand Total (for PAN/non-tax bills)
            net_rev = max(Decimal('0.00'), grand - vat)
            gross_profit = est.total_gross_profit if est.total_gross_profit is not None else (net_rev - cogs)
            margin_pct = (
                ((gross_profit / net_rev) * Decimal('100.00')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
                if net_rev > Decimal('0.00') else Decimal('0.0')
            )

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
                f"{gross:.2f}",
                f"{disc:.2f}",
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

        return response

    @staticmethod
    def export_stock_valuation_csv(branch=None, filename: str = "Stock_Valuation_Accurate.csv") -> HttpResponse:
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        headers = [
            'SKU',
            'Barcode',
            'Product / Handset Name',
            'Category',
            'Brand',
            'Branch',
            'Stock Qty',
            'Unit',
            'Tracking Type',
            'Tax Mode',
            'Active Selling MRP (NPR)',
            'Total Inward Landed Cost Valuation (NPR)',
            'Total Retail Valuation (NPR)',
            'Projected Realizable Profit (NPR)',
            'Projected Margin %'
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

            # 1. Serialized Smartphone Valuation (Actual Inward Landed Costs)
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

            # 2. Non-Serialized Multi-Batch Valuation (FIFO Batch Ledger)
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
                prod.sku,
                prod.barcode,
                prod.name,
                prod.category.name if prod.category else '',
                prod.brand.name if prod.brand else '',
                s.branch.name,
                f"{qty:.3f}",
                prod.base_unit.code if prod.base_unit else 'PCS',
                tracking_str,
                prod.get_tax_pricing_type_display() if hasattr(prod, 'get_tax_pricing_type_display') else prod.tax_pricing_type,
                f"{prod.selling_price:.2f}",
                f"{cost_val:.2f}",
                f"{retail_val:.2f}",
                f"{projected_profit:.2f}",
                f"{margin_pct:.1f}%"
            ]
            writer.writerow(sanitize_csv_row(raw_row))

        return response

    @staticmethod
    def export_customer_udhaari_csv(filename: str = "Customer_Udhaari_Book.csv") -> HttpResponse:
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        headers = [
            'Customer Name',
            'Mobile Number',
            'PAN Number',
            'Customer Type',
            'Credit Limit (NPR)',
            'Outstanding Udhaari Balance (NPR)',
            'Lifetime Spent (NPR)',
            'Status'
        ]
        writer.writerow(headers)

        customers = Customer.objects.filter(current_credit_balance__gt=Decimal('0.00')).order_by('-current_credit_balance')
        for c in customers:
            raw_row = [
                c.name,
                c.phone_number,
                c.pan_number or '',
                c.get_customer_type_display(),
                f"{c.credit_limit:.2f}",
                f"{c.current_credit_balance:.2f}",
                f"{c.total_spent:.2f}",
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
            'Effective Date',
            'Product Name',
            'SKU',
            'Old Cost Price (NPR)',
            'New Cost Price (NPR)',
            'Cost Delta (NPR)',
            'Old Selling MRP (NPR)',
            'New Selling MRP (NPR)',
            'Source Reference',
            'Changed By',
            'Remarks'
        ]
        writer.writerow(headers)

        history = ProductCostHistory.objects.select_related('product', 'changed_by').all().order_by('-date_effective', '-created_at')
        for h in history:
            cost_diff = h.new_cost_price - h.old_cost_price
            raw_row = [
                h.date_effective,
                h.product.name,
                h.product.sku,
                f"{h.old_cost_price:.2f}",
                f"{h.new_cost_price:.2f}",
                f"{cost_diff:+.2f}",
                f"{h.old_selling_price:.2f}",
                f"{h.new_selling_price:.2f}",
                h.source_reference or '',
                h.changed_by.username if h.changed_by else 'System',
                h.remarks or ''
            ]
            writer.writerow(sanitize_csv_row(raw_row))

        return response