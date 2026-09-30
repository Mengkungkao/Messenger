"""Small whisplay-daemon requests an app needs besides its board client.

    own_escape_key(app_id)   make Esc reach the app as "back" (the daemon
                             otherwise closes a foreground app on Esc)

Call it right after the app registers and **before it takes the screen**:
every ``app.register`` makes whisplay-daemon redraw its own desktop straight
to the LCD, which flashes over an app that already owns the screen (unless
MFruit OS's background wrapper is installed).

The Whisplay runtime client's ``register()`` does not send
``disable_esc_exit_key``, and the daemon only changes the fields a
registration names, so a second, partial ``app.register`` sets just this
flag and leaves the rest (name, launch command, gestures) as they are.
"""

from __future__ import annotations

import json
import logging
import socket

log = logging.getLogger("mfruit_sdk.daemon")

SOCKET_PATH = "/tmp/whisplay-daemon.sock"


def request(cmd: str, payload: dict, socket_path: str = SOCKET_PATH, timeout: float = 3.0) -> dict:
    body = json.dumps({"version": 1, "cmd": cmd, "payload": payload}) + "\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(socket_path)
        sock.sendall(body.encode("utf-8"))
        line = sock.makefile("r").readline().strip()
    if not line:
        raise RuntimeError("empty response from whisplay-daemon")
    response = json.loads(line)
    if not response.get("ok"):
        raise RuntimeError(response.get("error", f"{cmd} failed"))
    return response.get("payload") or {}


def own_escape_key(app_id: str, socket_path: str = SOCKET_PATH) -> bool:
    """Stop the daemon closing this app on Esc; returns False if the daemon is unreachable."""
    try:
        request("app.register", {"app_id": app_id, "disable_esc_exit_key": True}, socket_path)
    except (OSError, ValueError, RuntimeError) as exc:
        log.warning("could not claim the Esc key for %s: %s", app_id, exc)
        return False
    return True
