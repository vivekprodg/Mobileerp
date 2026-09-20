"""
Sales Services Package Bridge.
Provides clean backwards-compatibility for direct imports:
`from apps.sales.services import SalesPOSService, TradeInValuationEngine, TaxCalculator`
"""

from apps.sales.services.pos_service import SalesPOSService, TaxCalculator
from apps.sales.services.trade_in_engine import TradeInValuationEngine

__all__ = [
    'SalesPOSService',
    'TradeInValuationEngine',
    'TaxCalculator',
]