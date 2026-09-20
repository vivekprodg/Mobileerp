from decimal import Decimal
from django.contrib import admin
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from django.contrib import messages

from apps.taxation.models import TaxPeriodSummary
from apps.taxation.reports import TaxationReportGenerator


@admin.register(TaxPeriodSummary)
class TaxPeriodSummaryAdmin(admin.ModelAdmin):
    list_display = [
        'branch',
        'fiscal_year_badge',
        'nepali_year',
        'nepali_month_display',
        'period_bounds_display',
        'total_sales_vat_display',
        'total_purchase_vat_display',
        'net_vat_payable_badge',
        'is_locked_badge',
        'closed_by'
    ]
    list_filter = [
        'fiscal_year',
        'nepali_year',
        'nepali_month',
        'branch',
        'is_locked',
        'is_estimation_report'
    ]
    search_fields = [
        'branch__name',
        'branch__code',
        'fiscal_year',
        'nepali_year'
    ]
    readonly_fields = [
        'fiscal_year',
        'period_start_ad',
        'period_end_ad',
        'net_vat_payable',
        'created_at',
        'updated_at'
    ]
    actions = ['lock_selected_periods', 'unlock_selected_periods', 'recalculate_selected_periods']

    fieldsets = (
        ("1. Period & Fiscal Year Identity (नेपाली आर्थिक वर्ष)", {
            'description': "Each monthly tax and sales summary is tagged with its official Nepali Fiscal Year (e.g. Shrawan 2080 to Ashadh 2081 = FY 2080/81).",
            'fields': (
                ('branch', 'fiscal_year'),
                ('nepali_year', 'nepali_month'),
                ('period_start_ad', 'period_end_ad'),
            )
        }),
        ("2. Output VAT / Sales Register (बिक्री खाता सारांश)", {
            'fields': (
                ('total_sales_taxable', 'total_sales_non_taxable'),
                'total_sales_vat',
            )
        }),
        ("3. Input VAT / Purchase Register (खरिद खाता सारांश)", {
            'fields': (
                ('total_purchase_taxable', 'total_purchase_non_taxable'),
                'total_purchase_vat',
            )
        }),
        ("4. Net Assessment & Reconciliation Status", {
            'fields': (
                'net_vat_payable',
                ('is_estimation_report', 'is_locked'),
                'closed_by',
                ('created_at', 'updated_at'),
            )
        }),
    )

    def fiscal_year_badge(self, obj):
        fy = obj.fiscal_year or '-'
        return format_html(
            '<span style="background-color: #0f172a; color: #ffffff; padding: 2px 8px; border-radius: 999px; font-weight: 700; font-size: 11px;">{}</span>',
            fy
        )
    fiscal_year_badge.short_description = _("Fiscal Year")
    fiscal_year_badge.admin_order_field = 'fiscal_year'

    def nepali_month_display(self, obj):
        return format_html(
            '<strong>{}</strong> <small style="color: #64748b;">({})</small>',
            obj.get_nepali_month_display(),
            obj.month_name_np
        )
    nepali_month_display.short_description = _("BS Month")
    nepali_month_display.admin_order_field = 'nepali_month'

    def period_bounds_display(self, obj):
        if obj.period_start_ad and obj.period_end_ad:
            return f"{obj.period_start_ad.strftime('%b %d')} - {obj.period_end_ad.strftime('%b %d, %Y')}"
        return "-"
    period_bounds_display.short_description = _("AD Date Range")

    def total_sales_vat_display(self, obj):
        return f"Rs. {obj.total_sales_vat:,.2f}"
    total_sales_vat_display.short_description = _("Output VAT")

    def total_purchase_vat_display(self, obj):
        return f"Rs. {obj.total_purchase_vat:,.2f}"
    total_purchase_vat_display.short_description = _("Input VAT")

    def net_vat_payable_badge(self, obj):
        val = obj.net_vat_payable
        if val > Decimal('0.00'):
            return format_html(
                '<span style="color: #dc2626; background-color: #fee2e2; padding: 2px 8px; border-radius: 999px; font-weight: bold; font-family: monospace;">Rs. {:,.2f} (Payable)</span>',
                val
            )
        elif val < Decimal('0.00'):
            return format_html(
                '<span style="color: #059669; background-color: #d1fae5; padding: 2px 8px; border-radius: 999px; font-weight: bold; font-family: monospace;">Rs. {:,.2f} (Credit)</span>',
                abs(val)
            )
        return format_html('<span style="color: #64748b; font-family: monospace;">Rs. 0.00</span>')
    net_vat_payable_badge.short_description = _("Net VAT Assessment")
    net_vat_payable_badge.admin_order_field = 'net_vat_payable'

    def is_locked_badge(self, obj):
        if obj.is_locked:
            return format_html(
                '<span style="color: #ffffff; background-color: #475569; padding: 2px 7px; border-radius: 999px; font-weight: bold; font-size: 10px;">LOCKED</span>'
            )
        return format_html(
            '<span style="color: #047857; background-color: #ecfdf5; border: 1px solid #a7f3d0; padding: 2px 7px; border-radius: 999px; font-weight: bold; font-size: 10px;">OPEN</span>'
        )
    is_locked_badge.short_description = _("Lock Status")
    is_locked_badge.admin_order_field = 'is_locked'

    @admin.action(description=_("Lock selected tax periods"))
    def lock_selected_periods(self, request, queryset):
        updated = queryset.update(is_locked=True, closed_by=request.user)
        messages.success(request, f"{updated} tax period summary record(s) locked successfully.")

    @admin.action(description=_("Unlock selected tax periods"))
    def unlock_selected_periods(self, request, queryset):
        updated = queryset.update(is_locked=False)
        messages.success(request, f"{updated} tax period summary record(s) unlocked.")

    @admin.action(description=_("Recalculate figures from live Sales & Purchases"))
    def recalculate_selected_periods(self, request, queryset):
        recalculated = 0
        for summary in queryset.filter(is_locked=False):
            if summary.period_start_ad and summary.period_end_ad:
                sales_report = TaxationReportGenerator.generate_sales_book(
                    summary.branch, summary.period_start_ad, summary.period_end_ad
                )
                purchase_report = TaxationReportGenerator.generate_purchase_book(
                    summary.branch, summary.period_start_ad, summary.period_end_ad
                )

                s_totals = sales_report.get('totals', {})
                p_totals = purchase_report.get('totals', {})

                summary.total_sales_taxable = s_totals.get('taxable', Decimal('0.00'))
                summary.total_sales_non_taxable = s_totals.get('non_taxable', Decimal('0.00'))
                summary.total_sales_vat = s_totals.get('vat', Decimal('0.00'))

                summary.total_purchase_taxable = p_totals.get('net', Decimal('0.00'))
                summary.total_purchase_vat = p_totals.get('vat', Decimal('0.00'))

                summary.save()
                recalculated += 1

        messages.success(request, f"{recalculated} open tax period summary record(s) re-aggregated from transactions.")