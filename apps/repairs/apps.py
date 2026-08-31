from django.apps import AppConfig

class RepairsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.repairs'
    verbose_name = 'Service, Repair & Warranty Management'

    def ready(self):
        import apps.repairs.signals