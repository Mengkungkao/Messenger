"""The packet format, its CRC, and pulling packets out of a byte stream."""

import pytest

from lora import packet as pk


def text(msg_id=1024, message="Hello from Pi", src=0x1A2B, dst=0xFFFF):
    return pk.Packet(pk.TEXT, msg_id, src, dst, message.encode())


# --- format ------------------------------------------------------------------

def test_brief_example_round_trips():
    packet = text()
    frame = packet.encode()
    assert frame[0] == 0xAA                       # START
    assert frame[1] == pk.TEXT                    # TYPE
    assert frame[2:4] == (1024).to_bytes(2, "big")  # ID
    assert frame[8] == len("Hello from Pi")       # LENGTH
    assert frame[9:-2] == b"Hello from Pi"        # MESSAGE
    assert len(frame) == pk.OVERHEAD + 13
    assert pk.decode(frame) == packet


def test_crc_is_ccitt_false():
    # The standard check value for CRC-16/CCITT-FALSE.
    assert pk.crc16(b"123456789") == 0x29B1


def test_every_single_bit_flip_is_caught():
    frame = text().encode()
    for index in range(1, len(frame)):     # START itself is checked separately
        for bit in range(8):
            corrupt = bytearray(frame)
            corrupt[index] ^= 1 << bit
            assert pk.decode(bytes(corrupt)) is None, (index, bit)


@pytest.mark.parametrize("frame", [
    b"",
    b"\xAA",
    b"\x55" + text().encode()[1:],                 # wrong START
    text().encode()[:-1],                          # truncated
    text().encode() + b"\x00",                     # trailing byte
])
def test_malformed_frames_are_rejected(frame):
    assert pk.decode(frame) is None


def test_unknown_type_is_rejected_even_with_good_crc():
    body = bytes([0x7F, 0, 1, 0, 1, 0, 2, 0])
    crc = pk.crc16(body)
    assert pk.decode(b"\xAA" + body + bytes([crc >> 8, crc & 0xFF])) is None


def test_unicode_and_empty_messages():
    for message in ("", "Café à 5h ✓", "日本語"):
        assert pk.decode(text(message=message).encode()).text == message


def test_size_limits():
    biggest = text(message="x" * pk.MAX_MESSAGE)
    assert len(biggest.encode()) == pk.MAX_PACKET
    assert pk.MAX_PACKET <= 237       # module takes 240 per write, 3 are addressing
    with pytest.raises(ValueError):
        text(message="x" * (pk.MAX_MESSAGE + 1)).encode()
    with pytest.raises(ValueError):
        pk.Packet(pk.TEXT, 0x10000, 1, 2).encode()
    with pytest.raises(ValueError):
        pk.Packet(0x7F, 1, 1, 2).encode()


def test_rssi_conversion():
    assert pk.rssi_dbm(0xA5) == -91
    assert pk.rssi_dbm(0xAA) == -86


# --- deframer ----------------------------------------------------------------

def packets(events):
    return [value for kind, value in events if kind == "packet"]


def rssis(events):
    return [value for kind, value in events if kind == "rssi"]


def test_packet_split_across_many_reads():
    deframer = pk.Deframer()
    frame = text().encode()
    events = []
    for byte in frame:
        events += deframer.feed(bytes([byte]))
    assert packets(events) == [text()]


def test_packet_is_delivered_before_its_rssi_arrives():
    deframer = pk.Deframer()
    events = deframer.feed(text().encode())
    assert packets(events) == [text()] and not rssis(events)
    assert rssis(deframer.feed(b"\xA5")) == [-91]


def test_rssi_in_the_same_read():
    events = pk.Deframer().feed(text().encode() + b"\xA5")
    assert packets(events) == [text()] and rssis(events) == [-91]


def test_rssi_byte_equal_to_start_is_still_rssi():
    """-86 dBm is 0xAA, the START byte. It must not swallow the next packet."""
    deframer = pk.Deframer()
    first, second = text(msg_id=1), text(msg_id=2)
    events = deframer.feed(first.encode() + b"\xAA")
    assert packets(events) == [first] and not rssis(events)   # undecided yet
    events = deframer.feed(second.encode() + b"\xAA")
    assert rssis(events) == [-86]
    assert packets(events) == [second]


def test_back_to_back_packets_without_rssi_reporting():
    deframer = pk.Deframer()
    a, b = text(msg_id=1), text(msg_id=2)
    assert packets(deframer.feed(a.encode() + b.encode())) == [a, b]


def test_resynchronises_after_noise_and_corruption():
    deframer = pk.Deframer()
    good = text(msg_id=7)
    corrupt = bytearray(text(msg_id=6).encode())
    corrupt[10] ^= 0xFF
    stream = b"\x00\x13\xAA\xAA\x99" + bytes(corrupt) + good.encode()
    assert packets(deframer.feed(stream)) == [good]
    assert deframer.packets_bad >= 1


def test_ignores_walkie_talkie_frames_on_the_same_channel():
    """WalkieTalkie frames start AA 55; 0x55 is not a packet type here."""
    walkie = b"\xAA\x55\x03abc\x12\x34"
    good = text()
    assert packets(pk.Deframer().feed(walkie + good.encode())) == [good]


def test_buffer_is_bounded():
    deframer = pk.Deframer()
    deframer.feed(b"\x01" * 10000)
    assert len(deframer._buffer) <= deframer.MAX_BUFFER
