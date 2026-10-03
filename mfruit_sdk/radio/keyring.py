"""This radio's keys, and the keys of every radio it has paired with.

Shared by every radio app (``keys.json`` in the shared radio directory),
so a radio paired in one app is paired in all of them. The file format is
WalkieTalkie's original ``keys.json``. It holds secrets: it is readable by
its owner only and must never be copied between devices. Losing it is not
dangerous, only inconvenient -- the radio makes new keys and has to be
paired again.

Another app may pair while this one runs; ``refresh()`` (called by the
peer lookups) reloads the file when it changed on disk.

See ``crypto`` for what the keys are used for.
"""

from __future__ import annotations

import logging
import os

from . import crypto
from .legacy import adopt_keys as adopt_legacy  # noqa: F401  (kept for existing callers)
from .settings import locked, radio_dir, read_json, write_json

log = logging.getLogger("mfruit_sdk.radio.keyring")

FILE_NAME = "keys.json"


class Keyring:
    def __init__(self, directory: str | None = None, legacy_path: str | None = None):
        self.directory = directory or radio_dir()
        self.path = os.path.join(self.directory, FILE_NAME)
        self._pairwise = {}
        self._peers = {}
        self._mtime = None
        if legacy_path:
            adopt_legacy(legacy_path, self.directory)
        self._load()

    # --- persistence ----------------------------------------------------
    def _stat(self):
        """A change signature: every save is an atomic replace (a new inode),
        and the timestamp alone can repeat within the clock's granularity."""
        try:
            info = os.stat(self.path)
        except OSError:
            return None
        return info.st_ino, info.st_mtime_ns, info.st_size

    def _load(self):
        with locked(self.directory):
            data = read_json(self.path) or {}
            try:
                self.private = bytes.fromhex(data["private"])
                self.broadcast_key = bytes.fromhex(data["broadcast"])
                self.public = crypto.public_of(self.private)
            except (KeyError, TypeError, ValueError):
                if data:
                    log.warning("could not read %s; making new keys", self.path)
                self._new_identity()
                data = {}
            self._peers = {}
            for addr, entry in (data.get("peers") or {}).items():
                try:
                    self._peers[int(addr)] = (bytes.fromhex(entry["public"]),
                                              bytes.fromhex(entry["broadcast"]))
                except (KeyError, TypeError, ValueError):
                    continue
            self._pairwise = {}
            if not data:
                self._write()
            self._mtime = self._stat()

    def refresh(self):
        """Reload if another app changed the shared keys."""
        if self._stat() != self._mtime:
            self._load()

    def _new_identity(self):
        self.private = crypto.new_private()
        self.public = crypto.public_of(self.private)
        self.broadcast_key = crypto.new_key()
        self._pairwise = {}
        log.info("made new keys for this radio")

    def _write(self):
        data = {
            "private": self.private.hex(),
            "broadcast": self.broadcast_key.hex(),
            "peers": {str(addr): {"public": public.hex(), "broadcast": broadcast.hex()}
                      for addr, (public, broadcast) in self._peers.items()},
        }
        try:
            write_json(self.path, data)
        except OSError:
            log.warning("could not write %s", self.path, exc_info=True)
        self._mtime = self._stat()

    def save(self):
        with locked(self.directory):
            self._write()

    def reset(self):
        """New keys, no peers: every radio has to pair again."""
        with locked(self.directory):
            self._new_identity()
            self._peers = {}
            self._write()

    # --- peers -----------------------------------------------------------
    def is_paired(self, addr: int) -> bool:
        self.refresh()
        return addr in self._peers

    @property
    def paired(self) -> list:
        self.refresh()
        return sorted(self._peers)

    def add_peer(self, addr: int, public: bytes, broadcast: bytes):
        with locked(self.directory):
            self._reload_peers_locked()
            self._peers[int(addr)] = (bytes(public), bytes(broadcast))
            self._pairwise.pop(int(addr), None)
            self._write()

    def remove_peer(self, addr: int):
        with locked(self.directory):
            self._reload_peers_locked()
            if self._peers.pop(int(addr), None) is not None:
                self._pairwise.pop(int(addr), None)
                self._write()

    def _reload_peers_locked(self):
        """Merge in peers another app added since we loaded (lock held)."""
        if self._stat() == self._mtime:
            return
        data = read_json(self.path) or {}
        if data.get("private") != self.private.hex():
            return              # the other app reset the keys; ours are stale
        for addr, entry in (data.get("peers") or {}).items():
            try:
                self._peers.setdefault(int(addr), (bytes.fromhex(entry["public"]),
                                                   bytes.fromhex(entry["broadcast"])))
            except (KeyError, TypeError, ValueError):
                continue

    def peer_public(self, addr: int) -> bytes | None:
        self.refresh()
        entry = self._peers.get(addr)
        return entry[0] if entry else None

    def peer_broadcast(self, addr: int) -> bytes | None:
        """The key ``addr`` seals its messages to ALL with."""
        self.refresh()
        entry = self._peers.get(addr)
        return entry[1] if entry else None

    def pairwise(self, addr: int) -> bytes | None:
        """The key only this radio and ``addr`` hold."""
        self.refresh()
        if addr not in self._pairwise:
            public = self.peer_public(addr)
            if public is None:
                return None
            self._pairwise[addr] = crypto.pairwise_key(self.private, public)
        return self._pairwise[addr]

    # --- pairing -----------------------------------------------------------
    def code_with(self, their_public: bytes) -> str:
        return crypto.pairing_code(self.private, their_public)

    def pair_body(self, their_public: bytes, src: int, dst: int, name: str) -> bytes:
        """Our public key in the clear; our broadcast key and name sealed
        so that only the owner of ``their_public`` can read them."""
        key = crypto.pairwise_key(self.private, their_public)
        secret = self.broadcast_key + name.encode("utf-8")[:20]
        return self.public + crypto.seal_blob(key, src, dst, secret)

    def open_pair_body(self, body: bytes, src: int, dst: int):
        """(public, broadcast key, name) from a pairing message, or None."""
        if len(body) < crypto.PUBLIC_SIZE:
            return None
        public = bytes(body[:crypto.PUBLIC_SIZE])
        try:
            key = crypto.pairwise_key(self.private, public)
        except ValueError:
            return None
        secret = crypto.open_blob(key, src, dst, body[crypto.PUBLIC_SIZE:])
        if secret is None or len(secret) < crypto.KEY_SIZE:
            return None
        name = secret[crypto.KEY_SIZE:].decode("utf-8", "replace").strip()[:20]
        return public, secret[:crypto.KEY_SIZE], name
