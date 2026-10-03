"""Short audio cues, and reading messages aloud.

Cues say what happened without looking: a rising pair when the mic
opens, a falling pair when a message arrives, a bright triple when the
other radio confirms delivery, a low buzz on failure.

Text-to-speech is optional (`tts.enabled`) and uses espeak-ng, which is
small, offline and in every Debian-based image. Everything plays through
`aplay` at the card's own rate, one sound at a time behind a lock, so a
cue and a spoken message queue rather than fight over the codec.
"""

from __future__ import annotations

import math
import shutil
import struct
import subprocess
import threading

from audio import dsp
from utils.logger import get_logger

log = get_logger("player")

RATE = dsp.HARDWARE_RATE


def tone(frequency: float, seconds: float, volume: float = 0.25) -> bytes:
    """A sine burst with raised edges, so it does not click."""
    count = int(seconds * RATE)
    edge = max(1, count // 20)
    out = bytearray()
    step = 2 * math.pi * frequency / RATE
    for index in range(count):
        envelope = min(1.0, index / edge, (count - index) / edge)
        out += struct.pack("<h", int(math.sin(step * index) * envelope * volume * 32767))
    return bytes(out)


def _gap(seconds: float) -> bytes:
    return bytes(int(seconds * RATE) * 2)


CUES = {
    "listen": tone(660, 0.06) + _gap(0.03) + tone(990, 0.08),
    "sent": tone(880, 0.05),
    "delivered": tone(990, 0.05) + _gap(0.03) + tone(1320, 0.05) + _gap(0.03) + tone(1760, 0.07),
    "received": tone(1320, 0.07) + _gap(0.03) + tone(880, 0.09),
    "failed": tone(300, 0.2),
    # An SOS heard: loud two-tone siren, long enough to be noticed across a room.
    "alarm": (tone(1760, 0.18) + tone(1175, 0.18)) * 3,
}


class Player:
    def __init__(self, device: str | None, tts_voice: str = "en", tts_wpm: int = 150):
        self.device = device
        self.tts_voice = tts_voice
        self.tts_wpm = tts_wpm
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return bool(self.device) and shutil.which("aplay") is not None

    @property
    def can_speak(self) -> bool:
        return self.available and shutil.which("espeak-ng") is not None

    def cue(self, name: str):
        """Fire and forget; never blocks the caller."""
        pcm = CUES.get(name)
        if pcm and self.available:
            threading.Thread(target=self._play, args=(pcm, RATE), name="cue",
                             daemon=True).start()

    def speak(self, text: str, blocking: bool = False) -> bool:
        if not text or not self.can_speak:
            return False
        if blocking:
            return self._speak(text)
        threading.Thread(target=self._speak, args=(text,), name="tts", daemon=True).start()
        return True

    def _speak(self, text: str) -> bool:
        try:
            wav = subprocess.run(
                ["espeak-ng", "-v", self.tts_voice, "-s", str(self.tts_wpm), "--stdout", text],
                capture_output=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            log.warning("espeak-ng failed", exc_info=True)
            return False
        # espeak-ng writes a 22.05 kHz WAV; skip its 44-byte header.
        return self._play(dsp.resample(wav[44:], 22050, RATE), RATE)

    def _play(self, pcm: bytes, rate: int) -> bool:
        command = ["aplay", "-q", "-D", self.device, "-t", "raw", "-f", "S16_LE",
                   "-r", str(rate), "-c", "1", "-"]
        with self._lock:
            try:
                done = subprocess.run(command, input=pcm, stderr=subprocess.DEVNULL,
                                      timeout=len(pcm) / 2 / rate + 10)
                return done.returncode == 0
            except (OSError, subprocess.SubprocessError):
                log.warning("playback failed", exc_info=True)
                return False
