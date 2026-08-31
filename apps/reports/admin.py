from django.contrib import admin
from apps.reports.models import ScheduledReportLog, InventoryValuationSnapshot, ProductCostHistory

@admin.register(ScheduledReportLog)
class ScheduledReportLogAdmin(admin.ModelAdmin):
    list_display = ['report_type', 'branch', 'generated_by', 'export_format', 'created_at']
    list_filter = ['report_type', 'branch', 'export_format', 'created_at']
    readonly_fields = [f.name for f in ScheduledReportLog._meta.fields]

    def has_add_permission(self, request):
        return False

@admin.register(InventoryValuationSnapshot)
class InventoryValuationSnapshotAdmin(admin.ModelAdmin):
    list_display = [
        'snapshot_date', 'branch', 'total_units_count',
        'total_cost_valuation', 'total_retail_valuation',
        'projected_margin', 'generated_by'
    ]
    list_filter = ['branch', 'snapshot_date']
    search_fields = ['branch__name', 'notes']
    readonly_fields = [f.name for f in InventoryValuationSnapshot._meta.fields]

    def has_add_permission(self, request):
        return False

@admin.register(ProductCostHistory)
class ProductCostHistoryAdmin(admin.ModelAdmin):
    list_display = [
        'date_effective', 'product', 'old_cost_price', 'new_cost_price',
        'old_selling_price', 'new_selling_price', 'source_reference', 'changed_by'
    ]
    list_filter = ['date_effective', 'product__category', 'product__brand']
    search_fields = ['product__name', 'product__sku', 'source_reference', 'remarks']
    readonly_fields = [f.name for f in ProductCostHistory._meta.fields]

    def has_add_permission(self, request):
        return False