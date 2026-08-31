from decimal import Decimal, ROUND_HALF_UP
from django.db.models import Sum, Q, F, DecimalField, Value, ExpressionWrapper
from django.db.models.functions import Coalesce

from apps.sales.models import SalesEstimate, SalesReturn, SalesReturnItem
from apps.purchases.models import GoodsReceivedNote
from apps.branches.models import Branch
from apps.core.models import SystemConfiguration


class TaxationReportGenerator:
    """
    Generates Sales Register (Annex 5 style) and Purchase Register (Annex 7 style)
    in estimation / proforma breakdown format.
    Includes both COMPLETED and PARTIALLY_RETURNED sales estimates, accurately
    deducting return refunds and tax adjustments from taxable and non-taxable columns.
    Accurately places amounts in the Non-Taxable / Exempt column when the store
    operates in PAN or No-Tax mode, and separates Taxable Base / VAT when in VAT mode.
    """

    @classmethod
    def generate_sales_book(cls, branch: Branch, start_date, end_date):
        config = SystemConfiguration.get_solo()
        is_vat_shop = (config.tax_system_mode == 'VAT')

        # Include COMPLETED and PARTIALLY_RETURNED sales estimates
        qs = SalesEstimate.objects.filter(
            branch=branch,
            bill_date_ad__gte=start_date,
            bill_date_ad__lte=end_date,
            status__in=['COMPLETED', 'PARTIALLY_RETURNED']
        ).prefetch_related('returns', 'returns__items').order_by('bill_date_ad', 'estimate_number')

        totals = qs.aggregate(
            total_taxable=Coalesce(Sum('taxable_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_non_taxable=Coalesce(Sum('non_taxable_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_vat=Coalesce(Sum('vat_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_grand=Coalesce(Sum('grand_total'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_subtotal=Coalesce(Sum('subtotal'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2)))
        )

        taxable_sum = totals['total_taxable']
        non_taxable_sum = totals['total_non_taxable']
        vat_sum = totals['total_vat']
        grand_sum = totals['total_grand']

        # Calculate return deductions for bills in this scope
        returns_aggregate = SalesReturn.objects.filter(original_estimate__in=qs).aggregate(
            total_refund=Coalesce(Sum('total_refund_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2)))
        )
        total_refund_amount = returns_aggregate['total_refund']

        if is_vat_shop:
            # Deduct returns proportionately from taxable base, VAT, and grand total
            returned_items = SalesReturnItem.objects.filter(sales_return__original_estimate__in=qs).select_related('estimate_item')
            return_taxable_deduct = Decimal('0.00')
            return_vat_deduct = Decimal('0.00')
            return_non_taxable_deduct = Decimal('0.00')

            for r_item in returned_items:
                est_item = r_item.estimate_item
                if est_item.is_vat_applicable and est_item.vat_rate > Decimal('0.00'):
                    rate = est_item.vat_rate
                    if est_item.tax_pricing_type == 'INCLUSIVE':
                        base_val = (r_item.refund_amount / (Decimal('1.00') + (rate / Decimal('100.00')))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                        vat_val = r_item.refund_amount - base_val
                    else:
                        base_val = r_item.refund_amount
                        vat_val = (base_val * (rate / Decimal('100.00'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                    return_taxable_deduct += base_val
                    return_vat_deduct += vat_val
                else:
                    return_non_taxable_deduct += r_item.refund_amount

            taxable_sum = max(Decimal('0.00'), taxable_sum - return_taxable_deduct)
            vat_sum = max(Decimal('0.00'), vat_sum - return_vat_deduct)
            non_taxable_sum = max(Decimal('0.00'), non_taxable_sum - return_non_taxable_deduct)
            grand_sum = max(Decimal('0.00'), grand_sum - total_refund_amount)
        else:
            # If PAN-only or No-Tax shop, all revenue is non-taxable base with 0.00 VAT
            taxable_sum = Decimal('0.00')
            vat_sum = Decimal('0.00')
            grand_sum = max(Decimal('0.00'), grand_sum - total_refund_amount)
            non_taxable_sum = grand_sum

        return {
            'records': qs,
            'is_vat_shop': is_vat_shop,
            'totals': {
                'taxable': taxable_sum,
                'non_taxable': non_taxable_sum,
                'vat': vat_sum,
                'grand_total': grand_sum,
                'total_refunds': total_refund_amount,
            }
        }

    @classmethod
    def generate_purchase_book(cls, branch: Branch, start_date, end_date):
        config = SystemConfiguration.get_solo()

        qs = GoodsReceivedNote.objects.filter(
            branch=branch,
            bill_date__gte=start_date,
            bill_date__lte=end_date,
            status='RECEIVED'
        ).select_related('supplier').order_by('bill_date', 'grn_number')

        totals = qs.aggregate(
            total_gross=Coalesce(Sum('gross_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_vat=Coalesce(Sum('vat_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2))),
            total_net=Coalesce(Sum('net_total_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=18, decimal_places=2)))
        )

        return {
            'records': qs,
            'totals': {
                'gross': totals['total_gross'],
                'vat': totals['total_vat'],
                'net': totals['total_net'],
            }
        }