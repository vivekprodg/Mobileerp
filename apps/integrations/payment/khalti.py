import requests
from decimal import Decimal
from typing import Dict, Any, Optional
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class KhaltiPaymentClient:
    """
    Khalti Payment Gateway (ePayment v2 API - Nepal).
    Resolves API credentials strictly from Django settings and validates production configuration.
    """
    INITIATE_URL = getattr(settings, 'KHALTI_INITIATE_URL', "https://a.khalti.com/api/v2/epayment/initiate/")
    LOOKUP_URL = getattr(settings, 'KHALTI_LOOKUP_URL', "https://a.khalti.com/api/v2/epayment/lookup/")

    def __init__(self, secret_key: Optional[str] = None):
        self.secret_key = (secret_key or getattr(settings, 'KHALTI_SECRET_KEY', '') or '').strip()

        # Enforce valid configuration in production mode
        if not settings.DEBUG:
            if not self.secret_key:
                raise ImproperlyConfigured(
                    "Production Error: Khalti API Secret Key (KHALTI_SECRET_KEY) "
                    "is missing or improperly configured in settings."
                )

        self.headers = {
            "Authorization": f"Key {self.secret_key}" if self.secret_key else "",
            "Content-Type": "application/json"
        }

    def initiate_payment(
        self,
        return_url: str,
        purchase_order_id: str,
        purchase_order_name: str,
        amount_npr: Decimal,
        customer_info: Optional[Dict[str, str]] = None
    ) -> Optional[Dict[str, Any]]:
        """Initiates an online payment session with Khalti Gateway."""
        if not self.secret_key:
            raise ImproperlyConfigured("Cannot initiate Khalti transaction: KHALTI_SECRET_KEY is not configured.")

        # Khalti expects amounts in Paisa (Rs. 1 = 100 Paisa)
        amount_paisa = int(amount_npr * 100)
        payload = {
            "return_url": return_url,
            "website_url": getattr(settings, 'KHALTI_WEBSITE_URL', "https://mobileshop.np"),
            "amount": amount_paisa,
            "purchase_order_id": purchase_order_id,
            "purchase_order_name": purchase_order_name,
            "customer_info": customer_info or {
                "name": "Walk-in Customer",
                "email": "customer@shop.com",
                "phone": "9800000000"
            }
        }
        try:
            resp = requests.post(self.INITIATE_URL, json=payload, headers=self.headers, timeout=10)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return None

    def verify_payment(self, pidx: str) -> bool:
        """Verifies payment completion status by pidx."""
        if not self.secret_key or not pidx:
            return False

        try:
            resp = requests.post(self.LOOKUP_URL, json={"pidx": pidx}, headers=self.headers, timeout=10)
            if resp.status_code == 200:
                return resp.json().get('status') == 'Completed'
        except Exception:
            pass
        return False