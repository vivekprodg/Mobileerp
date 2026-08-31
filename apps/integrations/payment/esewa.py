import hashlib
import hmac
import base64
import requests
from decimal import Decimal
from typing import Dict, Any, Optional
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class EsewaPaymentClient:
    """
    eSewa ePay Nepal Integration (v2 API with HMAC-SHA256 signature).
    Resolves credentials strictly from Django settings and validates production configuration.
    """
    EPAY_URL = getattr(settings, 'ESEWA_EPAY_URL', "https://epay.esewa.com.np/api/epay/main/v2/form")
    VERIFY_URL = getattr(settings, 'ESEWA_VERIFY_URL', "https://epay.esewa.com.np/api/epay/transaction/status/")

    def __init__(self, merchant_code: Optional[str] = None, secret_key: Optional[str] = None):
        self.merchant_code = (merchant_code or getattr(settings, 'ESEWA_MERCHANT_CODE', '') or '').strip()
        self.secret_key = (secret_key or getattr(settings, 'ESEWA_SECRET_KEY', '') or '').strip()

        # Enforce valid configuration in production mode
        if not settings.DEBUG:
            if not self.merchant_code or not self.secret_key:
                raise ImproperlyConfigured(
                    "Production Error: eSewa credentials (ESEWA_MERCHANT_CODE, ESEWA_SECRET_KEY) "
                    "are missing or improperly configured in settings."
                )

    def generate_signature(self, total_amount: str, transaction_uuid: str, product_code: str) -> str:
        """Constructs and signs message: total_amount,transaction_uuid,product_code using HMAC-SHA256."""
        if not self.secret_key:
            raise ImproperlyConfigured("Cannot generate eSewa payment signature: ESEWA_SECRET_KEY is not configured.")

        raw_data = f"total_amount={total_amount},transaction_uuid={transaction_uuid},product_code={product_code}"
        digest = hmac.new(
            self.secret_key.encode('utf-8'),
            raw_data.encode('utf-8'),
            hashlib.sha256
        ).digest()
        return base64.b64encode(digest).decode('utf-8')

    def generate_payment_payload(
        self,
        amount: Decimal,
        transaction_uuid: str,
        success_url: str,
        failure_url: str
    ) -> Dict[str, Any]:
        """Builds POST form parameters for eSewa v2 redirect."""
        if not self.merchant_code:
            raise ImproperlyConfigured("Cannot generate eSewa payload: ESEWA_MERCHANT_CODE is not configured.")

        amt_str = f"{amount:.2f}"
        signature = self.generate_signature(amt_str, transaction_uuid, self.merchant_code)

        return {
            'action_url': self.EPAY_URL,
            'amount': amt_str,
            'tax_amount': '0',
            'total_amount': amt_str,
            'transaction_uuid': transaction_uuid,
            'product_code': self.merchant_code,
            'product_service_charge': '0',
            'product_delivery_charge': '0',
            'success_url': success_url,
            'failure_url': failure_url,
            'signed_field_names': 'total_amount,transaction_uuid,product_code',
            'signature': signature,
        }

    def verify_transaction(self, total_amount: Decimal, transaction_uuid: str) -> bool:
        """Queries eSewa status endpoint to verify payment authenticity."""
        if not self.merchant_code:
            return False

        try:
            params = {
                'product_code': self.merchant_code,
                'total_amount': f"{total_amount:.2f}",
                'transaction_uuid': transaction_uuid,
            }
            resp = requests.get(self.VERIFY_URL, params=params, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                return data.get('status') == 'COMPLETE'
        except Exception:
            pass
        return False