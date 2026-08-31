from decimal import Decimal
from django import forms
from django.utils.translation import gettext_lazy as _
from apps.repairs.models import (
    RepairTicket, DeviceIntakeChecklist, DefectClassificationVerdict,
    RepairReplacedPart, CustomerQuotation, OpticalServiceTicket
)
from apps.inventory.models import Product

class RepairIntakeForm(forms.ModelForm):
    class Meta:
        model = RepairTicket
        fields = [
            'product', 'imei_or_serial', 'customer_name_manual',
            'customer_phone_manual', 'claimed_component', 'device_color',
            'security_pin_code', 'pattern_lock_sequence',
            'intake_accessories_received', 'reported_fault',
            'expected_delivery_date', 'technician'
        ]
        widgets = {
            'product': forms.Select(attrs={'class': 'form-select select2-enable', 'id': 'id_repair_product_select'}),
            'imei_or_serial': forms.TextInput(attrs={'class': 'form-control font-monospace fw-bold', 'placeholder': 'Scan 15-Digit IMEI...'}),
            'customer_name_manual': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Full Name'}),
            'customer_phone_manual': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '98XXXXXXXX'}),
            'claimed_component': forms.Select(attrs={'class': 'form-select'}),
            'device_color': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Titanium Blue'}),
            'security_pin_code': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'PIN / Password'}),
            'pattern_lock_sequence': forms.HiddenInput(attrs={'id': 'id_pattern_lock_sequence'}),
            'intake_accessories_received': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Handset with SIM Tray'}),
            'reported_fault': forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': 'Customer complaint...'}),
            'expected_delivery_date': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
            'technician': forms.Select(attrs={'class': 'form-select'}),
        }

class DeviceIntakeChecklistForm(forms.ModelForm):
    class Meta:
        model = DeviceIntakeChecklist
        fields = [
            'power_status', 'ldi_indicator_status', 'body_physical_grade',
            'is_front_glass_cracked', 'is_back_cover_glass_broken',
            'is_chassis_frame_bent', 'is_camera_lens_cracked',
            'has_display_lines_or_bleed', 'has_missing_screws',
            'is_sim_tray_present', 'is_third_party_repaired_before', 'intake_notes'
        ]
        widgets = {
            'power_status': forms.Select(attrs={'class': 'form-select form-select-sm'}),
            'ldi_indicator_status': forms.Select(attrs={'class': 'form-select form-select-sm'}),
            'body_physical_grade': forms.Select(attrs={'class': 'form-select form-select-sm'}),
            'is_front_glass_cracked': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'is_back_cover_glass_broken': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'is_chassis_frame_bent': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'is_camera_lens_cracked': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'has_display_lines_or_bleed': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'has_missing_screws': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'is_sim_tray_present': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'is_third_party_repaired_before': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'intake_notes': forms.TextInput(attrs={'class': 'form-control form-control-sm', 'placeholder': 'Additional physical scratches...'}),
        }

class DefectClassificationVerdictForm(forms.ModelForm):
    class Meta:
        model = DefectClassificationVerdict
        fields = ['verdict', 'technical_justification', 'rma_eligibility_certified']
        widgets = {
            'verdict': forms.Select(attrs={'class': 'form-select'}),
            'technical_justification': forms.Textarea(attrs={'class': 'form-control', 'rows': 3, 'placeholder': 'Lab diagnostic explanation...'}),
            'rma_eligibility_certified': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }

class RepairSparePartInstallForm(forms.ModelForm):
    class Meta:
        model = RepairReplacedPart
        fields = [
            'spare_part_product', 'quantity', 'old_part_serial_or_batch',
            'new_part_serial_or_batch', 'customer_charge',
            'replacement_warranty_months', 'defective_part_status'
        ]
        widgets = {
            'spare_part_product': forms.Select(attrs={'class': 'form-select'}),
            'quantity': forms.NumberInput(attrs={'class': 'form-control', 'step': '1', 'value': '1'}),
            'old_part_serial_or_batch': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Old Barcode/Serial'}),
            'new_part_serial_or_batch': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'New Serial (Scanned)'}),
            'customer_charge': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'value': '0.00'}),
            'replacement_warranty_months': forms.NumberInput(attrs={'class': 'form-control', 'step': '1', 'value': '6'}),
            'defective_part_status': forms.Select(attrs={'class': 'form-select'}),
        }

class OpticalServiceTicketForm(forms.ModelForm):
    class Meta:
        model = OpticalServiceTicket
        fields = [
            'service_type', 'lens_type_description', 'frame_brand_and_model',
            'right_eye_sph', 'right_eye_cyl', 'right_eye_axis', 'right_eye_add',
            'left_eye_sph', 'left_eye_cyl', 'left_eye_axis', 'left_eye_add',
            'pupillary_distance_pd'
        ]
        widgets = {
            'service_type': forms.Select(attrs={'class': 'form-select'}),
            'lens_type_description': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. 1.56 Anti-Glare Green Coating'}),
            'frame_brand_and_model': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. RayBan Aviator RB3025'}),
            'right_eye_sph': forms.NumberInput(attrs={'class': 'form-control form-control-sm text-center', 'step': '0.25'}),
            'right_eye_cyl': forms.NumberInput(attrs={'class': 'form-control form-control-sm text-center', 'step': '0.25'}),
            'right_eye_axis': forms.NumberInput(attrs={'class': 'form-control form-control-sm text-center', 'step': '1'}),
            'right_eye_add': forms.NumberInput(attrs={'class': 'form-control form-control-sm text-center', 'step': '0.25'}),
            'left_eye_sph': forms.NumberInput(attrs={'class': 'form-control form-control-sm text-center', 'step': '0.25'}),
            'left_eye_cyl': forms.NumberInput(attrs={'class': 'form-control form-control-sm text-center', 'step': '0.25'}),
            'left_eye_axis': forms.NumberInput(attrs={'class': 'form-control form-control-sm text-center', 'step': '1'}),
            'left_eye_add': forms.NumberInput(attrs={'class': 'form-control form-control-sm text-center', 'step': '0.25'}),
            'pupillary_distance_pd': forms.NumberInput(attrs={'class': 'form-control form-control-sm text-center', 'step': '0.5'}),
        }