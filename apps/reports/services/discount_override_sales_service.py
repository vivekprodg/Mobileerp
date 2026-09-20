"""
Discount, Concession & Supervisor Price Override Audit Service.
File Path: apps/reports/services/discount_override_sales_service.py

Capabilities:
1. Audits counter discounts and commercial profit leakage:
   - Line Item Discounts (AMOUNT in NPR vs. PERCENTAGE)
   - Invoice-Level Bill Discounts (AMOUNT vs. PERCENTAGE)
   - Catalog Price Overrides (Official Catalog Price vs. Cashier Override Rate)
2. Tracks authorizing manager/owner (`manager_override_by`), timestamp, and commercial justification
   (`discount_reason`, e.g. Customer Negotiation, Matching Competitor, Damage Packaging).
3. Reconciles multi-quantity lines with per-unit concession calculations.
"""

from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime
from typing import Dict, Any, List, Tuple, Optional

from django.db.models import Q
from django.utils import timezone

from apps.sales.models import SalesEstimateItem, SalesEstimate
from apps.branches.models import Branch
from apps.users.models import User
from apps.core.nepali_calendar import NepaliCalendar
from apps.core.utils.nepali_date_converter import ad_to_bs_string


class DiscountOverrideSalesService:
    """
    Business logic engine for Report 10: Discount & Price Override Concession Report.
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
    def get_discount_override_data(
        cls,
        filters: Dict[str, Any],
        user=None
    ) -> Dict[str, Any]:
        """
        Queries all sales lines where discounts or price overrides were granted.
        """
        active_branch = getattr(user, 'assigned_branch', None) if user and not user.is_superuser else None

        start_date, end_date, start_date_bs, end_date_bs = cls.resolve_date_range(filters)
        branch_id = filters.get('branch_id') or filters.get('branch', '')
        concession_type = str(filters.get('concession_type', '') or '').strip().lower()
        manager_id = filters.get('manager_id') or filters.get('manager', '')
        reason_query = str(filters.get('reason', '') or '').strip()
        search_query = str(filters.get('q', '') or '').strip()

        # 1. Base Query: Only completed sales items with concessions
        qs = SalesEstimateItem.objects.select_related(
            'estimate',
            'estimate__branch',
            'estimate__customer',
            'estimate__cashier',
            'estimate__salesperson',
            'estimate__manager_override_by',
            'product',
            'product__category',
            'product__brand',
            'product__base_unit'
        ).filter(
            estimate__status__in=['COMPLETED', 'PARTIALLY_RETURNED'],
            estimate__bill_date_ad__gte=start_date,
            estimate__bill_date_ad__lte=end_date
        ).filter(
            Q(item_discount_amount__gt=Decimal('0.00')) |
            Q(allocated_bill_discount_amount__gt=Decimal('0.00')) |
            Q(discount_amount__gt=Decimal('0.00')) |
            Q(price_override_amount__gt=Decimal('0.00'))
        )

        # 2. Scoping
        if branch_id and str(branch_id).strip() not in ['', 'all', 'None']:
            qs = qs.filter(estimate__branch_id=branch_id)
        elif active_branch and not (user.is_superuser or getattr(user, 'role', '') == 'OWNER'):
            qs = qs.filter(estimate__branch=active_branch)

        if manager_id and str(manager_id).strip() not in ['', 'all', 'None']:
            qs = qs.filter(estimate__manager_override_by_id=manager_id)

        if concession_type == 'amount':
            qs = qs.filter(Q(discount_type__in=['AMOUNT', 'FIXED']) | Q(estimate__bill_discount_type__in=['AMOUNT', 'FIXED']))
        elif concession_type == 'percentage':
            qs = qs.filter(Q(discount_type='PERCENTAGE') | Q(estimate__bill_discount_type='PERCENTAGE'))
        elif concession_type == 'price_override':
            qs = qs.filter(price_override_amount__gt=Decimal('0.00'))

        if reason_query:
            qs = qs.filter(estimate__discount_reason__icontains=reason_query)

        if search_query:
            qs = qs.filter(
                Q(product__name__icontains=search_query) |
                Q(product__sku__icontains=search_query) |
                Q(imei_number__icontains=search_query) |
                Q(estimate__estimate_number__icontains=search_query) |
                Q(estimate__customer_name_manual__icontains=search_query) |
                Q(estimate__discount_reason__icontains=search_query)
            )

        records: List[Dict[str, Any]] = []

        total_lines = 0
        total_price_overrides = Decimal('0.00')
        total_item_disc_amount_type = Decimal('0.00')
        total_item_disc_percent_type = Decimal('0.00')
        total_bill_disc_alloc = Decimal('0.00')
        total_concessions_granted = Decimal('0.00')

        for item in qs.order_by('-estimate__bill_date_ad', '-estimate__created_at', 'id'):
            est = item.estimate
            prod = item.product
            qty = item.quantity or Decimal('0.000')
            unit_price = item.unit_price or Decimal('0.00')
            official_price = item.official_unit_price or unit_price

            line_gross = (qty * unit_price).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            override_amt = item.price_override_amount or Decimal('0.00')
            item_disc = item.item_discount_amount or Decimal('0.00')
            bill_disc_alloc = item.allocated_bill_discount_amount or Decimal('0.00')
            total_concession = override_amt + item_disc + bill_disc_alloc

            disc_type = item.discount_type or 'NONE'
            disc_input = item.discount_input_value or Decimal('0.00')
            effective_pct = item.effective_discount_percent or item.discount_percent or Decimal('0.00')

            if disc_type in ['AMOUNT', 'FIXED']:
                total_item_disc_amount_type += item_disc
            else:
                total_item_disc_percent_type += item_disc

            total_lines += 1
            total_price_overrides += override_amt
            total_bill_disc_alloc += bill_disc_alloc
            total_concessions_granted += total_concession

            records.append({
                'id': item.id,
                'estimate_number': est.estimate_number,
                'estimate_pk': est.pk,
                'date_ad_str': est.bill_date_ad.strftime('%Y-%m-%d'),
                'date_bs_str': est.bill_date_bs or ad_to_bs_string(est.bill_date_ad, lang='en'),
                'branch_code': est.branch.code,
                'customer_name': est.recipient_display_name,
                'product_name': prod.name,
                'sku': prod.sku,
                'imei_number': item.imei_number or '',
                'quantity': qty,
                'unit_code': prod.base_unit.code if prod.base_unit else 'PCS',
                'official_unit_price': official_price,
                'unit_price': unit_price,
                'line_gross': line_gross,
                'price_override_amount': override_amt,
                'has_price_override': override_amt > Decimal('0.00'),
                'discount_type': disc_type,
                'discount_input_value': disc_input,
                'item_discount_amount': item_disc,
                'effective_discount_percent': effective_pct,
                'allocated_bill_discount': bill_disc_alloc,
                'total_line_concession': total_concession,
                'bill_discount_type': est.bill_discount_type,
                'bill_discount_amount': est.bill_discount_amount,
                'line_total': item.line_total,
                'manager_override_by': est.manager_override_by.get_full_name() or est.manager_override_by.username if est.manager_override_by else 'Cashier Direct',
                'discount_reason': est.discount_reason or 'Counter Negotiation',
                'cashier': est.cashier.username,
                'salesperson': est.salesperson.username if est.salesperson else est.cashier.username,
            })

        totals = {
            'total_lines': total_lines,
            'total_price_overrides': total_price_overrides,
            'total_item_disc_amount_type': total_item_disc_amount_type,
            'total_item_disc_percent_type': total_item_disc_percent_type,
            'total_bill_disc_alloc': total_bill_disc_alloc,
            'total_concessions_granted': total_concessions_granted,
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
            'managers': User.objects.filter(Q(role__in=['OWNER', 'MANAGER']) | Q(is_superuser=True), is_active=True).order_by('username'),
            'selected_branch': selected_branch,
            'filters': filters,
            'start_date': start_date.strftime('%Y-%m-%d'),
            'end_date': end_date.strftime('%Y-%m-%d'),
            'start_date_bs': start_date_bs,
            'end_date_bs': end_date_bs,
        }