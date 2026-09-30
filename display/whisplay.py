"""The Whisplay screen: the chat, and getting it onto the LCD.

The Whisplay HAT daemon owns the hardware -- LCD, backlight, RGB LED and
button -- and this module only ever talks to it through the client in
display/board.py. `render()` is a pure function from a `View` to a
240x280 image, which is what the tests and `--preview` exercise;
`Screen` pushes images to the daemon's framebuffer and runs the
backlight.

The look is MFruit OS's (the vendored MFruit App SDK, mfruit_sdk/): its
status bar, fonts, colours and footer hints, so the Messenger feels like
the rest of the device.

    ┌──────────────────────────────┐
    │ raspberrypi        ≋  ▭ 82%  │  who we talk to; WiFi, battery
    │ ┌──────────────┐             │  received: left
    │ │ Where are    │             │
    │ │ you?         │             │
    │ └──────────────┘             │
    │ 12:03 · -63 dBm              │
    │             ┌──────────────┐ │  sent: right
    │             │ On my way    │ │
    │             └──────────────┘ │
    │                    12:04 ✓   │  delivered / sending… / ✗
    │ ┌ typing on a keyboard|    ┐ │  only while typing
    │  hold talk  2× replies  4× exit │  what the button does now, or
    └──────────────────────────────┘  a status pill (listening, sending)

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

from mfruit_sdk.ui import Canvas, Row, draw_list, footer, status_bar, to_rgb565
from mfruit_sdk.ui import fonts as mfruit_fonts
from mfruit_sdk.ui import theme as mfruit
from PIL import Image

from utils.logger import get_logger

log = get_logger("display")

WIDTH, HEIGHT = mfruit.SCREEN_W, mfruit.SCREEN_H
CORNER_RADIUS = 20
THEME = mfruit.DARK

BG = THEME.bg
SURFACE = THEME.surface
BORDER = THEME.separator
TEXT = THEME.text
DIM = THEME.text_muted
ACCENT = THEME.accent       # the text cursor, the reply list
OK = THEME.success          # received, delivered
WARN = THEME.warning        # in progress, scrolled back
DANGER = THEME.error        # listening, failed

THEIRS = (40, 45, 55)       # received bubble
MINE = (30, 96, 186)        # sent bubble
MINE_FAILED = (120, 40, 48)

TONES = {"idle": SURFACE, "listen": DANGER, "busy": WARN, "ok": OK, "error": DANGER}
TONE_TEXT = {"idle": TEXT, "listen": BG, "busy": BG, "ok": BG, "error": BG}
META_TONES = {"dim": DIM, "ok": OK, "busy": WARN, "error": DANGER}

LED = {"idle": (0, 6, 10), "listen": (60, 0, 0), "busy": (40, 24, 0),
       "ok": (0, 40, 16), "error": (60, 0, 0)}

# Layout, in pixels: MFruit OS's status bar above, its footer below.
CONTENT_TOP, CONTENT_BOTTOM = mfruit.CONTENT_TOP, mfruit.CONTENT_BOTTOM
STATUS_TOP, STATUS_BOTTOM = 250, 272     # the status pill, where the hints go
SIDE = 10                                # gap between a bubble and the screen edge
BUBBLE_MAX_W = 176
PAD_X, PAD_Y = 9, 5
TEXT_SIZE, LINE_H = 15, 19
META_SIZE, META_H = 11, 14
GAP = 5                                  # between one message and the next
# The other radio's name is data: shrink it before cutting it.
TITLE_SIZES = (17, 15, 13)


def font(size: int, weight: str = "regular"):
    """MFruit OS's font (Inter; DejaVu where MFruit OS is not installed)."""
    return mfruit_fonts.font(size, weight)


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
    title: str = "Messenger"
    position: str = ""          # "8/10" while scrolled back
    bubbles: list = field(default_factory=list)     # oldest first
    anchor: int | None = None   # bubble at the bottom when scrolled; None = newest
    empty_hint: list = field(default_factory=list)  # lines shown with no messages
    compose: str | None = None  # being typed on a keyboard
    picker: Picker | None = None
    hints: list = field(default_factory=list)       # [(gesture, action)] for the footer
    status: str = ""            # a state worth a pill instead of the hints
    status_tone: str = "idle"
    level: float = 0.0          # mic level 0..1 while listening
    device: object = None       # mfruit_sdk.status.Status: WiFi and battery


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
        canvas.rounded_rectangle((x, y, x + bubble_w, y + bubble_h), radius=10, fill=fill,
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


def _chat(c: Canvas, view: View, top: int, bottom: int):
    """Bubbles from the bottom up; whatever does not fit scrolls off the top."""
    area = c.layer(WIDTH, bottom - top)
    canvas = area.draw
    if not view.bubbles:
        lines = view.empty_hint or ["No messages yet"]
        y = (bottom - top - len(lines) * 20) // 2
        for index, line in enumerate(lines):
            area.text(WIDTH // 2, y + index * 20, line, 14 if index == 0 else 13,
                      "semibold" if index == 0 else "regular",
                      TEXT if index == 0 else DIM, anchor="ma", max_width=WIDTH - 2 * SIDE)
    else:
        last = len(view.bubbles) - 1 if view.anchor is None else view.anchor
        newer = len(view.bubbles) - 1 - last
        y = bottom - top - (22 if newer else 2)     # room for "▼ 2 newer"
        for count, bubble in enumerate(reversed(view.bubbles[:last + 1])):
            height, paint = _bubble_block(canvas, bubble)
            if count and y - height < 0:
                # Whole messages only: none half cut under the status bar.
                # (The newest is drawn even when it alone is too tall, so
                # its end -- what was said last -- is always on screen.)
                break
            y -= height
            paint(canvas, y)
            y -= GAP
            if y < 0:
                break
        if newer:
            note = f"{view.position}  ▼ {newer} newer" if view.position else f"▼ {newer} newer"
            w = area.text_width(note, META_SIZE, "bold") + 14
            area.rounded(((WIDTH - w) // 2, bottom - top - 18, (WIDTH + w) // 2,
                          bottom - top - 2), 8, fill=WARN)
            area.text(WIDTH // 2, bottom - top - 10, note, META_SIZE, "bold", BG, anchor="mm")
    c.image.paste(area.image, (0, top))


def _compose(c: Canvas, text: str, bottom: int) -> int:
    """The line being typed, above the footer. Returns its top."""
    face = font(14)
    inner = WIDTH - 2 * SIDE - 20
    lines = wrap(c.draw, text, face, inner) or [""]
    lines = lines[-2:]                      # the end is what is being typed
    height = len(lines) * 18 + 10
    top = bottom - height
    c.rounded((SIDE, top, WIDTH - SIDE, bottom), 10, fill=SURFACE, outline=ACCENT)
    for index, line in enumerate(lines):
        c.draw.text((SIDE + 9, top + 5 + index * 18), line, font=face, fill=TEXT)
    # A drawn cursor: MFruit OS's font has no "▏".
    cursor_x = SIDE + 9 + _width(c.draw, lines[-1], face) + 1
    cursor_y = top + 5 + (len(lines) - 1) * 18
    c.rect((cursor_x, cursor_y + 2, cursor_x + 1, cursor_y + 16), ACCENT)
    return top


def _picker(c: Canvas, picker: Picker, top: int, bottom: int):
    c.rect((0, top, WIDTH, bottom), BG)
    rows = [Row(str(item)) for item in picker.items]
    draw_list(c, rows, picker.index, top=top, bottom=bottom)


def _status(c: Canvas, view: View):
    pill = (14, STATUS_TOP, WIDTH - 14, STATUS_BOTTOM)
    c.rounded(pill, 11, fill=TONES.get(view.status_tone, SURFACE))
    if view.status_tone == "listen" and view.level > 0:
        fill_w = int((pill[2] - pill[0]) * min(1.0, view.level))
        c.rounded((pill[0], pill[1], pill[0] + fill_w, pill[3]), 11, fill=(255, 150, 150))
    c.text(WIDTH // 2, (STATUS_TOP + STATUS_BOTTOM) // 2, view.status, 13, "semibold",
           TONE_TEXT.get(view.status_tone, TEXT), anchor="mm",
           max_width=pill[2] - pill[0] - 16)


def render(view: View) -> Image.Image:
    c = Canvas(theme=THEME)
    title = view.picker.title if view.picker is not None else view.title
    status_bar(c, title, view.device, title_sizes=TITLE_SIZES)
    bottom = CONTENT_BOTTOM
    if view.compose is not None:
        bottom = _compose(c, view.compose, bottom) - 4
    if view.picker is not None:
        _picker(c, view.picker, CONTENT_TOP, bottom)
    else:
        _chat(c, view, CONTENT_TOP, bottom)
    if view.status:
        _status(c, view)
    else:
        footer(c, view.hints)
    return c.image


def image_to_rgb565(image: Image.Image) -> bytes:
    """RGB image -> big-endian RGB565, the daemon's framebuffer format.

    MFruit App SDK's lookup-table converter: no numpy, ~11 ms per frame on
    a Pi Zero 2 W.
    """
    return to_rgb565(image)


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
