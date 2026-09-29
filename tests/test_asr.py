"""ASR plumbing: engine choice, silence, clean-up, WAV and resampling.

The engines themselves are not run here -- a model download is neither
fast nor deterministic. `python3 main.py --transcribe clip.wav` tests
the real one; see the README.
"""

import math
import struct

import numpy as np

from asr import speech_to_text as stt
from audio import dsp
from config import AsrConfig


def sine(freq, seconds, rate, amplitude=8000):
    count = int(seconds * rate)
    return b"".join(struct.pack("<h", int(amplitude * math.sin(2 * math.pi * freq * i / rate)))
                    for i in range(count))


def rms(pcm):
    samples = np.frombuffer(pcm, dtype="<i2").astype(float)
    return float(np.sqrt((samples ** 2).mean()))


def test_clean_transcript_drops_annotations():
    assert stt.clean_transcript("  [BLANK_AUDIO] Meet me (wind)  at five ") == "Meet me at five"
    assert stt.clean_transcript("*music*") == ""
    assert stt.clean_transcript("- Hello -") == "Hello"


def test_lower_case_engines_read_like_messages():
    """Vosk writes all lower case; the screen should not."""
    assert stt.clean_transcript("meet me at five o'clock") == "Meet me at five o'clock"
    assert stt.clean_transcript("Already Capitalised") == "Already Capitalised"
    assert stt.clean_transcript("") == ""


def test_silence_and_short_clips_skip_the_engine():
    class Exploding(stt.Engine):
        def _transcribe(self, pcm):
            raise AssertionError("should not be asked")

    engine = Exploding()
    assert engine.transcribe(bytes(32000)) == ""                    # 1 s of zeros
    assert engine.transcribe(sine(300, 0.1, 16000)) == ""           # too short
    assert stt.is_silence(bytes(64000))
    assert not stt.is_silence(sine(300, 1.0, 16000))


def test_engine_output_is_cleaned():
    class Echo(stt.Engine):
        def _transcribe(self, pcm):
            return " [BLANK_AUDIO]  Meet me  at five o'clock "

    assert Echo().transcribe(sine(300, 1.0, 16000)) == "Meet me at five o'clock"


def test_engine_selection():
    assert isinstance(stt.create_engine(AsrConfig(engine="none")), stt.NoEngine)
    assert isinstance(stt.create_engine(AsrConfig(engine="vosk", model="/nope")), stt.VoskEngine)
    auto = stt.create_engine(AsrConfig(engine="auto"))
    assert auto.name in ("faster-whisper", "vosk", "whisper-cpp", "none")
    assert stt.create_engine(AsrConfig(engine="bogus")).name == auto.name


def test_missing_vosk_model_reports_a_clear_error():
    engine = stt.VoskEngine("/no/such/model")
    done = []
    engine.warm_up(on_done=lambda: done.append(1))
    import time
    deadline = time.monotonic() + 2
    while not done and time.monotonic() < deadline:
        time.sleep(0.01)
    assert engine.error and not engine.ready


def test_downsample_keeps_speech_and_removes_what_would_fold():
    speech = dsp.to_asr(sine(1000, 0.5, 48000))
    assert len(speech) == 0.5 * 16000 * 2
    assert abs(rms(speech) - 8000 / math.sqrt(2)) < 200

    # 12 kHz cannot exist at 16 kHz; unfiltered it would fold to 4 kHz.
    folded = dsp.to_asr(sine(12000, 0.5, 48000))
    assert rms(folded) < 100


def test_speech_band_removes_hum_and_dc_but_keeps_speech():
    """Regression: the Zero 2 W mic's 'silence' was 70% sub-100 Hz hum plus DC."""
    rate = 16000
    t = np.arange(rate * 2) / rate
    hum = (330 + 1500 * np.sin(2 * np.pi * 50 * t)).astype("<i2").tobytes()
    assert rms(dsp.speech_band(hum)) < 30                 # was ~1100

    voice = sine(1000, 2.0, rate)
    kept = dsp.speech_band(voice)
    assert abs(rms(kept) - rms(voice)) / rms(voice) < 0.01
    assert len(kept) == len(voice)


def test_live_path_is_filtered():
    """What the recorder hands the recogniser has no DC left in it."""
    offset = (np.full(48000, 400)).astype("<i2").tobytes() # 1 s of pure DC at 48 kHz
    assert abs(np.frombuffer(dsp.to_asr(offset), dtype="<i2").mean()) < 5


def test_non_integer_resample_length():
    out = dsp.resample(sine(440, 1.0, 22050), 22050, 48000)
    assert abs(len(out) // 2 - 48000) <= 1


def test_wav_round_trip_resamples_to_16k(tmp_path):
    path = tmp_path / "clip.wav"
    stt.write_wav(path, sine(500, 1.0, 48000), rate=48000)
    pcm = stt.read_wav(path)
    assert len(pcm) == 16000 * 2


def test_model_loads_once_however_it_is_asked_for():
    """Regression: --transcribe called load() and then transcribe() loaded again."""
    class Counting(stt.Engine):
        loads = 0

        def load(self):
            Counting.loads += 1

        def _transcribe(self, pcm):
            return "ok"

    engine = Counting()
    engine.ensure_loaded()
    engine.transcribe(sine(300, 1.0, 16000))
    engine.transcribe(sine(300, 1.0, 16000))
    assert Counting.loads == 1 and engine.ready


class FakeWhisperModel:
    """faster_whisper.WhisperModel that knows which models are cached."""
    cached = set()
    calls = []

    def __init__(self, name, local_files_only=False, **options):
        FakeWhisperModel.calls.append(local_files_only)
        if local_files_only and name not in FakeWhisperModel.cached:
            raise FileNotFoundError(f"{name} is not in the cache")


def load_whisper(monkeypatch, cached):
    import sys
    import types
    fake = types.ModuleType("faster_whisper")
    fake.WhisperModel = FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake)
    FakeWhisperModel.cached, FakeWhisperModel.calls = cached, []
    stt.FasterWhisperEngine("tiny.en").load()
    return FakeWhisperModel.calls


def test_a_cached_whisper_model_loads_without_asking_the_internet(monkeypatch):
    """On Wi-Fi with no internet, asking whether the model is current
    waited 135 s before falling back to the cache."""
    assert load_whisper(monkeypatch, cached={"tiny.en"}) == [True]


def test_an_uncached_whisper_model_is_downloaded(monkeypatch):
    assert load_whisper(monkeypatch, cached=set()) == [True, False]
