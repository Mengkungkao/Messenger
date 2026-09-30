"""A managed app must leave MFruit OS's launch wrapper registered."""

from types import SimpleNamespace

import pytest

from display import board


@pytest.mark.parametrize("launch", ["standalone", "standalone-with-id", "native", "adopted"])
def test_acquiring_display_preserves_managed_registration(monkeypatch, launch):
    managed = launch in ("native", "adopted")
    events = []
    options = {}

    class Proxy:
        foreground_ready = False

        def __init__(self, **kwargs):
            options.update(kwargs)

        def ping(self):
            return True

        def register(self):
            events.append("register")

        def start_event_listener(self):
            events.append("listen")

        def acquire_foreground(self, **kwargs):
            events.append("focus")

    client = SimpleNamespace(WhisplayDaemonProxy=Proxy,
                             DEFAULT_DAEMON_SOCKET_PATH="/unused.sock")
    monkeypatch.setattr(board, "_import_client", lambda: client)
    monkeypatch.setattr(board, "own_escape_key", lambda *_: events.append("escape"))
    if hasattr(board, "watch_foreground_grants"):
        monkeypatch.setattr(board, "watch_foreground_grants", lambda *_: None)
    for variable in ("WHISPLAY_APP_ID", "MFRUIT_SESSION", "WHISPLAY_OS_APP_DIR"):
        monkeypatch.delenv(variable, raising=False)
    if launch != "standalone":
        monkeypatch.setenv("WHISPLAY_APP_ID", board.APP_ID)
    if launch == "native":
        monkeypatch.setenv("WHISPLAY_OS_APP_DIR", "/managed/current")
    elif launch == "adopted":
        # Adopted checkouts have the wrapper's session but no package directory.
        monkeypatch.setenv("MFRUIT_SESSION", "test-launch-session")
    proxy, mode = board.acquire_board()
    assert mode == "daemon" and proxy.foreground_ready
    assert ("register" in events) == (not managed)
    assert events.index("escape") < events.index("focus")
    if not managed:
        assert options["launch_command"].endswith("/run.sh")
        assert options["launch_cwd"]
