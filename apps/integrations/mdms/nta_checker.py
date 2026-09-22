"""
Nepal Telecommunications Authority (NTA) MDMS Gateway Client.

Features:
- Local database check first (queries ItemInstance to instantly resolve known handsets).
- Memory-cached lookups (24-hour TTL) preventing repeated external network hits.
- Non-blocking asynchronous background resolution worker preventing Gunicorn WSGI thread freezes.
- Strict 1.5-second connection & read timeout fallback.
- Standardized HTML badge generators and semantic metadata.
"""

import re
import threading
import requests
from typing import Dict, Any, Optional
from django.conf import settings
from django.core.cache import cache
from django.utils.html import mark_safe
from django.db.models import Q

class NTAMDMSClient:
    """
    Client for querying and validating handset IMEI registration with the
    Nepal Telecommunications Authority (NTA) Mobile Device Management System (MDMS).
    Designed to prevent worker thread lockups by checking local inventory first
    and running external HTTP lookups asynchronously.
    """

    LOOKUP_URL = getattr(
        settings,
        'NTA_MDMS_LOOKUP_URL',
        'https://mdms.nta.gov.np/api/v1/device/verify'
    )
    # 24 Hours Cache TTL (86,400 seconds)
    CACHE_TIMEOUT = getattr(settings, 'NTA_MDMS_CACHE_TIMEOUT', 86400)
    # 1.5 Seconds Strict Timeout Safeguard
    HTTP_TIMEOUT = getattr(settings, 'NTA_MDMS_HTTP_TIMEOUT', 1.5)

    @classmethod
    def luhn_checksum_valid(cls, imei_str: str) -> bool:
        """
        Validates 15-digit IMEI using the standard Luhn algorithm (Mod 10).
        """
        digits = [int(d) for d in str(imei_str) if d.isdigit()]
        if len(digits) != 15:
            return False

        total = 0
        for i, digit in enumerate(digits[:-1]):
            if i % 2 == 1:
                doubled = digit * 2
                total += (doubled // 10) + (doubled % 10)
            else:
                total += digit

        computed_check_digit = (10 - (total % 10)) % 10
        return computed_check_digit == digits[-1]

    @classmethod
    def validate_imei_format(cls, imei_str: str) -> bool:
        """Verifies whether an input string is a valid numeric IMEI (14, 15, or 16 digits)."""
        if not imei_str:
            return False
        clean = re.sub(r'\D', '', str(imei_str).strip())
        return len(clean) in [14, 15, 16]

    @classmethod
    def _execute_remote_lookup(cls, clean_imei: str, cache_key: str):
        """
        Worker executed synchronously or in a background thread to query the government portal
        and update the local cache upon completion.
        """
        try:
            headers = {
                'User-Agent': 'NepalMobileShopERP/2.5.0 (Enterprise POS Terminal)',
                'Accept': 'application/json',
                'Connection': 'close'
            }
            params = {'imei': clean_imei}

            response = requests.get(
                cls.LOOKUP_URL,
                params=params,
                headers=headers,
                timeout=cls.HTTP_TIMEOUT
            )

            if response.status_code == 200:
                data = response.json()
                is_reg = bool(
                    data.get('registered') is True or
                    str(data.get('status', '')).upper() in ['REGISTERED', 'WHITELISTED', 'ACTIVE', 'PAID'] or
                    data.get('is_registered') is True
                )

                status_code = 'REGISTERED_OFFICIAL' if is_reg else 'GRAY_UNREGISTERED'
                message_text = (
                    'NTA MDMS Registered (Official Nepal Import)'
                    if is_reg
                    else 'Unregistered / Gray Market Device (Subject to NTA Notice)'
                )

                result = {
                    'status': status_code,
                    'is_registered': is_reg,
                    'message': message_text,
                    'model': data.get('model', '') or data.get('model_name', ''),
                    'brand': data.get('brand', '') or data.get('brand_name', ''),
                    'raw_status': str(data.get('status', 'UNREGISTERED')).upper(),
                    'imei': clean_imei,
                    'from_cache': False
                }
                cache.set(cache_key, result, timeout=cls.CACHE_TIMEOUT)
                return result

            elif response.status_code == 404:
                result = {
                    'status': 'GRAY_UNREGISTERED',
                    'is_registered': False,
                    'message': 'Unregistered / Gray Market Device (Not Found in NTA MDMS)',
                    'raw_status': 'NOT_FOUND',
                    'imei': clean_imei,
                    'from_cache': False
                }
                cache.set(cache_key, result, timeout=cls.CACHE_TIMEOUT)
                return result

        except Exception:
            pass

        return None

    @classmethod
    def verify_imei(cls, imei_number: str, allow_sync_network: bool = False) -> Dict[str, Any]:
        """
        Verifies IMEI status against:
        1. Fast in-memory cache (sub-millisecond).
        2. Local Inventory database (`ItemInstance`) to verify pre-cataloged handsets instantly.
        3. External NTA Gateway (executed in background thread to protect live POS checkout).
        """
        raw_clean = str(imei_number or '').strip()
        clean_imei = re.sub(r'\D', '', raw_clean)

        if not cls.validate_imei_format(clean_imei):
            return {
                'status': 'INVALID_IMEI',
                'is_registered': False,
                'message': 'Invalid IMEI format. Expected 15 numeric digits.',
                'raw_status': 'INVALID',
                'imei': clean_imei
            }

        # ---------------------------------------------------------------------
        # STEP 1: Fast Cache Check
        # ---------------------------------------------------------------------
        cache_key = f"nta_mdms_verified_{clean_imei}"
        cached_result = cache.get(cache_key)
        if cached_result and isinstance(cached_result, dict):
            return cached_result

        # ---------------------------------------------------------------------
        # STEP 2: Check Local Inventory Database First (Zero Network Lag)
        # ---------------------------------------------------------------------
        try:
            from apps.inventory.models import ItemInstance
            local_instance = ItemInstance.objects.filter(
                Q(imei_1=clean_imei) | Q(imei_2=clean_imei)
            ).select_related('product', 'product__brand').first()

            if local_instance and local_instance.mdms_status in ['REGISTERED_OFFICIAL', 'GRAY_UNREGISTERED', 'INDIVIDUAL_CUSTOMS_PAID']:
                is_reg = (local_instance.mdms_status in ['REGISTERED_OFFICIAL', 'INDIVIDUAL_CUSTOMS_PAID'])
                result = {
                    'status': local_instance.mdms_status,
                    'is_registered': is_reg,
                    'message': f"Locally Verified in Inventory ({local_instance.get_mdms_status_display()})",
                    'model': getattr(local_instance.product, 'model_name', '') or local_instance.product.name,
                    'brand': local_instance.product.brand.name if getattr(local_instance.product, 'brand', None) else '',
                    'raw_status': local_instance.mdms_status,
                    'imei': clean_imei,
                    'from_cache': True,
                    'from_local_db': True
                }
                cache.set(cache_key, result, timeout=cls.CACHE_TIMEOUT)
                return result
        except Exception:
            pass

        # ---------------------------------------------------------------------
        # STEP 3: Handle External Network Verification
        # ---------------------------------------------------------------------
        if allow_sync_network:
            # Explicit synchronous request (e.g. user manually clicked "Verify MDMS" button in Trade-In)
            res = cls._execute_remote_lookup(clean_imei, cache_key)
            if res:
                return res
        else:
            # Non-blocking POS checkout: launch worker thread in background
            bg_worker = threading.Thread(
                target=cls._execute_remote_lookup,
                args=(clean_imei, cache_key),
                daemon=True
            )
            bg_worker.start()

        # Fallback returned instantly to protect Gunicorn threads from stalling
        return {
            'status': 'PENDING_VERIFICATION',
            'is_registered': False,
            'message': 'NTA MDMS Status Pending (Verification queued in background)',
            'raw_status': 'PENDING_BACKGROUND',
            'imei': clean_imei,
            'from_cache': False
        }

    @classmethod
    def get_badge_meta(cls, status_code: str) -> Dict[str, str]:
        """
        Returns semantic UI dictionary metadata for standard MDMS status codes.
        """
        mapping = {
            'REGISTERED_OFFICIAL': {
                'css_class': 'badge bg-success bg-opacity-10 text-success fw-bold fs-xs border border-success border-opacity-25',
                'icon': 'fas fa-check-circle me-1',
                'label': 'NTA MDMS REGISTERED'
            },
            'GRAY_UNREGISTERED': {
                'css_class': 'badge bg-danger bg-opacity-10 text-danger fw-bold fs-xs border border-danger border-opacity-25',
                'icon': 'fas fa-triangle-exclamation me-1',
                'label': 'GRAY / UNREGISTERED'
            },
            'INDIVIDUAL_CUSTOMS_PAID': {
                'css_class': 'badge bg-info bg-opacity-10 text-info fw-bold fs-xs border border-info border-opacity-25',
                'icon': 'fas fa-passport me-1',
                'label': 'CUSTOMS DUTY PAID'
            },
            'PENDING_VERIFICATION': {
                'css_class': 'badge bg-warning bg-opacity-10 text-warning-emphasis fw-bold fs-xs border border-warning border-opacity-25',
                'icon': 'fas fa-clock me-1',
                'label': 'MDMS PENDING CHECK'
            },
            'EXEMPT': {
                'css_class': 'badge bg-secondary bg-opacity-10 text-secondary fs-xs border',
                'icon': 'fas fa-shield-alt me-1',
                'label': 'EXEMPT / NON-CELLULAR'
            }
        }
        return mapping.get(status_code, {
            'css_class': 'badge bg-secondary bg-opacity-10 text-secondary fs-xs border',
            'icon': 'fas fa-circle-question me-1',
            'label': str(status_code).replace('_', ' ').upper()
        })

    @classmethod
    def format_mdms_badge(cls, status_code: str) -> str:
        """Formats and returns a safe HTML badge snippet."""
        meta = cls.get_badge_meta(status_code)
        html_str = f'<span class="{meta["css_class"]}"><i class="{meta["icon"]}"></i> {meta["label"]}</span>'
        return mark_safe(html_str)