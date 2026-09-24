from django import forms
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.forms import UserChangeForm, UserCreationForm
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from apps.users.models import User

class CustomUserChangeForm(UserChangeForm):
    """
    Staff edit form in Django Admin.
    Handles salted PBKDF2 hashing of POS Manager Override PINs and allows
    clearing configured PINs without overwriting existing hashes.
    """
    pin_code = forms.CharField(
        required=False,
        label=_("Manager Override PIN (4-6 digits)"),
        widget=forms.PasswordInput(attrs={
            'placeholder': 'Leave blank to keep unchanged',
            'autocomplete': 'new-password'
        }),
        help_text=_("Enter 4-6 numeric digits. Automatically stored as a secure PBKDF2 cryptographic hash.")
    )
    clear_pin = forms.BooleanField(
        required=False,
        label=_("Clear Existing PIN"),
        help_text=_("Check this box to remove the current override PIN from this account.")
    )

    class Meta(UserChangeForm.Meta):
        model = User

    def clean_pin_code(self):
        pin = self.cleaned_data.get('pin_code', '').strip()
        if pin:
            if not pin.isdigit() or not (4 <= len(pin) <= 6):
                raise forms.ValidationError(_("Manager Override PIN must be between 4 and 6 numeric digits."))
        return pin

    def save(self, commit=True):
        user = super().save(commit=False)
        clear_pin = self.cleaned_data.get('clear_pin')
        pin = self.cleaned_data.get('pin_code')

        if clear_pin:
            user.pin_code = None
        elif pin:
            user.set_pin(pin)

        if commit:
            user.save()
        return user

class CustomUserCreationForm(UserCreationForm):
    """
    Staff user onboarding form in Django Admin.
    Enforces phone number capture, role assignment, branch locking,
    and optional override PIN configuration.
    """
    phone_number = forms.CharField(
        required=True,
        label=_("Mobile Number (Nepal 98XXXXXXXX)"),
        widget=forms.TextInput(attrs={'placeholder': '98XXXXXXXX'})
    )
    pin_code = forms.CharField(
        required=False,
        label=_("Manager Override PIN (4-6 digits)"),
        widget=forms.PasswordInput(attrs={
            'placeholder': 'Optional 4-6 digit numeric PIN',
            'autocomplete': 'new-password'
        }),
        help_text=_("Optional 4-6 digit PIN used to authorize manager overrides on POS terminals.")
    )

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ('username', 'first_name', 'last_name', 'email', 'phone_number', 'role', 'assigned_branch')

    def clean_phone_number(self):
        phone = self.cleaned_data.get('phone_number', '').strip()
        if not phone:
            raise forms.ValidationError(_("Primary mobile number is required."))
        if User.objects.filter(phone_number=phone).exists():
            raise forms.ValidationError(_("A user with this mobile number already exists."))
        return phone

    def clean_pin_code(self):
        pin = self.cleaned_data.get('pin_code', '').strip()
        if pin:
            if not pin.isdigit() or not (4 <= len(pin) <= 6):
                raise forms.ValidationError(_("Manager Override PIN must be between 4 and 6 numeric digits."))
        return pin

    def save(self, commit=True):
        user = super().save(commit=False)
        pin = self.cleaned_data.get('pin_code')
        if pin:
            user.set_pin(pin)
        if commit:
            user.save()
        return user

@admin.register(User)
class UserAdmin(BaseUserAdmin):
    """
    Staff and Admin User Manager.
    Equipped with compound index support, POS PIN management,
    instant account unlock actions, and branch access controls.
    """
    form = CustomUserChangeForm
    add_form = CustomUserCreationForm

    list_display = [
        'username',
        'full_name_display',
        'phone_number',
        'role_badge',
        'assigned_branch',
        'has_pin_badge',
        'is_locked_badge',
        'is_active',
        'is_staff',
    ]
    list_filter = ['role', 'assigned_branch', 'is_locked', 'is_active', 'is_staff', 'is_superuser']
    search_fields = ['username', 'first_name', 'last_name', 'email', 'phone_number']
    ordering = ['username']

    fieldsets = (
        (None, {'fields': ('username', 'password')}),
        (_("Personal Information"), {'fields': (('first_name', 'last_name'), 'email')}),
        (_("Shop Roles & Branch Scoping"), {
            'fields': (
                'role',
                'phone_number',
                'assigned_branch',
            )
        }),
        (_("POS Terminal Security & PIN Override"), {
            'fields': (
                'pin_code',
                'clear_pin',
                ('failed_login_attempts', 'is_locked'),
            )
        }),
        (_("Permissions & System Access"), {
            'fields': (
                ('is_active', 'is_staff', 'is_superuser'),
                'groups',
                'user_permissions',
            )
        }),
        (_("Activity Timestamps"), {
            'fields': ('last_login', 'date_joined'),
        }),
    )

    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': (
                'username',
                ('first_name', 'last_name'),
                'phone_number',
                'email',
                'role',
                'assigned_branch',
                'pin_code',
                'password1',
                'password2',
            )
        }),
    )

    actions = ['unlock_accounts', 'reset_failed_attempts']

    @admin.display(description=_("Full Name"))
    def full_name_display(self, obj):
        return obj.get_full_name() or '-'

    @admin.display(description=_("System Role"))
    def role_badge(self, obj):
        colors = {
            'OWNER': '#7c3aed',       # Violet / Royal
            'MANAGER': '#2563eb',     # Blue
            'STAFF': '#0d9488',       # Teal
            'ACCOUNTANT': '#d97706',  # Amber
        }
        color = colors.get(obj.role, '#64748b')
        return format_html(
            '<span style="color: #ffffff; background-color: {}; padding: 2px 9px; '
            'border-radius: 999px; font-weight: 600; font-size: 11px;">{}</span>',
            color,
            obj.get_role_display()
        )

    @admin.display(description=_("PIN Active"), boolean=True)
    def has_pin_badge(self, obj):
        return obj.has_pin()

    @admin.display(description=_("Account Status"))
    def is_locked_badge(self, obj):
        if obj.is_locked:
            return format_html(
                '<span style="color: #ffffff; background-color: #dc2626; padding: 2px 7px; '
                'border-radius: 4px; font-weight: 700; font-size: 10px;">LOCKED</span>'
            )
        return format_html(
            '<span style="color: #15803d; font-weight: 600; font-size: 11px;">Active</span>'
        )

    @admin.action(description=_("Unlock selected accounts and reset failed attempts"))
    def unlock_accounts(self, request, queryset):
        updated_count = queryset.update(is_locked=False, failed_login_attempts=0)
        self.message_user(request, f"Successfully unlocked {updated_count} user account(s).")

    @admin.action(description=_("Reset failed login counters for selected accounts"))
    def reset_failed_attempts(self, request, queryset):
        updated_count = queryset.update(failed_login_attempts=0)
        self.message_user(request, f"Successfully reset login attempt counters for {updated_count} account(s).")