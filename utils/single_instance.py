"""One radio, one process.

Two copies of this app running at once is not merely wasteful, it is
actively destructive: each asks the daemon for the foreground, every
grant revokes the other one's session, and the two ping-pong focus
between themselves several times a second. The screen thrashes, both
processes burn a core, and neither ever settles -- observed at 98% CPU
and 24 focus grants per second.

They would also both hold /dev/ttyS0 and interleave bytes into the same
LoRa module, which corrupts every frame in both directions.

An `flock` on a file in the data directory is enough. It is released
automatically when the process dies, however it dies, so a crashed or
SIGKILLed instance never leaves a stale lock behind -- which a PID file
would.
"""

from __future__ import annotations

import fcntl
import os

from utils.logger import get_logger

log = get_logger("lock")

LOCK_NAME = "messenger.lock"


class AlreadyRunning(RuntimeError):
    pass


class SingleInstance:
    """Context manager holding an exclusive lock for the app's lifetime."""

    def __init__(self, data_dir):
        self.path = data_dir / LOCK_NAME
        self._handle = None

    def acquire(self):
        # Append mode, not "w": opening for write truncates immediately,
        # which would erase the running instance's pid before we even
        # discover the lock is held -- turning a useful error message
        # into "pid unknown".
        self._handle = open(self.path, "a+")
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._handle.seek(0)
            other = self._handle.read().strip()
            self._handle.close()
            self._handle = None
            raise AlreadyRunning(
                f"another instance is already running (pid {other or 'unknown'})"
            )
        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write(str(os.getpid()))
        self._handle.flush()
        return self

    def release(self):
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
        except OSError:
            pass
        self._handle = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_exc):
        self.release()
