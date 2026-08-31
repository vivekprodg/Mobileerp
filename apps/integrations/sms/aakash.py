import requests
from typing import Dict, Any, Optional

class AakashSMSClient:
    """
    Aakash SMS Gateway Client (Nepal).
    """
    API_URL = "https://v3.aakashsms.com/api/v3/sms/send"

    def __init__(self, auth_token: str = "mock_aakash_auth_token"):
        self.auth_token = auth_token

    def send_sms(self, to_phone: str, message: str) -> bool:
        # Standardize mobile format: 98XXXXXXXX
        clean_phone = to_phone.strip().replace("+977", "").replace("-", "")
        payload = {
            'auth_token': self.auth_token,
            'to': clean_phone,
            'text': message
        }
        try:
            resp = requests.post(self.API_URL, data=payload, timeout=8)
            if resp.status_code == 200:
                data = resp.json()
                return not data.get('error', True)
        except Exception:
            pass
        return False