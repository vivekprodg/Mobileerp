from django.contrib import admin
from django.utils.html import format_html
from apps.core.models import SystemConfiguration, AuditLog


@admin.register(SystemConfiguration)
class SystemConfigurationAdmin(admin.ModelAdmin):
    list_display = [
        'company_name_en', 'tax_system_mode_badge', 'pan_number',
        'vat_number', 'default_vat_rate', 'require_manager_approval_discount',
        'default_trade_in_margin_percent', 'thermal_printer_paper_width'
    ]
    raw_id_fields = [
        'default_cash_account', 'default_bank_account',
        'default_fonepay_account', 'default_esewa_account',
        'default_khalti_account', 'default_card_clearing_account',
        'default_receivable_account', 'default_payable_account',
        'default_inventory_asset_account', 'default_cogs_account',
        'default_sales_revenue_account', 'default_discount_expense_account',
        'default_vat_output_account', 'default_vat_input_account',
        'default_shrinkage_account', 'default_gateway_fee_account',
        'default_interest_expense_account', 'default_drawings_account',
    ]
    fieldsets = (
        ("1. Shop Identity & Tax Operating Mode", {
            'description': "Select whether POS bills operate in 13% VAT mode, 0% PAN mode, or simple estimation.",
            'fields': (
                'tax_system_mode', 'default_vat_rate',
                ('company_name_en', 'company_name_np'),
                ('pan_number', 'vat_number')
            )
        }),
        ("2. Cashier Discount & Manager Authorization Rules", {
            'description': "Configure discount ceilings where the POS terminal blocks checkout and demands a Manager PIN.",
            'fields': (
                'require_manager_approval_discount',
                'allow_negative_stock'
            )
        }),
        ("3. Estimation Voucher Disclaimers (Non-IRD)", {
            'description': "Customize header labels and internal quotation notices printed on customer receipts.",
            'fields': (
                'is_estimation_bill_only',
                'bill_header_title',
                'bill_estimate_disclaimer'
            )
        }),
        ("4. Second-Hand Trade-In & Police Legal Undertaking", {
            'description': "Configure fair market valuation profit margins and police anti-theft declaration text.",
            'fields': (
                'default_trade_in_margin_percent',
                'undertaking_declaration_text_np'
            )
        }),
        ("5. NTA MDMS Compliance & Verification (Nepal)", {
            'description': "Configure NTA MDMS verification enforcement and warning policies for handset sales.",
            'fields': (
                'enable_nta_mdms_tracking',
                'warn_on_gray_market_sale'
            )
        }),
        ("6. POS Localization & Printer Hardware", {
            'fields': (
                ('currency_symbol', 'currency_code'),
                ('enable_nepali_calendar', 'default_language'),
                'thermal_printer_paper_width'
            )
        }),
        ("7. General Ledger Control Account Bindings (Double-Entry Automation)", {
            'description': (
                "Map operational touchpoints to general ledger Chart of Accounts accounts for "
                "automated real-time double-entry journal postings across POS sales, payments, and inventory."
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
    )

    def tax_system_mode_badge(self, obj):
        colors = {
            'VAT': '#10b981',
            'PAN': '#2563eb',
            'NO_TAX': '#64748b',
        }
        color = colors.get(obj.tax_system_mode, '#0f172a')
        return format_html(
            '<span style="color: white; background-color: {}; padding: 3px 10px; border-radius: 999px; font-weight: bold; font-size: 11px;">{}</span>',
            color, obj.get_tax_system_mode_display()
        )
    tax_system_mode_badge.short_description = "Tax Mode"

    def has_add_permission(self, request):
        return not SystemConfiguration.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ['timestamp', 'action_badge', 'module', 'object_repr', 'user', 'branch', 'ip_address']
    list_filter = ['action_type', 'module', 'branch', 'timestamp']
    search_fields = ['object_repr', 'details', 'user__username', 'ip_address']
    date_hierarchy = 'timestamp'
    readonly_fields = [f.name for f in AuditLog._meta.fields]

    def action_badge(self, obj):
        colors = {
            'CREATE': '#10b981',
            'UPDATE': '#3b82f6',
            'DELETE': '#ef4444',
            'BILL_CANCEL': '#dc2626',
            'PRICE_OVERRIDE': '#f59e0b',
            'STOCK_ADJUST': '#8b5cf6',
            'LOGIN_FAIL': '#ef4444',
        }
        color = colors.get(obj.action_type, '#64748b')
        return format_html(
            '<span style="color: white; background-color: {}; padding: 2px 8px; border-radius: 999px; font-weight: 600; font-size: 10.5px;">{}</span>',
            color, obj.get_action_type_display()
        )
    action_badge.short_description = "Action"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False