"""Rules of the conversation on top of the packet format.

* Message IDs: 16 bits, counted up per radio and remembered across
  restarts, so a receiver never mistakes a new message for a repeat.
* Duplicates: when an ACK is lost the sender transmits again. The
  receiver ACKs every copy -- the sender is still waiting -- but shows
  and stores only the first.
* Long transcripts are split at word boundaries into packets that each
  fit, because ASR does not know about a 200-byte limit.
"""

from __future__ import annotations

import json
import random
import threading
import time
from pathlib import Path

from lora import packet as pk

BROADCAST = 0xFFFF

# How long a (sender, ID) pair is remembered as already shown. Longer
# than any retry sequence, far shorter than 65535 messages.
DUPLICATE_WINDOW_SECONDS = 600.0


def text_packet(src: int, dst: int, msg_id: int, text: str) -> pk.Packet:
    return pk.Packet(pk.TEXT, msg_id, src, dst, text.encode("utf-8"))


def ack_packet(src: int, received: pk.Packet) -> pk.Packet:
    """ACK for `received`, sent from `src` back to whoever sent it."""
    return pk.Packet(pk.ACK, received.msg_id, src, received.src)


def hello_packet(src: int, msg_id: int, name: str, reply: bool = False,
                 dst: int = BROADCAST) -> pk.Packet:
    return pk.Packet(pk.HELLO_REPLY if reply else pk.HELLO, msg_id, src, dst,
                     clip_utf8(name, 24))


def ping_packet(src: int, dst: int, msg_id: int, padding: int = 0) -> pk.Packet:
    return pk.Packet(pk.PING, msg_id, src, dst, bytes(min(padding, pk.MAX_MESSAGE)))


def is_for(packet: pk.Packet, address: int) -> bool:
    return packet.dst in (address, BROADCAST)


def clip_utf8(text: str, limit: int) -> bytes:
    """Encode, cut to `limit` bytes without splitting a character."""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return data
    return data[:limit].decode("utf-8", errors="ignore").encode("utf-8")


def split_text(text: str, limit: int = pk.MAX_MESSAGE) -> list:
    """Split into chunks of at most `limit` UTF-8 bytes, at spaces if possible.

    Whitespace is normalised first: a transcript's line breaks mean
    nothing, and a 240x280 screen wraps the text itself.
    """
    words = text.split()
    chunks, current = [], ""
    for word in words:
        candidate = f"{current} {word}" if current else word
        if len(candidate.encode("utf-8")) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        # A single word longer than a packet: cut it, character-safe.
        while len(word.encode("utf-8")) > limit:
            head = clip_utf8(word, limit).decode("utf-8")
            chunks.append(head)
            word = word[len(head):]
        current = word
    if current:
        chunks.append(current)
    return chunks


class MessageIds:
    """Next message ID, persisted so a restart does not reuse recent ones."""

    def __init__(self, path: Path | None = None):
        self.path = path
        self._lock = threading.Lock()
        self._next = None
        if path and path.is_file():
            try:
                self._next = int(json.loads(path.read_text())["next_id"])
            except (OSError, ValueError, KeyError, TypeError):
                self._next = None
        if not self._next or not 1 <= self._next <= 0xFFFF:
            # No memory of earlier IDs: start somewhere a receiver is
            # unlikely to have seen from us in the last ten minutes.
            self._next = random.randint(1, 0xFFFF)

    def next(self) -> int:
        with self._lock:
            value = self._next
            self._next = value % 0xFFFF + 1    # 1..65535, never 0
            if self.path:
                try:
                    tmp = self.path.with_suffix(".tmp")
                    tmp.write_text(json.dumps({"next_id": self._next}))
                    tmp.replace(self.path)
                except OSError:
                    pass
            return value


class DuplicateFilter:
    """Has this (sender, message ID) been seen recently?"""

    def __init__(self, window: float = DUPLICATE_WINDOW_SECONDS, clock=time.monotonic):
        self.window = window
        self.clock = clock
        self._seen = {}
        self._lock = threading.Lock()

    def seen(self, src: int, msg_id: int) -> bool:
        """True if already seen; otherwise records it and returns False."""
        now = self.clock()
        with self._lock:
            if len(self._seen) > 512:
                cutoff = now - self.window
                self._seen = {k: t for k, t in self._seen.items() if t >= cutoff}
            key = (src, msg_id)
            when = self._seen.get(key)
            if when is not None and now - when < self.window:
                return True
            self._seen[key] = now
            return False
