"""The Whisplay screen: the chat, and getting it onto the LCD.

The Whisplay HAT daemon owns the hardware -- LCD, backlight, RGB LED and
button -- and this module only ever talks to it through the client in
display/board.py. `render()` is a pure function from a `View` to a
240x280 image, which is what the tests and `--preview` exercise;
`Screen` pushes images to the daemon's framebuffer and runs the
backlight.

    ┌──────────────────────────────┐
    │ 2/10    raspberrypi   -63dBm │  who, and how well we hear them
    ├──────────────────────────────┤
    │ ┌──────────────┐             │  received: left
    │ │ Where are    │             │
    │ │ you?         │             │
    │ └──────────────┘             │
    │ 12:03 · -63 dBm              │
    │             ┌──────────────┐ │  sent: right
    │             │ On my way    │ │
    │             └──────────────┘ │
    │                    12:04 ✓   │  delivered / sending… / ✗
    │ > typing on a keyboard_      │  only while typing
    │ [ Hold: talk · 2×: replies ] │  what the button does now
    └──────────────────────────────┘

**Frames are only pushed when something changed.** On the stock LoRa
HAT jumpers, the LCD's DC line is the radio's M1, and every frame push
clocks it: the radio is deaf for the ~11 ms a full frame takes. So
nothing here animates except the microphone level while recording, when
the radio has nothing to hear anyway.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

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

THEIRS = (44, 50, 62)       # received bubble
MINE = (30, 96, 186)        # sent bubble
MINE_FAILED = (120, 40, 48)

TONES = {"idle": SURFACE, "listen": DANGER, "busy": WARN, "ok": OK, "error": DANGER}
TONE_TEXT = {"idle": TEXT, "listen": BG, "busy": BG, "ok": BG, "error": BG}
META_TONES = {"dim": DIM, "ok": OK, "busy": WARN, "error": DANGER}

LED = {"idle": (0, 6, 10), "listen": (60, 0, 0), "busy": (40, 24, 0),
       "ok": (0, 40, 16), "error": (60, 0, 0)}

# Layout, in pixels.
HEADER_H = 34
STATUS_TOP, STATUS_BOTTOM = 240, 266     # kept inside the panel's rounded corners
SIDE = 8                                 # gap between a bubble and the screen edge
BUBBLE_MAX_W = 176
PAD_X, PAD_Y = 9, 5
TEXT_SIZE, LINE_H = 15, 19
META_SIZE, META_H = 11, 14
GAP = 5                                  # between one message and the next

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
class Bubble:
    """One message in the chat."""
    text: str
    mine: bool                  # sent by us: right-hand side
    meta: str = ""              # "12:04 ✓", "12:03 · -63 dBm"
    meta_tone: str = "dim"      # dim | ok | busy | error
    sender: str = ""            # above a received bubble, when several radios talk
    selected: bool = False      # scrolled to: what 3 clicks read, 2 clicks resend
    failed: bool = False


@dataclass
class Picker:
    """The quick-reply list, over the chat."""
    items: list
    index: int = 0
    title: str = "Quick reply"


@dataclass
class View:
    """Everything on screen, as data."""
    title: str = "LoRa Messenger"
    signal: str = ""            # "-63 dBm", the last packet heard
    position: str = ""          # "8/10" while scrolled back
    bubbles: list = field(default_factory=list)     # oldest first
    anchor: int | None = None   # bubble at the bottom when scrolled; None = newest
    empty_hint: list = field(default_factory=list)  # lines shown with no messages
    compose: str | None = None  # being typed on a keyboard
    picker: Picker | None = None
    status: str = "Ready"
    status_tone: str = "idle"
    level: float = 0.0          # mic level 0..1 while listening


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


# --- the parts of the screen ---------------------------------------------------

def _header(draw, view: View):
    draw.rectangle((0, 0, WIDTH, HEADER_H), fill=SURFACE)
    draw.line((0, HEADER_H, WIDTH, HEADER_H), fill=BORDER)
    # 12 px down, the panel's rounded corners are only 2 px in: the edge
    # text can sit 8 px from the sides.
    small = font(11)
    left = _width(draw, view.position, small) + 10 if view.position else 0
    right = _width(draw, view.signal, small) + 10 if view.signal else 0
    # Centred on the screen when it fits there, else in the room left
    # between the position and the signal: "orangepizero2w" must not lose
    # half its name to symmetry.
    lo, hi = 8 + left, WIDTH - 8 - right
    for size in (16, 15, 14):               # a size smaller before an ellipsis
        face = font(size, "bold")
        if _width(draw, view.title, face) <= hi - lo:
            break
    title = _fit(draw, view.title, face, hi - lo)
    width = _width(draw, title, face)
    x = min(max((WIDTH - width) // 2, lo), hi - width)
    draw.text((x, 8 + (16 - size) // 2), title, font=face, fill=TEXT)
    if view.position:
        draw.text((8, 12), view.position, font=small, fill=WARN)
    if view.signal:
        draw.text((WIDTH - 8 - _width(draw, view.signal, small), 12), view.signal,
                  font=small, fill=DIM)


def _bubble_block(draw, bubble: Bubble):
    """(height, draw(canvas, y)) for one message: sender, bubble, meta line."""
    face = font(TEXT_SIZE)
    lines = wrap(draw, bubble.text, face, BUBBLE_MAX_W - 2 * PAD_X) or [""]
    text_w = max(_width(draw, line, face) for line in lines)
    bubble_w = text_w + 2 * PAD_X
    bubble_h = len(lines) * LINE_H + 2 * PAD_Y - 2
    sender_h = META_H if bubble.sender else 0
    meta_h = META_H if bubble.meta else 0
    height = sender_h + bubble_h + meta_h

    def paint(canvas, y):
        x = WIDTH - SIDE - bubble_w if bubble.mine else SIDE
        small = font(META_SIZE)
        if bubble.sender:
            canvas.text((x + 2, y), _fit(canvas, bubble.sender, small, BUBBLE_MAX_W),
                        font=small, fill=OK)
            y += sender_h
        fill = (MINE_FAILED if bubble.failed else MINE) if bubble.mine else THEIRS
        canvas.rounded_rectangle((x, y, x + bubble_w, y + bubble_h), radius=9, fill=fill,
                                 outline=WARN if bubble.selected else None,
                                 width=2 if bubble.selected else 1)
        for index, line in enumerate(lines):
            canvas.text((x + PAD_X, y + PAD_Y - 1 + index * LINE_H), line, font=face,
                        fill=TEXT)
        if bubble.meta:
            meta = _fit(canvas, bubble.meta, small, WIDTH - 2 * SIDE)
            meta_x = (WIDTH - SIDE - 2 - _width(canvas, meta, small) if bubble.mine
                      else x + 2)
            canvas.text((meta_x, y + bubble_h + 1), meta, font=small,
                        fill=META_TONES.get(bubble.meta_tone, DIM))

    return height, paint


def _chat(image, draw, view: View, top: int, bottom: int):
    """Bubbles from the bottom up; whatever does not fit scrolls off the top."""
    area = Image.new("RGB", (WIDTH, bottom - top), BG)
    canvas = ImageDraw.Draw(area)
    if not view.bubbles:
        face = font(13)
        lines = view.empty_hint or ["No messages yet"]
        y = (bottom - top - len(lines) * 20) // 2
        for index, line in enumerate(lines):
            colour = TEXT if index == 0 else DIM
            canvas.text(((WIDTH - _width(canvas, line, face)) // 2, y + index * 20), line,
                        font=font(13, "bold") if index == 0 else face, fill=colour)
    else:
        last = len(view.bubbles) - 1 if view.anchor is None else view.anchor
        newer = len(view.bubbles) - 1 - last
        y = bottom - top - (22 if newer else 2)     # room for "▼ 2 newer"
        for bubble in reversed(view.bubbles[:last + 1]):
            height, paint = _bubble_block(canvas, bubble)
            y -= height
            paint(canvas, y)
            y -= GAP
            if y < 0:
                break
        if newer:
            note = f"▼ {newer} newer"
            small = font(META_SIZE, "bold")
            w = _width(canvas, note, small) + 12
            canvas.rounded_rectangle(((WIDTH - w) // 2, bottom - top - 18,
                                      (WIDTH + w) // 2, bottom - top - 2),
                                     radius=7, fill=WARN)
            canvas.text(((WIDTH - w) // 2 + 6, bottom - top - 17), note, font=small, fill=BG)
    image.paste(area, (0, top))


def _compose(draw, text: str, bottom: int) -> int:
    """The line being typed, above the status bar. Returns its top."""
    face = font(14)
    inner = WIDTH - 2 * SIDE - 16
    lines = wrap(draw, f"{text}▏", face, inner) or ["▏"]
    lines = lines[-2:]                      # the end is what is being typed
    height = len(lines) * 18 + 8
    top = bottom - height
    draw.rounded_rectangle((SIDE, top, WIDTH - SIDE, bottom), radius=8, fill=SURFACE,
                           outline=ACCENT, width=1)
    for index, line in enumerate(lines):
        draw.text((SIDE + 8, top + 4 + index * 18), line, font=face, fill=TEXT)
    return top


def _picker(draw, picker: Picker, top: int, bottom: int):
    draw.rectangle((0, top, WIDTH, bottom), fill=BG)
    face, bold = font(15), font(15, "bold")
    draw.text((SIDE + 4, top + 4), picker.title, font=font(13, "bold"), fill=DIM)
    back = "2×: back"
    draw.text((WIDTH - SIDE - 4 - _width(draw, back, font(12)), top + 5), back,
              font=font(12), fill=DIM)
    row_h, first_y = 26, top + 24
    rows = max(1, (bottom - first_y) // row_h)
    count = len(picker.items)
    # Keep the highlighted row in view, a little below the top when it can be.
    start = min(max(0, picker.index - 1), max(0, count - rows))
    for row, index in enumerate(range(start, min(count, start + rows))):
        y = first_y + row * row_h
        chosen = index == picker.index
        if chosen:
            draw.rounded_rectangle((SIDE, y, WIDTH - SIDE, y + row_h - 3), radius=8,
                                   fill=MINE)
        label = _fit(draw, str(picker.items[index]), bold if chosen else face,
                     WIDTH - 2 * SIDE - 16)
        draw.text((SIDE + 8, y + 3), label, font=bold if chosen else face,
                  fill=TEXT if chosen else DIM)
    if start > 0:
        draw.text((WIDTH - SIDE - 14, first_y + 6), "▲", font=font(11), fill=DIM)
    if start + rows < count:
        draw.text((WIDTH - SIDE - 12, bottom - 14), "▼", font=font(11), fill=DIM)


def _status(draw, view: View):
    pill = (14, STATUS_TOP, WIDTH - 14, STATUS_BOTTOM)
    draw.rounded_rectangle(pill, radius=9, fill=TONES.get(view.status_tone, SURFACE))
    if view.status_tone == "listen" and view.level > 0:
        fill_w = int((pill[2] - pill[0]) * min(1.0, view.level))
        draw.rounded_rectangle((pill[0], pill[1], pill[0] + fill_w, pill[3]),
                               radius=9, fill=(255, 150, 150))
    face = font(13, "bold")
    status = _fit(draw, view.status, face, pill[2] - pill[0] - 12)
    draw.text(((WIDTH - _width(draw, status, face)) // 2, STATUS_TOP + 5), status,
              font=face, fill=TONE_TEXT.get(view.status_tone, TEXT))


def render(view: View) -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)
    _header(draw, view)
    bottom = STATUS_TOP - 4
    if view.compose is not None:
        bottom = _compose(draw, view.compose, bottom) - 4
    if view.picker is not None:
        _picker(draw, view.picker, HEADER_H + 1, bottom)
    else:
        _chat(image, draw, view, HEADER_H + 1, bottom)
    _status(draw, view)
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
        """Hand the desktop back lit: the daemon never resets the backlight.

        At 100% when the backlight pin is the radio's M0. Anything dimmer
        is PWM on M0, and stays after we leave: a WalkieTalkie still
        running in the background, or whatever uses the radio next, would
        be deaf. WalkieTalkie hands it back at 100% for the same reason.
        """
        level = 100 if self._locked_reason else self.config.brightness
        self._locked_reason = None
        self.set_backlight(level)
