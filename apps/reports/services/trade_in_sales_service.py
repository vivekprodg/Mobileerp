"""
Trade-In / Exchange Sales Settlement Report Business Logic Service.
File Path: apps/reports/services/trade_in_sales_service.py

Capabilities:
1. Audits combined transactions where an old phone was traded in to buy a new smartphone.
2. Links new handset sold (Selling Price, IMEI 1, Landed COGS) with the old phone acquired
   (Buy-back Valuation Credit, Condition Grade, NTA MDMS Status).
3. Reconciles net cash top-up paid by customer versus any surplus store credit refunded.
4. Computes combined deal profit: (New Phone Price - New Phone Cost) + Trade-in Profit Margin.
5. Verifies compliance status of statutory police anti-theft undertakings.
"""

from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime
from typing import Dict, Any, List, Tuple, Optional

from django.db.models import Q
from django.utils import timezone

from apps.sales.models import SalesEstimate, PhoneExchangeTradeIn
from apps.branches.models import Branch
from apps.core.nepali_calendar import NepaliCalendar
from apps.core.utils.nepali_date_converter import ad_to_bs_string


class TradeInSalesService:
    """
    Business logic engine for Report 11: Trade-In / Exchange Sales Settlement Report.
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
    def get_trade_in_sales_data(
        cls,
        filters: Dict[str, Any],
        user=None
    ) -> Dict[str, Any]:
        """
        Queries all sales estimates that incorporated a trade-in buy-back credit offset.
        """
        active_branch = getattr(user, 'assigned_branch', None) if user and not user.is_superuser else None

        start_date, end_date, start_date_bs, end_date_bs = cls.resolve_date_range(filters)
        branch_id = filters.get('branch_id') or filters.get('branch', '')
        search_query = str(filters.get('q', '') or '').strip()

        # 1. Base Query: Completed bills where trade-in credit was applied
        estimates_qs = SalesEstimate.objects.select_related(
            'branch',
            'customer',
            'cashier',
            'salesperson'
        ).prefetch_related(
            'items__product',
            'items__item_instance',
            'payment_transactions'
        ).filter(
            status__in=['COMPLETED', 'PARTIALLY_RETURNED'],
            has_trade_in_exchange=True,
            bill_date_ad__gte=start_date,
            bill_date_ad__lte=end_date
        )

        # 2. Scoping
        if branch_id and str(branch_id).strip() not in ['', 'all', 'None']:
            estimates_qs = estimates_qs.filter(branch_id=branch_id)
        elif active_branch and not (user.is_superuser or getattr(user, 'role', '') == 'OWNER'):
            estimates_qs = estimates_qs.filter(branch=active_branch)

        if search_query:
            estimates_qs = estimates_qs.filter(
                Q(estimate_number__icontains=search_query) |
                Q(trade_in_voucher_reference__icontains=search_query) |
                Q(customer_name_manual__icontains=search_query) |
                Q(customer_phone_manual__icontains=search_query) |
                Q(items__product__name__icontains=search_query) |
                Q(items__imei_number__icontains=search_query)
            ).distinct()

        # 3. Batch Map Attached Trade-In Vouchers
        voucher_codes = [
            e.trade_in_voucher_reference for e in estimates_qs if e.trade_in_voucher_reference
        ]
        vouchers_map: Dict[str, PhoneExchangeTradeIn] = {}
        if voucher_codes:
            v_list = PhoneExchangeTradeIn.objects.select_related(
                'legal_undertaking', 'inspection_checklist'
            ).filter(voucher_number__in=voucher_codes)
            for v in v_list:
                vouchers_map[v.voucher_number] = v

        records: List[Dict[str, Any]] = []

        total_deals_count = 0
        total_trade_in_credits = Decimal('0.00')
        total_new_phones_value = Decimal('0.00')
        total_cash_topup_collected = Decimal('0.00')
        total_combined_profit = Decimal('0.00')

        for est in estimates_qs.order_by('-bill_date_ad', '-created_at'):
            voucher = vouchers_map.get(est.trade_in_voucher_reference)

            # Sold New Phone Details
            sold_items = list(est.items.all())
            main_phone_item = next((it for it in sold_items if it.product.requires_imei_tracking), None) or (sold_items[0] if sold_items else None)

            new_phone_name = main_phone_item.product.name if main_phone_item else "New Merchandise"
            new_phone_price = main_phone_item.line_total if main_phone_item else est.subtotal
            new_phone_cost = (main_phone_item.cost_price * main_phone_item.base_unit_quantity) if main_phone_item else est.total_cost_amount
            new_phone_imei = main_phone_item.imei_number if main_phone_item else ''

            # Traded Old Phone Details
            if voucher:
                old_phone_model = f"{voucher.brand_name} {voucher.model_name}"
                old_phone_specs = f"{voucher.storage_capacity} {voucher.color_variant or ''}".strip()
                old_phone_imei = voucher.imei_1
                old_condition = voucher.get_recommended_condition_grade_display()
                old_mdms = voucher.get_mdms_status_display()
                undertaking_signed = bool(hasattr(voucher, 'legal_undertaking') and voucher.legal_undertaking.declaration_accepted)
                trade_in_credit = voucher.final_trade_in_value
                margin_deduction = voucher.shop_margin_deduction or Decimal('0.00')
            else:
                old_phone_model = "Pre-Owned Handset"
                old_phone_specs = "-"
                old_phone_imei = "-"
                old_condition = "Pre-Owned"
                old_mdms = "Verified"
                undertaking_signed = False
                trade_in_credit = est.trade_in_discount_amount
                margin_deduction = Decimal('0.00')

            cash_topup = est.paid_amount
            deal_profit = est.total_gross_profit + margin_deduction

            total_deals_count += 1
            total_trade_in_credits += trade_in_credit
            total_new_phones_value += new_phone_price
            total_cash_topup_collected += cash_topup
            total_combined_profit += deal_profit

            records.append({
                'estimate_number': est.estimate_number,
                'estimate_pk': est.pk,
                'voucher_number': est.trade_in_voucher_reference or '-',
                'voucher_pk': voucher.pk if voucher else None,
                'date_ad_str': est.bill_date_ad.strftime('%Y-%m-%d'),
                'date_bs_str': est.bill_date_bs or ad_to_bs_string(est.bill_date_ad, lang='en'),
                'branch_code': est.branch.code,
                'customer_name': est.recipient_display_name,
                'customer_phone': est.customer_phone_manual or (est.customer.phone_number if est.customer else '-'),
                # New Phone Sold
                'new_phone_name': new_phone_name,
                'new_phone_imei': new_phone_imei,
                'new_phone_price': new_phone_price,
                'new_phone_cost': new_phone_cost,
                # Old Phone Received
                'old_phone_model': old_phone_model,
                'old_phone_specs': old_phone_specs,
                'old_phone_imei': old_phone_imei,
                'old_condition': old_condition,
                'old_mdms': old_mdms,
                # Financials
                'trade_in_credit': trade_in_credit,
                'cash_topup_paid': cash_topup,
                'due_balance': est.due_amount,
                'deal_profit': deal_profit,
                'undertaking_signed': undertaking_signed,
                'cashier': est.cashier.username,
            })

        totals = {
            'total_deals_count': total_deals_count,
            'total_new_phones_value': total_new_phones_value,
            'total_trade_in_credits': total_trade_in_credits,
            'total_cash_topup_collected': total_cash_topup_collected,
            'total_combined_profit': total_combined_profit,
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
            'selected_branch': selected_branch,
            'filters': filters,
            'start_date': start_date.strftime('%Y-%m-%d'),
            'end_date': end_date.strftime('%Y-%m-%d'),
            'start_date_bs': start_date_bs,
            'end_date_bs': end_date_bs,
        }