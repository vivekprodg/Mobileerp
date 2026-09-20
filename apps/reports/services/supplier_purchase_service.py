"""
Supplier Purchase Turnover & Procurement Summary Service.
File Path: apps/reports/services/supplier_purchase_service.py

Capabilities:
1. Calculates cumulative procurement turnover by supplier / distributor across any custom date range.
2. Aggregates total bills received, gross purchases, trade discounts, 13% input VAT,
   shipping / customs / handling overheads, landed acquisition costs, and purchase returns (debit notes).
3. Evaluates net procurement outlay (Landed Purchases minus Purchase Returns).
4. Reconciles cash / bank payouts versus amounts added to supplier running credit (Udhaari).
5. Scopes to active branch terminal or aggregates across all stores with Gregorian AD & Nepali BS dates.
"""

from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime
from typing import Dict, Any, List, Optional, Tuple

from django.db.models import Q, Sum, Count, F, DecimalField, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.purchases.models import Supplier, GoodsReceivedNote, PurchaseReturn
from apps.branches.models import Branch
from apps.core.nepali_calendar import NepaliCalendar
from apps.core.utils.nepali_date_converter import ad_to_bs_string


class SupplierPurchaseService:
    """
    Business logic service for the Supplier-Wise Purchase Summary Report.
    """

    @classmethod
    def resolve_date_range(cls, raw_params: Dict[str, Any]) -> Tuple[date, date, str, str]:
        """
        Resolves start and end dates. Defaults to the 1st of the current
        Nepali Bikram Sambat (BS) month through today.
        Returns: (start_date, end_date, start_date_bs, end_date_bs)
        """
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
    def get_supplier_purchase_data(
        cls,
        filters: Dict[str, Any],
        user=None
    ) -> Dict[str, Any]:
        """
        Compiles supplier-level procurement metrics, returns deductions, and payables.
        """
        active_branch = getattr(user, 'assigned_branch', None) if user and not user.is_superuser else None

        start_date, end_date, start_date_bs, end_date_bs = cls.resolve_date_range(filters)
        branch_id = filters.get('branch_id') or filters.get('branch', '')
        supplier_id = filters.get('supplier_id') or filters.get('supplier', '')
        search_query = str(filters.get('q', '') or '').strip()

        # 1. Base Suppliers Query
        supplier_qs = Supplier.objects.all()

        if supplier_id and str(supplier_id).strip() not in ['', 'all', 'None']:
            supplier_qs = supplier_qs.filter(id=supplier_id)

        if search_query:
            supplier_qs = supplier_qs.filter(
                Q(code__icontains=search_query) |
                Q(company_name__icontains=search_query) |
                Q(contact_person__icontains=search_query) |
                Q(phone_number__icontains=search_query) |
                Q(pan_number__icontains=search_query) |
                Q(registration_number__icontains=search_query)
            )

        # 2. Build GRN Aggregations Map for Date Range
        grn_qs = GoodsReceivedNote.objects.filter(
            status='RECEIVED',
            bill_date__gte=start_date,
            bill_date__lte=end_date
        )

        if branch_id and str(branch_id).strip() not in ['', 'all', 'None']:
            grn_qs = grn_qs.filter(branch_id=branch_id)
        elif active_branch and not (user.is_superuser or getattr(user, 'role', '') == 'OWNER'):
            grn_qs = grn_qs.filter(branch=active_branch)

        grn_stats = grn_qs.values('supplier_id').annotate(
            bill_count=Count('id'),
            gross_total=Coalesce(Sum('gross_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            discount_total=Coalesce(Sum('discount_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            vat_total=Coalesce(Sum('vat_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            freight_total=Coalesce(Sum('extra_freight_charge'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            customs_total=Coalesce(Sum('customs_import_charge'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            handling_total=Coalesce(Sum('other_handling_charge'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            landed_total=Coalesce(Sum('total_landed_cost'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            net_total=Coalesce(Sum('net_total_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            paid_total=Coalesce(Sum('paid_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            due_total=Coalesce(Sum('due_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2)))
        )

        grn_map = {row['supplier_id']: row for row in grn_stats}

        # 3. Build Purchase Returns Aggregations Map for Date Range
        return_qs = PurchaseReturn.objects.filter(
            status='CONFIRMED',
            return_date__gte=start_date,
            return_date__lte=end_date
        )

        if branch_id and str(branch_id).strip() not in ['', 'all', 'None']:
            return_qs = return_qs.filter(branch_id=branch_id)
        elif active_branch and not (user.is_superuser or getattr(user, 'role', '') == 'OWNER'):
            return_qs = return_qs.filter(branch=active_branch)

        return_stats = return_qs.values('supplier_id').annotate(
            return_count=Count('id'),
            return_total=Coalesce(Sum('net_refund_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2)))
        )

        return_map = {row['supplier_id']: row for row in return_stats}

        # 4. Construct Itemized Supplier Records
        records: List[Dict[str, Any]] = []

        total_bills_count = 0
        total_gross = Decimal('0.00')
        total_discount = Decimal('0.00')
        total_vat = Decimal('0.00')
        total_overheads = Decimal('0.00')
        total_landed = Decimal('0.00')
        total_returns_amount = Decimal('0.00')
        total_net_procurement = Decimal('0.00')
        total_paid = Decimal('0.00')
        total_due_added = Decimal('0.00')
        total_current_debt = Decimal('0.00')

        for sup in supplier_qs.order_by('company_name'):
            g_data = grn_map.get(sup.id, {})
            r_data = return_map.get(sup.id, {})

            bills_count = g_data.get('bill_count', 0)
            returns_count = r_data.get('return_count', 0)

            # Skip suppliers with zero activity in period unless specifically searched
            if bills_count == 0 and returns_count == 0 and not search_query and not supplier_id:
                continue

            gross = g_data.get('gross_total', Decimal('0.00'))
            disc = g_data.get('discount_total', Decimal('0.00'))
            vat = g_data.get('vat_total', Decimal('0.00'))
            freight = g_data.get('freight_total', Decimal('0.00'))
            customs = g_data.get('customs_total', Decimal('0.00'))
            handling = g_data.get('handling_total', Decimal('0.00'))
            overheads = freight + customs + handling
            landed = g_data.get('landed_total', Decimal('0.00'))
            net_inv = g_data.get('net_total', Decimal('0.00'))
            paid = g_data.get('paid_total', Decimal('0.00'))
            due = g_data.get('due_total', Decimal('0.00'))

            returns_val = r_data.get('return_total', Decimal('0.00'))
            net_procurement = max(Decimal('0.00'), landed - returns_val)

            outstanding_debt = sup.current_balance or Decimal('0.00')

            total_bills_count += bills_count
            total_gross += gross
            total_discount += disc
            total_vat += vat
            total_overheads += overheads
            total_landed += landed
            total_returns_amount += returns_val
            total_net_procurement += net_procurement
            total_paid += paid
            total_due_added += due
            total_current_debt += outstanding_debt

            records.append({
                'supplier_id': sup.id,
                'supplier_code': sup.code,
                'company_name': sup.company_name,
                'contact_person': sup.contact_person,
                'phone_number': sup.phone_number,
                'pan_number': sup.pan_number or '-',
                'supplier_type': sup.get_supplier_type_display(),
                'is_authorized_distributor': sup.is_authorized_distributor,
                'bills_count': bills_count,
                'returns_count': returns_count,
                'gross_amount': gross,
                'discount_amount': disc,
                'vat_amount': vat,
                'overhead_amount': overheads,
                'landed_purchases': landed,
                'net_invoice_total': net_inv,
                'returns_amount': returns_val,
                'net_procurement': net_procurement,
                'paid_amount': paid,
                'due_amount': due,
                'current_outstanding': outstanding_debt,
                'last_purchase_date': sup.last_purchase_date,
            })

        # Sort: Highest procurement spend first
        records.sort(key=lambda x: x['net_procurement'], reverse=True)

        totals = {
            'total_suppliers_active': len(records),
            'total_bills_count': total_bills_count,
            'total_gross': total_gross,
            'total_discount': total_discount,
            'total_vat': total_vat,
            'total_overheads': total_overheads,
            'total_landed': total_landed,
            'total_returns_amount': total_returns_amount,
            'total_net_procurement': total_net_procurement,
            'total_paid': total_paid,
            'total_due_added': total_due_added,
            'total_current_debt': total_current_debt,
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
            'suppliers': Supplier.objects.filter(is_active=True).order_by('company_name'),
            'selected_branch': selected_branch,
            'filters': filters,
            'start_date': start_date.strftime('%Y-%m-%d'),
            'end_date': end_date.strftime('%Y-%m-%d'),
            'start_date_bs': start_date_bs,
            'end_date_bs': end_date_bs,
        }