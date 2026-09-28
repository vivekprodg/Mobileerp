from decimal import Decimal
from django import forms
from django.utils.translation import gettext_lazy as _

from apps.inventory.models import Product, UnitOfMeasurement
from apps.products.models import ProductPriceTier, BarcodeLabelTemplate
from apps.core.models import SystemConfiguration

class ProductQuickCreateForm(forms.ModelForm):
    """
    Front-counter quick registration form.
    - purchase_price is optional and cleanly defaults to 0.00 if left blank.
    - Barcode is completely optional without automatic generation.
    - Multi-component warranty support: Handset Body, Battery & Screen with explicit conditions.
    - Intelligent fallbacks: Defaults to 12M Body / 6M Battery / 3M Screen for smartphones,
      or 0M across the board for non-serialized consumable accessories.
    - Base unit falls back gracefully to standard PCS if unselected.
    - Tax pricing mode and VAT rate cleanly adapt to system configuration.
    """
    purchase_price = forms.DecimalField(
        required=False,
        initial=Decimal('0.00'),
        min_value=Decimal('0.00'),
        widget=forms.NumberInput(attrs={
            'class': 'form-control',
            'step': '0.01',
            'min': '0.00',
            'placeholder': '0.00'
        })
    )
    barcode = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'Scan physical barcode or leave blank (Optional)'
        })
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

    # -------------------------------------------------------------------------
    # MULTI-COMPONENT WARRANTY EXTENSION FIELDS
    # -------------------------------------------------------------------------
    battery_warranty_months = forms.IntegerField(
        required=False,
        min_value=0,
        initial=6,
        widget=forms.NumberInput(attrs={
            'class': 'form-control',
            'step': '1',
            'min': '0',
            'placeholder': '6'
        }),
        help_text=_("Internal battery replacement warranty duration in months (defaults to 6M).")
    )
    screen_warranty_months = forms.IntegerField(
        required=False,
        min_value=0,
        initial=3,
        widget=forms.NumberInput(attrs={
            'class': 'form-control',
            'step': '1',
            'min': '0',
            'placeholder': '3'
        }),
        help_text=_("Touch digitizer and screen panel factory defect warranty duration in months (defaults to 3M).")
    )
    warranty_conditions = forms.CharField(
        required=False,
        initial="Covers genuine manufacturing defects only. Void if physical drop cracks, glass breakage, or liquid/water ingress found.",
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'Coverage conditions and damage exclusions...'
        }),
        help_text=_("Physical and liquid damage exclusion terms printed on receipts and warranty cards.")
    )

    class Meta:
        model = Product
        fields = [
            'name', 'sku', 'barcode', 'category', 'brand',
            'model_name', 'model_number', 'variant_name', 'ram', 'internal_storage',
            'color_variant', 'network_type', 'base_unit',
            'purchase_price', 'selling_price', 'wholesale_price',
            'tax_pricing_type', 'requires_imei_tracking', 'is_vat_applicable', 'vat_rate',
            'warranty_months', 'battery_warranty_months', 'screen_warranty_months',
            'warranty_conditions', 'rack_number'
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
            'purchase_price': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'placeholder': '0.00'}),
            'selling_price': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'wholesale_price': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'warranty_months': forms.NumberInput(attrs={'class': 'form-control', 'step': '1', 'min': '0'}),
            'rack_number': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Rack-M01'}),
            'requires_imei_tracking': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        config = SystemConfiguration.get_solo()
        is_vat_shop = (config.tax_system_mode == 'VAT')

        # Make non-essential quick-add fields explicitly optional
        self.fields['purchase_price'].required = False
        self.fields['barcode'].required = False
        self.fields['tax_pricing_type'].required = False
        self.fields['is_vat_applicable'].required = False
        self.fields['vat_rate'].required = False
        self.fields['base_unit'].required = False
        self.fields['warranty_months'].required = False
        self.fields['battery_warranty_months'].required = False
        self.fields['screen_warranty_months'].required = False
        self.fields['warranty_conditions'].required = False

        if not self.instance.pk:
            self.fields['purchase_price'].initial = Decimal('0.00')

            if is_vat_shop:
                self.fields['is_vat_applicable'].initial = True
                self.fields['vat_rate'].initial = config.default_vat_rate
                self.fields['tax_pricing_type'].initial = 'INCLUSIVE'
            else:
                self.fields['is_vat_applicable'].initial = False
                self.fields['vat_rate'].initial = Decimal('0.00')
                self.fields['tax_pricing_type'].initial = 'EXEMPT'

            default_unit = UnitOfMeasurement.objects.filter(code='PCS').first()
            if default_unit:
                self.fields['base_unit'].initial = default_unit

            self.fields['warranty_months'].initial = 12
            self.fields['battery_warranty_months'].initial = 6
            self.fields['screen_warranty_months'].initial = 3

    def clean_purchase_price(self):
        """Clean purchase_price so an empty input defaults safely to 0.00."""
        val = self.cleaned_data.get('purchase_price')
        if val is None or str(val).strip() == '':
            return Decimal('0.00')
        return val

    def clean_warranty_months(self):
        """Fallback safely: defaults to 12 for phones or 0 for accessories."""
        val = self.cleaned_data.get('warranty_months')
        if val is None or str(val).strip() == '':
            requires_imei = self.cleaned_data.get('requires_imei_tracking', False)
            return 12 if requires_imei else 0
        try:
            return max(0, int(val))
        except (ValueError, TypeError):
            return 12

    def clean_battery_warranty_months(self):
        """Fallback safely: defaults to 6 for phones or 0 for accessories."""
        val = self.cleaned_data.get('battery_warranty_months')
        if val is None or str(val).strip() == '':
            requires_imei = self.cleaned_data.get('requires_imei_tracking', False)
            return 6 if requires_imei else 0
        try:
            return max(0, int(val))
        except (ValueError, TypeError):
            return 6

    def clean_screen_warranty_months(self):
        """Fallback safely: defaults to 3 for phones or 0 for accessories."""
        val = self.cleaned_data.get('screen_warranty_months')
        if val is None or str(val).strip() == '':
            requires_imei = self.cleaned_data.get('requires_imei_tracking', False)
            return 3 if requires_imei else 0
        try:
            return max(0, int(val))
        except (ValueError, TypeError):
            return 3

    def clean_warranty_conditions(self):
        conditions = self.cleaned_data.get('warranty_conditions', '').strip()
        if not conditions:
            requires_imei = self.cleaned_data.get('requires_imei_tracking', False)
            if requires_imei:
                return "Covers genuine manufacturing defects only. Void if physical drop cracks, glass breakage, or liquid/water ingress found."
            return "No warranty for consumable accessory."
        return conditions

    def clean_base_unit(self):
        """Fallback safely: resolve to PCS (Piece) if omitted."""
        unit = self.cleaned_data.get('base_unit')
        if not unit:
            unit = UnitOfMeasurement.objects.filter(code='PCS').first()
            if not unit:
                unit = UnitOfMeasurement.objects.create(
                    name='Piece',
                    name_np='पिस',
                    code='PCS',
                    allow_decimal=False
                )
        return unit

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

        if not cleaned_data.get('purchase_price'):
            cleaned_data['purchase_price'] = Decimal('0.00')

        if not cleaned_data.get('tax_pricing_type'):
            cleaned_data['tax_pricing_type'] = 'INCLUSIVE' if is_vat_shop else 'EXEMPT'

        if cleaned_data.get('is_vat_applicable') is None:
            cleaned_data['is_vat_applicable'] = is_vat_shop

        if cleaned_data.get('vat_rate') is None:
            cleaned_data['vat_rate'] = config.default_vat_rate if is_vat_shop else Decimal('0.00')

        if not cleaned_data.get('base_unit'):
            default_unit = UnitOfMeasurement.objects.filter(code='PCS').first()
            if not default_unit:
                default_unit = UnitOfMeasurement.objects.create(
                    name='Piece',
                    name_np='पिस',
                    code='PCS',
                    allow_decimal=False
                )
            cleaned_data['base_unit'] = default_unit

        is_phone = cleaned_data.get('requires_imei_tracking', False)
        if cleaned_data.get('warranty_months') is None:
            cleaned_data['warranty_months'] = 12 if is_phone else 0
        if cleaned_data.get('battery_warranty_months') is None:
            cleaned_data['battery_warranty_months'] = 6 if is_phone else 0
        if cleaned_data.get('screen_warranty_months') is None:
            cleaned_data['screen_warranty_months'] = 3 if is_phone else 0

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
        queryset=Product.objects.none(),
        widget=forms.Select(attrs={
            'class': 'form-select form-select-lg',
            'id': 'productSelectDropdown',
            'data-ajax--url': '/products/api/search/?mode=simple',
            'data-placeholder': _('Search product by name, SKU, or barcode...'),
            'onchange': 'if (typeof updateLivePreview === "function") { updateLivePreview(); }'
        }),
        help_text=_("Search and select product dynamically by Name, SKU, Model, or physical Barcode.")
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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        selected_product_id = None
        if self.is_bound:
            selected_product_id = self.data.get('product')
        elif self.initial.get('product'):
            val = self.initial.get('product')
            selected_product_id = getattr(val, 'id', val)
        elif self.initial.get('product_id'):
            selected_product_id = self.initial.get('product_id')

        if selected_product_id:
            try:
                self.fields['product'].queryset = Product.objects.filter(id=selected_product_id)
            except (ValueError, TypeError):
                self.fields['product'].queryset = Product.objects.none()
        else:
            self.fields['product'].queryset = Product.objects.none()

    def clean_product(self):
        product = self.cleaned_data.get('product')
        if not product:
            raw_id = self.data.get('product')
            if raw_id:
                product = Product.objects.filter(id=raw_id).first()
            if not product:
                raise forms.ValidationError(_("Please select a valid product."))
        return product