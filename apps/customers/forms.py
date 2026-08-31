from decimal import Decimal
from django import forms
from django.utils.translation import gettext_lazy as _
from apps.customers.models import Customer, CustomerUdhaariLedger

class CustomerForm(forms.ModelForm):
    class Meta:
        model = Customer
        fields = [
            'name', 'phone_number', 'alt_phone_number', 'email', 'address',
            'pan_number', 'customer_type', 'credit_limit',
            'date_of_birth_bs', 'preferred_branch', 'notes', 'is_active'
        ]
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Customer or Business Name'}),
            'phone_number': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '98XXXXXXXX'}),
            'alt_phone_number': forms.TextInput(attrs={'class': 'form-control'}),
            'email': forms.EmailInput(attrs={'class': 'form-control'}),
            'address': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Putalisadak, Kathmandu'}),
            'pan_number': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '9 Digit PAN (Optional)'}),
            'customer_type': forms.Select(attrs={'class': 'form-select'}),
            'credit_limit': forms.NumberInput(attrs={'class': 'form-control', 'step': '100.00'}),
            'date_of_birth_bs': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'YYYY-MM-DD'}),
            'preferred_branch': forms.Select(attrs={'class': 'form-select'}),
            'notes': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }

class CustomerPaymentForm(forms.ModelForm):
    PAYMENT_MODES = [
        ('CASH', 'Cash (नगद)'),
        ('ESEWA', 'eSewa (ईसेवा)'),
        ('KHALTI', 'Khalti (खल्ती)'),
        ('FONEPAY', 'FonePay QR (फोनपे)'),
        ('BANK_TRANSFER', 'Bank Transfer (बैंक ट्रान्सफर)'),
        ('CARD', 'Debit/Credit Card (कार्ड)'),
    ]

    payment_mode = forms.ChoiceField(
        choices=PAYMENT_MODES,
        widget=forms.Select(attrs={'class': 'form-select'})
    )

    class Meta:
        model = CustomerUdhaariLedger
        fields = ['amount', 'payment_mode', 'reference_invoice', 'remarks']
        widgets = {
            'amount': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'min': '0.01'}),
            'reference_invoice': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Receipt / Bank Ref No.'}),
            'remarks': forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': 'Payment notes...'}),
        }

    def clean_amount(self):
        amount = self.cleaned_data.get('amount')
        if amount is None or amount <= Decimal('0.00'):
            raise forms.ValidationError(_("Payment amount must be greater than zero."))
        return amount