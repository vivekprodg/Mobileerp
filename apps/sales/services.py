"""
Bridge file providing backwards compatibility for direct `from apps.sales.services import SalesPOSService, TradeInValuationEngine` imports.
"""
from apps.sales.services.pos_service import SalesPOSService
from apps.sales.services.trade_in_engine import TradeInValuationEngine

__all__ = [
    'SalesPOSService',
    'TradeInValuationEngine',
]