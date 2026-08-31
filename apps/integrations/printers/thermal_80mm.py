"""
Thermal Receipt ESC/POS Formatter for 80mm (48 Character Fixed Width) Rolls.
Module: apps.integrations.printers.thermal_80mm

Features:
- White-Label Branding: Streams uploaded company logo bitmaps directly to receipt printers (`GS v 0`).
- Dynamic Store Company Name (English + Devanagari Unicode rasterization).
- Dedicated, un-truncated dual IMEI line alignment (`[IMEI 1: ...]` & `[IMEI 2: ...]`).
- Strict 48-column tabular grid formatting with dynamic tax breakdown.
- Formats POS Sales Estimates, Repair Claim Intake Tokens, and Delivery Completion Slips.
"""

from decimal import Decimal
from typing import Optional
from apps.sales.models import SalesEstimate
from apps.repairs.models import RepairTicket
from apps.core.models import SystemConfiguration
from apps.integrations.printers.escpos_driver import RawEscPosBuilder


def contains_unicode(text: Optional[str]) -> bool:
    """
    Checks if a string contains non-ASCII characters (e.g. Nepali Devanagari Unicode script).
    """
    if not text:
        return False
    return any(ord(char) > 127 for char in str(text))


class Thermal80mmFormatter:
    """
    Generates ESC/POS byte streams for standard 80mm thermal printers (576 dots / 48 columns).
    """
    RECEIPT_WIDTH_CHARS = 48

    @classmethod
    def generate_slip_bytes(cls, estimate: SalesEstimate, config: SystemConfiguration) -> bytes:
        """
        Compiles complete 80mm thermal estimate slip byte stream with dynamic company branding,
        optional logo bitmap rasterization, dual-IMEI alignment, and proforma disclaimers.
        """
        builder = RawEscPosBuilder()
        branch = estimate.branch
        width = cls.RECEIPT_WIDTH_CHARS

        # Dynamic Company Branding Resolution
        company_title_en = branch.display_company_name if branch else config.company_name_en
        company_title_np = branch.display_company_name_np if branch else config.company_name_np

        # ---------------------------------------------------------------------
        # 1. HEADER: OPTIONAL STORE LOGO & DYNAMIC COMPANY BRANDING
        # ---------------------------------------------------------------------
        # If the branch has an uploaded logo, rasterize and print it at the top
        if branch and branch.logo:
            builder.print_logo_from_field(branch.logo, max_width=384, align='center')

        builder.write(builder.ALIGN_CENTER)
        builder.write(builder.DOUBLE_HEIGHT_ON)
        builder.write(builder.BOLD_ON)
        builder.write_text(f"{company_title_en.upper()}\n")
        builder.write(builder.BOLD_OFF)
        builder.write(builder.NORMAL_TEXT)

        # Render Nepali store name as crisp monochrome bitmap if Devanagari characters are present
        if company_title_np and contains_unicode(company_title_np):
            builder.print_unicode_text_as_image(
                text=company_title_np,
                font_size=24,
                max_width=576,
                align='center'
            )

        builder.write(builder.ALIGN_CENTER)
        builder.write_text(f"{branch.address}, {branch.city}\n")
        builder.write_text(f"Phone: {branch.phone_number}\n")

        if config.pan_number:
            builder.write_text(f"PAN No: {config.pan_number}\n")
        if config.vat_number and config.tax_system_mode == 'VAT':
            builder.write_text(f"VAT Reg No: {config.vat_number}\n")

        # ---------------------------------------------------------------------
        # 2. TITLE BANNER & NON-IRD DISCLAIMER NOTICE
        # ---------------------------------------------------------------------
        builder.double_divider(width=width)
        builder.write(builder.BOLD_ON)
        builder.write_text(f"*** {config.bill_header_title.upper()} ***\n")
        builder.write(builder.BOLD_OFF)
        if config.is_estimation_bill_only:
            builder.write_text("(Internal Estimate / Non-IRD Proforma Voucher)\n")
        builder.double_divider(width=width)

        # ---------------------------------------------------------------------
        # 3. METADATA & STAFF ALLOCATION
        # ---------------------------------------------------------------------
        cashier_label = (
            estimate.cashier.get_full_name() or estimate.cashier.username
            if estimate.cashier else "Counter Staff"
        )
        salesperson_label = (
            estimate.salesperson.get_full_name() or estimate.salesperson.username
            if estimate.salesperson else cashier_label
        )

        builder.write(builder.ALIGN_LEFT)
        builder.write_text(f"Estimate No : {estimate.estimate_number}\n")
        builder.write_text(f"Date (AD)   : {estimate.bill_date_ad} | BS: {estimate.bill_date_bs or '-'}\n")
        builder.write_text(f"Customer    : {estimate.recipient_display_name[:34]}\n")

        cust_phone = estimate.customer_phone_manual or (
            estimate.customer.phone_number if estimate.customer else None
        )
        if cust_phone:
            builder.write_text(f"Cust Phone  : {cust_phone}\n")

        cust_pan = estimate.customer_pan or (
            estimate.customer.pan_number if estimate.customer and estimate.customer.pan_number else None
        )
        if cust_pan:
            builder.write_text(f"Cust PAN    : {cust_pan}\n")

        builder.write_text(f"Sales Rep   : {salesperson_label[:34]}\n")
        builder.write_text(f"Billed By   : {cashier_label[:34]}\n")
        builder.divider('-', width=width)

        # ---------------------------------------------------------------------
        # 4. LINE ITEMS TABLE (48 COLUMNS FIXED WIDTH)
        # ---------------------------------------------------------------------
        has_tax = estimate.vat_amount > Decimal('0.00')

        if has_tax:
            builder.write(builder.BOLD_ON)
            builder.write_text(f"{'Item Description':<20}{'Tax':<6}{'Qty':>4}{'Rate':>8}{'Total':>10}\n")
            builder.write(builder.BOLD_OFF)
        else:
            builder.write(builder.BOLD_ON)
            builder.write_text(f"{'Item Description':<24}{'Qty':>6}{'Rate':>8}{'Total':>10}\n")
            builder.write(builder.BOLD_OFF)

        builder.divider('-', width=width)

        for item in estimate.items.select_related('product', 'item_instance').all():
            qty_str = f"{item.quantity:.0f}" if (item.quantity % 1 == 0) else f"{item.quantity:.2f}"
            rate_str = f"{item.unit_price:.2f}"
            total_str = f"{item.line_total:.2f}"

            if has_tax:
                prod_name = item.product.name[:19]
                tax_tag = (
                    "[INCL]" if item.tax_pricing_type == 'INCLUSIVE'
                    else ("[EXCL]" if item.tax_pricing_type == 'EXCLUSIVE' else "[EXMT]")
                )
                builder.write_text(f"{prod_name:<20}{tax_tag:<6}{qty_str:>4}{rate_str:>8}{total_str:>10}\n")
            else:
                prod_name = item.product.name[:23]
                builder.write_text(f"{prod_name:<24}{qty_str:>6}{rate_str:>8}{total_str:>10}\n")

            # Memory & Color Variant Specification
            if item.product.ram and item.product.internal_storage:
                color_str = f" {item.product.color_variant}" if item.product.color_variant else ""
                builder.write_text(f"  [{item.product.ram}/{item.product.internal_storage}{color_str}]\n")

            # -----------------------------------------------------------------
            # CLEAN DUAL-IMEI INDEPENDENT LINES (No mid-number wrapping)
            # -----------------------------------------------------------------
            if item.imei_number:
                builder.write_text(f"  [IMEI 1: {item.imei_number.strip()}]\n")

            if item.secondary_imei:
                builder.write_text(f"  [IMEI 2: {item.secondary_imei.strip()}]\n")

            if item.serial_number and item.serial_number not in [item.imei_number, item.secondary_imei]:
                builder.write_text(f"  [S/N: {item.serial_number.strip()}]\n")

            # NTA MDMS Compliance Tag
            if item.item_instance:
                mdms_label = item.item_instance.get_mdms_status_display()
                builder.write_text(f"  [NTA MDMS: {mdms_label[:32]}]\n")

            # Warranty Coverage Schedule
            if item.warranty_terms:
                builder.write_text(f"  [Warranty: {item.warranty_terms[:34]}]\n")

        builder.divider('-', width=width)

        # ---------------------------------------------------------------------
        # 5. FINANCIAL TOTALS & TAX BREAKDOWN (RIGHT-ALIGNED)
        # ---------------------------------------------------------------------
        builder.write(builder.ALIGN_RIGHT)
        builder.write_text(f"Gross Subtotal  : Rs. {estimate.subtotal:>10.2f}\n")

        total_disc = estimate.item_discount_total + estimate.bill_discount_amount
        if total_disc > Decimal('0.00'):
            builder.write_text(f"Total Discount  : Rs. -{total_disc:>9.2f}\n")

        if estimate.has_trade_in_exchange and estimate.trade_in_discount_amount > Decimal('0.00'):
            trade_ref = f" ({estimate.trade_in_voucher_reference})" if estimate.trade_in_voucher_reference else ""
            builder.write_text(f"Trade-In Credit{trade_ref[:8]}: Rs. -{estimate.trade_in_discount_amount:>9.2f}\n")

        if estimate.vat_amount > Decimal('0.00'):
            if estimate.taxable_amount > Decimal('0.00'):
                builder.write_text(f"Taxable Base    : Rs. {estimate.taxable_amount:>10.2f}\n")
            tax_label = f"Tax ({config.default_vat_rate}%):" if config.default_vat_rate > Decimal('0.00') else "Tax / VAT:"
            builder.write_text(f"{tax_label:<16}: Rs. {estimate.vat_amount:>10.2f}\n")

        builder.write(builder.BOLD_ON)
        builder.write_text(f"Grand Total     : Rs. {estimate.grand_total:>10.2f}\n")
        builder.write(builder.BOLD_OFF)
        builder.write_text(f"Paid Amount     : Rs. {estimate.paid_amount:>10.2f}\n")

        if estimate.due_amount > Decimal('0.00'):
            builder.write(builder.BOLD_ON)
            builder.write_text(f"Due (Udhaari)   : Rs. {estimate.due_amount:>10.2f}\n")
            builder.write(builder.BOLD_OFF)

        if estimate.change_returned > Decimal('0.00'):
            builder.write_text(f"Change Returned : Rs. {estimate.change_returned:>10.2f}\n")

        builder.divider('-', width=width)

        # ---------------------------------------------------------------------
        # 6. MULTI-MODE PAYMENT SPLITS
        # ---------------------------------------------------------------------
        builder.write(builder.ALIGN_LEFT)
        for pay in estimate.payment_transactions.all():
            mode_label = pay.get_payment_mode_display()
            ref_str = f" ({pay.transaction_ref})" if pay.transaction_ref else ""
            builder.write_text(f"Paid via {mode_label:<14}{ref_str[:12]}: Rs. {pay.amount:.2f}\n")

        builder.divider('-', width=width)

        # ---------------------------------------------------------------------
        # 7. PROFORMA ESTIMATION NOTICE & POLICY FOOTER
        # ---------------------------------------------------------------------
        builder.write(builder.ALIGN_CENTER)

        if config.bill_estimate_disclaimer:
            if contains_unicode(config.bill_estimate_disclaimer):
                builder.print_unicode_text_as_image(
                    text=config.bill_estimate_disclaimer,
                    font_size=20,
                    max_width=576,
                    align='center'
                )
            else:
                builder.write_text(f"{config.bill_estimate_disclaimer}\n")

        if branch.footer_estimate_note:
            if contains_unicode(branch.footer_estimate_note):
                builder.print_unicode_text_as_image(
                    text=branch.footer_estimate_note,
                    font_size=20,
                    max_width=576,
                    align='center'
                )
            else:
                builder.write_text(f"{branch.footer_estimate_note}\n")

        builder.write(builder.ALIGN_CENTER)
        builder.write_text("*** Thank You For Your Trust ***\n")
        builder.new_line(3)
        builder.write(builder.FEED_CUT)

        return builder.get_bytes()

    @classmethod
    def generate_repair_claim_token_bytes(cls, ticket: RepairTicket, config: SystemConfiguration) -> bytes:
        """
        Builds 80mm thermal intake claim token with dynamic company branding and logo.
        """
        builder = RawEscPosBuilder()
        branch = ticket.branch
        width = cls.RECEIPT_WIDTH_CHARS

        company_title_en = branch.display_company_name if branch else config.company_name_en

        # Print logo if uploaded
        if branch and branch.logo:
            builder.print_logo_from_field(branch.logo, max_width=384, align='center')

        builder.write(builder.ALIGN_CENTER)
        builder.write(builder.BOLD_ON)
        builder.write_text(f"{company_title_en.upper()}\n")
        builder.write(builder.BOLD_OFF)
        builder.write_text("SERVICE & REPAIR WORKSHOP\n")
        builder.write_text(f"Phone: {branch.phone_number}\n")
        builder.double_divider(width=width)

        if contains_unicode("CUSTOMER CLAIM TOKEN (अमानत पुर्जी)"):
            builder.print_unicode_text_as_image(
                text="CUSTOMER CLAIM TOKEN (अमानत पुर्जी)",
                font_size=22,
                max_width=576,
                align='center'
            )
        else:
            builder.write_text("CUSTOMER CLAIM TOKEN\n")

        builder.double_divider(width=width)

        builder.write(builder.ALIGN_LEFT)
        builder.write(builder.BOLD_ON)
        builder.write_text(f"TICKET NO   : {ticket.ticket_number}\n")
        builder.write(builder.BOLD_OFF)
        builder.write_text(f"Date / Time : {ticket.created_at.strftime('%Y-%m-%d %H:%M')}\n")
        builder.write_text(f"Customer    : {ticket.customer_name_manual[:34]}\n")
        builder.write_text(f"Phone       : {ticket.customer_phone_manual}\n")
        builder.divider('-', width=width)

        builder.write_text(f"Device Model: {ticket.product.name[:34]}\n")
        builder.write_text(f"IMEI/Serial : {ticket.imei_or_serial}\n")
        builder.write_text(f"Claim Type  : {ticket.get_claim_type_display()}\n")
        builder.write_text(f"Reported Bug: {ticket.reported_fault[:34]}\n")
        builder.write_text(f"Accessories : {ticket.intake_accessories_received[:34]}\n")

        if hasattr(ticket, 'intake_checklist'):
            chk = ticket.intake_checklist
            builder.divider('-', width=width)
            builder.write(builder.BOLD_ON)
            builder.write_text("INTAKE PHYSICAL AUDIT:\n")
            builder.write(builder.BOLD_OFF)
            builder.write_text(f"* Power State  : {chk.get_power_status_display()}\n")
            builder.write_text(f"* Body Grade   : {chk.get_body_physical_grade_display()}\n")
            builder.write_text(f"* Liquid (LDI) : {chk.get_ldi_indicator_status_display()}\n")
            if chk.is_front_glass_cracked:
                builder.write_text("* NOTE: Front screen glass has pre-existing cracks.\n")

        builder.divider('-', width=width)

        builder.write(builder.ALIGN_CENTER)
        builder.write_text("Scan to Check Live Repair Status:\n")
        tracking_url = f"https://mobileshop.np/repairs/track/{ticket.ticket_number}/"
        builder.print_qr_code(tracking_url, module_size=5)
        builder.new_line(1)

        builder.write(builder.ALIGN_LEFT)
        builder.write_text(
            "TERMS & CONDITIONS:\n"
            "1. Present this token for device collection.\n"
            "2. Back up data. Shop is not liable for data loss.\n"
            "3. Final warranty eligibility subject to lab inspection.\n"
            "4. Unclaimed devices after 45 days may be recycled.\n"
        )
        builder.new_line(2)
        builder.write(builder.ALIGN_CENTER)
        builder.write_text("--------------------------------\n")
        builder.write_text("Customer Signature\n")
        builder.new_line(3)
        builder.write(builder.FEED_CUT)

        return builder.get_bytes()

    @classmethod
    def generate_repair_delivery_receipt_bytes(cls, ticket: RepairTicket, config: SystemConfiguration) -> bytes:
        """
        Builds 80mm thermal delivery handover receipt printed when a repaired handset is collected.
        """
        builder = RawEscPosBuilder()
        branch = ticket.branch
        width = cls.RECEIPT_WIDTH_CHARS

        company_title_en = branch.display_company_name if branch else config.company_name_en

        # Print logo if uploaded
        if branch and branch.logo:
            builder.print_logo_from_field(branch.logo, max_width=384, align='center')

        tech_label = (
            ticket.technician.get_full_name() or ticket.technician.username
            if ticket.technician else "Lab Technician"
        )

        builder.write(builder.ALIGN_CENTER)
        builder.write(builder.DOUBLE_HEIGHT_ON)
        builder.write_text(f"{company_title_en.upper()}\n")
        builder.write(builder.NORMAL_TEXT)
        builder.write_text(f"{branch.address}, {branch.city}\n")
        builder.double_divider(width=width)

        builder.write(builder.BOLD_ON)
        builder.write_text("REPAIR COMPLETION & WARRANTY SLIP\n")
        builder.write(builder.BOLD_OFF)
        builder.double_divider(width=width)

        builder.write(builder.ALIGN_LEFT)
        builder.write_text(f"Ticket No   : {ticket.ticket_number}\n")
        builder.write_text(f"Customer    : {ticket.customer_name_manual[:24]} ({ticket.customer_phone_manual})\n")
        builder.write_text(f"Device      : {ticket.product.name[:28]} [{ticket.imei_or_serial}]\n")
        builder.write_text(f"Technician  : {tech_label[:34]}\n")
        builder.divider('-', width=width)

        builder.write(builder.BOLD_ON)
        builder.write_text(f"{'Replaced Part / Labor':<24}{'Warranty':>10}{'Amount':>14}\n")
        builder.write(builder.BOLD_OFF)
        builder.divider('-', width=width)

        for part in ticket.replaced_parts.select_related('spare_part_product').all():
            part_name = part.spare_part_product.name[:22]
            warranty_str = f"{part.replacement_warranty_months}M"
            charge_str = f"Rs. {part.customer_charge:.2f}"
            builder.write_text(f"{part_name:<24}{warranty_str:>10}{charge_str:>14}\n")
            if part.new_part_serial_or_batch:
                builder.write_text(f"  [New Part S/N: {part.new_part_serial_or_batch.strip()}]\n")

        if ticket.labor_charge > Decimal('0.00'):
            builder.write_text(f"{'Service / Labor Fee':<24}{'-':>10}{f'Rs. {ticket.labor_charge:.2f}':>14}\n")

        builder.divider('-', width=width)

        builder.write(builder.ALIGN_RIGHT)
        builder.write_text(f"Total Service Cost: Rs. {ticket.final_total_amount:>10.2f}\n")
        builder.write_text(f"Amount Paid       : Rs. {ticket.paid_amount:>10.2f}\n")
        if ticket.claim_type == 'FREE_WARRANTY':
            builder.write(builder.BOLD_ON)
            builder.write_text("CLAIM STATUS      : FREE IN-WARRANTY (Rs. 0.00)\n")
            builder.write(builder.BOLD_OFF)

        builder.divider('-', width=width)
        builder.write(builder.ALIGN_CENTER)
        builder.write_text("All replaced parts carry testing warranty.\n")
        builder.write_text("Physical / liquid damage voids replacement warranty.\n")
        builder.write_text("*** Thank You For Your Trust ***\n")
        builder.new_line(3)
        builder.write(builder.FEED_CUT)

        return builder.get_bytes()