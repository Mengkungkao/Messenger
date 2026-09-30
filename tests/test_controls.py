"""The button, end to end, through the app's own MFruit OS input controller.

The controller the app builds (thresholds from config, the talk and typing
rules, the screen check) is driven with a fake clock instead of its
thread, so the timing is exact and the test is fast.
"""

import pytest

from messaging import history as h
from tests.fakes import FakeModule
from tests.test_end_to_end import PI, make_app


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def app(tmp_path, monkeypatch):
    left, _ = FakeModule.pair()
    instance = make_app(tmp_path, monkeypatch, left, "RasPi", PI)
    clock = Clock()
    instance.input.clock = clock
    instance.input.gestures.clock = clock
    instance.clock = clock
    return instance


def step(app, seconds):
    app.clock.now += seconds
    app.input.gestures.poll()


def tap(app, count=1):
    for _ in range(count):
        app.input.press()
        step(app, 0.06)
        app.input.release()
        step(app, 0.15)
    step(app, 1.0)


def sent(app):
    return [m.text for m in app.history.messages if m.direction == h.TX]


def test_talking_starts_promptly_on_the_chat(app):
    app.input.press()
    step(app, app.config.input.hold_ms / 1000 + 0.01)
    assert app.listening                                # the mic is open while held
    app.input.release()
    assert not app.listening


def test_a_hold_in_the_reply_list_is_deliberate_and_acts_on_release(app):
    tap(app, 2)                                         # two clicks: the replies
    assert app.picker is not None
    tap(app)                                            # next: "Yes"
    app.input.press()
    step(app, 0.4)
    assert not app.armed                                # a talk-length hold is not enough
    step(app, 0.4)
    assert app.armed and sent(app) == []                # armed; nothing sent while held
    app.input.release()
    assert sent(app) == ["Yes"] and app.picker is None


def test_four_quick_clicks_leave(app):
    for _ in range(4):
        app.input.press()
        step(app, 0.06)
        app.input.release()
        step(app, 0.15)
    assert not app.running


def test_a_click_on_a_dark_screen_only_wakes_it(app):
    app.screen.set_backlight(0)
    assert not app.screen.awake
    tap(app, 2)
    assert app.picker is None and app.screen.awake


def test_a_hold_on_a_dark_screen_only_wakes_it(app):
    app.screen.set_backlight(0)
    app.input.press()
    step(app, app.config.input.hold_ms / 1000 + 0.01)
    assert app.screen.awake and not app.listening
    app.input.release()
    assert not app.listening and sent(app) == []
    step(app, 0.2)
    app.input.press()
    step(app, app.config.input.hold_ms / 1000 + 0.01)
    assert app.listening
    app.input.release()


def test_nothing_while_another_app_has_the_screen(app):
    app.board.foreground_ready = False
    tap(app, 2)
    assert app.picker is None
