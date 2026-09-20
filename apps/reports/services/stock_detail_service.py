"""
Stock Detail Ledger (Bin Card) Service.
File Path: D:\Mobile Shop\Inventory\apps\reports\services\stock_detail_service.py

Calculates an item-specific stock movement ledger with accurate historical opening balances,
running inventory balances, transaction rates, and Nepali BS dates matching the client's specification:
[S.N. | Date | Invoice Type | Invoice Number | In Stock | Out Stock | Unit | Price | Balance | Remarks]
"""

from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime, time
from typing import Dict, Any, List, Optional, Tuple

from django.db.models import Sum, Q
from django.utils import timezone

from apps.inventory.models import Product, BranchStock, StockMovementLog
from apps.branches.models import Branch
from apps.sales.models import SalesEstimateItem
from apps.purchases.models import GRNItem
from apps.core.nepali_calendar import NepaliCalendar

class StockDetailService:
    """
    Business logic engine for the Stock Detail Report (Bin Card / Running Ledger).
    Computes opening balance before start_date, then builds line-by-line running stock balances.
    """

    MOVEMENT_TYPE_LABELS = {
        'PURCHASE': 'Purchase Inward (GRN)',
        'SALE': 'Sales Invoice (POS)',
        'SALE_RETURN': 'Customer Sales Return',
        'TRADE_IN_ACQUISITION': 'Trade-In Buy-Back (+)',
        'TRADE_IN_SALE': 'Used Phone Sale (-)',
        'TRANSFER_IN': 'Branch Transfer In (+)',
        'TRANSFER_OUT': 'Branch Transfer Out (-)',
        'ADJUSTMENT_ADD': 'Stock Audit Correction (+)',
        'ADJUSTMENT_SUB': 'Damage / Lost / Expired (-)',
        'SERVICE_INTAKE': 'Repair Workshop Intake',
        'SERVICE_REPLACED_PART_DEDUCT': 'Repair Part Installed (-)',
        'SERVICE_DEFECTIVE_QUARANTINE': 'Defective Part Quarantined (+)',
        'RMA_VENDOR_DISPATCH': 'Vendor RMA Dispatch (-)',
        'RMA_VENDOR_REPLACEMENT_IN': 'Vendor RMA Replacement (+)',
    }

    @classmethod
    def resolve_date_range(cls, raw_params: Dict[str, Any]) -> Tuple[date, date, str, str]:
        """
        Resolves start and end dates. Defaults to the 1st of the current Nepali BS month up to today.
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
    def get_stock_detail(
        cls,
        filters: Dict[str, Any],
        user=None
    ) -> Dict[str, Any]:
        """
        Computes item-level chronological stock movements and calculates running balance row by row.
        """
        start_date, end_date, start_date_bs, end_date_bs = cls.resolve_date_range(filters)
        start_datetime = timezone.make_aware(datetime.combine(start_date, time.min))
        end_datetime = timezone.make_aware(datetime.combine(end_date, time.max))

        product_id = filters.get('product_id')
        product = None
        if product_id and str(product_id).strip() not in ['', 'None']:
            product = Product.objects.select_related('category', 'brand', 'base_unit').filter(id=product_id).first()

        # Fallback to search query if no product_id was selected
        if not product:
            search_query = str(filters.get('q', '') or '').strip()
            sku_query = str(filters.get('sku', '') or '').strip()
            lookup = search_query or sku_query
            if lookup:
                product = Product.objects.select_related('category', 'brand', 'base_unit').filter(
                    Q(sku__iexact=lookup) | Q(barcode__iexact=lookup) | Q(name__icontains=lookup)
                ).first()

        branch_id = filters.get('branch_id')
        selected_branch = None
        if branch_id and str(branch_id).strip() not in ['', 'all', 'None']:
            selected_branch = Branch.objects.filter(id=branch_id).first()
        elif user and not user.is_superuser:
            selected_branch = getattr(user, 'assigned_branch', None)

        if not product:
            return {
                'product': None,
                'selected_branch': selected_branch,
                'rows': [],
                'opening_balance': Decimal('0.000'),
                'closing_balance': Decimal('0.000'),
                'total_in': Decimal('0.000'),
                'total_out': Decimal('0.000'),
                'start_date': start_date,
                'end_date': end_date,
                'start_date_bs': start_date_bs,
                'end_date_bs': end_date_bs,
            }

        # ---------------------------------------------------------------------
        # 1. CALCULATE OPENING BALANCE
        # Formula: Opening Balance = Current Live Stock - (All movements from start_datetime to Now)
        # ---------------------------------------------------------------------
        stock_qs = BranchStock.objects.filter(product=product)
        if selected_branch:
            stock_qs = stock_qs.filter(branch=selected_branch)

        current_live_stock = stock_qs.aggregate(total=Sum('quantity'))['total'] or Decimal('0.000')

        movements_from_start_to_now_qs = StockMovementLog.objects.filter(
            product=product,
            created_at__gte=start_datetime
        )
        if selected_branch:
            movements_from_start_to_now_qs = movements_from_start_to_now_qs.filter(branch=selected_branch)

        net_movements_since_start = movements_from_start_to_now_qs.aggregate(
            total_delta=Sum('quantity_delta')
        )['total_delta'] or Decimal('0.000')

        opening_balance = current_live_stock - net_movements_since_start

        # ---------------------------------------------------------------------
        # 2. QUERY MOVEMENTS IN CHOSEN DATE PERIOD
        # ---------------------------------------------------------------------
        period_movements_qs = StockMovementLog.objects.filter(
            product=product,
            created_at__gte=start_datetime,
            created_at__lte=end_datetime
        ).select_related('branch', 'user').order_by('created_at', 'id')

        if selected_branch:
            period_movements_qs = period_movements_qs.filter(branch=selected_branch)

        movement_logs = list(period_movements_qs)

        # ---------------------------------------------------------------------
        # 3. PRE-FETCH TRANSACTION RATES TO PREVENT N+1 QUERIES
        # ---------------------------------------------------------------------
        ref_docs = [log.reference_document for log in movement_logs if log.reference_document]
        price_map: Dict[str, Decimal] = {}

        if ref_docs:
            sales_items = SalesEstimateItem.objects.filter(
                product=product,
                estimate__estimate_number__in=ref_docs
            ).values('estimate__estimate_number', 'unit_price')
            for item in sales_items:
                price_map[item['estimate__estimate_number']] = item['unit_price']

            grn_items = GRNItem.objects.filter(
                product=product,
                grn__grn_number__in=ref_docs
            ).values('grn__grn_number', 'purchase_rate')
            for item in grn_items:
                price_map[item['grn__grn_number']] = item['purchase_rate']

        # ---------------------------------------------------------------------
        # 4. STEP THROUGH MOVEMENTS & CALCULATE RUNNING BALANCE
        # ---------------------------------------------------------------------
        running_balance = opening_balance
        total_in = Decimal('0.000')
        total_out = Decimal('0.000')
        rows = []

        unit_code = product.base_unit.code if product.base_unit else 'Pcs'

        for idx, log in enumerate(movement_logs, start=1):
            delta = log.quantity_delta or Decimal('0.000')
            in_stock = Decimal('0.000')
            out_stock = Decimal('0.000')

            if delta >= Decimal('0.000'):
                in_stock = delta
                total_in += in_stock
            else:
                out_stock = abs(delta)
                total_out += out_stock

            running_balance += delta

            # Transaction rate resolution
            price = Decimal('0.00')
            if log.reference_document and log.reference_document in price_map:
                price = price_map[log.reference_document]
            elif log.movement_type in ['SALE', 'TRADE_IN_SALE']:
                price = product.selling_price
            else:
                price = product.purchase_price

            log_date_ad = log.created_at.date()
            y, m, d = NepaliCalendar.ad_to_bs(log_date_ad)
            log_date_bs = NepaliCalendar.format_bs(y, m, d, lang='en')

            # Build Remarks string
            remarks_parts = []
            if log.remarks:
                remarks_parts.append(log.remarks.strip())
            if log.imei_or_serial_number:
                remarks_parts.append(f"IMEI/SN: {log.imei_or_serial_number.strip()}")
            if not selected_branch and log.branch:
                remarks_parts.append(f"Branch: {log.branch.code}")

            remarks_str = " | ".join(remarks_parts) if remarks_parts else "-"

            invoice_type_label = cls.MOVEMENT_TYPE_LABELS.get(
                log.movement_type,
                log.get_movement_type_display() if hasattr(log, 'get_movement_type_display') else log.movement_type
            )

            rows.append({
                'sn': idx,
                'date_ad': log_date_ad,
                'date_bs': log_date_bs,
                'created_at': log.created_at,
                'invoice_type': invoice_type_label,
                'movement_type_raw': log.movement_type,
                'invoice_number': log.reference_document or '-',
                'in_stock': in_stock,
                'out_stock': out_stock,
                'unit': unit_code,
                'price': price,
                'balance': running_balance,
                'remarks': remarks_str,
                'user': log.user.username if log.user else 'System',
                'branch_code': log.branch.code if log.branch else '-'
            })

        return {
            'product': product,
            'selected_branch': selected_branch,
            'rows': rows,
            'opening_balance': opening_balance,
            'closing_balance': running_balance,
            'total_in': total_in,
            'total_out': total_out,
            'start_date': start_date,
            'end_date': end_date,
            'start_date_bs': start_date_bs,
            'end_date_bs': end_date_bs,
        }