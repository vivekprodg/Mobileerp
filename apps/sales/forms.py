from decimal import Decimal
from PIL import Image

from django import forms
from django.utils.translation import gettext_lazy as _
from apps.sales.models import (
    SalesEstimate, SalesReturn,
    PhoneExchangeTradeIn, TradeInInspectionChecklist, TradeInLegalUndertaking
)
from apps.customers.models import Customer
from apps.inventory.models import Product


# ==============================================================================
# KYC DOCUMENT FILE VALIDATION UTILITY
# ==============================================================================

KYC_ALLOWED_IMAGE_EXTENSIONS = {'jpg', 'jpeg', 'png', 'webp'}
KYC_ALLOWED_MIME_TYPES = {'image/jpeg', 'image/png', 'image/webp'}
KYC_MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB per document


def validate_kyc_document_image(file_obj, field_label="Uploaded document"):
    """
    Validates customer KYC uploads (Citizenship Card, Live Photo):
    1. Blocks files larger than 10MB.
    2. Enforces allowed image extensions (.jpg, .jpeg, .png, .webp).
    3. Blocks non-image MIME types and executable scripts.
    4. Verifies image integrity using Pillow.
    """
    if not file_obj:
        return file_obj

    if file_obj.size > KYC_MAX_FILE_SIZE:
        raise forms.ValidationError(
            _(f"{field_label} exceeds the 10MB maximum file size limit.")
        )

    ext = file_obj.name.split('.')[-1].lower() if '.' in file_obj.name else ''
    if ext not in KYC_ALLOWED_IMAGE_EXTENSIONS:
        raise forms.ValidationError(
            _(f"{field_label} must be a valid image file (.jpg, .jpeg, .png, .webp).")
        )

    if hasattr(file_obj, 'content_type') and file_obj.content_type.lower() not in KYC_ALLOWED_MIME_TYPES:
        raise forms.ValidationError(
            _(f"{field_label} format is invalid. Please upload a genuine JPG, PNG, or WebP image.")
        )

    try:
        img = Image.open(file_obj)
        img.verify()
        file_obj.seek(0)
    except Exception as e:
        raise forms.ValidationError(
            _(f"{field_label} is corrupt or not a valid image file.")
        ) from e

    return file_obj


# ==============================================================================
# TRADE-IN & POLICE KYC UNDERTAKING FORMS
# ==============================================================================

class TradeInDeviceIntakeForm(forms.ModelForm):
    class Meta:
        model = PhoneExchangeTradeIn
        fields = [
            'brand_name', 'model_name', 'ram_capacity', 'storage_capacity',
            'color_variant', 'imei_1', 'imei_2', 'serial_number',
            'mdms_status', 'market_base_value', 'customer_name_manual',
            'customer_phone_manual', 'evaluation_notes'
        ]
        widgets = {
            'brand_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Apple / Samsung / Xiaomi'}),
            'model_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. iPhone 13 / Galaxy S22'}),
            'ram_capacity': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. 6GB'}),
            'storage_capacity': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. 128GB'}),
            'color_variant': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Midnight Black'}),
            'imei_1': forms.TextInput(attrs={'class': 'form-control font-monospace fw-bold', 'placeholder': 'Primary 15-Digit IMEI'}),
            'imei_2': forms.TextInput(attrs={'class': 'form-control font-monospace', 'placeholder': 'Secondary IMEI (Optional)'}),
            'serial_number': forms.TextInput(attrs={'class': 'form-control font-monospace', 'placeholder': 'Serial No.'}),
            'mdms_status': forms.Select(attrs={'class': 'form-select'}),
            'market_base_value': forms.NumberInput(attrs={'class': 'form-control form-control-lg fw-bold text-primary', 'step': '100.00', 'placeholder': 'Pristine Market Price'}),
            'customer_name_manual': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Customer Full Name'}),
            'customer_phone_manual': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '98XXXXXXXX'}),
            'evaluation_notes': forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': 'Inspector diagnostic observations...'}),
        }


class TradeIn10PointChecklistForm(forms.ModelForm):
    class Meta:
        model = TradeInInspectionChecklist
        fields = [
            'touch_and_display', 'front_and_back_cameras', 'charging_and_battery',
            'wifi_bluetooth_gps', 'cellular_calling_mic_speaker', 'biometrics_security',
            'body_frame_condition', 'liquid_ingress_ldi', 'original_accessories_available',
            'account_lock_factory_reset', 'technician_remarks'
        ]
        widgets = {
            'touch_and_display': forms.Select(attrs={'class': 'form-select tradein-check-select'}),
            'front_and_back_cameras': forms.Select(attrs={'class': 'form-select tradein-check-select'}),
            'charging_and_battery': forms.Select(attrs={'class': 'form-select tradein-check-select'}),
            'wifi_bluetooth_gps': forms.Select(attrs={'class': 'form-select tradein-check-select'}),
            'cellular_calling_mic_speaker': forms.Select(attrs={'class': 'form-select tradein-check-select'}),
            'biometrics_security': forms.Select(attrs={'class': 'form-select tradein-check-select'}),
            'body_frame_condition': forms.Select(attrs={'class': 'form-select tradein-check-select'}),
            'liquid_ingress_ldi': forms.Select(attrs={'class': 'form-select tradein-check-select'}),
            'original_accessories_available': forms.Select(attrs={'class': 'form-select tradein-check-select'}),
            'account_lock_factory_reset': forms.Select(attrs={'class': 'form-select tradein-check-select'}),
            'technician_remarks': forms.TextInput(attrs={'class': 'form-control form-control-sm', 'placeholder': 'Specific hardware notes...'}),
        }


class TradeInLegalUndertakingForm(forms.ModelForm):
    class Meta:
        model = TradeInLegalUndertaking
        fields = [
            'customer_full_name', 'customer_father_or_spouse_name',
            'id_type', 'id_number', 'id_issued_district', 'id_issued_date_bs',
            'permanent_address', 'current_address',
            'id_front_image', 'id_back_image', 'customer_live_photo',
            'customer_digital_signature', 'declaration_accepted'
        ]
        widgets = {
            'customer_full_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Customer Name (as in Citizenship Card)'}),
            'customer_father_or_spouse_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': "Father's / Spouse's Full Name"}),
            'id_type': forms.Select(attrs={'class': 'form-select'}),
            'id_number': forms.TextInput(attrs={'class': 'form-control font-monospace', 'placeholder': 'Citizenship / NID Number'}),
            'id_issued_district': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Kathmandu, Kaski'}),
            'id_issued_date_bs': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'YYYY-MM-DD (BS)'}),
            'permanent_address': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'District, Municipality, Ward No.'}),
            'current_address': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Temporary / Room Address'}),
            'id_front_image': forms.FileInput(attrs={'class': 'form-control', 'accept': 'image/jpeg,image/png,image/webp'}),
            'id_back_image': forms.FileInput(attrs={'class': 'form-control', 'accept': 'image/jpeg,image/png,image/webp'}),
            'customer_live_photo': forms.FileInput(attrs={'class': 'form-control', 'accept': 'image/jpeg,image/png,image/webp'}),
            'customer_digital_signature': forms.HiddenInput(attrs={'id': 'id_signature_data'}),
            'declaration_accepted': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }

    def clean_id_front_image(self):
        file_obj = self.cleaned_data.get('id_front_image')
        return validate_kyc_document_image(file_obj, _("Citizenship / NID Front Photo"))

    def clean_id_back_image(self):
        file_obj = self.cleaned_data.get('id_back_image')
        return validate_kyc_document_image(file_obj, _("Citizenship / NID Back Photo"))

    def clean_customer_live_photo(self):
        file_obj = self.cleaned_data.get('customer_live_photo')
        return validate_kyc_document_image(file_obj, _("Customer Live Snapshot"))


# ==============================================================================
# SALES RETURN, HOLD & OVERRIDE FORMS
# ==============================================================================

class SalesReturnProcessForm(forms.Form):
    reason = forms.CharField(
        widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': 'Reason for return...'}),
        required=True
    )
    technician_notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': 'Technician diagnostic findings...'})
    )
    refund_mode = forms.ChoiceField(
        choices=[
            ('CASH', 'Cash Refund (नगद फिर्ता)'),
            ('STORE_CREDIT', 'Adjust in Customer Udhaari / Store Credit'),
            ('EXCHANGE_ADJUST', 'Adjusted in New Device Exchange'),
        ],
        widget=forms.Select(attrs={'class': 'form-select'})
    )


class SalesBillHoldForm(forms.ModelForm):
    class Meta:
        model = SalesEstimate
        fields = ['customer', 'customer_name_manual', 'customer_phone_manual', 'notes']
        widgets = {
            'customer': forms.Select(attrs={'class': 'form-select'}),
            'customer_name_manual': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Walk-in Name'}),
            'customer_phone_manual': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '98XXXXXXXX'}),
            'notes': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
        }


class ManagerDiscountOverrideForm(forms.Form):
    manager_pin = forms.CharField(
        max_length=6,
        widget=forms.PasswordInput(attrs={'class': 'form-control', 'placeholder': 'Enter 4-6 digit Manager PIN'})
    )
    discount_percent = forms.DecimalField(
        max_digits=5, decimal_places=2,
        widget=forms.NumberInput(attrs={'class': 'form-control', 'step': '0.5'})
    )