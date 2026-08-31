from datetime import date
from decimal import Decimal
from django.shortcuts import render
from django.views.generic import TemplateView
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from apps.taxation.reports import TaxationReportGenerator
from apps.taxation.ird_sync import NonIRDDisclaimerEngine
from apps.core.models import SystemConfiguration
from apps.core.nepali_calendar import NepaliCalendar


def get_current_bs_month_date_range():
    """
    Returns (start_date_ad, end_date_ad, bs_year, bs_month, month_name_en, month_name_np)
    for the current Bikram Sambat calendar month.
    Ensures that tax registers and VAT summaries strictly align with Nepali tax periods
    (e.g., 1st of Baishakh to current date / end of Baishakh), avoiding the Gregorian AD month mismatch.
    """
    today_ad = date.today()
    bs_year, bs_month, bs_day = NepaliCalendar.ad_to_bs(today_ad)

    # 1st day of the current BS month converted to Gregorian AD
    start_date_ad = NepaliCalendar.bs_to_ad(bs_year, bs_month, 1)

    # End date is today (or through the active query period)
    end_date_ad = today_ad

    month_name_en = NepaliCalendar.NEPALI_MONTH_NAMES_EN[bs_month - 1]
    month_name_np = NepaliCalendar.NEPALI_MONTH_NAMES_NP[bs_month - 1]

    return start_date_ad, end_date_ad, bs_year, bs_month, month_name_en, month_name_np


class TaxDashboardView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    """
    Monthly VAT / Tax Overview Dashboard.
    Scapes sales and purchase data strictly matching the active Bikram Sambat (BS) month.
    """
    template_name = 'taxation/vat_overview.html'

    def test_func(self):
        return self.request.user.is_superuser or getattr(self.request.user, 'role', '') in ['OWNER', 'ACCOUNTANT', 'MANAGER']

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        branch = getattr(self.request, 'active_branch', None)

        start_of_bs_month, today_ad, bs_year, bs_month, bs_month_en, bs_month_np = get_current_bs_month_date_range()

        sales_summary = {
            'taxable': Decimal('0.00'),
            'non_taxable': Decimal('0.00'),
            'vat': Decimal('0.00'),
            'grand_total': Decimal('0.00')
        }
        purchase_summary = {
            'gross': Decimal('0.00'),
            'vat': Decimal('0.00'),
            'net': Decimal('0.00')
        }
        net_vat = Decimal('0.00')

        if branch:
            sales_data = TaxationReportGenerator.generate_sales_book(branch, start_of_bs_month, today_ad)
            purchase_data = TaxationReportGenerator.generate_purchase_book(branch, start_of_bs_month, today_ad)
            sales_summary = sales_data['totals']
            purchase_summary = purchase_data['totals']
            net_vat = sales_summary['vat'] - purchase_summary['vat']

        context.update({
            'sales_summary': sales_summary,
            'purchase_summary': purchase_summary,
            'net_vat': net_vat,
            'bs_year': bs_year,
            'bs_month': bs_month,
            'bs_month_name_en': bs_month_en,
            'bs_month_name_np': bs_month_np,
            'period_start_ad': start_of_bs_month,
            'period_end_ad': today_ad,
            'config': SystemConfiguration.get_solo()
        })
        return NonIRDDisclaimerEngine.inject_disclaimer(context)


class SalesRegisterReportView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    """Annex 5 format Internal Sales Register defaulting to the current BS month date range."""
    template_name = 'taxation/sales_vat_register.html'

    def test_func(self):
        return self.request.user.is_superuser or getattr(self.request.user, 'role', '') in ['OWNER', 'ACCOUNTANT']

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        branch = getattr(self.request, 'active_branch', None)

        start_of_bs_month, today_ad, bs_year, bs_month, bs_month_en, bs_month_np = get_current_bs_month_date_range()

        start = self.request.GET.get('start_date', start_of_bs_month.strftime('%Y-%m-%d'))
        end = self.request.GET.get('end_date', today_ad.strftime('%Y-%m-%d'))

        if branch:
            context['report'] = TaxationReportGenerator.generate_sales_book(branch, start, end)

        context.update({
            'start_date': start,
            'end_date': end,
            'bs_month_name_en': bs_month_en,
            'bs_month_name_np': bs_month_np,
            'bs_year': bs_year,
            'config': SystemConfiguration.get_solo()
        })
        return NonIRDDisclaimerEngine.inject_disclaimer(context)


class PurchaseRegisterReportView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    """Annex 7 format Internal Purchase Register defaulting to the current BS month date range."""
    template_name = 'taxation/purchase_vat_register.html'

    def test_func(self):
        return self.request.user.is_superuser or getattr(self.request.user, 'role', '') in ['OWNER', 'ACCOUNTANT']

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        branch = getattr(self.request, 'active_branch', None)

        start_of_bs_month, today_ad, bs_year, bs_month, bs_month_en, bs_month_np = get_current_bs_month_date_range()

        start = self.request.GET.get('start_date', start_of_bs_month.strftime('%Y-%m-%d'))
        end = self.request.GET.get('end_date', today_ad.strftime('%Y-%m-%d'))

        if branch:
            context['report'] = TaxationReportGenerator.generate_purchase_book(branch, start, end)

        context.update({
            'start_date': start,
            'end_date': end,
            'bs_month_name_en': bs_month_en,
            'bs_month_name_np': bs_month_np,
            'bs_year': bs_year,
            'config': SystemConfiguration.get_solo()
        })
        return NonIRDDisclaimerEngine.inject_disclaimer(context)