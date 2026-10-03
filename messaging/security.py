"""Sealing messages for paired radios, with the keys every radio app shares.

The keys live in MFruit OS's shared radio store (mfruit_sdk.radio.keyring),
so a radio paired in WalkieTalkie is paired here too, and the other way
round. Which key seals a message:

    to one paired radio      its pairwise key     only that radio reads it
    to everyone (65535)      our broadcast key    every radio we paired with

A message for a radio we never paired with -- or to everyone, before any
pairing -- still goes out as plain TEXT, as before, and the chat marks it
"not encrypted". Every sealed packet authenticates its header (type, ID,
source, destination) and is sealed under a nonce made from a fresh random
salt plus the sender and message ID, so it never repeats under one key.
"""

from __future__ import annotations

import struct

from lora import packet as pk
from lora import protocol
from utils.logger import get_logger

log = get_logger("security")

_AAD = struct.Struct(">BHHH")
_CONTEXT = struct.Struct(">HH")


def _aad(kind: int, msg_id: int, src: int, dst: int) -> bytes:
    return _AAD.pack(kind, msg_id & 0xFFFF, src & 0xFFFF, dst & 0xFFFF)


def _context(src: int, msg_id: int) -> bytes:
    return _CONTEXT.pack(src & 0xFFFF, msg_id & 0xFFFF)


class Security:
    """Our side of the shared keys. ``keyring`` is a mfruit_sdk Keyring."""

    def __init__(self, keyring, contacts=None):
        from mfruit_sdk.radio import crypto
        self.crypto = crypto
        self.keyring = keyring
        self.contacts = contacts

    @classmethod
    def open_shared(cls):
        """The shared store, or None when this radio cannot encrypt (no
        cryptography package, or an older SDK copy)."""
        try:
            from mfruit_sdk.radio.contacts import Contacts
            from mfruit_sdk.radio.keyring import Keyring
            return cls(Keyring(), Contacts())
        except Exception as exc:
            log.warning("encryption unavailable, messages go out in the clear: %s", exc)
            return None

    # --- which key -----------------------------------------------------
    @property
    def paired(self) -> list:
        return self.keyring.paired

    def is_paired(self, address: int) -> bool:
        return self.keyring.is_paired(address)

    def can_seal_to(self, dst: int) -> bool:
        if dst == protocol.BROADCAST:
            return bool(self.keyring.paired)
        return self.keyring.is_paired(dst)

    def _sealing_key(self, dst: int) -> bytes | None:
        if dst == protocol.BROADCAST:
            return self.keyring.broadcast_key if self.keyring.paired else None
        return self.keyring.pairwise(dst)

    def _opening_key(self, src: int, dst: int) -> bytes | None:
        if dst == protocol.BROADCAST:
            return self.keyring.peer_broadcast(src)
        return self.keyring.pairwise(src)

    # --- sealing -------------------------------------------------------
    def text_packet(self, src: int, dst: int, msg_id: int, text: str) -> tuple:
        """(packet, sealed?) for a chat message: SECURE when we hold a key."""
        key = self._sealing_key(dst)
        if key is None:
            return protocol.text_packet(src, dst, msg_id, text), False
        body = self.crypto.seal_with(key, _context(src, msg_id),
                                     _aad(pk.SECURE, msg_id, src, dst), text.encode("utf-8"))
        return pk.Packet(pk.SECURE, msg_id, src, dst, body), True

    def open(self, packet: pk.Packet) -> str | None:
        """The text of a SECURE packet, or None if it is not for a key we hold."""
        key = self._opening_key(packet.src, packet.dst)
        if key is None:
            return None
        plain = self.crypto.open_with(key, _context(packet.src, packet.msg_id),
                                      _aad(packet.type, packet.msg_id, packet.src, packet.dst),
                                      packet.payload)
        return None if plain is None else plain.decode("utf-8", errors="replace")

    @property
    def overhead(self) -> int:
        return self.crypto.OVERHEAD

    # --- names -----------------------------------------------------------
    def name_for(self, address: int) -> str:
        if self.contacts is None:
            return ""
        return self.contacts.all().get(address, "")
