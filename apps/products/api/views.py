import re
from decimal import Decimal
from datetime import date, timedelta
from django.db.models import Q
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status, permissions

from apps.inventory.models import (
    Product, ItemInstance, UnitConversion, BranchStock,
    ProductBatch, DeviceComponentWarranty
)
from apps.products.api.serializers import (
    ProductListSerializer, ProductDetailSerializer, ItemInstanceSerializer
)
from apps.products.services import ProductCatalogService
from apps.integrations.mdms.nta_checker import NTAMDMSClient

def get_product_avatar_meta(product):
    """
    Intelligent Smart Hardware & Product Classification Engine.
    Evaluates category names, RAM/Storage presence, brand names, and smartphone model keywords
    to ensure phones (e.g. '15 128 Black-O', '16 128 Pink 1', 'iPhone', 'Galaxy') are always
    correctly classified with avatar-phone and 'PHONES' category key with 0 KB server image overhead.
    """
    cat_name = (product.category.name if product.category else '').lower()
    cat_name_np = (product.category.name_np if product.category and product.category.name_np else '').lower()
    brand_name = (product.brand.name if product.brand else '').lower()
    prod_name = (product.name or '').lower()
    model_name = (product.model_name or '').lower()
    full_text = f"{prod_name} {model_name} {cat_name} {cat_name_np} {brand_name}"

    # 1. Smartphones & Handsets Detection
    is_phone = bool(product.requires_imei_tracking)

    if not is_phone and any(k in cat_name or k in cat_name_np for k in ['phone', 'mobile', 'smartphone', 'ह्यान्डसेट', 'स्मार्टफोन']):
        is_phone = True

    if not is_phone and bool((product.ram and product.ram.strip()) or (product.internal_storage and product.internal_storage.strip())):
        is_phone = True

    if not is_phone:
        phone_patterns = [
            r'\biphone\b', r'\bgalaxy\b', r'\bredmi\b', r'\bpoco\b', r'\brealme\b',
            r'\boneplus\b', r'\bpixel\b', r'\boppo\b', r'\bvivo\b', r'\binfinix\b',
            r'\btecno\b', r'\bhonor\b', r'\bmotorola\b', r'\bnokia\b', r'\bxperia\b',
            r'\b(1[1-6]|se)\s+(128|256|512|64|1tb)\b',
            r'\b(1[1-6])\s+(pro|plus|max|promax|ultra)\b',
            r'\b\d{1,2}\s+\d{2,4}\s+(black|blue|pink|white|green|gold|silver|titanium|yellow|purple)\b'
        ]
        for pattern in phone_patterns:
            if re.search(pattern, full_text):
                is_phone = True
                break

    if is_phone:
        if 'apple' in brand_name or 'iphone' in prod_name:
            return {'avatar_class': 'avatar-phone', 'icon_class': 'fab fa-apple', 'category_key': 'PHONES'}
        elif 'samsung' in brand_name or 'galaxy' in prod_name:
            return {'avatar_class': 'avatar-phone', 'icon_class': 'fas fa-mobile-screen', 'category_key': 'PHONES'}
        return {'avatar_class': 'avatar-phone', 'icon_class': 'fas fa-mobile-screen-button', 'category_key': 'PHONES'}

    # 2. Fast Chargers, Adapters & Power Cables
    if any(k in full_text for k in ['charger', 'cable', 'adapter', 'power', 'chrg', 'चार्जर', 'केबल', 'data cable', 'type-c cable', 'lightning']):
        return {'avatar_class': 'avatar-charger', 'icon_class': 'fas fa-bolt', 'category_key': 'CHARGERS'}

    # 3. Tempered Glass, Screen Protectors & Back Covers
    if any(k in full_text for k in ['glass', 'cover', 'case', 'tempered', 'protector', 'कभर', 'ग्लास', 'गिलास', 'screen guard', 'skin']):
        return {'avatar_class': 'avatar-glass', 'icon_class': 'fas fa-shield-halved', 'category_key': 'GLASS_COVER'}

    # 4. Audio, Earbuds, Neckbands & Speakers
    if any(k in full_text for k in ['audio', 'earbud', 'headphone', 'airpod', 'neckband', 'speaker', 'earphone', 'tws', 'handsfree', 'एयरबड्स', 'हेडफोन']):
        return {'avatar_class': 'avatar-audio', 'icon_class': 'fas fa-headphones-simple', 'category_key': 'AUDIO'}

    # 5. Service Spare Parts, Screens & Replacement Batteries
    if product.is_spare_part or any(k in full_text for k in ['part', 'spare', 'battery', 'display', 'screen replacement', 'touch panel', 'charging flex', 'मर्मत', 'पार्ट्स']):
        return {'avatar_class': 'avatar-part', 'icon_class': 'fas fa-car-battery', 'category_key': 'PARTS'}

    # 6. Fallback General Product Icon
    return {'avatar_class': 'avatar-phone', 'icon_class': 'fas fa-box', 'category_key': 'ALL'}

def _get_base_product_queryset():
    """Returns base queryset safely taking into account optional is_active attribute."""
    qs = Product.objects.all()
    if hasattr(Product, 'is_active'):
        qs = qs.filter(is_active=True)
    return qs

class ProductSearchAPIView(APIView):
    """
    Universal High-Speed Product Search & Auto-Complete API.
    Supports two operating modes:
      - mode='pos': Rich POS structure (in-stock IMEIs, dual-SIM flags, warranty rules, tax pricing).
      - mode='simple' (default): Clean, lightweight records for dropdowns, purchase orders,
        transfers, repair tickets, stock adjustments, and barcode print batches.
    When 'q' is empty, returns the 20 most recently updated or highest-stock active products immediately.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, *args, **kwargs):
        query = request.GET.get('q', '').strip()
        mode = request.GET.get('mode', 'simple').strip().lower()
        customer_type = request.GET.get('customer_type', 'RETAIL').strip().upper()
        branch = getattr(request, 'active_branch', None)

        try:
            limit = max(1, min(int(request.GET.get('limit', 20 if not query else 30)), 100))
        except (ValueError, TypeError):
            limit = 20 if not query else 30

        # =========================================================================
        # 1. EMPTY QUERY: IMMEDIATE DISPLAY OF TOP/RECENT PRODUCTS
        # =========================================================================
        if not query:
            products_qs = _get_base_product_queryset().select_related(
                'base_unit', 'category', 'brand'
            ).prefetch_related('branch_stocks')

            products = list(products_qs.order_by('-updated_at')[:limit])

            if mode == 'pos':
                pos_results = [self._build_pos_product_payload(p, branch, customer_type) for p in products]
                return Response({'results': pos_results, 'count': len(pos_results)})

            simple_results = [self._build_simple_product_payload(p, branch, customer_type) for p in products]
            return Response({'results': simple_results, 'count': len(simple_results)})

        # =========================================================================
        # 2. PRIORITIZED LOOKUP: EXACT IMEI / SERIAL / DEVICE UID MATCH
        # =========================================================================
        imei_qs = ItemInstance.objects.filter(
            Q(imei_1__iexact=query) |
            Q(imei_2__iexact=query) |
            Q(serial_number__iexact=query) |
            Q(device_uid__iexact=query)
        ).select_related(
            'product', 'product__base_unit', 'product__category', 'product__brand', 'branch'
        ).prefetch_related('component_warranties')

        if branch:
            imei_match = (
                imei_qs.filter(branch=branch, status='IN_STOCK').first() or
                imei_qs.filter(status='IN_STOCK').first() or
                imei_qs.first()
            )
        else:
            imei_match = imei_qs.filter(status='IN_STOCK').first() or imei_qs.first()

        if imei_match:
            if mode == 'pos':
                return Response({'results': [self._build_pos_imei_payload(imei_match, customer_type)]})
            return Response({'results': [self._build_simple_imei_payload(imei_match, customer_type)]})

        # =========================================================================
        # 3. PRIORITIZED LOOKUP: EXACT PACKAGING BARCODE (BULK BOX CONVERSIONS)
        # =========================================================================
        pkg_match = UnitConversion.objects.filter(barcode__iexact=query).select_related(
            'product', 'product__base_unit', 'product__category', 'product__brand'
        ).first()

        if pkg_match:
            if mode == 'pos':
                return Response({'results': [self._build_pos_package_payload(pkg_match, branch)]})
            return Response({'results': [self._build_simple_package_payload(pkg_match, branch)]})

        # =========================================================================
        # 4. GENERAL LOOKUP: EXACT / PARTIAL MATCH ON NAME, SKU, BARCODE & SPECS
        # =========================================================================
        products = _get_base_product_queryset().filter(
            Q(barcode__iexact=query) |
            Q(sku__iexact=query) |
            Q(barcode__icontains=query) |
            Q(sku__icontains=query) |
            Q(name__icontains=query) |
            Q(model_name__icontains=query) |
            Q(model_number__icontains=query) |
            Q(variant_name__icontains=query) |
            Q(ram__icontains=query) |
            Q(internal_storage__icontains=query) |
            Q(processor_chipset__icontains=query)
        ).select_related(
            'base_unit', 'category', 'brand'
        ).prefetch_related('component_warranty_rules', 'branch_stocks')[:limit]

        if mode == 'pos':
            pos_results = [self._build_pos_product_payload(p, branch, customer_type) for p in products]
            return Response({'results': pos_results, 'count': len(pos_results)})

        simple_results = [self._build_simple_product_payload(p, branch, customer_type) for p in products]
        return Response({'results': simple_results, 'count': len(simple_results)})

    # -------------------------------------------------------------------------
    # PAYLOAD BUILDERS: SIMPLE MODE (STANDARD DROPDOWNS & SELECTORS)
    # -------------------------------------------------------------------------
    def _build_simple_product_payload(self, p, branch, customer_type):
        stock_qty = Decimal('0.000')
        if branch:
            bs = p.branch_stocks.filter(branch=branch).first()
            if bs:
                stock_qty = bs.available_quantity

        price = ProductCatalogService.get_applicable_price(p, Decimal('1.000'), customer_type)
        variant_desc = p.variant_name or f"{p.ram or ''}/{p.internal_storage or ''} {p.color_variant or ''}".strip()
        variant_tag = f" ({variant_desc})" if variant_desc else ""
        badge = "IMEI" if p.requires_imei_tracking else ("SPARE" if p.is_spare_part else "STD")

        return {
            'id': p.id,
            'text': f"{p.name}{variant_tag} [{p.sku}]",
            'title': f"{p.name}{variant_tag}",
            'subtitle': f"SKU: {p.sku} | Barcode: {p.barcode or 'N/A'} | Stock: {stock_qty}",
            'badge': badge,
            'sku': p.sku,
            'barcode': p.barcode or '',
            'model_name': p.model_name or '',
            'model_number': p.model_number or '',
            'price': str(price),
            'wholesale_price': str(p.wholesale_price or p.selling_price),
            'purchase_price': str(p.purchase_price),
            'available_stock': str(stock_qty),
            'requires_imei': p.requires_imei_tracking,
            'requires_serial': p.requires_serial_tracking,
            'is_spare_part': p.is_spare_part,
            'unit_code': p.base_unit.code if p.base_unit else 'Pcs',
            'unit_name': p.base_unit.name if p.base_unit else 'Piece',
            'is_vat_applicable': p.is_vat_applicable,
            'vat_rate': str(p.vat_rate),
            'tax_pricing_type': p.tax_pricing_type,
        }

    def _build_simple_imei_payload(self, imei_match, customer_type):
        prod = imei_match.product
        stock_qty = Decimal('1.000') if imei_match.status == 'IN_STOCK' else Decimal('0.000')
        price = ProductCatalogService.get_applicable_price(prod, Decimal('1.000'), customer_type)

        return {
            'id': prod.id,
            'text': f"{prod.name} [IMEI: {imei_match.imei_1}]",
            'title': f"{prod.name} ({imei_match.get_condition_display()})",
            'subtitle': f"IMEI: {imei_match.imei_1} | S/N: {imei_match.serial_number or 'N/A'} | Status: {imei_match.get_status_display()}",
            'badge': 'IMEI',
            'item_instance_id': imei_match.id,
            'imei_1': imei_match.imei_1 or '',
            'imei_2': imei_match.imei_2 or '',
            'serial_number': imei_match.serial_number or '',
            'sku': prod.sku,
            'barcode': prod.barcode or '',
            'model_name': prod.model_name or '',
            'model_number': prod.model_number or '',
            'price': str(price),
            'purchase_price': str(imei_match.landed_cost or prod.purchase_price),
            'available_stock': str(stock_qty),
            'requires_imei': True,
            'requires_serial': prod.requires_serial_tracking,
            'is_spare_part': prod.is_spare_part,
            'unit_code': prod.base_unit.code if prod.base_unit else 'Pcs',
            'mdms_status': imei_match.mdms_status,
            'status': imei_match.status,
        }

    def _build_simple_package_payload(self, pkg_match, branch):
        prod = pkg_match.product
        stock_qty = Decimal('0.000')
        if branch:
            bs = BranchStock.objects.filter(branch=branch, product=prod).first()
            if bs:
                stock_qty = bs.available_quantity

        pkg_price = pkg_match.selling_price_per_unit or (prod.selling_price * pkg_match.conversion_factor)
        return {
            'id': prod.id,
            'text': f"{prod.name} ({pkg_match.unit_name}) [{pkg_match.barcode}]",
            'title': f"{prod.name} ({pkg_match.unit_name})",
            'subtitle': f"Package: {pkg_match.unit_name} (x{pkg_match.conversion_factor}) | Barcode: {pkg_match.barcode}",
            'badge': 'PKG',
            'package_conversion_id': pkg_match.id,
            'sku': prod.sku,
            'barcode': pkg_match.barcode,
            'model_name': prod.model_name or '',
            'model_number': prod.model_number or '',
            'price': str(pkg_price),
            'purchase_price': str(prod.purchase_price * pkg_match.conversion_factor),
            'available_stock': str(stock_qty / pkg_match.conversion_factor if pkg_match.conversion_factor else stock_qty),
            'requires_imei': False,
            'requires_serial': False,
            'is_spare_part': prod.is_spare_part,
            'unit_code': pkg_match.unit_name,
        }

    # -------------------------------------------------------------------------
    # PAYLOAD BUILDERS: POS MODE (RICH BILLING & CART STRUCTURE)
    # -------------------------------------------------------------------------
    def _build_pos_imei_payload(self, imei_match, customer_type):
        prod = imei_match.product
        price = ProductCatalogService.get_applicable_price(prod, Decimal('1.000'), customer_type)
        variant_desc = prod.variant_name or f"{prod.ram or ''}/{prod.internal_storage or ''} {prod.color_variant or ''}".strip()
        avatar_meta = get_product_avatar_meta(prod)

        component_warranties = []
        today = date.today()
        cw_records = imei_match.component_warranties.all()

        if cw_records.exists():
            for cw in cw_records:
                is_valid = cw.is_currently_valid and (cw.warranty_expiry_date >= today)
                component_warranties.append({
                    'component_type': cw.component_type,
                    'component_name': cw.component_name,
                    'warranty_months': cw.warranty_months,
                    'start_date': str(cw.warranty_start_date),
                    'expiry_date': str(cw.warranty_expiry_date),
                    'is_valid': is_valid,
                    'days_remaining': max(0, (cw.warranty_expiry_date - today).days),
                    'status': cw.status,
                    'status_display': cw.get_status_display(),
                    'claim_count': cw.claim_count,
                })
        else:
            for rule in prod.component_warranty_rules.all():
                start_dt = imei_match.sale_date or imei_match.purchase_date or today
                exp_dt = start_dt + timedelta(days=rule.warranty_months * 30)
                is_valid = exp_dt >= today
                component_warranties.append({
                    'component_type': rule.component_type,
                    'component_name': rule.component_name,
                    'warranty_months': rule.warranty_months,
                    'start_date': str(start_dt),
                    'expiry_date': str(exp_dt),
                    'is_valid': is_valid,
                    'days_remaining': max(0, (exp_dt - today).days),
                    'status': 'ACTIVE' if is_valid else 'EXPIRED',
                    'status_display': 'Active' if is_valid else 'Expired',
                    'claim_count': 0,
                })

        is_dual_sim = prod.sim_configuration in ['DUAL_SIM', 'ESIM_DUAL']
        has_imei_2 = bool(imei_match.imei_2 and imei_match.imei_2.strip() != '')

        return {
            'match_type': 'IMEI',
            'product_id': prod.id,
            'category_id': prod.category_id,
            'category_name': prod.category.name if prod.category else '',
            'item_instance_id': imei_match.id,
            'device_uid': imei_match.device_uid,
            'imei_1': imei_match.imei_1 or '',
            'imei_2': imei_match.imei_2 or '',
            'requires_dual_imei_scan': is_dual_sim,
            'imei_2_already_assigned': has_imei_2,
            'serial_number': imei_match.serial_number or '',
            'name': f"{prod.name} ({imei_match.get_condition_display()})",
            'brand_name': prod.brand.name if prod.brand else '',
            'model_name': prod.model_name or prod.name,
            'model_number': prod.model_number or '',
            'specs': variant_desc or f"{prod.ram or ''}/{prod.internal_storage or ''}",
            'variant_tag': variant_desc,
            'ram': prod.ram or '',
            'storage': prod.internal_storage or '',
            'color': prod.color_variant or '',
            'condition': imei_match.get_condition_display(),
            'source_type': imei_match.get_source_type_display(),
            'mdms_status': imei_match.mdms_status,
            'mdms_status_label': imei_match.get_mdms_status_display(),
            'mdms_badge_html': NTAMDMSClient.format_mdms_badge(imei_match.mdms_status),
            'activation_status': imei_match.get_activation_status_display(),
            'status': imei_match.status,
            'sku': prod.sku,
            'barcode': prod.barcode,
            'unit_code': prod.base_unit.code if prod.base_unit else 'Pcs',
            'unit_name': prod.base_unit.name if prod.base_unit else 'Piece',
            'price': str(price),
            'cost_price': str(imei_match.landed_cost),
            'purchase_date': str(imei_match.purchase_date or ''),
            'sale_date': str(imei_match.sale_date or ''),
            'sold_invoice': imei_match.sold_invoice_reference or '',
            'customer_name': imei_match.customer_name or '',
            'customer_phone': imei_match.customer_phone or '',
            'batch_reference': imei_match.batch_reference or '',
            'is_vat_applicable': prod.is_vat_applicable,
            'vat_rate': str(prod.vat_rate),
            'tax_pricing_type': prod.tax_pricing_type,
            'warranty_months': prod.warranty_months,
            'warranty_provider': prod.warranty_provider or 'Brand Official',
            'component_warranties': component_warranties,
            'available_stock': '1.000' if imei_match.status == 'IN_STOCK' else '0.000',
            'requires_imei': True,
            'avatar_class': avatar_meta['avatar_class'],
            'icon_class': avatar_meta['icon_class'],
            'category_key': avatar_meta['category_key'],
        }

    def _build_pos_package_payload(self, pkg_match, branch):
        prod = pkg_match.product
        stock_qty = Decimal('0.000')
        if branch:
            bs = BranchStock.objects.filter(branch=branch, product=prod).first()
            if bs:
                stock_qty = bs.available_quantity

        pkg_price = pkg_match.selling_price_per_unit or (prod.selling_price * pkg_match.conversion_factor)
        avatar_meta = get_product_avatar_meta(prod)

        return {
            'match_type': 'PACKAGE_UNIT',
            'product_id': prod.id,
            'category_id': prod.category_id,
            'category_name': prod.category.name if prod.category else '',
            'package_conversion_id': pkg_match.id,
            'name': f"{prod.name} ({pkg_match.unit_name})",
            'brand_name': prod.brand.name if prod.brand else '',
            'model_name': prod.model_name or prod.name,
            'sku': prod.sku,
            'barcode': pkg_match.barcode,
            'unit_code': pkg_match.unit_name,
            'unit_name': pkg_match.unit_name,
            'conversion_factor': str(pkg_match.conversion_factor),
            'price': str(pkg_price),
            'cost_price': str(prod.purchase_price * pkg_match.conversion_factor),
            'is_vat_applicable': prod.is_vat_applicable,
            'vat_rate': str(prod.vat_rate),
            'tax_pricing_type': prod.tax_pricing_type,
            'available_stock': str(stock_qty / pkg_match.conversion_factor if pkg_match.conversion_factor else stock_qty),
            'requires_imei': False,
            'requires_dual_imei_scan': False,
            'avatar_class': avatar_meta['avatar_class'],
            'icon_class': avatar_meta['icon_class'],
            'category_key': avatar_meta['category_key'],
        }

    def _build_pos_product_payload(self, p, branch, customer_type):
        stock_qty = Decimal('0.000')
        if branch:
            bs = p.branch_stocks.filter(branch=branch).first()
            if bs:
                stock_qty = bs.available_quantity

        price = ProductCatalogService.get_applicable_price(p, Decimal('1.000'), customer_type)
        variant_desc = p.variant_name or f"{p.ram or ''}/{p.internal_storage or ''} {p.color_variant or ''}".strip()
        avatar_meta = get_product_avatar_meta(p)

        in_stock_units = []
        if p.requires_imei_tracking or avatar_meta['category_key'] == 'PHONES':
            instances_qs = p.tracked_instances.filter(status='IN_STOCK')
            if branch:
                instances_qs = instances_qs.filter(branch=branch)

            for inst in instances_qs[:50]:
                in_stock_units.append({
                    'item_instance_id': inst.id,
                    'imei_1': inst.imei_1 or '',
                    'imei_2': inst.imei_2 or '',
                    'serial_number': inst.serial_number or '',
                    'condition': inst.get_condition_display(),
                    'landed_cost': str(inst.landed_cost)
                })

        comp_rules = [
            {
                'component_type': r.component_type,
                'component_name': r.component_name,
                'warranty_months': r.warranty_months,
            }
            for r in p.component_warranty_rules.all()
        ]

        is_dual_sim = p.sim_configuration in ['DUAL_SIM', 'ESIM_DUAL']

        return {
            'match_type': 'PRODUCT',
            'product_id': p.id,
            'category_id': p.category_id,
            'category_name': p.category.name if p.category else '',
            'name': p.name,
            'brand_name': p.brand.name if p.brand else '',
            'model_name': p.model_name or p.name,
            'model_number': p.model_number or '',
            'specs': variant_desc or f"{p.ram or ''}/{p.internal_storage or ''}",
            'variant_tag': variant_desc,
            'ram': p.ram or '',
            'storage': p.internal_storage or '',
            'color': p.color_variant or '',
            'network_type': p.network_type or '',
            'sim_configuration': p.sim_configuration or '',
            'processor': p.processor_chipset or '',
            'sku': p.sku,
            'barcode': p.barcode,
            'unit_code': p.base_unit.code if p.base_unit else 'Pcs',
            'unit_name': p.base_unit.name if p.base_unit else 'Piece',
            'price': str(price),
            'wholesale_price': str(p.wholesale_price or p.selling_price),
            'cost_price': str(p.purchase_price),
            'is_vat_applicable': p.is_vat_applicable,
            'vat_rate': str(p.vat_rate),
            'tax_pricing_type': p.tax_pricing_type,
            'default_mdms_status': p.default_mdms_status,
            'default_mdms_label': p.get_default_mdms_status_display(),
            'mdms_badge_html': NTAMDMSClient.format_mdms_badge(p.default_mdms_status),
            'warranty_months': p.warranty_months,
            'warranty_provider': p.warranty_provider or '',
            'component_warranty_rules': comp_rules,
            'rack_location': f"{p.rack_number or ''} {p.shelf_identifier or ''}".strip(),
            'available_stock': str(stock_qty),
            'requires_imei': p.requires_imei_tracking or (avatar_meta['category_key'] == 'PHONES'),
            'requires_dual_imei_scan': (p.requires_imei_tracking or (avatar_meta['category_key'] == 'PHONES')) and is_dual_sim,
            'requires_serial': p.requires_serial_tracking,
            'in_stock_units': in_stock_units,
            'avatar_class': avatar_meta['avatar_class'],
            'icon_class': avatar_meta['icon_class'],
            'category_key': avatar_meta['category_key'],
        }

class ProductDetailAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk, *args, **kwargs):
        try:
            product = _get_base_product_queryset().get(pk=pk)
        except Product.DoesNotExist:
            return Response({'error': 'Product not found'}, status=status.HTTP_404_NOT_FOUND)

        serializer = ProductDetailSerializer(product, context={'request': request})
        return Response(serializer.data)