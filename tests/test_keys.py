"""A keyboard on the board: which devices count, and what the keys mean."""

import os
import time

from controls import keys
from controls.keys import EVENT, EV_KEY, KeyDecoder, KeyReader, has_keys

KEY_H, KEY_I, LEFTSHIFT = 35, 23, 42


def press(code, value=1):
    return EVENT.pack(0, 0, EV_KEY, code, value)


def tap(code):
    return press(code, 1) + press(code, 0)


def bitmap(*codes, words=4):
    """A sysfs capabilities/key file with these key bits set."""
    value = sum(1 << code for code in codes)
    mask = (1 << keys.LONG_BITS) - 1
    parts = [(value >> (keys.LONG_BITS * n)) & mask for n in reversed(range(words))]
    return " ".join(f"{part:x}" for part in parts) + "\n"


def test_only_devices_with_letter_keys_are_keyboards():
    assert has_keys(bitmap(*range(1, 120)))
    assert not has_keys(bitmap(116))                   # a power button
    assert not has_keys(bitmap(114, 115))              # volume buttons
    assert not has_keys(bitmap(*range(2, 12), 28))     # a remote with digits and OK


def test_letters_shift_caps_and_repeat():
    decoder = KeyDecoder()
    stream = (tap(KEY_H) + press(LEFTSHIFT) + tap(KEY_I) + press(LEFTSHIFT, 0)
              + tap(keys.KEY_CAPSLOCK) + tap(KEY_H) + press(LEFTSHIFT) + tap(KEY_H)
              + press(LEFTSHIFT, 0) + tap(2) + press(keys.KEY_BACKSPACE, 2))
    assert decoder.feed(stream) == [
        ("char", "h"), ("char", "I"), ("char", "H"), ("char", "h"), ("char", "1"),
        ("key", "backspace")]


def test_named_keys_and_events_split_across_reads():
    decoder = KeyDecoder()
    stream = tap(keys.KEY_ENTER) + tap(keys.KEY_ESC) + tap(keys.KEY_UP) + tap(keys.KEY_TAB)
    got = decoder.feed(stream[:5]) + decoder.feed(stream[5:30]) + decoder.feed(stream[30:])
    assert got == [("key", "enter"), ("key", "escape"), ("key", "up"), ("key", "tab")]


def test_other_event_types_are_ignored():
    decoder = KeyDecoder()
    assert decoder.feed(EVENT.pack(0, 0, 0x02, 0, 5) + EVENT.pack(0, 0, 0x00, 0, 0)) == []


def fake_input(tmp_path, name, codes):
    sysdir = tmp_path / "sys" / name / "device" / "capabilities"
    sysdir.mkdir(parents=True)
    (sysdir / "key").write_text(bitmap(*codes))
    (tmp_path / "dev").mkdir(exist_ok=True)
    os.mkfifo(tmp_path / "dev" / name)


def wait_for(condition, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


def test_a_plugged_in_keyboard_is_read_and_its_unplugging_noticed(tmp_path):
    fake_input(tmp_path, "event0", [116])                 # power button: left alone
    fake_input(tmp_path, "event3", range(1, 120))         # a keyboard
    chars, named = [], []
    reader = KeyReader(chars.append, named.append, input_dir=str(tmp_path / "dev"),
                       sys_dir=str(tmp_path / "sys"), rescan_seconds=0.05)
    assert reader.keyboards() == ["event3"]
    reader.start()
    writer = None
    try:
        assert wait_for(lambda: reader.connected)
        writer = os.open(tmp_path / "dev" / "event3", os.O_WRONLY | os.O_NONBLOCK)
        os.write(writer, tap(KEY_H) + tap(KEY_I) + tap(keys.KEY_ENTER))
        assert wait_for(lambda: named == ["enter"])
        assert chars == ["h", "i"]
        # Unplugged: its sysfs entry goes, and the reader lets go of it.
        os.remove(tmp_path / "sys" / "event3" / "device" / "capabilities" / "key")
        assert wait_for(lambda: not reader.connected)
    finally:
        reader.stop()
        if writer is not None:
            os.close(writer)


def test_no_keyboard_costs_nothing(tmp_path):
    reader = KeyReader(print, print, input_dir=str(tmp_path / "none"),
                       sys_dir=str(tmp_path / "none"), rescan_seconds=0.05)
    reader.start()
    try:
        time.sleep(0.15)
        assert not reader.connected
    finally:
        reader.stop()
