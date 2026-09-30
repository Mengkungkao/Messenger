"""PIL image -> big-endian RGB565 bytes, the daemon framebuffer format.

Uses per-channel lookup tables and two 8-bit planes merged by Pillow, so no
numpy is needed. Measured on a Pi Zero 2 W: ~11 ms per 240x280 frame, and it
avoids numpy's ~11 MB RSS / ~0.4 s import cost on a 512 MB board.
"""

from __future__ import annotations

from PIL import Image, ImageChops

_R_HIGH = [v & 0xF8 for v in range(256)]          # RRRRR... -> high byte bits 7..3
_G_HIGH = [v >> 5 for v in range(256)]            # GGG..... -> high byte bits 2..0
_G_LOW = [(v & 0x1C) << 3 for v in range(256)]    # ...GGG.. -> low byte bits 7..5
_B_LOW = [v >> 3 for v in range(256)]             # BBBBB... -> low byte bits 4..0


def to_rgb565(image: Image.Image) -> bytes:
    if image.mode != "RGB":
        image = image.convert("RGB")
    red, green, blue = image.split()
    high = ImageChops.add(red.point(_R_HIGH), green.point(_G_HIGH))
    low = ImageChops.add(green.point(_G_LOW), blue.point(_B_LOW))
    return Image.merge("LA", (high, low)).tobytes()


def from_rgb565(data: bytes, width: int, height: int) -> Image.Image:
    """Inverse conversion, used for screenshots of the live framebuffer."""
    pixels = bytearray(width * height * 3)
    for i in range(width * height):
        value = (data[2 * i] << 8) | data[2 * i + 1]
        r, g, b = (value >> 11) & 0x1F, (value >> 5) & 0x3F, value & 0x1F
        pixels[3 * i] = (r << 3) | (r >> 2)
        pixels[3 * i + 1] = (g << 2) | (g >> 4)
        pixels[3 * i + 2] = (b << 3) | (b >> 2)
    return Image.frombytes("RGB", (width, height), bytes(pixels))
