"""
Compatibility bridge file re-exporting InventoryService and ExcelProductImporter from the services package.
"""
from apps.inventory.services.inventory_service import InventoryService
from apps.inventory.services.excel_importer import ExcelProductImporter

__all__ = ['InventoryService', 'ExcelProductImporter']