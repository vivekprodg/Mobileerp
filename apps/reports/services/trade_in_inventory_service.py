"""
Pre-Owned / Traded-In Phone Inventory & Margin Business Logic Service.
File Path: apps/reports/services/trade_in_inventory_service.py

Capabilities:
1. Audits second-hand smartphones acquired from retail customers via trade-in exchange vouchers.
2. Cross-references ItemInstance where source_type = 'CUSTOMER_EXCHANGE_TRADE_IN' with PhoneExchangeTradeIn.
3. Tracks 10-point technical diagnosis scores, physical condition grades (Grade A, B, C, Refurbished),
   NTA MDMS verification, and seller customer identification.
4. Audits compliance with statutory police anti-theft undertakings (जिम्मानामा तथा मञ्जुरीनामा).
5. Computes buy-back landed costs, counter resale MRPs, realized/projected profit margins, and percentage markups.
"""

from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Any, List, Optional
from django.db.models import Q
from django.utils import timezone

from apps.inventory.models import ItemInstance, Product, Brand, ProductCategory
from apps.sales.models import PhoneExchangeTradeIn, TradeInLegalUndertaking
from apps.branches.models import Branch


class TradeInInventoryService:
    """
    Business logic engine for Report 12: Trade-In / Pre-Owned Phone Inventory Report.
    """

    @classmethod
    def get_trade_in_inventory_data(
        cls,
        filters: Dict[str, Any],
        user=None
    ) -> Dict[str, Any]:
        """
        Compiles all pre-owned buy-back handset inventory with profit margins,
        police compliance status, and seller details.
        """
        active_branch = getattr(user, 'assigned_branch', None) if user and not user.is_superuser else None

        branch_id = filters.get('branch_id') or filters.get('branch', '')
        status_filter = str(filters.get('status', '') or '').strip().upper()
        grade_filter = str(filters.get('grade', '') or filters.get('condition_grade', '') or '').strip().upper()
        mdms_filter = str(filters.get('mdms_status', '') or '').strip()
        undertaking_filter = str(filters.get('undertaking', '') or '').strip().lower()
        search_query = str(filters.get('q', '') or '').strip()

        # 1. Base Query: Only handsets originating from Customer Exchange Buy-Back
        qs = ItemInstance.objects.select_related(
            'product',
            'product__brand',
            'product__category',
            'branch'
        ).filter(source_type='CUSTOMER_EXCHANGE_TRADE_IN')

        # 2. Branch Scoping
        if branch_id and str(branch_id).strip() not in ['', 'all', 'None']:
            qs = qs.filter(branch_id=branch_id)
        elif active_branch and not (user.is_superuser or getattr(user, 'role', '') == 'OWNER'):
            qs = qs.filter(branch=active_branch)

        # 3. Status Filtering (IN_STOCK, SOLD, etc.)
        if status_filter and status_filter not in ['', 'ALL']:
            qs = qs.filter(status=status_filter)

        # 4. Condition Grade Filter
        if grade_filter and grade_filter not in ['', 'ALL']:
            qs = qs.filter(condition=grade_filter)

        # 5. NTA MDMS Filter
        if mdms_filter and mdms_filter not in ['', 'ALL']:
            qs = qs.filter(mdms_status=mdms_filter)

        # 6. Keyword Search Filter
        if search_query:
            qs = qs.filter(
                Q(imei_1__icontains=search_query) |
                Q(imei_2__icontains=search_query) |
                Q(serial_number__icontains=search_query) |
                Q(device_uid__icontains=search_query) |
                Q(trade_in_voucher_reference__icontains=search_query) |
                Q(product__name__icontains=search_query) |
                Q(customer_name__icontains=search_query) |
                Q(customer_phone__icontains=search_query) |
                Q(sold_invoice_reference__icontains=search_query)
            )

        # 7. Batch Map Trade-In Vouchers and Legal Undertakings to Eliminate N+1 Queries
        voucher_refs = list(qs.values_list('trade_in_voucher_reference', flat=True).distinct())
        voucher_refs = [v for v in voucher_refs if v]

        vouchers_map: Dict[str, PhoneExchangeTradeIn] = {}
        if voucher_refs:
            v_list = PhoneExchangeTradeIn.objects.filter(
                voucher_number__in=voucher_refs
            ).select_related('inspection_checklist', 'legal_undertaking', 'cashier')
            for v in v_list:
                vouchers_map[v.voucher_number] = v

        # 8. Build Detailed Records & Calculate Financials
        records: List[Dict[str, Any]] = []
        total_buyback_capital = Decimal('0.00')
        total_resale_revenue = Decimal('0.00')
        total_realized_profit = Decimal('0.00')
        in_stock_count = 0
        in_stock_capital = Decimal('0.00')
        sold_count = 0
        undertakings_signed_count = 0

        for item in qs.order_by('-created_at'):
            voucher = vouchers_map.get(item.trade_in_voucher_reference)
            undertaking = getattr(voucher, 'legal_undertaking', None) if voucher else None
            checklist = getattr(voucher, 'inspection_checklist', None) if voucher else None

            has_undertaking = bool(undertaking and undertaking.declaration_accepted)

            # Undertaking filter check
            if undertaking_filter == 'signed' and not has_undertaking:
                continue
            if undertaking_filter == 'pending' and has_undertaking:
                continue

            if has_undertaking:
                undertakings_signed_count += 1

            buyback_cost = item.landed_cost or (voucher.final_trade_in_value if voucher else item.product.purchase_price) or Decimal('0.00')
            total_buyback_capital += buyback_cost

            # Resale Price Determination
            if item.status == 'SOLD':
                sold_count += 1
                selling_price = item.sold_price or item.product.selling_price or Decimal('0.00')
                total_resale_revenue += selling_price
                margin = (selling_price - buyback_cost).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                total_realized_profit += margin
                is_sold = True
            else:
                if item.status == 'IN_STOCK':
                    in_stock_count += 1
                    in_stock_capital += buyback_cost
                selling_price = item.product.selling_price or Decimal('0.00')
                margin = (selling_price - buyback_cost).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                is_sold = False

            margin_pct = (
                ((margin / selling_price) * Decimal('100.00')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
                if selling_price > Decimal('0.00') else Decimal('0.0')
            )

            seller_name = voucher.customer_name_manual if voucher else (item.customer_name or 'Walk-in Seller')
            seller_phone = voucher.customer_phone_manual if voucher else (item.customer_phone or '')

            records.append({
                'id': item.id,
                'instance': item,
                'voucher': voucher,
                'voucher_number': item.trade_in_voucher_reference or (voucher.voucher_number if voucher else '-'),
                'voucher_pk': voucher.pk if voucher else None,
                'product': item.product,
                'brand_name': voucher.brand_name if voucher else (item.product.brand.name if item.product.brand else 'Pre-Owned'),
                'model_name': voucher.model_name if voucher else item.product.name,
                'specs': f"{voucher.storage_capacity if voucher else item.product.internal_storage or ''} {voucher.color_variant if voucher else item.product.color_variant or ''}".strip(),
                'imei_1': item.imei_1 or '-',
                'imei_2': item.imei_2 or '-',
                'condition_grade': item.condition,
                'condition_display': item.get_condition_display(),
                'score_percent': checklist.diagnostic_score_percent if checklist else None,
                'mdms_status': item.mdms_status,
                'mdms_display': item.get_mdms_status_display(),
                'status': item.status,
                'status_display': item.get_status_display(),
                'is_sold': is_sold,
                'sold_invoice': item.sold_invoice_reference or '-',
                'sold_date': item.sale_date,
                'buyer_name': item.customer_name if is_sold else None,
                'seller_name': seller_name,
                'seller_phone': seller_phone,
                'buyback_cost': buyback_cost,
                'selling_price': selling_price,
                'realized_margin': margin,
                'margin_percent': margin_pct,
                'has_undertaking': has_undertaking,
                'undertaking_id': undertaking.id_number if undertaking else None,
                'branch': item.branch,
                'branch_code': item.branch.code,
                'created_at': item.created_at,
            })

        compliance_rate = (
            round((undertakings_signed_count / len(records)) * 100, 1)
            if records else 100.0
        )

        overall_margin_pct = (
            round((total_realized_profit / total_resale_revenue) * 100, 1)
            if total_resale_revenue > 0 else 0.0
        )

        totals = {
            'total_traded_phones': len(records),
            'in_stock_count': in_stock_count,
            'in_stock_capital': in_stock_capital,
            'sold_count': sold_count,
            'total_buyback_capital': total_buyback_capital,
            'total_resale_revenue': total_resale_revenue,
            'total_realized_profit': total_realized_profit,
            'overall_margin_pct': overall_margin_pct,
            'undertakings_signed_count': undertakings_signed_count,
            'compliance_rate': compliance_rate,
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
            'conditions': ItemInstance.CONDITION_CHOICES,
            'mdms_statuses': Product.MDMS_STATUS_CHOICES,
            'selected_branch': selected_branch,
            'filters': filters,
        }