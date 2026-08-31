from datetime import date
from django.utils import timezone
from django.core.cache import cache
from apps.core.models import SystemConfiguration
from apps.core.utils.nepali_date_converter import ad_to_bs_string
from apps.branches.models import Branch


def global_system_context(request):
    """
    Exposes white-label branding (Dynamic Company Name, Optional Logo URL),
    master shop tax mode, dynamic tax rate, cached active branches,
    cached Bikram Sambat date context, user role-based UI hints, MDMS tracking flags,
    and trade-in parameters globally across all templates.
    """
    # 1. System Configuration Singleton
    config = getattr(request, 'system_config', None) or SystemConfiguration.get_solo()

    # 2. Date Context (Cached per current AD Date)
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
        # Cache for 24 hours (until next date boundary)
        cache.set(cache_key_bs_date, date_context, timeout=86400)

    # 3. Active Branches (Cached for 300 seconds)
    active_branches = cache.get_or_set(
        'global_active_branches_list',
        lambda: list(Branch.objects.filter(is_active=True).order_by('-is_main_branch', 'name')),
        timeout=300
    )

    # 4. White-Label Branding Resolution based on Active Store Outlet
    active_branch = getattr(request, 'active_branch', None)
    if not active_branch:
        active_branch = Branch.get_default_main_branch()

    resolved_company_name = active_branch.display_company_name if active_branch else config.company_name_en
    resolved_company_name_np = active_branch.display_company_name_np if active_branch else config.company_name_np
    resolved_logo_url = active_branch.logo_url if active_branch else ""
    has_custom_logo = bool(resolved_logo_url)

    # 5. User Role Scoped Context for UI Authorization Badges
    user = getattr(request, 'user', None)
    is_authenticated = user.is_authenticated if user else False
    user_role = getattr(user, 'role', '') if is_authenticated else ''

    return {
        # White-Label Dynamic Branding Context
        'COMPANY_NAME': resolved_company_name,
        'COMPANY_NAME_NP': resolved_company_name_np,
        'COMPANY_LOGO_URL': resolved_logo_url,
        'HAS_COMPANY_LOGO': has_custom_logo,
        'STORE_OUTLET_NAME': active_branch.name if active_branch else "Main Branch",
        'STORE_OUTLET_CODE': active_branch.code if active_branch else "MAIN",

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