"""Configuration: config.yaml, environment overrides, working defaults.

Every default is chosen so the app starts and does something useful with
an empty config.yaml. Environment variables win over the file:

    MESSENGER_RADIO_PORT=/dev/pts/3 MESSENGER_IDENTITY_NAME=Rover ./run.sh
"""

from __future__ import annotations

import os
import socket
import zlib
from dataclasses import dataclass, field, fields
from pathlib import Path

from utils.logger import get_logger

log = get_logger("config")

PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
ENV_PREFIX = "MESSENGER_"
BROADCAST = 0xFFFF


@dataclass
class RadioConfig:
    port: str = "/dev/ttyS0"
    uart_baud: int = 9600
    # These three are written into the module by provision_radio.py; the
    # app only uses them for airtime estimates and the status line.
    frequency_mhz: int = 868
    air_speed: int = 9600
    power_dbm: int = 22
    # This radio's address, 0-65534. None ("auto") derives one from the
    # hostname, so two boards with different names never need configuring.
    address: int | None = None
    # Who a spoken message goes to. 65535 is everyone in range, which is
    # what a two-radio setup wants: the other radio answers the ACK.
    peer_address: int = BROADCAST
    # ETSI caps EU 868 at 1%: 36 s of transmit per hour. A text message is
    # ~0.15 s, so this only ever bites a retry storm. 100 where licensed.
    duty_cycle_percent: float = 1.0
    # M0/M1 pins to drive. Leave None with the stock jumpers: GPIO 22/27
    # belong to the Whisplay LCD. Fill in only after rewiring M0/M1.
    mode_pins: list | None = None
    # Where M0/M1 physically are, whether or not we drive them.
    wired_mode_pins: list = field(default_factory=lambda: [22, 27])


@dataclass
class IdentityConfig:
    # The name the other radio shows next to "RX:". "auto" = hostname.
    name: str = "auto"


@dataclass
class MessagingConfig:
    # How long to wait for an ACK before sending again. A 60-byte message
    # and its ACK take ~0.5 s round trip at 9600 bps; 3 s leaves room for
    # a receiver busy transcribing.
    ack_timeout_seconds: float = 3.0
    # Resends after the first attempt, so 3 means up to 4 transmissions.
    max_retries: int = 3
    history_size: int = 200


@dataclass
class AudioConfig:
    capture_device: str = "auto"
    playback_device: str = "auto"
    preferred_card: str = "whisplay"
    # The Whisplay card's "mic" control. 100% overdrives its preamp, and
    # distorted audio transcribes badly; 80 is clear.
    mic_level: int | None = 80
    max_record_seconds: float = 15.0


@dataclass
class AsrConfig:
    # auto | faster-whisper | vosk | whisper-cpp | none
    engine: str = "auto"
    # faster-whisper model name, or the path of a vosk model directory or
    # a whisper.cpp ggml file.
    model: str = "tiny.en"
    language: str = "en"
    # 0 = let the engine decide.
    threads: int = 0


@dataclass
class TtsConfig:
    # Read received messages aloud with espeak-ng, if it is installed.
    enabled: bool = False
    voice: str = "en"
    words_per_minute: int = 150


@dataclass
class UiConfig:
    brightness: int = 80
    idle_dim_seconds: float = 30.0
    idle_dim_brightness: int = 15
    idle_off_seconds: float = 0.0
    led_enabled: bool = True


@dataclass
class InputConfig:
    debounce_ms: int = 75
    click_window_ms: int = 700
    hold_ms: int = 350
    # Type messages on stdin. "auto" = when stdin is a terminal.
    keyboard: str = "auto"


@dataclass
class Config:
    radio: RadioConfig = field(default_factory=RadioConfig)
    identity: IdentityConfig = field(default_factory=IdentityConfig)
    messaging: MessagingConfig = field(default_factory=MessagingConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    asr: AsrConfig = field(default_factory=AsrConfig)
    tts: TtsConfig = field(default_factory=TtsConfig)
    ui: UiConfig = field(default_factory=UiConfig)
    input: InputConfig = field(default_factory=InputConfig)
    source: str = "defaults"

    @property
    def data_dir(self) -> Path:
        path = Path(os.getenv(f"{ENV_PREFIX}DATA_DIR",
                              Path.home() / ".lora-messenger"))
        path.mkdir(parents=True, exist_ok=True)
        return path

    def sections(self) -> dict:
        return {spec.name: getattr(self, spec.name)
                for spec in fields(self) if spec.name != "source"}


def hostname() -> str:
    return socket.gethostname().split(".")[0].strip() or "radio"


def address_for(name: str) -> int:
    """A stable 0-65534 address from a name; 65535 is broadcast."""
    return zlib.crc32(name.encode("utf-8")) % 0xFFFF


def _apply(section, values: dict, where: str):
    known = {spec.name for spec in fields(section)}
    for key, value in (values or {}).items():
        if key not in known:
            log.warning("ignoring unknown setting %s.%s", where, key)
            continue
        setattr(section, key, value)


def _apply_env(config: Config):
    """MESSENGER_RADIO_PORT, MESSENGER_ASR_ENGINE, ... override the file."""
    for name, section in config.sections().items():
        for spec in fields(section):
            key = f"{ENV_PREFIX}{name.upper()}_{spec.name.upper()}"
            raw = os.getenv(key)
            if raw is None:
                continue
            current = getattr(section, spec.name)
            try:
                if isinstance(current, bool):
                    value = raw.strip().lower() in ("1", "true", "yes", "on")
                elif isinstance(current, int) and not isinstance(current, bool):
                    value = int(raw, 0)
                elif isinstance(current, float):
                    value = float(raw)
                else:
                    value = raw
            except ValueError:
                log.warning("%s=%r is not valid for %s", key, raw, spec.name)
                continue
            setattr(section, spec.name, value)


def _is_auto(value) -> bool:
    return value is None or (isinstance(value, str)
                             and value.strip().lower() in ("", "auto"))


def _normalise(config: Config):
    if _is_auto(config.identity.name):
        config.identity.name = hostname()
    config.identity.name = str(config.identity.name)[:24]

    address = config.radio.address
    if not _is_auto(address):
        try:
            address = int(address, 0) if isinstance(address, str) else int(address)
        except (TypeError, ValueError):
            address = -1
        if not 0 <= address <= 0xFFFE:
            log.warning("radio.address %r is not 0-65534; deriving one",
                        config.radio.address)
            address = None
    config.radio.address = (address_for(config.identity.name)
                            if _is_auto(address) else address)

    peer = config.radio.peer_address
    try:
        peer = int(peer, 0) if isinstance(peer, str) else int(peer)
    except (TypeError, ValueError):
        peer = BROADCAST
    config.radio.peer_address = peer if 0 <= peer <= 0xFFFF else BROADCAST


def load(path: Path | str | None = None) -> Config:
    config = Config()
    path = Path(path) if path else CONFIG_PATH
    if path.is_file():
        try:
            import yaml
            data = yaml.safe_load(path.read_text()) or {}
        except Exception as exc:
            log.error("could not read %s (%s); using defaults", path, exc)
            data = {}
        sections = config.sections()
        for name, values in data.items():
            if name in sections and isinstance(values, dict):
                _apply(sections[name], values, name)
            else:
                log.warning("ignoring unknown section %r in %s", name, path)
        config.source = str(path)
    _apply_env(config)
    _normalise(config)
    return config
