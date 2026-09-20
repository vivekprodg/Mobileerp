"""
Accounting Services Package.
Exposes the core Double-Entry Journal Engine and Auto-Posting Service.
"""

from .auto_posting import JournalEngine, AutoPostingService

__all__ = [
    'JournalEngine',
    'AutoPostingService',
]