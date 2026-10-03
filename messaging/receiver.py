"""What to do with each packet the radio hears.

    TEXT         ACK it (every copy), then show and store it (first copy only)
    ACK          hand to the sender, which is waiting for it
    HELLO        remember the name, answer with HELLO_REPLY (rate-limited)
    HELLO_REPLY  remember the name
    PING         ACK it; link tests, never shown

A TEXT or ACK from a radio whose name we do not know yet -- its start-up
HELLO was lost, or it started before us and we missed it -- makes us
send it a HELLO, which it answers with its name. Rate-limited like the
replies, so a radio that never answers costs one HELLO per half minute.

Packets for another address are ignored. A packet claiming to come from
our own address means two radios share one -- the module never echoes
our own transmissions back -- and is worth a loud log line, because each
would then take the other's ACKs as its own.
"""

from __future__ import annotations

import time

from lora import packet as pk
from lora import protocol
from messaging import history as h
from utils.logger import get_logger

log = get_logger("receiver")

HELLO_REPLY_SECONDS = 30.0


class Receiver:
    def __init__(self, link, sender, history: h.History, address: int,
                 on_message=None, security=None, pairing=None, emergency=None):
        self.security = security      # to open SECURE packets
        self.pairing = pairing        # messaging.pairing.Pairing
        self.emergency = emergency    # messaging.emergency.Emergency
        self.undecryptable = 0
        self.link = link
        self.sender = sender
        self.history = history
        self.address = address
        self.on_message = on_message    # callback(Message) for new incoming text
        self.duplicates = protocol.DuplicateFilter()
        self._hello_sent = {}
        self._last_rx = None
        self._last_rssi = None
        self.clashes = 0

    def handle(self, packet: pk.Packet):
        if packet.src == self.address:
            self.clashes += 1
            log.error("heard %s from our own address %04X: another radio uses it. "
                      "Give one of them a different radio.address.", packet,
                      self.address)
            return
        if not protocol.is_for(packet, self.address):
            log.debug("not for us: %s", packet)
            return

        if packet.type == pk.TEXT:
            self._on_text(packet)
        elif packet.type == pk.SECURE:
            self._on_secure(packet)
        elif packet.type in (pk.PAIR, pk.PAIR_REQUEST, pk.PAIR_ACCEPT):
            if self.pairing is not None:
                self.pairing.handle(packet, self._last_rssi)
        elif packet.type == pk.SOS:
            if self.emergency is not None:
                self.emergency.handle_sos(packet, self._last_rssi)
        elif packet.type == pk.SOS_CLEAR:
            if self.emergency is not None:
                self.emergency.handle_clear(packet)
        elif packet.type == pk.ACK:
            if self.emergency is not None and self.emergency.handle_ack(packet):
                pass
            elif not self.sender.handle_ack(packet):
                log.debug("stray ACK #%d from %04X", packet.msg_id, packet.src)
        elif packet.type in (pk.HELLO, pk.HELLO_REPLY):
            self._on_hello(packet)
        elif packet.type == pk.PING:
            self._ack(packet)

        # After the ACK, never before it: the sender is waiting on that.
        if packet.type in (pk.TEXT, pk.SECURE, pk.ACK) and packet.src not in self.history.names:
            self._say_hello(packet.src, reply=False)

    def handle_rssi(self, dbm: int):
        """The module's signal report for the packet just received."""
        self._last_rssi = dbm
        message = self._last_rx
        if message is not None and message.rssi is None \
                and time.time() - message.created < 2.0:
            self.history.update(message, rssi=dbm)

    def _ack(self, packet: pk.Packet):
        self.link.transmit(protocol.ack_packet(self.address, packet))

    def _on_text(self, packet: pk.Packet):
        self._accept_text(packet, packet.text, secure=False)

    def _on_secure(self, packet: pk.Packet):
        """Open a sealed message. Only a readable one is ACKed: an ACK tells
        the sender "delivered", and we cannot say that of what we cannot read."""
        text = self.security.open(packet) if self.security is not None else None
        if text is None:
            self.undecryptable += 1
            log.warning("cannot open #%d from %04X (not paired, keys changed, or "
                        "altered); ignored", packet.msg_id, packet.src)
            return
        self._accept_text(packet, text, secure=True)

    def _accept_text(self, packet: pk.Packet, text: str, secure: bool):
        # ACK first, even for a repeat: a repeat means our last ACK was lost.
        self._ack(packet)
        if self.duplicates.seen(packet.src, packet.msg_id):
            log.info("repeat of #%d from %s; ACKed again", packet.msg_id,
                     self.history.name_for(packet.src))
            return
        message = h.Message(h.RX, packet.msg_id, packet.src, text, h.RECEIVED,
                            attempts=1, secure=secure)
        self._last_rx = message
        self.history.add(message)
        log.info("received #%d from %s: %r", packet.msg_id,
                 self.history.name_for(packet.src), message.text)
        if self.on_message:
            try:
                self.on_message(message)
            except Exception:
                log.exception("message callback failed")

    def _on_hello(self, packet: pk.Packet):
        name = packet.text.strip()
        if self.history.set_name(packet.src, name):
            log.info("%04X is %r", packet.src, name)
        if packet.type == pk.HELLO:
            self._say_hello(packet.src, reply=True)

    def _say_hello(self, dst: int, reply: bool):
        now = time.monotonic()
        if now - self._hello_sent.get(dst, -1e9) >= HELLO_REPLY_SECONDS:
            self._hello_sent[dst] = now
            self.sender.send_hello(reply=reply, dst=dst)
