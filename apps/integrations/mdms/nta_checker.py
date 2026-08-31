"""
Nepal Telecommunications Authority (NTA) MDMS Gateway Client.
Module: apps.integrations.mdms.nta_checker

Features:
- 15-digit IMEI Luhn algorithm checksum validation and sanitization.
- Fast local memory caching (24-hour TTL / 86,400 seconds) preventing POS lag during peak hours.
- 2.5-second strict HTTP connection & read timeout safeguard preventing cash register lockups.
- Graceful fallback to 'PENDING_VERIFICATION' when government gateways are unreachable or rate-limited.
- Standardized HTML badge generators and semantic metadata for POS, Product catalog, and Trade-In wizards.
"""

import re
import requests
from typing import Dict, Any, Optional
from django.conf import settings
from django.core.cache import cache
from django.utils.html import mark_safe


class NTAMDMSClient:
    """
    Client for querying and validating handset IMEI registration with the
    Nepal Telecommunications Authority (NTA) Mobile Device Management System (MDMS).
    """

    LOOKUP_URL = getattr(
        settings,
        'NTA_MDMS_LOOKUP_URL',
        'https://mdms.nta.gov.np/api/v1/device/verify'
    )
    # 24 Hours Cache TTL (86,400 seconds)
    CACHE_TIMEOUT = getattr(settings, 'NTA_MDMS_CACHE_TIMEOUT', 86400)
    # 2.5 Seconds Strict Timeout Safeguard
    HTTP_TIMEOUT = getattr(settings, 'NTA_MDMS_HTTP_TIMEOUT', 2.5)

    @classmethod
    def luhn_checksum_valid(cls, imei_str: str) -> bool:
        """
        Validates 15-digit IMEI using the standard Luhn algorithm (Mod 10).
        Returns True if the check digit matches, False otherwise.
        """
        digits = [int(d) for d in str(imei_str) if d.isdigit()]
        if len(digits) != 15:
            return False

        total = 0
        for i, digit in enumerate(digits[:-1]):
            if i % 2 == 1:  # Double every second digit from the left (0-indexed odd positions)
                doubled = digit * 2
                total += (doubled // 10) + (doubled % 10)
            else:
                total += digit

        computed_check_digit = (10 - (total % 10)) % 10
        return computed_check_digit == digits[-1]

    @classmethod
    def validate_imei_format(cls, imei_str: str) -> bool:
        """
        Verifies whether an input string is a valid numeric IMEI (14, 15, or 16 digits).
        """
        if not imei_str:
            return False
        clean = re.sub(r'\D', '', str(imei_str).strip())
        return len(clean) in [14, 15, 16]

    @classmethod
    def verify_imei(cls, imei_number: str) -> Dict[str, Any]:
        """
        Verifies IMEI status against official NTA MDMS registry with high-speed 24h caching.

        Workflow:
        1. Validates and sanitizes raw input characters.
        2. Checks local cache (`nta_mdms_verified_<imei>`) for instant sub-millisecond response.
        3. Queries NTA MDMS endpoint with a strict 2.5-second timeout safeguard.
        4. Caches successful verifications for 24 hours (86,400s).
        5. If government gateway is offline, slow (>2.5s), or throws an exception,
           gracefully falls back to 'PENDING_VERIFICATION' without freezing POS checkout.
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

        # Step 1: Check fast in-memory cache
        cache_key = f"nta_mdms_verified_{clean_imei}"
        cached_result = cache.get(cache_key)
        if cached_result and isinstance(cached_result, dict):
            return cached_result

        # Step 2: Query live NTA Gateway with 2.5s timeout safeguard
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
                # Parse registration flags across various NTA response schemas
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

                # Step 3: Cache verified record for 24 hours (86,400s)
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

        except requests.exceptions.Timeout:
            # Step 4A: Gateway connection timed out (> 2.5 seconds)
            pass
        except requests.exceptions.RequestException:
            # Step 4B: Network unreachable, DNS resolution error, or gateway maintenance
            pass
        except Exception:
            # Step 4C: Defensive catch to guarantee POS register never crashes
            pass

        # Step 5: Graceful offline fallback
        return {
            'status': 'PENDING_VERIFICATION',
            'is_registered': False,
            'message': 'NTA MDMS Status Pending (Gateway unreachable — verify before sale)',
            'raw_status': 'PENDING_OFFLINE',
            'imei': clean_imei,
            'from_cache': False
        }

    @classmethod
    def get_badge_meta(cls, status_code: str) -> Dict[str, str]:
        """
        Returns semantic UI dictionary metadata (CSS class, FontAwesome icon, and clean text label)
        for standard MDMS status codes.
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
        """
        Formats and returns a safe HTML badge snippet for templates and API JSON payloads.
        """
        meta = cls.get_badge_meta(status_code)
        html_str = f'<span class="{meta["css_class"]}"><i class="{meta["icon"]}"></i> {meta["label"]}</span>'
        return mark_safe(html_str)