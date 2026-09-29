"""Getting hold of the Whisplay hardware.

Under `whisplay-daemon` the app never touches SPI or GPIO: the daemon
owns the LCD, the RGB LED, the backlight and the button, and hands out a
shared framebuffer plus an event stream. That separation is what makes
the LoRa HAT workable at all here -- the daemon is already driving GPIO
22 and 27 for the display, which are the same pins the SX126X uses for
M0/M1, so an app that grabbed GPIO directly would fight the screen.

`exit_gesture` is "none" on purpose. The daemon's default is four clicks
inside a 3-second window, and this app uses single clicks to scroll the
message history -- a user reading briskly produces four clicks in three
seconds routinely and would be killed while a message was in flight.
With "none" the app owns every gesture and handles exit itself, using
the much tighter 700 ms click window.

Carried over from WalkieTalkie, where every workaround below was found
the hard way on the same HAT and daemon.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

from utils.logger import get_logger

log = get_logger("board")

APP_ID = "whisplay-lora-messenger"
DISPLAY_NAME = "Messenger"
ICON = "MS"
EXIT_GESTURE = "none"
PRIORITY = 45

FOREGROUND_RETRY_SECONDS = 5.0


def _runtime_candidates() -> list:
    project_root = Path(__file__).resolve().parents[1]
    candidates = []
    env_path = os.getenv("WHISPLAY_RUNTIME")
    if env_path:
        candidates.append(Path(env_path))
    candidates += [
        Path.home() / "Whisplay" / "runtime",
        project_root.parent / "Whisplay" / "runtime",
        Path("/home/pi/Whisplay/runtime"),
        Path("/opt/whisplay/runtime"),
        Path("/usr/local/share/whisplay/runtime"),
    ]
    return candidates


def _import_client():
    for candidate in _runtime_candidates():
        if not (candidate / "whisplay_client.py").is_file():
            continue
        path = str(candidate)
        if path not in sys.path:
            sys.path.append(path)
        try:
            import whisplay_client
        except ImportError as exc:
            # Expected off-device: the runtime imports spidev/gpiod.
            log.warning("whisplay runtime at %s is unusable: %s", path, exc)
            return None
        log.info("using whisplay runtime at %s", path)
        return whisplay_client
    return None


class NullBoard:
    """Headless stand-in for development machines and PNG previews."""

    width = 240
    height = 280
    foreground_ready = True

    def fill_screen(self, colour): pass
    def draw_image(self, x, y, width, height, pixels): pass
    def set_backlight(self, brightness): pass
    def set_rgb(self, r, g, b): pass
    def set_rgb_fade(self, r, g, b, duration_ms=100): pass
    def button_pressed(self): return False
    def on_button_press(self, callback): pass
    def on_button_release(self, callback): pass
    def on_exit_request(self, callback): pass
    def on_focus_revoked(self, callback): pass
    def start_event_listener(self): pass
    def release_focus(self): pass
    def prepare_exit(self): pass
    def cleanup(self): pass


def start_foreground_retry(proxy, on_acquired=None,
                            interval: float = FOREGROUND_RETRY_SECONDS):
    """Keep asking for the screen until whoever holds it lets go.

    Without this an app autostarted at boot sits headless forever --
    receiving packets and drawing to nothing -- because another app
    happened to own the foreground at the moment it started.
    """

    def loop():
        attempts = 0
        while not getattr(proxy, "foreground_ready", False):
            time.sleep(interval)
            attempts += 1
            try:
                proxy.acquire_foreground(timeout_sec=1.0)
            except Exception:
                continue
            proxy.foreground_ready = True
            log.info("foreground acquired after %d attempt(s)", attempts)
            if on_acquired:
                try:
                    on_acquired()
                except Exception:
                    log.exception("foreground callback failed")
            return

    threading.Thread(target=loop, name="foreground-retry", daemon=True).start()


def _reattach_framebuffer(proxy, session_token: str):
    """Adopt a session the daemon granted us and re-map its framebuffer."""
    proxy._session_token = session_token
    payload = proxy._send_request("framebuffer.acquire", {
        "app_id": APP_ID, "session_token": session_token,
    })["payload"]
    proxy._attach_framebuffer(payload["buffer_handle"], int(payload["stride"]))


def watch_foreground_grants(proxy, on_reacquired, socket_path: str):
    """Re-attach the framebuffer when the daemon hands the screen back.

    This exists because of a gap in `whisplay_client`. When the user
    returns to an app that is still running, the daemon does not wait to
    be asked: `_launch_app` sees the process alive, calls `_grant_focus`
    itself -- which mints a **new session token and reallocates the
    framebuffer** -- and broadcasts `app_foreground_acquired`. The
    client's event loop handles `button_pressed`, `button_released`,
    `app_exit_requested` and `app_focus_revoked`, and silently drops
    that one.

    The app is then left holding a torn-down mmap and a stale token
    while the daemon, believing the app is foreground, stops drawing the
    desktop. Nobody paints anything: the screen goes black and stays
    black until the app is killed.

    Two traps, both hit while fixing this:

    * **Do not poll `acquire_foreground`.** After a back gesture the app
      is still running with the desktop showing, and a retry loop would
      snatch the screen from the user every few seconds.
    * **Do not call `acquire_foreground` from this handler.** It sends
      `app.focus.acquire`, the daemon answers by granting focus, and
      granting focus broadcasts `app_foreground_acquired` -- straight
      back into this handler, forever. The session token arrives in the
      event payload, so adopt that and ask only for the framebuffer;
      `framebuffer.acquire` broadcasts nothing. A token we already hold
      is our own grant echoing back and is ignored.
    """

    def loop():
        while True:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.connect(socket_path)
                    body = {"version": 1, "cmd": "events.subscribe",
                            "payload": {"app_id": APP_ID}}
                    client.sendall((json.dumps(body) + "\n").encode("utf-8"))
                    reader = client.makefile("r")
                    if not reader.readline():
                        raise RuntimeError("subscription ack missing")
                    for line in reader:
                        line = line.strip()
                        if not line:
                            continue
                        event = json.loads(line)
                        # Only this one event; the client owns the rest.
                        if event.get("event") != "app_foreground_acquired":
                            continue
                        token = (event.get("payload") or {}).get("session_token")
                        if not token:
                            continue
                        if token == getattr(proxy, "_session_token", None):
                            continue  # our own acquire, echoed back
                        log.info("daemon granted the screen back; re-attaching")
                        try:
                            _reattach_framebuffer(proxy, token)
                        except Exception:
                            log.warning("re-attach failed", exc_info=True)
                            continue
                        proxy.foreground_ready = True
                        if on_reacquired:
                            on_reacquired()
            except Exception:
                time.sleep(1.0)

    threading.Thread(target=loop, name="foreground-watch", daemon=True).start()


def acquire_board(launch_command: str | None = None,
                  launch_cwd: str | None = None,
                  on_foreground_acquired=None):
    """Return (board, mode): daemon | waiting | direct | headless."""
    client = _import_client()
    if client is None:
        log.warning("whisplay runtime not found; running headless")
        return NullBoard(), "headless"

    project_root = Path(__file__).resolve().parents[1]
    launch_command = launch_command or str(project_root / "run.sh")
    launch_cwd = launch_cwd or str(project_root)

    proxy = None
    daemon_alive = False
    try:
        proxy = client.WhisplayDaemonProxy(
            socket_path=client.DEFAULT_DAEMON_SOCKET_PATH,
            app_id=APP_ID, display_name=DISPLAY_NAME, icon=ICON,
            launch_command=launch_command, launch_cwd=launch_cwd,
            persist=True, exit_gesture=EXIT_GESTURE, priority=PRIORITY,
            use_daemon_default_log=True,
        )
        daemon_alive = proxy.ping()
    except Exception:
        log.exception("could not talk to the whisplay daemon")

    if not daemon_alive:
        try:
            board = client.WhisplayBoard()
            log.info("no daemon; driving the HAT directly over SPI")
            board.foreground_ready = True
            return board, "direct"
        except Exception:
            log.exception("direct hardware access failed; running headless")
            return NullBoard(), "headless"

    proxy.register()
    proxy.start_event_listener()
    try:
        proxy.acquire_foreground(timeout_sec=2.0)
        proxy.foreground_ready = True
        # Started only now: our own acquire triggers a grant broadcast,
        # and the watcher recognises it as ours by the session token --
        # which does not exist until acquire_foreground has returned.
        watch_foreground_grants(proxy, on_foreground_acquired,
                                client.DEFAULT_DAEMON_SOCKET_PATH)
        return proxy, "daemon"
    except Exception:
        # Another app owns the screen. Keep the radio running and take
        # the display over as soon as it is free.
        proxy.foreground_ready = False
        log.warning("another app holds the screen; retrying in the background")
        start_foreground_retry(proxy, on_foreground_acquired)
        watch_foreground_grants(proxy, on_foreground_acquired,
                                client.DEFAULT_DAEMON_SOCKET_PATH)
        return proxy, "waiting"
