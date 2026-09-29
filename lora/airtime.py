"""Duty-cycle accounting for the 868 MHz band.

EU 868 is not a free-for-all: ETSI EN 300 220 caps most of the band at
**1% duty cycle**, meaning a device may occupy the channel for at most
36 seconds in any hour. Nothing in the module enforces this -- it will
happily transmit until it cooks -- so the budget lives here, and every
transmission is checked against it before it goes out.

36 s/hour of airtime at 9600 bps is roughly 43 kB. A text message and
its ACK cost about 0.3 s together, so the budget only ever bites a
retry storm -- a link so bad every message is resent four times -- and
then it stops the radio from breaking the law rather than the user.

Set `duty_cycle_percent: 100` in config.yaml for licensed bands or
regions with no such limit (and to run bench tests unimpeded).
"""

from __future__ import annotations

import threading
import time
from collections import deque

WINDOW_SECONDS = 3600.0

# Per-packet cost the payload bits do not cover: preamble, sync word,
# header and CRC, plus the module's own turnaround. Measured behaviour
# of the E22 series is a little over 100 ms of overhead at low air rates;
# 0.12 s is a deliberately conservative estimate, because overrunning a
# legal limit is worse than under-using it.
PACKET_OVERHEAD_SECONDS = 0.12


class AirtimeBudget:
    """Rolling-window transmit-time accounting."""

    def __init__(self, air_speed_bps: int, duty_cycle_percent: float = 1.0,
                 window_seconds: float = WINDOW_SECONDS):
        self.air_speed = air_speed_bps
        self.duty_cycle = max(0.0, min(100.0, duty_cycle_percent)) / 100.0
        self.window = window_seconds
        self._events = deque()  # (monotonic_time, seconds_on_air)
        self._used = 0.0
        self._lock = threading.Lock()

    @property
    def limit_seconds(self) -> float:
        return self.window * self.duty_cycle

    @property
    def unlimited(self) -> bool:
        return self.duty_cycle >= 1.0

    def estimate(self, packet_bytes: int) -> float:
        """Seconds of airtime one packet of this size will occupy."""
        return packet_bytes * 8.0 / self.air_speed + PACKET_OVERHEAD_SECONDS

    def estimate_message(self, total_bytes: int, packet_bytes: int = 204) -> float:
        packets = max(1, -(-total_bytes // packet_bytes))
        whole, remainder = divmod(total_bytes, packet_bytes)
        seconds = whole * self.estimate(packet_bytes)
        if remainder:
            seconds += self.estimate(remainder)
        return seconds or self.estimate(0) * packets

    def _prune(self, now: float):
        cutoff = now - self.window
        while self._events and self._events[0][0] < cutoff:
            self._used -= self._events.popleft()[1]
        self._used = max(0.0, self._used)

    def used_seconds(self) -> float:
        with self._lock:
            self._prune(time.monotonic())
            return self._used

    def remaining_seconds(self) -> float:
        if self.unlimited:
            return float("inf")
        return max(0.0, self.limit_seconds - self.used_seconds())

    def fraction_used(self) -> float:
        if self.unlimited:
            return 0.0
        return min(1.0, self.used_seconds() / self.limit_seconds)

    def can_send(self, packet_bytes: int) -> bool:
        if self.unlimited:
            return True
        return self.estimate(packet_bytes) <= self.remaining_seconds()

    def record(self, packet_bytes: int) -> float:
        """Charge one transmission to the budget; returns its airtime."""
        seconds = self.estimate(packet_bytes)
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            self._events.append((now, seconds))
            self._used += seconds
        return seconds

    def wait_seconds(self, packet_bytes: int) -> float:
        """How long until this packet would fit in the budget. 0 if it fits now."""
        if self.unlimited:
            return 0.0
        need = self.estimate(packet_bytes)
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            free = self.limit_seconds - self._used
            if need <= free:
                return 0.0
            # Walk the window front until enough old airtime has aged out.
            reclaimed = 0.0
            for timestamp, seconds in self._events:
                reclaimed += seconds
                if free + reclaimed >= need:
                    return max(0.0, timestamp + self.window - now)
        return self.window
