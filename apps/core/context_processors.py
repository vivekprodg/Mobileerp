from datetime import date
from django.utils import timezone
from django.core.cache import cache
from apps.core.models import SystemConfiguration
from apps.core.utils.nepali_date_converter import ad_to_bs_string
from apps.branches.models import Branch

def global_system_context(request):
    """
    Exposes white-label branding, master shop tax mode, cached active branches,
    cached Bikram Sambat date strings, and user role hints globally to all templates.
    Optimized with complete memory caching to prevent per-request database hits on navigation headers.
    """
    # 1. System Configuration Singleton (Cached in Middleware or fetched here)
    config = getattr(request, 'system_config', None)
    if not config:
        config = cache.get_or_set(
            'system_configuration_singleton',
            SystemConfiguration.get_solo,
            timeout=3600
        )

    # 2. Bikram Sambat Date Context (Cached per calendar day)
    today_ad = timezone.now().date()
    cache_key_bs_date = f"bs_date_strings_{today_ad.isoformat()}"
    date_context = cache.get(cache_key_bs_date)

    if not date_context:
        bs_date_en = ad_to_bs_string(today_ad, lang='en')
        bs_date_np = ad_to_bs_string(today_ad, lang='np')
        date_context = {
            'CURRENT_BS_DATE_EN': bs_date_en,
            'CURRENT_BS_DATE_NP': bs_date_np,
        }
        cache.set(cache_key_bs_date, date_context, timeout=86400)

    # 3. Active Branches (Cached for 300 seconds)
    active_branches = cache.get_or_set(
        'global_active_branches_list',
        lambda: list(Branch.objects.filter(is_active=True).order_by('-is_main_branch', 'name')),
        timeout=300
    )

    # 4. Active Store Outlet & Cached Branding Dictionary
    active_branch = getattr(request, 'active_branch', None)
    if not active_branch:
        active_branch = Branch.get_default_main_branch()

    branch_id = active_branch.id if active_branch else 0
    cache_key_branding = f"resolved_branding_branch_{branch_id}"
    branding = cache.get(cache_key_branding)

    if not branding:
        branding = {
            'COMPANY_NAME': active_branch.display_company_name if active_branch else config.company_name_en,
            'COMPANY_NAME_NP': active_branch.display_company_name_np if active_branch else config.company_name_np,
            'COMPANY_LOGO_URL': active_branch.logo_url if active_branch else "",
            'HAS_COMPANY_LOGO': bool(active_branch.logo_url) if active_branch else False,
            'STORE_OUTLET_NAME': active_branch.name if active_branch else "Main Branch",
            'STORE_OUTLET_CODE': active_branch.code if active_branch else "MAIN",
        }
        cache.set(cache_key_branding, branding, timeout=3600)

    # 5. User Role Scoped Context for UI Authorization Badges
    user = getattr(request, 'user', None)
    is_authenticated = user.is_authenticated if user else False
    user_role = getattr(user, 'role', '') if is_authenticated else ''

    return {
        # White-Label Dynamic Branding (From Memory Cache)
        'COMPANY_NAME': branding['COMPANY_NAME'],
        'COMPANY_NAME_NP': branding['COMPANY_NAME_NP'],
        'COMPANY_LOGO_URL': branding['COMPANY_LOGO_URL'],
        'HAS_COMPANY_LOGO': branding['HAS_COMPANY_LOGO'],
        'STORE_OUTLET_NAME': branding['STORE_OUTLET_NAME'],
        'STORE_OUTLET_CODE': branding['STORE_OUTLET_CODE'],

        # Core Configuration & Tax Modes
        'SYS_CONFIG': config,
        'SHOP_TAX_MODE': config.tax_system_mode,
        'IS_VAT_MODE': config.tax_system_mode == 'VAT',
        'IS_PAN_MODE': config.tax_system_mode == 'PAN',
        'IS_NO_TAX_MODE': config.tax_system_mode == 'NO_TAX',
        'DEFAULT_TAX_RATE': config.default_vat_rate,
        'BILL_HEADER_TITLE': config.bill_header_title,
        'ACTIVE_BRANCH': active_branch,
        'AVAILABLE_BRANCHES': active_branches,
        'CURRENT_BS_DATE_EN': date_context['CURRENT_BS_DATE_EN'],
        'CURRENT_BS_DATE_NP': date_context['CURRENT_BS_DATE_NP'],
        'IS_ESTIMATE_ONLY': config.is_estimation_bill_only,
        'ESTIMATE_DISCLAIMER': config.bill_estimate_disclaimer,

        # User Role Authorization Hints
        'USER_ROLE': user_role,
        'IS_OWNER': is_authenticated and (user.is_superuser or user_role == 'OWNER'),
        'IS_MANAGER': is_authenticated and (user.is_superuser or user_role in ['OWNER', 'MANAGER']),
        'IS_ACCOUNTANT': is_authenticated and (user.is_superuser or user_role in ['OWNER', 'ACCOUNTANT']),
        'IS_STAFF': is_authenticated and (user_role == 'STAFF'),

        # NTA MDMS & Trade-In Global Context
        'ENABLE_MDMS_TRACKING': config.enable_nta_mdms_tracking,
        'MDMS_WARN_GRAY_MARKET': config.warn_on_gray_market_sale,
        'DEFAULT_TRADE_IN_MARGIN': config.default_trade_in_margin_percent,
        'LEGAL_UNDERTAKING_TEMPLATE': config.undertaking_declaration_text_np,
    }