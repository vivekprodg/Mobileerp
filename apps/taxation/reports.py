"""
Taxation & Proforma Book Report Generator (Annex 5 Sales Book, Annex 7 Purchase Book, Daily VAT Ledger, 6-Month Summaries & GL Reconciliation).

Architecture:
All underlying VAT calculation, line-item snapshotting, period aggregation,
6-month multi-period summaries, period comparisons, GL reconciliation, and drill-down resolution
have been centralized into `VATLedgerService`.
`TaxationReportGenerator` acts as the standard façade delegating directly to
`VATLedgerService`, guaranteeing backwards-compatibility and a single source of truth across the codebase.
"""

from datetime import date
from typing import Dict, Any, List, Optional, Union

from apps.branches.models import Branch
from apps.taxation.services.vat_ledger_service import VATLedgerService

class TaxationReportGenerator:
    """
    Standard façade delegating all taxation calculations, statutory book generation,
    6-month period-over-period comparisons, General Ledger reconciliations,
    and drill-down queries directly to VATLedgerService.
    """

    # =========================================================================
    # 1. SALES BOOK GENERATOR (ANNEX 5)
    # =========================================================================
    @classmethod
    def generate_sales_book(
        cls,
        branch: Optional[Branch],
        start_date: date,
        end_date: date,
        include_drilldown: bool = True
    ) -> Dict[str, Any]:
        """Annex 5 Sales Book."""
        return VATLedgerService.get_sales_vat_summary(
            branch=branch,
            start_date=start_date,
            end_date=end_date,
            include_drilldown=include_drilldown
        )

    # =========================================================================
    # 2. PURCHASE BOOK GENERATOR (ANNEX 7)
    # =========================================================================
    @classmethod
    def generate_purchase_book(
        cls,
        branch: Optional[Branch],
        start_date: date,
        end_date: date,
        include_drilldown: bool = True
    ) -> Dict[str, Any]:
        """Annex 7 Purchase Book."""
        return VATLedgerService.get_purchase_vat_summary(
            branch=branch,
            start_date=start_date,
            end_date=end_date,
            include_drilldown=include_drilldown
        )

    # =========================================================================
    # 3. DAY-WISE COMBINED VAT LEDGER
    # =========================================================================
    @classmethod
    def generate_daily_vat_ledger(
        cls,
        branch: Optional[Branch],
        start_date: date,
        end_date: date,
        include_drilldown: bool = True
    ) -> Dict[str, Any]:
        """Day-Wise Combined VAT & Tax Assessment Ledger."""
        return VATLedgerService.get_daily_vat_ledger(
            branch=branch,
            start_date=start_date,
            end_date=end_date,
            include_drilldown=include_drilldown
        )

    # =========================================================================
    # 4. MONTHLY VAT SUMMARY
    # =========================================================================
    @classmethod
    def get_monthly_vat_summary(
        cls,
        branch: Optional[Branch],
        bs_year: int,
        bs_month: int,
        include_drilldown: bool = False
    ) -> Dict[str, Any]:
        """Monthly VAT Summary Façade."""
        return VATLedgerService.get_monthly_vat_summary(
            branch=branch,
            bs_year=bs_year,
            bs_month=bs_month,
            include_drilldown=include_drilldown
        )

    # =========================================================================
    # 5. 6-MONTH VAT REPORT & PERIOD-OVER-PERIOD COMPARISON (EXPOSED FAÇADE)
    # =========================================================================
    @classmethod
    def generate_six_month_vat_summary(
        cls,
        branch: Optional[Branch] = None,
        end_bs_year: Optional[int] = None,
        end_bs_month: Optional[int] = None,
        start_bs_year: Optional[int] = None,
        start_bs_month: Optional[int] = None,
        fiscal_year_name: Optional[str] = None,
        half: Optional[int] = None,
        include_comparison: bool = True
    ) -> Dict[str, Any]:
        """
        6-Month Consecutive VAT Report & Summary Façade.
        Returns:
            - 6 monthly rows (Output VAT, Returns, Net Output, Input VAT, Debit Notes, Net Input, Net VAT)
            - Period totals and cumulative running balance
            - Embedded previous 6-month comparison with variance metrics
            - Hierarchical drilldown parameters and URLs
        """
        return VATLedgerService.get_six_month_vat_summary(
            branch=branch,
            end_bs_year=end_bs_year,
            end_bs_month=end_bs_month,
            start_bs_year=start_bs_year,
            start_bs_month=start_bs_month,
            fiscal_year_name=fiscal_year_name,
            half=half,
            include_comparison=include_comparison
        )

    @classmethod
    def get_six_month_vat_comparison(
        cls,
        branch: Optional[Branch] = None,
        end_bs_year: Optional[int] = None,
        end_bs_month: Optional[int] = None,
        start_bs_year: Optional[int] = None,
        start_bs_month: Optional[int] = None,
        fiscal_year_name: Optional[str] = None,
        half: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Period-Over-Period 6-Month VAT Comparison Façade.
        Compares Current 6 Months vs. Previous 6 Months across all 7 VAT dimensions:
        Output VAT, Sales Return VAT, Net Output VAT, Input VAT, Purchase Return VAT,
        Net Input VAT, Net VAT Assessment (Payable / Credit).
        """
        return VATLedgerService.get_six_month_vat_comparison(
            branch=branch,
            end_bs_year=end_bs_year,
            end_bs_month=end_bs_month,
            start_bs_year=start_bs_year,
            start_bs_month=start_bs_month,
            fiscal_year_name=fiscal_year_name,
            half=half
        )

    # =========================================================================
    # 6. FISCAL YEAR SUMMARY
    # =========================================================================
    @classmethod
    def get_fiscal_year_vat_summary(
        cls,
        branch: Optional[Branch],
        fiscal_year_name: str
    ) -> Dict[str, Any]:
        """Annual Nepali Fiscal Year VAT Summary Façade."""
        return VATLedgerService.get_fiscal_year_vat_summary(
            branch=branch,
            fiscal_year_name=fiscal_year_name
        )

    # =========================================================================
    # 7. GENERAL LEDGER PARITY RECONCILIATION
    # =========================================================================
    @classmethod
    def reconcile_vat_with_general_ledger(
        cls,
        branch: Optional[Branch],
        start_date: date,
        end_date: date,
        include_transaction_reconciliation: bool = True
    ) -> Dict[str, Any]:
        """
        Façade method connecting the VAT service reconciliation engine to the reporting layer:
        - Reconciles Output VAT (Annex 5 Net) vs. GL Account 2210.
        - Reconciles Input VAT (Annex 7 Net) vs. GL Account 1410.
        - Computes exact variances and Net VAT comparison.
        - Provides itemized transaction-level audit checks across all invoices, GRNs, returns, and debit notes.
        Does not duplicate reconciliation calculations; delegates cleanly to VATLedgerService.
        """
        return VATLedgerService.reconcile_vat_with_general_ledger(
            branch=branch,
            start_date=start_date,
            end_date=end_date,
            include_transaction_reconciliation=include_transaction_reconciliation
        )

    @classmethod
    def reconcile_transactions_vat_with_gl(
        cls,
        branch: Optional[Branch],
        start_date: date,
        end_date: date
    ) -> Dict[str, Any]:
        """Transaction-Level VAT vs. General Ledger Reconciliation Façade."""
        return VATLedgerService.reconcile_transactions_vat_with_gl(
            branch=branch,
            start_date=start_date,
            end_date=end_date
        )

    # =========================================================================
    # 8. DIRECT DRILL-DOWN RESOLVERS
    # =========================================================================
    @classmethod
    def get_invoice_vat_drilldown(cls, invoice_id_or_number: Union[int, str]) -> Dict[str, Any]:
        """Direct Sales Invoice VAT drilldown."""
        return VATLedgerService.get_invoice_vat_drilldown(invoice_id_or_number)

    @classmethod
    def get_grn_vat_drilldown(cls, grn_id_or_number: Union[int, str]) -> Dict[str, Any]:
        """Direct Inward GRN VAT drilldown."""
        return VATLedgerService.get_grn_vat_drilldown(grn_id_or_number)

    @classmethod
    def get_sales_return_vat_drilldown(cls, return_id_or_number: Union[int, str]) -> Dict[str, Any]:
        """Direct Customer Sales Return / Credit Note VAT drilldown."""
        return VATLedgerService.get_sales_return_vat_drilldown(return_id_or_number)

    @classmethod
    def get_purchase_return_vat_drilldown(cls, return_id_or_number: Union[int, str]) -> Dict[str, Any]:
        """Direct Supplier Purchase Return / Debit Note VAT drilldown."""
        return VATLedgerService.get_purchase_return_vat_drilldown(return_id_or_number)

    @classmethod
    def get_date_vat_drilldown(cls, branch: Optional[Branch], target_date: date) -> Dict[str, Any]:
        """Direct single-day VAT drilldown."""
        return VATLedgerService.get_day_vat_drilldown(branch, target_date)

    # =========================================================================
    # 9. BACKWARD-COMPATIBILITY METHOD BRIDGES
    # =========================================================================
    @staticmethod
    def _extract_sales_item_vat_detail(item, is_vat_shop: bool = True) -> Dict[str, Any]:
        return VATLedgerService.extract_sales_item_vat_detail(item, is_vat_shop)

    @staticmethod
    def _extract_sales_return_item_vat_detail(r_item, is_vat_shop: bool = True) -> Dict[str, Any]:
        return VATLedgerService.extract_sales_return_item_vat_detail(r_item, is_vat_shop)

    @staticmethod
    def _extract_grn_item_vat_detail(item) -> Dict[str, Any]:
        return VATLedgerService.extract_grn_item_vat_detail(item)

    @staticmethod
    def _extract_purchase_return_item_vat_detail(item) -> Dict[str, Any]:
        return VATLedgerService.extract_purchase_return_item_vat_detail(item)

    @staticmethod
    def _fetch_gl_journal_map(doc_numbers: List[str], voucher_type: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        return VATLedgerService._fetch_gl_journal_map(doc_numbers, voucher_type)