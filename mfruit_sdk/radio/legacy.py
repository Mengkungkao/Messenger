"""Seeding the shared radio store from WalkieTalkie's own files, once.

Before the shared store existed, WalkieTalkie kept its Device ID and name in
``settings.json`` and its keys in ``keys.json`` under ``~/.whisplay-walkie``
(or ``WALKIE_DATA_DIR``). Pairing is keyed by Device ID, so a radio that
changed its ID would no longer be recognised by the radios it paired with:
every radio app therefore calls ``adopt_walkietalkie()`` before it assigns an
ID of its own. The original files are never changed or removed.

Standard library only.
"""

from __future__ import annotations

import logging
import os
import shutil

from .settings import (DEVICE_FILE, _valid_address, locked, radio_dir, read_json,
                       write_json)

log = logging.getLogger("mfruit_sdk.radio.legacy")

KEYS_FILE = "keys.json"


def walkie_data_dir() -> str:
    return os.environ.get("WALKIE_DATA_DIR") or os.path.expanduser("~/.whisplay-walkie")


def walkie_identity() -> tuple:
    """(Device ID or None, name or "") from WalkieTalkie's settings.json."""
    data = read_json(os.path.join(walkie_data_dir(), "settings.json")) or {}
    address = (data.get("radio") or {}).get("address")
    name = (data.get("identity") or {}).get("callsign") or ""
    return (int(address) if _valid_address(address) else None,
            str(name)[:20] if isinstance(name, str) else "")


def adopt_keys(legacy_path: str, directory: str | None = None) -> bool:
    """Copy an app's own pre-shared ``keys.json`` into the shared store, once:
    only when the shared store has no keys yet. The original file stays where
    it was. True if copied."""
    directory = directory or radio_dir()
    target = os.path.join(directory, KEYS_FILE)
    with locked(directory):
        if os.path.exists(target) or not os.path.isfile(legacy_path):
            return False
        data = read_json(legacy_path)
        if not data or "private" not in data:
            return False
        shutil.copyfile(legacy_path, target + ".adopt")
        os.chmod(target + ".adopt", 0o600)
        os.replace(target + ".adopt", target)
    log.info("adopted the existing radio keys from %s", legacy_path)
    return True


def adopt_walkietalkie(directory: str | None = None) -> dict:
    """Seed ``device.json`` and ``keys.json`` from WalkieTalkie's files when the
    shared store has none yet. Returns what was adopted."""
    directory = directory or radio_dir()
    adopted = {}
    address, name = walkie_identity()
    device_path = os.path.join(directory, DEVICE_FILE)
    if address is not None:
        with locked(directory):
            if read_json(device_path) is None:
                write_json(device_path, {"address": address, "name": name or "", "schema": 1})
                adopted["device"] = address
                log.info("adopted Device ID %d from WalkieTalkie", address)
    if adopt_keys(os.path.join(walkie_data_dir(), KEYS_FILE), directory):
        adopted["keys"] = True
    return adopted
