"""Configuration loading, keyboard input, and the simulated air."""

import io

import config as config_module
from controls.keyboard import Keyboard
from lora import protocol
from tools.sim_air import take_packet


def test_empty_config_runs_with_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("MESSENGER_RADIO_ADDRESS", raising=False)
    config = config_module.load(tmp_path / "none.yaml")
    assert config.radio.port == "/dev/ttyS0"
    assert config.identity.name == config_module.hostname()
    assert config.radio.address == config_module.address_for(config.identity.name)
    assert 0 <= config.radio.address <= 0xFFFE
    assert config.radio.peer_address == 0xFFFF


def test_file_and_environment(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("radio:\n  address: 0x10\n  peer_address: 32\n"
                    "identity:\n  name: RasPi\nbogus:\n  x: 1\n")
    monkeypatch.setenv("MESSENGER_ASR_ENGINE", "vosk")
    monkeypatch.setenv("MESSENGER_MESSAGING_MAX_RETRIES", "5")
    config = config_module.load(path)
    assert (config.radio.address, config.radio.peer_address) == (16, 32)
    assert config.identity.name == "RasPi"
    assert config.asr.engine == "vosk" and config.messaging.max_retries == 5


def test_invalid_address_is_derived_instead(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("radio:\n  address: 70000\nidentity:\n  name: OrangePi\n")
    assert config_module.load(path).radio.address == config_module.address_for("OrangePi")


def test_hostnames_give_different_addresses():
    assert config_module.address_for("raspberrypi") != config_module.address_for("orangepi")


def test_keyboard_text_commands_and_recording():
    sent, commands = [], []
    board = Keyboard(sent.append, commands.append,
                     stream=io.StringIO("hello there\n\n/talk\nstop\n/HISTORY now\n"))
    board.recording = False

    def on_command(command):
        commands.append(command)
        if command == "talk":
            board.recording = True
        if command == "talk-stop":
            board.recording = False

    board.on_command = on_command
    board._run()
    assert sent == ["hello there"]
    assert commands == ["talk", "talk-stop", "history", "eof"]


def test_empty_keyboard_commands_do_not_stop_typing():
    sent, commands = [], []
    keyboard = Keyboard(sent.append, commands.append,
                        stream=io.StringIO("/\n/   \nhello\n/HELP\n"))
    keyboard._run()
    assert sent == ["hello"]
    assert commands == ["help", "eof"]


def test_sim_air_splits_host_writes_into_packets():
    first = protocol.text_packet(1, 0xFFFF, 1, "one").encode()
    second = protocol.ack_packet(2, protocol.text_packet(1, 2, 5, "x")).encode()
    buffer = bytearray(b"\xFF\xFF\x12" + first + b"\xFF\xFF\x12" + second[:4])
    assert take_packet(buffer) == first
    assert take_packet(buffer) is None           # second still arriving
    buffer.extend(second[4:])
    assert take_packet(buffer) == second
    assert not buffer


def test_run_sh_uses_the_venv_python_wherever_the_folder_moved(tmp_path):
    """A venv made under ~/Messager: its activate points at a folder that
    no longer exists, and run.sh used to fall back to the system python3."""
    import shutil
    import subprocess
    from pathlib import Path

    project = tmp_path / "Messenger"
    (project / ".venv" / "bin").mkdir(parents=True)
    shutil.copy(Path(config_module.PROJECT_ROOT) / "run.sh", project / "run.sh")
    (project / ".venv" / "bin" / "activate").write_text(
        'export PATH="/home/mengpi/Messager/.venv/bin:$PATH"\n')
    venv_python = project / ".venv" / "bin" / "python"
    venv_python.write_text('#!/bin/sh\necho "venv python $*"\n')
    venv_python.chmod(0o755)
    (project / "main.py").write_text("print('system python')\n")

    run = lambda: subprocess.run(["bash", str(project / "run.sh"), "--headless"],
                                 capture_output=True, text=True, cwd=tmp_path).stdout
    assert run().strip() == "venv python main.py --headless"
    shutil.rmtree(project / ".venv")
    assert run().strip() == "system python"


def test_a_managed_install_adopts_the_old_history_once_and_keeps_it(tmp_path, monkeypatch):
    import config as cfg
    old, managed = tmp_path / "old", tmp_path / "managed"
    old.mkdir()
    (old / "history.json").write_text('{"messages": []}')
    (old / "ids.json").write_text('{"next": 7}')
    monkeypatch.setattr(cfg, "legacy_data_dir", lambda: old)
    monkeypatch.delenv("MESSENGER_DATA_DIR", raising=False)
    monkeypatch.setenv("WHISPLAY_OS_APP_DATA", str(managed))
    assert cfg.Config().data_dir == managed
    assert (managed / "history.json").read_text() == '{"messages": []}'
    assert (managed / "ids.json").read_text() == '{"next": 7}'
    assert (old / "history.json").exists(), "the old files are kept"
    (old / "history.json").write_text("changed later")
    cfg.Config().data_dir
    assert (managed / "history.json").read_text() == '{"messages": []}', "only once"


def test_without_mfruit_os_the_old_folder_is_used_as_before(tmp_path, monkeypatch):
    import config as cfg
    monkeypatch.setattr(cfg, "legacy_data_dir", lambda: tmp_path / "old")
    monkeypatch.delenv("MESSENGER_DATA_DIR", raising=False)
    monkeypatch.delenv("WHISPLAY_OS_APP_DATA", raising=False)
    assert cfg.Config().data_dir == tmp_path / "old"
