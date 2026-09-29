"""Sharing one board -- one serial port, one module -- with WalkieTalkie."""

import os
import subprocess
import sys

import pytest

import config as config_module
import provision_radio
from display.board import NullBoard
from lora.sx126x import PortBusy, SX126x, port_conflicts
from main import Messenger


@pytest.fixture
def pty_port():
    """A serial port both apps could open: the slave end of a pty."""
    master, slave = os.openpty()
    try:
        yield os.ttyname(slave)
    finally:
        os.close(slave)
        os.close(master)


@pytest.fixture
def walkie_holds(tmp_path, pty_port):
    """WalkieTalkie running, with the radio's port open and locked."""
    folder = tmp_path / "WalkieTalkie"
    folder.mkdir()
    child = subprocess.Popen(
        [sys.executable, "-c",
         "import serial, sys; port = serial.Serial(sys.argv[1], exclusive=True); "
         "print('ready', flush=True); sys.stdin.read()", pty_port],
        cwd=folder, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        yield pty_port
    finally:
        child.stdin.close()
        child.wait(timeout=5)


def open_radio(port):
    return SX126x(port=port, addr=1, freq_mhz=868)


# --- one radio port, two apps ------------------------------------------------------

def test_a_second_opener_is_refused_until_the_first_closes(pty_port):
    first = open_radio(pty_port)
    try:
        with pytest.raises(PortBusy):
            open_radio(pty_port)
    finally:
        first.close()
    open_radio(pty_port).close()


def test_the_holder_is_named_by_its_app_folder(walkie_holds):
    with pytest.raises(PortBusy) as refused:
        open_radio(walkie_holds)
    assert refused.value.holders == ["WalkieTalkie"]
    assert "in use by WalkieTalkie" in str(refused.value)
    assert any(line.startswith("WalkieTalkie (") for line in port_conflicts(walkie_holds))


def test_the_app_says_which_app_to_quit(tmp_path, monkeypatch, walkie_holds):
    monkeypatch.setenv("MESSENGER_DATA_DIR", str(tmp_path / "data"))
    config = config_module.load(tmp_path / "missing.yaml")
    config.radio.port = walkie_holds
    config.input.keyboard = "off"
    config.asr.engine = "none"
    app = Messenger(config, board=NullBoard())
    app.player.device = None
    assert app.radio is None and app.sender is None
    assert app.view().status == "Radio busy: quit WalkieTalkie"
    app.send("anyone there?")                   # typed while refused
    assert app.history.latest().status == "failed"


# --- one module, two configs -------------------------------------------------------

def walkie_config(tmp_path, monkeypatch, radio_yaml):
    path = tmp_path / "WalkieTalkie" / "config.yaml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(f"radio:\n{radio_yaml}")
    monkeypatch.setattr(config_module, "WALKIE_CONFIG", path)


def test_auto_follows_what_walkietalkie_provisioned(tmp_path, monkeypatch):
    walkie_config(tmp_path, monkeypatch, "  frequency_mhz: 868\n  air_speed: 2400\n")
    radio = config_module.load(tmp_path / "missing.yaml").radio
    assert (radio.frequency_mhz, radio.air_speed) == (868, 2400)


def test_walkietalkie_defaults_apply_to_what_its_file_leaves_out(tmp_path, monkeypatch):
    walkie_config(tmp_path, monkeypatch, "  privacy_channel: 3\n")
    radio = config_module.load(tmp_path / "missing.yaml").radio
    assert (radio.frequency_mhz, radio.air_speed) == (868, 9600)


def test_without_walkietalkie_auto_is_868_and_9600(tmp_path):
    radio = config_module.load(tmp_path / "missing.yaml").radio
    assert (radio.frequency_mhz, radio.air_speed) == (868, 9600)


def test_numbers_in_our_config_and_environment_still_win(tmp_path, monkeypatch):
    walkie_config(tmp_path, monkeypatch, "  air_speed: 2400\n")
    ours = tmp_path / "config.yaml"
    ours.write_text("radio:\n  air_speed: 9600\n  frequency_mhz: '915'\n")
    radio = config_module.load(ours).radio
    assert (radio.frequency_mhz, radio.air_speed) == (915, 9600)
    monkeypatch.setenv("MESSENGER_RADIO_AIR_SPEED", "1200")
    assert config_module.load(tmp_path / "missing.yaml").radio.air_speed == 1200


def test_nonsense_falls_back_to_auto(tmp_path, monkeypatch):
    walkie_config(tmp_path, monkeypatch, "  air_speed: 2400\n")
    ours = tmp_path / "config.yaml"
    ours.write_text("radio:\n  air_speed: fast\n")
    assert config_module.load(ours).radio.air_speed == 2400


def test_provisioning_here_cannot_move_the_module_off_walkietalkie(tmp_path, monkeypatch,
                                                                    capsys):
    walkie_config(tmp_path, monkeypatch, "  frequency_mhz: 868\n  air_speed: 2400\n")
    monkeypatch.setattr(sys, "argv", ["provision_radio.py", "--air-speed", "9600"])
    assert provision_radio.main() == 2
    assert "WalkieTalkie provisioned this module for 868 MHz at 2400 bps" \
        in capsys.readouterr().err


def test_provisioning_what_walkietalkie_recorded_is_allowed(tmp_path, monkeypatch, capsys):
    walkie_config(tmp_path, monkeypatch, "  frequency_mhz: 868\n  air_speed: 2400\n")
    monkeypatch.setattr(provision_radio, "mode_pin_driver_missing", lambda: "not a Pi")
    monkeypatch.setattr(sys, "argv", ["provision_radio.py"])
    provision_radio.main()                      # stops later, at the GPIO check
    err = capsys.readouterr().err
    assert "WalkieTalkie provisioned" not in err and "not a Pi" in err
