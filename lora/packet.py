"""The on-air packet, and pulling whole packets out of a UART byte stream.

    +-------+------+--------+--------+--------+--------+-----------+----------+
    | START | TYPE |   ID   |  SRC   |  DST   | LENGTH |  MESSAGE  |  CRC-16  |
    | 0xAA  |  1   | 2 (BE) | 2 (BE) | 2 (BE) |   1    |  LENGTH   |  2 (BE)  |
    +-------+------+--------+--------+--------+--------+-----------+----------+

This is the START / TYPE / ID / LENGTH / MESSAGE layout of the project
brief with two additions:

* **SRC and DST addresses.** An ACK has to find its way back to the one
  radio that sent the message, and a receiver has to know *who* spoke to
  put "RX: OrangePi" on the screen. The module could address packets
  itself, but only by reprovisioning it -- which needs the M0/M1 pins the
  Whisplay LCD owns -- so the addresses travel in the packet instead and
  the module always transmits to everyone.
* **CRC-16/CCITT-FALSE** over everything after START, so a corrupted
  length byte is caught like any other corruption instead of
  desynchronising the parser for the rest of the session.

The SX1262 has its own CRC, but the UART between host and module has
none, and a login console left on the port injects bytes the radio CRC
never sees. This one covers the whole path.
"""

from __future__ import annotations

from dataclasses import dataclass

START = 0xAA

# Packet types.
TEXT = 0x01         # a message for the screen; answered by ACK
ACK = 0x02          # "got message ID n"; its ID is the message's
HELLO = 0x03        # "I am here, and this is my name"; answered by HELLO_REPLY
HELLO_REPLY = 0x04  # a HELLO that must not be answered, so two radios cannot loop
PING = 0x05         # link test; answered by ACK, never shown
# Pairing (see messaging/pairing.py): in the clear, only while pairing.
PAIR = 0x06         # "I am pairing": our public key and name, broadcast
PAIR_REQUEST = 0x07  # "pair with me": public key + our secrets sealed for you
PAIR_ACCEPT = 0x08  # "yes": the same, back
SECURE = 0x09       # a TEXT sealed with a pairing key; answered by ACK
# Emergency (see messaging/emergency.py): in the clear on purpose, so any
# messenger in range can show it and answer.
SOS = 0x0A          # "I need help": name, battery, place, message; answered by ACK
SOS_CLEAR = 0x0B    # "I am OK now": ends an SOS on every radio that showed it

TYPE_NAMES = {TEXT: "TEXT", ACK: "ACK", HELLO: "HELLO",
              HELLO_REPLY: "HELLO_REPLY", PING: "PING", PAIR: "PAIR",
              PAIR_REQUEST: "PAIR_REQUEST", PAIR_ACCEPT: "PAIR_ACCEPT",
              SECURE: "SECURE", SOS: "SOS", SOS_CLEAR: "SOS_CLEAR"}

HEADER_SIZE = 9     # START, TYPE, ID(2), SRC(2), DST(2), LENGTH
CRC_SIZE = 2
OVERHEAD = HEADER_SIZE + CRC_SIZE

# The module takes at most 240 bytes per write, three of which are its
# own addressing header. 200 leaves headroom and keeps every packet well
# under a quarter-second of air at 9600 bps.
MAX_MESSAGE = 200
MAX_PACKET = OVERHEAD + MAX_MESSAGE

CRC_INIT = 0xFFFF
CRC_POLY = 0x1021


def crc16(data: bytes) -> int:
    """CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflection."""
    crc = CRC_INIT
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ CRC_POLY) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


@dataclass(frozen=True)
class Packet:
    type: int
    msg_id: int
    src: int
    dst: int
    payload: bytes = b""

    @property
    def text(self) -> str:
        # errors="replace": a message cut mid-character by an older sender
        # should still show, not vanish.
        return self.payload.decode("utf-8", errors="replace")

    @property
    def type_name(self) -> str:
        return TYPE_NAMES.get(self.type, f"0x{self.type:02X}")

    def encode(self) -> bytes:
        return encode(self)

    def __str__(self):
        return (f"{self.type_name} id={self.msg_id} {self.src:#06x}->{self.dst:#06x} "
                f"{len(self.payload)}B")


def encode(packet: Packet) -> bytes:
    if packet.type not in TYPE_NAMES:
        raise ValueError(f"unknown packet type {packet.type:#04x}")
    if len(packet.payload) > MAX_MESSAGE:
        raise ValueError(f"message {len(packet.payload)} B is over the "
                         f"{MAX_MESSAGE} B limit")
    for name, value in (("id", packet.msg_id), ("src", packet.src), ("dst", packet.dst)):
        if not 0 <= value <= 0xFFFF:
            raise ValueError(f"{name} {value} does not fit in 16 bits")
    body = bytes([
        packet.type,
        packet.msg_id >> 8, packet.msg_id & 0xFF,
        packet.src >> 8, packet.src & 0xFF,
        packet.dst >> 8, packet.dst & 0xFF,
        len(packet.payload),
    ]) + packet.payload
    crc = crc16(body)
    return bytes([START]) + body + bytes([crc >> 8, crc & 0xFF])


def decode(frame: bytes) -> Packet | None:
    """One complete frame -> Packet. None if malformed or corrupt."""
    if len(frame) < OVERHEAD or frame[0] != START:
        return None
    if frame[1] not in TYPE_NAMES:
        return None
    length = frame[8]
    if length > MAX_MESSAGE or len(frame) != OVERHEAD + length:
        return None
    body = frame[1:HEADER_SIZE + length]
    expected = (frame[-2] << 8) | frame[-1]
    if crc16(body) != expected:
        return None
    return Packet(
        type=frame[1],
        msg_id=(frame[2] << 8) | frame[3],
        src=(frame[4] << 8) | frame[5],
        dst=(frame[6] << 8) | frame[7],
        payload=bytes(frame[HEADER_SIZE:HEADER_SIZE + length]),
    )


def rssi_dbm(byte: int) -> int:
    """The module reports RSSI as one byte b meaning -(256 - b) dBm."""
    return -(256 - byte)


class Deframer:
    """Feed arbitrary runs of UART bytes; get whole packets and RSSI out.

    `feed()` returns a list of events, each `("packet", Packet)` or
    `("rssi", dBm)`. A packet is delivered the instant its last byte
    arrives, so the ACK can go straight back. The module appends its RSSI
    byte a millisecond later, usually in the *next* read, so it arrives
    as its own event and belongs to the packet just before it.

    Telling that byte from the start of the next packet: the byte right
    after a good packet is RSSI unless it is START *and* the byte after
    it is a valid packet type. A lone trailing 0xAA -- a perfectly real
    -86 dBm -- waits for one more byte to decide. With RSSI reporting
    off in the module, the next START is simply the next packet.
    """

    MAX_BUFFER = 4096
    # A real reading lands between about -20 and -150 dBm. 0x00 would be
    # "-256 dBm": a stray byte, not a measurement.
    RSSI_BYTE_RANGE = (106, 236)

    def __init__(self):
        self._buffer = bytearray()
        self._rssi_expected = False
        self.packets_ok = 0
        self.packets_bad = 0
        self.bytes_skipped = 0

    @classmethod
    def plausible_rssi(cls, byte: int) -> bool:
        low, high = cls.RSSI_BYTE_RANGE
        return low <= byte <= high

    def _skip(self, count: int = 1):
        del self._buffer[:count]
        self.bytes_skipped += count

    def feed(self, data: bytes) -> list:
        self._buffer.extend(data)
        if len(self._buffer) > self.MAX_BUFFER:
            self._skip(len(self._buffer) - self.MAX_BUFFER)

        events = []
        while self._buffer:
            if self._rssi_expected:
                first = self._buffer[0]
                if first == START:
                    if len(self._buffer) < 2:
                        break  # -86 dBm, or the next packet? Wait and see.
                    if self._buffer[1] in TYPE_NAMES:
                        self._rssi_expected = False
                        continue  # the next packet; no RSSI reported
                self._rssi_expected = False
                if self.plausible_rssi(first):
                    del self._buffer[0]
                    events.append(("rssi", rssi_dbm(first)))
                continue

            start = self._buffer.find(START)
            if start < 0:
                self._skip(len(self._buffer))
                break
            if start:
                self._skip(start)
            if len(self._buffer) < 2:
                break
            if self._buffer[1] not in TYPE_NAMES:
                self._skip()   # 0xAA in noise, or someone else's protocol
                continue
            if len(self._buffer) < HEADER_SIZE:
                break
            length = self._buffer[8]
            if length > MAX_MESSAGE:
                self.packets_bad += 1
                self._skip()
                continue
            total = OVERHEAD + length
            if len(self._buffer) < total:
                break  # still arriving
            packet = decode(bytes(self._buffer[:total]))
            if packet is None:
                self.packets_bad += 1
                self._skip()   # resynchronise from the next START
                continue
            del self._buffer[:total]
            self.packets_ok += 1
            self._rssi_expected = True
            events.append(("packet", packet))
        return events
