"""Every message sent and received, with its ID, times and delivery state.

Kept in memory and written to `history.json` in the data directory
after each change: a few hundred small records, rewritten atomically, so
a crash or power cut loses at most the change in flight. The names other
radios announce (HELLO) live here too, so "RX: OrangePi" survives a
restart.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from utils.logger import get_logger

log = get_logger("history")

TX = "tx"
RX = "rx"

# Outgoing states, in order.
QUEUED = "queued"
SENDING = "sending"        # on the air, waiting for the ACK
DELIVERED = "delivered"    # ACK received
FAILED = "failed"          # no ACK after every retry, or duty cycle spent
# Incoming.
RECEIVED = "received"


@dataclass
class Message:
    direction: str
    msg_id: int
    peer: int                  # the other radio: sender for RX, recipient for TX
    text: str
    status: str
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    attempts: int = 0
    rssi: int | None = None
    rtt_ms: int | None = None  # TX: send-to-ACK time of the delivering attempt
    acked_by: int | None = None  # TX: who ACKed it -- the radio, for a broadcast
    part: str = ""             # "1/2" when a transcript was split
    secure: bool = False       # sealed with a pairing key (False: anyone in range could read it)
    kind: str = "text"         # text | sos | ok

    @property
    def key(self) -> tuple:
        return (self.direction, self.peer, self.msg_id)


class History:
    def __init__(self, path: Path | None = None, limit: int = 200):
        self.path = path
        self.limit = limit
        self.messages = []
        self.names = {}            # address -> announced name
        self._lock = threading.RLock()
        self._listeners = []
        self._load()

    # --- persistence ---------------------------------------------------
    def _load(self):
        if not self.path or not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text())
            known = set(Message.__dataclass_fields__)
            self.messages = [Message(**{k: v for k, v in item.items() if k in known})
                             for item in data.get("messages", [])][-self.limit:]
            self.names = {int(k): str(v) for k, v in data.get("names", {}).items()}
        except (OSError, ValueError, TypeError) as exc:
            log.warning("could not read %s (%s); starting empty", self.path, exc)
            self.messages, self.names = [], {}
            return
        # A message in flight when the app stopped will never be ACKed now.
        for message in self.messages:
            if message.direction == TX and message.status in (QUEUED, SENDING):
                message.status = FAILED

    def _save(self):
        if not self.path:
            return
        data = {"messages": [asdict(m) for m in self.messages],
                "names": {str(k): v for k, v in self.names.items()}}
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=0))
            tmp.replace(self.path)
        except OSError as exc:
            log.warning("could not save history: %s", exc)

    # --- changes -------------------------------------------------------
    def subscribe(self, callback):
        """callback(message) after every add or update."""
        self._listeners.append(callback)

    def _changed(self, message):
        self._save()
        for callback in list(self._listeners):
            try:
                callback(message)
            except Exception:
                log.exception("history listener failed")

    def add(self, message: Message) -> Message:
        with self._lock:
            self.messages.append(message)
            del self.messages[:-self.limit]
            self._changed(message)
        return message

    def update(self, message: Message, **changes) -> Message:
        with self._lock:
            for key, value in changes.items():
                setattr(message, key, value)
            message.updated = time.time()
            self._changed(message)
        return message

    def set_name(self, address: int, name: str) -> bool:
        with self._lock:
            if not name or self.names.get(address) == name:
                return False
            self.names[address] = name
            self._save()
            return True

    # --- queries -------------------------------------------------------
    def name_for(self, address: int) -> str:
        if address == 0xFFFF:
            return "all"
        return self.names.get(address) or f"#{address:04X}"

    def find(self, direction: str, peer: int, msg_id: int) -> Message | None:
        with self._lock:
            for message in reversed(self.messages):
                if message.key == (direction, peer, msg_id):
                    return message
        return None

    def latest(self, direction: str | None = None) -> Message | None:
        with self._lock:
            for message in reversed(self.messages):
                if direction is None or message.direction == direction:
                    return message
        return None

    def __len__(self):
        return len(self.messages)

    def __getitem__(self, index):
        return self.messages[index]
