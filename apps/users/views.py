from datetime import datetime, timedelta
from django.shortcuts import render, redirect, get_object_or_404
from django.views.generic import ListView, CreateView, UpdateView, View
from django.contrib.auth.views import LoginView, LogoutView
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib import messages
from django.urls import reverse_lazy
from django.http import JsonResponse
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone
from django.core.exceptions import PermissionDenied

from apps.users.models import User
from apps.users.forms import UserRegistrationForm, UserProfileUpdateForm
from apps.users.permissions import StaffManagementAccessMixin
from apps.core.models import AuditLog


class ShopLoginView(LoginView):
    """
    Staff and Cashier Terminal Login View.
    Implements a resilient temporary cooldown policy (15 minutes) after 5 consecutive failed logins
    instead of permanent database lockout, preventing denial-of-service attacks against store staff.
    """
    template_name = 'users/login.html'
    redirect_authenticated_user = True

    MAX_FAILED_ATTEMPTS = 5
    COOLDOWN_SECONDS = 900  # 15 minutes cooldown

    def _get_lockout_cache_key(self, identifier: str) -> str:
        clean_id = identifier.lower().strip()
        return f"auth_lockout_user_{clean_id}"

    def _get_attempts_cache_key(self, identifier: str) -> str:
        clean_id = identifier.lower().strip()
        return f"auth_failed_attempts_{clean_id}"

    def post(self, request, *args, **kwargs):
        raw_identifier = request.POST.get('username', '').strip()

        if raw_identifier:
            lockout_key = self._get_lockout_cache_key(raw_identifier)
            lockout_until = cache.get(lockout_key)

            if lockout_until:
                now = timezone.now()
                if now < lockout_until:
                    remaining_seconds = int((lockout_until - now).total_seconds())
                    remaining_minutes = max(1, (remaining_seconds + 59) // 60)
                    messages.error(
                        request,
                        f"Account is temporarily locked due to {self.MAX_FAILED_ATTEMPTS} failed attempts. "
                        f"Please wait {remaining_minutes} minute(s) before trying again."
                    )
                    form = self.get_form()
                    return self.render_to_response(self.get_context_data(form=form))
                else:
                    cache.delete(lockout_key)
                    cache.delete(self._get_attempts_cache_key(raw_identifier))

        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        user = form.get_user()
        raw_identifier = self.request.POST.get('username', '').strip()

        if raw_identifier:
            cache.delete(self._get_lockout_cache_key(raw_identifier))
            cache.delete(self._get_attempts_cache_key(raw_identifier))

        if user.failed_login_attempts > 0 or user.is_locked:
            user.failed_login_attempts = 0
            user.is_locked = False
            user.save(update_fields=['failed_login_attempts', 'is_locked'])

        remember_me = self.request.POST.get('remember_me')
        if not remember_me:
            self.request.session.set_expiry(0)
        else:
            self.request.session.set_expiry(1209600)  # 14 days

        return super().form_valid(form)

    def form_invalid(self, form):
        raw_identifier = self.request.POST.get('username', '').strip()

        if raw_identifier:
            attempts_key = self._get_attempts_cache_key(raw_identifier)
            lockout_key = self._get_lockout_cache_key(raw_identifier)

            current_attempts = cache.get(attempts_key, 0) + 1
            cache.set(attempts_key, current_attempts, timeout=self.COOLDOWN_SECONDS)

            user = User.objects.filter(
                Q(username__iexact=raw_identifier) | Q(phone_number__iexact=raw_identifier)
            ).first()

            if current_attempts >= self.MAX_FAILED_ATTEMPTS:
                lockout_until = timezone.now() + timedelta(seconds=self.COOLDOWN_SECONDS)
                cache.set(lockout_key, lockout_until, timeout=self.COOLDOWN_SECONDS)

                messages.error(
                    self.request,
                    f"Too many failed login attempts. This account has been temporarily locked for 15 minutes."
                )

                AuditLog.objects.create(
                    user=user,
                    branch=getattr(user, 'assigned_branch', None) if user else None,
                    action_type='LOGIN_FAIL',
                    module='Authentication',
                    object_repr=f"15-min cooldown triggered for: {raw_identifier}",
                    ip_address=self.request.META.get('REMOTE_ADDR'),
                    details={
                        'cooldown_seconds': self.COOLDOWN_SECONDS,
                        'total_attempts': current_attempts,
                        'input_identifier': raw_identifier
                    }
                )
            else:
                remaining_attempts = self.MAX_FAILED_ATTEMPTS - current_attempts
                messages.error(
                    self.request,
                    f"Invalid username or password. Warning: {remaining_attempts} attempt(s) remaining before a 15-minute temporary lockout."
                )

                AuditLog.objects.create(
                    user=user,
                    branch=getattr(user, 'assigned_branch', None) if user else None,
                    action_type='LOGIN_FAIL',
                    module='Authentication',
                    object_repr=f"Failed login attempt for: {raw_identifier}",
                    ip_address=self.request.META.get('REMOTE_ADDR'),
                    details={
                        'attempt_number': current_attempts,
                        'remaining_attempts': remaining_attempts,
                        'input_identifier': raw_identifier
                    }
                )
        else:
            messages.error(self.request, "Invalid username or password. Please try again.")

        return super().form_invalid(form)


class ShopLogoutView(LogoutView):
    next_page = reverse_lazy('users:login')


class UserListView(StaffManagementAccessMixin, ListView):
    """
    Staff Directory List View.
    - Super Admins / Central Owners: See ALL staff across all branches.
    - Branch Managers: Strictly see ONLY staff assigned to their branch and NEVER see Super Admins / Owners.
    """
    model = User
    template_name = 'users/user_list.html'
    context_object_name = 'staff_users'
    paginate_by = 25

    def get_queryset(self):
        req_user = self.request.user
        qs = User.objects.select_related('assigned_branch')

        is_owner_or_super = req_user.is_superuser or getattr(req_user, 'role', '') == 'OWNER'

        if not is_owner_or_super:
            manager_branch = getattr(req_user, 'assigned_branch', None)
            qs = qs.filter(
                is_superuser=False
            ).exclude(
                role='OWNER'
            ).filter(
                assigned_branch=manager_branch
            )

        q = self.request.GET.get('q', '').strip()
        role = self.request.GET.get('role', '').strip()
        branch_id = self.request.GET.get('branch', '').strip()
        is_active = self.request.GET.get('is_active', '').strip()

        if q:
            qs = qs.filter(
                Q(username__icontains=q) |
                Q(first_name__icontains=q) |
                Q(last_name__icontains=q) |
                Q(phone_number__icontains=q) |
                Q(email__icontains=q)
            )
        if role:
            qs = qs.filter(role=role)
        if branch_id and is_owner_or_super:
            qs = qs.filter(assigned_branch_id=branch_id)
        if is_active in ['true', '1']:
            qs = qs.filter(is_active=True)
        elif is_active in ['false', '0']:
            qs = qs.filter(is_active=False)

        return qs.order_by('role', 'username')


class UserCreateView(StaffManagementAccessMixin, CreateView):
    model = User
    form_class = UserRegistrationForm
    template_name = 'users/user_form.html'
    success_url = reverse_lazy('users:user_list')

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['request_user'] = self.request.user
        return kwargs

    def form_valid(self, form):
        req_user = self.request.user
        if not req_user.is_superuser and getattr(req_user, 'role', '') == 'MANAGER':
            form.instance.assigned_branch = req_user.assigned_branch
            if form.instance.role == 'OWNER':
                form.instance.role = 'STAFF'

        response = super().form_valid(form)
        messages.success(self.request, f"User '{self.object.username}' registered successfully.")
        return response


class UserUpdateView(StaffManagementAccessMixin, UpdateView):
    model = User
    form_class = UserProfileUpdateForm
    template_name = 'users/user_form.html'
    success_url = reverse_lazy('users:user_list')

    def get_queryset(self):
        req_user = self.request.user
        is_owner_or_super = req_user.is_superuser or getattr(req_user, 'role', '') == 'OWNER'

        if is_owner_or_super:
            return User.objects.all()

        manager_branch = getattr(req_user, 'assigned_branch', None)
        return User.objects.filter(
            is_superuser=False
        ).exclude(
            role='OWNER'
        ).filter(
            assigned_branch=manager_branch
        )

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['request_user'] = self.request.user
        return kwargs

    def form_valid(self, form):
        req_user = self.request.user
        if not req_user.is_superuser and getattr(req_user, 'role', '') == 'MANAGER':
            form.instance.assigned_branch = req_user.assigned_branch
            if form.instance.role == 'OWNER':
                form.instance.role = 'STAFF'

        response = super().form_valid(form)
        messages.success(self.request, f"User '{self.object.username}' updated successfully.")
        return response


class ValidateManagerPinAPIView(LoginRequiredMixin, View):
    """
    AJAX endpoint validating Manager/Owner override PINs on POS terminals.
    1. Tracks and isolates failed PIN attempts per authenticated user session.
    2. Allows targeting a specific supervisor ID for precise audit responsibility tracking.
    3. Verifies candidate PINs via constant-time cryptographic salted hash comparisons.
    """

    def post(self, request, *args, **kwargs):
        user_id = request.user.id
        cache_key = f"pin_attempt_rate_user_{user_id}"
        attempts = cache.get(cache_key, 0)

        if attempts >= 5:
            return JsonResponse({
                'status': 'error',
                'message': 'Too many failed PIN attempts on your session. Please wait 1 minute before retrying.'
            }, status=429)

        pin = request.POST.get('pin', '').strip()
        manager_id = request.POST.get('manager_id') or request.POST.get('supervisor_id')

        if not pin:
            return JsonResponse({'status': 'error', 'message': 'PIN code is required.'}, status=400)

        matched_manager = None

        if manager_id:
            manager = User.objects.filter(
                id=manager_id,
                is_active=True
            ).filter(
                Q(role__in=['OWNER', 'MANAGER']) | Q(is_superuser=True)
            ).exclude(
                Q(pin_code__isnull=True) | Q(pin_code='')
            ).first()

            if manager and manager.check_pin(pin):
                matched_manager = manager
        else:
            eligible_supervisors = User.objects.filter(
                is_active=True
            ).filter(
                Q(role__in=['OWNER', 'MANAGER']) | Q(is_superuser=True)
            ).exclude(
                Q(pin_code__isnull=True) | Q(pin_code='')
            )

            for candidate in eligible_supervisors:
                if candidate.check_pin(pin):
                    matched_manager = candidate
                    break

        if matched_manager:
            cache.delete(cache_key)
            return JsonResponse({
                'status': 'success',
                'manager_id': matched_manager.id,
                'manager_username': matched_manager.username,
                'manager_name': matched_manager.get_full_name() or matched_manager.username
            })

        cache.set(cache_key, attempts + 1, timeout=60)
        return JsonResponse({
            'status': 'error',
            'message': 'Invalid Manager Override PIN. Please verify credentials.'
        }, status=403)


class UserSearchAPIView(LoginRequiredMixin, View):
    """
    Dedicated JSON search endpoint for lightweight, dynamic staff lookups across
    POS cashier selection, technician ticket assignment, sales commissions,
    and branch management forms.
    Supports filtering by query (q), role, and branch isolation.
    """

    def get(self, request, *args, **kwargs):
        q = request.GET.get('q', '').strip()
        role_filter = request.GET.get('role', '').strip()
        branch_id = request.GET.get('branch', '').strip()
        include_inactive = request.GET.get('include_inactive', '0') in ['1', 'true', 'True']

        req_user = request.user
        is_owner_or_super = req_user.is_superuser or getattr(req_user, 'role', '') == 'OWNER'

        qs = User.objects.select_related('assigned_branch')

        # 1. Active status filtering
        if not include_inactive:
            qs = qs.filter(is_active=True)

        # 2. Branch isolation scoping
        if not is_owner_or_super:
            manager_branch = getattr(req_user, 'assigned_branch', None)
            qs = qs.filter(
                assigned_branch=manager_branch,
                is_superuser=False
            ).exclude(role='OWNER')
        else:
            if branch_id:
                qs = qs.filter(assigned_branch_id=branch_id)

        # 3. Role filter (supports comma-separated list like 'STAFF,MANAGER')
        if role_filter:
            roles = [r.strip().upper() for r in role_filter.split(',') if r.strip()]
            if len(roles) == 1:
                qs = qs.filter(role=roles[0])
            elif len(roles) > 1:
                qs = qs.filter(role__in=roles)

        # 4. Search term across name, username, and mobile
        if q:
            qs = qs.filter(
                Q(username__icontains=q) |
                Q(first_name__icontains=q) |
                Q(last_name__icontains=q) |
                Q(phone_number__icontains=q)
            )

        # Cap results to top 30 to preserve UI responsiveness
        staff_records = qs.order_by('first_name', 'last_name', 'username')[:30]

        results = []
        for u in staff_records:
            full_name = u.get_full_name() or u.username
            branch_name = u.assigned_branch.name if u.assigned_branch else "All Branches / Head Office"
            results.append({
                'id': u.id,
                'text': f"{full_name} ({u.get_role_display()}) - {u.phone_number}",
                'username': u.username,
                'full_name': full_name,
                'first_name': u.first_name,
                'last_name': u.last_name,
                'role': u.role,
                'role_display': u.get_role_display(),
                'phone_number': u.phone_number,
                'email': u.email,
                'branch_id': u.assigned_branch_id,
                'branch_name': branch_name,
                'is_active': u.is_active,
                'has_pin': u.has_pin(),
            })

        return JsonResponse({
            'status': 'success',
            'count': len(results),
            'results': results
        })