from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from django.shortcuts import render, redirect
from django.views.generic import TemplateView, View, ListView
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.db.models import Sum, Count, F, Q, DecimalField, Value, ExpressionWrapper
from django.db.models.functions import Coalesce
from django.core.paginator import Paginator
from django.contrib import messages

from apps.sales.models import SalesEstimate, SalesEstimateItem, SalesReturn, SalesReturnItem
from apps.inventory.models import BranchStock, Product, ProductCategory, ProductBatch, ItemInstance
from apps.customers.models import Customer
from apps.purchases.models import Supplier, GoodsReceivedNote
from apps.reports.models import InventoryValuationSnapshot, ProductCostHistory, ScheduledReportLog
from apps.reports.exports import CSVExportEngine
from apps.core.models import SystemConfiguration
from apps.core.utils.nepali_date_converter import ad_to_bs_string


class DailySalesReportView(LoginRequiredMixin, TemplateView):
    """
    Comprehensive Sales & Gross Profit Realization Analytics.
    Includes both COMPLETED and PARTIALLY_RETURNED sales estimates, accurately
    deducting return refunds and returned cost-of-goods-sold (COGS) so that retained
    items remain visible and revenue, taxes, and profit reflect true net performance.
    """
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

        # Query both COMPLETED and PARTIALLY_RETURNED bills to prevent invoice vanishing
        qs = SalesEstimate.objects.filter(
            bill_date_ad__gte=start_date,
            bill_date_ad__lte=end_date,
            status__in=['COMPLETED', 'PARTIALLY_RETURNED']
        )
        if branch and not self.request.user.is_superuser:
            qs = qs.filter(branch=branch)

        totals = qs.aggregate(
            total_sales=Coalesce(Sum('grand_total'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_subtotal=Coalesce(Sum('subtotal'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_item_disc=Coalesce(Sum('item_discount_total'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_bill_disc=Coalesce(Sum('bill_discount_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_taxable=Coalesce(Sum('taxable_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_non_taxable=Coalesce(Sum('non_taxable_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_vat=Coalesce(Sum('vat_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_cogs=Coalesce(Sum('total_cost_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_profit=Coalesce(Sum('total_gross_profit'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_cash=Coalesce(Sum('paid_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_due=Coalesce(Sum('due_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            bill_count=Count('id')
        )

        # Aggregate refunds and returned COGS for partially returned bills in this scope
        returns_qs = SalesReturn.objects.filter(original_estimate__in=qs)
        returns_aggregate = returns_qs.aggregate(
            total_refund=Coalesce(Sum('total_refund_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2)))
        )
        total_refunds = returns_aggregate['total_refund']

        returned_cogs_aggregate = SalesReturnItem.objects.filter(
            sales_return__original_estimate__in=qs
        ).aggregate(
            returned_cogs=Coalesce(
                Sum(
                    ExpressionWrapper(
                        F('base_unit_quantity') * F('estimate_item__cost_price'),
                        output_field=DecimalField(max_digits=18, decimal_places=2)
                    )
                ),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            )
        )
        total_returned_cogs = returned_cogs_aggregate['returned_cogs']

        # Calculate Net figures accurately accounting for returns
        gross_sales = totals['total_sales']
        net_sales = max(Decimal('0.00'), gross_sales - total_refunds)
        total_vat = totals['total_vat']
        net_cogs = max(Decimal('0.00'), totals['total_cogs'] - total_returned_cogs)
        total_discount = totals['total_item_disc'] + totals['total_bill_disc']

        net_revenue = max(Decimal('0.00'), net_sales - total_vat)
        realized_profit = max(Decimal('0.00'), net_revenue - net_cogs)
        margin_percent = (
            ((realized_profit / net_revenue) * Decimal('100.00')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
            if net_revenue > Decimal('0.00') else Decimal('0.0')
        )

        comparison_data = None
        if compare_period:
            period_days = (end_date - start_date).days + 1
            prev_end = start_date - timedelta(days=1)
            prev_start = prev_end - timedelta(days=period_days - 1)

            prev_qs = SalesEstimate.objects.filter(
                bill_date_ad__gte=prev_start,
                bill_date_ad__lte=prev_end,
                status__in=['COMPLETED', 'PARTIALLY_RETURNED']
            )
            if branch and not self.request.user.is_superuser:
                prev_qs = prev_qs.filter(branch=branch)

            prev_totals = prev_qs.aggregate(
                total_sales=Coalesce(Sum('grand_total'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
                total_cogs=Coalesce(Sum('total_cost_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
                total_vat=Coalesce(Sum('vat_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
                bill_count=Count('id')
            )

            prev_returns = SalesReturn.objects.filter(original_estimate__in=prev_qs).aggregate(
                total_refund=Coalesce(Sum('total_refund_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2)))
            )['total_refund']

            prev_returned_cogs = SalesReturnItem.objects.filter(
                sales_return__original_estimate__in=prev_qs
            ).aggregate(
                returned_cogs=Coalesce(
                    Sum(
                        ExpressionWrapper(
                            F('base_unit_quantity') * F('estimate_item__cost_price'),
                            output_field=DecimalField(max_digits=18, decimal_places=2)
                        )
                    ),
                    Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
                )
            )['returned_cogs']

            prev_net_sales = max(Decimal('0.00'), prev_totals['total_sales'] - prev_returns)
            prev_net_revenue = max(Decimal('0.00'), prev_net_sales - prev_totals['total_vat'])
            prev_net_cogs = max(Decimal('0.00'), prev_totals['total_cogs'] - prev_returned_cogs)
            prev_realized_profit = max(Decimal('0.00'), prev_net_revenue - prev_net_cogs)

            sales_growth_pct = (
                (((net_sales - prev_net_sales) / prev_net_sales) * Decimal('100.00')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
                if prev_net_sales > Decimal('0.00') else Decimal('0.0')
            )

            comparison_data = {
                'prev_start': prev_start,
                'prev_end': prev_end,
                'prev_sales': prev_net_sales,
                'prev_profit': prev_realized_profit,
                'prev_bill_count': prev_totals['bill_count'],
                'growth_pct': sales_growth_pct,
            }

        page_num = self.request.GET.get('page', 1)
        paginator = Paginator(
            qs.select_related('customer', 'cashier', 'salesperson', 'branch').prefetch_related('items__product').order_by('-created_at'),
            50
        )
        page_obj = paginator.get_page(page_num)

        formatted_start = start_date.strftime('%Y-%m-%d')
        formatted_end = end_date.strftime('%Y-%m-%d')

        context.update({
            'estimates': page_obj,
            'page_obj': page_obj,
            'is_paginated': page_obj.has_other_pages(),
            'totals': {
                'sales': net_sales,
                'gross_sales': gross_sales,
                'total_refunds': total_refunds,
                'subtotal': totals['total_subtotal'],
                'discount': total_discount,
                'taxable': totals['total_taxable'],
                'non_taxable': totals['total_non_taxable'],
                'vat': total_vat,
                'net_revenue': net_revenue,
                'cogs': net_cogs,
                'gross_profit': realized_profit,
                'margin_percent': margin_percent,
                'cash': totals['total_cash'],
                'due': totals['total_due'],
                'count': totals['bill_count']
            },
            'selected_date': formatted_start,
            'start_date': formatted_start,
            'end_date': formatted_end,
            'start_date_bs': ad_to_bs_string(start_date, lang='en'),
            'end_date_bs': ad_to_bs_string(end_date, lang='en'),
            'is_single_day': start_date == end_date,
            'comparison_data': comparison_data,
            'compare_period': compare_period,
        })
        return context


class InventoryValuationReportView(LoginRequiredMixin, UserPassesTestMixin, ListView):
    """
    Database-Aggregated Inventory Asset Valuation & Projected Margin Analysis.
    Uses ExpressionWrapper with explicit DecimalField output types to guarantee precision across all SQL backends.
    Annotates row-level cost and retail valuations for direct, accurate template rendering.
    """
    model = BranchStock
    template_name = 'reports/inventory_valuation.html'
    context_object_name = 'stocks'
    paginate_by = 50

    def test_func(self):
        return self.request.user.is_superuser or getattr(self.request.user, 'role', '') in ['OWNER', 'MANAGER', 'ACCOUNTANT']

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
        })
        return context

    def post(self, request, *args, **kwargs):
        """Locks a historical valuation snapshot using explicit Decimal expression types."""
        branch = getattr(request, 'active_branch', None)
        if not branch:
            messages.error(request, "Active branch context required to lock valuation snapshot.")
            return redirect('reports:inventory_valuation')

        base_qs = BranchStock.objects.filter(branch=branch, quantity__gt=Decimal('0.000')).select_related('product')

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

        today = date.today()
        bs_date_str = ad_to_bs_string(today, lang='en')
        projected_margin = summary['total_retail'] - summary['total_cost']

        snapshot = InventoryValuationSnapshot.objects.create(
            branch=branch,
            snapshot_date=today,
            snapshot_date_bs=bs_date_str,
            total_units_count=summary['total_units'],
            total_cost_valuation=summary['total_cost'],
            total_retail_valuation=summary['total_retail'],
            projected_margin=projected_margin,
            generated_by=request.user,
            notes=f"Audit valuation snapshot locked by {request.user.username} for {branch.name}"
        )

        messages.success(request, f"Valuation snapshot for {today} (Cost: Rs. {snapshot.total_cost_valuation:.2f}) successfully locked.")
        return redirect('reports:inventory_valuation')


class CustomerUdhaariReportView(LoginRequiredMixin, TemplateView):
    """Consolidated Customer Credit (Udhaari) Ledger with debt metrics."""
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


class ExportReportCSVView(LoginRequiredMixin, View):
    """Unified CSV export endpoint."""

    def get(self, request, report_type):
        branch = getattr(request, 'active_branch', None)

        if report_type == 'sales':
            start_str = request.GET.get('start_date', '')
            end_str = request.GET.get('end_date', '')
            single_str = request.GET.get('date', '')
            qs = SalesEstimate.objects.filter(status__in=['COMPLETED', 'PARTIALLY_RETURNED'])
            if branch and not request.user.is_superuser:
                qs = qs.filter(branch=branch)
            if start_str and end_str:
                qs = qs.filter(bill_date_ad__gte=start_str, bill_date_ad__lte=end_str)
            elif single_str:
                qs = qs.filter(bill_date_ad=single_str)
            return CSVExportEngine.export_sales_csv(qs)

        elif report_type == 'valuation':
            return CSVExportEngine.export_stock_valuation_csv(branch=branch)

        elif report_type == 'udhaari':
            return CSVExportEngine.export_customer_udhaari_csv()

        elif report_type == 'price_history':
            return CSVExportEngine.export_price_history_csv()

        return redirect('reports:daily_sales')