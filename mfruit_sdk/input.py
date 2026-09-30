"""InputController: the button and a keyboard, turned into MFruit OS actions.

Every MFruit app reads input through this class, so the same gesture or
key does the same thing in every app and in MFruit OS itself:

    action       button                 keyboard
    ----------   --------------------   -----------------------------
    next         tap                    Down, Right, Tab
    previous     2x                     Up, Left
    select       hold, then release     Enter
    back         4x                     Esc
    extra        3x                     (screen-specific; apps may map a letter)
    talk_start   hold (talk screens)    Space down (talk screens)
    talk_end     release after talking  Space up
    char         -                      a printable character
    erase        -                      Backspace
    key          -                      Home, End, PageUp, PageDown, Delete

**Talk screens.** Where ``talk()`` is true, holding the button (or Space)
talks while held -- after ``talk_press_ms`` if given, so the microphone
opens promptly; there, 3x ("extra") opens the selected item, because the
hold is taken. Everywhere else a hold *arms* at ``long_press_ms``
(``on_armed(True)``, show "release to open") and selects on release.
While ``typing()`` is true, Space types a space instead of talking.

**Ownership.** The keyboard is shared by every process (nobody grabs it) and
the button is routed by whisplay-daemon, so the controller acts only while
``active()`` is true (the app owns the screen), and only on keys it saw go
down while active: a key-up or auto-repeat of a key pressed elsewhere (the
Enter that launched this app, the Esc that closed the previous one) is
ignored. When the app loses the screen, call ``reset()``: it ends a talk in
progress and forgets half-made gestures.

``on_action`` is called with an ``Action`` from the button or keyboard
thread; keep it short (update state, wake your render loop).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, NamedTuple

from .gestures import DOUBLE, HOLD_END, HOLD_START, QUAD, TAP, TRIPLE, ButtonGestures
from .keys import DOWN, REPEAT, UP, KeyEvent, KeyReader

log = logging.getLogger("mfruit_sdk.input")

NEXT, PREVIOUS, SELECT, BACK, EXTRA = "next", "previous", "select", "back", "extra"
TALK_START, TALK_END = "talk_start", "talk_end"
CHAR, ERASE, KEY = "char", "erase", "key"

BUTTON, KEYBOARD = "button", "keyboard"

_CLICK_ACTIONS = {TAP: NEXT, DOUBLE: PREVIOUS, TRIPLE: EXTRA, QUAD: BACK}
_NAV_KEYS = {"down": NEXT, "right": NEXT, "tab": NEXT, "up": PREVIOUS, "left": PREVIOUS}
_ONCE_KEYS = {"enter": SELECT, "escape": BACK}
_OTHER_KEYS = {"home", "end", "pageup", "pagedown", "delete"}


class Action(NamedTuple):
    name: str
    source: str = BUTTON
    char: str = ""          # CHAR: the character
    key: str = ""           # KEY: the key's name; also set for keyboard actions
    held: float = 0.0       # TALK_END: seconds held


def _always() -> bool:
    return True


def _never() -> bool:
    return False


class InputController:
    def __init__(self, on_action: Callable[[Action], None], *,
                 talk: Callable[[], bool] = _never,
                 typing: Callable[[], bool] = _never,
                 active: Callable[[], bool] = _always,
                 on_armed: Callable[[bool], None] | None = None,
                 click_window_ms: int = 400, long_press_ms: int = 700, debounce_ms: int = 75,
                 talk_press_ms: int | None = None,
                 keyboard: bool = True, app_id: str | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 threaded: bool = True):
        self.on_action = on_action
        self.talk = talk
        self.typing = typing
        self.active = active
        self.on_armed = on_armed or (lambda armed: None)
        self.clock = clock
        hold_after = None
        if talk_press_ms is not None:
            # Talking should start promptly; opening from a menu stays a
            # deliberate hold, as in MFruit OS.
            talk_s, select_s = talk_press_ms / 1000.0, long_press_ms / 1000.0
            hold_after = lambda: talk_s if self.talk() else select_s  # noqa: E731
        self.gestures = ButtonGestures(self._on_gesture, click_window_ms=click_window_ms,
                                       long_press_ms=long_press_ms, debounce_ms=debounce_ms,
                                       clock=clock, threaded=threaded, hold_after=hold_after)
        # The app's id lets MFruit OS's key hub route keys to it while it has
        # the screen (see keys.py); WHISPLAY_APP_ID is set by mfruit-run.
        app_id = app_id or os.environ.get("WHISPLAY_APP_ID") or None
        self.keys = KeyReader(self.key_event, app_id=app_id) if keyboard else None
        self._lock = threading.RLock()
        self._armed = False
        self._talking = None        # None | BUTTON | KEYBOARD
        self._talk_started = 0.0
        self._owned = set()         # key codes that went down while we were active
        self._was_active = True

    # ------------------------------------------------------------ lifecycle
    def attach(self, board):
        """Wire a board's button callbacks (``on_button_press`` / ``on_button_release``)."""
        board.on_button_press(self.press)
        board.on_button_release(self.release)

    def start(self):
        self.gestures.start()
        if self.keys is not None:
            self.keys.start()

    def stop(self):
        self.gestures.stop()
        if self.keys is not None:
            self.keys.stop()

    @property
    def keyboard_connected(self) -> bool:
        return bool(self.keys is not None and self.keys.connected)

    @property
    def armed(self) -> bool:
        return self._armed

    @property
    def talking(self) -> bool:
        return self._talking is not None

    def reset(self):
        """The app lost (or regained) the screen: end any talk, forget gestures and keys."""
        with self._lock:
            self.gestures.reset()
            self._owned.clear()
            self._disarm()
            talking, self._talking = self._talking, None
            held = self.clock() - self._talk_started
        if talking:
            self._emit(Action(TALK_END, talking, held=held))

    def _check_active(self) -> bool:
        active = bool(self.active())
        if active != self._was_active:
            self._was_active = active
            log.debug("input %s", "active" if active else "inactive")
            self.reset()
        return active

    # --------------------------------------------------------------- button
    def press(self, *_args):
        if self._check_active():
            self.gestures.press()

    def release(self, *_args):
        if self._check_active():
            self.gestures.release()

    def _on_gesture(self, gesture: str, held: float):
        if not self._check_active():
            return
        if gesture == HOLD_START:
            with self._lock:
                if self._talking is not None:
                    return               # already talking from the keyboard
                if self.talk():
                    self._talking = BUTTON
                    self._talk_started = self.clock() - held
                else:
                    self._armed = True
            if self._talking == BUTTON:
                self._emit(Action(TALK_START, BUTTON))
            else:
                self.on_armed(True)
            return
        if gesture == HOLD_END:
            with self._lock:
                talking = self._talking == BUTTON
                armed = self._armed
                if talking:
                    self._talking = None
                self._disarm()
            if talking:
                self._emit(Action(TALK_END, BUTTON, held=held))
            elif armed:
                self._emit(Action(SELECT, BUTTON))
            return
        name = _CLICK_ACTIONS.get(gesture)
        if name:
            self._emit(Action(name, BUTTON))

    def _disarm(self):
        if self._armed:
            self._armed = False
            self.on_armed(False)

    # ------------------------------------------------------------- keyboard
    def key_event(self, event: KeyEvent):
        """Feed one KeyEvent (the reader thread calls this; tests may too)."""
        if not self._check_active():
            return
        with self._lock:
            if event.action == DOWN:
                self._owned.add(event.code)
            elif event.code not in self._owned:
                return                   # its press happened while someone else had the screen
            elif event.action == UP:
                self._owned.discard(event.code)
        if event.kind == "char":
            self._emit(Action(CHAR, KEYBOARD, char=event.value))
            return
        name = event.value
        if name == "space":
            self._space(event.action)
        elif name in _NAV_KEYS and event.action in (DOWN, REPEAT):
            self._emit(Action(_NAV_KEYS[name], KEYBOARD, key=name))
        elif name in _ONCE_KEYS and event.action == DOWN:
            self._emit(Action(_ONCE_KEYS[name], KEYBOARD, key=name))
        elif name == "backspace" and event.action in (DOWN, REPEAT):
            self._emit(Action(ERASE, KEYBOARD, key=name))
        elif name in _OTHER_KEYS and event.action == DOWN:
            self._emit(Action(KEY, KEYBOARD, key=name))

    def _space(self, action: int):
        if action == DOWN:
            with self._lock:
                talk = self._talking is None and self.talk() and not self.typing()
                if talk:
                    self._talking = KEYBOARD
                    self._talk_started = self.clock()
            if talk:
                self._emit(Action(TALK_START, KEYBOARD, key="space"))
            elif self._talking is None:
                self._emit(Action(CHAR, KEYBOARD, char=" ", key="space"))
        elif action == REPEAT:
            if self._talking is None and self.typing():
                self._emit(Action(CHAR, KEYBOARD, char=" ", key="space"))
        elif action == UP:
            with self._lock:
                talking = self._talking == KEYBOARD
                if talking:
                    self._talking = None
                held = self.clock() - self._talk_started
            if talking:
                self._emit(Action(TALK_END, KEYBOARD, key="space", held=held))

    # ----------------------------------------------------------------- out
    def _emit(self, action: Action):
        log.debug("action %s (%s)", action.name, action.source)
        try:
            self.on_action(action)
        except Exception:
            log.exception("input handler failed for %s", action.name)
