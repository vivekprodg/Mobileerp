"""
Payment Method-Wise Collection & Bank Reconciliation Service.
File Path: apps/reports/services/payment_method_sales_service.py

Capabilities:
1. Reconciles end-of-day register settlements across distinct payment modes:
   Cash Counter, FonePay QR, eSewa, Khalti, POS Card, Bank Transfer, and Customer Credit (Udhaari).
2. Computes total transactions count, gross tendered amount, and change returned on cash sales.
3. Evaluates the collection share percentage of each tender channel relative to total store intake.
"""

from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime
from typing import Dict, Any, List, Tuple

from django.db.models import (
    Sum, Count, DecimalField, Value
)
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.sales.models import SalesPaymentTransaction, SalesEstimate
from apps.branches.models import Branch
from apps.core.nepali_calendar import NepaliCalendar


class PaymentMethodSalesService:
    """
    Business logic engine for Report 9: Payment Method-Wise Collection Report.
    """

    MODE_DISPLAY_MAP = {
        'CASH': 'Cash Counter (नगद)',
        'FONEPAY': 'FonePay QR (फोनपे)',
        'ESEWA': 'eSewa Wallet (ईसेवा)',
        'KHALTI': 'Khalti Wallet (खल्ती)',
        'CARD': 'POS Debit/Credit Card (कार्ड)',
        'BANK_TRANSFER': 'Bank Transfer / ConnectIPS',
        'CREDIT': 'Customer Credit / Udhaari (उधारो)',
    }

    MODE_ICON_MAP = {
        'CASH': 'fas fa-money-bill-wave text-success',
        'FONEPAY': 'fas fa-qrcode text-danger',
        'ESEWA': 'fas fa-wallet text-success',
        'KHALTI': 'fas fa-mobile-screen text-primary',
        'CARD': 'fas fa-credit-card text-info',
        'BANK_TRANSFER': 'fas fa-building-columns text-primary',
        'CREDIT': 'fas fa-hand-holding-dollar text-danger',
    }

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
    def get_payment_method_data(
        cls,
        filters: Dict[str, Any],
        user=None
    ) -> Dict[str, Any]:
        """
        Aggregates financial inflow by payment mode and calculates tender percentages.
        """
        active_branch = getattr(user, 'assigned_branch', None) if user and not user.is_superuser else None

        start_date, end_date, start_date_bs, end_date_bs = cls.resolve_date_range(filters)
        branch_id = filters.get('branch_id') or filters.get('branch', '')
        mode_filter = str(filters.get('payment_mode', '') or '').strip().upper()

        # 1. Base Query: Only completed sales estimates in date range
        estimates_qs = SalesEstimate.objects.filter(
            status__in=['COMPLETED', 'PARTIALLY_RETURNED'],
            bill_date_ad__gte=start_date,
            bill_date_ad__lte=end_date
        )

        if branch_id and str(branch_id).strip() not in ['', 'all', 'None']:
            estimates_qs = estimates_qs.filter(branch_id=branch_id)
        elif active_branch and not (user.is_superuser or getattr(user, 'role', '') == 'OWNER'):
            estimates_qs = estimates_qs.filter(branch=active_branch)

        # 2. Payment Transactions Query
        payments_qs = SalesPaymentTransaction.objects.filter(
            estimate__in=estimates_qs
        )

        if mode_filter and mode_filter not in ['', 'ALL']:
            payments_qs = payments_qs.filter(payment_mode=mode_filter)

        # 3. Aggregate By payment_mode
        mode_stats = payments_qs.values('payment_mode').annotate(
            transaction_count=Count('id'),
            invoice_count=Count('estimate_id', distinct=True),
            tendered_amount=Coalesce(
                Sum('amount'),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))
            )
        )
        stats_map = {row['payment_mode']: row for row in mode_stats}

        # 4. Total Change Returned on Cash Bills in this scope
        total_change_given = estimates_qs.aggregate(
            total_change=Coalesce(Sum('change_returned'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2)))
        )['total_change']

        # 5. Calculate Total Inflow to derive Collection Share %
        total_tendered_gross = sum((r['tendered_amount'] for r in stats_map.values()), Decimal('0.00'))
        total_net_collections = max(Decimal('0.00'), total_tendered_gross - total_change_given)

        records: List[Dict[str, Any]] = []

        total_tx_count = 0
        total_invoices_count = 0

        # Ensure all defined payment modes appear cleanly
        all_modes = ['CASH', 'FONEPAY', 'ESEWA', 'KHALTI', 'CARD', 'BANK_TRANSFER', 'CREDIT']

        for mode in all_modes:
            if mode_filter and mode_filter not in ['', 'ALL'] and mode != mode_filter:
                continue

            m_data = stats_map.get(mode, {})
            tx_cnt = m_data.get('transaction_count', 0)
            inv_cnt = m_data.get('invoice_count', 0)
            gross_amt = m_data.get('tendered_amount', Decimal('0.00'))

            # Deduct change returned on cash sales
            if mode == 'CASH':
                net_amt = max(Decimal('0.00'), gross_amt - total_change_given)
                change_applied = total_change_given
            else:
                net_amt = gross_amt
                change_applied = Decimal('0.00')

            share_pct = (
                ((net_amt / total_net_collections) * Decimal('100.00')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
                if total_net_collections > Decimal('0.00') else Decimal('0.0')
            )

            total_tx_count += tx_cnt
            total_invoices_count += inv_cnt

            records.append({
                'payment_mode': mode,
                'mode_display': cls.MODE_DISPLAY_MAP.get(mode, mode),
                'icon_class': cls.MODE_ICON_MAP.get(mode, 'fas fa-receipt'),
                'transaction_count': tx_cnt,
                'invoice_count': inv_cnt,
                'gross_amount': gross_amt,
                'change_deducted': change_applied,
                'net_amount': net_amt,
                'share_percent': share_pct,
            })

        # Sort: Highest net amount first
        records.sort(key=lambda x: x['net_amount'], reverse=True)

        totals = {
            'modes_count': len([r for r in records if r['transaction_count'] > 0]),
            'total_transactions': total_tx_count,
            'total_tendered_gross': total_tendered_gross,
            'total_change_given': total_change_given,
            'total_net_collections': total_net_collections,
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