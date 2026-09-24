import json
from django.contrib import admin
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _

from apps.core.models import SystemConfiguration, AuditLog

@admin.register(SystemConfiguration)
class SystemConfigurationAdmin(admin.ModelAdmin):
    """
    Admin controller for the SystemConfiguration master singleton.
    Manages organization identity, operating tax mode, receipt headers,
    trade-in rules, MDMS policies, and General Ledger Chart of Accounts bindings.
    """
    list_display = [
        'company_name_en',
        'tax_system_mode_badge',
        'pan_number',
        'vat_number',
        'default_vat_rate',
        'require_manager_approval_discount',
        'default_trade_in_margin_percent',
        'thermal_printer_paper_width',
        'is_estimation_bill_only',
        'updated_at',
    ]

    readonly_fields = ['id', 'uuid', 'created_at', 'updated_at']

    raw_id_fields = [
        'default_cash_account',
        'default_bank_account',
        'default_fonepay_account',
        'default_esewa_account',
        'default_khalti_account',
        'default_card_clearing_account',
        'default_receivable_account',
        'default_payable_account',
        'default_inventory_asset_account',
        'default_cogs_account',
        'default_sales_revenue_account',
        'default_discount_expense_account',
        'default_vat_output_account',
        'default_vat_input_account',
        'default_shrinkage_account',
        'default_gateway_fee_account',
        'default_interest_expense_account',
        'default_drawings_account',
    ]

    fieldsets = (
        (_("1. Shop Identity & Tax Operating Mode"), {
            'description': _(
                "Controls whether the POS operates in 13% VAT mode, 0% Non-VAT PAN mode, "
                "or simple internal proforma quotation mode."
            ),
            'fields': (
                'tax_system_mode',
                'default_vat_rate',
                ('company_name_en', 'company_name_np'),
                ('pan_number', 'vat_number'),
            )
        }),
        (_("2. Cashier Discount & Manager Override Limits"), {
            'description': _(
                "Set the maximum discount percentage a cashier can apply before the "
                "POS terminal blocks checkout and demands a Manager Override PIN."
            ),
            'fields': (
                'require_manager_approval_discount',
                'allow_negative_stock',
            )
        }),
        (_("3. Estimation Voucher & Proforma Header Customization"), {
            'description': _(
                "Customize bill titles and disclaimer warnings printed on non-tax receipts."
            ),
            'fields': (
                'is_estimation_bill_only',
                'bill_header_title',
                'bill_estimate_disclaimer',
            )
        }),
        (_("4. Second-Hand Trade-In & Police Legal Undertaking"), {
            'description': _(
                "Configure fair valuation safety buffers and the legal anti-theft declaration "
                "required for customer signature during handset buy-backs."
            ),
            'fields': (
                'default_trade_in_margin_percent',
                'undertaking_declaration_text_np',
            )
        }),
        (_("5. NTA MDMS Handset Compliance (Nepal)"), {
            'description': _(
                "Enforce MDMS IMEI registration verification and configure gray market sales alerts."
            ),
            'fields': (
                'enable_nta_mdms_tracking',
                'warn_on_gray_market_sale',
            )
        }),
        (_("6. Localization & POS Hardware Controls"), {
            'fields': (
                ('currency_symbol', 'currency_code'),
                ('enable_nepali_calendar', 'default_language'),
                'thermal_printer_paper_width',
            )
        }),
        (_("7. Chart of Accounts Control Ledger Bindings (Double-Entry Automation)"), {
            'description': _(
                "Designate control accounts for automated journal voucher generation across sales, "
                "digital QR wallets, inventory assets, COGS, and tax obligations."
            ),
            'fields': (
                ('default_cash_account', 'default_bank_account'),
                ('default_fonepay_account', 'default_esewa_account'),
                ('default_khalti_account', 'default_card_clearing_account'),
                ('default_receivable_account', 'default_payable_account'),
                ('default_inventory_asset_account', 'default_cogs_account'),
                ('default_sales_revenue_account', 'default_discount_expense_account'),
                ('default_vat_output_account', 'default_vat_input_account'),
                ('default_shrinkage_account', 'default_gateway_fee_account'),
                ('default_interest_expense_account', 'default_drawings_account'),
            )
        }),
        (_("8. System Metadata"), {
            'classes': ('collapse',),
            'fields': (
                ('uuid', 'is_active'),
                ('created_at', 'updated_at'),
            )
        }),
    )

    @admin.display(description=_("Tax Mode"))
    def tax_system_mode_badge(self, obj):
        colors = {
            'VAT': '#10b981',     # Emerald Green
            'PAN': '#2563eb',     # Blue
            'NO_TAX': '#64748b',  # Slate Gray
        }
        color = colors.get(obj.tax_system_mode, '#0f172a')
        return format_html(
            '<span style="color: #ffffff; background-color: {}; padding: 3px 10px; '
            'border-radius: 999px; font-weight: 700; font-size: 11px; text-transform: uppercase;">{}</span>',
            color,
            obj.get_tax_system_mode_display()
        )

    def has_add_permission(self, request):
        # Enforce Singleton pattern: allow creation only if table is currently empty
        return not SystemConfiguration.objects.exists()

    def has_delete_permission(self, request, obj=None):
        # Master system configuration cannot be deleted
        return False

@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    """
    Immutable forensic security audit log viewer.
    Records all sensitive actions including manager discount overrides, bill voiding,
    manual stock deductions, MDMS overrides, and failed login alerts.
    """
    list_display = [
        'timestamp',
        'action_badge',
        'module_badge',
        'object_repr',
        'user',
        'branch',
        'ip_address',
    ]
    list_filter = ['action_type', 'module', 'branch', 'timestamp']
    search_fields = ['object_repr', 'details', 'user__username', 'user__first_name', 'user__last_name', 'ip_address']
    date_hierarchy = 'timestamp'
    ordering = ['-timestamp']

    readonly_fields = [
        'id',
        'timestamp',
        'user',
        'branch',
        'action_type',
        'module',
        'object_repr',
        'ip_address',
        'formatted_details',
    ]

    fieldsets = (
        (_("Security Event Identification"), {
            'fields': (
                ('timestamp', 'action_type'),
                ('module', 'object_repr'),
                ('user', 'branch'),
                'ip_address',
            )
        }),
        (_("Forensic State Diff & Context (JSON)"), {
            'fields': ('formatted_details',)
        }),
    )

    @admin.display(description=_("Action"))
    def action_badge(self, obj):
        colors = {
            'CREATE': '#10b981',            # Green
            'UPDATE': '#3b82f6',            # Blue
            'DELETE': '#ef4444',            # Red
            'BILL_CANCEL': '#b91c1c',       # Deep Red
            'PRICE_OVERRIDE': '#f59e0b',    # Amber
            'STOCK_ADJUST': '#8b5cf6',      # Purple
            'MDMS_OVERRIDE': '#6366f1',     # Indigo
            'TRADE_IN_PURCHASE': '#0d9488', # Teal
            'LOGIN_FAIL': '#dc2626',        # Crimson
        }
        color = colors.get(obj.action_type, '#64748b')
        return format_html(
            '<span style="color: #ffffff; background-color: {}; padding: 2px 8px; '
            'border-radius: 999px; font-weight: 600; font-size: 10.5px;">{}</span>',
            color,
            obj.get_action_type_display()
        )

    @admin.display(description=_("Module"))
    def module_badge(self, obj):
        return format_html(
            '<span style="background-color: #f1f5f9; color: #334155; border: 1px solid #cbd5e1; '
            'padding: 2px 7px; border-radius: 4px; font-weight: 600; font-size: 11px;">{}</span>',
            obj.module
        )

    @admin.display(description=_("Details / Diff Payload"))
    def formatted_details(self, obj):
        if not obj.details:
            return mark_safe('<em>No supplementary metadata captured.</em>')
        pretty_json = json.dumps(obj.details, indent=2, ensure_ascii=False)
        return format_html(
            '<pre style="background: #0f172a; color: #38bdf8; padding: 12px; border-radius: 6px; '
            'font-family: Consolas, monospace; font-size: 12px; max-height: 400px; overflow: auto;">{}</pre>',
            pretty_json
        )

    def has_add_permission(self, request):
        # Audit logs are generated strictly by backend audit hooks
        return False

    def has_change_permission(self, request, obj=None):
        # Audit logs are immutable records
        return False

    def has_delete_permission(self, request, obj=None):
        # Prevent log tampering or deletion
        return False