"""
Django Base Settings for Mobile Shop & Optical Inventory ERP (Nepal Context).
Provides core configuration, multi-branch scoping, white-label branding,
non-IRD proforma estimation rules, NTA MDMS compliance gateways, and
pre-owned device trade-in / KYC document parameterization.
"""

import os
from pathlib import Path
from decimal import Decimal
from decouple import config, Csv

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Security Settings
SECRET_KEY = config('SECRET_KEY', default='django-insecure-default-change-in-production-key')
DEBUG = config('DEBUG', default=False, cast=bool)
ALLOWED_HOSTS = config('ALLOWED_HOSTS', default='localhost,127.0.0.1,0.0.0.0', cast=Csv())

# Custom User Model
AUTH_USER_MODEL = 'users.User'

# Application definition
DJANGO_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.humanize',
]

THIRD_PARTY_APPS = [
    'rest_framework',
    'corsheaders',
]

LOCAL_APPS = [
    'apps.core.apps.CoreConfig',
    'apps.users.apps.UsersConfig',
    'apps.branches.apps.BranchesConfig',
    'apps.customers.apps.CustomersConfig',
    'apps.inventory.apps.InventoryConfig',
    'apps.products.apps.ProductsConfig',
    'apps.purchases.apps.PurchasesConfig',
    'apps.pos.apps.PosConfig',
    'apps.sales.apps.SalesConfig',
    'apps.repairs.apps.RepairsConfig',
    'apps.reports.apps.ReportsConfig',
    'apps.taxation.apps.TaxationConfig',
    'apps.integrations.apps.IntegrationsConfig',
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'apps.core.middleware.CoreBranchAndConfigMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'apps.core.context_processors.global_system_context',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'
ASGI_APPLICATION = 'config.asgi.application'

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator', 'OPTIONS': {'min_length': 6}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LOGIN_URL = 'users:login'
LOGIN_REDIRECT_URL = 'core:dashboard'
LOGOUT_REDIRECT_URL = 'users:login'

# Session & Cookie Security Hardening
SESSION_ENGINE = 'django.contrib.sessions.backends.db'
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
# Lowered session lifetime to 12 Hours (43,200s) to protect shared counter terminals and enforce daily shift logins
SESSION_COOKIE_AGE = config('SESSION_COOKIE_AGE', default=43200, cast=int)
SESSION_EXPIRE_AT_BROWSER_CLOSE = False
SESSION_SAVE_EVERY_REQUEST = False

CSRF_COOKIE_HTTPONLY = False
CSRF_COOKIE_SAMESITE = 'Lax'

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Kathmandu'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

# Dedicated File Storage Directories for Branding, Diagnostics, Optical Rx & Customer Ownership KYC
BRANCH_LOGO_UPLOAD_DIR = 'branches/logos/%Y/%m/'
REPAIR_EVIDENCE_UPLOAD_DIR = 'repairs/evidence/%Y/%m/'
OPTICAL_PRESCRIPTION_UPLOAD_DIR = 'repairs/optical_rx/%Y/%m/'
TRADE_IN_DOCUMENT_UPLOAD_DIR = 'trade_in/kyc/%Y/%m/'

# KYC & Ownership Undertaking Document Security Limits
KYC_ALLOWED_EXTENSIONS = ['jpg', 'jpeg', 'png', 'webp', 'pdf']
KYC_MAX_UPLOAD_SIZE = 12 * 1024 * 1024  # 12 MB limit

# NTA MDMS (Mobile Device Management System - Nepal Telecommunications Authority) Gateways
NTA_MDMS_LOOKUP_URL = config('NTA_MDMS_LOOKUP_URL', default='https://mdms.nta.gov.np/api/v1/device/verify')
NTA_MDMS_FALLBACK_PORTAL = 'https://mdms.nta.gov.np/'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.SessionAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 50,
}

CORS_ALLOW_ALL_ORIGINS = config('CORS_ALLOW_ALL_ORIGINS', default=False, cast=bool)
CORS_ALLOWED_ORIGINS = [
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "http://localhost:3000",
]

# Standardized default estimation slip header
IS_ESTIMATION_BILL_ONLY = config('IS_ESTIMATION_BILL_ONLY', default=True, cast=bool)
BILL_HEADER_TITLE = config('BILL_HEADER_TITLE', default="SALES ESTIMATE SLIP")
BILL_ESTIMATE_DISCLAIMER = config(
    'BILL_ESTIMATE_DISCLAIMER',
    default="NOTICE: This is an internal quotation / estimation slip only and NOT an official VAT/PAN Tax Invoice approved by IRD Nepal."
)

DEFAULT_CURRENCY = config('DEFAULT_CURRENCY', default='NPR')
DEFAULT_CURRENCY_SYMBOL = config('DEFAULT_CURRENCY_SYMBOL', default='Rs.')
DEFAULT_VAT_PERCENT = config('DEFAULT_VAT_PERCENT', default=Decimal('13.00'), cast=Decimal)
SHOP_PAN_NUMBER = config('SHOP_PAN_NUMBER', default='999999999')
SHOP_NAME = config('SHOP_NAME', default='Smart Mobile & Optical Gallery')
SHOP_ADDRESS = config('SHOP_ADDRESS', default='New Road, Kathmandu, Nepal')

SPARROW_SMS_TOKEN = config('SPARROW_SMS_TOKEN', default='')
AAKASH_SMS_AUTH_TOKEN = config('AAKASH_SMS_AUTH_TOKEN', default='')
FONEPAY_MERCHANT_ID = config('FONEPAY_MERCHANT_ID', default='')
ESEWA_MERCHANT_CODE = config('ESEWA_MERCHANT_CODE', default='EPAYTEST')
KHALTI_SECRET_KEY = config('KHALTI_SECRET_KEY', default='')

DEFAULT_BRANCH_ID = config('DEFAULT_BRANCH_ID', default=1, cast=int)
ENABLE_OFFLINE_PWA = config('ENABLE_OFFLINE_PWA', default=True, cast=bool)