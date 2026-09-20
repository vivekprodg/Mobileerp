"""
Damaged & Quarantined Defective Stock Business Logic Service.
File Path: apps/reports/services/damaged_stock_service.py

Capabilities:
1. Tracks defective parts from customer repair workshop jobs (swollen batteries,
   green line displays, shattered glass panels, broken motherboards) sitting in quarantine
   waiting for authorized distributor RMA return.
2. Tracks defective handsets returned by retail customers (SalesReturnItem with is_defective=True
   and ItemInstance with status='RETURNED_DEFECTIVE').
3. Queries BranchStock where quarantined_defective_quantity > 0.
4. Evaluates total locked capital (quantity * landed acquisition cost).
5. Groups defective items by authorized brand distributor for 1-click RMA batching.
"""

from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime
from typing import Dict, Any, List, Optional
from django.db.models import Q, F, Sum, Count, DecimalField, Value
from django.db.models.functions import Coalesce

from apps.inventory.models import BranchStock, Product, ProductCategory, Brand, ItemInstance
from apps.repairs.models import RepairReplacedPart
from apps.sales.models import SalesReturnItem
from apps.purchases.models import Supplier, GRNItem
from apps.branches.models import Branch
from apps.core.utils.nepali_date_converter import ad_to_bs_string


class DamagedStockService:
    """
    Business logic engine for Report 8: Damaged & Quarantined Defective Stock Report.
    """

    @classmethod
    def get_damaged_stock_data(
        cls,
        filters: Dict[str, Any],
        user=None
    ) -> Dict[str, Any]:
        """
        Compiles all quarantined defective inventory, replaced workshop parts,
        and customer return units with distributor supplier linkages.
        """
        active_branch = getattr(user, 'assigned_branch', None) if user and not user.is_superuser else None

        branch_id = filters.get('branch_id') or filters.get('branch', '')
        category_id = filters.get('category_id') or filters.get('category', '')
        brand_id = filters.get('brand_id') or filters.get('brand', '')
        search_query = str(filters.get('q', '') or '').strip()

        # ---------------------------------------------------------------------
        # 1. SECTION 1: WAREHOUSE QUARANTINED STOCKS (BranchStock)
        # ---------------------------------------------------------------------
        stock_qs = BranchStock.objects.select_related(
            'product',
            'product__category',
            'product__brand',
            'product__base_unit',
            'branch'
        ).filter(quarantined_defective_quantity__gt=Decimal('0.000'))

        if branch_id and str(branch_id).strip() not in ['', 'all', 'None']:
            stock_qs = stock_qs.filter(branch_id=branch_id)
        elif active_branch and not (user.is_superuser or getattr(user, 'role', '') == 'OWNER'):
            stock_qs = stock_qs.filter(branch=active_branch)

        if category_id and str(category_id).strip() not in ['', 'all', 'None']:
            stock_qs = stock_qs.filter(product__category_id=category_id)

        if brand_id and str(brand_id).strip() not in ['', 'all', 'None']:
            stock_qs = stock_qs.filter(product__brand_id=brand_id)

        if search_query:
            stock_qs = stock_qs.filter(
                Q(product__name__icontains=search_query) |
                Q(product__sku__icontains=search_query) |
                Q(product__barcode__icontains=search_query) |
                Q(product__rack_number__icontains=search_query)
            )

        # Batch-map latest supplier for each product
        product_ids = list(stock_qs.values_list('product_id', flat=True).distinct())
        supplier_map: Dict[int, str] = {}
        if product_ids:
            recent_grns = GRNItem.objects.filter(
                product_id__in=product_ids
            ).select_related('grn__supplier').order_by('product_id', '-created_at')

            for g_item in recent_grns:
                if g_item.product_id not in supplier_map and g_item.grn and g_item.grn.supplier:
                    supplier_map[g_item.product_id] = g_item.grn.supplier.company_name

        quarantined_stocks = []
        total_quarantined_units = Decimal('0.000')
        total_quarantined_cost = Decimal('0.00')

        for bs in stock_qs.order_by('-quarantined_defective_quantity'):
            qty = bs.quarantined_defective_quantity
            cost_rate = bs.product.purchase_price or Decimal('0.00')
            line_cost = (qty * cost_rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            total_quarantined_units += qty
            total_quarantined_cost += line_cost

            quarantined_stocks.append({
                'id': bs.id,
                'branch_stock': bs,
                'product': bs.product,
                'branch': bs.branch,
                'quantity': qty,
                'unit_code': bs.product.base_unit.code if bs.product.base_unit else 'PCS',
                'cost_price': cost_rate,
                'selling_price': bs.product.selling_price or Decimal('0.00'),
                'total_cost': line_cost,
                'last_supplier': supplier_map.get(bs.product.id, 'Authorized Distributor'),
                'rack_location': bs.product.rack_number or '-',
            })

        # ---------------------------------------------------------------------
        # 2. SECTION 2: WORKSHOP REPLACED DEFECTIVE PARTS (RepairReplacedPart)
        # ---------------------------------------------------------------------
        part_qs = RepairReplacedPart.objects.select_related(
            'spare_part_product',
            'spare_part_product__brand',
            'spare_part_product__category',
            'ticket',
            'ticket__product',
            'ticket__technician',
            'branch'
        ).filter(defective_part_status='QUARANTINED_FOR_RMA')

        if branch_id and str(branch_id).strip() not in ['', 'all', 'None']:
            part_qs = part_qs.filter(branch_id=branch_id)
        elif active_branch and not (user.is_superuser or getattr(user, 'role', '') == 'OWNER'):
            part_qs = part_qs.filter(branch=active_branch)

        if search_query:
            part_qs = part_qs.filter(
                Q(spare_part_product__name__icontains=search_query) |
                Q(old_part_serial_or_batch__icontains=search_query) |
                Q(ticket__ticket_number__icontains=search_query) |
                Q(ticket__imei_or_serial__icontains=search_query) |
                Q(ticket__reported_fault__icontains=search_query)
            )

        defective_parts = []
        total_repair_parts_units = Decimal('0.000')
        total_repair_parts_cost = Decimal('0.00')

        for part in part_qs.order_by('-created_at'):
            p_qty = part.quantity or Decimal('1.000')
            p_cost = part.cost_price or part.spare_part_product.purchase_price or Decimal('0.00')
            line_tot = (p_qty * p_cost).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            total_repair_parts_units += p_qty
            total_repair_parts_cost += line_tot

            # Defect justification
            defect_note = "Genuine Factory Component Failure"
            if hasattr(part.ticket, 'defect_verdict') and part.ticket.defect_verdict.technical_justification:
                defect_note = part.ticket.defect_verdict.technical_justification
            elif part.ticket.reported_fault:
                defect_note = part.ticket.reported_fault

            date_bs = ad_to_bs_string(part.created_at.date(), lang='en')

            defective_parts.append({
                'id': part.id,
                'part': part,
                'product': part.spare_part_product,
                'branch': part.branch,
                'ticket': part.ticket,
                'quantity': p_qty,
                'serial_or_barcode': part.old_part_serial_or_batch or part.ticket.imei_or_serial or '-',
                'defect_description': defect_note,
                'technician': part.ticket.technician,
                'cost_price': p_cost,
                'total_cost': line_tot,
                'created_at': part.created_at,
                'date_ad': part.created_at.strftime('%Y-%m-%d'),
                'date_bs': date_bs,
            })

        # ---------------------------------------------------------------------
        # 3. SECTION 3: DEFECTIVE SERIALIZED HANDSETS (ItemInstance)
        # ---------------------------------------------------------------------
        handset_qs = ItemInstance.objects.select_related(
            'product',
            'product__brand',
            'product__category',
            'branch'
        ).filter(status='RETURNED_DEFECTIVE')

        if branch_id and str(branch_id).strip() not in ['', 'all', 'None']:
            handset_qs = handset_qs.filter(branch_id=branch_id)
        elif active_branch and not (user.is_superuser or getattr(user, 'role', '') == 'OWNER'):
            handset_qs = handset_qs.filter(branch=active_branch)

        if search_query:
            handset_qs = handset_qs.filter(
                Q(imei_1__icontains=search_query) |
                Q(imei_2__icontains=search_query) |
                Q(serial_number__icontains=search_query) |
                Q(product__name__icontains=search_query) |
                Q(product__sku__icontains=search_query) |
                Q(warranty_remarks__icontains=search_query)
            )

        defective_handsets = []
        total_handsets_cost = Decimal('0.00')

        for h in handset_qs.order_by('-created_at'):
            cost = h.landed_cost or h.product.purchase_price or Decimal('0.00')
            total_handsets_cost += cost

            defective_handsets.append({
                'id': h.id,
                'instance': h,
                'product': h.product,
                'branch': h.branch,
                'imei_1': h.imei_1 or '-',
                'imei_2': h.imei_2 or '-',
                'serial_number': h.serial_number or '-',
                'condition': h.get_condition_display(),
                'mdms_status': h.get_mdms_status_display(),
                'landed_cost': cost,
                'selling_price': h.product.selling_price or Decimal('0.00'),
                'inward_reference': h.purchase_reference or '-',
                'defect_note': h.warranty_remarks or 'Customer Defective Return / DOA',
            })

        # ---------------------------------------------------------------------
        # 4. GRAND CONSOLIDATED TOTALS
        # ---------------------------------------------------------------------
        grand_total_locked_capital = total_quarantined_cost + total_repair_parts_cost + total_handsets_cost
        grand_total_defective_items = int(total_quarantined_units) + len(defective_parts) + len(defective_handsets)

        totals = {
            'total_quarantined_units': total_quarantined_units,
            'total_quarantined_cost': total_quarantined_cost,
            'total_repair_parts_count': len(defective_parts),
            'total_repair_parts_units': total_repair_parts_units,
            'total_repair_parts_cost': total_repair_parts_cost,
            'total_handsets_count': len(defective_handsets),
            'total_handsets_cost': total_handsets_cost,
            'grand_total_locked_capital': grand_total_locked_capital,
            'grand_total_defective_items': grand_total_defective_items,
        }

        selected_branch = None
        if branch_id and branch_id != 'all':
            selected_branch = Branch.objects.filter(id=branch_id).first()
        elif active_branch:
            selected_branch = active_branch

        return {
            'quarantined_stocks': quarantined_stocks,
            'defective_parts': defective_parts,
            'defective_handsets': defective_handsets,
            'totals': totals,
            'total_quarantined_units': totals['total_quarantined_units'],
            'total_quarantined_cost': totals['total_quarantined_cost'],
            'grand_total_locked_capital': totals['grand_total_locked_capital'],
            'branches': Branch.objects.filter(is_active=True).order_by('-is_main_branch', 'name'),
            'categories': ProductCategory.objects.filter(is_active=True).order_by('name'),
            'brands': Brand.objects.all().order_by('name'),
            'selected_branch': selected_branch,
            'filters': filters,
        }