"""Where the shared radio files live, and the two small ones: the module
settings MFruit OS's radio setup writes, and this radio's identity.

Standard library only. Files are written atomically, readable by their
owner only, under an exclusive lock so two apps never interleave writes.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import random
import socket
import tempfile
from dataclasses import asdict, dataclass

SCHEMA = 1
RADIO_FILE = "radio.json"
DEVICE_FILE = "device.json"
BROADCAST = 0xFFFF
MAX_ADDRESS = 0xFFFE

# Bands the radio setup knows, as (lowest, highest, default) MHz. The
# E22-900T22S module covers 850-930 MHz in 1 MHz channels.
BANDS = {
    "au915": (915, 928, 920),
    "eu868": (863, 870, 868),
    "us915": (902, 928, 915),
}


def radio_dir(home: str | None = None) -> str:
    """The shared radio directory (not created here)."""
    home = (home or os.environ.get("MFRUIT_HOME") or os.environ.get("WHISPLAY_OS_HOME")
            or os.path.expanduser("~/.whisplay-os"))
    return os.path.join(home, "shared", "radio")


def ensure_dir(directory: str) -> str:
    os.makedirs(directory, mode=0o700, exist_ok=True)
    return directory


@contextlib.contextmanager
def locked(directory: str):
    """Exclusive lock over the shared radio files (one writer at a time)."""
    ensure_dir(directory)
    with open(os.path.join(directory, ".lock"), "a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_json(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_json(path: str, data: dict) -> None:
    """Atomically, readable by the owner only."""
    directory = os.path.dirname(path)
    ensure_dir(directory)
    handle, temporary = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        os.fchmod(handle, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(data, out, indent=1, sort_keys=True)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


# --- module settings ---------------------------------------------------------
@dataclass
class RadioSettings:
    """What every radio on the channel must share, as provisioned."""
    frequency_mhz: int
    air_speed: int = 2400
    power_dbm: int = 22
    net_id: int = 0
    port: str = "/dev/ttyS0"
    band: str = ""
    module: str = "sx126x"
    provisioned_at: str = ""

    def to_dict(self) -> dict:
        return dict(asdict(self), schema=SCHEMA)


def load_radio(directory: str | None = None) -> RadioSettings | None:
    """The provisioned module settings, or None if the setup has not run."""
    data = read_json(os.path.join(directory or radio_dir(), RADIO_FILE))
    if not data or not isinstance(data.get("frequency_mhz"), int):
        return None
    fields = RadioSettings.__dataclass_fields__
    try:
        return RadioSettings(**{k: v for k, v in data.items() if k in fields})
    except TypeError:
        return None


def save_radio(settings: RadioSettings, directory: str | None = None) -> None:
    directory = directory or radio_dir()
    with locked(directory):
        write_json(os.path.join(directory, RADIO_FILE), settings.to_dict())


# --- this radio's identity ------------------------------------------------------
@dataclass
class Device:
    address: int
    name: str


def default_name() -> str:
    return (socket.gethostname().split(".")[0].strip() or "radio")[:20]


def load_device(directory: str | None = None, avoid=(), legacy_address: int | None = None,
                legacy_name: str = "") -> Device:
    """This radio's Device ID and name, assigned once and then kept.

    ``legacy_address``/``legacy_name`` let an app hand over the identity it
    used before the shared store existed, so radios already paired with it
    keep recognising it.
    """
    directory = directory or radio_dir()
    path = os.path.join(directory, DEVICE_FILE)
    data = read_json(path)
    if data and _valid_address(data.get("address")):
        return Device(int(data["address"]), str(data.get("name") or default_name())[:20])
    with locked(directory):
        data = read_json(path)                    # another app may have just made it
        if data and _valid_address(data.get("address")):
            return Device(int(data["address"]), str(data.get("name") or default_name())[:20])
        if _valid_address(legacy_address):
            address = int(legacy_address)
        else:
            taken = set(avoid) | {BROADCAST, 0}
            address = random.randint(1, MAX_ADDRESS)
            while address in taken:
                address = random.randint(1, MAX_ADDRESS)
        device = Device(address, (legacy_name or default_name())[:20])
        write_json(path, dict(asdict(device), schema=SCHEMA))
        return device


def save_device(device: Device, directory: str | None = None) -> None:
    if not _valid_address(device.address):
        raise ValueError(f"Device ID must be 1-{MAX_ADDRESS}")
    directory = directory or radio_dir()
    with locked(directory):
        write_json(os.path.join(directory, DEVICE_FILE),
                   dict(address=int(device.address), name=str(device.name)[:20], schema=SCHEMA))


def _valid_address(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= MAX_ADDRESS
