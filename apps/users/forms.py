import re
from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.utils.translation import gettext_lazy as _
from apps.users.models import User
from apps.branches.models import Branch


class UserRegistrationForm(UserCreationForm):
    password1 = forms.CharField(
        label=_("Staff Login Password"),
        widget=forms.PasswordInput(attrs={'class': 'form-control', 'placeholder': 'Minimum 6 characters'})
    )
    password2 = forms.CharField(
        label=_("Confirm Login Password"),
        widget=forms.PasswordInput(attrs={'class': 'form-control', 'placeholder': 'Repeat login password'})
    )
    pin_code = forms.CharField(
        required=False,
        label=_("Manager / Supervisor Quick POS PIN (4-6 Digits)"),
        widget=forms.PasswordInput(attrs={
            'class': 'form-control font-monospace',
            'placeholder': '4-6 Digit Numeric PIN',
            'maxlength': '6',
            'autocomplete': 'new-password'
        }),
        help_text=_("Mandatory for Owners & Managers to authorize POS discounts and price overrides.")
    )

    class Meta:
        model = User
        fields = [
            'username', 'first_name', 'last_name', 'email',
            'phone_number', 'role', 'assigned_branch', 'pin_code'
        ]
        widgets = {
            'username': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. kiran_pos'}),
            'first_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'First Name'}),
            'last_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Last Name'}),
            'email': forms.EmailInput(attrs={'class': 'form-control', 'placeholder': 'staff@mobileshop.np'}),
            'phone_number': forms.TextInput(attrs={'class': 'form-control font-monospace', 'placeholder': '98XXXXXXXX'}),
            'role': forms.Select(attrs={'class': 'form-select'}),
            'assigned_branch': forms.Select(attrs={'class': 'form-select'}),
        }

    def __init__(self, *args, **kwargs):
        self.request_user = kwargs.pop('request_user', None)
        super().__init__(*args, **kwargs)

        # Scoping for Branch Managers: prevent assigning OWNER role or other branches
        if self.request_user and not self.request_user.is_superuser and getattr(self.request_user, 'role', '') == 'MANAGER':
            allowed_roles = [
                ('MANAGER', 'Branch Manager (शाखा प्रबन्धक)'),
                ('STAFF', 'Cashier / Sales Staff (विक्रेता)'),
                ('ACCOUNTANT', 'Accountant (लेखापाल)'),
            ]
            self.fields['role'].choices = allowed_roles
            
            if getattr(self.request_user, 'assigned_branch', None):
                self.fields['assigned_branch'].queryset = Branch.objects.filter(
                    id=self.request_user.assigned_branch.id,
                    is_active=True
                )
                self.fields['assigned_branch'].initial = self.request_user.assigned_branch
                self.fields['assigned_branch'].empty_label = None

    def clean_username(self):
        username = self.cleaned_data.get('username', '').strip()
        if not username:
            raise forms.ValidationError(_("Username is required."))
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError(_(f"A user with username '{username}' already exists."))
        return username

    def clean_phone_number(self):
        phone = self.cleaned_data.get('phone_number', '').strip()
        clean_phone = re.sub(r'\D', '', phone)
        if not clean_phone or len(clean_phone) < 9:
            raise forms.ValidationError(_("Please enter a valid mobile phone number."))
        if User.objects.filter(phone_number__iexact=phone).exists():
            raise forms.ValidationError(_(f"A user with phone number '{phone}' already exists."))
        return phone

    def clean_pin_code(self):
        pin = self.cleaned_data.get('pin_code', '').strip()
        role = self.cleaned_data.get('role')
        if pin:
            if not pin.isdigit() or not (4 <= len(pin) <= 6):
                raise forms.ValidationError(_("PIN code must be between 4 and 6 numeric digits."))
        elif role in ['OWNER', 'MANAGER']:
            raise forms.ValidationError(_("A 4-6 digit numeric PIN is required for Owner and Manager accounts to authorize POS price overrides."))
        return pin

    def save(self, commit=True):
        user = super().save(commit=False)
        raw_pin = self.cleaned_data.get('pin_code')
        if raw_pin:
            user.set_pin(raw_pin)
        else:
            user.pin_code = None
        if commit:
            user.save()
        return user


class UserProfileUpdateForm(forms.ModelForm):
    pin_code = forms.CharField(
        required=False,
        label=_("Manager / Supervisor Quick POS PIN (4-6 Digits)"),
        widget=forms.PasswordInput(attrs={
            'class': 'form-control font-monospace',
            'placeholder': 'Leave blank to keep existing PIN',
            'maxlength': '6',
            'autocomplete': 'new-password'
        }),
        help_text=_("Enter a new 4-6 digit numeric PIN to update, or leave blank to keep unchanged.")
    )

    class Meta:
        model = User
        fields = [
            'username', 'phone_number', 'first_name', 'last_name',
            'email', 'role', 'assigned_branch', 'pin_code'
        ]
        widgets = {
            'username': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Username'}),
            'phone_number': forms.TextInput(attrs={'class': 'form-control font-monospace', 'placeholder': '98XXXXXXXX'}),
            'first_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'First Name'}),
            'last_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Last Name'}),
            'email': forms.EmailInput(attrs={'class': 'form-control', 'placeholder': 'Email Address'}),
            'role': forms.Select(attrs={'class': 'form-select'}),
            'assigned_branch': forms.Select(attrs={'class': 'form-select'}),
        }

    def __init__(self, *args, **kwargs):
        self.request_user = kwargs.pop('request_user', None)
        super().__init__(*args, **kwargs)

        # Scoping for Branch Managers: prevent promoting to OWNER or switching branch
        if self.request_user and not self.request_user.is_superuser and getattr(self.request_user, 'role', '') == 'MANAGER':
            allowed_roles = [
                ('MANAGER', 'Branch Manager (शाखा प्रबन्धक)'),
                ('STAFF', 'Cashier / Sales Staff (विक्रेता)'),
                ('ACCOUNTANT', 'Accountant (लेखापाल)'),
            ]
            self.fields['role'].choices = allowed_roles
            
            if getattr(self.request_user, 'assigned_branch', None):
                self.fields['assigned_branch'].queryset = Branch.objects.filter(
                    id=self.request_user.assigned_branch.id,
                    is_active=True
                )
                self.fields['assigned_branch'].initial = self.request_user.assigned_branch
                self.fields['assigned_branch'].empty_label = None

    def clean_username(self):
        username = self.cleaned_data.get('username', '').strip()
        if not username:
            raise forms.ValidationError(_("Username is required."))
        qs = User.objects.filter(username__iexact=username)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError(_(f"A user with username '{username}' already exists."))
        return username

    def clean_phone_number(self):
        phone = self.cleaned_data.get('phone_number', '').strip()
        if not phone:
            raise forms.ValidationError(_("Mobile number is required."))
        qs = User.objects.filter(phone_number__iexact=phone)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError(_(f"A user with phone number '{phone}' already exists."))
        return phone

    def clean_pin_code(self):
        pin = self.cleaned_data.get('pin_code', '').strip()
        role = self.cleaned_data.get('role')
        if pin:
            if not pin.isdigit() or not (4 <= len(pin) <= 6):
                raise forms.ValidationError(_("PIN code must be between 4 and 6 numeric digits."))
        elif not self.instance.has_pin() and role in ['OWNER', 'MANAGER']:
            raise forms.ValidationError(_("A 4-6 digit numeric PIN is required for Owner and Manager accounts."))
        return pin

    def save(self, commit=True):
        user = super().save(commit=False)
        raw_pin = self.cleaned_data.get('pin_code')
        if raw_pin:
            user.set_pin(raw_pin)
        if commit:
            user.save()
        return user