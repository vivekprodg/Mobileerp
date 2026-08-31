import requests
from typing import Dict, Any, Optional

class SparrowSMSClient:
    """
    Sparrow SMS Gateway Client (Nepal).
    """
    API_URL = "https://api.sparrowsms.com/v2/sms/"

    def __init__(self, token: str = "mock_sparrow_token", identity: str = "MobileShop"):
        self.token = token
        self.identity = identity

    def send_sms(self, to_phone: str, message: str) -> bool:
        clean_phone = to_phone.strip().replace("+977", "").replace("-", "")
        params = {
            'token': self.token,
            'from': self.identity,
            'to': clean_phone,
            'text': message
        }
        try:
            resp = requests.get(self.API_URL, params=params, timeout=8)
            return resp.status_code == 200
        except Exception:
            pass
        return False