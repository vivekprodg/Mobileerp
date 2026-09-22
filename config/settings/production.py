"""
Production Settings for Mobile Shop & Optical Store ERP (Nepal Context).
Enforces strict secret key validation, domain whitelisting, HTTPS/SSL encryption,
centralized shared cache (Redis or DatabaseCache) across all Gunicorn workers,
and persistent audit logging directories.
"""

import os
from pathlib import Path
from decouple import config, Csv
import dj_database_url
from django.core.exceptions import ImproperlyConfigured

from .base import *

DEBUG = False

# ==============================================================================
# 1. ALLOWED HOSTS & DOMAIN WHITELISTING
# ==============================================================================
ALLOWED_HOSTS = config(
    'ALLOWED_HOSTS',
    default='localhost,127.0.0.1',
    cast=Csv()
)

# ==============================================================================
# 2. SECRET KEY SECURITY & INTEGRITY CHECK
# ==============================================================================
INSECURE_PLACEHOLDER_KEYS = [
    '',
    'django-insecure-default-change-in-production-key',
    'django-insecure-change-this-in-production-key-here',
    'change-this-in-production-key-here',
]

if (
    not SECRET_KEY
    or SECRET_KEY in INSECURE_PLACEHOLDER_KEYS
    or SECRET_KEY.startswith('django-insecure-')
    or len(SECRET_KEY) < 40
):
    raise ImproperlyConfigured(
        "CRITICAL SECURITY ERROR: The application cannot start in production mode "
        "because the SECRET_KEY is missing, using an insecure default, or too short (<40 chars). "
        "Please generate a 50+ character random SECRET_KEY and set it in your .env file."
    )

# ==============================================================================
# 3. PRODUCTION DATABASE (PostgreSQL with Connection Pooling)
# ==============================================================================
DATABASES = {
    'default': dj_database_url.config(
        default=f"postgres://{config('DB_USER', default='postgres')}:{config('DB_PASSWORD', default='postgres_secure_password')}@{config('DB_HOST', default='127.0.0.1')}:{config('DB_PORT', default='5432')}/{config('DB_NAME', default='mobile_shop_db')}",
        conn_max_age=600,
        conn_health_checks=True,
        ssl_require=config('DB_SSL_REQUIRE', default=False, cast=bool),
    )
}

# ==============================================================================
# 4. CENTRALIZED PRODUCTION CACHE (Shared across Gunicorn workers)
# ==============================================================================
# Prevents multi-worker cache thrashing where each worker maintains an isolated
# cache and re-queries PostgreSQL repeatedly.
REDIS_URL = config('REDIS_URL', default='')
CACHE_TYPE = config('CACHE_TYPE', default='redis' if REDIS_URL else 'db')

if CACHE_TYPE == 'redis' and REDIS_URL:
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.redis.RedisCache',
            'LOCATION': REDIS_URL,
            'KEY_PREFIX': 'mobileshop_prod',
            'TIMEOUT': 300,
        }
    }
elif CACHE_TYPE == 'db':
    # Shared database cache across all workers (run: python manage.py createcachetable)
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.db.DatabaseCache',
            'LOCATION': 'erp_production_cache_table',
            'TIMEOUT': 300,
        }
    }
else:
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'mobileshop_prod_locmem',
            'TIMEOUT': 300,
        }
    }

# ==============================================================================
# 5. HTTPS / SSL ENCRYPTION & COOKIE SECURITY
# ==============================================================================
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SECURE_SSL_REDIRECT = config('SECURE_SSL_REDIRECT', default=True, cast=bool)

SESSION_COOKIE_SECURE = config('SESSION_COOKIE_SECURE', default=True, cast=bool)
CSRF_COOKIE_SECURE = config('CSRF_COOKIE_SECURE', default=True, cast=bool)
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = False
SESSION_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SAMESITE = 'Lax'

SESSION_COOKIE_AGE = config('SESSION_COOKIE_AGE', default=43200, cast=int)

SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'DENY'
SECURE_HSTS_SECONDS = config('SECURE_HSTS_SECONDS', default=31536000, cast=int)
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True

CSRF_TRUSTED_ORIGINS = config(
    'CSRF_TRUSTED_ORIGINS',
    default='https://localhost,https://127.0.0.1',
    cast=Csv()
)

# ==============================================================================
# 6. LOG FILE DIRECTORIES & PRODUCTION LOGGING
# ==============================================================================
LOG_DIR = BASE_DIR / 'logs'
LOG_DIR.mkdir(parents=True, exist_ok=True)

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '[{asctime}] {levelname} [{name}:{lineno}] {message}',
            'style': '{',
            'datefmt': '%Y-%m-%d %H:%M:%S',
        },
        'simple': {
            'format': '{levelname} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
        'error_file': {
            'level': 'ERROR',
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': LOG_DIR / 'error.log',
            'maxBytes': 1024 * 1024 * 15,
            'backupCount': 10,
            'formatter': 'verbose',
        },
        'app_file': {
            'level': 'INFO',
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': LOG_DIR / 'application.log',
            'maxBytes': 1024 * 1024 * 15,
            'backupCount': 10,
            'formatter': 'verbose',
        },
    },
    'root': {
        'handlers': ['console', 'app_file', 'error_file'],
        'level': 'INFO',
    },
    'loggers': {
        'django': {
            'handlers': ['console', 'error_file'],
            'level': 'INFO',
            'propagate': False,
        },
        'django.security': {
            'handlers': ['console', 'error_file'],
            'level': 'WARNING',
            'propagate': False,
        },
        'apps': {
            'handlers': ['console', 'app_file', 'error_file'],
            'level': 'INFO',
            'propagate': False,
        },
    },
}