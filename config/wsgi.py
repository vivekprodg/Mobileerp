"""
WSGI config for Mobile Shop Inventory & Sales Management System.
Exposes the WSGI callable as a module-level variable named `application`.
"""

import os
from django.core.wsgi import get_wsgi_application

# Default to development settings if not specified
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.development')

application = get_wsgi_application()