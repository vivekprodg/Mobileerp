from datetime import date, timedelta
from decimal import Decimal
from typing import Dict, Any, Optional

from apps.inventory.models import (
    ItemInstance, Product, DeviceComponentWarranty, ProductComponentWarrantyRule
)
from apps.repairs.models import DeviceIntakeChecklist, DefectClassificationVerdict

class WarrantyEvaluationEngine:
    """
    Automated Diagnostic Engine determining whether a reported device fault
    qualifies as a Free Systematic Factory Defect, Accidental Drop Damage,
    Liquid Ingress, or Manufacturer Special Concession.

    Enforces Nepal Retail Mobile Industry Component Standards:
    - Step 1 (Physical/Water Check): Cracks or red LDI immediately void warranty (Customer must pay).
    - Step 2 (Screen Claim): Verified against 3-Month screen window (90 days).
    - Step 3 (Battery Claim): Verified against 6-Month battery window (180 days).
    - Step 4 (Motherboard / Body Claim): Verified against 12-Month device window (365 days).
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
        clean_key = (imei_or_serial or '').strip()
        claimed_comp_norm = (claimed_component or 'DEVICE').upper().strip()

        # ---------------------------------------------------------------------
        # 0. RESOLVE ITEM INSTANCE IN SYSTEM RECORDS
        # ---------------------------------------------------------------------
        instance = (
            ItemInstance.objects.filter(imei_1=clean_key).first() or
            ItemInstance.objects.filter(imei_2=clean_key).first() or
            ItemInstance.objects.filter(serial_number=clean_key).first() or
            ItemInstance.objects.filter(device_uid=clean_key).first()
        )

        if not instance:
            return {
                'is_found': False,
                'verdict': 'DEVICE_NOT_FOUND',
                'is_eligible_free': False,
                'message': f"Device identifier '{clean_key}' was not found in registered shop sales records.",
            }

        product = instance.product

        # ---------------------------------------------------------------------
        # STEP 1: PHYSICAL, LIQUID & TAMPERING VERIFICATION (CUSTOMER PAYS IF VOID)
        # ---------------------------------------------------------------------
        if checklist:
            # 1A. Liquid Contact Indicator Check
            if checklist.ldi_indicator_status == 'PINK_RED_TRIGGERED':
                return {
                    'is_found': True,
                    'item_instance': instance,
                    'product': product,
                    'claimed_component': claimed_comp_norm,
                    'verdict': 'VOID_LIQUID_INGRESS',
                    'is_eligible_free': False,
                    'message': "Liquid Damage Indicator (LDI) is triggered pink/red. Official warranty is void; repair must be billed as Paid.",
                }

            # 1B. Physical Drop / Cracks / Frame Dent Check
            has_physical_damage = (
                checklist.is_front_glass_cracked or
                checklist.is_back_cover_glass_broken or
                checklist.is_chassis_frame_bent or
                checklist.is_camera_lens_cracked or
                checklist.body_physical_grade in ['CORNER_DENT', 'BENT_BODY']
            )

            if has_physical_damage:
                return {
                    'is_found': True,
                    'item_instance': instance,
                    'product': product,
                    'claimed_component': claimed_comp_norm,
                    'verdict': 'VOID_PHYSICAL_DROP_DAMAGE',
                    'is_eligible_free': False,
                    'message': "Physical impact cracks, glass breakage, or frame dents detected. Official warranty is void; repair must be billed as Paid.",
                }

            # 1C. Third-Party Repair / Tampered Seal Check
            if checklist.is_third_party_repaired_before:
                return {
                    'is_found': True,
                    'item_instance': instance,
                    'product': product,
                    'claimed_component': claimed_comp_norm,
                    'verdict': 'VOID_THIRD_PARTY_TAMPERING',
                    'is_eligible_free': False,
                    'message': "Third-party tampering, broken seals, or unauthorized local glue detected. Warranty is void.",
                }

            # 1D. Brand Special Green-Line Recall Concession (Pristine AMOLED Screens)
            if (
                claimed_comp_norm == 'SCREEN' and
                checklist.has_display_lines_or_bleed and
                not checklist.is_front_glass_cracked and
                checklist.body_physical_grade == 'PRISTINE'
            ):
                return {
                    'is_found': True,
                    'item_instance': instance,
                    'product': product,
                    'claimed_component': claimed_comp_norm,
                    'component_name': "Screen / Display Panel (AMOLED)",
                    'verdict': 'BRAND_SPECIAL_RECALL',
                    'is_eligible_free': True,
                    'message': "Uncracked AMOLED display qualifies for Brand Special Free Green-Line Recall Program.",
                }

        # ---------------------------------------------------------------------
        # STEPS 2, 3, 4: COMPONENT-LEVEL WARRANTY WINDOW EVALUATION
        # - Step 2 (Screen Claim): Exactly 3-Month window (90 days).
        # - Step 3 (Battery Claim): Exactly 6-Month window (180 days).
        # - Step 4 (Motherboard / Body Claim): Exactly 12-Month window (365 days).
        # ---------------------------------------------------------------------
        cw_record = instance.component_warranties.filter(component_type=claimed_comp_norm).first()

        if cw_record:
            is_valid = cw_record.is_currently_valid and (cw_record.warranty_expiry_date >= target_date)
            days_left = max(0, (cw_record.warranty_expiry_date - target_date).days)
            verdict = 'GENUINE_FACTORY_DEFECT' if is_valid else 'OUT_OF_WARRANTY_STANDARD'

            if is_valid:
                msg = f"{cw_record.component_name} is covered under active warranty until {cw_record.warranty_expiry_date} ({days_left} day(s) remaining)."
            else:
                msg = f"{cw_record.component_name} warranty expired on {cw_record.warranty_expiry_date}. Standard paid repair charges apply."

            return {
                'is_found': True,
                'item_instance': instance,
                'product': product,
                'claimed_component': claimed_comp_norm,
                'component_warranty_id': cw_record.id,
                'component_name': cw_record.component_name,
                'warranty_months': cw_record.warranty_months,
                'warranty_expiry_date': cw_record.warranty_expiry_date,
                'days_remaining': days_left,
                'verdict': verdict,
                'is_eligible_free': is_valid,
                'message': msg,
            }

        # ---------------------------------------------------------------------
        # DYNAMIC FALLBACK CALCULATION (FOR OLDER RECORDS WITHOUT WARRANTY CARDS)
        # Defaults accurately: Screen -> 3M (90d), Battery -> 6M (180d), Device -> 12M (365d)
        # Rather than assuming 12 months for everything.
        # ---------------------------------------------------------------------
        rule = product.component_warranty_rules.filter(component_type=claimed_comp_norm).first()

        if rule:
            duration_months = rule.warranty_months
            comp_name = rule.component_name
        else:
            if claimed_comp_norm == 'SCREEN':
                duration_months = 3
                comp_name = "Screen / Display Panel"
            elif claimed_comp_norm == 'BATTERY':
                duration_months = 6
                comp_name = "Internal Battery"
            elif claimed_comp_norm == 'DEVICE':
                duration_months = product.warranty_months or 12
                comp_name = "Main Handset Body & Motherboard"
            else:
                duration_months = product.warranty_months or 12
                comp_name = f"{claimed_component.title()} Component"

        # Exact calendar days calculation from start date
        start_date = instance.sale_date or instance.purchase_date or target_date

        if duration_months == 3 or claimed_comp_norm == 'SCREEN':
            duration_days = 90 if duration_months == 3 else duration_months * 30
        elif duration_months == 6 or claimed_comp_norm == 'BATTERY':
            duration_days = 180 if duration_months == 6 else duration_months * 30
        elif duration_months == 12 or claimed_comp_norm == 'DEVICE':
            duration_days = 365 if duration_months == 12 else duration_months * 30
        elif duration_months > 0:
            duration_days = duration_months * 30
        else:
            duration_days = 0

        exp_date = start_date + timedelta(days=duration_days)
        is_valid = (exp_date >= target_date) and (duration_months > 0)
        days_left = max(0, (exp_date - target_date).days)
        verdict = 'GENUINE_FACTORY_DEFECT' if is_valid else 'OUT_OF_WARRANTY_STANDARD'

        if is_valid:
            msg = f"{comp_name} is covered under {duration_months}-Month standard warranty until {exp_date} ({days_left} day(s) remaining)."
        else:
            msg = f"{comp_name} {duration_months}-Month warranty expired on {exp_date}. Standard paid repair charges apply."

        return {
            'is_found': True,
            'item_instance': instance,
            'product': product,
            'claimed_component': claimed_comp_norm,
            'component_warranty_id': None,
            'component_name': comp_name,
            'warranty_months': duration_months,
            'warranty_expiry_date': exp_date,
            'days_remaining': days_left,
            'verdict': verdict,
            'is_eligible_free': is_valid,
            'message': msg,
        }