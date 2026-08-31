import re
from decimal import Decimal
from django import forms
from django.forms import inlineformset_factory
from django.utils.translation import gettext_lazy as _
from apps.purchases.models import (
    Supplier, PurchaseOrder, PurchaseOrderItem,
    GoodsReceivedNote, GRNItem, SupplierUdhaariLedger
)
from apps.inventory.models import Product, UnitOfMeasurement


# ==============================================================================
# 1. SUPPLIER FORM
# ==============================================================================

class SupplierForm(forms.ModelForm):
    """
    Comprehensive Validator & Input Controller for Supplier Registration.
    Strictly enforces 5 core mandatory fields while cleanly organizing
    banking, DOA policies, ratings, and KYC document attachments.
    """

    class Meta:
        model = Supplier
        fields = [
            # 1. Core Identification (5 Mandatory)
            'company_name', 'contact_person', 'phone_number', 'address', 'registration_number',
            # 2. Type & Secondary Location Details
            'supplier_type', 'designation', 'alt_phone', 'email', 'pan_number', 'vat_number',
            'city', 'province', 'country',
            # 3. Business Terms & Banking
            'credit_period_days', 'credit_limit', 'opening_balance', 'balance_type',
            'preferred_payment_method', 'bank_name', 'bank_account_number', 'account_holder_name',
            'bank_branch', 'qr_payment_details',
            # 4. Catalog Coverage & Vendor Evaluation
            'product_categories_supplied', 'brands_supplied', 'is_preferred', 'rating',
            # 5. Mobile-Specific Distributor & Warranty Policies
            'is_authorized_distributor', 'distributor_tier', 'warranty_support_available',
            'brand_authorization_details', 'warranty_claim_contact', 'doa_policy', 'defective_return_policy',
            # 6. Control, KYC Documents & Remarks
            'status', 'kyc_document', 'notes'
        ]
        widgets = {
            # Core 5 Mandatory
            'company_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Apex Mobile Importers Pvt. Ltd.'}),
            'contact_person': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Ramesh Shrestha'}),
            'phone_number': forms.TextInput(attrs={'class': 'form-control font-monospace', 'placeholder': '98XXXXXXXX / 01XXXXXX'}),
            'address': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Tamrakar Complex, 3rd Floor, Pako, New Road'}),
            'registration_number': forms.TextInput(attrs={'class': 'form-control font-monospace', 'placeholder': 'e.g. 102938/078/079'}),

            # Secondary Basics
            'supplier_type': forms.Select(attrs={'class': 'form-select'}),
            'designation': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Managing Director / Sales Head'}),
            'alt_phone': forms.TextInput(attrs={'class': 'form-control font-monospace', 'placeholder': 'Optional secondary phone'}),
            'email': forms.EmailInput(attrs={'class': 'form-control', 'placeholder': 'billing@supplier.com.np'}),
            'pan_number': forms.TextInput(attrs={'class': 'form-control font-monospace', 'placeholder': '9-digit PAN (Optional)'}),
            'vat_number': forms.TextInput(attrs={'class': 'form-control font-monospace', 'placeholder': 'VAT Registration No.'}),
            'city': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Kathmandu'}),
            'province': forms.Select(attrs={'class': 'form-select'}),
            'country': forms.TextInput(attrs={'class': 'form-control'}),

            # Credit & Banking
            'credit_period_days': forms.NumberInput(attrs={'class': 'form-control', 'min': '0', 'step': '1'}),
            'credit_limit': forms.NumberInput(attrs={'class': 'form-control font-monospace', 'step': '100.00', 'min': '0.00'}),
            'opening_balance': forms.NumberInput(attrs={'class': 'form-control font-monospace', 'step': '0.01', 'min': '0.00'}),
            'balance_type': forms.Select(attrs={'class': 'form-select'}),
            'preferred_payment_method': forms.Select(attrs={'class': 'form-select'}),
            'bank_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Nabil Bank Ltd.'}),
            'bank_account_number': forms.TextInput(attrs={'class': 'form-control font-monospace', 'placeholder': 'e.g. 01201017500123'}),
            'account_holder_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Apex Mobile Importers Pvt. Ltd.'}),
            'bank_branch': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. New Road Branch, Kathmandu'}),
            'qr_payment_details': forms.Textarea(attrs={'class': 'form-control font-monospace', 'rows': 2, 'placeholder': 'FonePay Merchant ID / QR payment remarks...'}),

            # Catalog & Evaluation
            'product_categories_supplied': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Smartphones, Optical Frames, Tempered Glass, Chargers'}),
            'brands_supplied': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Samsung, Apple, Xiaomi, Vivo, Realme'}),
            'is_preferred': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'rating': forms.Select(attrs={'class': 'form-select'}),

            # Mobile Distributor & Warranty Policies
            'is_authorized_distributor': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'distributor_tier': forms.Select(attrs={'class': 'form-select'}),
            'warranty_support_available': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'brand_authorization_details': forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': 'Official authorization certificate reference and brand coverage...'}),
            'warranty_claim_contact': forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': 'Service center lab address, engineer contact numbers, and drop point...'}),
            'doa_policy': forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': '7-Day DOA unboxed dead handset replacement policy...'}),
            'defective_return_policy': forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': 'RMA policy for swollen batteries, green line display claims, and credit notes...'}),

            # Status & KYC
            'status': forms.Select(attrs={'class': 'form-select'}),
            'kyc_document': forms.FileInput(attrs={'class': 'form-control', 'accept': '.pdf,.jpg,.jpeg,.png,.webp'}),
            'notes': forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': 'Internal notes, distributor contact hierarchy, etc.'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['company_name'].required = True
        self.fields['contact_person'].required = True
        self.fields['phone_number'].required = True
        self.fields['address'].required = True
        self.fields['registration_number'].required = True

        optional_fields = [
            'supplier_type', 'designation', 'alt_phone', 'email', 'pan_number', 'vat_number',
            'city', 'province', 'country', 'credit_period_days', 'credit_limit', 'opening_balance',
            'balance_type', 'preferred_payment_method', 'bank_name', 'bank_account_number',
            'account_holder_name', 'bank_branch', 'qr_payment_details', 'product_categories_supplied',
            'brands_supplied', 'is_preferred', 'rating', 'is_authorized_distributor',
            'distributor_tier', 'warranty_support_available', 'brand_authorization_details',
            'warranty_claim_contact', 'doa_policy', 'defective_return_policy', 'status',
            'kyc_document', 'notes'
        ]
        for f_name in optional_fields:
            if f_name in self.fields:
                self.fields[f_name].required = False

        if not self.instance.pk:
            self.fields['credit_limit'].initial = Decimal('0.00')
            self.fields['opening_balance'].initial = Decimal('0.00')
            self.fields['credit_period_days'].initial = 30
            self.fields['rating'].initial = 5
            self.fields['status'].initial = 'ACTIVE'

    def clean_company_name(self):
        val = self.cleaned_data.get('company_name', '').strip()
        if not val:
            raise forms.ValidationError(_("Company / Firm Name is strictly mandatory."))
        return val

    def clean_contact_person(self):
        val = self.cleaned_data.get('contact_person', '').strip()
        if not val:
            raise forms.ValidationError(_("Contact Person name is strictly mandatory."))
        return val

    def clean_phone_number(self):
        val = self.cleaned_data.get('phone_number', '').strip()
        if not val:
            raise forms.ValidationError(_("Primary mobile / phone number is strictly mandatory."))
        
        qs = Supplier.objects.filter(phone_number__iexact=val)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError(_(f"Supplier with phone number '{val}' is already registered."))
        return val

    def clean_address(self):
        val = self.cleaned_data.get('address', '').strip()
        if not val:
            raise forms.ValidationError(_("Street Address / Location is strictly mandatory."))
        return val

    def clean_registration_number(self):
        val = self.cleaned_data.get('registration_number', '').strip()
        if not val:
            raise forms.ValidationError(_("Government / Company Registration Number is strictly mandatory."))
        return val

    def clean_credit_limit(self):
        val = self.cleaned_data.get('credit_limit')
        return val if val is not None else Decimal('0.00')

    def clean_opening_balance(self):
        val = self.cleaned_data.get('opening_balance')
        return val if val is not None else Decimal('0.00')

    def clean_credit_period_days(self):
        val = self.cleaned_data.get('credit_period_days')
        return val if val is not None else 30

    def clean_rating(self):
        val = self.cleaned_data.get('rating')
        return val if val is not None else 5

    def clean_kyc_document(self):
        file_obj = self.cleaned_data.get('kyc_document')
        if file_obj:
            allowed_exts = ['pdf', 'jpg', 'jpeg', 'png', 'webp']
            ext = file_obj.name.split('.')[-1].lower() if '.' in file_obj.name else ''
            if ext not in allowed_exts:
                raise forms.ValidationError(_(f"Unsupported file format (.{ext}). Allowed: PDF, JPG, PNG, WebP."))
            if file_obj.size > 15 * 1024 * 1024:
                raise forms.ValidationError(_("Uploaded document exceeds maximum 15MB size limit."))
        return file_obj


# ==============================================================================
# 2. PURCHASE ORDER (PO) HEADER & LINE ITEM FORMSET
# ==============================================================================

class PurchaseOrderForm(forms.ModelForm):
    class Meta:
        model = PurchaseOrder
        fields = [
            'supplier', 'order_date', 'expected_delivery_date', 'notes'
        ]
        widgets = {
            'supplier': forms.Select(attrs={'class': 'form-select select2-enable', 'required': 'required'}),
            'order_date': forms.DateInput(attrs={'class': 'form-control', 'type': 'date', 'required': 'required'}),
            'expected_delivery_date': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
            'notes': forms.Textarea(attrs={
                'class': 'form-control', 'rows': 2,
                'placeholder': 'Special delivery instructions, consignment urgency, or payment terms...'
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['supplier'].queryset = Supplier.objects.filter(is_active=True).order_by('company_name')
        if not self.instance.pk:
            import datetime
            self.fields['order_date'].initial = datetime.date.today()


class PurchaseOrderItemForm(forms.ModelForm):
    class Meta:
        model = PurchaseOrderItem
        fields = ['product', 'unit', 'ordered_quantity', 'unit_cost_price']
        widgets = {
            'product': forms.Select(attrs={'class': 'form-select form-select-sm select-po-product', 'required': 'required'}),
            'unit': forms.Select(attrs={'class': 'form-select form-select-sm select-po-unit'}),
            'ordered_quantity': forms.NumberInput(attrs={'class': 'form-control form-control-sm text-center font-monospace po-qty', 'step': '1', 'min': '1', 'value': '1', 'required': 'required'}),
            'unit_cost_price': forms.NumberInput(attrs={'class': 'form-control form-control-sm text-end font-monospace po-price', 'step': '0.01', 'min': '0.00', 'placeholder': '0.00', 'required': 'required'}),
        }

    def clean_ordered_quantity(self):
        qty = self.cleaned_data.get('ordered_quantity')
        if qty is None or qty <= Decimal('0.000'):
            raise forms.ValidationError(_("Ordered quantity must be greater than zero."))
        return qty

    def clean_unit_cost_price(self):
        price = self.cleaned_data.get('unit_cost_price')
        if price is None or price < Decimal('0.00'):
            raise forms.ValidationError(_("Cost price cannot be negative."))
        return price


PurchaseOrderItemFormSet = inlineformset_factory(
    PurchaseOrder,
    PurchaseOrderItem,
    form=PurchaseOrderItemForm,
    extra=2,
    can_delete=True
)


# ==============================================================================
# 3. GOODS RECEIVED NOTE (GRN) HEADER FORM
# ==============================================================================

class GoodsReceivedNoteForm(forms.ModelForm):
    class Meta:
        model = GoodsReceivedNote
        fields = [
            'supplier', 'purchase_order', 'supplier_bill_no', 'supplier_product_code',
            'bill_date', 'bill_date_bs', 'is_vat_bill',
            'distributor_mdms_certified', 'mdms_tax_invoice_ref',
            'extra_freight_charge', 'customs_import_charge', 'other_handling_charge',
            'paid_amount', 'warranty_provider', 'warranty_months',
            'authorized_service_center', 'remarks'
        ]
        widgets = {
            'supplier': forms.Select(attrs={'class': 'form-select select2-enable'}),
            'purchase_order': forms.Select(attrs={'class': 'form-select'}),
            'supplier_bill_no': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. INV-9908 / Chal-54'}),
            'supplier_product_code': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. BATCH-SAM-2024-Q3'}),
            'bill_date': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
            'bill_date_bs': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '2081-XX-XX'}),
            'is_vat_bill': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'distributor_mdms_certified': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'mdms_tax_invoice_ref': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. NTA-PP-2081-82/9012 or Customs PP No.'}),
            'extra_freight_charge': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'customs_import_charge': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'other_handling_charge': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'paid_amount': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'warranty_provider': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Samsung Nepal Official / IMS'}),
            'warranty_months': forms.NumberInput(attrs={'class': 'form-control', 'step': '1'}),
            'authorized_service_center': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. CTC Mall 5th Floor Service Center'}),
            'remarks': forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': 'Stock receiving remarks...'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['supplier'].required = True
        self.fields['supplier_bill_no'].required = True
        self.fields['bill_date'].required = True

        optional_fields = [
            'purchase_order', 'supplier_product_code', 'bill_date_bs', 'is_vat_bill',
            'distributor_mdms_certified', 'mdms_tax_invoice_ref',
            'extra_freight_charge', 'customs_import_charge', 'other_handling_charge',
            'paid_amount', 'warranty_provider', 'warranty_months',
            'authorized_service_center', 'remarks'
        ]
        for f in optional_fields:
            if f in self.fields:
                self.fields[f].required = False

        if not self.instance.pk:
            self.fields['warranty_months'].initial = 12
            self.fields['extra_freight_charge'].initial = Decimal('0.00')
            self.fields['customs_import_charge'].initial = Decimal('0.00')
            self.fields['other_handling_charge'].initial = Decimal('0.00')
            self.fields['paid_amount'].initial = Decimal('0.00')
            self.fields['distributor_mdms_certified'].initial = True


# ==============================================================================
# 4. GRN LINE ITEM FORM & INLINE FORMSET
# ==============================================================================

class GRNItemForm(forms.ModelForm):
    class Meta:
        model = GRNItem
        fields = [
            'product', 'supplier_item_code', 'unit_conversion',
            'purchased_quantity', 'conversion_factor', 'purchase_rate',
            'discount_percent', 'is_vat_applicable', 'vat_rate',
            'default_mdms_status', 'warranty_months', 'warranty_provider', 'scanned_imei_list'
        ]
        widgets = {
            'product': forms.Select(attrs={'class': 'form-select grn-product-select'}),
            'supplier_item_code': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Supplier SKU'}),
            'unit_conversion': forms.Select(attrs={'class': 'form-select grn-conversion-select'}),
            'purchased_quantity': forms.NumberInput(attrs={'class': 'form-control grn-qty', 'step': '0.001'}),
            'conversion_factor': forms.NumberInput(attrs={'class': 'form-control grn-factor', 'step': '0.001'}),
            'purchase_rate': forms.NumberInput(attrs={'class': 'form-control grn-rate', 'step': '0.01'}),
            'discount_percent': forms.NumberInput(attrs={'class': 'form-control grn-discount', 'step': '0.1'}),
            'is_vat_applicable': forms.CheckboxInput(attrs={'class': 'form-check-input grn-vat-check'}),
            'vat_rate': forms.NumberInput(attrs={'class': 'form-control grn-vat-rate', 'step': '0.01'}),
            'default_mdms_status': forms.Select(attrs={'class': 'form-select form-select-sm grn-mdms-select'}),
            'warranty_months': forms.NumberInput(attrs={'class': 'form-control', 'step': '1'}),
            'warranty_provider': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Distributor'}),
            'scanned_imei_list': forms.Textarea(attrs={
                'class': 'form-control font-monospace fs-xs',
                'rows': 2,
                'placeholder': 'Dual-IMEI device list (IMEI1|IMEI2 per line)'
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['product'].required = True
        self.fields['purchased_quantity'].required = True
        self.fields['purchase_rate'].required = True

        optional_fields = [
            'supplier_item_code', 'unit_conversion', 'conversion_factor',
            'discount_percent', 'is_vat_applicable', 'vat_rate',
            'default_mdms_status', 'warranty_months', 'warranty_provider', 'scanned_imei_list'
        ]
        for f in optional_fields:
            if f in self.fields:
                self.fields[f].required = False

        if not self.instance.pk:
            self.fields['conversion_factor'].initial = Decimal('1.000')
            self.fields['discount_percent'].initial = Decimal('0.00')
            self.fields['vat_rate'].initial = Decimal('0.00')
            self.fields['default_mdms_status'].initial = 'REGISTERED_OFFICIAL'
            self.fields['warranty_months'].initial = 12

    def clean_conversion_factor(self):
        val = self.cleaned_data.get('conversion_factor')
        return val if val and val > Decimal('0.000') else Decimal('1.000')

    def clean_discount_percent(self):
        val = self.cleaned_data.get('discount_percent')
        return val if val is not None else Decimal('0.00')

    def clean_vat_rate(self):
        val = self.cleaned_data.get('vat_rate')
        return val if val is not None else Decimal('0.00')

    def clean_default_mdms_status(self):
        val = self.cleaned_data.get('default_mdms_status')
        return val if val else 'REGISTERED_OFFICIAL'

    def clean_warranty_months(self):
        val = self.cleaned_data.get('warranty_months')
        return val if val is not None else 12

    def clean(self):
        cleaned_data = super().clean()
        product = cleaned_data.get('product')
        quantity = cleaned_data.get('purchased_quantity') or Decimal('0.000')
        scanned_raw = cleaned_data.get('scanned_imei_list') or ''

        if product and (product.requires_imei_tracking or product.requires_serial_tracking):
            expected_units = int(quantity)
            tokens = [t.strip() for t in re.split(r'[\n,;]+', scanned_raw) if t.strip()]

            if quantity > 0 and len(tokens) != expected_units:
                raise forms.ValidationError(
                    _(f"IMEI Count Mismatch on '{product.name}': Purchased quantity is {expected_units} unit(s), "
                      f"but {len(tokens)} device IMEI pair(s) were scanned.")
                )

            for token in tokens:
                parts = token.split('|')
                im1 = parts[0].strip() if len(parts) > 0 and parts[0].strip() else ''
                im2 = parts[1].strip() if len(parts) > 1 and parts[1].strip() else ''

                if im1 and not im1.isdigit():
                    raise forms.ValidationError(_(f"Invalid IMEI 1 '{im1}'. IMEIs must be numeric."))
                if im2 and not im2.isdigit():
                    raise forms.ValidationError(_(f"Invalid IMEI 2 '{im2}'. IMEIs must be numeric."))
                if im1 and im2 and im1 == im2:
                    raise forms.ValidationError(_(f"IMEI 1 and IMEI 2 cannot be identical ('{im1}')."))

        return cleaned_data


GRNItemFormSet = inlineformset_factory(
    GoodsReceivedNote,
    GRNItem,
    form=GRNItemForm,
    extra=1,
    can_delete=True
)


# ==============================================================================
# 5. SUPPLIER PAYMENT / PAYOUT FORM
# ==============================================================================

class SupplierPaymentForm(forms.ModelForm):
    class Meta:
        model = SupplierUdhaariLedger
        fields = ['amount', 'payment_mode', 'reference_number', 'cheque_date', 'remarks']
        widgets = {
            'amount': forms.NumberInput(attrs={'class': 'form-control font-monospace', 'step': '0.01', 'min': '0.01'}),
            'payment_mode': forms.Select(attrs={'class': 'form-select'}),
            'reference_number': forms.TextInput(attrs={'class': 'form-control font-monospace', 'placeholder': 'Cheque No. / Bank Ref'}),
            'cheque_date': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
            'remarks': forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': 'Payment notes...'}),
        }

    def clean_amount(self):
        val = self.cleaned_data.get('amount')
        if val is None or val <= Decimal('0.00'):
            raise forms.ValidationError(_("Payment amount must be greater than zero."))
        return val