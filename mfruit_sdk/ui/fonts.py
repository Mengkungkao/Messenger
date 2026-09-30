"""MFruit OS's fonts (Inter) with the DejaVu fallback, and a text raster cache.

Inter ships with MFruit OS (SIL OFL), so an app on a board with MFruit OS
installed looks exactly like the launcher; elsewhere DejaVu (installed with
whisplay-daemon's dependencies) is used. Search order:

    $MFRUIT_FONT_DIR
    <this package>/fonts                       an app may vendor Inter here
    <MFruit OS source>/assets/fonts            when the SDK runs inside MFruit OS
    $MFRUIT_HOME/system/current/assets/fonts   exported by mfruit-run
    ~/.whisplay-os/system/current/assets/fonts
    /home/*/.whisplay-os/system/current/assets/fonts   (apps started as another user)
    ~/MFruitOS/assets/fonts                    a source checkout (development machines)
"""

from __future__ import annotations

import glob
import logging
import os
from collections import OrderedDict

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("mfruit_sdk.fonts")

WEIGHTS = {
    "regular": ("Inter-Regular.ttf", "DejaVuSans.ttf"),
    "medium": ("Inter-Medium.ttf", "DejaVuSans.ttf"),
    "semibold": ("Inter-SemiBold.ttf", "DejaVuSans-Bold.ttf"),
    "bold": ("Inter-Bold.ttf", "DejaVuSans-Bold.ttf"),
}
SYSTEM_FONT_DIRS = ("/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu",
                    "/usr/share/fonts/TTF")
INSTALLED = os.path.join("system", "current", "assets", "fonts")
HERE = os.path.dirname(os.path.abspath(__file__))


def font_dirs() -> list:
    dirs = [os.environ.get("MFRUIT_FONT_DIR", ""),
            os.path.join(os.path.dirname(HERE), "fonts"),
            os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(HERE))),
                         "assets", "fonts")]
    for home in (os.environ.get("MFRUIT_HOME", ""), os.environ.get("WHISPLAY_OS_HOME", ""),
                 os.path.expanduser("~/.whisplay-os")):
        if home:
            dirs.append(os.path.join(home, INSTALLED))
    dirs += sorted(glob.glob(os.path.join("/home/*/.whisplay-os", INSTALLED)))
    dirs.append(os.path.expanduser(os.path.join("~", "MFruitOS", "assets", "fonts")))
    return [d for d in dirs if d]


def _basic_layout():
    layout = getattr(ImageFont, "Layout", None)  # Pillow >= 9.1
    if layout is not None:
        return layout.BASIC
    return getattr(ImageFont, "LAYOUT_BASIC", 0)


class Fonts:
    def __init__(self, dirs: list | None = None):
        self.dirs = list(dirs) if dirs is not None else font_dirs()
        self._cache = {}
        self._paths = {}
        self._layout = _basic_layout()
        self.text = TextCache(self)

    def path(self, weight: str):
        if weight not in self._paths:
            bundled, system = WEIGHTS.get(weight, WEIGHTS["regular"])
            candidates = [os.path.join(d, bundled) for d in self.dirs]
            candidates += [os.path.join(d, system) for d in SYSTEM_FONT_DIRS]
            self._paths[weight] = next((p for p in candidates if os.path.isfile(p)), None)
            if self._paths[weight] is None:
                log.warning("no TrueType font for weight %s; using PIL's default", weight)
        return self._paths[weight]

    @property
    def is_inter(self) -> bool:
        return os.path.basename(self.path("regular") or "").startswith("Inter")

    def get(self, size: int, weight: str = "regular"):
        key = (weight, int(size))
        font = self._cache.get(key)
        if font is None:
            path = self.path(weight)
            try:
                font = (ImageFont.truetype(path, size=int(size), layout_engine=self._layout)
                        if path else ImageFont.load_default())
            except OSError as exc:
                log.error("cannot load font %s: %s", path, exc)
                font = ImageFont.load_default()
            self._cache[key] = font
        return font


class TextCache:
    """Rasterised text, cached.

    FreeType rasterisation was ~85 % of a frame on a Pi Zero 2 W (~3 ms per
    string) and the same strings are drawn every frame. A string is
    rasterised once into an 8-bit alpha mask and then pasted in any colour,
    which gives the same pixels as ``ImageDraw.text``.
    """

    MAX_ENTRIES = 800

    def __init__(self, fonts: Fonts):
        self.fonts = fonts
        self._masks = OrderedDict()
        self._lengths = {}
        self._fits = {}

    def _remember(self, store, key, value):
        store[key] = value
        if isinstance(store, OrderedDict):
            store.move_to_end(key)
            if len(store) > self.MAX_ENTRIES:
                store.popitem(last=False)
        elif len(store) > self.MAX_ENTRIES * 4:
            store.clear()
        return value

    def length(self, text: str, size: int, weight: str) -> int:
        key = (text, size, weight)
        value = self._lengths.get(key)
        if value is None:
            font = self.fonts.get(size, weight)
            try:
                width = font.getlength(text)
            except AttributeError:          # Pillow < 8: bitmap fallback font
                width = font.getsize(text)[0]
            value = self._remember(self._lengths, key, int(width))
        return value

    def fit(self, text: str, size: int, weight: str, max_width: int) -> str:
        """``text`` shortened with an ellipsis to fit ``max_width`` pixels."""
        key = (text, size, weight, max_width)
        value = self._fits.get(key)
        if value is not None:
            return value
        if self.length(text, size, weight) <= max_width:
            return self._remember(self._fits, key, text)
        low, high = 0, len(text)
        while low < high:
            mid = (low + high + 1) // 2
            if self.length(text[:mid].rstrip() + "…", size, weight) <= max_width:
                low = mid
            else:
                high = mid - 1
        return self._remember(self._fits, key, text[:low].rstrip() + "…")

    def mask(self, text: str, size: int, weight: str, anchor: str):
        """(alpha mask, dx, dy) to paste at the anchor point."""
        key = (text, size, weight, anchor)
        value = self._masks.get(key)
        if value is not None:
            self._masks.move_to_end(key)
            return value
        font = self.fonts.get(size, weight)
        try:
            x0, y0, x1, y1 = font.getbbox(text, anchor=anchor)
        except (TypeError, ValueError):  # bitmap fallback fonts do not take anchors
            x0, y0, x1, y1 = font.getbbox(text)
        mask = Image.new("L", (max(1, x1 - x0), max(1, y1 - y0)), 0)
        draw = ImageDraw.Draw(mask)
        try:
            draw.text((-x0, -y0), text, font=font, fill=255, anchor=anchor)
        except (TypeError, ValueError):
            draw.text((-x0, -y0), text, font=font, fill=255)
        return self._remember(self._masks, key, (mask, x0, y0))


_shared = None


def shared() -> Fonts:
    """One Fonts instance per process."""
    global _shared
    if _shared is None:
        _shared = Fonts()
    return _shared


def font(size: int, weight: str = "regular"):
    return shared().get(size, weight)
