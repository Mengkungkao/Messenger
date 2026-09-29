import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import config as config_module


@pytest.fixture(autouse=True)
def no_walkietalkie_nearby(tmp_path, monkeypatch):
    """Tests must not depend on a WalkieTalkie install on the host."""
    monkeypatch.setattr(config_module, "WALKIE_CONFIG", tmp_path / "no-walkie.yaml")
