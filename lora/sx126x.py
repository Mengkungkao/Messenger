"""SX126X (E22-900T22S) UART driver, rewritten for a long-running daemon.

Carried over unchanged from WalkieTalkie. The Waveshare SX126X LoRa HAT
puts an EBYTE E22 module between the host and the SX1262 chip: the host
talks UART to the module's own firmware, which drives the SX1262 over
SPI internally. So there is no SPI, BUSY, DIO1 or RESET line to manage
from Linux -- only the serial port and the M0/M1 mode pins.

Differences from the stock Waveshare `sx126x.py` that matter here:

* **The receive path blocks instead of polling.** The original spins on
  `ser.inWaiting()` inside the main loop, which pins a core at 100% for
  the entire life of the app. Here a reader thread sits in a blocking
  `read(1)`, so an idle radio costs literally no CPU. On a Zero 2 W
  running off a battery that is the single largest power win available.

* **Mode pins are optional.** M0/M1 are GPIO 22 and 27, and on this
  build the Whisplay daemon already owns both to drive the LCD. Grabbing
  them would fight the display, so by default the driver assumes the
  module was provisioned into transparent mode beforehand (see
  `provision_radio.py`) and never touches GPIO. Set `mode_pins` only if
  M0/M1 have been rewired to free lines.

* **Configuration is persistent.** The stock driver writes register
  header 0xC2, which is volatile -- every setting is lost at power-off,
  so it has to reconfigure at each start, which in turn requires the
  mode pins. We write 0xC0 instead: the module keeps its frequency,
  address and air rate across power cycles, and the app can then run
  with no GPIO access at all.
"""

from __future__ import annotations

import errno
import os
import threading
import time

import serial

from utils.logger import get_logger

log = get_logger("sx126x")

UART_BAUD = {1200: 0x00, 2400: 0x20, 4800: 0x40, 9600: 0x60,
             19200: 0x80, 38400: 0xA0, 57600: 0xC0, 115200: 0xE0}
AIR_SPEED = {1200: 0x01, 2400: 0x02, 4800: 0x03, 9600: 0x04,
             19200: 0x05, 38400: 0x06, 62500: 0x07}
POWER_DBM = {22: 0x00, 17: 0x01, 13: 0x02, 10: 0x03}
BUFFER_SIZE = {240: 0x00, 128: 0x40, 64: 0x80, 32: 0xC0}

# Register byte 0: 0xC0 persists across power-off, 0xC2 is volatile.
REG_PERSIST = 0xC0
REG_VOLATILE = 0xC2

MAX_PACKET = 240

# A port that fails is reopened, but not more often than this.
REOPEN_SECONDS = 2.0

CMDLINE = "/proc/cmdline"


class PortBusy(serial.SerialException):
    """Another program has the radio's port locked -- WalkieTalkie, usually.

    Both apps lock the port when they open it, so the second one to start
    is refused here instead of each silently getting half of every packet.
    """

    def __init__(self, port: str, holders: list):
        self.holders = holders
        super().__init__(errno.EBUSY,
                         f"{port} is in use by {', '.join(holders) or 'another program'}")


def _describe(pid: str) -> str:
    """A process in words: a Python app by its folder (WalkieTalkie,
    Messenger), since both are just "python3" otherwise."""
    try:
        with open(f"/proc/{pid}/comm") as handle:
            command = handle.read().strip()
    except OSError:
        return "a process"
    if command.startswith("python"):
        try:
            return os.path.basename(os.readlink(f"/proc/{pid}/cwd")) or command
        except OSError:
            pass
    return command


def port_users(port: str) -> list:
    """(who, pid) for each other process with this port open.

    Only processes we may look at are seen, which covers the cases that
    matter: the other app and an auto-login shell run as the same user.
    """
    real = os.path.realpath(port)
    own = os.getpid()
    users = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == own:
            continue
        try:
            descriptors = os.listdir(f"/proc/{entry}/fd")
        except OSError:
            continue  # another user's process
        for descriptor in descriptors:
            try:
                target = os.readlink(f"/proc/{entry}/fd/{descriptor}")
            except OSError:
                continue
            if target == real:
                users.append((_describe(entry), int(entry)))
                break
    return users


def port_conflicts(port: str) -> list:
    """What else is using this serial port, in words.

    Anything else reading the port takes bytes meant for the radio -- a
    fragment arrives short, fails its check, and the message arrives
    broken or not at all -- and a login shell on it hangs the port up
    whenever it restarts, which is what "[Errno 5] Input/output error" on
    a write means.
    """
    problems = []
    real = os.path.realpath(port)
    name = os.path.basename(real)
    aliases = {name, "serial0"} if name in ("ttyS0", "ttyAMA0") else {name}
    try:
        with open(CMDLINE) as handle:
            arguments = handle.read().split()
    except OSError:
        arguments = []
    if any(arg.startswith("console=") and arg[8:].split(",")[0] in aliases
           for arg in arguments):
        problems.append(f"the kernel console is on {name}")
    problems += [f"{who} ({pid}) has {name} open" for who, pid in port_users(port)]
    return problems


def band_start(freq_mhz: int) -> int:
    """Base frequency the module counts its channel offset from."""
    return 850 if freq_mhz > 850 else 410


class SX126x:
    """Transparent-mode transport: bytes in, bytes out, plus addressing."""

    def __init__(self, port: str, addr: int, freq_mhz: int,
                 uart_baud: int = 9600, mode_pins: tuple | None = None,
                 read_timeout: float | None = None):
        self.addr = addr & 0xFFFF
        self.freq_mhz = freq_mhz
        self.channel = freq_mhz - band_start(freq_mhz)
        self.mode_pins = mode_pins
        self._gpio = None
        self._tx_lock = threading.Lock()
        self.port = port
        self.uart_baud = uart_baud
        self.read_timeout = read_timeout
        self.reopens = 0
        self._closing = False
        self._reopen_lock = threading.Lock()
        self._last_reopen = -REOPEN_SECONDS

        self.ser = self._open()

        if mode_pins:
            self._setup_gpio()
            self.set_mode(0, 0)  # transparent
        log.info(
            "radio open: %s @%d baud, addr=%d, %d MHz (ch %d), mode pins %s",
            port, uart_baud, self.addr, freq_mhz, self.channel,
            mode_pins or "not used (module pre-provisioned)",
        )

    def _open(self):
        # timeout=None makes read(1) block in the kernel until a byte
        # arrives -- no wakeups, no polling, no CPU while the channel is
        # quiet. Provisioning passes a real timeout for its handshake.
        # exclusive=True takes an flock on the port; WalkieTalkie does too.
        try:
            ser = serial.Serial(self.port, self.uart_baud, timeout=self.read_timeout,
                                exclusive=True)
        except serial.SerialException as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                raise PortBusy(self.port, [who for who, _ in port_users(self.port)]) from exc
            raise
        ser.reset_input_buffer()
        return ser

    def _recover(self, failed, reason) -> bool:
        """Reopen the port after `failed` stopped working.

        True when the port in use is now a fresh one -- reopened here, or
        already by the other thread. A hung-up port stays dead for good:
        without this the radio went deaf and mute until the app was
        restarted, and the log filled with the same write error.
        """
        with self._reopen_lock:
            if self._closing:
                return False
            if self.ser is not failed:
                return True
            now = time.monotonic()
            if now - self._last_reopen < REOPEN_SECONDS:
                return False
            self._last_reopen = now
            try:
                failed.close()
            except Exception:
                pass
            try:
                self.ser = self._open()
            except Exception as exc:
                log.error("could not reopen %s: %s", self.port, exc)
                return False
            self.reopens += 1
            log.warning(
                "%s failed (%s) and was reopened. If this repeats, something "
                "else is on the port -- a login console hangs it up whenever "
                "its shell restarts. ./setup.sh moves the console off it.",
                self.port, reason)
            return True

    # --- mode pins (only when rewired off 22/27) -----------------------
    def _setup_gpio(self):
        import RPi.GPIO as GPIO  # imported lazily: absent off-device

        self._gpio = GPIO
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        for pin in self.mode_pins:
            GPIO.setup(pin, GPIO.OUT)

    def set_mode(self, m0: int, m1: int):
        if not self._gpio:
            return
        self._gpio.output(self.mode_pins[0], m0)
        self._gpio.output(self.mode_pins[1], m1)
        time.sleep(0.05)

    # --- transmit ------------------------------------------------------
    def send(self, dst_addr: int, data: bytes, channel: int | None = None):
        """Send one packet to `dst_addr` (0xFFFF broadcasts).

        The first three bytes are addressing metadata the module strips
        before transmitting; only `data` goes on the air.
        """
        if len(data) > MAX_PACKET - 3:
            raise ValueError(f"packet {len(data)} B over the {MAX_PACKET - 3} B limit")
        chan = self.channel if channel is None else channel
        header = bytes([(dst_addr >> 8) & 0xFF, dst_addr & 0xFF, chan & 0xFF])
        with self._tx_lock:
            ser = self.ser
            try:
                ser.write(header + data)
                ser.flush()
            except (serial.SerialException, OSError) as exc:
                if not self._recover(ser, exc):
                    raise
                self.ser.write(header + data)
                self.ser.flush()

    # --- receive -------------------------------------------------------
    def read_blocking(self) -> bytes:
        """Block until at least one byte arrives, then drain what is there.

        Returns b"" when the port is closed or the read is cancelled,
        which is how the reader thread learns to stop.
        """
        ser = self.ser
        try:
            first = ser.read(1)
            if not first:
                return b""
            waiting = ser.in_waiting
            return first + (ser.read(waiting) if waiting else b"")
        except (serial.SerialException, OSError, TypeError) as exc:
            self._recover(ser, exc)
            return b""

    def wake_reader(self):
        """Unblock a thread parked in `read_blocking` so it can exit."""
        try:
            self.ser.cancel_read()
        except Exception:
            pass

    def close(self):
        self._closing = True
        self.wake_reader()
        try:
            self.ser.close()
        except Exception:
            pass
        if self._gpio:
            try:
                self._gpio.cleanup(list(self.mode_pins))
            except Exception:
                pass

    # --- configuration (needs the mode pins; see provision_radio.py) ---
    def configure(self, addr: int, freq_mhz: int, air_speed: int = 9600,
                  power: int = 22, net_id: int = 0, buffer_size: int = 240,
                  crypt: int = 0, rssi: bool = True,
                  persist: bool = True) -> bool:
        """Write the module's registers. Requires M0=0, M1=1 (config mode)."""
        for name, table, value in (
            ("air speed", AIR_SPEED, air_speed), ("power", POWER_DBM, power),
            ("buffer size", BUFFER_SIZE, buffer_size),
        ):
            if value not in table:
                raise ValueError(f"unsupported {name}: {value} (have {sorted(table)})")

        self.set_mode(0, 1)
        time.sleep(0.1)

        channel = freq_mhz - band_start(freq_mhz)
        reg = bytes([
            REG_PERSIST if persist else REG_VOLATILE, 0x00, 0x09,
            (addr >> 8) & 0xFF, addr & 0xFF, net_id & 0xFF,
            UART_BAUD[9600] + AIR_SPEED[air_speed],
            # +0x20 enables ambient-noise RSSI readback.
            BUFFER_SIZE[buffer_size] + POWER_DBM[power] + 0x20,
            channel,
            # 0x40 = fixed-point (addressed) transmission; 0x80 appends a
            # per-packet RSSI byte, which `packet.Deframer` picks up.
            0x43 + (0x80 if rssi else 0x00),
            (crypt >> 8) & 0xFF, crypt & 0xFF,
        ])

        ok = False
        for attempt in range(3):
            self.ser.reset_input_buffer()
            self.ser.write(reg)
            self.ser.flush()
            time.sleep(0.3)
            reply = self.ser.read(12)
            if reply and reply[0] == 0xC1:
                ok = True
                break
            log.warning("config attempt %d got %r; retrying", attempt + 1, reply)
            time.sleep(0.3)

        self.set_mode(0, 0)
        time.sleep(0.1)
        if ok:
            self.addr = addr & 0xFFFF
            self.freq_mhz = freq_mhz
            self.channel = channel
        return ok

    def read_settings(self) -> bytes | None:
        """Read back the module's 12 configuration bytes."""
        self.set_mode(0, 1)
        time.sleep(0.1)
        self.ser.reset_input_buffer()
        self.ser.write(bytes([0xC1, 0x00, 0x09]))
        self.ser.flush()
        time.sleep(0.3)
        reply = self.ser.read(12)
        self.set_mode(0, 0)
        time.sleep(0.1)
        return reply if reply and reply[0] == 0xC1 else None


def describe_settings(reg: bytes) -> dict:
    """Decode the 12-byte register dump into something human-readable."""
    inv = lambda table, value: next((k for k, v in table.items() if v == value), None)
    channel = reg[8]
    start = 850  # E22-900T22S; a 400-series module would report 410
    return {
        "address": (reg[3] << 8) | reg[4],
        "net_id": reg[5],
        "uart_baud": inv(UART_BAUD, reg[6] & 0xE0),
        "air_speed": inv(AIR_SPEED, reg[6] & 0x07),
        "power_dbm": inv(POWER_DBM, reg[7] & 0x03),
        "buffer_size": inv(BUFFER_SIZE, reg[7] & 0xC0),
        "channel": channel,
        "frequency_mhz": start + channel,
        "fixed_transmission": bool(reg[9] & 0x40),
        "rssi_appended": bool(reg[9] & 0x80),
    }
