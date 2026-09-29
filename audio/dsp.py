"""Sample-rate conversion for speech recognition.

The sound cards run at 48 kHz -- the Orange Pi's Whisplay card offers
nothing else -- and the ASR engines want 16 kHz. ALSA's plug layer would
convert, but its "linear" converter does not filter, and folds
everything above 8 kHz back into the voice band as hiss (WalkieTalkie
measured exactly this). So the card is opened at 48 kHz and converted
here, low-pass first.

numpy only: scipy is not installed on the radios.
"""

from __future__ import annotations

import numpy as np

HARDWARE_RATE = 48000
ASR_RATE = 16000


def _lowpass(taps: int, cutoff: float) -> np.ndarray:
    """Windowed-sinc low-pass; `cutoff` in cycles per sample."""
    n = np.arange(taps) - (taps - 1) / 2
    h = 2 * cutoff * np.sinc(2 * cutoff * n) * np.kaiser(taps, 8.6)
    return h / h.sum()


def _filter(x: np.ndarray, h: np.ndarray) -> np.ndarray:
    """Linear-phase FIR by FFT, delay removed: same length out."""
    if not x.size:
        return x
    n = x.size + h.size - 1
    size = 1 << (n - 1).bit_length()
    y = np.fft.irfft(np.fft.rfft(x, size) * np.fft.rfft(h, size), size)[:n]
    delay = (h.size - 1) // 2
    return y[delay:delay + x.size]


def resample(pcm: bytes, from_rate: int, to_rate: int) -> bytes:
    """16-bit mono PCM from one rate to another, filtered."""
    if from_rate == to_rate or not pcm:
        return pcm
    x = np.frombuffer(pcm[: len(pcm) & ~1], dtype="<i2").astype(np.float64)
    if to_rate < from_rate:
        # Pass to 90% of the new Nyquist; be well down by it.
        x = _filter(x, _lowpass(255, 0.45 * to_rate / from_rate))
    if from_rate % to_rate == 0:
        y = x[:: from_rate // to_rate]
    else:
        count = int(x.size * to_rate / from_rate)
        positions = np.arange(count) * (from_rate / to_rate)
        y = np.interp(positions, np.arange(x.size), x)
    return np.clip(np.round(y), -32768, 32767).astype("<i2").tobytes()


# Measured on the Zero 2 W's Whisplay mic in a quiet room: 70% of the
# captured energy was below 100 Hz (mains hum, handling rumble) plus a DC
# offset of 330, and only 17% was in the 300-3400 Hz speech band. None of
# it helps a recogniser, and all of it hides silence from the silence
# gate. 801 taps at 16 kHz puts the transition at roughly 50-150 Hz.
_HIGHPASS_HZ = 100.0
_HIGHPASS_TAPS = 801
_HIGHPASS = -_lowpass(_HIGHPASS_TAPS, _HIGHPASS_HZ / ASR_RATE)
_HIGHPASS[(_HIGHPASS_TAPS - 1) // 2] += 1.0     # delta minus low-pass


def speech_band(pcm16: bytes) -> bytes:
    """16 kHz PCM with DC and everything under ~100 Hz removed."""
    x = np.frombuffer(pcm16[: len(pcm16) & ~1], dtype="<i2").astype(np.float64)
    y = _filter(x, _HIGHPASS)
    return np.clip(np.round(y), -32768, 32767).astype("<i2").tobytes()


def to_asr(pcm48: bytes) -> bytes:
    """What the card captured -> what the recogniser should hear."""
    return speech_band(resample(pcm48, HARDWARE_RATE, ASR_RATE))
