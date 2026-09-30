"""A 240x280 frame with cached text and the MFruit drawing primitives.

``Canvas.image`` / ``Canvas.draw`` are an ordinary PIL image and ImageDraw,
so an app draws its own content with Pillow as before and uses the canvas
for text (fast, cached) and the shared chrome in ``mfruit_sdk.ui.chrome``.
"""

from __future__ import annotations

from PIL import Image, ImageDraw

from . import fonts as fonts_module
from .theme import DARK, SCREEN_H, SCREEN_W


class Canvas:
    def __init__(self, theme=DARK, fonts=None, size=(SCREEN_W, SCREEN_H), image=None):
        self.theme = theme
        self.fonts = fonts or fonts_module.shared()
        self.image = image if image is not None else Image.new("RGB", size, theme.bg)
        self.draw = ImageDraw.Draw(self.image)

    @classmethod
    def over(cls, draw, theme=DARK, fonts=None) -> "Canvas":
        """A canvas over the image an existing ImageDraw draws on.

        For screens written as ``render(draw, state)``: Pillow (9.0 and
        later, all the boards run) keeps the image as ``draw._image``.
        """
        image = getattr(draw, "_image", None)
        if image is None:
            raise ValueError("this Pillow's ImageDraw does not expose its image")
        canvas = cls(theme, fonts, image=image)
        canvas.draw = draw
        return canvas

    @property
    def width(self) -> int:
        return self.image.width

    @property
    def height(self) -> int:
        return self.image.height

    def layer(self, width: int, height: int) -> "Canvas":
        """A canvas over a separate image, used to clip scrolling content."""
        return Canvas(self.theme, self.fonts, (width, height))

    # --------------------------------------------------------------- text
    def font(self, size: int, weight: str = "regular"):
        return self.fonts.get(size, weight)

    def text_width(self, text: str, size: int, weight: str = "regular") -> int:
        return self.fonts.text.length(text, size, weight)

    def fit(self, text: str, size: int, weight: str, max_width: int) -> str:
        """``text`` ellipsized to ``max_width`` pixels."""
        return self.fonts.text.fit(text, size, weight, max_width)

    def wrap(self, text: str, size: int, weight: str, max_width: int,
             max_lines: int = 99) -> list:
        lines = []
        for paragraph in str(text).split("\n"):
            line = ""
            for word in paragraph.split(" "):
                candidate = f"{line} {word}".strip()
                if self.text_width(candidate, size, weight) <= max_width or not line:
                    line = candidate
                else:
                    lines.append(line)
                    line = word
            lines.append(line)
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            lines[-1] = self.fit(lines[-1] + " …", size, weight, max_width)
        return [self.fit(line, size, weight, max_width) for line in lines]

    def text(self, x: float, y: float, text: str, size: int = 15, weight: str = "regular",
             color=None, anchor: str = "la", max_width: int | None = None) -> int:
        """Draw ``text``; returns its width. ``anchor`` as in PIL (la, mm, rm, ...)."""
        text = str(text)
        if max_width is not None:
            text = self.fit(text, size, weight, max_width)
        if not text:
            return 0
        mask, dx, dy = self.fonts.text.mask(text, size, weight, anchor)
        self.image.paste(color or self.theme.text,
                         (int(round(x)) + dx, int(round(y)) + dy), mask)
        return self.text_width(text, size, weight)

    # ------------------------------------------------------------- shapes
    def rect(self, box, fill):
        self.draw.rectangle(box, fill=fill)

    def rounded(self, box, radius: int, fill=None, outline=None, width: int = 1):
        self.draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)

    def hline(self, x0: int, x1: int, y: int, color=None):
        self.draw.line([(x0, y), (x1, y)], fill=color or self.theme.separator, width=1)

    def progress(self, box, fraction: float, fg=None, bg=None):
        x0, y0, x1, y1 = box
        radius = (y1 - y0) // 2
        self.rounded(box, radius, fill=bg or self.theme.surface_hi)
        fraction = max(0.0, min(1.0, fraction))
        if fraction > 0:
            self.rounded((x0, y0, x0 + max(y1 - y0, int((x1 - x0) * fraction)), y1), radius,
                         fill=fg or self.theme.accent)

    def toggle(self, x: int, y: int, on: bool):
        """A 30x16 switch whose left edge is ``x``."""
        t = self.theme
        self.rounded((x, y, x + 30, y + 16), 8, fill=t.accent if on else t.surface_hi)
        knob = x + 16 if on else x + 2
        self.draw.ellipse((knob, y + 2, knob + 12, y + 14), fill=(255, 255, 255))

    def pill(self, x: int, y: int, text: str, fg, bg, size: int = 11,
             align_right: bool = False) -> int:
        width = self.text_width(text, size, "semibold") + 12
        if align_right:
            x -= width
        self.rounded((x, y, x + width, y + size + 7), (size + 7) // 2, fill=bg)
        self.text(x + 6, y + 3, text, size, "semibold", fg)
        return width

    def spinner(self, cx: int, cy: int, radius: int, phase: int, color=None):
        start = (phase * 45) % 360
        self.draw.arc((cx - radius, cy - radius, cx + radius, cy + radius), start, start + 270,
                      fill=color or self.theme.accent, width=3)

    def chevron(self, x: int, y: int, size: int, color=None):
        """A right-pointing chevron in a ``size`` box at (x, y)."""
        color = color or self.theme.text_faint
        mid = y + size // 2
        self.draw.line([(x + size // 3, y + 1), (x + size * 2 // 3, mid),
                        (x + size // 3, y + size - 1)], fill=color, width=2)

    def bolt(self, x: int, y: int, size: int, color):
        s = size / 12.0
        points = [(7, 0), (2, 7), (5.5, 7), (4, 12), (10, 4.5), (6.5, 4.5), (8, 0)]
        self.draw.polygon([(x + px * s, y + py * s) for px, py in points], fill=color)
