"""
Item-Wise Detailed Sales Report Business Logic Service.

Capabilities:
1. Multi-Calendar Date & Fiscal Year Engine:
   - Supports filtering line items by official Nepali Fiscal Year (आर्थिक वर्ष, e.g. '2080/81', '2081/82').
   - Resolves Shrawan 1 through the exact dynamic last day of Ashadh (accounting for 30, 31, or 32 days).
   - Accurately converts Nepali BS date inputs to Gregorian AD dates for database querying.
2. Surfaces individual line items sold across counter terminals into a detailed audit report.
3. Extracts separate discount categories:
   - Pure Item Discount (direct AMOUNT in NPR or PERCENTAGE input)
   - Allocated share of Bill-Level Discount
   - Price Override Concession amount (Catalog Price minus Unit Selling Rate)
4. Computes exact unit profit: Net Line Total - Tax - (cost_price * base_unit_quantity).
5. Exposes Primary IMEI 1, Secondary IMEI 2, Serial Number, Product Specs, and Staff Allocation.
"""

import re
from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime
from typing import Dict, Any, List, Tuple, Optional

from django.db.models import Q
from django.utils import timezone

from apps.sales.models import SalesEstimateItem
from apps.inventory.models import ProductCategory, Brand
from apps.branches.models import Branch
from apps.users.models import User
from apps.core.nepali_calendar import NepaliCalendar
from apps.core.utils.nepali_date_converter import ad_to_bs_string


class ItemSalesService:
    """
    Business logic engine for Report 2: Item-Wise Detailed Sales Report.
    """

    @classmethod
    def get_available_fiscal_years(cls) -> List[str]:
        """Returns standard list of relevant Nepali Fiscal Years for selection dropdowns."""
        today = timezone.now().date()
        current_bs_y, current_bs_m, _ = NepaliCalendar.ad_to_bs(today)
        current_fy = NepaliCalendar.get_fiscal_year(current_bs_y, current_bs_m)

        base_years = [2079, 2080, 2081, 2082, 2083]
        fys = [f"{y}/{str(y + 1)[-2:]}" for y in base_years]
        if current_fy not in fys:
            fys.append(current_fy)
        fys.sort(reverse=True)
        return fys

    @classmethod
    def _parse_flexible_date(cls, val_str: str) -> Optional[Tuple[date, str]]:
        """
        Accepts arbitrary date strings in either Gregorian AD or Nepali BS (delimited by - or /)
        and returns a clean (ad_date, bs_date_string) tuple.
        """
        if not val_str:
            return None
        clean = re.sub(r'[^\d]', '-', str(val_str).strip())
        parts = [int(p) for p in clean.split('-') if p]
        if len(parts) != 3:
            return None

        # Format: YYYY-MM-DD in BS (e.g. 2080-04-01)
        if 2000 <= parts[0] <= 2095:
            bs_y, bs_m, bs_d = parts[0], parts[1], parts[2]
            bs_m = max(1, min(12, bs_m))
            max_days = NepaliCalendar.get_days_in_month(bs_y, bs_m)
            bs_d = max(1, min(max_days, bs_d))
            ad_date = NepaliCalendar.bs_to_ad(bs_y, bs_m, bs_d)
            bs_str = f"{bs_y:04d}-{bs_m:02d}-{bs_d:02d}"
            return ad_date, bs_str

        # Format: DD-MM-YYYY in BS (e.g. 01-04-2080)
        elif 2000 <= parts[2] <= 2095:
            bs_y, bs_m, bs_d = parts[2], parts[1], parts[0]
            bs_m = max(1, min(12, bs_m))
            max_days = NepaliCalendar.get_days_in_month(bs_y, bs_m)
            bs_d = max(1, min(max_days, bs_d))
            ad_date = NepaliCalendar.bs_to_ad(bs_y, bs_m, bs_d)
            bs_str = f"{bs_y:04d}-{bs_m:02d}-{bs_d:02d}"
            return ad_date, bs_str

        # Format: YYYY-MM-DD in AD (e.g. 2023-07-17)
        elif 1970 <= parts[0] <= 2050:
            try:
                ad_date = date(parts[0], parts[1], parts[2])
                y, m, d = NepaliCalendar.ad_to_bs(ad_date)
                bs_str = NepaliCalendar.format_bs(y, m, d, lang='en')
                return ad_date, bs_str
            except (ValueError, TypeError):
                return None

        return None

    @classmethod
    def resolve_date_range(cls, raw_params: Dict[str, Any]) -> Tuple[date, date, str, str, str]:
        """
        Resolves query dates and Nepali Fiscal Year with multi-format support:
        1. Fiscal Year parameter (e.g. '2080/81', '2081/82', '2080-81').
        2. Direct Nepali BS dates ('YYYY-MM-DD').
        3. Gregorian AD dates ('YYYY-MM-DD').
        4. Default fallback: 1st of the current BS month up to today.

        Returns:
            Tuple[date, date, str, str, str]:
            (start_date_ad, end_date_ad, start_date_bs, end_date_bs, fiscal_year)
        """
        today_ad = timezone.now().date()
        today_bs_y, today_bs_m, today_bs_d = NepaliCalendar.ad_to_bs(today_ad)

        fy_param = str(raw_params.get('fiscal_year') or raw_params.get('fy') or '').strip()
        start_param = str(raw_params.get('start_date') or raw_params.get('start_date_bs') or '').strip()
        end_param = str(raw_params.get('end_date') or raw_params.get('end_date_bs') or '').strip()

        # Case 1: Fiscal Year preset selected without explicit overriding date range
        if fy_param and fy_param.lower() not in ['all', 'none', '']:
            clean_fy = fy_param.replace('-', '/').strip()
            if not start_param and not end_param:
                try:
                    start_ad, end_ad, start_bs, end_bs = NepaliCalendar.get_fiscal_year_range(clean_fy)
                    parts = clean_fy.split('/')
                    norm_fy = f"{int(parts[0])}/{str(int(parts[0]) + 1)[-2:]}"
                    return start_ad, end_ad, start_bs, end_bs, norm_fy
                except Exception:
                    pass

        # Case 2: Parse custom start and end date inputs (supporting both AD and BS formats)
        start_date = None
        end_date = None
        start_date_bs = ""
        end_date_bs = ""

        if start_param:
            parsed_start = cls._parse_flexible_date(start_param)
            if parsed_start:
                start_date, start_date_bs = parsed_start

        if end_param:
            parsed_end = cls._parse_flexible_date(end_param)
            if parsed_end:
                end_date, end_date_bs = parsed_end

        # Case 3: If fiscal_year was chosen along with custom dates within it
        if fy_param and fy_param.lower() not in ['all', 'none', '']:
            clean_fy = fy_param.replace('-', '/').strip()
            try:
                fy_start_ad, fy_end_ad, fy_start_bs, fy_end_bs = NepaliCalendar.get_fiscal_year_range(clean_fy)
                parts = clean_fy.split('/')
                resolved_fy = f"{int(parts[0])}/{str(int(parts[0]) + 1)[-2:]}"
                start_date = start_date or fy_start_ad
                end_date = end_date or fy_end_ad
                start_date_bs = start_date_bs or fy_start_bs
                end_date_bs = end_date_bs or fy_end_bs
                if start_date > end_date:
                    start_date, end_date = end_date, start_date
                    start_date_bs, end_date_bs = end_date_bs, start_date_bs
                return start_date, end_date, start_date_bs, end_date_bs, resolved_fy
            except Exception:
                pass

        # Case 4: Fallback to current ongoing BS month up to today
        if not start_date or not end_date:
            start_of_bs_month = NepaliCalendar.bs_to_ad(today_bs_y, today_bs_m, 1)
            start_date = start_date or start_of_bs_month
            end_date = end_date or today_ad

        if start_date > end_date:
            start_date, end_date = end_date, start_date

        start_y, start_m, start_d = NepaliCalendar.ad_to_bs(start_date)
        end_y, end_m, end_d = NepaliCalendar.ad_to_bs(end_date)

        start_date_bs = NepaliCalendar.format_bs(start_y, start_m, start_d, lang='en')
        end_date_bs = NepaliCalendar.format_bs(end_y, end_m, end_d, lang='en')
        resolved_fy = NepaliCalendar.get_fiscal_year(start_y, start_m)

        return start_date, end_date, start_date_bs, end_date_bs, resolved_fy

    @classmethod
    def get_item_sales_data(
        cls,
        filters: Dict[str, Any],
        user=None
    ) -> Dict[str, Any]:
        """
        Queries all SalesEstimateItem rows for completed bills with filters for fiscal year,
        IMEI, product, category, brand, salesperson, cashier, and concessions.
        """
        active_branch = getattr(user, 'assigned_branch', None) if user and not user.is_superuser else None

        start_date, end_date, start_date_bs, end_date_bs, fiscal_year = cls.resolve_date_range(filters)
        branch_id = filters.get('branch_id') or filters.get('branch', '')
        category_id = filters.get('category_id') or filters.get('category', '')
        brand_id = filters.get('brand_id') or filters.get('brand', '')
        salesperson_id = filters.get('salesperson_id') or filters.get('salesperson', '')
        cashier_id = filters.get('cashier_id') or filters.get('cashier', '')
        discount_filter = str(filters.get('discount_filter', '') or '').strip().lower()
        serialized_only = str(filters.get('serialized_only', '') or '').strip().lower()
        search_query = str(filters.get('q', '') or '').strip()

        # 1. Base Query: Only completed/partially returned bills in resolved date range
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
            'product__base_unit',
            'item_instance'
        ).filter(
            estimate__status__in=['COMPLETED', 'PARTIALLY_RETURNED'],
            estimate__bill_date_ad__gte=start_date,
            estimate__bill_date_ad__lte=end_date
        )

        # 2. Scoping
        if branch_id and str(branch_id).strip() not in ['', 'all', 'None']:
            qs = qs.filter(estimate__branch_id=branch_id)
        elif active_branch and not (user.is_superuser or getattr(user, 'role', '') == 'OWNER'):
            qs = qs.filter(estimate__branch=active_branch)

        if category_id and str(category_id).strip() not in ['', 'all', 'None']:
            qs = qs.filter(product__category_id=category_id)

        if brand_id and str(brand_id).strip() not in ['', 'all', 'None']:
            qs = qs.filter(product__brand_id=brand_id)

        if salesperson_id and str(salesperson_id).strip() not in ['', 'all', 'None']:
            qs = qs.filter(estimate__salesperson_id=salesperson_id)

        if cashier_id and str(cashier_id).strip() not in ['', 'all', 'None']:
            qs = qs.filter(estimate__cashier_id=cashier_id)

        if serialized_only in ['true', 'yes', '1']:
            qs = qs.filter(Q(item_instance__isnull=False) | Q(product__requires_imei_tracking=True))

        if discount_filter == 'discounted':
            qs = qs.filter(Q(discount_amount__gt=Decimal('0.00')) | Q(price_override_amount__gt=Decimal('0.00')))
        elif discount_filter == 'override_only':
            qs = qs.filter(price_override_amount__gt=Decimal('0.00'))

        if search_query:
            qs = qs.filter(
                Q(product__name__icontains=search_query) |
                Q(product__sku__icontains=search_query) |
                Q(product__barcode__icontains=search_query) |
                Q(imei_number__icontains=search_query) |
                Q(secondary_imei__icontains=search_query) |
                Q(serial_number__icontains=search_query) |
                Q(estimate__estimate_number__icontains=search_query) |
                Q(estimate__customer_name_manual__icontains=search_query) |
                Q(estimate__customer_phone_manual__icontains=search_query) |
                Q(estimate__customer__name__icontains=search_query)
            )

        # 3. Process Individual Item Records
        records: List[Dict[str, Any]] = []

        total_units = Decimal('0.000')
        total_gross_subtotal = Decimal('0.00')
        total_price_overrides = Decimal('0.00')
        total_item_discounts = Decimal('0.00')
        total_bill_discounts = Decimal('0.00')
        total_concessions = Decimal('0.00')
        total_vat = Decimal('0.00')
        total_net = Decimal('0.00')
        total_cogs = Decimal('0.00')
        total_profit = Decimal('0.00')

        for item in qs.order_by('-estimate__bill_date_ad', '-estimate__created_at', 'id'):
            est = item.estimate
            prod = item.product
            qty = item.quantity or Decimal('0.000')
            unit_price = item.unit_price or Decimal('0.00')
            official_price = item.official_unit_price or unit_price

            line_gross = (qty * unit_price).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            override_amt = item.price_override_amount or Decimal('0.00')

            item_disc = item.item_discount_amount or Decimal('0.00')
            alloc_bill_disc = item.allocated_bill_discount_amount or Decimal('0.00')
            total_line_disc = item.discount_amount or (item_disc + alloc_bill_disc)

            vat_amt = item.tax_amount or Decimal('0.00')
            line_tot = item.line_total or Decimal('0.00')

            unit_cost = item.cost_price or Decimal('0.00')
            line_cogs = (unit_cost * item.base_unit_quantity).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            line_profit = item.line_gross_profit

            net_rev_base = max(Decimal('0.00'), line_tot - vat_amt)
            line_margin_pct = (
                ((line_profit / net_rev_base) * Decimal('100.00')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
                if net_rev_base > Decimal('0.00') else Decimal('0.0')
            )

            disc_type = item.discount_type or 'NONE'
            disc_input = item.discount_input_value or Decimal('0.00')
            effective_pct = item.effective_discount_percent or item.discount_percent or Decimal('0.00')

            total_units += qty
            total_gross_subtotal += line_gross
            total_price_overrides += override_amt
            total_item_discounts += item_disc
            total_bill_discounts += alloc_bill_disc
            total_concessions += total_line_disc
            total_vat += vat_amt
            total_net += line_tot
            total_cogs += line_cogs
            total_profit += line_profit

            records.append({
                'id': item.id,
                'estimate_number': est.estimate_number,
                'estimate_pk': est.pk,
                'date_ad': est.bill_date_ad,
                'date_ad_str': est.bill_date_ad.strftime('%Y-%m-%d'),
                'date_bs': est.bill_date_bs or ad_to_bs_string(est.bill_date_ad, lang='en'),
                'fiscal_year': est.fiscal_year or fiscal_year,
                'branch_code': est.branch.code,
                'branch_name': est.branch.name,
                'customer_name': est.recipient_display_name,
                'customer_phone': est.customer_phone_manual or (est.customer.phone_number if est.customer else '-'),
                'product_id': prod.id,
                'product_name': prod.name,
                'sku': prod.sku,
                'barcode': prod.barcode or '',
                'category_name': prod.category.name if prod.category else 'General',
                'brand_name': prod.brand.name if prod.brand else '-',
                'unit_code': prod.base_unit.code if prod.base_unit else 'PCS',
                'quantity': qty,
                'official_unit_price': official_price,
                'unit_price': unit_price,
                'line_gross': line_gross,
                'price_override_amount': override_amt,
                'discount_type': disc_type,
                'discount_input_value': disc_input,
                'item_discount_amount': item_disc,
                'effective_discount_percent': effective_pct,
                'allocated_bill_discount': alloc_bill_disc,
                'total_line_discount': total_line_disc,
                'tax_pricing_type': item.get_tax_pricing_type_display(),
                'vat_rate': item.vat_rate,
                'tax_amount': vat_amt,
                'line_total': line_tot,
                'unit_cost': unit_cost,
                'line_cogs': line_cogs,
                'line_profit': line_profit,
                'line_margin_percent': line_margin_pct,
                'imei_number': item.imei_number or '',
                'secondary_imei': item.secondary_imei or '',
                'serial_number': item.serial_number or '',
                'cashier_username': est.cashier.username,
                'salesperson_username': est.salesperson.username if est.salesperson else est.cashier.username,
                'manager_override_by': est.manager_override_by.username if est.manager_override_by else '',
                'discount_reason': est.discount_reason or '',
                'warranty_terms': item.warranty_terms or '',
            })

        overall_net_rev = max(Decimal('0.00'), total_net - total_vat)
        overall_margin_pct = (
            ((total_profit / overall_net_rev) * Decimal('100.00')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
            if overall_net_rev > Decimal('0.00') else Decimal('0.0')
        )

        totals = {
            'records_count': len(records),
            'total_units': total_units,
            'total_gross_subtotal': total_gross_subtotal,
            'total_price_overrides': total_price_overrides,
            'total_item_discounts': total_item_discounts,
            'total_bill_discounts': total_bill_discounts,
            'total_concessions': total_concessions,
            'total_vat': total_vat,
            'total_net': total_net,
            'total_cogs': total_cogs,
            'total_profit': total_profit,
            'overall_margin_pct': overall_margin_pct,
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
            'categories': ProductCategory.objects.filter(is_active=True).order_by('name'),
            'brands': Brand.objects.all().order_by('name'),
            'staff_members': User.objects.filter(is_active=True).order_by('username'),
            'selected_branch': selected_branch,
            'filters': filters,
            'start_date': start_date.strftime('%Y-%m-%d'),
            'end_date': end_date.strftime('%Y-%m-%d'),
            'start_date_bs': start_date_bs,
            'end_date_bs': end_date_bs,
            'fiscal_year': fiscal_year,
            'available_fiscal_years': cls.get_available_fiscal_years(),
        }