"""The chat: what is shown, the one-button gestures, quick replies, typing."""

import time

import pytest

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
    assert app.view().title == "LoRa Messenger"
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
    assert app.view().status == "Hold: talk · 2×: replies"
    assert app.view().empty_hint[1] == "Hold: talk"
    app.asr = NoEngine()
    assert app.view().status == "Hold or 2×: quick reply"
    assert app.view().empty_hint[1] == "Hold or 2 clicks: quick reply"


# --- quick replies ------------------------------------------------------------------

def test_without_speech_recognition_a_hold_opens_the_replies(no_asr):
    app = no_asr
    app._on_talk_start()
    assert app.picker is not None and app.picker.items[0] == "OK"
    app._on_talk_end(held_seconds=1.0)                 # letting go sends nothing
    assert sent_texts(app) == []
    app._on_gesture("single")                          # click: next
    assert app.picker.index == 1
    app._on_talk_start()                               # hold: send it
    app._on_talk_end(held_seconds=1.0)
    assert app.picker is None
    assert sent_texts(app) == ["Yes"]


def test_two_clicks_open_and_close_the_replies(app):
    app._on_gesture("double")
    assert app.picker is not None
    assert app.view().picker is app.picker
    assert app.view().status == "Click: next · Hold: send"
    app._on_gesture("single")
    app._on_gesture("single")
    app._on_gesture("double")
    assert app.picker is None and sent_texts(app) == []


def test_the_list_wraps_and_closes_itself(app):
    app.open_picker()
    for _ in range(len(app.config.messaging.quick_replies)):
        app._on_gesture("single")
    assert app.picker.index == 0
    app._expire(now=time.monotonic() + main_module.PICKER_SECONDS + 1)
    assert app.picker is None


def test_a_failed_message_can_be_resent_from_the_list(app):
    failed = app.history.add(h.Message(h.TX, 5, protocol.BROADCAST, "lost", h.FAILED))
    app._on_gesture("double")
    assert app.picker.items[0] == "↻ Resend: lost"
    app._on_talk_start()
    assert failed.status == h.QUEUED
    assert sent_texts(app) == ["lost"]                 # the same message, not a copy


def test_quick_replies_come_from_the_config(tmp_path, monkeypatch):
    import config as config_module
    path = tmp_path / "config.yaml"
    path.write_text("messaging:\n  quick_replies: [Yes, No, '  On  my way ', '', Yes]\n")
    assert config_module.load(path).messaging.quick_replies == ["Yes", "No", "On my way"]
    path.write_text("messaging:\n  quick_replies: []\n")
    assert config_module.load(path).messaging.quick_replies[0] == "OK"


# --- a keyboard on the board ----------------------------------------------------------

def type_text(app, text):
    for char in text:
        app._on_char(char)


def test_typing_then_enter_sends(app):
    type_text(app, "Hi there")
    view = app.view()
    assert view.compose == "Hi there"
    assert view.status == "Enter: send · Esc: cancel"
    app._on_key("backspace")
    app._on_key("enter")
    assert app.compose is None
    assert sent_texts(app) == ["Hi ther"]


def test_escape_cancels_and_a_leading_space_does_not_start(app):
    app._on_char(" ")
    assert app.compose is None
    type_text(app, "oops")
    app._on_key("escape")
    assert app.compose is None
    app._on_key("enter")
    assert sent_texts(app) == []
    type_text(app, "x")
    app._on_key("backspace")                           # emptied: closed
    assert app.compose is None


def test_keys_move_through_the_list_and_the_chat(app):
    app._on_key("tab")
    app._on_key("down")
    app._on_key("down")
    app._on_key("up")
    assert app.picker.index == 1
    app._on_key("enter")
    assert sent_texts(app) == ["Yes"]
    for n in range(3):
        app.history.add(h.Message(h.RX, n + 1, ORANGE, f"r{n}", h.RECEIVED))
    for _ in range(10):
        app._on_key("up")
    assert app.selected().text == "Yes"                # the oldest; arrows stop there
    for _ in range(10):
        app._on_key("down")
    assert app.scroll == 0


def test_typing_takes_over_from_the_list(app):
    app.open_picker()
    type_text(app, "ok")
    assert app.picker is None and app.compose == "ok"


def test_keys_are_ignored_without_the_screen(app):
    app.board.foreground_ready = False
    type_text(app, "hello")
    app._on_key("enter")
    assert app.compose is None and sent_texts(app) == []
