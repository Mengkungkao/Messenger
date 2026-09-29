"""Sender (ACK/retry), receiver (ACK, dedupe, names) and history."""

import threading
import time

import pytest

from lora import packet as pk
from lora import protocol
from messaging import history as h
from messaging.receiver import Receiver
from messaging.sender import Sender
from tests.fakes import FakeLink

ME, THEM = 0x0001, 0x0002


def wait_for(condition, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def parts(tmp_path):
    link = FakeLink()
    history = h.History(tmp_path / "history.json")
    ids = protocol.MessageIds(tmp_path / "ids.json")
    sender = Sender(link, history, ME, ids, ack_timeout=0.15, max_retries=2, name="me")
    sender.start()
    yield link, history, sender
    sender.stop()


def test_delivered_when_the_ack_arrives(parts):
    link, history, sender = parts
    link.on_transmit = lambda p: p.type == pk.TEXT and threading.Timer(
        0.02, sender.handle_ack, [protocol.ack_packet(THEM, p)]).start()
    [message] = sender.send_text("Meet me at five o'clock")
    assert wait_for(lambda: message.status == h.DELIVERED)
    assert message.attempts == 1
    assert message.acked_by == THEM
    assert message.rtt_ms is not None and message.rtt_ms < 1000


def test_retries_then_fails_without_an_ack(parts):
    link, history, sender = parts
    [message] = sender.send_text("anyone?")
    assert wait_for(lambda: message.status == h.FAILED)
    assert message.attempts == 3                  # first try + 2 retries
    texts = [p for p in link.sent if p.type == pk.TEXT]
    assert len(texts) == 3
    assert len({p.msg_id for p in texts}) == 1    # retries reuse the ID


def test_delivered_on_a_retry(parts):
    link, history, sender = parts
    calls = []

    def ack_second(packet):
        calls.append(packet)
        if len(calls) == 2:
            sender.handle_ack(protocol.ack_packet(THEM, packet))

    link.on_transmit = ack_second
    [message] = sender.send_text("second time lucky")
    assert wait_for(lambda: message.status == h.DELIVERED)
    assert message.attempts == 2


def test_ack_from_the_wrong_radio_is_ignored(tmp_path):
    link = FakeLink()
    history = h.History(tmp_path / "h.json")
    sender = Sender(link, history, ME, protocol.MessageIds(), ack_timeout=0.1,
                    max_retries=0)
    sender.start()
    try:
        link.on_transmit = lambda p: sender.handle_ack(protocol.ack_packet(0x0009, p))
        [message] = sender.send_text("for 2 only", dst=THEM)
        assert wait_for(lambda: message.status == h.FAILED)
    finally:
        sender.stop()


def test_late_ack_still_marks_delivered(parts):
    link, history, sender = parts
    [message] = sender.send_text("slow")
    assert wait_for(lambda: message.status == h.FAILED)
    assert sender.handle_ack(protocol.ack_packet(THEM, link.sent[0]))
    assert message.status == h.DELIVERED


def test_duty_cycle_refusal_fails_at_once(tmp_path):
    history = h.History()
    sender = Sender(FakeLink(accept=False), history, ME, protocol.MessageIds(),
                    ack_timeout=5, max_retries=3)
    sender.start()
    try:
        [message] = sender.send_text("no budget")
        assert wait_for(lambda: message.status == h.FAILED, timeout=1.0)
        assert message.attempts == 1
    finally:
        sender.stop()


def test_long_transcripts_are_split_and_numbered(parts):
    link, history, sender = parts
    messages = sender.send_text("word " * 100)
    assert len(messages) == 3
    assert [m.part for m in messages] == ["1/3", "2/3", "3/3"]
    assert len({m.msg_id for m in messages}) == 3


def test_resend_only_failed_outgoing(parts):
    link, history, sender = parts
    [message] = sender.send_text("again")
    assert wait_for(lambda: message.status == h.FAILED)
    link.on_transmit = lambda p: sender.handle_ack(protocol.ack_packet(THEM, p))
    assert sender.resend(message)
    assert wait_for(lambda: message.status == h.DELIVERED)
    assert not sender.resend(message)


# --- receiver --------------------------------------------------------------------

@pytest.fixture
def receiver(tmp_path):
    link = FakeLink()
    history = h.History(tmp_path / "history.json")
    sender = Sender(link, history, ME, protocol.MessageIds(), name="me")
    got = []
    rx = Receiver(link, sender, history, ME, on_message=got.append)
    return rx, link, history, got


def test_text_is_acked_stored_and_announced(receiver):
    rx, link, history, got = receiver
    rx.handle(protocol.text_packet(THEM, protocol.BROADCAST, 1024, "Hello, how are you?"))
    ack = link.sent[0]                     # the ACK goes first
    assert (ack.type, ack.msg_id, ack.dst) == (pk.ACK, 1024, THEM)
    assert [m.text for m in got] == ["Hello, how are you?"]
    assert history.latest(h.RX).status == h.RECEIVED


def test_repeat_is_acked_again_but_shown_once(receiver):
    rx, link, history, got = receiver
    history.set_name(THEM, "OrangePi")
    packet = protocol.text_packet(THEM, ME, 5, "once")
    rx.handle(packet)
    rx.handle(packet)
    assert [p.type for p in link.sent] == [pk.ACK, pk.ACK]
    assert len(got) == 1 and len(history) == 1


def test_packets_for_someone_else_are_ignored(receiver):
    rx, link, history, got = receiver
    rx.handle(protocol.text_packet(THEM, 0x0003, 5, "not yours"))
    assert not link.sent and not got


def test_own_address_is_flagged_as_a_clash(receiver):
    rx, link, history, got = receiver
    rx.handle(protocol.text_packet(ME, protocol.BROADCAST, 5, "echo?"))
    assert rx.clashes == 1 and not got and not link.sent


def test_rssi_attaches_to_the_message_just_received(receiver):
    rx, link, history, got = receiver
    rx.handle(protocol.text_packet(THEM, ME, 9, "signal?"))
    rx.handle_rssi(-91)
    assert got[0].rssi == -91


def test_hello_learns_the_name_and_replies_once(receiver):
    rx, link, history, got = receiver
    rx.handle(protocol.hello_packet(THEM, 1, "OrangePi"))
    rx.handle(protocol.hello_packet(THEM, 2, "OrangePi"))
    assert history.name_for(THEM) == "OrangePi"
    replies = [p for p in link.sent if p.type == pk.HELLO_REPLY]
    assert len(replies) == 1 and replies[0].dst == THEM and replies[0].text == "me"


def test_hello_reply_is_never_answered(receiver):
    rx, link, history, got = receiver
    rx.handle(protocol.hello_packet(THEM, 1, "OrangePi", reply=True))
    assert history.name_for(THEM) == "OrangePi" and not link.sent


def test_unknown_sender_is_asked_for_its_name_once(receiver):
    """Regression: with both start-up HELLOs lost, names were never learned."""
    rx, link, history, got = receiver
    rx.handle(protocol.text_packet(THEM, ME, 1, "who am I?"))
    rx.handle(protocol.text_packet(THEM, ME, 2, "still me"))
    rx.handle(pk.Packet(pk.ACK, 99, THEM, ME))
    assert [p.type for p in link.sent] == [pk.ACK, pk.HELLO, pk.ACK]
    hello = link.sent[1]
    assert hello.dst == THEM and hello.text == "me"


def test_known_sender_is_not_asked(receiver):
    rx, link, history, got = receiver
    history.set_name(THEM, "OrangePi")
    rx.handle(protocol.text_packet(THEM, ME, 1, "hi"))
    assert [p.type for p in link.sent] == [pk.ACK]


def test_ping_is_acked_and_not_shown(receiver):
    rx, link, history, got = receiver
    rx.handle(protocol.ping_packet(THEM, ME, 77, padding=10))
    assert [p.type for p in link.sent] == [pk.ACK] and not got and not len(history)


# --- history ---------------------------------------------------------------------

def test_history_persists_names_and_messages(tmp_path):
    path = tmp_path / "history.json"
    history = h.History(path)
    history.set_name(THEM, "OrangePi")
    history.add(h.Message(h.RX, 3, THEM, "Café ✓", h.RECEIVED, rssi=-80))
    again = h.History(path)
    assert again.name_for(THEM) == "OrangePi"
    assert again.latest().text == "Café ✓" and again.latest().rssi == -80


def test_messages_in_flight_at_shutdown_load_as_failed(tmp_path):
    path = tmp_path / "history.json"
    history = h.History(path)
    history.add(h.Message(h.TX, 3, THEM, "cut off", h.SENDING))
    assert h.History(path).latest().status == h.FAILED


def test_history_is_bounded(tmp_path):
    history = h.History(tmp_path / "history.json", limit=5)
    for index in range(12):
        history.add(h.Message(h.RX, index, THEM, str(index), h.RECEIVED))
    assert [m.text for m in history.messages] == ["7", "8", "9", "10", "11"]


def test_corrupt_history_file_starts_empty(tmp_path):
    path = tmp_path / "history.json"
    path.write_text("{broken")
    assert len(h.History(path)) == 0


def test_name_fallbacks():
    history = h.History()
    assert history.name_for(0xFFFF) == "all"
    assert history.name_for(0x1A2B) == "#1A2B"


# --- ACK wait and air rate -------------------------------------------------------

def test_ack_wait_grows_as_the_air_rate_drops():
    from lora.link import Link

    class Radio:
        uart_baud = 9600

    full = pk.MAX_PACKET
    waits = [Link(Radio(), rate).round_trip_seconds(full, pk.OVERHEAD)
             for rate in (9600, 2400, 1200)]
    assert waits == sorted(waits)
    assert 0.5 < waits[0] < 1.2           # a full message and its ACK at 9.6k
    assert waits[2] > 2.0                 # most of the old fixed 3 s at 1.2k


def test_an_ack_still_on_the_air_is_not_resent_for(tmp_path, monkeypatch):
    import messaging.sender as sender_module
    monkeypatch.setattr(sender_module.random, "uniform", lambda a, b: 0.0)
    link = FakeLink()
    link.round_trip_seconds = lambda sent, reply: 0.3
    history = h.History()
    sender = Sender(link, history, ME, protocol.MessageIds(), ack_timeout=0.1,
                    max_retries=2)
    sender.start()
    try:
        link.on_transmit = lambda p: p.type == pk.TEXT and threading.Timer(
            0.25, sender.handle_ack, [protocol.ack_packet(THEM, p)]).start()
        [message] = sender.send_text("slow air")
        assert wait_for(lambda: message.status == h.DELIVERED)
        assert message.attempts == 1       # a bare 0.1 s wait would have resent
    finally:
        sender.stop()


def test_an_ack_just_as_the_sender_gives_up_still_counts(tmp_path):
    """The window between "failed" and "no longer in flight": an ACK there
    was taken for the pending message and lost."""
    link = FakeLink()
    history = h.History()
    sender = Sender(link, history, ME, protocol.MessageIds(), ack_timeout=0.05,
                    max_retries=0)
    acked = []

    def ack_the_moment_it_fails(message):
        if message.status == h.FAILED and not acked:
            acked.append(sender.handle_ack(protocol.ack_packet(THEM, link.sent[0])))

    history.subscribe(ack_the_moment_it_fails)
    sender.start()
    try:
        [message] = sender.send_text("just in time")
        assert wait_for(lambda: acked)
        assert acked == [True] and message.status == h.DELIVERED
    finally:
        sender.stop()
