"""The screen: layout survives any text, frames only when changed, backlight rules."""

from mfruit_sdk.status import Status

from display.board import NullBoard
from display.whisplay import (CONTENT_BOTTOM, CONTENT_TOP, HEIGHT, MINE, STATUS_TOP,
                              THEIRS, THEME, WARN, WIDTH, Bubble, Picker, Screen, View,
                              image_to_rgb565, render, wrap)
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
    image = render(View(title="OrangePi", device=Status(3, 82, False), bubbles=[
        Bubble("Hello, how are you?", False, "17:00 · -91 dBm"),
        Bubble("Fine, thanks", True, "17:01 ✓", "ok")]))
    assert image.size == (WIDTH, HEIGHT)
    assert len(image_to_rgb565(image)) == WIDTH * HEIGHT * 2


def test_any_text_renders():
    for message in ("", "x", "word " * 200, "Supercalifragilistic" * 20, "日本語 ✓ Café"):
        for status_tone in ("idle", "listen", "busy", "ok", "error"):
            bubbles = [Bubble(message, mine, "m" * 80, sender="n" * 60,
                              selected=mine, failed=mine) for mine in (False, True)]
            render(View(title="t" * 60, position="10/10", device=Status(2, 5, True),
                        bubbles=bubbles, anchor=0, status=message or "Ready",
                        status_tone=status_tone, level=0.7, compose=message))
            render(View(picker=Picker([message or "x"] * 12, index=11),
                        hints=[("tap", message or "x")] * 5))


def colour_at(image, x, y):
    return image.getpixel((x, y))


def bubble_rows(image, x):
    """y of every pixel in column x that is a bubble's fill colour."""
    return {y for y in range(HEIGHT) if colour_at(image, x, y) in (MINE, THEIRS)}


def test_received_left_sent_right():
    image = render(View(bubbles=[Bubble("hi", False, "12:00"), Bubble("yo", True, "12:01")]))
    assert any(colour_at(image, 12, y) == THEIRS for y in range(HEIGHT))
    assert any(colour_at(image, WIDTH - 12, y) == MINE for y in range(HEIGHT))
    assert not any(colour_at(image, 12, y) == MINE for y in range(HEIGHT))
    assert not any(colour_at(image, WIDTH - 12, y) == THEIRS for y in range(HEIGHT))


def test_newest_is_at_the_bottom_and_old_ones_scroll_off():
    sent = [Bubble(f"message {n}", True, "12:00") for n in range(9)]
    many = sent + [Bubble("the newest", False, "12:01")]
    image = render(View(bubbles=many))
    assert max(bubble_rows(image, 12)) > max(bubble_rows(image, WIDTH - 12))
    # Five one-line messages already overflow the chat; the older five are
    # off the top, and drawing them or not changes nothing on screen.
    assert image.tobytes() == render(View(bubbles=many[-5:])).tobytes()


def test_scrolled_back_shows_the_anchor_at_the_bottom():
    many = [Bubble(f"message {n}", False, "12:00") for n in range(10)]
    live, back = render(View(bubbles=many)), render(View(bubbles=many, anchor=3))
    assert live.tobytes() != back.tobytes()
    assert any(colour_at(back, x, y) == WARN        # "▼ 6 newer", and the outline
               for x in range(WIDTH) for y in range(STATUS_TOP - 30, STATUS_TOP))


def test_the_reply_list_and_typing_replace_what_they_cover():
    chat = View(bubbles=[Bubble("hi", False, "12:00")])
    picking = View(bubbles=chat.bubbles, picker=Picker(["OK", "Yes"], index=1))
    typing = View(bubbles=chat.bubbles, compose="hello")
    assert render(picking).tobytes() != render(chat).tobytes()
    assert render(typing).tobytes() != render(chat).tobytes()
    # The chosen reply is highlighted as MFruit OS highlights a list row.
    assert any(colour_at(render(picking), 20, y) == THEME.accent_dim for y in range(HEIGHT))


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


def test_leaving_hands_the_backlight_back_at_full_when_it_is_m0():
    # Any less is PWM on M0: a WalkieTalkie left running would go deaf.
    board = CountingBoard()
    screen = Screen(board, UiConfig(brightness=80))
    screen.lock_brightness("M0 shares the backlight pin")
    screen.restore()
    assert board.backlight[-1] == 100


def test_leaving_restores_the_configured_brightness_otherwise():
    board = CountingBoard()
    Screen(board, UiConfig(brightness=80)).restore()
    assert board.backlight[-1] == 80


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


def test_a_dimmed_backlight_is_named_as_the_deafening_state(monkeypatch):
    """PWM at 75%: transparent three reads in four, and still deaf."""
    from lora import modepins
    reads = iter([(1, 0, 1, 1) if i % 4 == 0 else (0, 0, 1, 1) for i in range(12)])
    monkeypatch.setattr(modepins, "_read", lambda m0, m1: next(reads))
    result = modepins.sample(samples=12, seconds=0)
    assert not result["transparent"]
    assert result["levels"] == (1, 0)
    assert result["detail"] == "module is in wake-on-radio tx mode (25% of the time)"


def test_a_long_radio_name_is_not_cut(monkeypatch):
    """"orangepizero2w" once came out "orangepize…" on the Pi Zero. The name
    is data: the status bar shrinks it before it would cut it, even beside
    WiFi and a three-digit battery."""
    from mfruit_sdk.ui.canvas import Canvas
    drawn = []
    real = Canvas.text
    monkeypatch.setattr(Canvas, "text", lambda self, x, y, text, *a, **k:
                        drawn.append(self.fit(str(text), a[0], a[1], k["max_width"])
                                     if k.get("max_width") and len(a) > 1 else str(text))
                        or real(self, x, y, text, *a, **k))
    render(View(title="orangepizero2w", device=Status(3, 100, True)))
    assert "orangepizero2w" in drawn


def test_content_stays_between_the_status_bar_and_the_footer():
    many = [Bubble(f"message {n} " * 4, n % 2 == 0, "12:00 ✓") for n in range(10)]
    for view in (View(bubbles=many), View(bubbles=many, anchor=4),
                 View(bubbles=many, compose="typing " * 10),
                 View(picker=Picker(["OK"] * 20, index=19))):
        image = render(view)
        for box in ((0, 32, WIDTH, CONTENT_TOP - 1), (0, CONTENT_BOTTOM + 1, WIDTH, 250)):
            assert image.crop(box).getcolors() == [
                ((box[2] - box[0]) * (box[3] - box[1]), THEME.bg)], box


def test_the_footer_shows_hints_until_there_is_something_to_say():
    hints = [("hold", "talk"), ("2×", "replies"), ("4×", "exit")]
    idle = render(View(hints=hints))
    listening = render(View(hints=hints, status="Listening… 1.0s", status_tone="listen"))
    footer = (0, STATUS_TOP, WIDTH, HEIGHT - 8)
    assert idle.crop(footer).tobytes() != listening.crop(footer).tobytes()
    assert any(colour_at(listening, WIDTH // 2, y) == (255, 92, 92) or
               colour_at(listening, 20, y) == THEME.error for y in range(STATUS_TOP, HEIGHT))
