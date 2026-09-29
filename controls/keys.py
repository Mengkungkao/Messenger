"""A USB or Bluetooth keyboard plugged into the board: type a message.

    letters, digits, punctuation   start (or go on) typing
    Enter                          send what was typed / send the picked reply
    Esc                            cancel typing / close the reply list
    Backspace                      delete the last character
    Up / Down                      older / newer message (or move in the list)
    Tab                            open the quick replies

The Whisplay daemon reads keyboards itself, but hands keys only to its
own built-in apps. So the messenger reads them too, straight from
/dev/input/event*. That takes nothing from the daemon: neither grabs the
device, and every reader gets every event. The daemon's own "Esc quits
the app" is turned off when the app registers (install.sh), so Esc can
cancel typing instead.

Only devices that have letter keys count as keyboards: the Orange Pi's
power button, its ADC buttons and its IR receiver are input devices
too. Keyboards plugged in later are picked up within `RESCAN_SECONDS`.
US layout.
"""

from __future__ import annotations

import os
import select
import struct
import threading
import time

from utils.logger import get_logger

log = get_logger("keys")

INPUT_DIR = "/dev/input"
SYS_INPUT_DIR = "/sys/class/input"
RESCAN_SECONDS = 2.0

EV_KEY = 0x01
PRESS, REPEAT = 1, 2
# struct input_event: struct timeval (two longs), __u16 type, __u16 code, __s32 value.
EVENT = struct.Struct("llHHi")
LONG_BITS = struct.calcsize("L") * 8

KEY_ESC, KEY_BACKSPACE, KEY_TAB, KEY_ENTER = 1, 14, 15, 28
KEY_A, KEY_Z, KEY_SPACE, KEY_CAPSLOCK = 30, 44, 57, 58
KEY_KPENTER, KEY_UP, KEY_DOWN = 96, 103, 108
SHIFTS = {42, 54}
NAMED = {KEY_ENTER: "enter", KEY_KPENTER: "enter", KEY_ESC: "escape",
         KEY_BACKSPACE: "backspace", KEY_UP: "up", KEY_DOWN: "down", KEY_TAB: "tab"}
# What makes an input device a keyboard rather than a button or a remote.
KEYBOARD_KEYS = (KEY_A, KEY_Z, KEY_ENTER, KEY_SPACE)

_ROWS = {
    2: "1!", 3: "2@", 4: "3#", 5: "4$", 6: "5%", 7: "6^", 8: "7&", 9: "8*", 10: "9(",
    11: "0)", 12: "-_", 13: "=+", 16: "qQ", 17: "wW", 18: "eE", 19: "rR", 20: "tT",
    21: "yY", 22: "uU", 23: "iI", 24: "oO", 25: "pP", 26: "[{", 27: "]}", 30: "aA",
    31: "sS", 32: "dD", 33: "fF", 34: "gG", 35: "hH", 36: "jJ", 37: "kK", 38: "lL",
    39: ";:", 40: "'\"", 41: "`~", 43: "\\|", 44: "zZ", 45: "xX", 46: "cC", 47: "vV",
    48: "bB", 49: "nN", 50: "mM", 51: ",<", 52: ".>", 53: "/?", KEY_SPACE: "  ",
}


def has_keys(bitmap: str, codes=KEYBOARD_KEYS) -> bool:
    """Does a sysfs `capabilities/key` bitmap include every one of `codes`?

    The file is hex words, most significant first, each one a C long.
    """
    value = 0
    for word in bitmap.split():
        value = (value << LONG_BITS) | int(word, 16)
    return all(value >> code & 1 for code in codes)


class KeyDecoder:
    """input_event bytes -> ("char", "a") / ("key", "enter")."""

    def __init__(self):
        self._shift = 0
        self._caps = False
        self._partial = b""

    def feed(self, data: bytes) -> list:
        data = self._partial + data
        whole = len(data) - len(data) % EVENT.size
        self._partial = data[whole:]
        out = []
        for offset in range(0, whole, EVENT.size):
            _, _, kind, code, value = EVENT.unpack_from(data, offset)
            if kind == EV_KEY:
                result = self._key(code, value)
                if result:
                    out.append(result)
        return out

    def _key(self, code: int, value: int):
        if code in SHIFTS:
            if value == PRESS:
                self._shift += 1
            elif value == 0:
                self._shift = max(0, self._shift - 1)
            return None
        if value not in (PRESS, REPEAT):
            return None
        if code == KEY_CAPSLOCK:
            if value == PRESS:
                self._caps = not self._caps
            return None
        if code in NAMED:
            return ("key", NAMED[code])
        pair = _ROWS.get(code)
        if not pair:
            return None
        upper = bool(self._shift)
        if pair[0].isalpha() and self._caps:
            upper = not upper
        return ("char", pair[1] if upper else pair[0])


class KeyReader:
    """Reads every keyboard on the board; calls on_char(c) and on_key(name)."""

    def __init__(self, on_char, on_key, input_dir: str = INPUT_DIR,
                 sys_dir: str = SYS_INPUT_DIR, rescan_seconds: float = RESCAN_SECONDS):
        self.on_char = on_char
        self.on_key = on_key
        self.input_dir = input_dir
        self.sys_dir = sys_dir
        self.rescan = rescan_seconds
        self._open = {}             # fd -> (name, decoder)
        self._running = False
        self._thread = None

    @property
    def connected(self) -> bool:
        """Is a keyboard plugged in (and open) right now?"""
        return bool(self._open)

    def keyboards(self) -> list:
        """eventN names of the input devices that are keyboards."""
        found = []
        try:
            names = sorted(os.listdir(self.sys_dir))
        except OSError:
            return found
        for name in names:
            if not name.startswith("event"):
                continue
            try:
                with open(os.path.join(self.sys_dir, name, "device", "capabilities",
                                       "key")) as handle:
                    if has_keys(handle.read()):
                        found.append(name)
            except (OSError, ValueError):
                continue
        return found

    def start(self):
        if self._thread:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="keys", daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=self.rescan + 1.0)
            self._thread = None

    def _scan(self):
        wanted = set(self.keyboards())
        for fd, (name, _) in list(self._open.items()):
            if name not in wanted:
                self._close(fd)
        have = {name for name, _ in self._open.values()}
        for name in sorted(wanted - have):
            try:
                fd = os.open(os.path.join(self.input_dir, name), os.O_RDONLY | os.O_NONBLOCK)
            except OSError as exc:
                log.debug("cannot read keyboard %s: %s", name, exc)
                continue
            self._open[fd] = (name, KeyDecoder())
            log.info("keyboard %s: type a message, Enter sends", name)

    def _close(self, fd):
        name, _ = self._open.pop(fd, (None, None))
        try:
            os.close(fd)
        except OSError:
            pass
        if name:
            log.info("keyboard %s gone", name)

    def _loop(self):
        try:
            while self._running:
                self._scan()
                if not self._open:
                    # Nothing plugged in: look again later, costing nothing now.
                    self._sleep(self.rescan)
                    continue
                try:
                    ready, _, _ = select.select(list(self._open), [], [], self.rescan)
                except (OSError, ValueError):
                    for fd in list(self._open):
                        self._close(fd)
                    continue
                for fd in ready:
                    self._read(fd)
        finally:
            for fd in list(self._open):
                self._close(fd)

    def _sleep(self, seconds: float):
        step = 0.2
        while self._running and seconds > 0:
            time.sleep(min(step, seconds))
            seconds -= step

    def _read(self, fd):
        try:
            data = os.read(fd, EVENT.size * 64)
        except BlockingIOError:
            return
        except OSError:
            self._close(fd)          # unplugged
            return
        if not data:
            return
        for kind, value in self._open[fd][1].feed(data):
            callback = self.on_char if kind == "char" else self.on_key
            try:
                callback(value)
            except Exception:
                log.exception("key handler failed")
