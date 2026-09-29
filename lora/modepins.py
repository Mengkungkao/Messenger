"""Is the module actually able to hear the air?

The SX126X decides what it is doing from two pins, M0 and M1:

    M0=0 M1=0   transparent -- transmits and receives
    M0=1 M1=0   wake-on-radio transmit
    M0=0 M1=1   configuration -- deaf to the air
    M0=1 M1=1   sleep -- deaf to the air

On this hardware those are GPIO 22 and 27, which are also two of the
Whisplay LCD's control lines. With the HAT's M0/M1 jumpers fitted, the
display driver decides what mode the radio is in, and it parks them
high -- so the module sits in sleep, silently. Everything above the
radio keeps working: the app records, encodes, fragments and writes to
the UART, and the log cheerfully says "sent 7 packets". Nothing goes on
the air, and nothing is received, and there is no error anywhere.

That is a bad failure to leave invisible, so the app checks and says so.

**Reading these pins is the trap.** `RPi.GPIO.setup(pin, IN)`
reconfigures the pad to an input before reading it, which both destroys
the value being measured and knocks the module out of transparent mode
if it happened to be in it. This reads the SoC's level and function
registers straight out of /dev/gpiomem instead, touching nothing.
"""

from __future__ import annotations

import mmap
import os
import time

from utils.logger import get_logger

log = get_logger("modepins")

GPIOMEM = "/dev/gpiomem"
DEVICE_TREE_COMPATIBLE = "/proc/device-tree/compatible"
GPLEV0 = 0x34
DEFAULT_M0 = 22
DEFAULT_M1 = 27

# The Whisplay HAT drives these same two lines. Its source numbers pins in
# BOARD mode, which is why the collision is easy to miss:
#
#   LED_PIN = 15 (BOARD) -> BCM 22 -> LCD backlight, and the LoRa M0
#   DC_PIN  = 13 (BOARD) -> BCM 27 -> LCD data/command, and the LoRa M1
#
# The backlight is active-low and dimmed by 1 kHz PWM ("duty_cycle =
# 100 - brightness"), so any brightness between 0 and 100 toggles M0 a
# thousand times a second and the module thrashes between transparent and
# wake-on-radio. Only a steady 100% holds M0 low, which is the one state
# where the radio can hear anything.
WHISPLAY_BACKLIGHT_BCM = 22
WHISPLAY_DC_BCM = 27


def conflicts_with_backlight(mode_pins) -> bool:
    """Does driving these mode pins fight the LCD backlight?"""
    return bool(mode_pins) and WHISPLAY_BACKLIGHT_BCM in tuple(mode_pins)

FUNCTIONS = {0: "input", 1: "output", 4: "alt0", 5: "alt1", 6: "alt2",
             7: "alt3", 3: "alt4", 2: "alt5"}

MODES = {
    (0, 0): ("transparent", True),
    (1, 0): ("wake-on-radio tx", False),
    (0, 1): ("configuration", False),
    (1, 1): ("sleep", False),
}


def _broadcom_soc() -> bool:
    """Is /dev/gpiomem the BCM2835-style register block `_read` assumes?

    Only a Raspberry Pi's is. On anything else -- an Orange Pi's H618 --
    these offsets would read unrelated registers and could report a deaf
    radio that is fine, so the pins are treated as unreadable instead.
    """
    try:
        with open(DEVICE_TREE_COMPATIBLE, "rb") as handle:
            return b"brcm,bcm2" in handle.read()
    except OSError:
        return False


def _read(m0: int, m1: int):
    """(level_m0, level_m1, func_m0, func_m1), or None if unreadable."""
    if not _broadcom_soc():
        return None
    try:
        fd = os.open(GPIOMEM, os.O_RDONLY | os.O_SYNC)
    except OSError:
        return None
    try:
        mem = mmap.mmap(fd, 4096, mmap.MAP_SHARED, mmap.PROT_READ)
    except (OSError, ValueError):
        os.close(fd)
        return None
    try:
        def reg(offset):
            return int.from_bytes(mem[offset:offset + 4], "little")

        levels = reg(GPLEV0)
        out = []
        for pin in (m0, m1):
            fsel = reg((pin // 10) * 4)
            out.append(((levels >> pin) & 1, (fsel >> ((pin % 10) * 3)) & 0b111))
        return out[0][0], out[1][0], out[0][1], out[1][1]
    finally:
        mem.close()
        os.close(fd)


def sample(m0: int = DEFAULT_M0, m1: int = DEFAULT_M1,
           samples: int = 12, seconds: float = 1.0) -> dict:
    """Watch the mode pins for a moment.

    Sampled rather than read once because the LCD toggles these lines
    constantly; a single read can catch a transient low and report a
    healthy radio that is deaf almost all the time.
    """
    seen = []
    for index in range(max(1, samples)):
        reading = _read(m0, m1)
        if reading is None:
            return {"readable": False, "transparent": False,
                    "detail": "cannot read /dev/gpiomem"}
        seen.append((reading[0], reading[1]))
        if index + 1 < samples:
            time.sleep(seconds / samples)

    transparent = sum(1 for pair in seen if pair == (0, 0))
    fraction = transparent / len(seen)
    worst = max(set(seen), key=seen.count)
    name, usable = MODES.get(worst, (f"M0={worst[0]} M1={worst[1]}", False))
    return {
        "readable": True,
        "transparent": fraction > 0.95,
        "fraction": fraction,
        "mode": name,
        "usable": usable,
        "levels": worst,
        "detail": (
            "mode pins are transparent" if fraction > 0.95 else
            f"module is in {name} mode ({int((1 - fraction) * 100)}% of the time)"
        ),
    }


def check_and_warn(m0: int = DEFAULT_M0, m1: int = DEFAULT_M1) -> dict:
    """Sample and log loudly if the radio cannot hear anything."""
    result = sample(m0, m1)
    if not result["readable"]:
        log.info("cannot inspect the mode pins: %s", result["detail"])
        return result
    if result["transparent"]:
        # Deliberately not "the module is listening". This reads the Pi's
        # own pins; it cannot tell whether they are actually wired to the
        # module. Unconnected pins read low and look perfect, which is
        # exactly the false all-clear this tool exists to avoid giving.
        log.info("mode pins M0=0 M1=0 (transparent) -- assuming they reach "
                 "the module's M0/M1")
        return result

    stuck = [pin for pin, level in ((m0, result["levels"][0]),
                                    (m1, result["levels"][1])) if level]
    log.error(
        "THE RADIO IS DEAF: %s. M0=GPIO%d M1=GPIO%d, and %s not going low. "
        "Nothing will be sent or received, though the log will still say "
        "packets were sent. Either something else is holding that line "
        "(the LCD drives GPIO 22/27, so a fitted M0/M1 jumper does exactly "
        "this), or the pin cannot sink it -- move that wire to another free "
        "GPIO and update radio.mode_pins, or tie the pin to ground.",
        result["detail"], m0, m1,
        " and ".join(f"GPIO{p}" for p in stuck) + (" is" if len(stuck) == 1 else " are"),
    )
    return result
