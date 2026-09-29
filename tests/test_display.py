"""The screen: layout survives any text, frames only when changed, backlight rules."""

from display.board import NullBoard
from display.whisplay import HEIGHT, WIDTH, Screen, View, image_to_rgb565, render, wrap
from config import UiConfig


class CountingBoard(NullBoard):
    def __init__(self):
        self.frames = 0
        self.backlight = []
        self.leds = []

    def draw_image(self, x, y, width, height, pixels):
        assert len(pixels) == width * height * 2
        self.frames += 1

    def set_backlight(self, brightness):
        self.backlight.append(brightness)

    def set_rgb(self, r, g, b):
        self.leds.append((r, g, b))


def test_sample_screen_renders_at_panel_size():
    image = render(View(label="RX: OrangePi", meta="17:00 · #1024 · -91 dBm",
                        message="Hello, how are you?", tx_line="TX: Message sent ✓",
                        tx_tone="ok", footer="raspberrypi #1A2B · 868 MHz"))
    assert image.size == (WIDTH, HEIGHT)
    assert len(image_to_rgb565(image)) == WIDTH * HEIGHT * 2


def test_any_text_renders():
    for message in ("", "x", "word " * 200, "Supercalifragilistic" * 20, "日本語 ✓ Café"):
        for status_tone in ("idle", "listen", "busy", "ok", "error"):
            render(View(message=message, status_tone=status_tone, level=0.7,
                        label="RX: " + "n" * 60, position="10/200", footer="f" * 100))


def test_wrap_respects_width():
    from PIL import Image, ImageDraw
    from display.whisplay import font
    draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    lines = wrap(draw, "a " * 50 + "Z" * 80, font(18), 200)
    assert all(draw.textlength(line, font=font(18)) <= 200 for line in lines)


def test_identical_frames_are_not_pushed():
    board = CountingBoard()
    screen = Screen(board, UiConfig())
    assert screen.show(View(status="Ready"))
    assert not screen.show(View(status="Ready"))
    assert screen.show(View(status="Listening…", status_tone="listen"))
    assert board.frames == 2
    assert board.leds[-1] == (60, 0, 0)          # red while listening


def test_nothing_drawn_without_the_foreground():
    board = CountingBoard()
    board.foreground_ready = False
    assert not Screen(board, UiConfig()).show(View())
    assert board.frames == 0


def test_locked_backlight_never_dims():
    board = CountingBoard()
    screen = Screen(board, UiConfig(idle_dim_seconds=0.0001, idle_off_seconds=0.0002))
    screen.lock_brightness("M0 shares the backlight pin")
    import time
    time.sleep(0.01)
    screen.apply_idle_policy()
    assert board.backlight[-1] == 100 and screen.awake
    assert screen.next_idle_change() == float("inf")


def test_idle_dims_and_poke_wakes():
    board = CountingBoard()
    screen = Screen(board, UiConfig(brightness=80, idle_dim_seconds=0.001,
                                    idle_dim_brightness=15))
    import time
    time.sleep(0.01)
    screen.apply_idle_policy()
    assert board.backlight[-1] == 15 and not screen.awake
    screen.poke()
    assert board.backlight[-1] == 80 and screen.awake
