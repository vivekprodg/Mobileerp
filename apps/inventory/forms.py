import re
from decimal import Decimal
from django import forms
from django.forms import inlineformset_factory
from django.utils.translation import gettext_lazy as _

from apps.inventory.models import (
    Product, ProductCategory, ProductSubCategory, Brand,
    ProductComponentWarrantyRule, UnitOfMeasurement, UnitConversion,
    ItemInstance, DeviceComponentWarranty, VendorRMAClaim, VendorRMAClaimItem
)
from apps.core.models import SystemConfiguration


# ==============================================================================
# WIDGET GENERATOR HELPERS
# ==============================================================================

def make_text_input(placeholder="", css_class="form-control", **extra_attrs):
    attrs = {'class': css_class, 'placeholder': placeholder}
    attrs.update(extra_attrs)
    return forms.TextInput(attrs=attrs)


def make_number_input(step="0.01", min_val=None, max_val=None, placeholder="", css_class="form-control", **extra_attrs):
    attrs = {'class': css_class, 'step': str(step), 'placeholder': placeholder}
    if min_val is not None:
        attrs['min'] = str(min_val)
    if max_val is not None:
        attrs['max'] = str(max_val)
    attrs.update(extra_attrs)
    return forms.NumberInput(attrs=attrs)


def make_select(css_class="form-select", **extra_attrs):
    attrs = {'class': css_class}
    attrs.update(extra_attrs)
    return forms.Select(attrs=attrs)


def make_textarea(rows=2, placeholder="", css_class="form-control", **extra_attrs):
    attrs = {'class': css_class, 'rows': str(rows), 'placeholder': placeholder}
    attrs.update(extra_attrs)
    return forms.Textarea(attrs=attrs)


def make_checkbox(css_class="form-check-input", **extra_attrs):
    attrs = {'class': css_class}
    attrs.update(extra_attrs)
    return forms.CheckboxInput(attrs=attrs)


# ==============================================================================
# UNIT OF MEASUREMENT (UOM) FORM
# ==============================================================================

class UnitOfMeasurementForm(forms.ModelForm):
    class Meta:
        model = UnitOfMeasurement
        fields = ['name', 'name_np', 'code', 'allow_decimal']
        widgets = {
            'name': make_text_input(placeholder='e.g. Piece, Pair, Set, Box, Kilogram, Meter'),
            'name_np': make_text_input(placeholder='e.g. पिस, जोडी, सेट, बक्स, किलोग्राम, मिटर'),
            'code': make_text_input(placeholder='e.g. PCS, PR, SET, BOX, KG, MTR'),
            'allow_decimal': make_checkbox(),
        }
        help_texts = {
            'name': _('Primary English unit label displayed in forms and receipts.'),
            'name_np': _('Devanagari unit name for localized customer estimation slips.'),
            'code': _('Short uppercase symbol or abbreviation (e.g. PCS, BOX).'),
            'allow_decimal': _('Check this if items using this unit can be sold in fractions (e.g. 1.5 meters or 0.250 kg).'),
        }

    def clean_name(self):
        name = self.cleaned_data.get('name', '').strip()
        if not name:
            raise forms.ValidationError(_("Unit name is required."))
        qs = UnitOfMeasurement.objects.filter(name__iexact=name)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError(_(f"A unit with name '{name}' already exists."))
        return name

    def clean_code(self):
        code = self.cleaned_data.get('code', '').strip().upper()
        if not code:
            raise forms.ValidationError(_("Unit code is required."))
        qs = UnitOfMeasurement.objects.filter(code__iexact=code)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError(_(f"A unit with code '{code}' already exists."))
        return code


# ==============================================================================
# CATEGORY & SUBCATEGORY FORMS
# ==============================================================================

class ProductCategoryForm(forms.ModelForm):
    class Meta:
        model = ProductCategory
        fields = ['name', 'name_np', 'code', 'description', 'is_active']
        widgets = {
            'name': make_text_input(placeholder='e.g. Smartphones, Mobile Accessories, Optical Frames'),
            'name_np': make_text_input(placeholder='e.g. स्मार्टफोन, मोबाइल सामान'),
            'code': make_text_input(placeholder='e.g. MOB, ACC, OPT, WAT'),
            'description': make_textarea(rows=3, placeholder='Category description and scope...'),
            'is_active': make_checkbox(),
        }
        help_texts = {
            'code': _('Unique classification short code used in SKU and report generation.'),
            'name_np': _('Devanagari display name for estimation receipts.'),
            'is_active': _('Inactive categories are hidden from POS search and product creation forms.'),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.pk:
            self.fields['is_active'].initial = True


# ==============================================================================
# COMPONENT WARRANTY RULE FORMSET
# ==============================================================================

class ProductComponentWarrantyRuleForm(forms.ModelForm):
    class Meta:
        model = ProductComponentWarrantyRule
        fields = ['component_type', 'component_name', 'warranty_months', 'coverage_conditions']
        widgets = {
            'component_type': make_select(css_class='form-select form-select-sm'),
            'component_name': make_text_input(placeholder='e.g. Screen / Display Panel', css_class='form-control form-control-sm'),
            'warranty_months': make_number_input(step=1, min_val=0, css_class='form-control form-control-sm'),
            'coverage_conditions': make_text_input(placeholder='e.g. Covers manufacturing defects only. Void if cracked.', css_class='form-control form-control-sm'),
        }
        help_texts = {
            'warranty_months': _('Warranty duration in months for this specific sub-component.'),
            'coverage_conditions': _('Printed on estimation warranty certificate given to customer.'),
        }


ProductComponentWarrantyRuleFormSet = inlineformset_factory(
    Product,
    ProductComponentWarrantyRule,
    form=ProductComponentWarrantyRuleForm,
    extra=3,
    can_delete=True
)


# ==============================================================================
# MASTER PRODUCT SPECIFICATION FORM
# ==============================================================================

class ProductForm(forms.ModelForm):
    barcode = forms.CharField(
        required=False,
        label=_("Barcode Number (EAN/UPC)"),
        widget=make_text_input(placeholder='Scan physical EAN/UPC or leave blank (Optional)'),
        help_text=_("Optional: Scan the manufacturer barcode or leave completely blank.")
    )

    class Meta:
        model = Product
        fields = [
            # 1. Identification
            'name', 'sku', 'barcode', 'category', 'subcategory', 'brand',
            'model_name', 'model_number', 'is_spare_part', 'description', 'image',
            # 2. Variants & Network
            'variant_name', 'ram', 'internal_storage', 'color_variant',
            'network_type', 'sim_configuration', 'region_variant',
            'default_mdms_status',
            # 3. Hardware Specs
            'operating_system', 'processor_chipset', 'display_size', 'display_type',
            'display_resolution', 'refresh_rate', 'rear_camera', 'front_camera',
            'battery_capacity', 'fast_charging', 'fingerprint_sensor', 'face_unlock',
            'expandable_storage', 'max_memory_card', 'usb_port_type', 'headphone_jack_35mm',
            'wifi_spec', 'bluetooth_version', 'nfc_available', 'gps_capabilities',
            'size_dimension', 'base_unit',
            # 4. Pricing & Taxes
            'purchase_price', 'selling_price', 'wholesale_price', 'max_discount_percent',
            'tax_pricing_type', 'is_vat_applicable', 'vat_rate',
            # 5. Inventory & Warehouse
            'inventory_tracking_type', 'requires_imei_tracking', 'requires_serial_tracking',
            'reorder_level', 'rack_number', 'shelf_identifier', 'bin_location',
            # 6. Warranty
            'warranty_months', 'warranty_provider',
        ]
        widgets = {
            # Identification
            'name': make_text_input(placeholder='e.g. Samsung Galaxy A55 5G (8GB/256GB Awesome Navy)'),
            'sku': make_text_input(placeholder='Auto-generated or custom code (e.g. SAM-A55-256)'),
            'barcode': make_text_input(placeholder='Scan physical EAN/UPC or leave blank (Optional)'),
            'category': make_select(id='id_category_select'),
            'subcategory': make_select(),
            'brand': make_select(),
            'model_name': make_text_input(placeholder='e.g. Galaxy A55 5G'),
            'model_number': make_text_input(placeholder='e.g. SM-A556E/DS'),
            'is_spare_part': make_checkbox(),
            'image': forms.FileInput(attrs={'class': 'form-control'}),
            'description': make_textarea(rows=2, placeholder='Overview and packaging remarks...'),

            # Mobile Variants
            'variant_name': make_text_input(placeholder='e.g. 8GB/256GB - Awesome Navy'),
            'ram': make_text_input(placeholder='e.g. 8GB'),
            'internal_storage': make_text_input(placeholder='e.g. 256GB'),
            'color_variant': make_text_input(placeholder='e.g. Awesome Navy'),
            'network_type': make_select(),
            'sim_configuration': make_select(),
            'region_variant': make_text_input(placeholder='e.g. Nepal Official / Global'),
            'default_mdms_status': make_select(),

            # Hardware Specifications
            'operating_system': make_text_input(placeholder='e.g. Android 14, One UI 6.1'),
            'processor_chipset': make_text_input(placeholder='e.g. Exynos 1480 (4nm)'),
            'display_size': make_text_input(placeholder='e.g. 6.6 inches'),
            'display_type': make_text_input(placeholder='e.g. Super AMOLED, 120Hz, HDR10+'),
            'display_resolution': make_text_input(placeholder='e.g. 1080 x 2340 pixels'),
            'refresh_rate': make_text_input(placeholder='e.g. 120Hz'),
            'rear_camera': make_text_input(placeholder='e.g. 50MP (OIS) + 12MP (UW) + 5MP (Macro)'),
            'front_camera': make_text_input(placeholder='e.g. 32MP Wide'),
            'battery_capacity': make_text_input(placeholder='e.g. 5000 mAh'),
            'fast_charging': make_text_input(placeholder='e.g. 25W Wired'),
            'fingerprint_sensor': make_text_input(placeholder='e.g. Under Display, Optical'),
            'face_unlock': make_checkbox(),
            'expandable_storage': make_checkbox(),
            'max_memory_card': make_text_input(placeholder='e.g. microSDXC (uses shared SIM slot)'),
            'usb_port_type': make_text_input(placeholder='e.g. USB Type-C 2.0, OTG'),
            'headphone_jack_35mm': make_checkbox(),
            'wifi_spec': make_text_input(placeholder='e.g. Wi-Fi 802.11 a/b/g/n/ac/6'),
            'bluetooth_version': make_text_input(placeholder='e.g. 5.3, A2DP, LE'),
            'nfc_available': make_checkbox(),
            'gps_capabilities': make_text_input(placeholder='e.g. GPS, GLONASS, GALILEO, BDS'),
            'size_dimension': make_text_input(placeholder='e.g. 161.1 x 77.4 x 8.2 mm'),
            'base_unit': make_select(),

            # Pricing & Taxes
            'purchase_price': make_number_input(step="0.01", min_val=0, placeholder='0.00'),
            'selling_price': make_number_input(step="0.01", min_val=0, placeholder='0.00'),
            'wholesale_price': make_number_input(step="0.01", min_val=0, placeholder='0.00'),
            'max_discount_percent': make_number_input(step="0.5", min_val=0, max_val=100, placeholder='10.0'),
            'tax_pricing_type': make_select(),
            'vat_rate': make_number_input(step="0.01", min_val=0, max_val=100, placeholder='0.00'),
            'is_vat_applicable': make_checkbox(),

            # Inventory & Storage Racks
            'inventory_tracking_type': make_select(),
            'requires_imei_tracking': make_checkbox(id='id_requires_imei'),
            'requires_serial_tracking': make_checkbox(),
            'reorder_level': make_number_input(step="1", min_val=0, placeholder='5'),
            'rack_number': make_text_input(placeholder='e.g. Rack-M02'),
            'shelf_identifier': make_text_input(placeholder='e.g. Shelf-B'),
            'bin_location': make_text_input(placeholder='e.g. Bin-04'),

            # Warranty
            'warranty_months': make_number_input(step=1, min_val=0, placeholder='12'),
            'warranty_provider': make_text_input(placeholder='e.g. Authorized National Distributor / Brand'),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['barcode'].required = False

        if not self.instance.pk:
            config = SystemConfiguration.get_solo()
            if config.tax_system_mode == 'VAT':
                self.fields['is_vat_applicable'].initial = True
                self.fields['vat_rate'].initial = config.default_vat_rate
                self.fields['tax_pricing_type'].initial = 'INCLUSIVE'
            else:
                self.fields['is_vat_applicable'].initial = False
                self.fields['vat_rate'].initial = Decimal('0.00')
                self.fields['tax_pricing_type'].initial = 'EXEMPT'

            base_pcs = UnitOfMeasurement.objects.filter(code='PCS').first()
            if base_pcs:
                self.fields['base_unit'].initial = base_pcs

    def clean_barcode(self):
        code = self.cleaned_data.get('barcode')
        if code:
            code = str(code).strip()
            if code == '':
                return None
            qs = Product.objects.filter(barcode__iexact=code)
            if self.instance and self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise forms.ValidationError(_(f"A product with barcode '{code}' already exists."))
            return code
        return None


# ==============================================================================
# IMEI INSTANCE MDMS STATUS UPDATE FORM
# ==============================================================================

class ItemInstanceMDMSUpdateForm(forms.ModelForm):
    class Meta:
        model = ItemInstance
        fields = ['mdms_status', 'mdms_verification_date', 'mdms_remarks']
        widgets = {
            'mdms_status': make_select(),
            'mdms_verification_date': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
            'mdms_remarks': make_text_input(placeholder='e.g. Verified on NTA MDMS Official Portal'),
        }


# ==============================================================================
# VENDOR RMA & WARRANTY CLAIM FORMS
# ==============================================================================

class VendorRMAClaimForm(forms.ModelForm):
    class Meta:
        model = VendorRMAClaim
        fields = ['supplier', 'distributor_service_center', 'distributor_tracking_ref', 'resolution_notes']
        widgets = {
            'supplier': make_select(),
            'distributor_service_center': make_text_input(placeholder='e.g. Samsung Authorized Central Lab, CTC Mall'),
            'distributor_tracking_ref': make_text_input(placeholder='e.g. Courier AWB No. / Distributor Delivery Challan'),
            'resolution_notes': make_textarea(rows=3, placeholder='Dispatch notes and defect summaries...'),
        }


# ==============================================================================
# PACKAGING UNIT CONVERSION FORM
# ==============================================================================

class UnitConversionForm(forms.ModelForm):
    class Meta:
        model = UnitConversion
        fields = ['unit_name', 'conversion_factor', 'selling_price_per_unit', 'barcode']
        widgets = {
            'unit_name': make_text_input(placeholder='e.g. Box (50 Pieces), Packet (10 Pcs)'),
            'conversion_factor': make_number_input(step="0.001", min_val="0.001", placeholder='e.g. 50.000'),
            'selling_price_per_unit': make_number_input(step="0.01", min_val="0.00", placeholder='Optional custom package price'),
            'barcode': make_text_input(placeholder='Package outer barcode (Optional)'),
        }

    def clean_barcode(self):
        code = self.cleaned_data.get('barcode')
        if code:
            code = str(code).strip()
            if code == '':
                return None
            qs = UnitConversion.objects.filter(barcode__iexact=code)
            if self.instance and self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise forms.ValidationError(_(f"A package unit with barcode '{code}' already exists."))
            return code
        return None


# ==============================================================================
# STOCK ADJUSTMENT FORM
# ==============================================================================

class ManualStockAdjustmentForm(forms.Form):
    ADJUSTMENT_TYPES = [
        ('ADJUSTMENT_ADD', 'Add Stock (+) Physical Audit / Found'),
        ('ADJUSTMENT_SUB', 'Deduct Stock (-) Broken / Lost / Expired / Damaged'),
    ]

    adjustment_type = forms.ChoiceField(
        choices=ADJUSTMENT_TYPES,
        widget=make_select()
    )
    quantity = forms.DecimalField(
        min_value=Decimal('0.001'),
        widget=make_number_input(step="0.001", min_val="0.001", placeholder="Quantity to adjust")
    )
    remarks = forms.CharField(
        widget=make_textarea(rows=2, placeholder="Mandatory reason for audit trail...")
    )


# ==============================================================================
# EXCEL IMPORT PIPELINE FORMS
# ==============================================================================

class ProductExcelUploadForm(forms.Form):
    excel_file = forms.FileField(
        label=_("Select Excel / CSV File"),
        widget=forms.FileInput(attrs={'class': 'form-control form-control-lg', 'accept': '.xlsx, .xls, .csv'}),
        help_text=_("Upload spreadsheet containing catalog products (.xlsx, .xls, .csv up to 25MB).")
    )

    def clean_excel_file(self):
        file = self.cleaned_data.get('excel_file')
        if file:
            extension = file.name.split('.')[-1].lower()
            if extension not in ['xlsx', 'xls', 'csv']:
                raise forms.ValidationError(_("Unsupported file format. Please upload a valid .xlsx, .xls, or .csv file."))
            if file.size > 25 * 1024 * 1024:
                raise forms.ValidationError(_("File size exceeds the 25MB maximum upload limit."))
        return file


class ProductExcelMappingForm(forms.Form):
    CONFLICT_CHOICES = [
        ('MERGE', 'Consolidate & Add Stock (अनुशंसा गरिएको - मौज्दात जोड्ने)'),
        ('OVERWRITE', 'Update & Overwrite Existing Details (नयाँ दर र मौज्दात अपडेट)'),
        ('SKIP', 'Skip Existing Products (पहिले दर्ता भएको छोड्ने)'),
        ('NEW_VARIANT', 'Generate New SKU Variants (नयाँ भेरियन्ट बनाउने)'),
    ]

    conflict_strategy = forms.ChoiceField(
        choices=CONFLICT_CHOICES,
        initial='MERGE',
        widget=forms.RadioSelect(attrs={'class': 'form-check-input'})
    )