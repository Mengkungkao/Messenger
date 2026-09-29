"""Typing messages instead of speaking them -- for testing, or no mic.

    any text     send it
    /talk        start recording; press Enter again to stop and send
    /hello       announce this radio (the other one learns our name)
    /history     print the message history
    /retry       resend the last failed message
    /help        this list
    /quit        leave

Runs on a daemon thread reading stdin, so it never holds the app open.
"""

from __future__ import annotations

import sys
import threading

HELP = __doc__.split("\n\n")[1]


class Keyboard:
    def __init__(self, on_text, on_command, stream=None):
        self.on_text = on_text
        self.on_command = on_command
        self.stream = stream or sys.stdin
        self.recording = False
        self._thread = None

    @staticmethod
    def wanted(setting: str) -> bool:
        setting = (setting or "auto").strip().lower()
        if setting in ("1", "true", "yes", "on"):
            return True
        if setting in ("0", "false", "no", "off"):
            return False
        return sys.stdin is not None and sys.stdin.isatty()

    def start(self):
        self._thread = threading.Thread(target=self._run, name="keyboard", daemon=True)
        self._thread.start()

    def _run(self):
        for raw in self.stream:
            line = raw.strip()
            if self.recording:
                # Enter while recording stops it, whatever was typed.
                self.on_command("talk-stop")
                continue
            if not line:
                continue
            if line.startswith("/"):
                self.on_command(line[1:].split()[0].lower())
            else:
                self.on_text(line)
        self.on_command("eof")
