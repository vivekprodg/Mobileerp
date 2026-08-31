import uuid
from decimal import Decimal
from django.db import models
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _
from apps.core.models import TimeStampedModel


class Branch(TimeStampedModel):
    """
    Branch entity supporting multi-location shops (e.g., New Road, Pokhara, Lalitpur).
    Supports White-Label customization with custom Company / Business Name
    and optional Company Logo upload per store outlet.
    """
    code = models.CharField(max_length=20, unique=True, verbose_name=_("Branch Code"), db_index=True)
    name = models.CharField(max_length=150, verbose_name=_("Branch / Outlet Name (e.g. New Road Branch)"))
    name_np = models.CharField(max_length=150, blank=True, null=True, verbose_name=_("Branch Name (Nepali)"))
    is_main_branch = models.BooleanField(default=False, verbose_name=_("Is Main / Central Head Office"))

    # White-Label Custom Business Identity & Logo
    company_name = models.CharField(
        max_length=200,
        blank=True,
        null=True,
        verbose_name=_("Company / Shop Business Name (English)"),
        help_text=_("Official business name displayed on headers, receipts, and POS screens. If blank, inherits from Central Head Office or System Configuration.")
    )
    company_name_np = models.CharField(
        max_length=200,
        blank=True,
        null=True,
        verbose_name=_("Company / Shop Business Name (Nepali)"),
        help_text=_("Devanagari company name for localized customer slips (e.g. स्मार्ट मोबाइल तथा अप्टिकल हब).")
    )
    logo = models.ImageField(
        upload_to='branches/logos/%Y/%m/',
        blank=True,
        null=True,
        verbose_name=_("Company / Outlet Logo"),
        help_text=_("Optional: Upload company logo (PNG, JPG, WebP, SVG). If not uploaded, the system cleanly displays company text.")
    )

    # Location & Contact (Nepal)
    address = models.CharField(max_length=255, verbose_name=_("Location Address (e.g. Ward No. 22, New Road)"))
    city = models.CharField(max_length=100, default="Kathmandu", blank=True)
    district = models.CharField(max_length=100, default="Kathmandu", blank=True)
    province = models.CharField(
        max_length=50,
        choices=[
            ('Koshi', 'Koshi Province'),
            ('Madhesh', 'Madhesh Province'),
            ('Bagmati', 'Bagmati Province'),
            ('Gandaki', 'Gandaki Province'),
            ('Lumbini', 'Lumbini Province'),
            ('Karnali', 'Karnali Province'),
            ('Sudurpashchim', 'Sudurpashchim Province'),
        ],
        default='Bagmati'
    )
    phone_number = models.CharField(max_length=25, verbose_name=_("Branch Phone / Mobile"))
    email = models.EmailField(blank=True, null=True)

    # Billing & Printer Setup
    invoice_prefix = models.CharField(
        max_length=10, default="EST",
        help_text=_("Prefix attached to sequential estimate slips (e.g. EST-NR-000001)")
    )
    header_contact_info = models.TextField(
        blank=True,
        help_text=_("Lines printed below header on thermal slips (e.g., Phone, Ward No, Landline)")
    )
    footer_estimate_note = models.CharField(
        max_length=255,
        default="* Estimation slip only. Exchange possible within 7 days with this estimate slip. *",
        verbose_name=_("Slip Footer Note")
    )
    manager = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='managed_branches'
    )

    class Meta:
        db_table = 'branch_locations'
        ordering = ['-is_main_branch', 'name']
        verbose_name = _('Branch / Outlet')
        verbose_name_plural = _('Branches & Outlets')

    def __str__(self):
        brand_title = self.display_company_name
        return f"{brand_title} - {self.name} ({self.code})"

    def save(self, *args, **kwargs):
        if self.is_main_branch:
            # Enforce single primary main branch flag
            Branch.objects.filter(is_main_branch=True).exclude(pk=self.pk).update(is_main_branch=False)
        super().save(*args, **kwargs)

    # =========================================================================
    # WHITE-LABEL DYNAMIC BRANDING RESOLUTION PROPERTIES
    # =========================================================================

    @property
    def display_company_name(self) -> str:
        """
        Returns the resolved Company Name in English:
        1. Branch custom company_name if set.
        2. Central Main Branch company_name if set.
        3. Core SystemConfiguration company_name_en.
        4. Fallback to self.name.
        """
        if self.company_name and self.company_name.strip():
            return self.company_name.strip()

        if not self.is_main_branch:
            main_branch = Branch.objects.filter(is_main_branch=True, is_active=True).exclude(pk=self.pk).first()
            if main_branch and main_branch.company_name and main_branch.company_name.strip():
                return main_branch.company_name.strip()

        from apps.core.models import SystemConfiguration
        config = SystemConfiguration.get_solo()
        if config and config.company_name_en and config.company_name_en.strip():
            return config.company_name_en.strip()

        return self.name

    @property
    def display_company_name_np(self) -> str:
        """
        Returns the resolved Company Name in Nepali (Devanagari):
        1. Branch custom company_name_np if set.
        2. Central Main Branch company_name_np if set.
        3. Core SystemConfiguration company_name_np.
        4. Fallback to self.name_np or self.display_company_name.
        """
        if self.company_name_np and self.company_name_np.strip():
            return self.company_name_np.strip()

        if not self.is_main_branch:
            main_branch = Branch.objects.filter(is_main_branch=True, is_active=True).exclude(pk=self.pk).first()
            if main_branch and main_branch.company_name_np and main_branch.company_name_np.strip():
                return main_branch.company_name_np.strip()

        from apps.core.models import SystemConfiguration
        config = SystemConfiguration.get_solo()
        if config and config.company_name_np and config.company_name_np.strip():
            return config.company_name_np.strip()

        return self.name_np or self.display_company_name

    @property
    def logo_url(self) -> str:
        """
        Returns the valid URL of the uploaded logo image, or None if no logo exists.
        Inherits from Central Main Branch if current branch has no custom logo uploaded.
        """
        if self.logo and hasattr(self.logo, 'url'):
            try:
                return self.logo.url
            except Exception:
                pass

        if not self.is_main_branch:
            main_branch = Branch.objects.filter(is_main_branch=True, is_active=True).exclude(pk=self.pk).first()
            if main_branch and main_branch.logo and hasattr(main_branch.logo, 'url'):
                try:
                    return main_branch.logo.url
                except Exception:
                    pass

        return ""

    @property
    def has_logo(self) -> bool:
        """Returns True if a valid logo image file is available."""
        return bool(self.logo_url)

    @classmethod
    def get_default_main_branch(cls):
        """
        Self-healing class method.
        Returns the primary main branch, active branch, or automatically creates
        a default Central Branch if the database is newly initialized.
        """
        branch = cls.objects.filter(is_main_branch=True, is_active=True).first() or \
                 cls.objects.filter(is_active=True).first() or \
                 cls.objects.first()

        if not branch:
            branch = cls.objects.create(
                code="BR-MAIN-01",
                name="Main Central Branch",
                name_np="मुख्य केन्द्रीय शाखा",
                company_name="Smart Mobile & Optics Hub",
                company_name_np="स्मार्ट मोबाइल तथा अप्टिकल हब",
                is_main_branch=True,
                is_active=True,
                address="New Road, Ward No. 22",
                city="Kathmandu",
                district="Kathmandu",
                province="Bagmati",
                phone_number="01-4220000",
                email="store@mobileshop.np",
                invoice_prefix="EST-NR",
                header_contact_info="New Road, Kathmandu | Tel: 01-4220000",
                footer_estimate_note="* Estimation slip only. Exchange possible within 7 days with this estimate slip. *"
            )
        return branch


class BranchDocumentSequence(TimeStampedModel):
    """
    High-concurrency document sequence counter with database row-level locking (select_for_update).
    Prevents duplicate document numbering collisions across concurrent POS terminals,
    repair intake desks, GRN receivers, and transfer generators.
    """
    DOCUMENT_TYPE_CHOICES = [
        ('SALES_ESTIMATE', 'Sales Estimate Slip (EST)'),
        ('REPAIR_TICKET', 'Repair / Service Ticket (SRV)'),
        ('STOCK_TRANSFER', 'Stock Transfer Requisition (TRF)'),
        ('GOODS_RECEIPT', 'Goods Received Note (GRN)'),
        ('TRADE_IN_VOUCHER', 'Trade-In / Buy-Back Voucher (EXC)'),
        ('SALES_RETURN', 'Sales Return Voucher (RET)'),
        ('VENDOR_RMA', 'Vendor RMA Claim (RMA)'),
    ]

    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, related_name='document_sequences',
        verbose_name=_("Branch / Outlet")
    )
    document_type = models.CharField(
        max_length=30, choices=DOCUMENT_TYPE_CHOICES, db_index=True,
        verbose_name=_("Document Type")
    )
    prefix = models.CharField(
        max_length=20, blank=True, null=True,
        verbose_name=_("Document Prefix"),
        help_text=_("Prefix override (e.g. EST-NR, SRV-KTM). If blank, uses system defaults.")
    )
    last_number = models.PositiveBigIntegerField(
        default=0,
        verbose_name=_("Last Allocated Sequence Number")
    )
    padding_digits = models.PositiveIntegerField(
        default=6,
        verbose_name=_("Zero Padding Digits (e.g. 6 -> 000001)")
    )

    class Meta:
        db_table = 'branch_document_sequences'
        unique_together = ('branch', 'document_type')
        ordering = ['branch', 'document_type']
        verbose_name = _('Branch Document Sequence Counter')
        verbose_name_plural = _('Branch Document Sequence Counters')

    def __str__(self):
        return f"{self.branch.code} - {self.get_document_type_display()} [Last: {self.last_number}]"

    @classmethod
    def get_next_sequence_number(
        cls,
        branch: 'Branch',
        document_type: str,
        prefix_override: str = None,
        padding: int = 6
    ) -> str:
        """
        Atomically increments and retrieves the next strictly unique sequential document number
        using database row-level locking (select_for_update).
        Guarantees thread-safe isolation across concurrent checkouts and POS counters.
        """
        seq_obj, created = cls.objects.select_for_update().get_or_create(
            branch=branch,
            document_type=document_type,
            defaults={
                'prefix': prefix_override,
                'last_number': 0,
                'padding_digits': padding
            }
        )

        if prefix_override and seq_obj.prefix != prefix_override:
            seq_obj.prefix = prefix_override

        # Self-heal initial counter from existing legacy records if initializing for the first time
        if created and seq_obj.last_number == 0:
            if document_type == 'SALES_ESTIMATE':
                from apps.sales.models import SalesEstimate
                seq_obj.last_number = SalesEstimate.objects.filter(branch=branch).count()
            elif document_type == 'REPAIR_TICKET':
                from apps.repairs.models import RepairTicket
                seq_obj.last_number = RepairTicket.objects.filter(branch=branch).count()
            elif document_type == 'STOCK_TRANSFER':
                from apps.branches.models import StockTransferRequest
                seq_obj.last_number = StockTransferRequest.objects.filter(source_branch=branch).count()

        seq_obj.last_number += 1
        seq_obj.save(update_fields=['last_number', 'prefix', 'updated_at'])

        # Resolve formatting prefix
        resolved_prefix = seq_obj.prefix
        if not resolved_prefix:
            if document_type == 'SALES_ESTIMATE':
                resolved_prefix = branch.invoice_prefix or "EST"
            elif document_type == 'REPAIR_TICKET':
                resolved_prefix = f"SRV-{branch.code}"
            elif document_type == 'STOCK_TRANSFER':
                resolved_prefix = f"TRF-{branch.code}"
            elif document_type == 'GOODS_RECEIPT':
                resolved_prefix = f"GRN-{branch.code}"
            elif document_type == 'TRADE_IN_VOUCHER':
                resolved_prefix = f"EXC-{branch.code}"
            elif document_type == 'SALES_RETURN':
                resolved_prefix = f"RET-{branch.code}"
            elif document_type == 'VENDOR_RMA':
                resolved_prefix = f"RMA-{branch.code}"
            else:
                resolved_prefix = f"DOC-{branch.code}"

        pad_len = seq_obj.padding_digits or padding
        return f"{resolved_prefix}-{seq_obj.last_number:0{pad_len}d}"


class StockTransferRequest(TimeStampedModel):
    """
    Inter-branch stock transfer workflow header (Request -> Dispatched/In-Transit -> Received/Verified).
    """
    STATUS_CHOICES = [
        ('DRAFT', 'Draft / Pending Approval'),
        ('DISPATCHED', 'Dispatched / In-Transit'),
        ('RECEIVED', 'Received & Stock Updated'),
        ('CANCELLED', 'Cancelled / Rejected'),
    ]

    transfer_no = models.CharField(max_length=30, unique=True, db_index=True)
    source_branch = models.ForeignKey(
        Branch, on_delete=models.PROTECT, related_name='transfers_sent'
    )
    destination_branch = models.ForeignKey(
        Branch, on_delete=models.PROTECT, related_name='transfers_received'
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='DRAFT', db_index=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='transfers_requested'
    )
    dispatched_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='transfers_dispatched'
    )
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='transfers_received_by'
    )
    transfer_date = models.DateField(auto_now_add=True)
    dispatched_date = models.DateTimeField(blank=True, null=True)
    received_date = models.DateTimeField(blank=True, null=True)
    notes = models.TextField(blank=True, null=True)

    class Meta:
        db_table = 'branch_stock_transfers'
        ordering = ['-created_at']
        verbose_name = _('Stock Transfer Request')
        verbose_name_plural = _('Stock Transfer Requests')

    def __str__(self):
        return f"{self.transfer_no} ({self.source_branch.code} -> {self.destination_branch.code}) [{self.status}]"


class StockTransferItem(TimeStampedModel):
    """
    Itemized line items within a stock transfer shipment, linking specific products,
    quantities, and individual IMEI / Serial identifiers.
    """
    transfer_request = models.ForeignKey(
        StockTransferRequest, on_delete=models.CASCADE, related_name='items',
        verbose_name=_("Transfer Requisition")
    )
    product = models.ForeignKey(
        'inventory.Product', on_delete=models.PROTECT, related_name='transfer_items',
        verbose_name=_("Product / Handset Model")
    )
    quantity = models.DecimalField(
        max_digits=10, decimal_places=3, default=Decimal('1.000'),
        verbose_name=_("Transferred Quantity")
    )
    scanned_imei_or_serial = models.TextField(
        blank=True, null=True,
        verbose_name=_("Scanned IMEIs / Serials"),
        help_text=_("Comma or newline separated 15-digit IMEIs for smartphone models")
    )
    item_instance = models.ForeignKey(
        'inventory.ItemInstance', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='transfer_records', verbose_name=_("Linked Serialized Unit")
    )
    notes = models.CharField(
        max_length=255, blank=True, null=True,
        verbose_name=_("Line Remarks")
    )

    class Meta:
        db_table = 'branch_stock_transfer_items'
        verbose_name = _('Stock Transfer Line Item')
        verbose_name_plural = _('Stock Transfer Line Items')

    def __str__(self):
        return f"{self.transfer_request.transfer_no}: {self.product.name} x {self.quantity}"