"""
Salesperson Performance & Commission Intelligence Service.
File Path: apps/reports/services/salesperson_sales_service.py

Capabilities:
1. Evaluates sales counter staff productivity, sales volume, and gross profit generation.
2. Distinguishes high-value smartphone units sold (IMEI-tracked) from accessory units sold.
3. Groups sales estimates by salesperson_id (falling back to cashier if unassigned).
4. Computes total invoices handled, gross sales, discounts given, net turnover, COGS,
   gross profit contribution, average ticket/basket size, and overall margin %.
"""

from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime
from typing import Dict, Any, List, Tuple, Optional

from django.db.models import (
    Q, Sum, Count, DecimalField, Value, Case, When, F
)
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.sales.models import SalesEstimate, SalesEstimateItem
from apps.branches.models import Branch
from apps.users.models import User
from apps.core.nepali_calendar import NepaliCalendar


class SalespersonSalesService:
    """
    Business logic engine for Report 7: Salesperson-Wise Sales Report.
    """

    @classmethod
    def resolve_date_range(cls, raw_params: Dict[str, Any]) -> Tuple[date, date, str, str]:
        """Resolves date boundaries defaulting to current Nepali BS month."""
        today_ad = timezone.now().date()
        start_str = str(raw_params.get('start_date', '') or '').strip()
        end_str = str(raw_params.get('end_date', '') or '').strip()

        start_date = None
        end_date = None

        if start_str:
            try:
                start_date = datetime.strptime(start_str, '%Y-%m-%d').date()
            except (ValueError, TypeError):
                start_date = None

        if end_str:
            try:
                end_date = datetime.strptime(end_str, '%Y-%m-%d').date()
            except (ValueError, TypeError):
                end_date = None

        if not start_date or not end_date:
            bs_year, bs_month, _ = NepaliCalendar.ad_to_bs(today_ad)
            start_of_bs_month = NepaliCalendar.bs_to_ad(bs_year, bs_month, 1)
            start_date = start_date or start_of_bs_month
            end_date = end_date or today_ad

        if start_date > end_date:
            start_date, end_date = end_date, start_date

        start_y, start_m, start_d = NepaliCalendar.ad_to_bs(start_date)
        end_y, end_m, end_d = NepaliCalendar.ad_to_bs(end_date)

        start_date_bs = NepaliCalendar.format_bs(start_y, start_m, start_d, lang='en')
        end_date_bs = NepaliCalendar.format_bs(end_y, end_m, end_d, lang='en')

        return start_date, end_date, start_date_bs, end_date_bs

    @classmethod
    def get_salesperson_sales_data(
        cls,
        filters: Dict[str, Any],
        user=None
    ) -> Dict[str, Any]:
        """
        Groups sales estimates and items by salesperson and aggregates turnover, units, and margins.
        """
        active_branch = getattr(user, 'assigned_branch', None) if user and not user.is_superuser else None

        start_date, end_date, start_date_bs, end_date_bs = cls.resolve_date_range(filters)
        branch_id = filters.get('branch_id') or filters.get('branch', '')
        salesperson_id = filters.get('salesperson_id') or filters.get('salesperson', '')
        search_query = str(filters.get('q', '') or '').strip()

        # 1. Base Query: Only completed and partially returned sales estimates
        estimates_qs = SalesEstimate.objects.filter(
            status__in=['COMPLETED', 'PARTIALLY_RETURNED'],
            bill_date_ad__gte=start_date,
            bill_date_ad__lte=end_date
        )

        # 2. Scoping
        if branch_id and str(branch_id).strip() not in ['', 'all', 'None']:
            estimates_qs = estimates_qs.filter(branch_id=branch_id)
        elif active_branch and not (user.is_superuser or getattr(user, 'role', '') == 'OWNER'):
            estimates_qs = estimates_qs.filter(branch=active_branch)

        if salesperson_id and str(salesperson_id).strip() not in ['', 'all', 'None']:
            estimates_qs = estimates_qs.filter(
                Q(salesperson_id=salesperson_id) | (Q(salesperson__isnull=True) & Q(cashier_id=salesperson_id))
            )

        if search_query:
            estimates_qs = estimates_qs.filter(
                Q(salesperson__first_name__icontains=search_query) |
                Q(salesperson__last_name__icontains=search_query) |
                Q(salesperson__username__icontains=search_query) |
                Q(cashier__first_name__icontains=search_query) |
                Q(cashier__username__icontains=search_query)
            )

        # 3. Aggregate Estimate Totals grouped by salesperson_id (fallback to cashier_id if null)
        estimates_grouped = estimates_qs.annotate(
            effective_rep_id=Coalesce(F('salesperson_id'), F('cashier_id'))
        ).values('effective_rep_id').annotate(
            invoice_count=Count('id'),
            gross_subtotal=Coalesce(Sum('subtotal'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            item_discounts=Coalesce(Sum('item_discount_total'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            bill_discounts=Coalesce(Sum('bill_discount_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            trade_in_credits=Coalesce(Sum('trade_in_discount_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            vat_amount=Coalesce(Sum('vat_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            grand_total=Coalesce(Sum('grand_total'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_cogs=Coalesce(Sum('total_cost_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_profit=Coalesce(Sum('total_gross_profit'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            paid_amount=Coalesce(Sum('paid_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            due_amount=Coalesce(Sum('due_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2)))
        )

        rep_stats_map = {row['effective_rep_id']: row for row in estimates_grouped}
        rep_ids = [k for k in rep_stats_map.keys() if k is not None]

        # 4. Aggregate Quantity: Phone Units (IMEI) vs Accessory Units
        items_qs = SalesEstimateItem.objects.filter(estimate__in=estimates_qs).annotate(
            effective_rep_id=Coalesce(F('estimate__salesperson_id'), F('estimate__cashier_id'))
        ).values('effective_rep_id').annotate(
            total_units=Coalesce(Sum('quantity'), Value(Decimal('0.000'), output_field=DecimalField(max_digits=18, decimal_places=3))),
            phone_units=Coalesce(
                Sum(Case(When(product__requires_imei_tracking=True, then=F('quantity')))),
                Value(Decimal('0.000'), output_field=DecimalField(max_digits=18, decimal_places=3))
            ),
            accessory_units=Coalesce(
                Sum(Case(When(product__requires_imei_tracking=False, then=F('quantity')))),
                Value(Decimal('0.000'), output_field=DecimalField(max_digits=18, decimal_places=3))
            )
        )
        units_map = {row['effective_rep_id']: row for row in items_qs}

        # 5. Resolve User Details
        users_qs = User.objects.select_related('assigned_branch').filter(id__in=rep_ids)
        user_info_map = {
            u.id: {
                'name': u.get_full_name() or u.username,
                'username': u.username,
                'role': u.get_role_display(),
                'branch_name': u.assigned_branch.name if u.assigned_branch else 'Unassigned'
            }
            for u in users_qs
        }

        # 6. Build Detailed Records
        records: List[Dict[str, Any]] = []

        total_invoices_all = 0
        total_phones_all = Decimal('0.000')
        total_accessories_all = Decimal('0.000')
        total_units_all = Decimal('0.000')
        total_gross_all = Decimal('0.00')
        total_discounts_all = Decimal('0.00')
        total_net_all = Decimal('0.00')
        total_cogs_all = Decimal('0.00')
        total_profit_all = Decimal('0.00')
        total_paid_all = Decimal('0.00')
        total_due_all = Decimal('0.00')

        for rep_id, est_data in rep_stats_map.items():
            u_info = user_info_map.get(rep_id, {
                'name': f"User #{rep_id}",
                'username': f"user_{rep_id}",
                'role': 'Staff',
                'branch_name': '-'
            })

            u_data = units_map.get(rep_id, {})
            p_units = u_data.get('phone_units', Decimal('0.000'))
            a_units = u_data.get('accessory_units', Decimal('0.000'))
            tot_units = u_data.get('total_units', Decimal('0.000'))

            inv_cnt = est_data.get('invoice_count', 0)
            gross = est_data.get('gross_subtotal', Decimal('0.00'))
            it_disc = est_data.get('item_discounts', Decimal('0.00'))
            b_disc = est_data.get('bill_discounts', Decimal('0.00'))
            tot_disc = it_disc + b_disc
            tr_credit = est_data.get('trade_in_credits', Decimal('0.00'))
            vat = est_data.get('vat_amount', Decimal('0.00'))
            grand = est_data.get('grand_total', Decimal('0.00'))
            cogs = est_data.get('total_cogs', Decimal('0.00'))
            profit = est_data.get('total_profit', Decimal('0.00'))
            paid = est_data.get('paid_amount', Decimal('0.00'))
            due = est_data.get('due_amount', Decimal('0.00'))

            net_merch_rev = max(Decimal('0.00'), grand - vat + tr_credit)
            margin_pct = (
                ((profit / net_merch_rev) * Decimal('100.00')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
                if net_merch_rev > Decimal('0.00') else Decimal('0.0')
            )

            # Average Basket / Ticket Size
            avg_basket = (
                (grand / Decimal(str(inv_cnt))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                if inv_cnt > 0 else Decimal('0.00')
            )

            total_invoices_all += inv_cnt
            total_phones_all += p_units
            total_accessories_all += a_units
            total_units_all += tot_units
            total_gross_all += gross
            total_discounts_all += tot_disc
            total_net_all += grand
            total_cogs_all += cogs
            total_profit_all += profit
            total_paid_all += paid
            total_due_all += due

            records.append({
                'salesperson_id': rep_id,
                'name': u_info['name'],
                'username': u_info['username'],
                'role': u_info['role'],
                'branch_name': u_info['branch_name'],
                'invoices_count': inv_cnt,
                'phone_units_sold': p_units,
                'accessory_units_sold': a_units,
                'total_units_sold': tot_units,
                'gross_sales': gross,
                'total_discounts': tot_disc,
                'net_turnover': grand,
                'total_cogs': cogs,
                'gross_profit': profit,
                'margin_percent': margin_pct,
                'avg_basket_size': avg_basket,
                'paid_amount': paid,
                'due_amount': due,
            })

        # Sort by net turnover descending
        records.sort(key=lambda x: x['net_turnover'], reverse=True)

        overall_margin_pct = (
            ((total_profit_all / total_net_all) * Decimal('100.00')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
            if total_net_all > Decimal('0.00') else Decimal('0.0')
        )
        overall_avg_basket = (
            (total_net_all / Decimal(str(total_invoices_all))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            if total_invoices_all > 0 else Decimal('0.00')
        )

        totals = {
            'salespeople_count': len(records),
            'total_invoices': total_invoices_all,
            'total_phones_sold': total_phones_all,
            'total_accessories_sold': total_accessories_all,
            'total_units_sold': total_units_all,
            'total_gross_sales': total_gross_all,
            'total_discounts_given': total_discounts_all,
            'total_net_turnover': total_net_all,
            'total_cogs': total_cogs_all,
            'total_gross_profit': total_profit_all,
            'overall_margin_pct': overall_margin_pct,
            'overall_avg_basket': overall_avg_basket,
            'total_paid': total_paid_all,
            'total_due': total_due_all,
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
            'salespeople': User.objects.filter(is_active=True).order_by('username'),
            'selected_branch': selected_branch,
            'filters': filters,
            'start_date': start_date.strftime('%Y-%m-%d'),
            'end_date': end_date.strftime('%Y-%m-%d'),
            'start_date_bs': start_date_bs,
            'end_date_bs': end_date_bs,
        }