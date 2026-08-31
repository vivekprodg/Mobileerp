"""
Development Settings for Mobile Shop ERP.
"""

from .base import *
import dj_database_url

DEBUG = True

ALLOWED_HOSTS = ['*']

# Local PostgreSQL Database
DATABASES = {
    'default': dj_database_url.config(
        default=f"postgres://{config('DB_USER', default='postgres')}:{config('DB_PASSWORD', default='postgres_secure_password')}@{config('DB_HOST', default='127.0.0.1')}:{config('DB_PORT', default='5432')}/{config('DB_NAME', default='mobile_shop_db')}",
        conn_max_age=600,
        conn_health_checks=True,
    )
}

# Explicit Development CSRF & Cookie Configuration
CSRF_COOKIE_SECURE = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_HTTPONLY = False
CSRF_USE_SESSIONS = False

CSRF_TRUSTED_ORIGINS = [
    'http://localhost:8000',
    'http://127.0.0.1:8000',
    'http://0.0.0.0:8000',
]

STATICFILES_STORAGE = 'django.contrib.staticfiles.storage.StaticFilesStorage'
EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

INTERNAL_IPS = [
    '127.0.0.1',
    'localhost',
]