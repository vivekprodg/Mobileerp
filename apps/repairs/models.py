import uuid
from decimal import Decimal
from datetime import date, timedelta
from django.db import models
from django.conf import settings
from django.utils.translation import gettext_lazy as _
from apps.core.models import TimeStampedModel
from apps.branches.models import Branch
from apps.customers.models import Customer
from apps.inventory.models import Product, ItemInstance, DeviceComponentWarranty

class RepairTicket(TimeStampedModel):
    """
    Master Service & Repair Order managing device lifecycle from intake inspection
    to diagnostic verdict, quotation approval, spare part installation, QC, and POS delivery.
    """
    SERVICE_STATUS_CHOICES = [
        ('RECEIVED', '1. Received / In-Queue (बुझिलिएको)'),
        ('DIAGNOSING', '2. Under Lab Diagnostics (जाँच हुँदै)'),
        ('QUOTATION_PENDING', '3. Awaiting Customer Approval (स्वीकृति बाँकी)'),
        ('APPROVED', '4. Customer Approved (स्वीकृत)'),
        ('IN_REPAIR', '5. In-Repair by Technician (मर्मत हुँदै)'),
        ('WAITING_PARTS', '6. Waiting for Spare Parts (सामान पर्खिंदै)'),
        ('QC_TESTING', '7. Quality & Hardware Testing (परीक्षण)'),
        ('READY_FOR_PICKUP', '8. Ready for Customer Pickup (तयार भएको)'),
        ('DELIVERED', '9. Delivered & Closed (हस्तान्तरण सम्पन्न)'),
        ('CANCELLED', '10. Cancelled / Returned Unrepaired (रद्द गरिएको)'),
    ]

    CLAIM_TYPE_CHOICES = [
        ('FREE_WARRANTY', 'Free In-Warranty Claim (नि:शुल्क वारेन्टी दावी)'),
        ('PAID_OUT_OF_WARRANTY', 'Paid Service / Out of Warranty (सशुल्क मर्मत)'),
        ('BRAND_SPECIAL_POLICY', 'Brand Special Recall / Green Line Policy (विशेष नीति)'),
        ('GOODWILL_DISCOUNT', 'Goodwill / Commercial Concession'),
    ]

    ticket_number = models.CharField(max_length=50, unique=True, db_index=True, verbose_name=_("Ticket No."))
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='repair_tickets')
    customer = models.ForeignKey(Customer, on_delete=models.SET_NULL, null=True, blank=True, related_name='repair_tickets')
    customer_name_manual = models.CharField(max_length=150, verbose_name=_("Customer Name"))
    customer_phone_manual = models.CharField(max_length=30, db_index=True, verbose_name=_("Customer Phone"))

    # Handset Linkage
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='repair_tickets', verbose_name=_("Device Model"))
    item_instance = models.ForeignKey(ItemInstance, on_delete=models.SET_NULL, null=True, blank=True, related_name='repair_history')
    imei_or_serial = models.CharField(max_length=50, db_index=True, verbose_name=_("IMEI / Serial"))
    component_warranty_record = models.ForeignKey(
        DeviceComponentWarranty, on_delete=models.SET_NULL, null=True, blank=True, related_name='active_repairs'
    )

    # Security & Access
    device_color = models.CharField(max_length=50, blank=True, null=True)
    security_pin_code = models.CharField(max_length=30, blank=True, null=True, help_text=_("Screen PIN / Password"))
    pattern_lock_sequence = models.CharField(max_length=50, blank=True, null=True, help_text=_("e.g. 1-2-3-5-9"))
    backup_warning_acknowledged = models.BooleanField(default=True, verbose_name=_("Customer Backed Up Data"))

    # Initial Complaint & Intake
    reported_fault = models.TextField(verbose_name=_("Customer Reported Problem"))
    intake_accessories_received = models.CharField(
        max_length=255, blank=True, null=True, default="Handset Only (No SIM / Memory Card)",
        help_text=_("e.g. Back cover, SIM tray, Box")
    )

    # Diagnostic & Assessment Classification
    claim_type = models.CharField(max_length=30, choices=CLAIM_TYPE_CHOICES, default='PAID_OUT_OF_WARRANTY', db_index=True)
    claimed_component = models.CharField(max_length=30, default='DEVICE', verbose_name=_("Claimed Component"))
    service_status = models.CharField(max_length=30, choices=SERVICE_STATUS_CHOICES, default='RECEIVED', db_index=True)
    
    technician = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='assigned_repair_tickets'
    )
    technician_diagnostic_findings = models.TextField(blank=True, null=True)
    qc_passed_notes = models.TextField(blank=True, null=True, help_text=_("Speaker, Mic, Charging & Screen QC checklist notes"))

    # Cost Breakdown
    estimated_cost = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Estimated Cost (NPR)"))
    labor_charge = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Labor Charge (NPR)"))
    parts_cost = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Parts Charge (NPR)"))
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    final_total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    paid_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    
    # Dates & Timestamps
    expected_delivery_date = models.DateField(blank=True, null=True)
    delivered_date = models.DateTimeField(blank=True, null=True)
    pos_invoice_reference = models.CharField(max_length=60, blank=True, null=True, help_text=_("POS Estimation Slip No. on pickup"))

    class Meta:
        db_table = 'rep_repair_tickets'
        ordering = ['-created_at']
        verbose_name = _('Repair Ticket')
        verbose_name_plural = _('Repair Tickets')

    def __str__(self):
        return f"{self.ticket_number} - {self.product.name} ({self.customer_name_manual}) [{self.service_status}]"

    def recalculate_totals(self):
        self.final_total_amount = max(Decimal('0.00'), (self.labor_charge + self.parts_cost) - self.discount_amount)

    def save(self, *args, **kwargs):
        self.recalculate_totals()
        super().save(*args, **kwargs)

class DeviceIntakeChecklist(TimeStampedModel):
    """
    Physical inspection and pre-existing condition audit recorded at counter intake
    to protect against false claims of damage during technician handling.
    """
    POWER_STATUS_CHOICES = [
        ('POWERS_ON', 'Powers On Normally (खुल्छ)'),
        ('DEAD_NO_POWER', 'Dead / No Response (खुल्दैन / डेड)'),
        ('RESTART_LOOP', 'Bootloop / Constant Restart'),
        ('LOW_BATTERY_UNTESTED', 'Low Battery / Untested'),
    ]

    LDI_STATUS_CHOICES = [
        ('WHITE_CLEAN', 'White / Clean (No Liquid Damage)'),
        ('PINK_RED_TRIGGERED', 'Pink / Red (Liquid Contact Triggered)'),
        ('MISSING_OR_TORN', 'LDI Sticker Missing / Torn'),
        ('UNTESTED_SEALED', 'Untested (Requires Disassembly)'),
    ]

    GRADE_CHOICES = [
        ('PRISTINE', 'Grade 0: Pristine / Flawless Condition'),
        ('MINOR_SCUFFS', 'Grade 1: Light Daily Scratches'),
        ('CORNER_DENT', 'Grade 2: Visible Corner Drop Dent'),
        ('BENT_BODY', 'Grade 3: Bent Frame / Deep Crack'),
    ]

    ticket = models.OneToOneField(RepairTicket, on_delete=models.CASCADE, related_name='intake_checklist')
    power_status = models.CharField(max_length=30, choices=POWER_STATUS_CHOICES, default='POWERS_ON')
    ldi_indicator_status = models.CharField(max_length=30, choices=LDI_STATUS_CHOICES, default='WHITE_CLEAN')
    body_physical_grade = models.CharField(max_length=30, choices=GRADE_CHOICES, default='MINOR_SCUFFS')

    # Specific Component Checkboxes
    is_front_glass_cracked = models.BooleanField(default=False)
    is_back_cover_glass_broken = models.BooleanField(default=False)
    is_chassis_frame_bent = models.BooleanField(default=False)
    is_camera_lens_cracked = models.BooleanField(default=False)
    has_display_lines_or_bleed = models.BooleanField(default=False, help_text=_("Green Line / Screen Bleed"))
    has_missing_screws = models.BooleanField(default=False)
    is_sim_tray_present = models.BooleanField(default=True)
    is_third_party_repaired_before = models.BooleanField(default=False, help_text=_("Previous seal broken / local glue found"))
    intake_notes = models.TextField(blank=True, null=True, help_text=_("Specific scratches or physical notes..."))

    class Meta:
        db_table = 'rep_intake_checklists'
        verbose_name = _('Intake Physical Checklist')
        verbose_name_plural = _('Intake Physical Checklists')

class DefectClassificationVerdict(TimeStampedModel):
    """
    Formal diagnostic decision determining if the repair is a genuine factory defect,
    accidental physical/liquid drop damage, or special manufacturer concession.
    """
    VERDICT_CHOICES = [
        ('GENUINE_FACTORY_DEFECT', '1. Genuine Factory / Systematic Defect (Free Warranty Claim)'),
        ('VOID_PHYSICAL_DROP_DAMAGE', '2. Warranty Void: Physical Drop Damage / Cracks (Paid)'),
        ('VOID_LIQUID_INGRESS', '3. Warranty Void: Liquid / Moisture Corrosion (Paid)'),
        ('VOID_THIRD_PARTY_TAMPERING', '4. Warranty Void: Unauthorized Seal Broken / Tampered'),
        ('BRAND_SPECIAL_RECALL', '5. Manufacturer Green-Line / Special Recall Program'),
        ('OUT_OF_WARRANTY_STANDARD', '6. Out of Warranty by Elapsed Time (Standard Paid)'),
    ]

    ticket = models.OneToOneField(RepairTicket, on_delete=models.CASCADE, related_name='defect_verdict')
    verdict = models.CharField(max_length=40, choices=VERDICT_CHOICES, default='GENUINE_FACTORY_DEFECT', db_index=True)
    diagnosed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    inspection_date = models.DateField(default=date.today)
    technical_justification = models.TextField(verbose_name=_("Technician Technical Analysis & Proof Reason"))
    rma_eligibility_certified = models.BooleanField(default=False, help_text=_("Certified for distributor reimbursement"))

    class Meta:
        db_table = 'rep_defect_verdicts'
        verbose_name = _('Defect Classification Verdict')
        verbose_name_plural = _('Defect Classification Verdicts')

class RepairDiagnosticEvidence(TimeStampedModel):
    """
    Immutable photo vault capturing microscopic motherboard burns, LDI stickers,
    and frame impact marks to resolve customer disputes and submit with vendor RMA claims.
    """
    EVIDENCE_TYPES = [
        ('LDI_STICKER', 'Liquid Indicator Sticker (LDI)'),
        ('CORNER_DENT', 'Corner Impact / Frame Dent'),
        ('GLASS_CRACK', 'Display / Glass Micro-Crack'),
        ('PCB_CORROSION', 'Motherboard Corrosion / Burnt IC'),
        ('FACTORY_SEAL', 'Tampered / Missing Factory Seal'),
        ('OTHER', 'General Diagnostic Photo'),
    ]

    ticket = models.ForeignKey(RepairTicket, on_delete=models.CASCADE, related_name='evidence_photos')
    evidence_type = models.CharField(max_length=30, choices=EVIDENCE_TYPES, default='OTHER')
    image = models.ImageField(upload_to=settings.REPAIR_EVIDENCE_UPLOAD_DIR)
    caption = models.CharField(max_length=255, blank=True, null=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)

    class Meta:
        db_table = 'rep_diagnostic_evidences'
        ordering = ['-created_at']
        verbose_name = _('Repair Diagnostic Evidence Photo')
        verbose_name_plural = _('Repair Diagnostic Evidence Photos')

class RepairReplacedPart(TimeStampedModel):
    """
    Tracks inventory deduction of installed spare parts, old vs. new serial numbers,
    and creates new sub-warranties on customer delivery.
    """
    DEFECTIVE_PART_DESTINATIONS = [
        ('QUARANTINED_FOR_RMA', 'Quarantined in Shop for Vendor RMA Claim'),
        ('RETURNED_TO_CUSTOMER', 'Handed Back to Customer (Old Part)'),
        ('SCRAPPED_LOCALLY', 'Scrapped / Recycled in Shop'),
    ]

    ticket = models.ForeignKey(RepairTicket, on_delete=models.CASCADE, related_name='replaced_parts')
    spare_part_product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='repair_usages')
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT)

    quantity = models.DecimalField(max_digits=10, decimal_places=3, default=Decimal('1.000'))
    cost_price = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'), help_text=_("Landed Cost"))
    customer_charge = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        help_text=_("Billed to Customer (0.00 if Free Warranty)")
    )
    old_part_serial_or_batch = models.CharField(max_length=80, blank=True, null=True, verbose_name=_("Old Defective Serial"))
    new_part_serial_or_batch = models.CharField(max_length=80, blank=True, null=True, verbose_name=_("New Part Serial"))

    # Sub-Warranty Tracking
    replacement_warranty_months = models.PositiveIntegerField(default=6, verbose_name=_("Sub-Warranty (Months)"))
    warranty_start_date = models.DateField(default=date.today)
    warranty_end_date = models.DateField(blank=True, null=True)
    defective_part_status = models.CharField(
        max_length=30, choices=DEFECTIVE_PART_DESTINATIONS, default='QUARANTINED_FOR_RMA', db_index=True
    )

    class Meta:
        db_table = 'rep_replaced_parts'
        verbose_name = _('Repair Replaced Part')
        verbose_name_plural = _('Repair Replaced Parts')

    def save(self, *args, **kwargs):
        if not self.warranty_end_date and self.replacement_warranty_months > 0:
            self.warranty_end_date = self.warranty_start_date + timedelta(days=self.replacement_warranty_months * 30)
        super().save(*args, **kwargs)

class CustomerQuotation(TimeStampedModel):
    """
    Quotation generated when free warranty is voided due to physical/liquid damage.
    Allows customers to review diagnostic photos and approve or reject costs.
    """
    APPROVAL_STATUS_CHOICES = [
        ('PENDING', 'Pending Customer Decision'),
        ('APPROVED', 'Approved by Customer'),
        ('REJECTED', 'Rejected / Repair Cancelled'),
    ]

    ticket = models.OneToOneField(RepairTicket, on_delete=models.CASCADE, related_name='quotation')
    quote_token = models.CharField(max_length=64, unique=True, db_index=True)
    estimated_parts_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    estimated_labor_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    total_quoted_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    approval_status = models.CharField(max_length=20, choices=APPROVAL_STATUS_CHOICES, default='PENDING', db_index=True)
    customer_response_timestamp = models.DateTimeField(blank=True, null=True)
    rejection_reason = models.CharField(max_length=255, blank=True, null=True)

    class Meta:
        db_table = 'rep_customer_quotations'
        verbose_name = _('Customer Repair Quotation')
        verbose_name_plural = _('Customer Repair Quotations')

    def save(self, *args, **kwargs):
        if not self.quote_token:
            self.quote_token = uuid.uuid4().hex
        self.total_quoted_amount = self.estimated_parts_total + self.estimated_labor_total
        super().save(*args, **kwargs)

class OpticalServiceTicket(TimeStampedModel):
    """
    Dedicated workspace for spectacle frame alignments, ultrasonic cleaning,
    screw/nose pad maintenance, and ophthalmic prescription lens fitting.
    """
    SERVICE_TYPE_CHOICES = [
        ('LENS_FITTING', 'Prescription Lens Cutting & Fitting (लेन्स फिटिङ)'),
        ('FRAME_ALIGNMENT', 'Frame Straightening & Alignment (फ्रेम मिलाउने)'),
        ('NOSEPAD_SCREW_REPLACE', 'Nose Pad & Screw Replacement'),
        ('ULTRASONIC_CLEAN', 'Ultrasonic Deep Cleaning & Polishing'),
        ('LENS_COATING_CLAIM', 'Lens Coating Warranty Claim (ARC Peeling)'),
    ]

    ticket = models.OneToOneField(RepairTicket, on_delete=models.CASCADE, related_name='optical_details')
    service_type = models.CharField(max_length=30, choices=SERVICE_TYPE_CHOICES, default='LENS_FITTING')

    # Right Eye (OD) Prescription
    right_eye_sph = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.00'), verbose_name=_("R SPH"))
    right_eye_cyl = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.00'), verbose_name=_("R CYL"))
    right_eye_axis = models.PositiveIntegerField(default=0, verbose_name=_("R AXIS"))
    right_eye_add = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.00'), verbose_name=_("R ADD"))

    # Left Eye (OS) Prescription
    left_eye_sph = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.00'), verbose_name=_("L SPH"))
    left_eye_cyl = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.00'), verbose_name=_("L CYL"))
    left_eye_axis = models.PositiveIntegerField(default=0, verbose_name=_("L AXIS"))
    left_eye_add = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.00'), verbose_name=_("L ADD"))

    pupillary_distance_pd = models.DecimalField(max_digits=5, decimal_places=1, default=Decimal('62.0'), verbose_name=_("PD (mm)"))
    lens_type_description = models.CharField(max_length=150, blank=True, null=True, help_text=_("e.g. 1.61 BlueCut ARC Anti-Glare"))
    frame_brand_and_model = models.CharField(max_length=150, blank=True, null=True)

    class Meta:
        db_table = 'rep_optical_service_tickets'
        verbose_name = _('Optical Eyewear Service Record')
        verbose_name_plural = _('Optical Eyewear Service Records')

class TechnicianCommissionLog(TimeStampedModel):
    """
    Tracks technician productivity, turnaround time (TAT), and labor commission splits.
    """
    ticket = models.ForeignKey(RepairTicket, on_delete=models.CASCADE, related_name='commission_logs')
    technician = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='repair_commissions')
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT)
    labor_amount_collected = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    commission_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('30.00'))
    commission_earned = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    turnaround_time_hours = models.DecimalField(max_digits=6, decimal_places=1, default=Decimal('0.0'))
    is_settled_in_payroll = models.BooleanField(default=False)

    class Meta:
        db_table = 'rep_technician_commissions'
        ordering = ['-created_at']
        verbose_name = _('Technician Commission Log')
        verbose_name_plural = _('Technician Commission Logs')

    def save(self, *args, **kwargs):
        self.commission_earned = (self.labor_amount_collected * (self.commission_percentage / Decimal('100.00'))).quantize(Decimal('0.01'))
        super().save(*args, **kwargs)