import io
import base64
from typing import Optional


class BarcodeGenerator:
    """
    Generates Code128, EAN-13, and QR Code representations as Base64 strings or SVG/PNG buffers.
    Fallbacks gracefully if python-barcode or qrcode are not yet installed in the target environment.
    """

    @staticmethod
    def generate_code128_svg(code_str: str) -> Optional[str]:
        """Returns raw SVG string for a Code128 barcode."""
        try:
            import barcode
            from barcode.writer import SVGWriter
            code_cls = barcode.get_barcode_class('code128')
            writer = SVGWriter()
            buf = io.BytesIO()
            code_inst = code_cls(code_str, writer=writer)
            code_inst.write(buf, options={'write_text': True, 'quiet_zone': 2.0})
            return buf.getvalue().decode('utf-8')
        except ImportError:
            return None

    @staticmethod
    def generate_code128_base64_png(code_str: str) -> Optional[str]:
        """Returns base64 encoded PNG representation of Code128 barcode."""
        try:
            import barcode
            from barcode.writer import ImageWriter
            code_cls = barcode.get_barcode_class('code128')
            writer = ImageWriter()
            buf = io.BytesIO()
            code_inst = code_cls(code_str, writer=writer)
            code_inst.write(buf, options={'write_text': True, 'module_height': 12.0})
            encoded = base64.b64encode(buf.getvalue()).decode('ascii')
            return f"data:image/png;base64,{encoded}"
        except ImportError:
            return None

    @staticmethod
    def generate_qr_base64(data_str: str) -> Optional[str]:
        """Returns base64 encoded PNG representation of QR code (useful for FonePay/eSewa/Bills)."""
        try:
            import qrcode
            qr = qrcode.QRCode(
                version=1,
                error_correction=qrcode.constants.ERROR_CORRECT_M,
                box_size=6,
                border=2,
            )
            qr.add_data(data_str)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = io.BytesIO()
            img.save(buf, format='PNG')
            encoded = base64.b64encode(buf.getvalue()).decode('ascii')
            return f"data:image/png;base64,{encoded}"
        except ImportError:
            return None