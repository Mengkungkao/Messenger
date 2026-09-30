"""One button -> tap, double, triple, quad, hold_start, hold_end.

The same recogniser as MFruit OS's launcher, on a thread instead of an
event loop:

    press, release, quiet for click_window   -> "tap"
    two / three clicks inside the window     -> "double" / "triple"
    four clicks                              -> "quad"  (fires on the 4th release)
    press and keep holding                   -> "hold_start" at long_press,
                                                "hold_end" on release (held seconds)

What a hold *means* is decided by ``InputController``: on a talk screen
hold_start opens the microphone while the button is still down; everywhere
else the hold only arms and acts on release, so the release can never leak
to whatever owns the screen next (MFruit OS root cause RC1).

Timing rules, both measured on Whisplay hardware:

* Debounce the press edge only (``debounce_ms`` after the last accepted
  release). Gating releases as well swallows genuine 30-60 ms clicks.
* A hold is never counted as a click, and a tap made just before a hold is
  reported first ("tap" then "hold_start").

Events are queued under the state lock and delivered in order by whichever
thread gets there first, outside that lock, so a slow handler cannot
reorder "hold_start"/"hold_end" and handlers may call back into the
recogniser.
"""

from __future__ import annotations

import collections
import logging
import threading
import time
from typing import Callable

log = logging.getLogger("mfruit_sdk.gestures")

TAP, DOUBLE, TRIPLE, QUAD = "tap", "double", "triple", "quad"
HOLD_START, HOLD_END = "hold_start", "hold_end"
CLICKS = {1: TAP, 2: DOUBLE, 3: TRIPLE, 4: QUAD}


class ButtonGestures:
    def __init__(self, on_gesture: Callable[[str, float], None], click_window_ms: int = 400,
                 long_press_ms: int = 700, debounce_ms: int = 75,
                 clock: Callable[[], float] = time.monotonic, threaded: bool = True,
                 hold_after: Callable[[], float] | None = None):
        self.on_gesture = on_gesture
        self.click_window = click_window_ms / 1000.0
        self.long_press = long_press_ms / 1000.0
        # Seconds until a still-held press is a hold, asked at each press
        # (a talk screen starts talking sooner than a menu arms).
        self.hold_after = hold_after
        self.debounce = debounce_ms / 1000.0
        self.clock = clock
        self._lock = threading.Condition()
        self._dispatching = threading.RLock()
        self._queue = collections.deque()
        self._pressed = False
        self._press_at = 0.0
        self._hold_at = 0.0          # when a still-held press becomes a hold
        self._holding = False
        self._clicks = 0
        self._burst_at = 0.0         # when collected clicks resolve
        self._last_release = -1e9
        self._running = False
        self._thread = None
        self._threaded = threaded

    # ------------------------------------------------------------ lifecycle
    def start(self):
        if not self._threaded or (self._thread and self._thread.is_alive()):
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="mfruit-gestures", daemon=True)
        self._thread.start()

    def stop(self):
        with self._lock:
            self._running = False
            self._lock.notify_all()
        if self._thread:
            self._thread.join(timeout=1.5)
            self._thread = None

    @property
    def pressed(self) -> bool:
        return self._pressed

    @property
    def holding(self) -> bool:
        return self._holding

    def reset(self) -> bool:
        """Forget any gesture in progress. Returns True if a hold was cut short."""
        with self._lock:
            was_holding = self._holding
            self._pressed = self._holding = False
            self._clicks = 0
            self._hold_at = self._burst_at = 0.0
            self._queue.clear()
            self._lock.notify_all()
        return was_holding

    # ---------------------------------------------------------------- edges
    def press(self, *_args):
        now = self.clock()
        with self._lock:
            if self._pressed:
                return                   # a second press without a release
            if now - self._last_release < self.debounce:
                return                   # contact chatter after a release
            self._pressed = True
            self._press_at = now
            self._hold_at = now + (self.hold_after() if self.hold_after else self.long_press)
            self._burst_at = 0.0         # wait for this press before resolving clicks
            self._lock.notify_all()

    def release(self, *_args):
        now = self.clock()
        with self._lock:
            if not self._pressed:
                return                   # release without a press we accepted
            self._pressed = False
            self._last_release = now
            self._hold_at = 0.0
            if self._holding:
                self._holding = False
                self._queue.append((HOLD_END, now - self._press_at))
            else:
                self._clicks += 1
                if self._clicks >= 4:
                    self._clicks = 0
                    self._queue.append((QUAD, 0.0))
                else:
                    self._burst_at = now + self.click_window
            self._lock.notify_all()
        self._drain()

    # --------------------------------------------------------------- timers
    def poll(self):
        """Fire whatever is due (the worker thread calls this; tests call it directly)."""
        now = self.clock()
        with self._lock:
            if self._pressed and self._hold_at and now >= self._hold_at:
                self._hold_at = 0.0
                self._flush_clicks()     # tap-then-hold: the tap happens first
                self._holding = True
                self._queue.append((HOLD_START, now - self._press_at))
            if not self._pressed and self._burst_at and now >= self._burst_at:
                self._burst_at = 0.0
                self._flush_clicks()
        self._drain()

    def _flush_clicks(self):
        count, self._clicks = self._clicks, 0
        if count:
            self._queue.append((CLICKS[min(count, 4)], 0.0))

    def _loop(self):
        while True:
            self.poll()
            with self._lock:
                if not self._running:
                    return
                if self._queue:
                    continue
                # Computed and waited on under the lock, so a press that
                # lands now notifies this wait instead of being missed.
                # Nothing pending: sleep until an edge (or stop) wakes us,
                # so an idle app makes no wakeups at all.
                deadlines = [d for d in (self._hold_at, self._burst_at) if d]
                if not deadlines:
                    self._lock.wait()
                    continue
                wait = min(deadlines) - self.clock()
                if wait > 0:
                    self._lock.wait(timeout=wait)

    def _drain(self):
        with self._dispatching:
            while True:
                with self._lock:
                    if not self._queue:
                        return
                    name, held = self._queue.popleft()
                log.debug("gesture %s", name)
                try:
                    self.on_gesture(name, held)
                except Exception:
                    log.exception("gesture handler failed for %s", name)
