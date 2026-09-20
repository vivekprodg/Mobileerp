from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _

class AccountingConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.accounting'
    verbose_name = _('Double-Entry General Ledger & Financial Accounting')

    def ready(self):
        """
        Connect signals to automate double-entry postings upon transaction lifecycle events
        (Sales finalization, GRN verification, Supplier payouts, Customer Udhaari, Returns, and Write-offs).
        """
        try:
            import apps.accounting.signals  # noqa: F401
        except ImportError:
            pass