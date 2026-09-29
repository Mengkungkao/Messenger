"""Two radios, end to end: driver, deframer, link, sender, receiver, app.

The modules are fakes (tests/fakes.py) that behave like E22s in
fixed-point mode -- they eat the 3-byte address header and append an
RSSI byte -- and everything above them is the production code.
"""

import time

import pytest

import config as config_module
from display.board import NullBoard
from lora import protocol
from lora.link import Link
from main import Messenger
from messaging import history as h
from messaging.receiver import Receiver
from messaging.sender import Sender
from tests.fakes import FakeEngine, FakeModule, FakeRecorder, FakeSX126x, LossyModule

PI, ORANGE = 0x0001, 0x0002


def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


class Station:
    def __init__(self, module, address, name, ack_timeout=0.3):
        self.radio = FakeSX126x(module)
        self.link = Link(self.radio, duty_cycle_percent=100)
        self.history = h.History()
        self.sender = Sender(self.link, self.history, address, protocol.MessageIds(),
                             ack_timeout=ack_timeout, max_retries=3, name=name)
        self.inbox = []
        self.receiver = Receiver(self.link, self.sender, self.history, address,
                                 on_message=self.inbox.append)
        self.link.on_packet = self.receiver.handle
        self.link.on_rssi = self.receiver.handle_rssi
        self.link.start()
        self.sender.start()

    def close(self):
        self.sender.stop()
        self.link.stop()
        self.radio.close()


@pytest.fixture
def pair():
    left, right = FakeModule.pair()
    left.addr, right.addr = PI, ORANGE
    stations = Station(left, PI, "RasPi"), Station(right, ORANGE, "OrangePi")
    yield stations
    for station in stations:
        station.close()


def test_message_is_delivered_and_acknowledged(pair):
    pi, orange = pair
    [message] = pi.sender.send_text("Meet me at five o'clock")
    assert wait_for(lambda: message.status == h.DELIVERED)
    assert [m.text for m in orange.inbox] == ["Meet me at five o'clock"]
    assert orange.inbox[0].peer == PI
    assert wait_for(lambda: orange.inbox[0].rssi == -91)   # the fake reports -91 dBm
    assert message.acked_by == ORANGE and message.attempts == 1


def test_hello_exchanges_names(pair):
    pi, orange = pair
    pi.sender.send_hello()
    assert wait_for(lambda: orange.history.name_for(PI) == "RasPi")
    assert wait_for(lambda: pi.history.name_for(ORANGE) == "OrangePi")


def test_both_directions_at_once(pair):
    pi, orange = pair
    a = pi.sender.send_text("from the Pi")
    b = orange.sender.send_text("from the Orange Pi")
    assert wait_for(lambda: a[0].status == h.DELIVERED and b[0].status == h.DELIVERED)
    assert [m.text for m in pi.inbox] == ["from the Orange Pi"]
    assert [m.text for m in orange.inbox] == ["from the Pi"]


def test_lost_packet_is_retried_and_shown_once():
    lossy = LossyModule("pi", addr=PI, drop_indices={0})   # first transmission lost
    other = FakeModule("orange", addr=ORANGE)
    lossy.peers.append(other)
    other.peers.append(lossy)
    pi, orange = Station(lossy, PI, "RasPi"), Station(other, ORANGE, "OrangePi")
    try:
        [message] = pi.sender.send_text("try again")
        assert wait_for(lambda: message.status == h.DELIVERED)
        assert message.attempts == 2
        assert len(orange.inbox) == 1
    finally:
        pi.close()
        orange.close()


def test_lost_ack_leads_to_a_repeat_that_is_not_shown_twice():
    pi_module = FakeModule("pi", addr=PI)
    orange_module = LossyModule("orange", addr=ORANGE, drop_indices={0})  # first ACK lost
    pi_module.peers.append(orange_module)
    orange_module.peers.append(pi_module)
    pi, orange = Station(pi_module, PI, "RasPi"), Station(orange_module, ORANGE, "OrangePi")
    try:
        [message] = pi.sender.send_text("did you get that?")
        assert wait_for(lambda: message.status == h.DELIVERED)
        assert message.attempts == 2
        assert len(orange.inbox) == 1 and len(orange.history) == 1
    finally:
        pi.close()
        orange.close()


# --- the whole app ---------------------------------------------------------------

def make_app(tmp_path, monkeypatch, module, name, address):
    monkeypatch.setenv("MESSENGER_DATA_DIR", str(tmp_path / name))
    config = config_module.load(tmp_path / "missing.yaml")
    config.identity.name = name
    config.radio.address = address
    config.input.keyboard = "off"
    config.input.physical_keyboard = False   # never read the test machine's keyboard
    config.asr.engine = "none"
    config.messaging.ack_timeout_seconds = 0.3
    config.ui.idle_dim_seconds = 0
    app = Messenger(config, board=NullBoard(), radio=FakeSX126x(module))
    app.recorder = FakeRecorder()
    app.asr = FakeEngine("Meet me at five o'clock")
    app.player.device = None             # no beeps from the test machine
    return app


def last(app):
    """The newest bubble in the chat, or an empty one."""
    from display.whisplay import Bubble
    bubbles = app.view().bubbles
    return bubbles[-1] if bubbles else Bubble("", False)


def test_final_demonstration(tmp_path, monkeypatch):
    """The brief's demo: speak on the Pi, read it on the Orange Pi, see it delivered."""
    import threading

    left, right = FakeModule.pair()
    pi = make_app(tmp_path, monkeypatch, left, "RasPi", PI)
    orange = make_app(tmp_path, monkeypatch, right, "OrangePi", ORANGE)
    threads = [threading.Thread(target=app.run, daemon=True) for app in (pi, orange)]
    for thread in threads:
        thread.start()
    try:
        # HELLOs at start-up teach each radio the other's name.
        assert wait_for(lambda: pi.history.name_for(ORANGE) == "OrangePi")

        pi._on_talk_start()
        assert pi.view().status.startswith("Listening")
        pi._on_talk_end(held_seconds=2.0)

        # Pi: recognised text shown locally, on the right, then delivered.
        assert wait_for(lambda: last(pi).meta.endswith("✓"))
        view = pi.view()
        assert view.bubbles[-1].text == "Meet me at five o'clock"
        assert view.bubbles[-1].mine and view.bubbles[-1].meta_tone == "ok"
        assert view.title == "OrangePi"

        # Orange Pi: received, validated, on the left, under the sender's name.
        view = orange.view()
        assert view.title == "RasPi"
        assert view.bubbles[-1].text == "Meet me at five o'clock"
        assert not view.bubbles[-1].mine
        assert wait_for(lambda: "-91 dBm" in last(orange).meta)
    finally:
        pi.stop()
        orange.stop()
        for thread in threads:
            thread.join(timeout=5)


def test_silence_is_not_sent(tmp_path, monkeypatch):
    left, right = FakeModule.pair()
    app = make_app(tmp_path, monkeypatch, left, "RasPi", PI)
    app.asr = FakeEngine("")
    app._on_talk_start()
    app._on_talk_end(held_seconds=2.0)
    assert wait_for(lambda: not app.transcribing)
    assert app.view().status == "No speech heard"
    assert len(app.history) == 0
    assert not left.transmitted


def test_slip_of_the_finger_is_not_transcribed(tmp_path, monkeypatch):
    left, _ = FakeModule.pair()
    app = make_app(tmp_path, monkeypatch, left, "RasPi", PI)
    app._on_talk_start()
    app._on_talk_end(held_seconds=0.1)
    assert not app.asr.clips
    assert app.view().status.startswith("Too short")


def test_scrolling_back_and_resending_a_failed_message(tmp_path, monkeypatch):
    left, _ = FakeModule.pair()
    app = make_app(tmp_path, monkeypatch, left, "RasPi", PI)
    for index in range(3):
        app.history.add(h.Message(h.RX, index + 1, ORANGE, f"msg {index}", h.RECEIVED))
    failed = app.history.add(h.Message(h.TX, 9, protocol.BROADCAST, "lost", h.FAILED))
    view = app.view()
    assert [b.text for b in view.bubbles] == ["msg 0", "msg 1", "msg 2", "lost"]
    assert view.anchor is None and view.position == ""
    assert view.bubbles[-1].failed and "not confirmed" in view.bubbles[-1].meta
    app._on_gesture("single")
    view = app.view()
    assert view.anchor == 2 and view.position == "3/4" and view.bubbles[2].selected
    app._on_gesture("single")
    app._on_gesture("single")
    assert app.selected().text == "msg 0"
    app._on_gesture("single")
    assert app.scroll == 0 and app.selected() is None     # past the oldest: back to now
    # A reply arrives after the failed message; one click back selects it.
    app.history.add(h.Message(h.RX, 7, ORANGE, "after", h.RECEIVED))
    app._on_gesture("single")
    assert app.selected() is failed
    assert app.view().status == "2×: resend · 1×: older"
    app.sender.start()
    try:
        app._on_gesture("double")
        assert app.scroll == 0 and app.picker is None
        assert wait_for(lambda: failed.status in (h.SENDING, h.FAILED) and failed.attempts)
    finally:
        app.sender.stop()
