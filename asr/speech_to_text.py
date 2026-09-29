"""Speech to text, offline, with whichever engine this board has.

    faster-whisper   Whisper on CTranslate2, int8. The most accurate here.
                     Measured on a Pi 4 with tiny.en: ~2.7 s for a 3 s
                     utterance, 5 s for 11 s -- Whisper always encodes a
                     padded 30 s window, so short clips cost nearly the
                     same fixed amount. Wants ~300 MB of RAM: fine on a
                     Pi 4 or a 1 GB+ Orange Pi Zero 2W, tight on a 512 MB
                     Pi Zero 2 W, and slower on any Cortex-A53 board.
    vosk             Kaldi, streaming. Less accurate, far lighter (a 40 MB
                     model, ~150 MB RAM): the choice for a Pi Zero 2 W.
    whisper-cpp      The whisper.cpp command-line tool, if it is on PATH.
    none             No ASR: type messages on the keyboard instead.

`auto` takes the first that is installed, in that order. Every engine
takes 16 kHz 16-bit mono PCM -- what audio/recorder.py produces -- and
returns plain text, "" when nothing was said.

Loading a model takes seconds, so `warm_up()` does it in the background
at start-up, and the first push-to-talk is as quick as the rest.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import wave
from pathlib import Path

from utils.logger import get_logger

log = get_logger("asr")

SAMPLE_RATE = 16000

# Below this RMS (of 32768) a clip is silence: do not ask Whisper, which
# famously transcribes silence as "Thank you." or "you".
SILENCE_RMS = 120.0
MIN_SECONDS = 0.3

# Non-speech annotations engines emit: [BLANK_AUDIO], (wind blowing), ...
_ANNOTATION = re.compile(r"\[[^\]]*\]|\([^)]*\)|\*[^*]*\*")


class AsrUnavailable(RuntimeError):
    pass


def clean_transcript(text: str) -> str:
    """Strip engine annotations, tidy whitespace, start with a capital.

    Vosk writes everything in lower case ("meet me at five o'clock"); a
    capital first letter is all it takes to read like a message.
    """
    text = _ANNOTATION.sub(" ", text or "")
    text = " ".join(text.split()).strip(" -")
    return text[:1].upper() + text[1:]


def pcm_rms(pcm: bytes) -> float:
    import numpy as np
    samples = np.frombuffer(pcm[: len(pcm) & ~1], dtype="<i2").astype(np.float32)
    return float(np.sqrt((samples ** 2).mean())) if samples.size else 0.0


def is_silence(pcm: bytes) -> bool:
    return len(pcm) < MIN_SECONDS * SAMPLE_RATE * 2 or pcm_rms(pcm) < SILENCE_RMS


class Engine:
    name = "none"

    def __init__(self):
        self._loaded = False
        self._lock = threading.Lock()
        self.error = None

    @property
    def ready(self) -> bool:
        return self._loaded

    def load(self):
        """Load the model. Raises AsrUnavailable if it cannot be.

        Engines override this; callers use ensure_loaded()."""

    def ensure_loaded(self):
        with self._lock:
            if not self._loaded:
                self.load()
                self._loaded = True

    def warm_up(self, on_done=None):
        def run():
            try:
                self.ensure_loaded()
                log.info("%s ready", self.name)
            except Exception as exc:
                self.error = str(exc)
                log.error("%s could not load: %s", self.name, exc)
            if on_done:
                on_done()
        threading.Thread(target=run, name="asr-load", daemon=True).start()

    def transcribe(self, pcm: bytes) -> str:
        if is_silence(pcm):
            log.info("clip is silent (%.1f s, rms %.0f); not transcribing",
                     len(pcm) / SAMPLE_RATE / 2, pcm_rms(pcm) if pcm else 0)
            return ""
        self.ensure_loaded()
        with self._lock:
            return clean_transcript(self._transcribe(pcm))

    def _transcribe(self, pcm: bytes) -> str:
        return ""


class NoEngine(Engine):
    name = "none"

    def transcribe(self, pcm: bytes) -> str:
        return ""


class FasterWhisperEngine(Engine):
    name = "faster-whisper"

    def __init__(self, model: str = "tiny.en", language: str = "en", threads: int = 0):
        super().__init__()
        self.model_name = model
        self.language = language or None
        self.threads = threads
        self._model = None

    @staticmethod
    def installed() -> bool:
        try:
            import faster_whisper  # noqa: F401
            return True
        except ImportError:
            return False

    def load(self):
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise AsrUnavailable(f"faster-whisper is not installed: {exc}")
        options = dict(device="cpu", compute_type="int8", cpu_threads=self.threads or 0)
        # The cached copy first, without asking the Hugging Face hub if it is
        # still current. With no network that question fails at once, but on
        # Wi-Fi with no internet it waits out its retries: 135 s measured
        # before it fell back to the cache anyway. The radio needs neither.
        try:
            self._model = WhisperModel(self.model_name, local_files_only=True, **options)
            log.info("loaded Whisper model %r from the local cache", self.model_name)
            return
        except Exception as exc:
            log.info("Whisper model %r is not cached yet (%s); downloading it, once",
                     self.model_name, type(exc).__name__)
        self._model = WhisperModel(self.model_name, **options)

    def _transcribe(self, pcm: bytes) -> str:
        import numpy as np
        audio = np.frombuffer(pcm[: len(pcm) & ~1], dtype="<i2").astype(np.float32) / 32768.0
        language = self.language
        if language and self.model_name.endswith(".en"):
            language = "en"
        segments, _info = self._model.transcribe(
            audio, language=language, beam_size=1,
            # One utterance, spoken on purpose: no carrying context into
            # a hallucination loop, and the VAD trims the silence around it.
            condition_on_previous_text=False, vad_filter=True,
            without_timestamps=True,
        )
        return " ".join(segment.text for segment in segments)


class VoskEngine(Engine):
    name = "vosk"

    def __init__(self, model: str):
        super().__init__()
        self.model_path = model
        self._model = None

    @staticmethod
    def installed() -> bool:
        try:
            import vosk  # noqa: F401
            return True
        except ImportError:
            return False

    def load(self):
        try:
            import vosk
        except ImportError as exc:
            raise AsrUnavailable(f"vosk is not installed: {exc}")
        if not Path(self.model_path).is_dir():
            raise AsrUnavailable(
                f"vosk model directory {self.model_path!r} not found; set asr.model "
                "to an unpacked model, e.g. vosk-model-small-en-us-0.15")
        vosk.SetLogLevel(-1)
        self._model = vosk.Model(self.model_path)

    def _transcribe(self, pcm: bytes) -> str:
        import vosk
        recogniser = vosk.KaldiRecognizer(self._model, SAMPLE_RATE)
        step = SAMPLE_RATE  # half a second of 16-bit audio per chunk
        for offset in range(0, len(pcm), step):
            recogniser.AcceptWaveform(pcm[offset:offset + step])
        return json.loads(recogniser.FinalResult()).get("text", "")


class WhisperCppEngine(Engine):
    name = "whisper-cpp"
    BINARIES = ("whisper-cli", "whisper-cpp", "whisper.cpp")

    def __init__(self, model: str, language: str = "en", threads: int = 0):
        super().__init__()
        self.model_path = model
        self.language = language or "en"
        self.threads = threads
        self.binary = self.find_binary()

    @classmethod
    def find_binary(cls) -> str | None:
        for name in cls.BINARIES:
            path = shutil.which(name)
            if path:
                return path
        return None

    @classmethod
    def installed(cls) -> bool:
        return cls.find_binary() is not None

    def load(self):
        if not self.binary:
            raise AsrUnavailable("no whisper.cpp binary (whisper-cli) on PATH")
        if not Path(self.model_path).is_file():
            raise AsrUnavailable(f"whisper.cpp model {self.model_path!r} not found; "
                                 "set asr.model to a ggml .bin file")

    def _transcribe(self, pcm: bytes) -> str:
        with tempfile.NamedTemporaryFile(suffix=".wav") as handle:
            write_wav(handle.name, pcm)
            command = [self.binary, "-m", self.model_path, "-f", handle.name,
                       "-l", self.language, "-nt", "-np"]
            if self.threads:
                command += ["-t", str(self.threads)]
            done = subprocess.run(command, capture_output=True, text=True, timeout=120)
        if done.returncode != 0:
            raise RuntimeError(f"whisper.cpp failed: {done.stderr.strip()[-200:]}")
        return done.stdout


def write_wav(path, pcm: bytes, rate: int = SAMPLE_RATE):
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm)


def read_wav(path) -> bytes:
    """A mono 16-bit WAV as 16 kHz PCM, resampling if it is not already."""
    with wave.open(str(path), "rb") as source:
        if source.getsampwidth() != 2:
            raise ValueError("need 16-bit PCM")
        channels, rate = source.getnchannels(), source.getframerate()
        pcm = source.readframes(source.getnframes())
    import numpy as np
    samples = np.frombuffer(pcm, dtype="<i2")
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1).astype("<i2")
    if rate != SAMPLE_RATE:
        from audio import dsp
        samples = np.frombuffer(dsp.resample(samples.tobytes(), rate, SAMPLE_RATE),
                                dtype="<i2")
    return samples.tobytes()


def create_engine(config) -> Engine:
    """Build the engine config.asr asks for. Never raises: NoEngine instead."""
    choice = (config.engine or "auto").strip().lower()
    threads = int(config.threads or 0) or min(4, os.cpu_count() or 1)
    candidates = {
        "faster-whisper": lambda: FasterWhisperEngine(config.model, config.language, threads),
        "vosk": lambda: VoskEngine(config.model),
        "whisper-cpp": lambda: WhisperCppEngine(config.model, config.language, threads),
    }
    if choice == "none":
        return NoEngine()
    if choice in candidates:
        return candidates[choice]()
    if choice != "auto":
        log.warning("unknown asr.engine %r; using auto", config.engine)
    for name, installed in (("faster-whisper", FasterWhisperEngine.installed),
                            ("vosk", VoskEngine.installed),
                            ("whisper-cpp", WhisperCppEngine.installed)):
        if installed():
            log.info("ASR engine: %s", name)
            return candidates[name]()
    log.warning("no ASR engine installed (./setup.sh installs faster-whisper); "
                "voice input is off, keyboard input still works")
    return NoEngine()
