import io
import os
from typing import Optional, Union
from PIL import Image, ImageDraw, ImageFont


class RawEscPosBuilder:
    """
    Standard ESC/POS Command Builder for 80mm (576 dots) and 58mm (384 dots)
    Thermal Receipt Printers.
    
    Features:
    - Native High-Speed ASCII & Control Commands (bold, double-height, align, cuts).
    - ESC/POS QR Code Model 2 generation.
    - Automated Monochrome Bitmap Raster Graphics Engine:
      - Converts uploaded company logos (PNG, JPG, WebP) into crisp 1-bit monochrome bitmaps (`GS v 0`).
      - Converts complex Unicode / Nepali Devanagari text (e.g. 'स्मार्ट मोबाइल तथा अप्टिकल हब')
        into sharp 1-bit monochrome raster images, eliminating garbled hardware printer output.
    """
    ESC = b'\x1b'
    GS = b'\x1d'

    # Text Alignment
    ALIGN_LEFT = ESC + b'a\x00'
    ALIGN_CENTER = ESC + b'a\x01'
    ALIGN_RIGHT = ESC + b'a\x02'

    # Typography & Weight
    BOLD_ON = ESC + b'E\x01'
    BOLD_OFF = ESC + b'E\x00'
    UNDERLINE_ON = ESC + b'-\x01'
    UNDERLINE_OFF = ESC + b'-\x00'
    INVERT_ON = GS + b'B\x01'
    INVERT_OFF = GS + b'B\x00'

    # Size Variations
    DOUBLE_HEIGHT_ON = ESC + b'!\x10'
    DOUBLE_WIDTH_ON = ESC + b'!\x20'
    DOUBLE_SIZE_ON = ESC + b'!\x30'
    NORMAL_TEXT = ESC + b'!\x00'

    # Hardware Controls
    FEED_CUT = GS + b'V\x41\x03'
    DRAWER_KICK = ESC + b'p\x00\x19\xfa'
    INITIALIZE = ESC + b'@'

    def __init__(self):
        self.buffer = io.BytesIO()
        self.write(self.INITIALIZE)

    def write(self, data: bytes):
        self.buffer.write(data)

    def write_text(self, text: str, encoding: str = 'utf-8'):
        """Writes standard ASCII/Latin text directly using the specified encoding."""
        self.buffer.write(text.encode(encoding, errors='ignore'))

    def new_line(self, count: int = 1):
        self.buffer.write(b'\n' * count)

    def divider(self, char: str = '-', width: int = 48):
        self.buffer.write((char * width).encode('ascii') + b'\n')

    def double_divider(self, width: int = 48):
        self.divider('=', width)

    # =========================================================================
    # UNICODE / NEPALI TEXT TO MONOCHROME BITMAP RASTER GRAPHICS ENGINE
    # =========================================================================

    @staticmethod
    def _find_devanagari_font(font_size: int = 24) -> Optional[ImageFont.ImageFont]:
        """
        Locates an available TTF font supporting Devanagari Unicode script,
        falling back to default system fonts gracefully.
        """
        candidate_font_paths = [
            # Windows Fonts
            "C:\\Windows\\Fonts\\Nirmala.ttf",
            "C:\\Windows\\Fonts\\mangal.ttf",
            "C:\\Windows\\Fonts\\aparaj.ttf",
            "C:\\Windows\\Fonts\\arial.ttf",
            # Linux / Ubuntu Fonts
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
            "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            # macOS Fonts
            "/System/Library/Fonts/Supplemental/DevanagariMT.ttc",
            "/Library/Fonts/Arial Unicode.ttf",
        ]

        for path in candidate_font_paths:
            if os.path.exists(path):
                try:
                    return ImageFont.truetype(path, font_size)
                except Exception:
                    continue

        try:
            return ImageFont.load_default()
        except Exception:
            return None

    @classmethod
    def render_unicode_text_to_image(
        cls,
        text: str,
        font_size: int = 24,
        max_width: int = 576,
        align: str = 'center'
    ) -> Optional[Image.Image]:
        """
        Renders a Unicode / Nepali Devanagari text string into a clean, sharp
        1-bit monochrome PIL Image.
        """
        if not text or not text.strip():
            return None

        font = cls._find_devanagari_font(font_size)
        lines = text.strip().split('\n')

        # Dummy image to calculate exact text bounding box
        dummy_img = Image.new('RGB', (max_width, 1000), color=(255, 255, 255))
        draw_dummy = ImageDraw.Draw(dummy_img)

        line_heights = []
        total_height = 8

        for line in lines:
            if font and hasattr(draw_dummy, 'textbbox'):
                bbox = draw_dummy.textbbox((0, 0), line, font=font)
                lh = max(font_size + 4, bbox[3] - bbox[1] + 6)
            else:
                lh = font_size + 8
            line_heights.append(lh)
            total_height += lh

        # Create target image
        img = Image.new('RGB', (max_width, total_height), color=(255, 255, 255))
        draw = ImageDraw.Draw(img)

        curr_y = 4
        for i, line in enumerate(lines):
            lh = line_heights[i]
            if font and hasattr(draw, 'textbbox'):
                bbox = draw.textbbox((0, 0), line, font=font)
                lw = bbox[2] - bbox[0]
            else:
                lw = len(line) * (font_size // 2)

            if align == 'center':
                x = max(0, (max_width - lw) // 2)
            elif align == 'right':
                x = max(0, max_width - lw - 8)
            else:
                x = 8

            draw.text((x, curr_y), line, font=font, fill=(0, 0, 0))
            curr_y += lh

        # Convert to 1-bit monochrome with sharp threshold
        return img.convert('L').point(lambda p: 255 if p > 160 else 0, mode='1')

    # =========================================================================
    # HARDWARE IMAGE RASTERIZATION (LOGOS & BITMAPS)
    # =========================================================================

    def print_image(self, img: Image.Image, max_width: int = 576):
        """
        Encodes a 1-bit monochrome PIL image into standard ESC/POS raster bit image commands
        (`GS v 0` format: b'\x1d\x76\x30\x00' + xL + xH + yL + yH + raster_data).
        """
        if img is None:
            return

        # Ensure image is in RGB before thresholding if in RGBA
        if img.mode == 'RGBA':
            background = Image.new('RGB', img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[3])  # 3 is the alpha channel
            img = background

        # Ensure 1-bit monochrome
        if img.mode != '1':
            img = img.convert('L').point(lambda p: 255 if p > 160 else 0, mode='1')

        # Ensure width is scaled proportionally if exceeding max printable area
        width, height = img.size
        if width > max_width:
            ratio = max_width / float(width)
            new_height = max(1, int(height * ratio))
            img = img.resize((max_width, new_height), Image.Resampling.NEAREST if hasattr(Image, 'Resampling') else Image.NEAREST)
            width, height = img.size

        # Ensure width is byte-aligned (divisible by 8)
        byte_width = (width + 7) // 8
        aligned_width = byte_width * 8

        if aligned_width != width:
            padded_img = Image.new('1', (aligned_width, height), color=1)
            padded_img.paste(img, (0, 0))
            img = padded_img
            width = aligned_width

        xL = byte_width % 256
        xH = byte_width // 256
        yL = height % 256
        yH = height // 256

        # Convert pixels to packed bytes (0 = white, 1 = black in ESC/POS)
        raw_pixels = img.load()
        raster_data = bytearray()

        for y in range(height):
            for x_byte in range(byte_width):
                byte_val = 0
                for bit in range(8):
                    px_x = (x_byte * 8) + bit
                    if px_x < width:
                        # In mode '1', 0 is black (dot on) and 255/1 is white (dot off)
                        if raw_pixels[px_x, y] == 0:
                            byte_val |= (1 << (7 - bit))
                raster_data.append(byte_val)

        # Center alignment
        self.write(self.ALIGN_CENTER)
        # GS v 0 0 xL xH yL yH
        cmd = self.GS + b'v0\x00' + bytes([xL, xH, yL, yH]) + bytes(raster_data)
        self.write(cmd)
        self.write(self.ALIGN_LEFT)

    def print_logo_from_field(self, image_field, max_width: int = 384, align: str = 'center'):
        """
        Convenience method to rasterize an uploaded Django ImageField file directly
        into ESC/POS raster byte streams.
        """
        if not image_field:
            return

        try:
            # Handle Django FieldFile or path string
            if hasattr(image_field, 'path') and os.path.exists(image_field.path):
                img = Image.open(image_field.path)
            elif hasattr(image_field, 'file'):
                image_field.seek(0)
                img = Image.open(image_field.file)
            elif isinstance(image_field, str) and os.path.exists(image_field):
                img = Image.open(image_field)
            else:
                return

            self.print_image(img, max_width=max_width)
            self.new_line(1)
        except Exception:
            # Gracefully fail without locking up printer if image is unreadable
            pass

    def print_unicode_text_as_image(
        self,
        text: str,
        font_size: int = 24,
        max_width: int = 576,
        align: str = 'center'
    ):
        """
        Converts Unicode/Nepali text to image and streams the raster bit command directly into the buffer.
        """
        try:
            img = self.render_unicode_text_to_image(
                text=text,
                font_size=font_size,
                max_width=max_width,
                align=align
            )
            if img:
                self.print_image(img, max_width=max_width)
        except Exception:
            # Safe ASCII fallback if Pillow font rendering fails
            clean_text = text.encode('ascii', errors='replace').decode('ascii')
            self.write_text(clean_text + "\n")

    # =========================================================================
    # QR CODE GENERATION
    # =========================================================================

    def print_qr_code(self, data_str: str, module_size: int = 6):
        """
        Standard ESC/POS QR Code Model 2 raw byte sequence generation.
        """
        data_bytes = data_str.encode('utf-8')
        length = len(data_bytes) + 3
        pL = length % 256
        pH = length // 256

        # 1. Select QR Model (Model 2)
        self.write(self.GS + b'(k\x04\x00\x31\x41\x32\x00')
        # 2. Set Module Size
        self.write(self.GS + b'(k\x03\x00\x31\x43' + bytes([module_size]))
        # 3. Set Error Correction Level (Level M)
        self.write(self.GS + b'(k\x03\x00\x31\x45\x31')
        # 4. Store Data
        self.write(self.GS + b'(k' + bytes([pL, pH]) + b'\x31\x50\x30' + data_bytes)
        # 5. Print Stored QR Code
        self.write(self.GS + b'(k\x03\x00\x31\x51\x30')

    def get_bytes(self) -> bytes:
        return self.buffer.getvalue()