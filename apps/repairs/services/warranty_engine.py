from datetime import date, timedelta
from decimal import Decimal
from typing import Dict, Any, Optional
from apps.inventory.models import ItemInstance, Product, DeviceComponentWarranty, ProductComponentWarrantyRule
from apps.repairs.models import DeviceIntakeChecklist, DefectClassificationVerdict

class WarrantyEvaluationEngine:
    """
    Automated Diagnostic Engine determining whether a reported device fault
    qualifies as a Free Systematic Factory Defect, Accidental Drop Damage,
    Liquid Ingress, or Manufacturer Special Concession.
    """

    @classmethod
    def evaluate_warranty_eligibility(
        cls,
        imei_or_serial: str,
        claimed_component: str = 'DEVICE',
        checklist: Optional[DeviceIntakeChecklist] = None,
        check_date: Optional[date] = None
    ) -> Dict[str, Any]:
        target_date = check_date or date.today()
        clean_key = imei_or_serial.strip()

        instance = ItemInstance.objects.filter(
            imei_1=clean_key
        ).first() or ItemInstance.objects.filter(
            serial_number=clean_key
        ).first() or ItemInstance.objects.filter(
            imei_2=clean_key
        ).first()

        if not instance:
            return {
                'is_found': False,
                'verdict': 'DEVICE_NOT_FOUND',
                'is_eligible_free': False,
                'message': f"Identifier '{clean_key}' is not registered in shop sales records.",
            }

        product = instance.product

        # Step 1: Check Physical & Liquid Damage Flags from Checklist
        if checklist:
            if checklist.ldi_indicator_status == 'PINK_RED_TRIGGERED':
                return {
                    'is_found': True,
                    'item_instance': instance,
                    'product': product,
                    'verdict': 'VOID_LIQUID_INGRESS',
                    'is_eligible_free': False,
                    'message': "Liquid Damage Indicator (LDI) is triggered pink/red. Official warranty is void.",
                }

            if checklist.is_front_glass_cracked or checklist.is_back_cover_glass_broken or checklist.is_chassis_frame_bent or checklist.body_physical_grade in ['CORNER_DENT', 'BENT_BODY']:
                return {
                    'is_found': True,
                    'item_instance': instance,
                    'product': product,
                    'verdict': 'VOID_PHYSICAL_DROP_DAMAGE',
                    'is_eligible_free': False,
                    'message': "Physical impact cracks or frame dent detected. Official warranty is void.",
                }

            if checklist.is_third_party_repaired_before:
                return {
                    'is_found': True,
                    'item_instance': instance,
                    'product': product,
                    'verdict': 'VOID_THIRD_PARTY_TAMPERING',
                    'is_eligible_free': False,
                    'message': "Third-party tampering or broken seals detected. Warranty void.",
                }

            # Step 2: Check Manufacturer Green Line Special Policy Program
            if checklist.has_display_lines_or_bleed and not checklist.is_front_glass_cracked and checklist.body_physical_grade == 'PRISTINE':
                return {
                    'is_found': True,
                    'item_instance': instance,
                    'product': product,
                    'verdict': 'BRAND_SPECIAL_RECALL',
                    'is_eligible_free': True,
                    'message': "Uncracked AMOLED Screen qualifies for Brand Special Free Green-Line Recall Policy.",
                }

        # Step 3: Check Component-Level Active Warranty Ledger
        cw_record = instance.component_warranties.filter(component_type=claimed_component).first()
        if cw_record:
            is_valid = cw_record.is_currently_valid and (cw_record.warranty_expiry_date >= target_date)
            days_left = max(0, (cw_record.warranty_expiry_date - target_date).days)
            verdict = 'GENUINE_FACTORY_DEFECT' if is_valid else 'OUT_OF_WARRANTY_STANDARD'

            return {
                'is_found': True,
                'item_instance': instance,
                'product': product,
                'component_warranty_id': cw_record.id,
                'component_name': cw_record.component_name,
                'warranty_expiry_date': cw_record.warranty_expiry_date,
                'days_remaining': days_left,
                'verdict': verdict,
                'is_eligible_free': is_valid,
                'message': f"Component covered under warranty until {cw_record.warranty_expiry_date}" if is_valid else "Component warranty has expired.",
            }

        # Dynamic fallback calculation from product master rules
        rule = product.component_warranty_rules.filter(component_type=claimed_component).first()
        duration = rule.warranty_months if rule else (product.warranty_months or 12)
        start_date = instance.sale_date or instance.purchase_date or target_date
        exp_date = start_date + timedelta(days=duration * 30)
        is_valid = exp_date >= target_date

        return {
            'is_found': True,
            'item_instance': instance,
            'product': product,
            'component_warranty_id': None,
            'component_name': rule.component_name if rule else f"{claimed_component.title()} Component",
            'warranty_expiry_date': exp_date,
            'days_remaining': max(0, (exp_date - target_date).days),
            'verdict': 'GENUINE_FACTORY_DEFECT' if is_valid else 'OUT_OF_WARRANTY_STANDARD',
            'is_eligible_free': is_valid,
            'message': f"Standard warranty valid until {exp_date}" if is_valid else "Standard warranty expired.",
        }