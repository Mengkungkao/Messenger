"""The radio as the rest of the app sees it: packets in, packets out.

One reader thread sits in a blocking UART read -- no polling, no CPU
while the channel is quiet -- feeds the bytes to the deframer and hands
each packet to `on_packet`. Transmits are charged to the duty-cycle
budget first and refused when it is spent.

Every packet goes to the module's broadcast address; who it is *for* is
in the packet's own DST field (see lora/packet.py for why).
"""

from __future__ import annotations

import threading
import time

from lora import packet as pk
from lora.airtime import AirtimeBudget
from utils.logger import get_logger

log = get_logger("link")

MODULE_BROADCAST = 0xFFFF


class Link:
    def __init__(self, radio, air_speed: int = 9600,
                 duty_cycle_percent: float = 1.0):
        self.radio = radio
        self.airtime = AirtimeBudget(air_speed, duty_cycle_percent)
        self.deframer = pk.Deframer()
        self.on_packet = None       # callback(Packet)
        self.on_rssi = None         # callback(dBm) -- belongs to the last packet
        self.tx_packets = 0
        self.rx_packets = 0
        self.last_rssi = None
        self._running = False
        self._thread = None
        self._tx_lock = threading.Lock()

    # --- receive -------------------------------------------------------
    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._read_loop, name="lora-rx",
                                        daemon=True)
        self._thread.start()

    def _read_loop(self):
        while self._running:
            data = self.radio.read_blocking()
            if not data:
                if self._running:
                    time.sleep(0.2)   # a dead port returns at once; do not spin
                continue
            for kind, value in self.deframer.feed(data):
                if kind == "rssi":
                    self.last_rssi = value
                    self._call(self.on_rssi, value)
                else:
                    self.rx_packets += 1
                    log.debug("rx %s", value)
                    self._call(self.on_packet, value)

    @staticmethod
    def _call(callback, *args):
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:
            log.exception("packet handler failed")

    # --- transmit ------------------------------------------------------
    def transmit(self, packet: pk.Packet) -> bool:
        """Put one packet on the air. False if the duty cycle forbids it."""
        frame = packet.encode()
        with self._tx_lock:
            if not self.airtime.can_send(len(frame)):
                log.warning("duty cycle spent: not sending %s (%.0f s until it fits)",
                            packet, self.airtime.wait_seconds(len(frame)))
                return False
            try:
                self.radio.send(MODULE_BROADCAST, frame)
            except Exception as exc:
                log.error("transmit failed: %s", exc)
                return False
            self.airtime.record(len(frame))
            self.tx_packets += 1
        log.debug("tx %s", packet)
        return True

    def stop(self):
        self._running = False
        try:
            self.radio.wake_reader()
        except Exception:
            pass
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
