import os
from decimal import Decimal
from PIL import Image
from django import forms
from django.forms import inlineformset_factory
from django.utils.translation import gettext_lazy as _
from apps.branches.models import Branch, StockTransferRequest, StockTransferItem


ALLOWED_LOGO_EXTENSIONS = {'jpg', 'jpeg', 'png', 'webp', 'svg'}
ALLOWED_LOGO_MIME_TYPES = {'image/jpeg', 'image/png', 'image/webp', 'image/svg+xml'}
MAX_LOGO_FILE_SIZE = 5 * 1024 * 1024  # 5 MB


class BranchForm(forms.ModelForm):
    class Meta:
        model = Branch
        fields = [
            'company_name', 'company_name_np', 'logo',
            'code', 'name', 'name_np', 'is_main_branch', 'address',
            'city', 'district', 'province', 'phone_number', 'email',
            'invoice_prefix', 'header_contact_info', 'footer_estimate_note',
            'manager', 'is_active'
        ]
        widgets = {
            'company_name': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'e.g. Prodigy Technosys Pvt. Ltd. (Your Company Name)'
            }),
            'company_name_np': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'e.g. स्मार्ट मोबाइल तथा अप्टिकल हब'
            }),
            'logo': forms.FileInput(attrs={
                'class': 'form-control',
                'accept': 'image/png,image/jpeg,image/webp,image/svg+xml'
            }),
            'code': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. BR-MAIN-01'}),
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Main Central Branch'}),
            'name_np': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. मुख्य केन्द्रीय शाखा'}),
            'address': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Ward No, Street, Market Location'}),
            'city': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Kathmandu'}),
            'district': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Kathmandu'}),
            'province': forms.Select(attrs={'class': 'form-select'}),
            'phone_number': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '01-4220000 / 98XXXXXXXX'}),
            'email': forms.EmailInput(attrs={'class': 'form-control', 'placeholder': 'store@mobileshop.np'}),
            'invoice_prefix': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'EST-NR'}),
            'header_contact_info': forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': 'Printed below header on thermal slips...'}),
            'footer_estimate_note': forms.TextInput(attrs={'class': 'form-control'}),
            'manager': forms.Select(attrs={'class': 'form-select'}),
            'is_main_branch': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }
        help_texts = {
            'company_name': _('Official branding title displayed on screen headers, login cards, and customer receipts.'),
            'logo': _('Optional: Upload your company/shop logo. If left empty, text branding is used automatically.'),
            'is_main_branch': _('Set as the Primary Central Head Office. Secondary branches inherit its logo and branding if not individually customized.')
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Ensure non-critical location fields are non-blocking
        self.fields['district'].required = False
        self.fields['city'].required = False
        self.fields['company_name'].required = False
        self.fields['company_name_np'].required = False
        self.fields['logo'].required = False

    def clean_district(self):
        val = self.cleaned_data.get('district', '').strip()
        if not val:
            city_val = self.cleaned_data.get('city', '').strip()
            return city_val if city_val else 'Kathmandu'
        return val

    def clean_logo(self):
        file_obj = self.cleaned_data.get('logo')
        if not file_obj or not hasattr(file_obj, 'size'):
            return file_obj

        if file_obj.size > MAX_LOGO_FILE_SIZE:
            raise forms.ValidationError(_("Company logo image exceeds the maximum 5MB file size limit."))

        ext = file_obj.name.split('.')[-1].lower() if '.' in file_obj.name else ''
        if ext not in ALLOWED_LOGO_EXTENSIONS:
            raise forms.ValidationError(_(f"Unsupported image format (.{ext}). Allowed formats: PNG, JPG, JPEG, WebP, SVG."))

        if ext != 'svg':
            try:
                img = Image.open(file_obj)
                img.verify()
                file_obj.seek(0)
            except Exception as e:
                raise forms.ValidationError(_("The uploaded file is corrupt or not a valid image.")) from e

        return file_obj


class BranchSwitchForm(forms.Form):
    """Allows staff/owners to toggle their active terminal context."""
    branch = forms.ModelChoiceField(
        queryset=Branch.objects.filter(is_active=True),
        empty_label=None,
        widget=forms.Select(attrs={'class': 'form-select form-select-sm', 'onchange': 'this.form.submit();'})
    )


class StockTransferRequestForm(forms.ModelForm):
    class Meta:
        model = StockTransferRequest
        fields = ['destination_branch', 'notes']
        widgets = {
            'destination_branch': forms.Select(attrs={'class': 'form-select form-select-lg', 'required': 'required'}),
            'notes': forms.Textarea(attrs={'class': 'form-control', 'rows': 3, 'placeholder': 'Reason for stock transfer, urgency, or consignment notes...'}),
        }

    def __init__(self, *args, **kwargs):
        self.source_branch = kwargs.pop('source_branch', None)
        super().__init__(*args, **kwargs)
        if self.source_branch:
            self.fields['destination_branch'].queryset = Branch.objects.filter(
                is_active=True
            ).exclude(pk=self.source_branch.pk)


class StockTransferItemForm(forms.ModelForm):
    class Meta:
        model = StockTransferItem
        fields = ['product', 'quantity', 'scanned_imei_or_serial', 'notes']
        widgets = {
            'product': forms.Select(attrs={'class': 'form-select form-select-sm select-product'}),
            'quantity': forms.NumberInput(attrs={'class': 'form-control form-control-sm text-center font-monospace', 'step': '1', 'min': '1', 'value': '1'}),
            'scanned_imei_or_serial': forms.TextInput(attrs={'class': 'form-control form-control-sm font-monospace', 'placeholder': 'Scan/Enter IMEI 1...'}),
            'notes': forms.TextInput(attrs={'class': 'form-control form-control-sm', 'placeholder': 'Optional line note'}),
        }

    def clean_quantity(self):
        qty = self.cleaned_data.get('quantity')
        if qty is None or qty <= Decimal('0.000'):
            raise forms.ValidationError(_("Transfer quantity must be greater than zero."))
        return qty


StockTransferItemFormSet = inlineformset_factory(
    StockTransferRequest,
    StockTransferItem,
    form=StockTransferItemForm,
    extra=2,
    can_delete=True
)