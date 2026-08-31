from django.conf import settings
from apps.repairs.models import RepairTicket
from apps.integrations.sms.sparrow import SparrowSMSClient
from apps.integrations.sms.aakash import AakashSMSClient


class RepairNotificationService:
    """
    Automated notification dispatcher triggering Aakash & Sparrow SMS
    alerts for intake tokens, quotation approvals, ready for pickup, and handover.
    Includes robust fallback handling for walk-in tickets without assigned phone numbers.
    """

    @classmethod
    def _get_recipient_phone(cls, ticket: RepairTicket) -> str:
        """Resolves target recipient phone number from manual field or linked customer record."""
        phone = (ticket.customer_phone_manual or "").strip()
        if not phone and ticket.customer:
            phone = (ticket.customer.phone_number or "").strip()
        return phone

    @staticmethod
    def _send(phone: str, message: str) -> bool:
        if not phone:
            return False

        # Attempt Aakash SMS first
        if getattr(settings, 'AAKASH_SMS_AUTH_TOKEN', None):
            client = AakashSMSClient(auth_token=settings.AAKASH_SMS_AUTH_TOKEN)
            if client.send_sms(phone, message):
                return True

        # Fallback to Sparrow SMS
        if getattr(settings, 'SPARROW_SMS_TOKEN', None):
            client = SparrowSMSClient(token=settings.SPARROW_SMS_TOKEN)
            return client.send_sms(phone, message)

        return False

    @classmethod
    def send_intake_confirmation(cls, ticket: RepairTicket) -> bool:
        phone = cls._get_recipient_phone(ticket)
        if not phone:
            return False
        msg = (
            f"Dear {ticket.customer_name_manual}, your {ticket.product.name} has been received for service. "
            f"Ticket No: {ticket.ticket_number}. Track live status at: https://mobileshop.np/repairs/track/{ticket.ticket_number}/"
        )
        return cls._send(phone, msg)

    @classmethod
    def send_quotation_approval_request(cls, ticket: RepairTicket, quote_token: str) -> bool:
        phone = cls._get_recipient_phone(ticket)
        if not phone:
            return False
        msg = (
            f"Dear {ticket.customer_name_manual}, lab diagnosis completed for {ticket.ticket_number}. "
            f"Total estimated repair cost: Rs. {ticket.final_total_amount}. "
            f"Please review photos & approve at: https://mobileshop.np/repairs/quote/{quote_token}/"
        )
        return cls._send(phone, msg)

    @classmethod
    def send_ready_for_pickup(cls, ticket: RepairTicket) -> bool:
        phone = cls._get_recipient_phone(ticket)
        if not phone:
            return False
        msg = (
            f"Good news! Your device {ticket.product.name} is repaired and passed QC testing. "
            f"Please collect it from {ticket.branch.name}. Total Payable: Rs. {ticket.final_total_amount}."
        )
        return cls._send(phone, msg)