"""Sending with delivery confirmation: transmit, wait for the ACK, retry.

One worker thread takes messages off a queue and sends them one at a
time, so their order on the air is the order they were spoken. For each:

    transmit -> wait ack_timeout (+ jitter) for the ACK
             -> none? transmit again, up to max_retries more times
             -> ACK: DELIVERED, with the round-trip time
             -> still nothing: FAILED

The wait is ack_timeout on top of the time the message and its ACK
spend on the UARTs and the air, which grows as the air rate drops: at
1200 bps a fixed 3 s would resend a message whose ACK was on its way.

The jitter matters: two radios that both lost a packet to the same
collision would otherwise retry in lock-step and collide again.

ACKs themselves are not queued. They are sent straight from the receive
path, so a radio waiting on its own ACK still answers the other side.
"""

from __future__ import annotations

import queue
import random
import threading
import time

from lora import packet as pk
from lora import protocol
from messaging import history as h
from utils.logger import get_logger

log = get_logger("sender")

RETRY_JITTER_SECONDS = 0.5
# An ACK for a message already given up on still means it arrived.
LATE_ACK_LOOKBACK = 20


class Sender:
    def __init__(self, link, history: h.History, address: int,
                 ids: protocol.MessageIds, ack_timeout: float = 3.0,
                 max_retries: int = 3, name: str = ""):
        self.link = link
        self.history = history
        self.address = address
        self.ids = ids
        self.name = name
        self.ack_timeout = ack_timeout
        self.max_retries = max(0, int(max_retries))
        self._queue = queue.Queue()
        self._ack = threading.Event()
        self._lock = threading.Lock()
        self._pending = None
        self._acked_by = None
        self._thread = None
        self._stopping = False

    # --- lifecycle -----------------------------------------------------
    def start(self):
        if self._thread:
            return
        self._thread = threading.Thread(target=self._run, name="sender", daemon=True)
        self._thread.start()

    def stop(self):
        if not self._thread:
            return
        self._stopping = True
        self._queue.put(None)
        self._ack.set()
        self._thread.join(timeout=self.ack_timeout + 2.0)
        self._thread = None

    @property
    def busy(self) -> bool:
        return self._pending is not None or not self._queue.empty()

    # --- API -----------------------------------------------------------
    def send_text(self, text: str, dst: int = protocol.BROADCAST) -> list:
        """Queue `text` for `dst`. Returns the history entries, one per packet."""
        chunks = protocol.split_text(text)
        messages = []
        for index, chunk in enumerate(chunks):
            message = h.Message(
                h.TX, self.ids.next(), dst, chunk, h.QUEUED,
                part=f"{index + 1}/{len(chunks)}" if len(chunks) > 1 else "",
            )
            self.history.add(message)
            self._queue.put(message)
            messages.append(message)
        return messages

    def resend(self, message: h.Message) -> bool:
        """Try a FAILED message again, under the same ID."""
        if message.direction != h.TX or message.status != h.FAILED:
            return False
        self.history.update(message, status=h.QUEUED, attempts=0)
        self._queue.put(message)
        return True

    def send_hello(self, reply: bool = False, dst: int = protocol.BROADCAST) -> bool:
        return self.link.transmit(protocol.hello_packet(
            self.address, self.ids.next(), self.name, reply=reply, dst=dst))

    def handle_ack(self, ack) -> bool:
        """Called by the receiver for every ACK addressed to us."""
        with self._lock:
            pending = self._pending
            if (pending is not None and ack.msg_id == pending.msg_id
                    and pending.peer in (protocol.BROADCAST, ack.src)):
                self._acked_by = ack.src
                self._ack.set()
                return True
        # Not the one in flight: perhaps one we already gave up on.
        for message in reversed(self.history.messages[-LATE_ACK_LOOKBACK:]):
            if (message.direction == h.TX and message.msg_id == ack.msg_id
                    and message.status == h.FAILED
                    and message.peer in (protocol.BROADCAST, ack.src)):
                log.info("late ACK for #%d from %04X", ack.msg_id, ack.src)
                self.history.update(message, status=h.DELIVERED, acked_by=ack.src)
                return True
        return False

    # --- worker --------------------------------------------------------
    def _run(self):
        while True:
            message = self._queue.get()
            if message is None:
                return
            try:
                self._deliver(message)
            except Exception:
                log.exception("sending #%d failed", message.msg_id)
                self.history.update(message, status=h.FAILED)

    def _deliver(self, message: h.Message):
        packet = protocol.text_packet(self.address, message.peer, message.msg_id,
                                      message.text)
        in_flight = self.link.round_trip_seconds(len(packet.encode()), pk.OVERHEAD)
        with self._lock:
            self._pending = message
            self._acked_by = None
            self._ack.clear()
        try:
            self.history.update(message, status=h.SENDING)
            for attempt in range(1, self.max_retries + 2):
                sent_at = time.monotonic()
                if not self.link.transmit(packet):
                    log.warning("#%d not sent (duty cycle or radio error)",
                                message.msg_id)
                    self.history.update(message, status=h.FAILED, attempts=attempt)
                    return
                self.history.update(message, attempts=attempt)
                log.info("sent #%d to %s, attempt %d: %r", message.msg_id,
                         self.history.name_for(message.peer), attempt, message.text)
                wait = (self.ack_timeout + in_flight
                        + random.uniform(0, RETRY_JITTER_SECONDS))
                if self._ack.wait(wait) and self._acked_by is not None:
                    rtt = int((time.monotonic() - sent_at) * 1000)
                    self.history.update(message, status=h.DELIVERED, rtt_ms=rtt,
                                        acked_by=self._acked_by)
                    log.info("#%d delivered to %s in %d ms", message.msg_id,
                             self.history.name_for(self._acked_by), rtt)
                    return
                if self._stopping:
                    return   # leave it as SENDING; history marks it failed on load
            log.warning("#%d failed: no ACK after %d attempts", message.msg_id,
                        message.attempts)
            # No longer in flight *before* it reads failed: an ACK landing in
            # between would otherwise be taken for the pending message and
            # dropped, and the message stay "not confirmed" though it arrived.
            with self._lock:
                self._pending = None
            self.history.update(message, status=h.FAILED)
        finally:
            with self._lock:
                self._pending = None
