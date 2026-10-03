"""The screens and button flows for SOS, an alarm heard, and pairing."""

import time

import pytest
from mfruit_sdk.keys import DOWN, KeyEvent

from lora import packet as pk
from lora import protocol
from messaging import emergency as em
from messaging import history as h
from tests.fakes import FakeModule
from tests.test_chat import BACK_ACTION, CODES, HOLD, QUAD, TAP, TWICE, key, press  # noqa: F401
from tests.test_end_to_end import ORANGE, PI, make_app, wait_for

pytest.importorskip("mfruit_sdk.radio.keyring", reason="needs cryptography")


@pytest.fixture
def app(tmp_path, monkeypatch):
    left, right = FakeModule.pair()
    instance = make_app(tmp_path, monkeypatch, left, "RasPi", PI)
    instance.config.emergency.countdown_seconds = 3
    instance.module = left
    instance.other = right
    return instance


def open_sos_menu(app):
    press(app, TWICE, TWICE)                # open the replies; a second 2x wraps to the last row
    assert app.picker.items[app.picker.index] == "SOS emergency"
    press(app, HOLD)
    return app.menu


# ============================================================ sending
def test_the_sos_row_is_last_in_the_replies_and_asks_before_anything_is_sent(app):
    menu = open_sos_menu(app)
    assert menu.kind == "sos" and not app.emergency.active
    labels = [label for label, _ in app._menu_items()]
    assert labels[0] == "Cancel", "the safe row is the first one"
    press(app, QUAD)                        # four clicks: out of the question
    assert app.menu is None and not app.emergency.active
    assert app.emergency.outgoing is None


def test_sos_counts_down_and_four_clicks_cancel_it_without_sending(app):
    open_sos_menu(app)
    press(app, TAP)
    press(app, HOLD)                        # "Send SOS in 3 s"
    assert app.menu.kind == "countdown" and app.menu.tone == "alarm"
    assert menu_title(app).startswith("SOS in ")
    press(app, QUAD)
    assert app.menu is None and app.emergency.outgoing is None
    assert app._status()[0] == "SOS cancelled"


def test_sos_is_sent_when_the_countdown_ends_and_leaving_is_blocked_until_it_stops(app):
    open_sos_menu(app)
    press(app, TAP)
    press(app, HOLD)
    app._sync(app._countdown_until + 0.01)
    assert app.emergency.active and app.menu is None
    assert app._status() == ("SOS · calling for help…", "alarm")
    assert any(m.kind == "sos" and m.direction == h.TX for m in app.history.messages)
    press(app, QUAD)                        # four clicks must not leave the app
    assert app.running and app.menu.kind == "sos" and app.menu.title == "SOS is active"
    press(app, HOLD)                        # "Keep sending" (the first row)
    assert app.emergency.active and app.running


def test_im_ok_stops_the_sos_and_allows_leaving_again(app):
    app._start_sos()
    press(app, QUAD)
    press(app, TAP)
    press(app, HOLD)                        # "I'm OK - stop SOS"
    assert not app.emergency.active
    press(app, QUAD)
    assert not app.running


def test_the_footer_and_status_follow_who_heard_the_sos(app):
    app._start_sos()
    app.emergency.outgoing.ackers.add(2)
    assert app._status() == ("SOS · heard by 1", "alarm")
    app._on_emergency_change()
    assert [m.status for m in app.history.messages if m.kind == "sos"] == [h.DELIVERED]


def menu_title(app):
    from controls import menus
    return str(menus.resolve(app.menu.title))


# ============================================================ hearing one
def sos_packet(src=ORANGE, msg_id=9, name="Blake", battery=41, place="Creek crossing",
               message="Broken ankle"):
    return pk.Packet(pk.SOS, msg_id, src, protocol.BROADCAST,
                     em.sos_body(name, battery, place, message))


def test_an_sos_heard_takes_the_screen_with_who_where_and_how_to_answer(app):
    app.receiver.handle(sos_packet())
    app._sync(time.monotonic())
    assert app.menu.kind == "alarm" and menu_title(app) == "SOS: Blake"
    view = app.view()
    assert view.picker.tone == "alarm" and view.status_tone == "alarm"
    assert "Broken ankle" in view.picker.lines and "Creek crossing" in view.picker.lines
    assert "Battery 41%" in view.picker.lines
    assert view.picker.items == ["On my way", "I'm calling for help", "Dismiss alarm"]


def test_answering_replies_to_the_sender_and_clears_the_alarm(app):
    app.receiver.handle(sos_packet())
    app._sync(time.monotonic())
    press(app, HOLD)                        # "On my way"
    assert any(m.text == "On my way" and m.peer == ORANGE for m in app.history.messages
               if m.direction == h.TX)
    app._sync(time.monotonic())
    assert app.menu is None and app.emergency.active_alarms() == []


def test_typing_or_stray_keys_never_dismiss_an_alarm(app):
    app.receiver.handle(sos_packet())
    app._sync(time.monotonic())
    key(app, "tab")
    app.input.key_event(KeyEvent("char", "x", DOWN, 30))
    assert app.menu.kind == "alarm" and app.compose is None


def test_four_clicks_dismiss_it_but_it_stays_in_the_chat_log(app):
    app.receiver.handle(sos_packet())
    app._sync(time.monotonic())
    press(app, QUAD)
    app._sync(time.monotonic())
    assert app.menu is None
    assert any(m.kind == "sos" and "Broken ankle" in m.text for m in app.history.messages)


def test_a_cleared_alarm_closes_and_says_so(app):
    app.receiver.handle(sos_packet())
    app._sync(time.monotonic())
    app.receiver.handle(pk.Packet(pk.SOS_CLEAR, 9, ORANGE, protocol.BROADCAST))
    app._sync(time.monotonic())
    assert app.menu is None and app._status() == ("Blake: I'm OK", "ok")


def test_our_own_sos_comes_before_an_alarm_from_someone_else(app):
    app._start_sos()
    app.receiver.handle(sos_packet())
    app._sync(time.monotonic())
    assert app.menu is None
    app.emergency.clear()
    for _ in range(3):
        app.emergency.outgoing.last_sent = -1e9
        app.emergency.step()
    app._sync(time.monotonic())
    assert app.menu.kind == "alarm"        # the alarm that waited


def test_the_alarm_repeats_its_sound_until_answered(app):
    beeps = []
    app.player.cue = beeps.append
    app.receiver.handle(sos_packet())
    now = time.monotonic()
    app._sync(now)
    app._sync(now + 1)
    assert beeps == ["alarm"]
    app._sync(now + 7)
    assert beeps == ["alarm", "alarm"]


# ============================================================== pairing
def test_pair_a_radio_is_in_the_list_and_its_screen_explains_what_to_do(app):
    press(app, TWICE, TWICE, TWICE)         # open, SOS (last), then Pair a radio before it
    assert app.picker.items[app.picker.index] == "Pair a radio"
    press(app, HOLD)
    assert app.menu.kind == "pair" and app.pairing.active
    view = app.view()
    assert view.picker.title == "Pair a radio"
    assert any("other radio" in line for line in view.picker.lines)
    assert view.picker.items == ["Cancel"]
    press(app, HOLD)
    assert app.menu is None and not app.pairing.active


def test_the_pairing_window_closing_returns_to_the_chat_with_a_message(app):
    app.open_menu(__import__("controls.menus", fromlist=["x"]).pair_menu(app))
    app.pairing.clock = lambda: app.pairing.until + 1      # the window has run out
    app._sync(time.monotonic())
    assert app.menu is None
    assert app._flash[0] == "Pairing timed out"


def test_paired_radios_are_listed_and_can_be_unpaired(app, tmp_path):
    from controls import menus
    app.security.keyring.add_peer(ORANGE, bytes(32), bytes(32))
    app.security.contacts.set(ORANGE, "Blake")
    app.open_menu(menus.paired_menu(app))
    assert [label for label, _ in app._menu_items()] == ["Blake", "Back"]
    press(app, HOLD)
    assert app.menu.kind == "confirm" and menu_title(app) == "Unpair Blake?"
    press(app, TAP, HOLD)                   # "Unpair"
    assert not app.security.is_paired(ORANGE) and app.security.contacts.all() == {}


def test_a_plain_message_from_a_paired_radio_is_flagged_not_encrypted(app):
    app.security.keyring.add_peer(ORANGE, bytes(32), bytes(32))
    app.receiver.handle(protocol.text_packet(ORANGE, PI, 5, "send money"))
    bubble = app.view().bubbles[-1]
    assert "NOT encrypted" in bubble.meta and bubble.meta_tone == "error"


def test_the_whole_screen_renders_in_every_new_state(app, tmp_path):
    from controls import menus
    from display.whisplay import render
    app.receiver.handle(sos_packet())
    app._sync(time.monotonic())
    render(app.view()).save(tmp_path / "alarm.png")
    app.emergency.dismiss(app.emergency.active_alarms()[0])
    app._sync(time.monotonic())
    app.open_menu(menus.pair_menu(app))
    render(app.view()).save(tmp_path / "pair.png")
    app.close_menu()
    app._start_sos()
    render(app.view()).save(tmp_path / "sos-active.png")
    assert (tmp_path / "alarm.png").stat().st_size > 1000
