"""The chat: what is shown, the button, quick replies, typing on a keyboard.

Input goes in the way it does on the device: button actions through the
app's handler (what MFruit OS's input controller calls), and keys through
the controller itself, so ownership and the Space/typing rule are real.
"""

import time

import pytest
from mfruit_sdk.input import BACK, EXTRA, NEXT, PREVIOUS, SELECT, Action
from mfruit_sdk.keys import DOWN, UP, KeyEvent

from asr.speech_to_text import NoEngine
from lora import protocol
from messaging import history as h
from tests.fakes import FakeEngine, FakeModule
from tests.test_end_to_end import ORANGE, PI, make_app, wait_for

import main as main_module


@pytest.fixture
def app(tmp_path, monkeypatch):
    left, right = FakeModule.pair()
    instance = make_app(tmp_path, monkeypatch, left, "RasPi", PI)
    instance.module = left
    return instance


@pytest.fixture
def no_asr(app):
    app.asr = NoEngine()
    return app


def sent_texts(app):
    return [m.text for m in app.history.messages if m.direction == h.TX]


def queued(app, text):
    return any(m.text == text for m in app.history.messages if m.direction == h.TX)


# MFruit OS input actions, named after the button gesture that makes them.
TAP, TWICE, HOLD, THRICE, QUAD = NEXT, PREVIOUS, SELECT, EXTRA, BACK
CODES = {"enter": 28, "escape": 1, "backspace": 14, "tab": 15, "up": 103, "down": 108,
         "space": 57}


def press(app, *names):
    for name in names:
        app._on_action(Action(name))


def key(app, name, action=DOWN):
    app.input.key_event(KeyEvent("key", name, action, CODES[name]))


def type_text(app, text):
    for char in text:
        if char == " ":
            key(app, "space")
            key(app, "space", UP)
        else:
            app.input.key_event(KeyEvent("char", char, DOWN, 30))


# --- what the chat shows ----------------------------------------------------------

def test_only_the_latest_ten_are_shown_sent_and_received(app):
    for n in range(14):
        direction = h.RX if n % 2 else h.TX
        app.history.add(h.Message(direction, n + 1, ORANGE, f"m{n}",
                                  h.RECEIVED if n % 2 else h.DELIVERED))
    view = app.view()
    assert [b.text for b in view.bubbles] == [f"m{n}" for n in range(4, 14)]
    assert [b.mine for b in view.bubbles] == [n % 2 == 0 for n in range(4, 14)]
    assert len(app.history) == 14                      # the rest is still kept


def test_the_title_is_the_other_radio(app):
    assert app.view().title == "Messenger"
    app.history.set_name(ORANGE, "OrangePi")           # a HELLO, before any message
    assert app.view().title == "OrangePi"
    app.history.set_name(0x0009, "Third")
    app.history.add(h.Message(h.RX, 1, ORANGE, "a", h.RECEIVED))
    app.history.add(h.Message(h.RX, 1, 0x0009, "b", h.RECEIVED))
    view = app.view()
    assert view.title == "2 radios"
    assert [b.sender for b in view.bubbles] == ["OrangePi", "Third"]


def test_sent_messages_say_how_they_are_doing(app):
    states = [h.QUEUED, h.SENDING, h.DELIVERED, h.FAILED]
    for n, status in enumerate(states):
        app.history.add(h.Message(h.TX, n + 1, protocol.BROADCAST, f"s{n}", status))
    metas = [(b.meta, b.meta_tone) for b in app.view().bubbles]
    assert metas[0][0].endswith("waiting…") and metas[0][1] == "busy"
    assert metas[1][0].endswith("sending…") and metas[1][1] == "busy"
    assert metas[2][0].endswith("✓") and metas[2][1] == "ok"
    assert metas[3][0].endswith("✗ not confirmed") and metas[3][1] == "error"


def test_the_hint_says_what_the_button_does_here(app, no_asr):
    app.asr = FakeEngine()
    assert app.view().hints == main_module.HINTS_TALK
    assert app.view().status == ""                     # nothing else going on
    assert app.view().empty_hint[1] == "Hold to talk"
    app.asr = NoEngine()
    assert app.view().hints == main_module.HINTS_NO_ASR
    assert app.view().empty_hint[1] == "Hold: quick replies"


# --- quick replies ------------------------------------------------------------------

def test_without_speech_recognition_a_hold_opens_the_replies(no_asr):
    app = no_asr
    press(app, HOLD)                                   # no voice: the hold picks
    assert app.picker is not None and app.picker.items[0] == "OK"
    press(app, TAP)                                    # tap: next
    assert app.picker.index == 1
    press(app, HOLD)                                   # hold: send it
    assert app.picker is None
    assert sent_texts(app) == ["Yes"]


def test_two_clicks_open_the_replies_and_four_go_back(app):
    press(app, TWICE)
    assert app.picker is not None
    assert app.view().picker is app.picker
    assert app.view().hints == main_module.HINTS_PICKER
    press(app, TAP, TAP, TWICE)                        # a list: 2 clicks go back up
    assert app.picker.index == 1
    press(app, QUAD)
    assert app.picker is None and sent_texts(app) == []


def test_the_footer_says_release_while_a_hold_is_armed(app):
    app.open_picker()
    app._on_armed(True)
    assert app.view().hints == [("release", "to send")]
    app._on_armed(False)
    assert app.view().hints == main_module.HINTS_PICKER


def test_the_list_wraps_and_closes_itself(app):
    app.open_picker()
    for _ in range(len(app.config.messaging.quick_replies)):
        press(app, TAP)
    assert app.picker.index == 0
    app._expire(now=time.monotonic() + main_module.PICKER_SECONDS + 1)
    assert app.picker is None


def test_a_failed_message_can_be_resent_from_the_list(app):
    failed = app.history.add(h.Message(h.TX, 5, protocol.BROADCAST, "lost", h.FAILED))
    press(app, TWICE)
    assert app.picker.items[0] == "↻ Resend: lost"
    press(app, HOLD)
    assert failed.status == h.QUEUED
    assert sent_texts(app) == ["lost"]                 # the same message, not a copy


def test_four_clicks_leave_the_app(app):
    press(app, QUAD)
    assert not app.running


def test_quick_replies_come_from_the_config(tmp_path, monkeypatch):
    import config as config_module
    path = tmp_path / "config.yaml"
    path.write_text("messaging:\n  quick_replies: [Yes, No, '  On  my way ', '', Yes]\n")
    assert config_module.load(path).messaging.quick_replies == ["Yes", "No", "On my way"]
    path.write_text("messaging:\n  quick_replies: []\n")
    assert config_module.load(path).messaging.quick_replies[0] == "OK"


# --- a keyboard on the board ----------------------------------------------------------

def test_typing_then_enter_sends(app):
    type_text(app, "Hi there")
    view = app.view()
    assert view.compose == "Hi there"
    assert view.hints == main_module.HINTS_TYPING
    key(app, "backspace")
    key(app, "enter")
    assert app.compose is None
    assert sent_texts(app) == ["Hi ther"]


def test_escape_cancels_and_a_leading_space_does_not_start(no_asr):
    app = no_asr                                       # no voice: Space is only a space
    type_text(app, " ")
    assert app.compose is None
    type_text(app, "oops")
    key(app, "escape")
    assert app.compose is None and app.running         # typing cancelled, still here
    key(app, "enter")                                  # nothing typed: the replies
    assert app.picker is not None and sent_texts(app) == []
    type_text(app, "x")
    assert app.picker is None and app.compose == "x"
    key(app, "backspace")                              # emptied: closed
    assert app.compose is None


def test_space_held_talks_on_the_chat(app):
    key(app, "space")
    assert app.listening
    time.sleep(0.5)                                    # longer than MIN_TALK_SECONDS
    key(app, "space", UP)
    assert not app.listening
    assert wait_for(lambda: queued(app, "Meet me at five o'clock"))


def test_space_types_once_typing_has_started(app):
    type_text(app, "on my way")
    assert app.compose == "on my way" and not app.listening


def test_escape_on_the_chat_leaves_the_app(app):
    key(app, "escape")
    assert not app.running


def test_four_clicks_while_typing_only_cancel_the_typing(app):
    type_text(app, "draft")
    press(app, QUAD)
    assert app.compose is None and app.running


def test_keys_move_through_the_list_and_the_chat(app):
    key(app, "tab")
    key(app, "down")
    key(app, "down")
    key(app, "up")
    assert app.picker.index == 1
    key(app, "enter")
    assert sent_texts(app) == ["Yes"]
    for n in range(3):
        app.history.add(h.Message(h.RX, n + 1, ORANGE, f"r{n}", h.RECEIVED))
    for _ in range(10):
        key(app, "up")
    assert app.selected().text == "Yes"                # the oldest; arrows stop there
    for _ in range(10):
        key(app, "down")
    assert app.scroll == 0


def test_typing_takes_over_from_the_list(app):
    app.open_picker()
    type_text(app, "ok")
    assert app.picker is None and app.compose == "ok"


def test_keys_are_ignored_without_the_screen(app):
    """In the background the keyboard belongs to whatever has the screen."""
    app.board.foreground_ready = False
    type_text(app, "hello")
    key(app, "enter")
    key(app, "escape")
    assert app.compose is None and sent_texts(app) == [] and app.running
    app.board.foreground_ready = True
    key(app, "enter", UP)                              # its press was not ours
    assert app.picker is None


def test_losing_the_screen_forgets_input_in_progress(app):
    key(app, "space")
    assert app.listening
    app._on_focus_revoked()                            # a held Space is let go of
    assert not app.listening
