"""
Cashier-Wise Collection & Settlement Audit Service.
File Path: apps/reports/services/cashier_sales_service.py

Capabilities:
1. Audits counter cashier accountability: Total invoices billed, gross turnover,
   and net payable amounts.
2. Breaks down collections by tender mode: Cash Tendered, FonePay QR, eSewa,
   Khalti, POS Card Swipes, Bank Transfers, and Authorized Customer Udhaari (Credit).
3. Reconciles physical cash refunds processed by the cashier on customer sales returns.
4. Audits cash change returned on bills to verify physical drawer float balance.
"""

from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime
from typing import Dict, Any, List, Tuple, Optional

from django.db.models import (
    Q, Sum, Count, DecimalField, Value, Case, When, F
)
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.sales.models import SalesEstimate, SalesPaymentTransaction, SalesReturn
from apps.branches.models import Branch
from apps.users.models import User
from apps.core.nepali_calendar import NepaliCalendar


class CashierSalesService:
    """
    Business logic engine for Report 8: Cashier-Wise Sales & Collection Report.
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
    def get_cashier_sales_data(
        cls,
        filters: Dict[str, Any],
        user=None
    ) -> Dict[str, Any]:
        """
        Groups sales transactions by cashier and analyzes collections, payment modes, and return refunds.
        """
        active_branch = getattr(user, 'assigned_branch', None) if user and not user.is_superuser else None

        start_date, end_date, start_date_bs, end_date_bs = cls.resolve_date_range(filters)
        branch_id = filters.get('branch_id') or filters.get('branch', '')
        cashier_id = filters.get('cashier_id') or filters.get('cashier', '')
        search_query = str(filters.get('q', '') or '').strip()

        # 1. Base Query: Completed sales estimates
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

        if cashier_id and str(cashier_id).strip() not in ['', 'all', 'None']:
            estimates_qs = estimates_qs.filter(cashier_id=cashier_id)

        if search_query:
            estimates_qs = estimates_qs.filter(
                Q(cashier__first_name__icontains=search_query) |
                Q(cashier__last_name__icontains=search_query) |
                Q(cashier__username__icontains=search_query)
            )

        # 3. Aggregate Invoices & Discrepancies by Cashier
        cashier_stats = estimates_qs.values('cashier_id').annotate(
            invoice_count=Count('id'),
            gross_subtotal=Coalesce(Sum('subtotal'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            item_discounts=Coalesce(Sum('item_discount_total'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            bill_discounts=Coalesce(Sum('bill_discount_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            net_turnover=Coalesce(Sum('grand_total'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            paid_sum=Coalesce(Sum('paid_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            due_sum=Coalesce(Sum('due_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            change_returned_sum=Coalesce(Sum('change_returned'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2)))
        )
        c_stats_map = {row['cashier_id']: row for row in cashier_stats}
        c_ids = list(c_stats_map.keys())

        # 4. Aggregate Payments Tendered by Payment Mode per Cashier
        payments_qs = SalesPaymentTransaction.objects.filter(
            estimate__in=estimates_qs
        ).values('estimate__cashier_id').annotate(
            cash_tendered=Coalesce(
                Sum(Case(When(payment_mode='CASH', then=F('amount')))),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
            fonepay_sum=Coalesce(
                Sum(Case(When(payment_mode='FONEPAY', then=F('amount')))),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
            esewa_sum=Coalesce(
                Sum(Case(When(payment_mode='ESEWA', then=F('amount')))),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
            khalti_sum=Coalesce(
                Sum(Case(When(payment_mode='KHALTI', then=F('amount')))),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
            card_sum=Coalesce(
                Sum(Case(When(payment_mode='CARD', then=F('amount')))),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
            bank_sum=Coalesce(
                Sum(Case(When(payment_mode='BANK_TRANSFER', then=F('amount')))),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
            credit_sum=Coalesce(
                Sum(Case(When(payment_mode='CREDIT', then=F('amount')))),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            )
        )
        pay_map = {row['estimate__cashier_id']: row for row in payments_qs}

        # 5. Query Returns Processed by Cashier in this period
        returns_qs = SalesReturn.objects.filter(
            processed_by_id__in=c_ids,
            created_at__date__gte=start_date,
            created_at__date__lte=end_date
        ).values('processed_by_id').annotate(
            returns_count=Count('id'),
            cash_refund_sum=Coalesce(
                Sum(Case(When(refund_mode='CASH', then=F('total_refund_amount')))),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            ),
            total_refund_sum=Coalesce(
                Sum('total_refund_amount'),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            )
        )
        ret_map = {row['processed_by_id']: row for row in returns_qs}

        # 6. Resolve User Details
        users_qs = User.objects.select_related('assigned_branch').filter(id__in=c_ids)
        user_info_map = {
            u.id: {
                'name': u.get_full_name() or u.username,
                'username': u.username,
                'branch_name': u.assigned_branch.name if u.assigned_branch else 'Unassigned'
            }
            for u in users_qs
        }

        # 7. Construct Records
        records: List[Dict[str, Any]] = []

        total_invoices_all = 0
        total_gross_all = Decimal('0.00')
        total_discounts_all = Decimal('0.00')
        total_net_all = Decimal('0.00')
        total_cash_net_all = Decimal('0.00')
        total_fonepay_all = Decimal('0.00')
        total_esewa_all = Decimal('0.00')
        total_khalti_all = Decimal('0.00')
        total_card_all = Decimal('0.00')
        total_bank_all = Decimal('0.00')
        total_credit_all = Decimal('0.00')
        total_refunds_all = Decimal('0.00')

        for c_id, c_data in c_stats_map.items():
            u_info = user_info_map.get(c_id, {
                'name': f"Cashier #{c_id}",
                'username': f"user_{c_id}",
                'branch_name': '-'
            })

            p_data = pay_map.get(c_id, {})
            r_data = ret_map.get(c_id, {})

            inv_cnt = c_data.get('invoice_count', 0)
            gross = c_data.get('gross_subtotal', Decimal('0.00'))
            it_disc = c_data.get('item_discounts', Decimal('0.00'))
            b_disc = c_data.get('bill_discounts', Decimal('0.00'))
            tot_disc = it_disc + b_disc
            net = c_data.get('net_turnover', Decimal('0.00'))
            change_given = c_data.get('change_returned_sum', Decimal('0.00'))

            raw_cash = p_data.get('cash_tendered', Decimal('0.00'))
            cash_net = max(Decimal('0.00'), raw_cash - change_given)

            fonepay = p_data.get('fonepay_sum', Decimal('0.00'))
            esewa = p_data.get('esewa_sum', Decimal('0.00'))
            khalti = p_data.get('khalti_sum', Decimal('0.00'))
            digital_total = fonepay + esewa + khalti

            card = p_data.get('card_sum', Decimal('0.00'))
            bank = p_data.get('bank_sum', Decimal('0.00'))
            credit = p_data.get('credit_sum', Decimal('0.00'))

            returns_cnt = r_data.get('returns_count', 0)
            cash_refunds = r_data.get('cash_refund_sum', Decimal('0.00'))
            total_refunds = r_data.get('total_refund_sum', Decimal('0.00'))

            total_invoices_all += inv_cnt
            total_gross_all += gross
            total_discounts_all += tot_disc
            total_net_all += net
            total_cash_net_all += cash_net
            total_fonepay_all += fonepay
            total_esewa_all += esewa
            total_khalti_all += khalti
            total_card_all += card
            total_bank_all += bank
            total_credit_all += credit
            total_refunds_all += total_refunds

            records.append({
                'cashier_id': c_id,
                'name': u_info['name'],
                'username': u_info['username'],
                'branch_name': u_info['branch_name'],
                'invoices_count': inv_cnt,
                'gross_subtotal': gross,
                'total_discounts': tot_disc,
                'net_turnover': net,
                'cash_net_retained': cash_net,
                'change_given': change_given,
                'digital_total': digital_total,
                'fonepay_collected': fonepay,
                'esewa_collected': esewa,
                'khalti_collected': khalti,
                'card_collected': card,
                'bank_collected': bank,
                'credit_authorized': credit,
                'returns_count': returns_cnt,
                'cash_refunds_issued': cash_refunds,
                'total_refunds_issued': total_refunds,
            })

        # Sort by net turnover descending
        records.sort(key=lambda x: x['net_turnover'], reverse=True)

        totals = {
            'cashiers_count': len(records),
            'total_invoices': total_invoices_all,
            'total_gross_subtotal': total_gross_all,
            'total_discounts_given': total_discounts_all,
            'total_net_turnover': total_net_all,
            'total_cash_net': total_cash_net_all,
            'total_digital': total_fonepay_all + total_esewa_all + total_khalti_all,
            'total_fonepay': total_fonepay_all,
            'total_esewa': total_esewa_all,
            'total_khalti': total_khalti_all,
            'total_card': total_card_all,
            'total_bank': total_bank_all,
            'total_credit': total_credit_all,
            'total_refunds_issued': total_refunds_all,
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
            'cashiers': User.objects.filter(is_active=True).order_by('username'),
            'selected_branch': selected_branch,
            'filters': filters,
            'start_date': start_date.strftime('%Y-%m-%d'),
            'end_date': end_date.strftime('%Y-%m-%d'),
            'start_date_bs': start_date_bs,
            'end_date_bs': end_date_bs,
        }