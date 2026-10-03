"""SOS: a call for help to every radio in range, and the answer to one.

This is a call for help over a LoRa link, **not a certified emergency
service**. Range depends on terrain, an SOS is only heard by radios that are
switched on, in range and running the Messenger, and nobody has to answer.
Always carry another way to call for help.

Sending
    start()      repeats SOS every ``first_interval`` seconds until some radio
                 ACKs it (so you know it was heard), then every ``interval``
                 seconds so radios that come into range later still hear it,
                 until you press "I'm OK" or ``max_hours`` have passed. Every
                 repeat is the same message ID, so receivers show one alarm.
    clear()      "I'm OK": sends SOS_CLEAR a few times, which ends the alarm
                 on every radio that showed it.
    SOS ignores the duty-cycle budget of ordinary messages.

Receiving
    handle_sos() ACKs every copy (after a random delay, so a dozen radios do
                 not answer on top of each other) and raises one alarm per
                 (sender, ID). A dismissed alarm stays dismissed until the
                 sender clears it or starts a new SOS.

An SOS travels in the clear on purpose, so any messenger in range can show
and answer it. That also means anyone can read it and anyone can fake one:
treat it as a call to go and look, not as proof.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field

from lora import packet as pk
from lora import protocol
from utils.logger import get_logger

log = get_logger("emergency")

CLEAR_REPEATS = 3
CLEAR_GAP_SECONDS = 4.0
ACK_JITTER_SECONDS = 1.5
MAX_BODY = 160
RETRY_JITTER_SECONDS = 2.0


def sos_body(name: str, battery: int | None, place: str, message: str) -> bytes:
    """name | battery % (or empty) | place | message, UTF-8, within one packet."""
    fields = [clean(name)[:24], "" if battery is None else str(int(battery)),
              clean(place)[:60], clean(message)[:60]]
    return protocol.clip_utf8("|".join(fields), MAX_BODY)


def clean(text: str) -> str:
    return " ".join(str(text).replace("|", "/").split())


def parse_body(payload: bytes) -> dict:
    parts = payload.decode("utf-8", errors="replace").split("|")
    parts += [""] * (4 - len(parts))
    name, battery, place, message = parts[:4]
    return {"name": name.strip(), "battery": int(battery) if battery.strip().isdigit() else None,
            "place": place.strip(), "message": message.strip() or "Need help"}


@dataclass
class Alarm:
    """An SOS this radio heard."""
    src: int
    msg_id: int
    name: str
    battery: int | None
    place: str
    message: str
    first_heard: float
    last_heard: float
    copies: int = 1
    rssi: int | None = None
    dismissed: bool = False
    cleared: bool = False

    @property
    def key(self) -> tuple:
        return (self.src, self.msg_id)


@dataclass
class Outgoing:
    """The SOS this radio is sending."""
    msg_id: int
    started: float
    body: bytes
    transmissions: int = 0
    ackers: set = field(default_factory=set)
    last_sent: float = 0.0
    clearing: int = 0              # SOS_CLEAR sends still to do
    done: bool = False
    note: str = ""


class Emergency:
    def __init__(self, link, address: int, ids, config, battery=lambda: None, name: str = "",
                 clock=time.monotonic, on_change=None, on_alarm=None, rng=None):
        self.link = link
        self.address = address
        self.ids = ids
        self.config = config
        self.battery = battery
        self.name = name
        self.clock = clock
        self.on_change = on_change or (lambda: None)
        self.on_alarm = on_alarm or (lambda alarm: None)
        self.rng = rng or random.Random()
        self.outgoing = None
        self.alarms = {}                 # (src, id) -> Alarm
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._thread = None
        self._stop = False

    # --- lifecycle -------------------------------------------------------
    def start_worker(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="sos", daemon=True)
            self._thread.start()

    def stop_worker(self):
        self._stop = True
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None

    # --- sending ---------------------------------------------------------
    @property
    def active(self) -> bool:
        return self.outgoing is not None and not self.outgoing.done and not self.outgoing.clearing

    @property
    def heard_by(self) -> int:
        return len(self.outgoing.ackers) if self.outgoing else 0

    def start(self) -> bool:
        """Begin sending an SOS. False if one is already being sent."""
        with self._lock:
            if self.active:
                return False
            cfg = self.config
            body = sos_body(self.name, self.battery(), cfg.place, cfg.message)
            self.outgoing = Outgoing(self.ids.next(), self.clock(), body)
            log.warning("SOS started (#%d): %r", self.outgoing.msg_id, body.decode("utf-8", "replace"))
        self._wake.set()
        self.on_change()
        return True

    def clear(self) -> bool:
        """"I'm OK": stop repeating, tell everyone who may be showing the alarm."""
        with self._lock:
            if not self.active:
                return False
            self.outgoing.clearing = CLEAR_REPEATS
            log.warning("SOS cleared by the user after %d transmissions",
                        self.outgoing.transmissions)
        self._wake.set()
        self.on_change()
        return True

    def handle_ack(self, ack: pk.Packet) -> bool:
        """True if this ACK answered our SOS."""
        with self._lock:
            out = self.outgoing
            if out is None or out.done or ack.msg_id != out.msg_id:
                return False
            if ack.src not in out.ackers:
                out.ackers.add(ack.src)
                log.warning("SOS heard by %04X (%d radio%s)", ack.src, len(out.ackers),
                            "" if len(out.ackers) == 1 else "s")
        self._wake.set()
        self.on_change()
        return True

    def interval(self) -> float:
        cfg = self.config
        return cfg.interval_seconds if self.outgoing and self.outgoing.ackers \
            else cfg.first_interval_seconds

    def next_due(self, now: float | None = None) -> float | None:
        """When the worker next has something to send (monotonic), or None."""
        now = self.clock() if now is None else now
        with self._lock:
            out = self.outgoing
            if out is None or out.done:
                return None
            if out.clearing:
                return out.last_sent + CLEAR_GAP_SECONDS if out.last_sent else now
            return out.last_sent + self.interval() if out.last_sent else now

    def step(self, now: float | None = None) -> None:
        """Send whatever is due. Called by the worker; tests call it directly."""
        now = self.clock() if now is None else now
        with self._lock:
            out = self.outgoing
            if out is None or out.done:
                return
            if now - out.started > self.config.max_hours * 3600 and not out.clearing:
                out.done = True
                out.note = "SOS stopped after the time limit"
                log.warning(out.note)
                changed = True
            else:
                due = self.next_due(now)
                if due is None or now < due:
                    return
                changed = False
                if out.clearing:
                    self.link.transmit(pk.Packet(pk.SOS_CLEAR, out.msg_id, self.address,
                                                 protocol.BROADCAST), force=True)
                    out.clearing -= 1
                    out.last_sent = now
                    if not out.clearing:
                        out.done = True
                        out.note = "SOS ended: I'm OK"
                    changed = True
                else:
                    sent = self.link.transmit(pk.Packet(pk.SOS, out.msg_id, self.address,
                                                        protocol.BROADCAST, out.body), force=True)
                    out.last_sent = now + self.rng.uniform(0, RETRY_JITTER_SECONDS)
                    if sent:
                        out.transmissions += 1
                        changed = True
        if changed:
            self.on_change()

    def _run(self):
        while not self._stop:
            self.step()
            due = self.next_due()
            wait = 5.0 if due is None else max(0.05, due - self.clock())
            self._wake.wait(min(wait, 5.0))
            self._wake.clear()

    # --- receiving -------------------------------------------------------
    def handle_sos(self, packet: pk.Packet, rssi: int | None = None) -> Alarm:
        """An SOS from another radio: ACK it and raise (or refresh) its alarm."""
        self._ack_later(packet)
        info = parse_body(packet.payload)
        now = self.clock()
        with self._lock:
            alarm = self.alarms.get((packet.src, packet.msg_id))
            if alarm is not None:
                alarm.copies += 1
                alarm.last_heard = now
                alarm.rssi = rssi if rssi is not None else alarm.rssi
                return alarm
            # A new SOS from the same radio replaces its older alarms.
            for key in [k for k in self.alarms if k[0] == packet.src]:
                del self.alarms[key]
            alarm = Alarm(packet.src, packet.msg_id, info["name"], info["battery"],
                          info["place"], info["message"], now, now, rssi=rssi)
            self.alarms[alarm.key] = alarm
        log.warning("SOS from %s (%04X): %r", alarm.name or "an unknown radio", packet.src,
                    alarm.message)
        try:
            self.on_alarm(alarm)
        finally:
            self.on_change()
        return alarm

    def handle_clear(self, packet: pk.Packet) -> Alarm | None:
        with self._lock:
            alarm = self.alarms.get((packet.src, packet.msg_id))
            if alarm is None or alarm.cleared:
                return None
            alarm.cleared = True
        log.warning("%s says: I'm OK", alarm.name or f"{packet.src:04X}")
        self.on_change()
        return alarm

    def active_alarms(self) -> list:
        """Alarms still to show: not dismissed, not cleared, newest first."""
        with self._lock:
            return sorted((a for a in self.alarms.values() if not a.dismissed and not a.cleared),
                          key=lambda a: -a.first_heard)

    def dismiss(self, alarm: Alarm) -> None:
        with self._lock:
            alarm.dismissed = True
        self.on_change()

    def _ack_later(self, packet: pk.Packet):
        delay = self.rng.uniform(0, ACK_JITTER_SECONDS)
        ack = protocol.ack_packet(self.address, packet)
        if delay <= 0.01:
            self.link.transmit(ack, force=True)
            return
        timer = threading.Timer(delay, self.link.transmit, args=(ack,), kwargs={"force": True})
        timer.daemon = True
        timer.start()
