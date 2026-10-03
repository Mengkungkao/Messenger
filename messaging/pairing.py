"""Pairing two radios: the same exchange of keys as WalkieTalkie's, here in
the messenger's own packets, stored in the keys every radio app shares.

    both people open Pair a radio      each radio beacons PAIR every few seconds:
                                       its public key and name, in the clear
    one picks the other in the list    PAIR_REQUEST: our public key, then our
                                       broadcast key and name sealed for them
    both screens show the same code    four digits from the two public keys
    the other accepts                  PAIR_ACCEPT: the same, back; both radios
                                       store each other in the shared keyring

Someone in the middle who substitutes their own key makes the two codes
differ, which is why the person accepting compares them first. Pairing
packets are only acted on while the window is open on this radio.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from lora import packet as pk
from lora import protocol
from utils.logger import get_logger

log = get_logger("pairing")

WINDOW_SECONDS = 120.0
BEACON_SECONDS = 5.0           # ~0.3 s of air each at 2.4k: ~7 s per window
ANSWER_SECONDS = 30.0
PUBLIC_SIZE = 32
MAX_NAME_BYTES = 20


@dataclass
class Found:
    address: int
    name: str
    public: bytes
    heard: float
    rssi: int | None = None


@dataclass
class Request:
    """A pairing waiting on a person: ours (outgoing) or theirs (incoming)."""
    address: int
    name: str
    public: bytes
    code: str
    at: float
    broadcast: bytes = b""


class Pairing:
    def __init__(self, link, security, address: int, name: str, ids, history=None,
                 clock=time.monotonic, on_change=None):
        self.link = link
        self.security = security
        self.address = address
        self.name = name
        self.ids = ids
        self.history = history
        self.clock = clock
        self.on_change = on_change or (lambda: None)
        self.active = False
        self.until = 0.0
        self.found = {}                 # address -> Found
        self.outgoing = None            # Request we sent, waiting for their accept
        self.incoming = None            # Request they sent, waiting for our person
        self.result = ""                # last outcome, for the screen
        self._next_beacon = 0.0

    # --- the window ------------------------------------------------------
    def start(self):
        self.active = True
        self.until = self.clock() + WINDOW_SECONDS
        self.found, self.outgoing, self.incoming, self.result = {}, None, None, ""
        self._next_beacon = 0.0
        log.info("pairing window open for %.0f s", WINDOW_SECONDS)
        self.tick()

    def stop(self):
        if self.active:
            log.info("pairing window closed")
        self.active = False
        self.outgoing = self.incoming = None

    def next_due(self) -> float | None:
        if not self.active:
            return None
        due = [self.until, self._next_beacon]
        if self.outgoing:
            due.append(self.outgoing.at + ANSWER_SECONDS)
        return min(due)

    def tick(self):
        """Beacon when due, and expire what timed out."""
        if not self.active:
            return
        now = self.clock()
        if now >= self.until:
            self.result = self.result or "Pairing timed out"
            self.stop()
            self.on_change()
            return
        if self.outgoing and now - self.outgoing.at > ANSWER_SECONDS:
            self.result = f"{self.outgoing.name} did not accept"
            self.outgoing = None
            self.on_change()
        if now >= self._next_beacon:
            body = self.security.keyring.public + self.name.encode("utf-8")[:MAX_NAME_BYTES]
            self.link.transmit(pk.Packet(pk.PAIR, self.ids.next(), self.address,
                                         protocol.BROADCAST, body))
            self._next_beacon = now + BEACON_SECONDS

    # --- what the person does ------------------------------------------------
    def candidates(self) -> list:
        """Radios heard beaconing, newest first."""
        return sorted(self.found.values(), key=lambda f: -f.heard)

    def request(self, address: int) -> bool:
        found = self.found.get(address)
        if not self.active or found is None or self.incoming is not None:
            return False
        keyring = self.security.keyring
        body = keyring.pair_body(found.public, self.address, address, self.name)
        if not self.link.transmit(pk.Packet(pk.PAIR_REQUEST, self.ids.next(), self.address,
                                            address, body)):
            self.result = "Could not send (radio busy)"
            return False
        self.outgoing = Request(address, found.name, found.public,
                                keyring.code_with(found.public), self.clock())
        self.result = ""
        log.info("asked %s (%04X) to pair; code %s", found.name, address, self.outgoing.code)
        return True

    def accept(self) -> bool:
        request = self.incoming
        if request is None:
            return False
        keyring = self.security.keyring
        body = keyring.pair_body(request.public, self.address, request.address, self.name)
        self.link.transmit(pk.Packet(pk.PAIR_ACCEPT, self.ids.next(), self.address,
                                     request.address, body))
        self._store(request)
        self.incoming = None
        return True

    def cancel_request(self):
        if self.outgoing is not None:
            log.info("cancelled the pairing request to %s", self.outgoing.name)
            self.outgoing = None

    def reject(self):
        if self.incoming is not None:
            log.info("refused to pair with %s", self.incoming.name)
            self.result = f"Not paired with {self.incoming.name}"
            self.incoming = None

    # --- packets -----------------------------------------------------------------
    def handle(self, packet: pk.Packet, rssi: int | None = None) -> bool:
        """True if ``packet`` was a pairing packet (whether or not it was used)."""
        if packet.type not in (pk.PAIR, pk.PAIR_REQUEST, pk.PAIR_ACCEPT):
            return False
        if not self.active:
            log.debug("pairing packet from %04X ignored: window closed", packet.src)
            return True
        if packet.type == pk.PAIR:
            if len(packet.payload) < PUBLIC_SIZE or self.security.is_paired(packet.src):
                return True
            name = packet.payload[PUBLIC_SIZE:].decode("utf-8", "replace").strip()
            self.found[packet.src] = Found(packet.src, name or f"Radio {packet.src}",
                                           bytes(packet.payload[:PUBLIC_SIZE]), self.clock(), rssi)
            self.on_change()
            return True
        if packet.dst != self.address:
            return True
        opened = self.security.keyring.open_pair_body(packet.payload, packet.src, packet.dst)
        if opened is None:
            log.warning("unreadable pairing message from %04X", packet.src)
            return True
        public, broadcast, name = opened
        name = name or f"Radio {packet.src}"
        if packet.type == pk.PAIR_REQUEST:
            if self.outgoing is None and self.incoming is None:
                self.incoming = Request(packet.src, name, public,
                                        self.security.keyring.code_with(public), self.clock(),
                                        broadcast)
                log.info("%s (%04X) asks to pair; code %s", name, packet.src, self.incoming.code)
        elif self.outgoing is not None and self.outgoing.address == packet.src \
                and self.outgoing.public == public:
            self.outgoing.broadcast = broadcast
            self.outgoing.name = name
            self._store(self.outgoing)
            self.outgoing = None
        self.on_change()
        return True

    def _store(self, request: Request):
        self.security.keyring.add_peer(request.address, request.public, request.broadcast)
        if self.security.contacts is not None:
            self.security.contacts.set(request.address, request.name)
        if self.history is not None:
            self.history.set_name(request.address, request.name)
        self.found.pop(request.address, None)
        self.result = f"Paired with {request.name}"
        log.info("paired with %s (%04X)", request.name, request.address)
