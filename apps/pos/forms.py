from decimal import Decimal
from django import forms
from django.utils.translation import gettext_lazy as _
from apps.pos.models import CashDrawerSession

class OpenCashDrawerForm(forms.ModelForm):
    class Meta:
        model = CashDrawerSession
        fields = ['opening_cash', 'remarks']
        widgets = {
            'opening_cash': forms.NumberInput(attrs={
                'class': 'form-control form-control-lg',
                'placeholder': '0.00',
                'step': '1.00',
                'min': '0.00'
            }),
            'remarks': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Shift notes / Counter float denominations...'
            }),
        }

class CloseCashDrawerForm(forms.ModelForm):
    class Meta:
        model = CashDrawerSession
        fields = ['actual_closing_cash', 'remarks']
        widgets = {
            'actual_closing_cash': forms.NumberInput(attrs={
                'class': 'form-control form-control-lg',
                'placeholder': 'Counted physical cash in drawer',
                'step': '1.00',
                'min': '0.00'
            }),
            'remarks': forms.Textarea(attrs={
                'class': 'form-control',
                'rows': 3,
                'placeholder': 'Shortage/Excess explanation or end of shift remarks...'
            }),
        }

    def clean_actual_closing_cash(self):
        val = self.cleaned_data.get('actual_closing_cash')
        if val is None or val < Decimal('0.00'):
            raise forms.ValidationError(_("Actual closing cash cannot be negative."))
        return val