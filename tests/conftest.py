import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import config as config_module


@pytest.fixture(autouse=True)
def no_walkietalkie_nearby(tmp_path, monkeypatch):
    """Tests must not depend on a WalkieTalkie install on the host."""
    monkeypatch.setattr(config_module, "WALKIE_CONFIG", tmp_path / "no-walkie.yaml")


@pytest.fixture(autouse=True)
def private_radio_store(tmp_path, monkeypatch):
    """The shared radio store (identity, keys, radio settings) lives under
    MFruit OS's home; tests get their own, never the host's."""
    monkeypatch.setenv("MFRUIT_HOME", str(tmp_path / "mfruit-home"))
    monkeypatch.delenv("WHISPLAY_OS_HOME", raising=False)
    monkeypatch.setenv("WALKIE_DATA_DIR", str(tmp_path / "no-walkie-data"))
    monkeypatch.delenv("WHISPLAY_OS_APP_DATA", raising=False)
