"""Pairing, encrypted messages and SOS, between simulated radios.

Everything above the fake modules is production code: the driver,
deframer, link, sender, receiver, pairing and emergency modules, and the
keys of the shared radio store (one directory per simulated radio).
"""

import time

import pytest

from lora import packet as pk
from lora import protocol
from lora.link import Link
from messaging import emergency as em
from messaging import history as h
from messaging.pairing import Pairing
from messaging.receiver import Receiver
from messaging.security import Security
from messaging.sender import Sender
from tests.fakes import FakeLink, FakeModule, FakeSX126x
from tests.test_end_to_end import wait_for

crypto_tests = pytest.importorskip("mfruit_sdk.radio.keyring", reason="needs cryptography")
from mfruit_sdk.radio.contacts import Contacts  # noqa: E402
from mfruit_sdk.radio.keyring import Keyring  # noqa: E402


class Cfg:
    message, place = "Need help", "North ridge camp"
    first_interval_seconds, interval_seconds, max_hours = 10.0, 60.0, 12.0


class Station:
    """One simulated radio with its own shared-store directory."""

    def __init__(self, module, address, name, tmp_path, ack_timeout=0.3, secure=True):
        store = tmp_path / name
        self.security = Security(Keyring(str(store)), Contacts(str(store))) if secure else None
        self.radio = FakeSX126x(module)
        self.link = Link(self.radio, duty_cycle_percent=100)
        self.history = h.History()
        self.ids = protocol.MessageIds()
        self.sender = Sender(self.link, self.history, address, self.ids,
                             ack_timeout=ack_timeout, max_retries=2, name=name,
                             security=self.security)
        self.inbox = []
        self.alarms = []
        self.emergency = em.Emergency(self.link, address, self.ids, Cfg, name=name,
                                      battery=lambda: 87, on_alarm=self.alarms.append)
        self.pairing = (Pairing(self.link, self.security, address, name, self.ids, self.history)
                        if secure else None)
        self.receiver = Receiver(self.link, self.sender, self.history, address,
                                 on_message=self.inbox.append, security=self.security,
                                 pairing=self.pairing, emergency=self.emergency)
        self.link.on_packet = self.receiver.handle
        self.link.on_rssi = self.receiver.handle_rssi
        self.link.start()
        self.sender.start()
        self.address = address

    def close(self):
        self.sender.stop()
        self.emergency.stop_worker()
        self.link.stop()
        self.radio.close()


@pytest.fixture
def pair(tmp_path):
    left, right = FakeModule.pair()
    left.addr, right.addr = 1, 2
    a, b = Station(left, 1, "Alex", tmp_path), Station(right, 2, "Blake", tmp_path)
    yield a, b
    a.close()
    b.close()


def pair_them(a, b):
    """The whole pairing, as the two people would do it."""
    a.pairing.start()
    b.pairing.start()
    assert wait_for(lambda: 2 in a.pairing.found and 1 in b.pairing.found)
    assert a.pairing.request(2)
    assert wait_for(lambda: b.pairing.incoming is not None)
    assert a.pairing.outgoing.code == b.pairing.incoming.code, "both screens show one code"
    assert b.pairing.accept()
    assert wait_for(lambda: a.pairing.result.startswith("Paired")
                    and b.pairing.result.startswith("Paired"))
    assert a.security.is_paired(2) and b.security.is_paired(1)


# ============================================================== pairing
def test_two_radios_pair_with_matching_codes_and_names(pair):
    a, b = pair
    pair_them(a, b)
    assert a.security.contacts.all() == {2: "Blake"}
    assert b.security.contacts.all() == {1: "Alex"}
    assert a.pairing.result == "Paired with Blake" and b.pairing.result == "Paired with Alex"
    assert a.pairing.found == {} and a.pairing.outgoing is None


def test_pairing_packets_are_ignored_unless_the_window_is_open(pair):
    a, b = pair
    a.pairing.start()                      # only Alex is pairing
    time.sleep(0.4)
    assert b.pairing.found == {} and a.pairing.found == {}
    assert not b.security.is_paired(1)


def test_a_substituted_key_shows_a_different_code(pair, tmp_path):
    a, b = pair
    mallory = Keyring(str(tmp_path / "mallory"))
    a.pairing.start()
    a.pairing.found[2] = type("F", (), dict(address=2, name="Blake", public=mallory.public,
                                           heard=time.monotonic(), rssi=None))()
    a.pairing.request(2)
    assert a.pairing.outgoing.code != b.security.keyring.code_with(a.security.keyring.public)


def test_refusing_pairs_nothing(pair):
    a, b = pair
    a.pairing.start()
    b.pairing.start()
    assert wait_for(lambda: 2 in a.pairing.found)
    a.pairing.request(2)
    assert wait_for(lambda: b.pairing.incoming is not None)
    b.pairing.reject()
    time.sleep(0.3)
    assert not a.security.is_paired(2) and not b.security.is_paired(1)


def test_the_window_closes_by_itself():
    clock = [0.0]
    security = Security(_ring(), None)
    pairing = Pairing(FakeLink(), security, 1, "A", protocol.MessageIds(), clock=lambda: clock[0])
    pairing.start()
    assert pairing.active
    clock[0] = 1000
    pairing.tick()
    assert not pairing.active and pairing.result == "Pairing timed out"


def _ring():
    import tempfile
    return Keyring(tempfile.mkdtemp(prefix="mfruit-test-keys-"))


# ======================================================= encrypted chat
def test_paired_radios_exchange_sealed_messages(pair):
    a, b = pair
    pair_them(a, b)
    a.sender.send_text("Meet at the bridge", 2)
    assert wait_for(lambda: b.inbox)
    received = b.inbox[0]
    assert (received.text, received.secure) == ("Meet at the bridge", True)
    assert wait_for(lambda: a.history.messages[-1].status == h.DELIVERED)
    assert a.history.messages[-1].secure
    on_air = [pk.decode(frame) for frame in _frames(a.radio)]
    assert any(p.type == pk.SECURE for p in on_air)
    assert not any(b"bridge" in p.payload for p in on_air if p), "nothing readable on the air"


def _frames(radio):
    return [bytes(frame) for frame in radio._module.transmitted]


def test_broadcast_to_everyone_paired_uses_the_broadcast_key(pair):
    a, b = pair
    pair_them(a, b)
    a.sender.send_text("Everyone: lunch", protocol.BROADCAST)
    assert wait_for(lambda: b.inbox)
    assert b.inbox[0].secure and b.inbox[0].text == "Everyone: lunch"


def test_unpaired_radios_still_chat_in_the_clear_and_it_is_marked(pair):
    a, b = pair
    a.sender.send_text("hello", protocol.BROADCAST)
    assert wait_for(lambda: b.inbox)
    assert not b.inbox[0].secure and not a.history.messages[-1].secure


def test_a_sealed_message_nobody_can_open_is_neither_shown_nor_acked(pair, tmp_path):
    a, b = pair
    pair_them(a, b)
    stranger = Security(Keyring(str(tmp_path / "stranger")))
    packet, sealed = stranger.text_packet(1, 2, 77, "secret") if stranger.can_seal_to(2) \
        else (None, False)
    assert not sealed                      # the stranger holds no key for Blake...
    forged = pk.Packet(pk.SECURE, 77, 1, 2, b"\x00" * 40)    # ...so only noise can be sent
    before = len(b.radio._module.transmitted)
    b.receiver.handle(forged)
    assert b.inbox == [] and b.receiver.undecryptable == 1
    assert len(b.radio._module.transmitted) == before, "no ACK for what could not be read"


def test_tampering_with_a_sealed_packet_is_detected(pair):
    a, b = pair
    pair_them(a, b)
    packet, sealed = a.security.text_packet(1, 2, 5, "pay 10")
    assert sealed
    for altered in (pk.Packet(packet.type, packet.msg_id, packet.src, 1, packet.payload),
                    pk.Packet(packet.type, packet.msg_id + 1, packet.src, packet.dst, packet.payload),
                    pk.Packet(packet.type, packet.msg_id, packet.src, packet.dst,
                              packet.payload[:-1] + bytes([packet.payload[-1] ^ 1]))):
        assert b.security.open(altered) is None
    assert b.security.open(packet) == "pay 10"


def test_a_long_sealed_message_is_split_to_fit_one_packet(pair):
    a, b = pair
    pair_them(a, b)
    parts = a.sender.send_text("word " * 120, 2)
    assert len(parts) > 1
    assert all(len(a.security.text_packet(1, 2, m.msg_id, m.text)[0].payload) <= pk.MAX_MESSAGE
               for m in parts)


def test_a_queued_encrypted_message_is_never_sent_in_the_clear_if_its_key_goes(pair):
    a, b = pair
    pair_them(a, b)
    [message] = a.sender.send_text("secret plans", 2)
    a.sender.stop()
    a.security.keyring.remove_peer(2)
    assert a.sender._packet_for(message) is None


def test_security_is_optional_for_radios_without_cryptography(tmp_path):
    left, right = FakeModule.pair()
    left.addr, right.addr = 1, 2
    plain = Station(left, 1, "Old", tmp_path, secure=False)
    other = Station(right, 2, "New", tmp_path)
    try:
        plain.sender.send_text("works", protocol.BROADCAST)
        assert wait_for(lambda: other.inbox) and not other.inbox[0].secure
    finally:
        plain.close()
        other.close()


# ================================================================ SOS
def test_sos_is_heard_acked_and_shown_once_however_often_it_repeats(pair):
    a, b = pair
    assert a.emergency.start()
    a.emergency.step()
    assert wait_for(lambda: b.alarms and a.emergency.heard_by == 1)
    alarm = b.alarms[0]
    assert (alarm.name, alarm.battery, alarm.place, alarm.message) == (
        "Alex", 87, "North ridge camp", "Need help")
    b.emergency.dismiss(alarm)
    a.emergency.outgoing.last_sent = -1e9           # repeat now
    a.emergency.step()
    assert wait_for(lambda: alarm.copies == 2)
    assert len(b.alarms) == 1 and b.emergency.active_alarms() == [], "dismissed stays dismissed"


def test_sos_repeats_fast_until_answered_then_slowly(pair):
    a, b = pair
    cfg = Cfg
    a.emergency.start()
    assert a.emergency.interval() == cfg.first_interval_seconds
    a.emergency.outgoing.ackers.add(2)
    assert a.emergency.interval() == cfg.interval_seconds


def test_sos_sends_on_schedule_and_ignores_the_duty_cycle():
    link, clock = FakeLink(), [100.0]
    sos = em.Emergency(link, 1, protocol.MessageIds(), Cfg, name="Alex", clock=lambda: clock[0],
                       rng=_NoJitter())
    sos.start()
    sos.step()
    assert len(link.sent) == 1 and link.sent[0].type == pk.SOS and link.forced == link.sent
    sos.step()                              # not due yet
    clock[0] += 9
    sos.step()
    assert len(link.sent) == 1
    clock[0] += 2
    sos.step()
    assert len(link.sent) == 2 and link.sent[0].msg_id == link.sent[1].msg_id


class _NoJitter:
    @staticmethod
    def uniform(a, b):
        return 0.0


def test_im_ok_ends_the_alarm_on_the_other_radio(pair):
    a, b = pair
    a.emergency.start()
    a.emergency.step()
    assert wait_for(lambda: b.alarms)
    a.emergency.clear()
    for _ in range(3):
        a.emergency.outgoing.last_sent = -1e9
        a.emergency.step()
    assert not a.emergency.active and a.emergency.outgoing.done
    assert wait_for(lambda: b.alarms[0].cleared)
    assert b.emergency.active_alarms() == []


def test_sos_stops_after_the_time_limit_so_a_forgotten_one_does_not_drain_the_battery():
    link, clock = FakeLink(), [0.0]
    sos = em.Emergency(link, 1, protocol.MessageIds(), Cfg, clock=lambda: clock[0])
    sos.start()
    sos.step()
    clock[0] = Cfg.max_hours * 3600 + 1
    sos.step()
    assert sos.outgoing.done and "time limit" in sos.outgoing.note
    count = len(link.sent)
    clock[0] += 100
    sos.step()
    assert len(link.sent) == count


def test_a_second_sos_from_the_same_radio_replaces_its_alarm(pair):
    a, b = pair
    first = pk.Packet(pk.SOS, 10, 1, protocol.BROADCAST, em.sos_body("Alex", 50, "", "one"))
    second = pk.Packet(pk.SOS, 11, 1, protocol.BROADCAST, em.sos_body("Alex", 40, "", "two"))
    b.emergency.handle_sos(first)
    b.emergency.handle_sos(second)
    assert [a_.message for a_ in b.emergency.active_alarms()] == ["two"]


def test_sos_text_survives_odd_characters_and_is_clipped():
    body = em.sos_body("A|B", None, "x" * 500, "héllo | wörld")
    assert len(body) <= em.MAX_BODY
    parsed = em.parse_body(body)
    assert parsed["name"] == "A/B" and parsed["battery"] is None
    assert em.parse_body(b"\xff\xfe")["message"] == "Need help"


def test_only_one_sos_at_a_time():
    sos = em.Emergency(FakeLink(), 1, protocol.MessageIds(), Cfg)
    assert sos.start() and not sos.start()
