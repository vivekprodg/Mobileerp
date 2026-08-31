"""
Non-IRD Mock Synchronizer & Compliance Disclaimer Formatter.
This system outputs disclaimer-wrapped estimation ledgers strictly for proforma use.
"""
from apps.core.models import SystemConfiguration

class NonIRDDisclaimerEngine:
    @staticmethod
    def get_mandatory_banner() -> str:
        config = SystemConfiguration.get_solo()
        return (
            f"*** {config.bill_header_title} ***\n"
            f"{config.bill_estimate_disclaimer}\n"
            f"Printed for shop internal accounting & valuation only."
        )

    @staticmethod
    def inject_disclaimer(context_dict: dict) -> dict:
        context_dict['NON_IRD_NOTICE'] = NonIRDDisclaimerEngine.get_mandatory_banner()
        context_dict['IS_PROFORMA'] = True
        return context_dict