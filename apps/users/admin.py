from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.forms import UserChangeForm, UserCreationForm
from django import forms
from django.utils.translation import gettext_lazy as _
from apps.users.models import User


class CustomUserChangeForm(UserChangeForm):
    pin_code = forms.CharField(
        required=False,
        label=_("Manager Override PIN (4-6 digits)"),
        widget=forms.PasswordInput(attrs={
            'placeholder': 'Leave blank to keep unchanged',
            'autocomplete': 'new-password'
        }),
        help_text=_("Raw 4-6 digit numeric PIN. Stored securely as a salted PBKDF2 cryptographic hash.")
    )

    class Meta(UserChangeForm.Meta):
        model = User

    def clean_pin_code(self):
        pin = self.cleaned_data.get('pin_code', '').strip()
        if pin:
            if not pin.isdigit() or not (4 <= len(pin) <= 6):
                raise forms.ValidationError(_("PIN code must be between 4 and 6 numeric digits."))
        return pin

    def save(self, commit=True):
        user = super().save(commit=False)
        pin = self.cleaned_data.get('pin_code')
        if pin:
            user.set_pin(pin)
        if commit:
            user.save()
        return user


class CustomUserCreationForm(UserCreationForm):
    phone_number = forms.CharField(
        required=True,
        label=_("Mobile Number (Nepal 98XXXXXXXX)"),
        widget=forms.TextInput(attrs={'placeholder': '98XXXXXXXX'})
    )
    pin_code = forms.CharField(
        required=False,
        label=_("Manager Override PIN (4-6 digits)"),
        widget=forms.PasswordInput(attrs={
            'placeholder': '4-6 digit numeric PIN',
            'autocomplete': 'new-password'
        }),
        help_text=_("Optional 4-6 digit numeric PIN. Stored securely as a salted hash.")
    )

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ('username', 'email', 'phone_number', 'role', 'assigned_branch')

    def clean_pin_code(self):
        pin = self.cleaned_data.get('pin_code', '').strip()
        if pin:
            if not pin.isdigit() or not (4 <= len(pin) <= 6):
                raise forms.ValidationError(_("PIN code must be between 4 and 6 numeric digits."))
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
    form = CustomUserChangeForm
    add_form = CustomUserCreationForm
    list_display = [
        'username', 'email', 'phone_number', 'role',
        'assigned_branch', 'has_pin_badge', 'is_active', 'is_staff'
    ]
    list_filter = ['role', 'assigned_branch', 'is_active']
    search_fields = ['username', 'first_name', 'last_name', 'email', 'phone_number']

    fieldsets = BaseUserAdmin.fieldsets + (
        ("Shop Roles & POS Security", {
            'fields': ('role', 'phone_number', 'assigned_branch', 'pin_code', 'failed_login_attempts', 'is_locked')
        }),
    )

    add_fieldsets = BaseUserAdmin.add_fieldsets + (
        ("Shop Profile", {
            'fields': ('role', 'phone_number', 'assigned_branch', 'pin_code')
        }),
    )

    @admin.display(description=_("PIN Active"), boolean=True)
    def has_pin_badge(self, obj):
        return obj.has_pin()