"""The Whisplay screen: what the messenger shows, and getting it there.

The Whisplay HAT daemon owns the hardware -- LCD, backlight, RGB LED and
button -- and this module only ever talks to it through the client in
display/board.py. `render()` is a pure function from a `View` to a
240x280 image, which is what the tests and `--preview` exercise;
`Screen` pushes images to the daemon's framebuffer and runs the
backlight.

    ┌──────────────────────────────┐
    │        LoRa Messenger        │
    ├──────────────────────────────┤
    │ RX: OrangePi            3/12 │
    │ 12:04 · #1024 · -91 dBm      │
    │ "Hello, how are you?"        │
    │                              │
    │ TX: Message delivered ✓      │
    │ [ Ready                    ] │
    │   RasPi #1A2B · 868 MHz      │
    └──────────────────────────────┘

**Frames are only pushed when something changed.** On the stock LoRa
HAT jumpers, the LCD's DC line is the radio's M1, and every frame push
clocks it: the radio is deaf for the ~11 ms a full frame takes. So
nothing here animates except the microphone level while recording, when
the radio has nothing to hear anyway.
"""

from __future__ import annotations

import textwrap
import threading
import time
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont

from utils.logger import get_logger

log = get_logger("display")

WIDTH, HEIGHT = 240, 280
CORNER_RADIUS = 20

BG = (10, 12, 16)
SURFACE = (24, 28, 36)
BORDER = (58, 66, 82)
TEXT = (236, 240, 246)
DIM = (140, 150, 166)
ACCENT = (86, 168, 255)     # our own messages
OK = (64, 208, 138)         # received, delivered
WARN = (245, 178, 62)       # in progress
DANGER = (255, 92, 92)      # listening, failed

TONES = {"idle": SURFACE, "listen": DANGER, "busy": WARN, "ok": OK, "error": DANGER}
TONE_TEXT = {"idle": TEXT, "listen": BG, "busy": BG, "ok": BG, "error": BG}

LED = {"idle": (0, 6, 10), "listen": (60, 0, 0), "busy": (40, 24, 0),
       "ok": (0, 40, 16), "error": (60, 0, 0)}

_FONTS = {
    "regular": ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
    "bold": ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
             "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
}
_font_cache = {}


def font(size: int, weight: str = "regular"):
    key = (size, weight)
    if key not in _font_cache:
        for path in _FONTS[weight]:
            try:
                _font_cache[key] = ImageFont.truetype(path, size)
                break
            except OSError:
                continue
        else:
            _font_cache[key] = ImageFont.load_default()
    return _font_cache[key]


@dataclass
class View:
    """Everything on screen, as data."""
    title: str = "LoRa Messenger"
    label: str = ""             # "RX: OrangePi" / "TX → all"
    label_tone: str = "rx"      # rx | tx
    meta: str = ""              # time, ID, signal
    message: str = ""           # the text, unquoted
    position: str = ""          # "3/12" while browsing history
    tx_line: str = ""           # "TX: Message delivered ✓"
    tx_tone: str = "idle"
    status: str = "Ready"
    status_tone: str = "idle"
    level: float = 0.0          # mic level 0..1 while listening
    footer: str = ""


def _width(draw, text, face) -> int:
    return int(draw.textlength(text, font=face))


def _fit(draw, text: str, face, width: int) -> str:
    if _width(draw, text, face) <= width:
        return text
    while text and _width(draw, text + "…", face) > width:
        text = text[:-1]
    return text + "…"


def wrap(draw, text: str, face, width: int) -> list:
    """Greedy word wrap by pixel width."""
    lines = []
    for paragraph in text.split("\n"):
        words = paragraph.split()
        line = ""
        for word in words:
            candidate = f"{line} {word}".strip()
            if _width(draw, candidate, face) <= width:
                line = candidate
                continue
            if line:
                lines.append(line)
            # A word wider than the line is cut into pieces that fit.
            while _width(draw, word, face) > width and len(word) > 1:
                low, high = 1, len(word)        # longest prefix that fits
                while low < high:
                    middle = (low + high + 1) // 2
                    if _width(draw, word[:middle], face) <= width:
                        low = middle
                    else:
                        high = middle - 1
                lines.append(word[:low])
                word = word[low:]
            line = word
        if line:
            lines.append(line)
    return lines


def render(view: View) -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)
    margin = 12
    inner = WIDTH - 2 * margin

    # Title bar.
    draw.rectangle((0, 0, WIDTH, 36), fill=SURFACE)
    draw.line((0, 36, WIDTH, 36), fill=BORDER)
    face = font(18, "bold")
    draw.text(((WIDTH - _width(draw, view.title, face)) // 2, 9), view.title,
              font=face, fill=TEXT)

    # Who and when.
    y = 44
    if view.label:
        colour = OK if view.label_tone == "rx" else ACCENT
        pos_face = font(12)
        pos_w = _width(draw, view.position, pos_face) + 6 if view.position else 0
        draw.text((margin, y), _fit(draw, view.label, font(17, "bold"), inner - pos_w),
                  font=font(17, "bold"), fill=colour)
        if view.position:
            draw.text((WIDTH - margin - pos_w + 6, y + 4), view.position,
                      font=pos_face, fill=DIM)
    if view.meta:
        draw.text((margin, y + 22), _fit(draw, view.meta, font(12), inner),
                  font=font(12), fill=DIM)

    # The message: as large as fits in the space.
    top, bottom = 88, 196
    if view.message:
        quoted = f"“{view.message}”"
        for size in (20, 17, 15, 13):
            face = font(size)
            line_h = size + 5
            lines = wrap(draw, quoted, face, inner)
            if len(lines) * line_h <= bottom - top:
                break
        max_lines = (bottom - top) // line_h
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            lines[-1] = _fit(draw, lines[-1] + "…", face, inner)
        for index, line in enumerate(lines):
            draw.text((margin, top + index * line_h), line, font=face, fill=TEXT)
    else:
        face = font(14)
        hint = "Hold the button and speak"
        draw.text(((WIDTH - _width(draw, hint, face)) // 2, (top + bottom) // 2 - 8),
                  hint, font=face, fill=DIM)

    # Last transmission's fate.
    draw.line((margin, 202, WIDTH - margin, 202), fill=BORDER)
    if view.tx_line:
        colour = {"ok": OK, "error": DANGER, "busy": WARN}.get(view.tx_tone, DIM)
        draw.text((margin, 208), _fit(draw, view.tx_line, font(15, "bold"), inner),
                  font=font(15, "bold"), fill=colour)

    # Status pill, with the mic level while listening.
    pill = (margin, 232, WIDTH - margin, 256)
    draw.rounded_rectangle(pill, radius=8, fill=TONES.get(view.status_tone, SURFACE))
    if view.status_tone == "listen" and view.level > 0:
        fill_w = int((pill[2] - pill[0]) * min(1.0, view.level))
        draw.rounded_rectangle((pill[0], pill[1], pill[0] + fill_w, pill[3]),
                               radius=8, fill=(255, 150, 150))
    face = font(15, "bold")
    status = _fit(draw, view.status, face, inner - 12)
    draw.text(((WIDTH - _width(draw, status, face)) // 2, 235), status, font=face,
              fill=TONE_TEXT.get(view.status_tone, TEXT))

    # Footer, kept inside the panel's rounded bottom corners.
    if view.footer:
        face = font(11)
        footer = _fit(draw, view.footer, face, WIDTH - 2 * CORNER_RADIUS - 8)
        draw.text(((WIDTH - _width(draw, footer, face)) // 2, 262), footer,
                  font=face, fill=DIM)
    return image


def image_to_rgb565(image: Image.Image) -> bytes:
    """RGB image -> big-endian RGB565, the daemon's framebuffer format."""
    import numpy as np
    arr = np.asarray(image.convert("RGB"), dtype=np.uint16)
    packed = ((arr[:, :, 0] & 0xF8) << 8) | ((arr[:, :, 1] & 0xFC) << 3) | (arr[:, :, 2] >> 3)
    return packed.astype(">u2").tobytes()


class Screen:
    """Frames to the board, only when changed; backlight and LED policy."""

    def __init__(self, board, ui_config):
        self.board = board
        self.config = ui_config
        self._lock = threading.Lock()
        self._last_hash = None
        self._last_led = None
        self._backlight = None
        self._locked_reason = None
        self._last_activity = time.monotonic()
        self.frames_pushed = 0
        self.set_backlight(ui_config.brightness)

    def show(self, view: View) -> bool:
        if not getattr(self.board, "foreground_ready", True):
            return False
        image = render(view)
        digest = hash(image.tobytes())
        if digest == self._last_hash:
            return False
        with self._lock:
            try:
                self.board.draw_image(0, 0, WIDTH, HEIGHT, image_to_rgb565(image))
            except Exception:
                log.warning("framebuffer write failed", exc_info=True)
                return False
        self._last_hash = digest
        self.frames_pushed += 1
        self.set_led(LED.get(view.status_tone, LED["idle"]))
        return True

    def invalidate(self):
        self._last_hash = None
        self._last_led = None

    def set_led(self, colour):
        if not self.config.led_enabled or colour == self._last_led:
            return
        self._last_led = colour
        try:
            self.board.set_rgb(*colour)
        except Exception:
            log.debug("LED write failed", exc_info=True)

    # --- backlight -----------------------------------------------------
    def set_backlight(self, brightness: int):
        if self._locked_reason:
            brightness = 100
        brightness = max(0, min(100, int(brightness)))
        if brightness == self._backlight:
            return
        self._backlight = brightness
        try:
            self.board.set_backlight(brightness)
        except Exception:
            log.debug("backlight write failed", exc_info=True)

    def lock_brightness(self, reason: str):
        """Pin full brightness: the backlight pin is also the radio's M0.

        Dimming is 1 kHz PWM on that pin, which would flip the module in
        and out of transparent mode a thousand times a second. Only a
        steady 100% holds M0 low, where the radio can hear.
        """
        self._locked_reason = reason
        self.set_backlight(100)
        log.warning("backlight pinned at 100%%: %s", reason)

    def poke(self):
        """Someone is here, or a message came in: wake the screen."""
        self._last_activity = time.monotonic()
        if self._backlight != self.config.brightness:
            self.set_backlight(self.config.brightness)
            self.invalidate()

    @property
    def awake(self) -> bool:
        return bool(self._backlight) and (self._locked_reason is not None
                                          or self._backlight >= self.config.brightness)

    def apply_idle_policy(self):
        if self._locked_reason:
            return
        idle = time.monotonic() - self._last_activity
        if self.config.idle_off_seconds and idle >= self.config.idle_off_seconds:
            self.set_backlight(0)
        elif self.config.idle_dim_seconds and idle >= self.config.idle_dim_seconds:
            self.set_backlight(self.config.idle_dim_brightness)

    def next_idle_change(self) -> float:
        if self._locked_reason:
            return float("inf")
        idle = time.monotonic() - self._last_activity
        for threshold in (self.config.idle_dim_seconds, self.config.idle_off_seconds):
            if threshold and idle < threshold:
                return threshold - idle
        return float("inf")

    def restore(self):
        """Hand the desktop back lit: the daemon never resets the backlight."""
        self._locked_reason = None
        self.set_backlight(self.config.brightness)
