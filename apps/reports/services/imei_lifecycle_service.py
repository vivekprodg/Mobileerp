"""
Forensic Single-IMEI Lifecycle Journey & Audit History Service.
File Path: apps/reports/services/imei_lifecycle_service.py

Traces the entire cradle-to-grave lifecycle of any physical smartphone:
1. When was it received via GRN? (Date, Supplier, Landed Cost, Bill No)
2. Was it transferred between store branches? (Dates, Origin, Destination, Transit Status)
3. When was it sold via POS? (Bill No, Customer Name, Phone, Selling Price, Cashier, Salesperson)
4. Were there any component warranty claims or repair workshop tickets?
5. Was it ever returned, quarantined for RMA, or traded back in as a pre-owned handset?
"""

from datetime import date, datetime, time
from decimal import Decimal
from typing import Dict, Any, List, Optional
from django.db.models import Q
from django.utils import timezone

from apps.inventory.models import ItemInstance, DeviceComponentWarranty, StockMovementLog
from apps.branches.models import StockTransferItem
from apps.sales.models import SalesEstimateItem, PhoneExchangeTradeIn, SalesReturnItem
from apps.repairs.models import RepairTicket
from apps.purchases.models import GoodsReceivedNote, GRNItem
from apps.core.utils.nepali_date_converter import ad_to_bs_string


class IMEILifecycleService:
    """
    Business logic engine compiling the forensic chronological journey of a single IMEI.
    """

    @classmethod
    def get_lifecycle_data(
        cls,
        search_query: str,
        user=None
    ) -> Dict[str, Any]:
        """
        Accepts any single IMEI 1, IMEI 2, Serial Number, or Device UID,
        retrieves the physical ItemInstance and cross-queries all related procurement,
        transfer, sales, repair, and trade-in ledgers into a sorted timeline.
        """
        clean_key = str(search_query or '').strip()

        if not clean_key:
            return {
                'query': '',
                'device': None,
                'timeline': [],
                'total_events_count': 0,
                'is_found': False,
            }

        # 1. Look up physical device instance
        device = ItemInstance.objects.select_related(
            'product',
            'product__brand',
            'product__category',
            'product__base_unit',
            'branch'
        ).filter(
            Q(imei_1__iexact=clean_key) |
            Q(imei_2__iexact=clean_key) |
            Q(serial_number__iexact=clean_key) |
            Q(device_uid__iexact=clean_key)
        ).first()

        # Fallback: Check if IMEI exists in sales or trade-in records even if instance was archived
        if not device:
            trade_match = PhoneExchangeTradeIn.objects.filter(
                Q(imei_1__iexact=clean_key) | Q(imei_2__iexact=clean_key)
            ).first()
            if trade_match and trade_match.restocked_item_instance:
                device = trade_match.restocked_item_instance

        if not device:
            sale_line = SalesEstimateItem.objects.filter(
                Q(imei_number__iexact=clean_key) | Q(secondary_imei__iexact=clean_key)
            ).select_related('item_instance').first()
            if sale_line and sale_line.item_instance:
                device = sale_line.item_instance

        if not device:
            return {
                'query': clean_key,
                'device': None,
                'timeline': [],
                'total_events_count': 0,
                'is_found': False,
            }

        primary_imei = device.imei_1 or clean_key
        secondary_imei = device.imei_2 or ''

        # 2. Compile Unified Forensic Timeline Events
        raw_events: List[Dict[str, Any]] = []

        # EVENT A: Procurement / GRN Inward
        if device.purchase_date:
            try:
                grn_dt = timezone.make_aware(datetime.combine(device.purchase_date, time.min))
            except Exception:
                grn_dt = datetime.combine(device.purchase_date, time.min)

            raw_events.append({
                'event_type': 'GRN_INWARD',
                'title': 'Goods Received Note (GRN) Inward Procurement',
                'timestamp': grn_dt,
                'branch_name': device.branch.name,
                'badge_color': 'success',
                'icon_class': 'fas fa-truck-ramp-box',
                'reference_doc': device.purchase_reference or 'GRN',
                'actor': device.supplier_name or 'Supplier / Distributor',
                'details': (
                    f"Received at {device.branch.name}. Supplier: {device.supplier_name or 'Authorized Distributor'} | "
                    f"Batch Ref: {device.batch_reference or 'Standard'} | Landed Acquisition Cost: Rs. {device.landed_cost:,.2f}."
                )
            })

        # EVENT B: Trade-In Acquisition (If device was acquired as a customer buy-back)
        if device.source_type == 'CUSTOMER_EXCHANGE_TRADE_IN' or device.trade_in_voucher_reference:
            trade_vouchers = PhoneExchangeTradeIn.objects.filter(
                Q(voucher_number=device.trade_in_voucher_reference) |
                Q(imei_1__iexact=primary_imei) |
                Q(restocked_item_instance=device)
            ).select_related('branch', 'cashier', 'customer')

            for tv in trade_vouchers:
                undertaking_signed = hasattr(tv, 'legal_undertaking')
                raw_events.append({
                    'event_type': 'TRADE_IN_BUYBACK',
                    'title': f"Customer Buy-Back / Trade-In ({tv.voucher_number})",
                    'timestamp': tv.created_at,
                    'branch_name': tv.branch.name,
                    'badge_color': 'danger',
                    'icon_class': 'fas fa-repeat',
                    'reference_doc': tv.voucher_number,
                    'actor': tv.cashier.username if tv.cashier else 'Counter Staff',
                    'details': (
                        f"Acquired from {tv.customer_name_manual} ({tv.customer_phone_manual}). "
                        f"Valuation Payout: Rs. {tv.final_trade_in_value:,.2f} | Condition: {tv.get_recommended_condition_grade_display()} | "
                        f"Police Undertaking Form: {'Signed & Verified' if undertaking_signed else 'Pending'}."
                    )
                })

        # EVENT C: Inter-Branch Stock Transfers
        transfers = StockTransferItem.objects.filter(
            Q(item_instance=device) |
            Q(scanned_imei_or_serial__icontains=primary_imei)
        ).select_related(
            'transfer_request',
            'transfer_request__source_branch',
            'transfer_request__destination_branch',
            'transfer_request__requested_by',
            'transfer_request__dispatched_by',
            'transfer_request__received_by'
        )

        for t_item in transfers:
            trf = t_item.transfer_request
            raw_events.append({
                'event_type': 'BRANCH_TRANSFER',
                'title': f"Inter-Branch Transfer ({trf.get_status_display()})",
                'timestamp': trf.created_at,
                'branch_name': f"{trf.source_branch.code} → {trf.destination_branch.code}",
                'badge_color': 'info',
                'icon_class': 'fas fa-dolly',
                'reference_doc': trf.transfer_no,
                'actor': trf.requested_by.username if trf.requested_by else 'System',
                'details': (
                    f"Consignment {trf.transfer_no}: Origin: {trf.source_branch.name} → Destination: {trf.destination_branch.name}. "
                    f"Requested by {trf.requested_by.username if trf.requested_by else 'Staff'}. "
                    f"Dispatched by: {trf.dispatched_by.username if trf.dispatched_by else 'Pending'}, "
                    f"Received by: {trf.received_by.username if trf.received_by else 'In-Transit'}."
                )
            })

        # EVENT D: POS Retail Sales & Invoicing
        sales_lines = SalesEstimateItem.objects.filter(
            Q(item_instance=device) |
            Q(imei_number__iexact=primary_imei) |
            (Q(secondary_imei__iexact=secondary_imei) if secondary_imei else Q(pk__in=[]))
        ).select_related(
            'estimate',
            'estimate__branch',
            'estimate__customer',
            'estimate__cashier',
            'estimate__salesperson'
        )

        for s_line in sales_lines:
            est = s_line.estimate
            cust_label = est.recipient_display_name
            cust_phone = est.customer_phone_manual or (est.customer.phone_number if est.customer else '-')
            salesperson_label = est.salesperson.username if est.salesperson else est.cashier.username

            raw_events.append({
                'event_type': 'POS_SALE',
                'title': f"POS Customer Sale ({est.estimate_number})",
                'timestamp': est.created_at,
                'branch_name': est.branch.name,
                'badge_color': 'primary',
                'icon_class': 'fas fa-receipt',
                'reference_doc': est.estimate_number,
                'actor': est.cashier.username,
                'details': (
                    f"Sold to {cust_label} (Phone: {cust_phone}) for Rs. {s_line.unit_price:,.2f}. "
                    f"Cashier: {est.cashier.username} | Sales Rep: {salesperson_label} | "
                    f"Status: {est.get_status_display()} ({est.get_payment_status_display()})."
                )
            })

        # EVENT E: Customer Warranty Registration
        warranties = DeviceComponentWarranty.objects.filter(item_instance=device)
        for w in warranties:
            try:
                w_dt = timezone.make_aware(datetime.combine(w.warranty_start_date, time.min))
            except Exception:
                w_dt = datetime.combine(w.warranty_start_date, time.min)

            raw_events.append({
                'event_type': 'WARRANTY_REGISTRATION',
                'title': f"Warranty Card Stamped: {w.component_name}",
                'timestamp': w_dt,
                'branch_name': device.branch.name,
                'badge_color': 'secondary',
                'icon_class': 'fas fa-shield-halved',
                'reference_doc': f"{w.component_name} ({w.warranty_months}M)",
                'actor': 'System Warranty Stamping',
                'details': (
                    f"Warranty active for {w.warranty_months} months until {w.warranty_expiry_date}. "
                    f"Status: {w.get_status_display()} (Claim count: {w.claim_count})."
                )
            })

        # EVENT F: Repair Tickets & Service Laboratory Claims
        repairs = RepairTicket.objects.filter(
            Q(item_instance=device) |
            Q(imei_or_serial__iexact=primary_imei) |
            (Q(imei_or_serial__iexact=secondary_imei) if secondary_imei else Q(pk__in=[]))
        ).select_related('branch', 'technician')

        for rep in repairs:
            tech_label = rep.technician.username if rep.technician else 'Unassigned Tech'
            raw_events.append({
                'event_type': 'SERVICE_REPAIR',
                'title': f"Repair Ticket Created ({rep.ticket_number})",
                'timestamp': rep.created_at,
                'branch_name': rep.branch.name,
                'badge_color': 'warning',
                'icon_class': 'fas fa-screwdriver-wrench',
                'reference_doc': rep.ticket_number,
                'actor': tech_label,
                'details': (
                    f"Fault: {rep.reported_fault} | Claim: {rep.get_claim_type_display()} | "
                    f"Status: {rep.get_service_status_display()} | Total Amount: Rs. {rep.final_total_amount:,.2f}."
                )
            })

        # EVENT G: Customer Sales Returns
        returns = SalesReturnItem.objects.filter(
            Q(estimate_item__item_instance=device) |
            Q(returned_imei__iexact=primary_imei)
        ).select_related('sales_return', 'sales_return__branch', 'sales_return__processed_by')

        for ret_item in returns:
            ret_voucher = ret_item.sales_return
            raw_events.append({
                'event_type': 'SALES_RETURN',
                'title': f"Customer Sales Return ({ret_voucher.return_number})",
                'timestamp': ret_voucher.created_at,
                'branch_name': ret_voucher.branch.name,
                'badge_color': 'danger',
                'icon_class': 'fas fa-rotate-left',
                'reference_doc': ret_voucher.return_number,
                'actor': ret_voucher.processed_by.username if ret_voucher.processed_by else 'Staff',
                'details': (
                    f"Returned under voucher {ret_voucher.return_number}. Reason: {ret_voucher.reason}. "
                    f"Defective: {'Yes (Quarantined)' if ret_item.is_defective else 'No (Restocked to sellable)'}. "
                    f"Refund Amount: Rs. {ret_item.refund_amount:,.2f}."
                )
            })

        # 3. Sort Chronological Events (Newest on Top)
        raw_events.sort(key=lambda x: x['timestamp'], reverse=True)

        # 4. Attach formatted AD and BS date strings (Using Python dictionary unpacking)
        formatted_timeline = []
        for ev in raw_events:
            dt_ad = ev['timestamp'].date() if hasattr(ev['timestamp'], 'date') else ev['timestamp']
            formatted_timeline.append({
                **ev,
                'date_ad_str': dt_ad.strftime('%Y-%m-%d') if hasattr(dt_ad, 'strftime') else str(dt_ad),
                'date_bs_str': ad_to_bs_string(dt_ad, lang='en') if dt_ad else '',
                'time_str': ev['timestamp'].strftime('%H:%M') if hasattr(ev['timestamp'], 'strftime') else '',
            })

        return {
            'query': clean_key,
            'device': device,
            'timeline': formatted_timeline,
            'total_events_count': len(formatted_timeline),
            'is_found': True,
        }