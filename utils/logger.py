"""Logging shared by every module.

Under the Whisplay daemon, stdout/stderr are appended to
`~/.whisplay-daemon/daemon-app.log`, so a plain stream handler is all
that is needed -- no rotation, no second log file to hunt for.
"""

from __future__ import annotations

import logging
import os
import sys

_CONFIGURED = False
LEVEL_ENV = "MESSENGER_LOG_LEVEL"


def _configure():
    global _CONFIGURED
    if _CONFIGURED:
        return
    level = getattr(logging, os.getenv(LEVEL_ENV, "INFO").upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)-10s %(message)s",
                          datefmt="%H:%M:%S")
    )
    root = logging.getLogger("messenger")
    root.setLevel(level)
    root.addHandler(handler)
    root.propagate = False
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    _configure()
    return logging.getLogger(f"messenger.{name}")
