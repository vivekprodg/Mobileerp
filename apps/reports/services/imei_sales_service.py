"""
Sold Handset IMEI Registry & Warranty History Service.
File Path: apps/reports/services/imei_sales_service.py

Capabilities:
1. Provides a forensic audit ledger of every serialized mobile phone sold to retail customers.
2. Exposes Primary IMEI 1, Secondary IMEI 2, Serial Number, and Device UID.
3. Links exact invoice numbers, sale dates (AD & BS), customer names, mobile numbers, and PAN.
4. Audits NTA MDMS compliance status (Registered Official vs. Gray Market), physical condition grade,
   landed cost, sold price, realized gross margin, and remaining active warranty coverage days.
"""

from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime
from typing import Dict, Any, List, Tuple

from django.db.models import Q
from django.utils import timezone

from apps.sales.models import SalesEstimateItem
from apps.inventory.models import ItemInstance, Brand
from apps.branches.models import Branch
from apps.core.nepali_calendar import NepaliCalendar
from apps.core.utils.nepali_date_converter import ad_to_bs_string


class IMEISalesService:
    """
    Business logic engine for Report 6: Sold Handset & IMEI Registry Report.
    """

    @classmethod
    def resolve_date_range(cls, raw_params: Dict[str, Any]) -> Tuple[date, date, str, str]:
        """Resolves date range with Nepali BS defaults."""
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
    def get_imei_sales_data(
        cls,
        filters: Dict[str, Any],
        user=None
    ) -> Dict[str, Any]:
        """
        Queries all sold handset lines with dual-IMEI parameters, warranty status, and profits.
        """
        active_branch = getattr(user, 'assigned_branch', None) if user and not user.is_superuser else None

        start_date, end_date, start_date_bs, end_date_bs = cls.resolve_date_range(filters)
        branch_id = filters.get('branch_id') or filters.get('branch', '')
        brand_id = filters.get('brand_id') or filters.get('brand', '')
        condition_filter = str(filters.get('condition', '') or '').strip()
        mdms_filter = str(filters.get('mdms_status', '') or '').strip()
        search_query = str(filters.get('q', '') or '').strip()

        today = timezone.now().date()

        # 1. Base Query: Only completed sales of serialized phones
        qs = SalesEstimateItem.objects.select_related(
            'estimate',
            'estimate__branch',
            'estimate__customer',
            'estimate__cashier',
            'estimate__salesperson',
            'product',
            'product__brand',
            'product__base_unit',
            'item_instance'
        ).filter(
            estimate__status__in=['COMPLETED', 'PARTIALLY_RETURNED'],
            estimate__bill_date_ad__gte=start_date,
            estimate__bill_date_ad__lte=end_date
        ).filter(
            Q(item_instance__isnull=False) |
            Q(product__requires_imei_tracking=True) |
            ~Q(imei_number__exact='') |
            Q(imei_number__isnull=False)
        )

        # 2. Scoping
        if branch_id and str(branch_id).strip() not in ['', 'all', 'None']:
            qs = qs.filter(estimate__branch_id=branch_id)
        elif active_branch and not (user.is_superuser or getattr(user, 'role', '') == 'OWNER'):
            qs = qs.filter(estimate__branch=active_branch)

        if brand_id and str(brand_id).strip() not in ['', 'all', 'None']:
            qs = qs.filter(product__brand_id=brand_id)

        if condition_filter and condition_filter not in ['', 'all']:
            qs = qs.filter(
                Q(device_condition=condition_filter) |
                Q(item_instance__condition=condition_filter)
            )

        if mdms_filter and mdms_filter not in ['', 'all']:
            qs = qs.filter(
                Q(item_instance__mdms_status=mdms_filter) |
                Q(product__default_mdms_status=mdms_filter)
            )

        if search_query:
            qs = qs.filter(
                Q(imei_number__icontains=search_query) |
                Q(secondary_imei__icontains=search_query) |
                Q(serial_number__icontains=search_query) |
                Q(estimate__estimate_number__icontains=search_query) |
                Q(estimate__customer_name_manual__icontains=search_query) |
                Q(estimate__customer_phone_manual__icontains=search_query) |
                Q(estimate__customer__name__icontains=search_query) |
                Q(estimate__customer__phone_number__icontains=search_query) |
                Q(product__name__icontains=search_query) |
                Q(product__sku__icontains=search_query)
            )

        # 3. Build Detailed Sold Handset Records
        records: List[Dict[str, Any]] = []

        total_handsets_count = 0
        total_sold_amount = Decimal('0.00')
        total_cost_amount = Decimal('0.00')
        total_profit_amount = Decimal('0.00')
        active_warranty_count = 0
        mdms_registered_count = 0
        gray_market_count = 0

        for itm in qs.order_by('-estimate__bill_date_ad', '-estimate__created_at', 'id'):
            est = itm.estimate
            prod = itm.product
            inst = itm.item_instance

            im1 = itm.imei_number or (inst.imei_1 if inst else '')
            im2 = itm.secondary_imei or (inst.imei_2 if inst else '')
            sn = itm.serial_number or (inst.serial_number if inst else '')
            uid = inst.device_uid if inst else '-'

            condition_label = itm.device_condition or (inst.get_condition_display() if inst else "Brand New")
            mdms_status_code = (inst.mdms_status if inst else prod.default_mdms_status) or 'REGISTERED_OFFICIAL'

            if mdms_status_code == 'REGISTERED_OFFICIAL':
                mdms_label = "MDMS OK"
                mdms_registered_count += 1
            elif mdms_status_code == 'GRAY_UNREGISTERED':
                mdms_label = "Gray Market"
                gray_market_count += 1
            else:
                mdms_label = mdms_status_code

            landed_cost = itm.cost_price or (inst.landed_cost if inst else prod.purchase_price) or Decimal('0.00')
            sold_price = itm.line_total or (itm.unit_price * itm.quantity)
            profit = (sold_price - itm.tax_amount - (landed_cost * itm.base_unit_quantity)).quantize(
                Decimal('0.01'), rounding=ROUND_HALF_UP
            )

            margin_pct = (
                ((profit / (sold_price - itm.tax_amount)) * Decimal('100.00')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
                if (sold_price - itm.tax_amount) > Decimal('0.00') else Decimal('0.0')
            )

            # Warranty Status Evaluation
            w_exp = itm.warranty_expiry_date or (inst.warranty_end_date if inst else None)
            is_warranty_active = False
            days_left = 0
            if w_exp:
                is_warranty_active = (w_exp >= today)
                if is_warranty_active:
                    days_left = (w_exp - today).days
                    active_warranty_count += 1

            total_handsets_count += 1
            total_sold_amount += sold_price
            total_cost_amount += (landed_cost * itm.base_unit_quantity)
            total_profit_amount += profit

            specs_tag = f"{prod.ram or ''}/{prod.internal_storage or ''}".strip('/')
            if prod.color_variant:
                specs_tag = f"{specs_tag} {prod.color_variant}".strip()

            records.append({
                'id': itm.id,
                'estimate_number': est.estimate_number,
                'estimate_pk': est.pk,
                'date_ad_str': est.bill_date_ad.strftime('%Y-%m-%d'),
                'date_bs_str': est.bill_date_bs or ad_to_bs_string(est.bill_date_ad, lang='en'),
                'customer_name': est.recipient_display_name,
                'customer_phone': est.customer_phone_manual or (est.customer.phone_number if est.customer else '-'),
                'product_name': prod.name,
                'brand_name': prod.brand.name if prod.brand else '-',
                'specs': specs_tag or '-',
                'sku': prod.sku,
                'imei_1': im1 or '-',
                'imei_2': im2 or '',
                'serial_number': sn or '',
                'device_uid': uid,
                'condition': condition_label,
                'mdms_status': mdms_status_code,
                'mdms_label': mdms_label,
                'landed_cost': landed_cost,
                'sold_price': sold_price,
                'gross_profit': profit,
                'margin_percent': margin_pct,
                'warranty_terms': itm.warranty_terms or f"{itm.warranty_months}M Official Warranty",
                'warranty_expiry_str': w_exp.strftime('%Y-%m-%d') if w_exp else '-',
                'is_warranty_active': is_warranty_active,
                'days_left': days_left,
                'branch_code': est.branch.code,
                'branch_name': est.branch.name,
                'salesperson': est.salesperson.username if est.salesperson else est.cashier.username,
                'cashier': est.cashier.username,
            })

        overall_margin_pct = (
            ((total_profit_amount / total_sold_amount) * Decimal('100.00')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
            if total_sold_amount > Decimal('0.00') else Decimal('0.0')
        )

        totals = {
            'total_handsets_count': total_handsets_count,
            'total_sold_amount': total_sold_amount,
            'total_cost_amount': total_cost_amount,
            'total_profit_amount': total_profit_amount,
            'overall_margin_pct': overall_margin_pct,
            'active_warranty_count': active_warranty_count,
            'mdms_registered_count': mdms_registered_count,
            'gray_market_count': gray_market_count,
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
            'brands': Brand.objects.all().order_by('name'),
            'conditions': [
                ('BRAND_NEW', 'Brand New'),
                ('USED_GRADE_A', 'Grade A (Flawless)'),
                ('USED_GRADE_B', 'Grade B (Minor Wear)'),
                ('USED_GRADE_C', 'Grade C (Heavy Wear)'),
            ],
            'selected_branch': selected_branch,
            'filters': filters,
            'start_date': start_date.strftime('%Y-%m-%d'),
            'end_date': end_date.strftime('%Y-%m-%d'),
            'start_date_bs': start_date_bs,
            'end_date_bs': end_date_bs,
        }