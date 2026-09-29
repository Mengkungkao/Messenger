#!/usr/bin/env python3
"""Ask whisplay-daemon to launch the app, and let it own the process.

Used at boot instead of a systemd service that runs the app directly.

The daemon ties foreground focus to the process **it** spawned:

    if app.process is not None and app.process.poll() is not None:
        if self.foreground_app_id == app.app_id:
            self._release_focus(app, "process_exit")

So an app started by systemd is invisible to it. Selecting the app on
the desktop makes the daemon spawn a second copy; that copy finds the
single-instance lock held and exits, and the daemon reads its exit as
the app quitting and revokes focus -- the screen flicks to the app and
straight back to the desktop, every time. The app looks like it will not
open.

Launching through the daemon avoids all of it: the daemon owns the
process and `app.list` reports it running.
"""

from __future__ import annotations

import json
import socket
import sys
import time

SOCKET_PATH = "/tmp/whisplay-daemon.sock"
APP_ID = "whisplay-lora-messenger"


def request(cmd: str, payload: dict | None = None, timeout: float = 5.0) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(SOCKET_PATH)
        body = {"version": 1, "cmd": cmd, "payload": payload or {}}
        client.sendall((json.dumps(body) + "\n").encode("utf-8"))
        return json.loads(client.makefile("r").readline() or "{}")


def wait_for_daemon(deadline_seconds: float = 90.0) -> bool:
    """The daemon may still be starting when we are; wait rather than fail."""
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        try:
            if request("health.ping").get("ok"):
                return True
        except OSError:
            pass
        time.sleep(2.0)
    return False


def main() -> int:
    if not wait_for_daemon():
        print("whisplay-daemon never came up; not launching", file=sys.stderr)
        return 1

    try:
        listing = request("app.list").get("payload", {}).get("apps", [])
    except OSError as exc:
        print(f"could not list apps: {exc}", file=sys.stderr)
        return 1

    running = any(a.get("app_id") == APP_ID and a.get("running")
                  for a in listing)
    # Ask regardless. "running" can be stale -- the daemon only notices a
    # dead child on its next poll, so a launcher that skipped on this flag
    # silently did nothing after a restart and left the radio down. When
    # the app really is running the daemon just grants it focus, which is
    # exactly what picking it on the desktop does.
    print("already running; asking for focus" if running else "launching")

    try:
        reply = request("app.launch", {"app_id": APP_ID})
    except OSError as exc:
        print(f"could not launch: {exc}", file=sys.stderr)
        return 1
    print("launched" if reply.get("ok") else f"daemon refused: {reply}")
    return 0 if reply.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
