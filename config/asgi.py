"""
ASGI config for Mobile Shop Inventory & Sales Management System.
Exposes the ASGI callable as a module-level variable named `application`.
"""

import os
from django.core.asgi import get_asgi_application

# Default to development settings if not specified
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.development')

application = get_asgi_application()