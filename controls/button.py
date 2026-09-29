"""One button, every interaction.

The Whisplay HAT has a single button, so gestures are encoded in click
count and hold duration:

    press, release, pause         -> single
    press x2 quickly              -> double
    press x3 quickly              -> triple
    press x4 quickly              -> quad   (fires immediately)
    press and keep holding        -> hold_start ... hold_end

`hold_start` is what makes push-to-talk work, and it is the reason this
detector is not the usual click counter. A long press cannot be reported
on the release edge here: the microphone has to open *while the button
is still down*, so the worker thread arms a deadline at `hold_ms` after
the press and fires `on_hold_start` when it expires with the button
still held. `on_hold_end` then fires on release with the held duration,
and no click is counted -- a talk burst must never be mistaken for a
click, or a five-second transmission would also step the contact list.

Two timing rules, both measured on this hardware rather than guessed:

* **Debounce the press edge only** (75 ms since the last accepted
  release). Gating the release edge as well swallows genuine short
  clicks, which are routinely only 30-60 ms long.
* **Click window 700 ms.** On this button, deliberate multi-clicks land
  158-522 ms apart and ordinary browsing clicks 1214 ms or more apart.
  700 ms sits in the empty band between the two clusters; the 400 ms
  default splits real quad-clicks into four singles.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from utils.logger import get_logger

log = get_logger("button")

SINGLE = "single"
DOUBLE = "double"
TRIPLE = "triple"
QUAD = "quad"

_GESTURE_BY_COUNT = {1: SINGLE, 2: DOUBLE, 3: TRIPLE, 4: QUAD}
MAX_CLICKS = 4


class GestureDetector:
    """Turns raw press/release edges into gestures and hold spans."""

    def __init__(
        self,
        on_gesture: Callable[[str], None],
        on_hold_start: Callable[[], None] | None = None,
        on_hold_end: Callable[[float], None] | None = None,
        debounce_ms: int = 75,
        click_window_ms: int = 700,
        hold_ms: int = 350,
    ):
        self.on_gesture = on_gesture
        self.on_hold_start = on_hold_start
        self.on_hold_end = on_hold_end
        self.debounce = debounce_ms / 1000.0
        self.click_window = click_window_ms / 1000.0
        self.hold = hold_ms / 1000.0

        self._cond = threading.Condition(threading.Lock())
        self._clicks = 0
        self._click_deadline = 0.0
        self._press_time = 0.0
        self._last_release = -1e9
        self._last_click = 0.0
        self._gaps = []
        self._last_gaps = []
        self._pressed = False
        self._holding = False
        self._running = False
        self._thread = None

    # --- lifecycle -----------------------------------------------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, name="gesture-detector", daemon=True
        )
        self._thread.start()

    def stop(self):
        with self._cond:
            self._running = False
            self._cond.notify_all()
        if self._thread:
            self._thread.join(timeout=1.5)

    def attach(self, board):
        board.on_button_press(self.handle_press)
        board.on_button_release(self.handle_release)

    @property
    def holding(self) -> bool:
        return self._holding

    # --- edges ---------------------------------------------------------
    def handle_press(self, *_args):
        now = time.monotonic()
        with self._cond:
            if self._pressed:
                return  # duplicate press with no intervening release
            if now - self._last_release < self.debounce:
                return  # contact chatter on the release edge
            self._pressed = True
            self._press_time = now
            self._cond.notify_all()

    def handle_release(self, *_args):
        now = time.monotonic()
        emit_gesture = None
        held_for = None
        with self._cond:
            if not self._pressed:
                return  # release with no matching press (bounce tail)
            self._pressed = False
            self._last_release = now
            held = now - self._press_time

            if self._holding:
                # A talk burst. Never counts as a click, and clears any
                # clicks queued before it.
                self._holding = False
                self._clicks = 0
                self._click_deadline = 0.0
                self._gaps = []
                held_for = held
            else:
                if self._last_click:
                    self._gaps.append(int((now - self._last_click) * 1000))
                self._last_click = now
                self._clicks += 1
                if self._clicks >= MAX_CLICKS:
                    emit_gesture = _GESTURE_BY_COUNT[MAX_CLICKS]
                    self._clicks = 0
                    self._click_deadline = 0.0
                    self._last_gaps, self._gaps = self._gaps, []
                else:
                    self._click_deadline = now + self.click_window
            self._cond.notify_all()

        if held_for is not None and self.on_hold_end:
            self._safely(self.on_hold_end, held_for)
        if emit_gesture:
            self._dispatch(emit_gesture)

    # --- worker --------------------------------------------------------
    def _loop(self):
        while True:
            fire_hold = False
            gesture = None
            with self._cond:
                if not self._running:
                    return

                # Whichever deadline is live: opening the mic, or closing
                # the click window. Only one can be pending at a time.
                if self._pressed and not self._holding:
                    remaining = (self._press_time + self.hold) - time.monotonic()
                    if remaining > 0:
                        self._cond.wait(timeout=remaining)
                        continue
                    self._holding = True
                    fire_hold = True
                elif self._click_deadline > 0.0:
                    remaining = self._click_deadline - time.monotonic()
                    if remaining > 0:
                        self._cond.wait(timeout=remaining)
                        continue
                    clicks = self._clicks
                    self._clicks = 0
                    self._click_deadline = 0.0
                    self._last_gaps, self._gaps = self._gaps, []
                    gesture = _GESTURE_BY_COUNT.get(clicks)
                else:
                    # Nothing pending: sleep until an edge wakes us. No
                    # timeout, so an idle app makes no wakeups at all.
                    self._cond.wait()
                    continue

            if fire_hold and self.on_hold_start:
                self._safely(self.on_hold_start)
            if gesture:
                self._dispatch(gesture)

    def _dispatch(self, gesture: str):
        if self._last_gaps:
            log.info("gesture: %s (gaps %s ms, window %d ms)", gesture,
                     "/".join(str(g) for g in self._last_gaps),
                     int(self.click_window * 1000))
            self._last_gaps = []
        else:
            log.info("gesture: %s", gesture)
        self._safely(self.on_gesture, gesture)

    @staticmethod
    def _safely(callback, *args):
        try:
            callback(*args)
        except Exception:
            log.exception("button handler failed")
