"""
Government Tax Sync Shield & Non-IRD Estimation Compliance Engine.

IMPORTANT NOTICE:
This ERP installation is strictly operating in INTERNAL PROFORMA / ESTIMATION MODE.
The software has NOT been submitted for IRD (Inland Revenue Department) certification,
and all external live tax department sync triggers (CBMS API / ird.gov.np) are
permanently disabled and shielded.

Core Shield Functions:
1. Complete Network Transmission Block:
   - Permanently disables outgoing HTTP/REST requests to external government CBMS servers.
   - Completely shields the application during bulk imports of historical invoices (e.g. 2080 B.S.)
     and daily point-of-sale checkout sessions.
2. Mandatory Non-IRD Proforma Disclaimer Formatting:
   - Injects legal and operational disclaimers into thermal 80mm/58mm slips, formal A4 invoices,
     and monthly VAT register exports.
3. Mock & Safe Simulation Handlers:
   - Provides safe mock responses for any service or background worker attempting to trigger
     tax synchronization, returning zero-network, audit-logged dummy confirmation.
4. Thread-Safe Migration Guard:
   - Includes the `suppress_ird_sync()` context manager for bulk migrations and batch scripts.
"""

import logging
import threading
from contextlib import contextmanager
from decimal import Decimal
from typing import Dict, Any, Optional, Union

from django.conf import settings
from django.utils import timezone
from apps.core.models import SystemConfiguration, AuditLog

logger = logging.getLogger(__name__)

# Master System Lockdown: Explicitly prevents any network connection to external IRD servers
IS_IRD_API_ENABLED = False

# Thread-local storage for managing runtime sync suppression during bulk data imports
_thread_locals = threading.local()


# =============================================================================
# 1. THREAD-SAFE SYNC SUPPRESSION GUARDS
# =============================================================================

def is_ird_sync_suppressed() -> bool:
    """Returns True if tax sync is globally disabled or suppressed on the current thread."""
    if not IS_IRD_API_ENABLED:
        return True
    return getattr(_thread_locals, 'suppress_ird_sync', False)


def mute_ird_sync():
    """Explicitly mutes tax sync on the current thread."""
    _thread_locals.suppress_ird_sync = True


def unmute_ird_sync():
    """Unmutes tax sync on the current thread."""
    _thread_locals.suppress_ird_sync = False


@contextmanager
def suppress_ird_sync():
    """
    Context manager to safely guarantee that no external tax sync triggers during
    large-scale historical data migrations (e.g., Mobilesoft 2080 B.S. imports).
    
    Usage:
        with suppress_ird_sync():
            # Run migration here safely with 100% network isolation
            call_command('migrate_mobilesoft_data', ...)
    """
    mute_ird_sync()
    try:
        yield
    finally:
        unmute_ird_sync()


# =============================================================================
# 2. NON-IRD DISCLAIMER & BANNER FORMATTER
# =============================================================================

class NonIRDDisclaimerEngine:
    """
    Enforces clear, legally compliant disclaimers across all printed slips,
    thermal receipts, and web views, clarifying that documents are internal
    estimations and not approved IRD tax invoices.
    """

    @staticmethod
    def get_mandatory_banner() -> str:
        """
        Returns the standardized header notice configured in SystemConfiguration.
        """
        try:
            config = SystemConfiguration.get_solo()
            header_title = config.bill_header_title or "SALES ESTIMATE SLIP"
            disclaimer = config.bill_estimate_disclaimer or (
                "NOTICE: This is an internal quotation / estimation slip only and NOT an official Tax Invoice approved by IRD Nepal."
            )
            return (
                f"*** {header_title.upper()} ***\n"
                f"{disclaimer}\n"
                f"Issued strictly for store internal accounting, warranty tracking & valuation."
            )
        except Exception:
            return (
                "*** SALES ESTIMATE SLIP ***\n"
                "NOTICE: Internal estimation slip only. NOT an official Tax Invoice approved by IRD Nepal.\n"
                "Issued strictly for store internal accounting, warranty tracking & valuation."
            )

    @staticmethod
    def get_thermal_disclaimer() -> str:
        """
        Returns a concise 2-line disclaimer optimized for 80mm and 58mm thermal printers.
        """
        return (
            "* ESTIMATION SLIP ONLY - NOT A VAT/TAX INVOICE *\n"
            "* Exchange possible within 7 days with this slip *"
        )

    @staticmethod
    def get_proforma_header() -> Dict[str, str]:
        """
        Returns structured dictionary headers for web templates and PDF generators.
        """
        try:
            config = SystemConfiguration.get_solo()
            return {
                'title': config.bill_header_title or "SALES ESTIMATE SLIP",
                'subtitle': "Internal Store Proforma & Estimation Document (गैर-कर बिजक)",
                'disclaimer': config.bill_estimate_disclaimer,
                'is_approved_tax_invoice': False,
                'compliance_mode': "PROFORMA_NON_IRD"
            }
        except Exception:
            return {
                'title': "SALES ESTIMATE SLIP",
                'subtitle': "Internal Store Proforma & Estimation Document (गैर-कर बिजक)",
                'disclaimer': "Internal estimation slip only. Not an official Tax Invoice.",
                'is_approved_tax_invoice': False,
                'compliance_mode': "PROFORMA_NON_IRD"
            }

    @staticmethod
    def inject_disclaimer(context_dict: dict) -> dict:
        """
        Injects standard non-tax compliance flags into Django view context dictionaries.
        """
        if context_dict is None:
            context_dict = {}

        context_dict['NON_IRD_NOTICE'] = NonIRDDisclaimerEngine.get_mandatory_banner()
        context_dict['THERMAL_DISCLAIMER'] = NonIRDDisclaimerEngine.get_thermal_disclaimer()
        context_dict['IS_PROFORMA'] = True
        context_dict['IS_IRD_APPROVED'] = False
        context_dict['TAX_SYSTEM_SHIELD_ACTIVE'] = True
        return context_dict


# =============================================================================
# 3. REAL-TIME GOVERNMENT TAX SYNC SHIELD
# =============================================================================

class IRDSyncShield:
    """
    Active defense shield for the ERP system:
    - Bypasses any external network call to CBMS / IRD servers.
    - Generates isolated mock receipts with zero network overhead.
    - Guarantees that historical migration records (2080 B.S. & onwards) are stored
      locally without triggering external compliance alarms.
    """

    @classmethod
    def is_sync_active(cls) -> bool:
        """
        Always returns False to signify that live external API synchronization is disabled.
        """
        return False

    @classmethod
    def sync_sales_estimate(
        cls,
        estimate: Any,
        user: Any = None,
        force: bool = False
    ) -> Dict[str, Any]:
        """
        Intercepts sales invoice checkout sync requests.
        Completely shields the transaction and returns a safe, local simulation response.
        """
        estimate_number = getattr(estimate, 'estimate_number', str(estimate))
        grand_total = getattr(estimate, 'grand_total', Decimal('0.00'))

        logger.info(
            f"[IRD Sync Shield Active] Intercepted invoice {estimate_number} (Rs. {grand_total:,.2f}). "
            f"External IRD API transmission permanently blocked (Non-IRD System Mode)."
        )

        return {
            'status': 'SHIELDED',
            'is_transmitted': False,
            'ird_approved': False,
            'mode': 'NON_IRD_PROFORMA',
            'estimate_number': estimate_number,
            'transmission_timestamp': timezone.now().isoformat(),
            'message': (
                f"Invoice {estimate_number} recorded in local ERP. "
                f"External transmission skipped: system operating in Non-IRD Estimation mode."
            )
        }

    @classmethod
    def sync_purchase_grn(
        cls,
        grn: Any,
        user: Any = None,
        force: bool = False
    ) -> Dict[str, Any]:
        """
        Intercepts purchase GRN inward tax sync requests.
        Completely shields the procurement receipt and returns a safe local simulation response.
        """
        grn_number = getattr(grn, 'grn_number', str(grn))
        supplier_bill_no = getattr(grn, 'supplier_bill_no', '')

        logger.info(
            f"[IRD Sync Shield Active] Intercepted Purchase GRN {grn_number} (Bill: {supplier_bill_no}). "
            f"External API transmission blocked."
        )

        return {
            'status': 'SHIELDED',
            'is_transmitted': False,
            'ird_approved': False,
            'mode': 'NON_IRD_PROFORMA',
            'grn_number': grn_number,
            'transmission_timestamp': timezone.now().isoformat(),
            'message': (
                f"GRN {grn_number} recorded in local ERP inventory. "
                f"External IRD Annex 7 transmission skipped (Non-IRD System Mode)."
            )
        }

    @classmethod
    def sync_sales_return(
        cls,
        sales_return: Any,
        user: Any = None
    ) -> Dict[str, Any]:
        """
        Intercepts credit note / sales return sync requests with zero external network impact.
        """
        return_number = getattr(sales_return, 'return_number', str(sales_return))

        logger.info(
            f"[IRD Sync Shield Active] Intercepted Sales Return {return_number}. "
            f"External Credit Note transmission blocked."
        )

        return {
            'status': 'SHIELDED',
            'is_transmitted': False,
            'ird_approved': False,
            'mode': 'NON_IRD_PROFORMA',
            'return_number': return_number,
            'transmission_timestamp': timezone.now().isoformat(),
            'message': f"Sales return {return_number} recorded locally without external tax transmission."
        }

    @classmethod
    def test_government_portal_connection(cls) -> Dict[str, Any]:
        """
        Diagnostic connection check. Explicitly certifies that live government endpoints
        are not contacted and that the store's data privacy is 100% maintained.
        """
        return {
            'status': 'PROTECTED',
            'network_connected_to_ird': False,
            'cbms_active': False,
            'shield_status': 'ACTIVE_ENGAGED',
            'software_classification': 'INTERNAL_STORE_MANAGEMENT_ERP',
            'ird_compliance_notice': (
                "System is uncertified and operates exclusively for private inventory, "
                "internal estimation slips, and double-entry store accounting."
            )
        }


# =============================================================================
# 4. BACKWARD COMPATIBILITY & CONVENIENCE BRIDGES
# =============================================================================

# Alias IRDSyncShield as IRDSyncService for any legacy modules
IRDSyncService = IRDSyncShield

# Module-level convenience functions
sync_sales_estimate = IRDSyncShield.sync_sales_estimate
sync_purchase_grn = IRDSyncShield.sync_purchase_grn
sync_sales_return = IRDSyncShield.sync_sales_return
get_mandatory_banner = NonIRDDisclaimerEngine.get_mandatory_banner
inject_disclaimer = NonIRDDisclaimerEngine.inject_disclaimer