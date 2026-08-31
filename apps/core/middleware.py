from django.utils.deprecation import MiddlewareMixin
from django.core.cache import cache
from apps.core.models import SystemConfiguration
from apps.branches.models import Branch


class CoreBranchAndConfigMiddleware(MiddlewareMixin):
    """
    Attaches current active branch context and system configuration singleton
    to every HttpRequest.
    Enforces role-based branch scoping so non-privileged staff accounts are strictly
    locked to their assigned home branch, while Owners, Managers, and Superusers
    can seamlessly switch between active branch sessions.
    """

    def process_request(self, request):
        # 1. Cache System Configuration Singleton
        request.system_config = cache.get_or_set(
            'system_configuration_singleton',
            SystemConfiguration.get_solo,
            timeout=300
        )

        active_branch = None
        user = getattr(request, 'user', None)

        # 2. Check if user is authenticated and enforce role-based branch restrictions
        if user and user.is_authenticated:
            is_privileged = user.is_superuser or getattr(user, 'role', '') in ['OWNER', 'MANAGER']
            assigned = getattr(user, 'assigned_branch', None)

            if not is_privileged and assigned and assigned.is_active:
                # Non-privileged staff are strictly locked to their assigned branch
                active_branch = assigned
                request.active_branch = active_branch
                if hasattr(request, 'session'):
                    if request.session.get('active_branch_id') != active_branch.id:
                        request.session['active_branch_id'] = active_branch.id
                return

        # 3. For Privileged Users (Superusers, Owners, Managers) and Anonymous Requests:
        session_branch_id = request.session.get('active_branch_id') if hasattr(request, 'session') else None

        # Priority 1: Check Session for explicitly selected branch
        if session_branch_id:
            active_branch = Branch.objects.filter(id=session_branch_id, is_active=True).first()

        # Priority 2: User's assigned default branch
        if not active_branch and user and user.is_authenticated:
            assigned = getattr(user, 'assigned_branch', None)
            if assigned and assigned.is_active:
                active_branch = assigned

        # Priority 3: Guaranteed Self-Healing Central Fallback
        if not active_branch:
            active_branch = Branch.get_default_main_branch()

        request.active_branch = active_branch

        # 4. Persist to session ONLY if session value changed or was missing
        if hasattr(request, 'session') and active_branch:
            if session_branch_id != active_branch.id:
                request.session['active_branch_id'] = active_branch.id