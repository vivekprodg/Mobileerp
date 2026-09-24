import json
import re
import uuid
from decimal import Decimal, ROUND_HALF_UP
from PIL import Image

from django.shortcuts import render, redirect, get_object_or_404
from django.views.generic import DetailView, TemplateView, View
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib import messages
from django.http import JsonResponse
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.repairs.models import (
    RepairTicket, DeviceIntakeChecklist, DefectClassificationVerdict,
    RepairDiagnosticEvidence, RepairReplacedPart, CustomerQuotation,
    OpticalServiceTicket
)
from apps.repairs.forms import (
    RepairIntakeForm, DeviceIntakeChecklistForm, DefectClassificationVerdictForm,
    RepairSparePartInstallForm, OpticalServiceTicketForm
)
from apps.repairs.services import (
    RepairTicketService, RepairPartsService, RepairNotificationService,
    WarrantyEvaluationEngine, VendorRMAService
)
from apps.inventory.models import (
    Product, ProductCategory, Brand, UnitOfMeasurement, BranchStock, StockMovementLog
)
from apps.branches.models import Branch, BranchDocumentSequence
from apps.core.models import SystemConfiguration, AuditLog
from apps.users.models import User
from apps.customers.models import Customer

# ==============================================================================
# IMAGE UPLOAD VALIDATION UTILITY
# ==============================================================================
ALLOWED_IMAGE_EXTENSIONS = {'jpg', 'jpeg', 'png', 'webp'}
ALLOWED_MIME_TYPES = {'image/jpeg', 'image/png', 'image/webp'}
MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 MB limit

def validate_uploaded_image(file_obj):
    """
    Strictly verifies uploaded diagnostic evidence files:
    1. Enforces a maximum file size cap (10MB).
    2. Validates filename extension against approved formats.
    3. Validates MIME content-type.
    4. Performs Pillow image verification to block malicious scripts disguised as images.
    """
    if file_obj.size > MAX_UPLOAD_SIZE:
        raise ValidationError(f"File '{file_obj.name}' exceeds the 10MB maximum size limit.")

    ext = file_obj.name.split('.')[-1].lower() if '.' in file_obj.name else ''
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        raise ValidationError(f"File '{file_obj.name}' has an unsupported extension (.{ext}). Allowed formats: JPG, PNG, WebP.")

    if hasattr(file_obj, 'content_type') and file_obj.content_type.lower() not in ALLOWED_MIME_TYPES:
        raise ValidationError(f"File '{file_obj.name}' is not a valid image format.")

    try:
        img = Image.open(file_obj)
        img.verify()
        file_obj.seek(0)
    except Exception as e:
        raise ValidationError(f"File '{file_obj.name}' is corrupt or not a valid image.") from e

# ==============================================================================
# WORKSHOP DASHBOARD & INTAKE VIEWS
# ==============================================================================
class RepairDashboardView(LoginRequiredMixin, TemplateView):
    """Workshop dashboard displaying live repair queues and KPI metrics."""
    template_name = 'repairs/repair_dashboard.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        branch = getattr(self.request, 'active_branch', None) or Branch.get_default_main_branch()

        qs = RepairTicket.objects.select_related('product', 'technician', 'branch')
        if branch and not self.request.user.is_superuser:
            qs = qs.filter(branch=branch)

        search_query = self.request.GET.get('q', '').strip()
        status_filter = self.request.GET.get('status', '').strip()
        claim_filter = self.request.GET.get('claim_type', '').strip()

        if search_query:
            qs = qs.filter(
                Q(ticket_number__icontains=search_query) |
                Q(imei_or_serial__icontains=search_query) |
                Q(customer_name_manual__icontains=search_query) |
                Q(customer_phone_manual__icontains=search_query) |
                Q(product__name__icontains=search_query)
            )

        if status_filter:
            qs = qs.filter(service_status=status_filter)
        if claim_filter:
            qs = qs.filter(claim_type=claim_filter)

        context.update({
            'tickets': qs.order_by('-created_at')[:100],
            'kpi_active_queue': qs.exclude(service_status__in=['DELIVERED', 'CANCELLED']).count(),
            'kpi_waiting_approval': qs.filter(service_status='QUOTATION_PENDING').count(),
            'kpi_ready_pickup': qs.filter(service_status='READY_FOR_PICKUP').count(),
            'kpi_warranty_claims': qs.filter(claim_type='FREE_WARRANTY').count(),
            'branches': Branch.objects.filter(is_active=True),
        })
        return context

class RepairTicketCreateView(LoginRequiredMixin, View):
    """
    Counter intake screen recording physical checklist, security pattern,
    and verified photo uploads with strict size and format validation.
    """
    template_name = 'repairs/intake_form.html'

    def get(self, request, *args, **kwargs):
        return render(request, self.template_name, {
            'intake_form': RepairIntakeForm(),
            'checklist_form': DeviceIntakeChecklistForm(),
            'products': Product.objects.filter(is_active=True).order_by('name'),
        })

    def post(self, request, *args, **kwargs):
        branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()
        intake_form = RepairIntakeForm(request.POST)
        checklist_form = DeviceIntakeChecklistForm(request.POST)
        uploaded_files = request.FILES.getlist('evidence_images')

        file_validation_errors = []
        for img_file in uploaded_files:
            try:
                validate_uploaded_image(img_file)
            except ValidationError as ve:
                file_validation_errors.append(str(ve.message if hasattr(ve, 'message') else ve))

        if file_validation_errors:
            for err in file_validation_errors:
                messages.error(request, err)
            return render(request, self.template_name, {
                'intake_form': intake_form,
                'checklist_form': checklist_form,
                'products': Product.objects.filter(is_active=True).order_by('name'),
            })

        if intake_form.is_valid() and checklist_form.is_valid():
            try:
                with transaction.atomic():
                    ticket = RepairTicketService.create_intake_ticket(
                        branch=branch,
                        product=intake_form.cleaned_data['product'],
                        imei_or_serial=intake_form.cleaned_data['imei_or_serial'],
                        customer_name=intake_form.cleaned_data['customer_name_manual'],
                        customer_phone=intake_form.cleaned_data['customer_phone_manual'],
                        reported_fault=intake_form.cleaned_data['reported_fault'],
                        claimed_component=intake_form.cleaned_data['claimed_component'],
                        pin_code=intake_form.cleaned_data.get('security_pin_code', ''),
                        pattern_lock=intake_form.cleaned_data.get('pattern_lock_sequence', ''),
                        intake_accessories=intake_form.cleaned_data.get('intake_accessories_received', ''),
                        checklist_data=checklist_form.cleaned_data,
                        technician=intake_form.cleaned_data.get('technician'),
                        created_by=request.user
                    )

                    for img_file in uploaded_files:
                        RepairDiagnosticEvidence.objects.create(
                            ticket=ticket,
                            evidence_type='OTHER',
                            image=img_file,
                            caption="Counter intake initial photo",
                            uploaded_by=request.user
                        )

                    RepairNotificationService.send_intake_confirmation(ticket)

                messages.success(request, f"Repair Ticket {ticket.ticket_number} booked successfully.")
                return redirect('repairs:ticket_detail', pk=ticket.pk)

            except Exception as e:
                messages.error(request, f"Error booking repair: {str(e)}")
        else:
            messages.error(request, "Please correct the form errors below.")

        return render(request, self.template_name, {
            'intake_form': intake_form,
            'checklist_form': checklist_form,
            'products': Product.objects.filter(is_active=True).order_by('name'),
        })

class RepairTicketDetailView(LoginRequiredMixin, DetailView):
    """Complete technician workbench with teardown diagnostics, photo evidence, and part replacements."""
    model = RepairTicket
    template_name = 'repairs/ticket_detail.html'
    context_object_name = 'ticket'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update({
            'checklist': getattr(self.object, 'intake_checklist', None),
            'verdict': getattr(self.object, 'defect_verdict', None),
            'quotation': getattr(self.object, 'quotation', None),
            'evidence_photos': self.object.evidence_photos.all(),
            'replaced_parts': self.object.replaced_parts.select_related('spare_part_product').all(),
            'spare_parts_catalog': Product.objects.filter(is_active=True).order_by('name'),
            'verdict_form': DefectClassificationVerdictForm(instance=getattr(self.object, 'defect_verdict', None)),
            'part_form': RepairSparePartInstallForm(),
        })
        return context

class RepairDiagnosticsVerdictView(LoginRequiredMixin, UserPassesTestMixin, View):
    """Endpoint for technicians to record defect classifications."""

    def test_func(self):
        return self.request.user.is_superuser or getattr(self.request.user, 'role', '') in ['OWNER', 'MANAGER', 'STAFF']

    def post(self, request, pk, *args, **kwargs):
        ticket = get_object_or_404(RepairTicket, pk=pk)
        verdict = request.POST.get('verdict')
        justification = request.POST.get('technical_justification', '')
        est_labor = Decimal(request.POST.get('estimated_labor', '0.00'))
        est_parts = Decimal(request.POST.get('estimated_parts', '0.00'))

        try:
            with transaction.atomic():
                updated_ticket = RepairTicketService.update_diagnostic_findings(
                    ticket=ticket,
                    verdict=verdict,
                    technical_justification=justification,
                    estimated_labor=est_labor,
                    estimated_parts=est_parts,
                    diagnosed_by=request.user
                )

                if updated_ticket.service_status == 'QUOTATION_PENDING' and hasattr(updated_ticket, 'quotation'):
                    RepairNotificationService.send_quotation_approval_request(
                        ticket=updated_ticket,
                        quote_token=updated_ticket.quotation.quote_token
                    )

            messages.success(request, f"Diagnostic verdict saved: {updated_ticket.get_claim_type_display()}")
        except Exception as e:
            messages.error(request, f"Failed to save verdict: {str(e)}")

        return redirect('repairs:ticket_detail', pk=ticket.pk)

class RepairSparePartInstallView(LoginRequiredMixin, UserPassesTestMixin, View):
    """Installs a spare part from stock, deducts inventory, and generates sub-warranties."""

    def test_func(self):
        return self.request.user.is_superuser or getattr(self.request.user, 'role', '') in ['OWNER', 'MANAGER', 'STAFF']

    def post(self, request, pk, *args, **kwargs):
        ticket = get_object_or_404(RepairTicket, pk=pk)
        part_id = request.POST.get('spare_part_product')
        qty = Decimal(request.POST.get('quantity', '1'))
        old_sn = request.POST.get('old_part_serial_or_batch', '')
        new_sn = request.POST.get('new_part_serial_or_batch', '')
        charge = Decimal(request.POST.get('customer_charge', '0.00'))
        sub_warranty = int(request.POST.get('replacement_warranty_months', '6'))
        destination = request.POST.get('defective_part_status', 'QUARANTINED_FOR_RMA')

        spare_part = get_object_or_404(Product, id=part_id)

        try:
            with transaction.atomic():
                RepairPartsService.install_replacement_part(
                    ticket=ticket,
                    spare_part_product=spare_part,
                    quantity=qty,
                    old_part_serial=old_sn,
                    new_part_serial=new_sn,
                    customer_charge=charge,
                    sub_warranty_months=sub_warranty,
                    defective_destination=destination,
                    user=request.user
                )
            messages.success(request, f"Installed {spare_part.name}. Live inventory deducted.")
        except Exception as e:
            messages.error(request, f"Stock installation error: {str(e)}")

        return redirect('repairs:ticket_detail', pk=ticket.pk)

class RepairStatusUpdateView(LoginRequiredMixin, UserPassesTestMixin, View):
    """Enforces role permissions on repair stage transitions."""

    def test_func(self):
        user = self.request.user
        return user.is_authenticated and (
            user.is_superuser or getattr(user, 'role', '') in ['OWNER', 'MANAGER', 'STAFF']
        )

    def handle_no_permission(self):
        messages.error(self.request, "Permission Denied: You do not have authority to alter repair ticket lifecycle statuses.")
        return redirect('repairs:dashboard')

    def post(self, request, pk, *args, **kwargs):
        ticket = get_object_or_404(RepairTicket, pk=pk)
        new_status = request.POST.get('new_status')

        if new_status in dict(RepairTicket.SERVICE_STATUS_CHOICES):
            ticket.service_status = new_status
            if new_status == 'READY_FOR_PICKUP':
                RepairNotificationService.send_ready_for_pickup(ticket)
            ticket.save(update_fields=['service_status', 'updated_at'])
            messages.success(request, f"Ticket status updated to: {ticket.get_service_status_display()}")
        return redirect('repairs:ticket_detail', pk=ticket.pk)

class RepairTicketSearchAPIView(LoginRequiredMixin, View):
    """
    API endpoint for POS terminal and workshop live search:
    - If q is empty: Returns active tickets in READY_FOR_PICKUP or IN_REPAIR status.
    - If q is provided: Searches across ticket_number, imei_or_serial,
      customer_name_manual, customer_phone_manual, and product__name.
    - Returns ticket ID, ticket number, device model, customer info, balance payable,
      and formatted status badge HTML.
    """

    def get(self, request, *args, **kwargs):
        branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()
        qs = RepairTicket.objects.select_related('product', 'customer', 'branch', 'technician')

        if branch and not request.user.is_superuser:
            qs = qs.filter(branch=branch)

        q = request.GET.get('q', '').strip()
        status_param = request.GET.get('status', '').strip()

        if q:
            qs = qs.filter(
                Q(ticket_number__icontains=q) |
                Q(imei_or_serial__icontains=q) |
                Q(customer_name_manual__icontains=q) |
                Q(customer_phone_manual__icontains=q) |
                Q(product__name__icontains=q)
            )
        else:
            if status_param:
                qs = qs.filter(service_status=status_param)
            else:
                qs = qs.filter(service_status__in=['READY_FOR_PICKUP', 'IN_REPAIR'])

        status_colors = {
            'RECEIVED': '#94a3b8',
            'DIAGNOSING': '#f59e0b',
            'QUOTATION_PENDING': '#ec4899',
            'APPROVED': '#06b6d4',
            'IN_REPAIR': '#3b82f6',
            'WAITING_PARTS': '#eab308',
            'QC_TESTING': '#8b5cf6',
            'READY_FOR_PICKUP': '#10b981',
            'DELIVERED': '#059669',
            'CANCELLED': '#ef4444',
        }

        tickets_data = []
        for t in qs.order_by('-created_at')[:50]:
            balance = max(Decimal('0.00'), t.final_total_amount - t.paid_amount)
            badge_color = status_colors.get(t.service_status, '#64748b')
            badge_html = (
                f'<span style="color: white; background-color: {badge_color}; '
                f'padding: 2px 8px; border-radius: 999px; font-weight: 600; font-size: 11px;">'
                f'{t.get_service_status_display()}</span>'
            )

            tickets_data.append({
                'id': t.id,
                'ticket_number': t.ticket_number,
                'device_model': t.product.name if t.product else '',
                'imei_or_serial': t.imei_or_serial,
                'customer_name': t.customer_name_manual,
                'customer_phone': t.customer_phone_manual,
                'service_status': t.service_status,
                'service_status_display': t.get_service_status_display(),
                'status_badge_html': badge_html,
                'claim_type': t.claim_type,
                'claim_type_display': t.get_claim_type_display(),
                'labor_charge': float(t.labor_charge),
                'parts_cost': float(t.parts_cost),
                'discount_amount': float(t.discount_amount),
                'final_total_amount': float(t.final_total_amount),
                'paid_amount': float(t.paid_amount),
                'balance_payable': float(balance),
                'technician_name': t.technician.get_full_name() or t.technician.username if t.technician else 'Unassigned',
                'reported_fault': t.reported_fault,
                'is_ready_for_pickup': (t.service_status == 'READY_FOR_PICKUP'),
                'created_at': t.created_at.strftime('%Y-%m-%d %H:%M') if t.created_at else '',
            })

        return JsonResponse({
            'success': True,
            'count': len(tickets_data),
            'results': tickets_data,
        })

class CustomerQuotationApprovalView(View):
    """
    Public customer-facing portal to review teardown evidence and approve/decline paid quotes.
    Prevents quotation re-decision once an approval or rejection has already been finalized.
    """
    template_name = 'repairs/customer_approval_portal.html'

    def get(self, request, token, *args, **kwargs):
        quote = get_object_or_404(CustomerQuotation, quote_token=token)
        ticket = quote.ticket
        is_already_decided = (quote.approval_status != 'PENDING')

        return render(request, self.template_name, {
            'quote': quote,
            'ticket': ticket,
            'photos': ticket.evidence_photos.all(),
            'decision_recorded': is_already_decided,
            'config': SystemConfiguration.get_solo()
        })

    def post(self, request, token, *args, **kwargs):
        quote = get_object_or_404(CustomerQuotation, quote_token=token)
        ticket = quote.ticket

        if quote.approval_status != 'PENDING':
            messages.info(
                request,
                "A decision for this repair quotation has already been locked and cannot be resubmitted."
            )
            return render(request, self.template_name, {
                'quote': quote,
                'ticket': ticket,
                'photos': ticket.evidence_photos.all(),
                'decision_recorded': True,
                'config': SystemConfiguration.get_solo()
            })

        decision = request.POST.get('decision')
        reason = request.POST.get('rejection_reason', '')

        with transaction.atomic():
            if decision == 'APPROVE':
                quote.approval_status = 'APPROVED'
                ticket.service_status = 'APPROVED'
                messages.success(request, "Quotation approved. Workshop technician will proceed with repair.")
            else:
                quote.approval_status = 'REJECTED'
                quote.rejection_reason = reason
                ticket.service_status = 'CANCELLED'
                messages.info(request, "Quotation declined. Device is prepared for return.")

            quote.customer_response_timestamp = timezone.now()
            quote.save(update_fields=['approval_status', 'rejection_reason', 'customer_response_timestamp', 'updated_at'])
            ticket.save(update_fields=['service_status', 'updated_at'])

        return render(request, self.template_name, {
            'quote': quote,
            'ticket': ticket,
            'photos': ticket.evidence_photos.all(),
            'decision_recorded': True,
            'config': SystemConfiguration.get_solo()
        })

class PublicTrackingPortalView(View):
    """
    Self-service tracking portal for customers to check repair progress.
    Enforces strict 10-digit mobile number matching to prevent IDOR privacy leaks.
    """
    template_name = 'repairs/public_tracking.html'

    def get(self, request, ticket_no=None, *args, **kwargs):
        ticket = None
        searched_no = (ticket_no or request.GET.get('ticket_no', '')).strip()
        searched_phone = request.GET.get('phone', '').strip()
        missing_phone_warning = False
        invalid_phone_format = False

        if searched_no:
            clean_phone = re.sub(r'\D', '', searched_phone)
            if len(clean_phone) == 13 and clean_phone.startswith('977'):
                clean_phone = clean_phone[3:]

            if not searched_phone:
                missing_phone_warning = True
            elif len(clean_phone) != 10:
                invalid_phone_format = True
            else:
                qs = RepairTicket.objects.filter(ticket_number__iexact=searched_no)

                matched_ticket = qs.filter(
                    Q(customer_phone_manual=clean_phone) |
                    Q(customer_phone_manual=f"+977{clean_phone}") |
                    Q(customer_phone_manual=f"+977-{clean_phone}") |
                    Q(customer__phone_number=clean_phone) |
                    Q(customer__phone_number=f"+977{clean_phone}")
                ).first()

                if matched_ticket:
                    ticket = matched_ticket

        return render(request, self.template_name, {
            'ticket': ticket,
            'searched_no': searched_no,
            'searched_phone': searched_phone,
            'missing_phone_warning': missing_phone_warning,
            'invalid_phone_format': invalid_phone_format,
            'config': SystemConfiguration.get_solo()
        })

class RepairClaimTokenPrintView(LoginRequiredMixin, View):
    """Renders 80mm thermal claim slip given to customer upon intake."""

    def get(self, request, pk, *args, **kwargs):
        ticket = get_object_or_404(RepairTicket, pk=pk)
        return render(request, 'repairs/claim_token_thermal_80mm.html', {
            'ticket': ticket,
            'checklist': getattr(ticket, 'intake_checklist', None),
            'config': SystemConfiguration.get_solo()
        })

class RepairJobSheetA4PrintView(LoginRequiredMixin, View):
    """Renders detailed A4 Job Sheet attached to device tray in technician lab."""

    def get(self, request, pk, *args, **kwargs):
        ticket = get_object_or_404(RepairTicket, pk=pk)
        return render(request, 'repairs/job_sheet_a4.html', {
            'ticket': ticket,
            'checklist': getattr(ticket, 'intake_checklist', None),
            'verdict': getattr(ticket, 'defect_verdict', None),
            'photos': ticket.evidence_photos.all(),
            'config': SystemConfiguration.get_solo()
        })

# ==============================================================================
# OPTICAL & EYEWEAR PRESCRIPTION SERVICE DESK
# ==============================================================================
class OpticalServiceDeskView(LoginRequiredMixin, View):
    """
    Dedicated workspace for ophthalmic prescription lens fittings, frame alignments,
    ultrasonic cleaning, and optical lens coating warranty claims.
    """
    template_name = 'repairs/optical_service_form.html'

    def get_common_context(self, request, form=None):
        branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()

        optical_qs = OpticalServiceTicket.objects.select_related(
            'ticket', 'ticket__product', 'ticket__technician', 'ticket__branch', 'ticket__customer'
        )
        if branch and not request.user.is_superuser:
            optical_qs = optical_qs.filter(ticket__branch=branch)

        optical_tickets = list(optical_qs.order_by('-created_at')[:50])

        optical_products = Product.objects.filter(
            Q(category__name__icontains='optic') |
            Q(category__name__icontains='glass') |
            Q(category__name__icontains='spectacle') |
            Q(category__name__icontains='lens') |
            Q(category__name__icontains='frame') |
            Q(category__name_np__icontains='चस्मा') |
            Q(is_active=True)
        ).filter(is_active=True).order_by('name')

        technicians = User.objects.filter(
            Q(role__in=['STAFF', 'MANAGER', 'OWNER']) | Q(is_superuser=True),
            is_active=True
        ).order_by('username')

        active_jobs_count = optical_qs.exclude(ticket__service_status__in=['DELIVERED', 'CANCELLED']).count()
        fitting_jobs_count = optical_qs.filter(service_type='LENS_FITTING', ticket__service_status__in=['RECEIVED', 'IN_REPAIR', 'WAITING_PARTS']).count()
        ready_jobs_count = optical_qs.filter(ticket__service_status='READY_FOR_PICKUP').count()

        return {
            'form': form or OpticalServiceTicketForm(),
            'optical_tickets': optical_tickets,
            'optical_products': optical_products,
            'technicians': technicians,
            'active_branch': branch,
            'kpi_active_jobs': active_jobs_count,
            'kpi_fitting_jobs': fitting_jobs_count,
            'kpi_ready_jobs': ready_jobs_count,
            'config': SystemConfiguration.get_solo()
        }

    def get(self, request, *args, **kwargs):
        context = self.get_common_context(request)
        return render(request, self.template_name, context)

    def post(self, request, *args, **kwargs):
        branch = getattr(request, 'active_branch', None) or Branch.get_default_main_branch()
        form = OpticalServiceTicketForm(request.POST)

        customer_name = request.POST.get('customer_name_manual', '').strip()
        customer_phone = request.POST.get('customer_phone_manual', '').strip()
        frame_product_id = request.POST.get('frame_product_id')
        technician_id = request.POST.get('technician_id')
        expected_delivery = request.POST.get('expected_delivery_date')

        service_charge = Decimal(str(request.POST.get('service_charge', '0.00') or '0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        lens_price = Decimal(str(request.POST.get('lens_price', '0.00') or '0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        notes = request.POST.get('reported_fault', '').strip()

        if not customer_name:
            messages.error(request, "Customer full name is required for optical prescription service.")
            return render(request, self.template_name, self.get_common_context(request, form=form))

        if not customer_phone:
            messages.error(request, "Customer mobile phone number is required.")
            return render(request, self.template_name, self.get_common_context(request, form=form))

        if form.is_valid():
            try:
                with transaction.atomic():
                    # 1. Resolve Catalog Product for Frame / Lens Fitting
                    product_obj = None
                    if frame_product_id:
                        product_obj = Product.objects.filter(id=frame_product_id, is_active=True).first()

                    if not product_obj:
                        frame_label = form.cleaned_data.get('frame_brand_and_model') or "Spectacles & Eyewear Frame"
                        product_obj = Product.objects.filter(name__iexact=frame_label).first()

                    if not product_obj:
                        opt_category = ProductCategory.objects.filter(
                            Q(name__icontains='optic') | Q(name__icontains='spectacle') | Q(name__icontains='frame')
                        ).first()

                        if not opt_category:
                            opt_category = ProductCategory.objects.create(
                                name="Optical & Eyewear",
                                name_np="चस्मा तथा अप्टिकल",
                                code="OPT",
                                is_active=True
                            )

                        base_unit_pcs = UnitOfMeasurement.objects.filter(code='PCS').first()
                        if not base_unit_pcs:
                            base_unit_pcs = UnitOfMeasurement.objects.create(
                                name="Piece",
                                name_np="पिस",
                                code="PCS",
                                allow_decimal=False
                            )

                        brand_obj = Brand.objects.filter(name__iexact="Generic Optical").first()
                        if not brand_obj:
                            brand_obj = Brand.objects.create(name="Generic Optical", origin_country="Nepal")

                        product_obj = Product.objects.create(
                            name=form.cleaned_data.get('frame_brand_and_model') or "Prescription Eyewear & Lens Fitting",
                            model_name=form.cleaned_data.get('frame_brand_and_model') or "Optical Frame",
                            sku=f"OPT-{uuid.uuid4().hex[:6].upper()}",
                            barcode=None,
                            category=opt_category,
                            brand=brand_obj,
                            base_unit=base_unit_pcs,
                            purchase_price=Decimal('0.00'),
                            selling_price=lens_price or Decimal('500.00'),
                            is_vat_applicable=False,
                            tax_pricing_type='EXEMPT',
                            requires_imei_tracking=False,
                            warranty_months=6
                        )

                    # 2. Resolve Technician
                    technician_user = None
                    if technician_id:
                        technician_user = User.objects.filter(id=technician_id, is_active=True).first()

                    # 3. Generate Sequential Ticket Number & Create Base RepairTicket
                    ticket_number = RepairTicketService.generate_ticket_number(branch)
                    service_type_val = form.cleaned_data.get('service_type')
                    service_type_display = dict(OpticalServiceTicket.SERVICE_TYPE_CHOICES).get(service_type_val, "Prescription Lens Fitting")
                    lens_desc = form.cleaned_data.get('lens_type_description') or "Custom Ophthalmic Prescription Lenses"
                    frame_desc = form.cleaned_data.get('frame_brand_and_model') or product_obj.name

                    ticket = RepairTicket.objects.create(
                        ticket_number=ticket_number,
                        branch=branch,
                        product=product_obj,
                        imei_or_serial=f"OPT-{uuid.uuid4().hex[:8].upper()}",
                        customer_name_manual=customer_name,
                        customer_phone_manual=customer_phone,
                        reported_fault=notes or f"{service_type_display}: {lens_desc} ({frame_desc})",
                        claimed_component='OTHER',
                        claim_type='PAID_OUT_OF_WARRANTY',
                        service_status='IN_REPAIR',
                        technician=technician_user,
                        labor_charge=service_charge,
                        parts_cost=lens_price,
                        final_total_amount=service_charge + lens_price,
                        intake_accessories_received=f"Frame: {frame_desc} | Lens: {lens_desc}",
                        expected_delivery_date=expected_delivery or None
                    )

                    # 4. Create Linked OpticalServiceTicket
                    optical_ticket = form.save(commit=False)
                    optical_ticket.ticket = ticket
                    optical_ticket.save()

                    # 5. Send Notification & Create Audit Log
                    RepairNotificationService.send_intake_confirmation(ticket)

                    AuditLog.objects.create(
                        user=request.user,
                        branch=branch,
                        action_type='CREATE',
                        module='OpticalServiceDesk',
                        object_repr=f"Optical Ticket {ticket.ticket_number}",
                        ip_address=request.META.get('REMOTE_ADDR'),
                        details={
                            'service_type': service_type_val,
                            'customer': customer_name,
                            'phone': customer_phone,
                            'total_charge': str(ticket.final_total_amount),
                            'right_sph': str(optical_ticket.right_eye_sph),
                            'left_sph': str(optical_ticket.left_eye_sph),
                            'pd': str(optical_ticket.pupillary_distance_pd)
                        }
                    )

                messages.success(request, f"Prescription Service Order '{ticket.ticket_number}' created successfully.")
                return redirect('repairs:optical_desk')

            except Exception as e:
                messages.error(request, f"Error saving optical order: {str(e)}")
        else:
            messages.error(request, "Validation errors occurred in the prescription power values. Please review entries.")

        return render(request, self.template_name, self.get_common_context(request, form=form))