"""WiFi level and battery for the status bar, read the way MFruit OS reads them.

    wifi_level()     0..3, 0 = not connected, None = no wireless interface
    read_battery()   (percent or None, charging) from pisugar-server

``StatusMonitor`` refreshes both on its own thread (a pisugar read can take
up to a second when the server is slow) and calls ``on_change`` when either
changes, so a render loop only ever reads cached values.
"""

from __future__ import annotations

import logging
import os
import re
import socket
import threading
from typing import Callable, NamedTuple, Optional

log = logging.getLogger("mfruit_sdk.status")

PISUGAR_SOCKETS = ("/tmp/pisugar-server.sock", "/run/pisugar-server.sock")


class Status(NamedTuple):
    wifi_level: Optional[int] = None     # None: no WiFi hardware; 0: disconnected; 1..3
    battery: Optional[int] = None        # percent, None when there is no battery
    charging: bool = False


def _read(path: str) -> str:
    try:
        with open(path) as handle:
            return handle.read()
    except OSError:
        return ""


def has_default_route() -> bool:
    for line in _read("/proc/net/route").splitlines()[1:]:
        fields = line.split()
        if len(fields) > 2 and fields[1] == "00000000":
            return True
    return False


def wifi_level() -> Optional[int]:
    """0..3 like the daemon's status icon; None if there is no wireless interface."""
    lines = _read("/proc/net/wireless").splitlines()[2:]
    if not lines:
        return None
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            quality = float(parts[2].rstrip("."))
        except ValueError:
            continue
        if quality <= 0:
            continue
        if not has_default_route():
            return 0                     # associated, but not actually online
        return 3 if quality >= 55 else 2 if quality >= 35 else 1
    return 0


def read_battery() -> tuple:
    """Battery percent and charging flag from pisugar-server, if present."""
    path = next((p for p in PISUGAR_SOCKETS if os.path.exists(p)), None)
    if path is None:
        return None, False
    level = _pisugar(path, "get battery", r"battery:\s*(-?[\d.]+)")
    charging = _pisugar(path, "get battery_charging", r"battery_charging:\s*(\w+)")
    try:
        percent = int(float(level)) if level is not None else None
    except ValueError:
        percent = None
    if percent is not None and percent < 0:
        percent = None
    return (min(100, percent) if percent is not None else None,
            str(charging).lower() == "true")


def _pisugar(path: str, command: str, pattern: str) -> Optional[str]:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            sock.connect(path)
            sock.sendall((command + "\n").encode())
            data = sock.recv(256).decode("utf-8", "replace")
    except OSError:
        return None
    match = re.search(pattern, data, flags=re.IGNORECASE)
    return match.group(1) if match else None


def read_status() -> Status:
    battery, charging = read_battery()
    return Status(wifi_level(), battery, charging)


class StatusMonitor:
    def __init__(self, interval: float = 10.0, on_change: Callable[[Status], None] | None = None,
                 reader: Callable[[], Status] = read_status):
        self.interval = interval
        self.on_change = on_change
        self.reader = reader
        self._status = Status()
        self._stop = threading.Event()
        self._thread = None

    def sample(self) -> Status:
        return self._status

    def refresh(self) -> Status:
        try:
            status = self.reader()
        except Exception:
            log.debug("status read failed", exc_info=True)
            return self._status
        changed = status != self._status
        self._status = status
        if changed and self.on_change:
            try:
                self.on_change(status)
            except Exception:
                log.exception("status handler failed")
        return status

    def start(self):
        if self._thread:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="mfruit-status", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _loop(self):
        while not self._stop.is_set():
            self.refresh()
            self._stop.wait(self.interval)
