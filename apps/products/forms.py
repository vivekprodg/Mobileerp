from decimal import Decimal
from django import forms
from django.utils.translation import gettext_lazy as _

from apps.inventory.models import Product
from apps.products.models import ProductPriceTier, BarcodeLabelTemplate
from apps.core.models import SystemConfiguration


class ProductQuickCreateForm(forms.ModelForm):
    """
    Front-counter quick registration form.
    Barcode is completely optional without automatic generation.
    """
    barcode = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Scan physical barcode or leave blank (Optional)'})
    )
    initial_stock = forms.DecimalField(
        required=False,
        initial=Decimal('0.000'),
        min_value=Decimal('0.000'),
        widget=forms.NumberInput(attrs={'class': 'form-control', 'placeholder': '0.00'})
    )

    tax_pricing_type = forms.ChoiceField(
        choices=Product.TAX_PRICING_TYPE_CHOICES,
        required=False,
        widget=forms.Select(attrs={'class': 'form-select'})
    )
    is_vat_applicable = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'})
    )
    vat_rate = forms.DecimalField(
        required=False,
        initial=Decimal('0.00'),
        min_value=Decimal('0.00'),
        max_value=Decimal('100.00'),
        widget=forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'})
    )

    class Meta:
        model = Product
        fields = [
            'name', 'sku', 'barcode', 'category', 'brand',
            'model_name', 'model_number', 'variant_name', 'ram', 'internal_storage',
            'color_variant', 'network_type', 'base_unit',
            'purchase_price', 'selling_price', 'wholesale_price',
            'tax_pricing_type', 'requires_imei_tracking', 'is_vat_applicable', 'vat_rate',
            'warranty_months', 'rack_number'
        ]
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Samsung Galaxy A55 5G'}),
            'sku': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Auto or Custom'}),
            'barcode': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Scan physical barcode or leave blank (Optional)'}),
            'category': forms.Select(attrs={'class': 'form-select'}),
            'brand': forms.Select(attrs={'class': 'form-select'}),
            'model_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Galaxy A55'}),
            'model_number': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'SM-A556E/DS'}),
            'variant_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '8GB/256GB Awesome Navy'}),
            'ram': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '8GB'}),
            'internal_storage': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '256GB'}),
            'color_variant': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Awesome Navy'}),
            'network_type': forms.Select(attrs={'class': 'form-select'}),
            'base_unit': forms.Select(attrs={'class': 'form-select'}),
            'purchase_price': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'selling_price': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'wholesale_price': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'warranty_months': forms.NumberInput(attrs={'class': 'form-control', 'step': '1'}),
            'rack_number': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Rack-M01'}),
            'requires_imei_tracking': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        config = SystemConfiguration.get_solo()
        is_vat_shop = (config.tax_system_mode == 'VAT')

        self.fields['barcode'].required = False
        self.fields['tax_pricing_type'].required = False
        self.fields['is_vat_applicable'].required = False
        self.fields['vat_rate'].required = False

        if not self.instance.pk:
            if is_vat_shop:
                self.fields['is_vat_applicable'].initial = True
                self.fields['vat_rate'].initial = config.default_vat_rate
                self.fields['tax_pricing_type'].initial = 'INCLUSIVE'
            else:
                self.fields['is_vat_applicable'].initial = False
                self.fields['vat_rate'].initial = Decimal('0.00')
                self.fields['tax_pricing_type'].initial = 'EXEMPT'

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

    def clean_tax_pricing_type(self):
        val = self.cleaned_data.get('tax_pricing_type')
        if not val or str(val).strip() == '':
            config = SystemConfiguration.get_solo()
            return 'INCLUSIVE' if config.tax_system_mode == 'VAT' else 'EXEMPT'
        return val

    def clean_vat_rate(self):
        val = self.cleaned_data.get('vat_rate')
        if val is None:
            config = SystemConfiguration.get_solo()
            return config.default_vat_rate if config.tax_system_mode == 'VAT' else Decimal('0.00')
        return val

    def clean(self):
        cleaned_data = super().clean()
        config = SystemConfiguration.get_solo()
        is_vat_shop = (config.tax_system_mode == 'VAT')

        if not cleaned_data.get('tax_pricing_type'):
            cleaned_data['tax_pricing_type'] = 'INCLUSIVE' if is_vat_shop else 'EXEMPT'

        if cleaned_data.get('is_vat_applicable') is None:
            cleaned_data['is_vat_applicable'] = is_vat_shop

        if cleaned_data.get('vat_rate') is None:
            cleaned_data['vat_rate'] = config.default_vat_rate if is_vat_shop else Decimal('0.00')

        return cleaned_data


class ProductPriceTierForm(forms.ModelForm):
    class Meta:
        model = ProductPriceTier
        fields = ['tier_type', 'min_quantity', 'price_per_unit']
        widgets = {
            'tier_type': forms.Select(attrs={'class': 'form-select'}),
            'min_quantity': forms.NumberInput(attrs={'class': 'form-control', 'step': '1'}),
            'price_per_unit': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
        }


class BarcodePrintBatchForm(forms.Form):
    product = forms.ModelChoiceField(
        queryset=Product.objects.filter(is_active=True).select_related('category', 'brand'),
        widget=forms.Select(attrs={
            'class': 'form-select form-select-lg',
            'id': 'productSelectDropdown',
            'onchange': 'updateLivePreview()'
        })
    )
    quantity = forms.IntegerField(
        min_value=1,
        max_value=500,
        initial=10,
        widget=forms.NumberInput(attrs={
            'class': 'form-control form-control-lg text-center fw-bold',
            'id': 'quantityInput',
            'placeholder': 'e.g. 24'
        })
    )
    template = forms.ModelChoiceField(
        queryset=BarcodeLabelTemplate.objects.all(),
        required=False,
        empty_label="Standard Mobile Sticker (50mm x 25mm)",
        widget=forms.Select(attrs={
            'class': 'form-select form-select-lg',
            'id': 'templateSelectDropdown'
        })
    )