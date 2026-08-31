"""
Product Catalog, Pricing Evaluation & Thermal Barcode Calibration Engine.
File Path: apps/products/services.py

Features:
1. Calibrated Sticker Dimensions:
   - Sets exact millimeter dimensions matching physical barcode printer rolls (default 50mm x 25mm).
   - Generates pixel-aligned Code128 barcodes and Base64 QR code representations.
2. Mobile Specification Compilation:
   - Includes Model Name, Memory specs (e.g. 8GB/256GB), Color, MRP, Warranty period, and MDMS compliance badge.
3. Multi-Tiered Wholesale & Retail Pricing Matrix:
   - Resolves optimal volume prices based on customer account tier (Retail, Wholesale, Dealer, VIP).
"""

import io
import uuid
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Dict, Any, List

from django.db import transaction
from django.db.models import Q

from apps.inventory.models import Product, BranchStock, UnitConversion, ItemInstance
from apps.products.models import ProductPriceTier, BarcodeLabelTemplate
from apps.branches.models import Branch
from apps.core.models import SystemConfiguration
from apps.core.utils.barcode_generator import BarcodeGenerator


class ProductCatalogService:
    """
    High-level business service managing SKU/barcode generation, tiered pricing
    evaluation, and batch barcode label generation with dynamic tax annotations.
    """

    @staticmethod
    def generate_unique_sku(prefix: str = "PRD") -> str:
        """Auto-generates a clean unique SKU (e.g. PRD-A55-9B2F)."""
        return f"{prefix}-{uuid.uuid4().hex[:6].upper()}"

    @staticmethod
    def generate_internal_barcode(prefix: str = "890") -> str:
        """Generates a standard 12-digit internal EAN/Code128 barcode string."""
        unique_num = f"{uuid.uuid4().int % 1000000000:09d}"
        return f"{prefix}{unique_num}"

    @classmethod
    def get_applicable_price(
        cls,
        product: Product,
        quantity: Decimal,
        customer_type: str = 'RETAIL'
    ) -> Decimal:
        """
        Determines the optimal unit price based on customer tier (Retail, Wholesale, VIP, Dealer) and volume.
        """
        tier_mapping = {
            'WHOLESALE': 'WHOLESALE',
            'VIP': 'SPECIAL',
            'DEALER': 'DEALER'
        }
        target_tier = tier_mapping.get(customer_type, 'RETAIL')

        matched_tier = ProductPriceTier.objects.filter(
            product=product,
            tier_type=target_tier,
            min_quantity__lte=quantity,
            is_active=True
        ).order_by('-min_quantity').first()

        if matched_tier:
            return matched_tier.price_per_unit

        if customer_type == 'WHOLESALE' and product.wholesale_price:
            return product.wholesale_price

        return product.selling_price

    @classmethod
    def compile_sticker_data(
        cls,
        product: Product,
        branch: Optional[Branch] = None,
        template: Optional[BarcodeLabelTemplate] = None
    ) -> Dict[str, Any]:
        """
        Compiles complete sticker preview payload with calibrated Base64/SVG Code128 barcodes,
        phone variant specifications (RAM/ROM/Color), retail MRP, warranty badge, and dynamic tax labels.
        """
        config = SystemConfiguration.get_solo()
        code_to_render = (product.barcode or product.sku or cls.generate_internal_barcode()).strip()

        barcode_svg = BarcodeGenerator.generate_code128_svg(code_to_render)
        barcode_base64 = BarcodeGenerator.generate_code128_base64_png(code_to_render)
        qr_base64 = BarcodeGenerator.generate_qr_base64(code_to_render)

        # 1. Extract RAM / ROM / Color Variant Tag
        variant_desc = ""
        if product.ram and product.internal_storage:
            variant_desc = f"{product.ram}/{product.internal_storage}"
            if product.color_variant:
                variant_desc += f" {product.color_variant}"
        elif product.variant_name:
            variant_desc = product.variant_name

        # 2. Tax Annotation
        if config.tax_system_mode == 'VAT' and product.is_vat_applicable and product.vat_rate > Decimal('0.00'):
            tax_note = f"Incl. {product.vat_rate:.0f}% Tax" if product.tax_pricing_type == 'INCLUSIVE' else f"+{product.vat_rate:.0f}% Tax"
        else:
            tax_note = "MRP (Net Rate)"

        # 3. Label Dimensions (defaulting to calibrated 50mm x 25mm roll)
        width_mm = template.width_mm if template else 50
        height_mm = template.height_mm if template else 25

        # 4. Warranty Label
        warranty_str = f"{product.warranty_months}M Warranty" if product.warranty_months > 0 else "No Warranty"

        return {
            'product_id': product.id,
            'name': product.name,
            'model_name': product.model_name or product.name,
            'model_number': product.model_number or '',
            'variant_tag': variant_desc.strip(),
            'ram': product.ram or '',
            'storage': product.internal_storage or '',
            'color': product.color_variant or '',
            'sku': product.sku,
            'barcode': code_to_render,
            'price': f"{product.selling_price:.2f}",
            'price_formatted': f"Rs. {product.selling_price:,.2f}",
            'warranty': warranty_str,
            'rack_location': f"Rack: {product.rack_number}" if product.rack_number else "",
            'tax_note': tax_note,
            'barcode_svg': barcode_svg,
            'barcode_png': barcode_base64,
            'qr_code': qr_base64,
            'branch_name': branch.name if branch else config.company_name_en,
            'is_estimation_only': config.is_estimation_bill_only,
            'width_mm': width_mm,
            'height_mm': height_mm
        }

    @classmethod
    def generate_batch_barcode_stickers(
        cls,
        product: Product,
        quantity: int,
        template: Optional[BarcodeLabelTemplate] = None,
        branch: Optional[Branch] = None
    ) -> List[Dict[str, Any]]:
        """Generates an itemized array of calibrated sticker data payloads."""
        sticker = cls.compile_sticker_data(product=product, branch=branch, template=template)
        qty = max(1, min(500, int(quantity)))
        return [sticker for _ in range(qty)]