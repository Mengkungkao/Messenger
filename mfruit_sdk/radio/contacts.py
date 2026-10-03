"""Names of the paired radios, shared by every radio app (``contacts.json``).

Only names live here; keys are in ``keyring`` and how strongly or when a
radio was last heard is each app's own business. Standard library only.
"""

from __future__ import annotations

import os

from .settings import BROADCAST, locked, radio_dir, read_json, write_json

FILE_NAME = "contacts.json"
MAX_NAME = 20


class Contacts:
    def __init__(self, directory: str | None = None):
        self.directory = directory or radio_dir()
        self.path = os.path.join(self.directory, FILE_NAME)

    def all(self) -> dict:
        """{address: name}, re-read every time (another app may have changed it)."""
        data = read_json(self.path) or {}
        result = {}
        for key, name in (data.get("contacts") or {}).items():
            try:
                address = int(key)
            except ValueError:
                continue
            if 0 < address < BROADCAST and isinstance(name, str):
                result[address] = name[:MAX_NAME]
        return result

    def name_for(self, address: int, default: str = "") -> str:
        return self.all().get(int(address), default or f"Radio {address}")

    def set(self, address: int, name: str) -> None:
        if not 0 < int(address) < BROADCAST:
            raise ValueError("not a radio address")
        self._update(lambda contacts: contacts.__setitem__(int(address), name.strip()[:MAX_NAME]))

    def remove(self, address: int) -> None:
        self._update(lambda contacts: contacts.pop(int(address), None))

    def _update(self, change) -> None:
        with locked(self.directory):
            contacts = self.all()
            change(contacts)
            write_json(self.path, {"contacts": {str(a): n for a, n in sorted(contacts.items())},
                                   "schema": 1})
