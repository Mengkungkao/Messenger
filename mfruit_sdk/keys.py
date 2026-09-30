"""USB and Bluetooth keyboards, from /dev/input/event* or MFruit OS's key hub.

**While MFruit OS runs it holds every keyboard exclusively** (EVIOCGRAB,
``grab=True``) and passes each key to the program that owns the screen,
through its key hub (``state/keys.sock``): itself, or the foreground app.
Without that, every key also reaches the Linux console -- on a board whose
console logs a shell in automatically, typing into an app typed into that
shell too (it restarted MFruit OS from the shell's history). An app's
reader (``app_id=...``) connects to the hub by itself when MFruit OS runs,
and reads the devices directly when it does not.

whisplay-daemon hands keys only to its own pages (and closes an external
app on Esc unless it registers ``disable_esc_exit_key``), so apps never
relied on it for keys. A reader must still ignore keys pressed while its
app did not own the screen (``InputController`` does that).

Only devices with letter keys count as keyboards: the Orange Pi's power
button, its ADC buttons and its IR receiver are input devices too. A
keyboard plugged in, or paired over Bluetooth, later is picked up at once:
the reader watches /dev/input with inotify, so it makes no wakeups at all
while nothing happens (where inotify is unavailable it looks again every
``RESCAN_SECONDS``). A keyboard that goes away while a key is held reports
that key released, so a held Space cannot leave the microphone open.
US layout.

Events are ``KeyEvent(kind, value, action, code)``:

    KeyEvent("char", "a", DOWN, 30)        printable character, down or repeat
    KeyEvent("key", "enter", DOWN, 28)     named key: down, repeat and up
"""

from __future__ import annotations

import ctypes
import ctypes.util
import fcntl
import glob
import json
import logging
import os
import select
import socket
import struct
import threading
from typing import Callable, NamedTuple, Optional

log = logging.getLogger("mfruit_sdk.keys")

INPUT_DIR = "/dev/input"
SYS_INPUT_DIR = "/sys/class/input"
RESCAN_SECONDS = 2.0
HUB_NAME = "keys.sock"
# ioctl(fd, EVIOCGRAB, 1): only this file descriptor receives the device's
# events -- not the Linux console, not other readers. _IOW('E', 0x90, int).
EVIOCGRAB = 0x40044590

EV_KEY = 0x01
UP, DOWN, REPEAT = 0, 1, 2
# struct input_event: struct timeval (two longs), __u16 type, __u16 code, __s32 value.
EVENT = struct.Struct("llHHi")
LONG_BITS = struct.calcsize("L") * 8

KEY_ESC, KEY_BACKSPACE, KEY_TAB, KEY_ENTER = 1, 14, 15, 28
KEY_A, KEY_Z, KEY_SPACE, KEY_CAPSLOCK = 30, 44, 57, 58
KEY_KPENTER, KEY_HOME, KEY_UP, KEY_PAGEUP = 96, 102, 103, 104
KEY_LEFT, KEY_RIGHT, KEY_END, KEY_DOWN, KEY_PAGEDOWN, KEY_DELETE = 105, 106, 107, 108, 109, 111
SHIFTS = {42, 54}
CONTROLS = {29, 97, 56, 100, 125, 126}          # Ctrl, Alt and Meta, left and right
NAMED = {
    KEY_ENTER: "enter", KEY_KPENTER: "enter", KEY_ESC: "escape", KEY_BACKSPACE: "backspace",
    KEY_TAB: "tab", KEY_SPACE: "space", KEY_UP: "up", KEY_DOWN: "down", KEY_LEFT: "left",
    KEY_RIGHT: "right", KEY_HOME: "home", KEY_END: "end", KEY_PAGEUP: "pageup",
    KEY_PAGEDOWN: "pagedown", KEY_DELETE: "delete",
}
# What makes an input device a keyboard rather than a button or a remote.
KEYBOARD_KEYS = (KEY_A, KEY_Z, KEY_ENTER, KEY_SPACE)

_ROWS = {
    2: "1!", 3: "2@", 4: "3#", 5: "4$", 6: "5%", 7: "6^", 8: "7&", 9: "8*", 10: "9(",
    11: "0)", 12: "-_", 13: "=+", 16: "qQ", 17: "wW", 18: "eE", 19: "rR", 20: "tT",
    21: "yY", 22: "uU", 23: "iI", 24: "oO", 25: "pP", 26: "[{", 27: "]}", 30: "aA",
    31: "sS", 32: "dD", 33: "fF", 34: "gG", 35: "hH", 36: "jJ", 37: "kK", 38: "lL",
    39: ";:", 40: "'\"", 41: "`~", 43: "\\|", 44: "zZ", 45: "xX", 46: "cC", 47: "vV",
    48: "bB", 49: "nN", 50: "mM", 51: ",<", 52: ".>", 53: "/?",
}


class KeyEvent(NamedTuple):
    kind: str        # "char" | "key"
    value: str       # the character, or the key's name
    action: int      # DOWN, REPEAT or UP (chars: DOWN or REPEAT)
    code: int


def has_keys(bitmap: str, codes=KEYBOARD_KEYS) -> bool:
    """Does a sysfs ``capabilities/key`` bitmap include every one of ``codes``?

    The file is hex words, most significant first, each one a C long.
    """
    value = 0
    for word in bitmap.split():
        value = (value << LONG_BITS) | int(word, 16)
    return all(value >> code & 1 for code in codes)


class KeyDecoder:
    """input_event bytes -> KeyEvents, for one device."""

    def __init__(self):
        self._shift = 0
        self._caps = False
        self._modifiers = set()
        self._held = {}           # code -> name, named keys currently down
        self._partial = b""

    def feed(self, data: bytes) -> list:
        data = self._partial + data
        whole = len(data) - len(data) % EVENT.size
        self._partial = data[whole:]
        out = []
        for offset in range(0, whole, EVENT.size):
            _, _, kind, code, value = EVENT.unpack_from(data, offset)
            if kind == EV_KEY:
                event = self._key(code, value)
                if event:
                    out.append(event)
        return out

    def release_all(self) -> list:
        """Key-ups for every named key still down (the device went away)."""
        events = [KeyEvent("key", name, UP, code) for code, name in self._held.items()]
        self._held.clear()
        self._shift = 0
        self._modifiers.clear()
        return events

    def _key(self, code: int, value: int):
        if code in SHIFTS:
            if value == DOWN:
                self._shift += 1
            elif value == UP:
                self._shift = max(0, self._shift - 1)
            return None
        if code in CONTROLS:
            if value == DOWN:
                self._modifiers.add(code)
            elif value == UP:
                self._modifiers.discard(code)
            return None
        if code == KEY_CAPSLOCK:
            if value == DOWN:
                self._caps = not self._caps
            return None
        if code in NAMED:
            if value == DOWN:
                self._held[code] = NAMED[code]
            elif value == UP:
                if self._held.pop(code, None) is None:
                    return None          # went down before we started reading
            return KeyEvent("key", NAMED[code], value, code)
        if value not in (DOWN, REPEAT) or self._modifiers:
            return None                  # Ctrl+C and friends are not typing
        pair = _ROWS.get(code)
        if not pair:
            return None
        upper = bool(self._shift)
        if pair[0].isalpha() and self._caps:
            upper = not upper
        return KeyEvent("char", pair[1] if upper else pair[0], value, code)


class DeviceWatch:
    """inotify on the input directory: a device node appeared, went away,
    or had its permissions set (udev gives /dev/input/eventN to the input
    group just after creating it)."""

    IN_ATTRIB, IN_MOVED_TO, IN_CREATE, IN_DELETE = 0x4, 0x80, 0x100, 0x200

    def __init__(self, path: str):
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        mask = self.IN_ATTRIB | self.IN_MOVED_TO | self.IN_CREATE | self.IN_DELETE
        if libc.inotify_add_watch(fd, os.fsencode(path), mask) < 0:
            errno = ctypes.get_errno()
            os.close(fd)
            raise OSError(errno, f"cannot watch {path}")
        self.fd = fd

    @classmethod
    def open(cls, path: str) -> Optional["DeviceWatch"]:
        try:
            return cls(path)
        except (OSError, AttributeError) as exc:     # no inotify: fall back to polling
            log.debug("no inotify on %s (%s); polling for keyboards", path, exc)
            return None

    def drain(self):
        """Discard queued notifications (the caller rescans instead)."""
        while True:
            try:
                if not os.read(self.fd, 4096):
                    return
            except BlockingIOError:
                return
            except OSError:
                return

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass


def hub_socket_path() -> Optional[str]:
    """MFruit OS's key hub, if MFruit OS is running on this board.

    While it runs, MFruit OS holds every keyboard exclusively (so the Linux
    console, the daemon and stray readers get nothing) and hands each key
    to the program that owns the screen, through this socket.
    """
    candidates = [os.environ.get("MFRUIT_KEYS_SOCKET", "")]
    for home in (os.environ.get("MFRUIT_HOME", ""), os.environ.get("WHISPLAY_OS_HOME", ""),
                 os.path.expanduser("~/.whisplay-os")):
        if home:
            candidates.append(os.path.join(home, "state", HUB_NAME))
    candidates += sorted(glob.glob(os.path.join("/home/*/.whisplay-os/state", HUB_NAME)))
    return next((path for path in candidates if path and os.path.exists(path)), None)


class KeyReader:
    """Reads the keyboards on its own thread; calls ``on_event(KeyEvent)``.

    With ``app_id`` (an app): while MFruit OS runs, keys come from its key
    hub -- only while this app owns the screen -- otherwise straight from
    the devices. MFruit OS itself reads the devices with ``grab=True``.
    ``on_devices(list)`` is called when the set of keyboards changes.
    """

    def __init__(self, on_event: Callable[[KeyEvent], None], input_dir: str = INPUT_DIR,
                 sys_dir: str = SYS_INPUT_DIR, rescan_seconds: float = RESCAN_SECONDS,
                 grab: bool = False, app_id: Optional[str] = None, hub=None,
                 on_devices: Optional[Callable[[list], None]] = None):
        self.on_event = on_event
        self.input_dir = input_dir
        self.sys_dir = sys_dir
        self.rescan = rescan_seconds
        self.grab = grab
        self.app_id = app_id
        # None: find MFruit OS's hub when there is an app_id; False: never;
        # a path: that socket.
        self.hub = hub
        self.on_devices = on_devices
        self._open = {}             # fd -> (name, decoder)
        self._lock = threading.Lock()
        self._hub_devices = None    # keyboards the hub reports, while connected to it
        self._reported = None
        self._stop = threading.Event()
        self._wake = None           # pipe that interrupts select() on stop()
        self._thread = None

    @property
    def connected(self) -> bool:
        """Is a keyboard plugged in (and readable) right now?"""
        return bool(self.devices)

    @property
    def devices(self) -> list:
        """eventN names of the keyboards being read right now."""
        if self._hub_devices is not None:
            return list(self._hub_devices)
        return sorted(name for name, _ in list(self._open.values()))

    @property
    def via_hub(self) -> bool:
        return self._hub_devices is not None

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
        self._stop.clear()
        self._wake = os.pipe()
        self._thread = threading.Thread(target=self._loop, name="mfruit-keys", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._wake:
            try:
                os.write(self._wake[1], b"x")
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._wake:
            for fd in self._wake:
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._wake = None

    def set_grab(self, grab: bool):
        """Hold the keyboards exclusively, or let other readers have them too."""
        with self._lock:
            self.grab = grab
            for fd, (name, _) in list(self._open.items()):
                self._apply_grab(fd, name)

    def _apply_grab(self, fd, name):
        try:
            fcntl.ioctl(fd, EVIOCGRAB, 1 if self.grab else 0)
        except OSError as exc:
            log.warning("cannot %s keyboard %s: %s", "grab" if self.grab else "release",
                        name, exc)

    def _report_devices(self):
        devices = self.devices
        if devices != self._reported:
            self._reported = devices
            if self.on_devices:
                try:
                    self.on_devices(devices)
                except Exception:
                    log.exception("keyboard list handler failed")

    # --------------------------------------------------------------- loop
    def _hub_path(self) -> Optional[str]:
        if self.hub is False or self.grab:
            return None
        if isinstance(self.hub, str):
            return self.hub if os.path.exists(self.hub) else None
        return hub_socket_path() if self.app_id else None

    def _loop(self):
        try:
            while not self._stop.is_set():
                path = self._hub_path()
                if path and self._run_hub(path):
                    continue            # the hub went away (MFruit OS restarting): look again
                self._run_direct()
        finally:
            self._hub_devices = None

    def _run_direct(self):
        """Read the devices until stop() -- or, for an app, until MFruit OS's hub appears."""
        wake = self._wake[0]
        watch = DeviceWatch.open(self.input_dir)
        hub_dir = None
        if self.app_id and self.hub is not False and not self.grab:
            state = os.path.dirname(hub_socket_path() or os.path.join(
                os.environ.get("MFRUIT_HOME") or os.path.expanduser("~/.whisplay-os"),
                "state", HUB_NAME))
            hub_dir = state if os.path.isdir(state) else None
        hub_watch = DeviceWatch.open(hub_dir) if hub_dir else None
        rescan = True
        try:
            while not self._stop.is_set():
                if rescan:
                    with self._lock:
                        self._scan()
                    self._report_devices()
                    rescan = False
                    if self._hub_path() and self._hub_reachable():
                        return          # MFruit OS is running: take keys from it
                # Wait for keys, for stop(), for a device to come or go --
                # or, without inotify, for the next look round.
                fds = list(self._open) + [wake] + [w.fd for w in (watch, hub_watch) if w]
                try:
                    ready, _, _ = select.select(fds, [], [],
                                                None if watch else self.rescan)
                except (OSError, ValueError):
                    with self._lock:
                        for fd in list(self._open):
                            self._close(fd)
                    self._stop.wait(self.rescan)
                    rescan = True
                    continue
                if not ready and not watch:
                    rescan = True
                for fd in ready:
                    if fd == wake:
                        continue
                    if watch and fd == watch.fd:
                        watch.drain()
                        rescan = True
                    elif hub_watch and fd == hub_watch.fd:
                        hub_watch.drain()
                        rescan = True
                    elif not self._read(fd):
                        rescan = True        # it went away: look again
        finally:
            with self._lock:
                for fd in list(self._open):
                    self._close(fd)
            for w in (watch, hub_watch):
                if w:
                    w.close()

    def _hub_reachable(self) -> bool:
        path = self._hub_path()
        if not path:
            return False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(1.0)
                probe.connect(path)
            return True
        except OSError:
            return False                 # a socket left behind by a crash

    def _run_hub(self, path: str) -> bool:
        """Take keys from MFruit OS's hub until it goes away. False: could not connect."""
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(3.0)
            sock.connect(path)
            sock.sendall((json.dumps({"app_id": self.app_id}) + "\n").encode("utf-8"))
            sock.settimeout(None)
        except OSError as exc:
            log.debug("key hub %s not reachable: %s", path, exc)
            return False
        log.info("keyboard through MFruit OS (%s)", path)
        self._hub_devices = []
        held = {}                        # code -> name: named keys down, via the hub
        wake = self._wake[0]
        buffer = b""
        try:
            while not self._stop.is_set():
                ready, _, _ = select.select([sock, wake], [], [])
                if wake in ready:
                    break
                data = sock.recv(4096)
                if not data:
                    break                # MFruit OS stopped
                buffer += data
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if line.strip():
                        self._hub_message(json.loads(line.decode("utf-8")), held)
        except (OSError, ValueError) as exc:
            log.debug("key hub connection ended: %s", exc)
        finally:
            sock.close()
            self._hub_devices = None
            self._reported = None
            # Keys held when the hub went away are let go of: a held Space
            # must not leave the microphone open.
            self._dispatch([KeyEvent("key", name, UP, code) for code, name in held.items()])
        return True

    def _hub_message(self, message: dict, held: dict):
        kind = message.get("type")
        if kind == "keyboards":
            self._hub_devices = [str(d) for d in message.get("devices", [])]
            self._report_devices()
        elif kind == "key":
            event = KeyEvent(str(message["kind"]), str(message["value"]), int(message["action"]),
                             int(message["code"]))
            if event.kind == "key":
                if event.action == DOWN:
                    held[event.code] = event.value
                elif event.action == UP:
                    held.pop(event.code, None)
            self._dispatch([event])

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
            if self.grab:
                self._apply_grab(fd, name)
            log.info("keyboard connected: %s%s", name, " (held exclusively)" if self.grab else "")

    def _close(self, fd):
        name, decoder = self._open.pop(fd, (None, None))
        try:
            os.close(fd)
        except OSError:
            pass
        if decoder is not None:
            self._dispatch(decoder.release_all())
        if name:
            log.info("keyboard gone: %s", name)

    def _read(self, fd) -> bool:
        """Read and dispatch what is waiting; False if the device went away."""
        try:
            data = os.read(fd, EVENT.size * 64)
        except BlockingIOError:
            return True
        except OSError:
            with self._lock:
                self._close(fd)          # unplugged, or the Bluetooth link dropped
            return False
        if data and fd in self._open:
            self._dispatch(self._open[fd][1].feed(data))
        return True

    def _dispatch(self, events):
        for event in events:
            try:
                self.on_event(event)
            except Exception:
                log.exception("key handler failed for %s", event)
