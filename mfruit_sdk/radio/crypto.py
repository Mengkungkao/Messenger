"""Encryption for everything a paired radio says (shared by every radio app).

LoRa is a broadcast medium: anyone with a module on the frequency hears
every packet. So content is sealed, and only radios that paired can open
it.

**Keys.** Each radio has an X25519 key pair, made once at install. Two
radios that pair exchange public keys and derive a *pairwise* key both
can compute and nobody listening can. Each radio also has a random
*broadcast* key, which it hands to every radio it pairs with, sealed
under the pairwise key. Then:

    to one radio   sealed with our pairwise key    only that radio reads it
    to ALL         sealed with our broadcast key   every radio we paired
                                                   with reads it, and no other

**The code.** Both screens show four digits derived from the two public
keys. Someone in the middle of a pairing substitutes their own key, and
the two codes then differ -- which is what comparing them catches.

**Packets.** ChaCha20-Poly1305. The 10-byte header travels in the clear,
because receivers route on it, but is authenticated: changing a single
bit of source, destination or channel makes the packet fail to open.
The nonce is six random bytes sent with the packet plus the sender and
fragment numbers from the header, so it never repeats under one key.
That costs 22 bytes per fragment, about an eighth of a packet.
"""

from __future__ import annotations

import os
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (X25519PrivateKey,
                                                              X25519PublicKey)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

KEY_SIZE = 32
PUBLIC_SIZE = 32
SALT_SIZE = 6
TAG_SIZE = 16
OVERHEAD = SALT_SIZE + TAG_SIZE  # 22 bytes per sealed fragment
BLOB_NONCE_SIZE = 12

_RAW = serialization.Encoding.Raw


def new_private() -> bytes:
    return X25519PrivateKey.generate().private_bytes(
        _RAW, serialization.PrivateFormat.Raw, serialization.NoEncryption())


def public_of(private: bytes) -> bytes:
    return X25519PrivateKey.from_private_bytes(private).public_key().public_bytes(
        _RAW, serialization.PublicFormat.Raw)


def new_key() -> bytes:
    return os.urandom(KEY_SIZE)


def _derive(private: bytes, their_public: bytes, label: bytes, length: int) -> bytes:
    shared = X25519PrivateKey.from_private_bytes(private).exchange(
        X25519PublicKey.from_public_bytes(their_public))
    # Both ends must feed identical input, so the two public keys go in
    # sorted rather than as "mine, theirs".
    low, high = sorted((public_of(private), bytes(their_public)))
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=None,
                info=b"walkie " + label + low + high).derive(shared)


def pairwise_key(private: bytes, their_public: bytes) -> bytes:
    return _derive(private, their_public, b"pairwise", KEY_SIZE)


def pairing_code(private: bytes, their_public: bytes) -> str:
    """Four digits both radios show; they match only if nobody interfered."""
    digest = _derive(private, their_public, b"pairing code", 4)
    return f"{int.from_bytes(digest, 'big') % 10000:04d}"


def _nonce(salt: bytes, header: bytes) -> bytes:
    # salt(6) | src(2) | msg_id(1) | seq(1) | 00 00. The source keeps the
    # two ends of a pairwise key apart; msg_id and seq keep fragments of
    # one message apart; the salt keeps restarts and reused msg_ids apart.
    return bytes(salt) + header[2:4] + header[6:8] + b"\0\0"


def seal(key: bytes, header: bytes, plaintext: bytes) -> bytes:
    """salt || ciphertext || tag, with the header authenticated alongside."""
    salt = os.urandom(SALT_SIZE)
    return salt + ChaCha20Poly1305(key).encrypt(_nonce(salt, header),
                                                bytes(plaintext), bytes(header))


def open_sealed(key: bytes, header: bytes, body: bytes) -> bytes | None:
    """The plaintext, or None if the key is wrong or anything was altered."""
    if len(body) < OVERHEAD:
        return None
    salt = body[:SALT_SIZE]
    try:
        return ChaCha20Poly1305(key).decrypt(_nonce(salt, header),
                                             bytes(body[SALT_SIZE:]), bytes(header))
    except InvalidTag:
        return None


def salt_of(body: bytes) -> bytes:
    return bytes(body[:SALT_SIZE])


# --- pairing messages -------------------------------------------------------
_PAIR_AAD = struct.Struct(">8sHH")


def seal_blob(key: bytes, src: int, dst: int, plaintext: bytes) -> bytes:
    """For a pairing message: bound to who sent it to whom."""
    nonce = os.urandom(BLOB_NONCE_SIZE)
    aad = _PAIR_AAD.pack(b"walkiepr", src & 0xFFFF, dst & 0xFFFF)
    return nonce + ChaCha20Poly1305(key).encrypt(nonce, bytes(plaintext), aad)


def open_blob(key: bytes, src: int, dst: int, blob: bytes) -> bytes | None:
    if len(blob) < BLOB_NONCE_SIZE + TAG_SIZE:
        return None
    aad = _PAIR_AAD.pack(b"walkiepr", src & 0xFFFF, dst & 0xFFFF)
    try:
        return ChaCha20Poly1305(key).decrypt(bytes(blob[:BLOB_NONCE_SIZE]),
                                             bytes(blob[BLOB_NONCE_SIZE:]), aad)
    except InvalidTag:
        return None


# --- any packet format ---------------------------------------------------------
# ``seal``/``open_sealed`` take their nonce inputs from WalkieTalkie's
# 10-byte header. Apps with another header pass the same inputs
# explicitly: ``context`` is 4 bytes that differ for every packet one
# sender seals under one key (for example its address, a message ID and a
# fragment number), and ``aad`` is what must arrive unaltered.

def seal_with(key: bytes, context: bytes, aad: bytes, plaintext: bytes) -> bytes:
    """salt || ciphertext || tag; ``aad`` is authenticated, not encrypted."""
    if len(context) != 4:
        raise ValueError("context must be 4 bytes")
    salt = os.urandom(SALT_SIZE)
    return salt + ChaCha20Poly1305(key).encrypt(bytes(salt) + bytes(context) + b"\0\0",
                                                bytes(plaintext), bytes(aad))


def open_with(key: bytes, context: bytes, aad: bytes, body: bytes) -> bytes | None:
    """The plaintext, or None if the key is wrong or anything was altered."""
    if len(body) < OVERHEAD or len(context) != 4:
        return None
    salt = bytes(body[:SALT_SIZE])
    try:
        return ChaCha20Poly1305(key).decrypt(salt + bytes(context) + b"\0\0",
                                             bytes(body[SALT_SIZE:]), bytes(aad))
    except InvalidTag:
        return None
