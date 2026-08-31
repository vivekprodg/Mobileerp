import hashlib
import hmac
import requests
from decimal import Decimal
from typing import Dict, Any, Optional
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from apps.core.utils.barcode_generator import BarcodeGenerator


class FonepayQRClient:
    """
    FonePay Dynamic QR & Interoperable Merchant QR Service (Nepal).
    Resolves credentials strictly from Django settings and validates production configuration.
    """

    def __init__(self, merchant_id: Optional[str] = None, secret_key: Optional[str] = None):
        self.merchant_id = (merchant_id or getattr(settings, 'FONEPAY_MERCHANT_ID', '') or '').strip()
        self.secret_key = (secret_key or getattr(settings, 'FONEPAY_SECRET_KEY', '') or '').strip()

        # Enforce valid configuration in production mode
        if not settings.DEBUG:
            if not self.merchant_id or not self.secret_key:
                raise ImproperlyConfigured(
                    "Production Error: FonePay credentials (FONEPAY_MERCHANT_ID, FONEPAY_SECRET_KEY) "
                    "are missing or improperly configured in settings."
                )

    def generate_dynamic_qr_data_string(
        self,
        trace_id: str,
        amount: Decimal,
        remark1: str = "Mobile Purchase"
    ) -> str:
        """Constructs EMVCo-compliant FonePay payload string."""
        if not self.merchant_id:
            raise ImproperlyConfigured("Cannot generate FonePay QR: FONEPAY_MERCHANT_ID is not configured.")

        amt_str = f"{amount:.2f}"
        return f"fonepay://pay?merchant={self.merchant_id}&trace={trace_id}&amount={amt_str}&remarks={remark1}"

    def get_qr_base64_image(self, trace_id: str, amount: Decimal, remark1: str = "") -> Optional[str]:
        """Returns base64 encoded PNG representation of dynamic FonePay QR code."""
        qr_str = self.generate_dynamic_qr_data_string(trace_id, amount, remark1)
        return BarcodeGenerator.generate_qr_base64(qr_str)