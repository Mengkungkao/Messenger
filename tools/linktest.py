#!/usr/bin/env python3
"""Radio bring-up and link measurements, without the app.

    python3 tools/linktest.py hello            send HELLO once, then print what is heard
    python3 tools/linktest.py listen           print every packet heard; ACK pings
    python3 tools/linktest.py ping -n 50       50 pings: loss, round trip, signal

Run `listen` (or the app itself -- it answers pings too) on the other
radio. The app must not be running on *this* radio: only one process can
own the serial port.

`ping` is the range / packet-loss / latency test from the project plan.
Each ping waits for its ACK before the next goes, so the round-trip time
is a real one: host -> UART -> air -> other radio -> ACK -> air -> host.
Move one radio away between runs and watch loss and RSSI change.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config as config_module  # noqa: E402
from lora import packet as pk, protocol  # noqa: E402
from lora.link import Link  # noqa: E402
from lora.sx126x import SX126x  # noqa: E402


def open_link(config, port=None):
    rc = config.radio
    radio = SX126x(port=port or rc.port, addr=rc.address, freq_mhz=rc.frequency_mhz,
                   uart_baud=rc.uart_baud,
                   mode_pins=tuple(rc.mode_pins) if rc.mode_pins else None)
    # Measurements should not be throttled; keep it short and legal by hand.
    return Link(radio, rc.air_speed, duty_cycle_percent=100.0)


def printer(address):
    def on_packet(packet):
        text = f" {packet.text!r}" if packet.type in (pk.TEXT, pk.HELLO, pk.HELLO_REPLY) else ""
        mark = "" if protocol.is_for(packet, address) else " (not for us)"
        print(f"{time.strftime('%H:%M:%S')} {packet}{text}{mark}", flush=True)
    return on_packet


def listen(link, address, ids, name, seconds=None):
    show = printer(address)

    def on_packet(packet):
        show(packet)
        if not protocol.is_for(packet, address):
            return
        if packet.type in (pk.TEXT, pk.PING):
            link.transmit(protocol.ack_packet(address, packet))
        elif packet.type == pk.HELLO:
            link.transmit(protocol.hello_packet(address, ids.next(), name, reply=True,
                                                dst=packet.src))

    link.on_packet = on_packet
    link.on_rssi = lambda dbm: print(f"           rssi {dbm} dBm", flush=True)
    link.start()
    try:
        if seconds:
            time.sleep(seconds)
        else:
            while True:
                time.sleep(3600)
    except KeyboardInterrupt:
        pass


def ping(link, address, ids, dst, count, size, timeout, gap):
    acked = threading.Event()
    state = {"id": None, "rssi": None}

    def on_packet(packet):
        if packet.type == pk.ACK and packet.dst == address and packet.msg_id == state["id"]:
            acked.set()

    def on_rssi(dbm):
        state["rssi"] = dbm

    link.on_packet, link.on_rssi = on_packet, on_rssi
    link.start()
    rtts, rssis, lost = [], [], 0
    for index in range(1, count + 1):
        msg_id = ids.next()
        state["id"], state["rssi"] = msg_id, None
        acked.clear()
        sent = time.monotonic()
        link.transmit(protocol.ping_packet(address, dst, msg_id, size))
        if acked.wait(timeout):
            rtt = (time.monotonic() - sent) * 1000
            time.sleep(0.05)   # the RSSI byte trails the ACK
            rtts.append(rtt)
            if state["rssi"] is not None:
                rssis.append(state["rssi"])
            print(f"{index:4d}  #{msg_id:<5d} ACK {rtt:6.0f} ms  "
                  f"{state['rssi'] if state['rssi'] is not None else '?'} dBm", flush=True)
        else:
            lost += 1
            print(f"{index:4d}  #{msg_id:<5d} lost", flush=True)
        time.sleep(gap)

    print(f"\n{count} pings, {lost} lost ({100 * lost / count:.0f}% loss)")
    if rtts:
        print(f"round trip ms: min {min(rtts):.0f}  avg {statistics.mean(rtts):.0f}  "
              f"max {max(rtts):.0f}")
    if rssis:
        print(f"ACK signal dBm: min {min(rssis)}  avg {statistics.mean(rssis):.0f}  "
              f"max {max(rssis)}")
    return 0 if lost < count else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("mode", choices=("hello", "listen", "ping"))
    parser.add_argument("--port", help="serial port (default: config.yaml)")
    parser.add_argument("-n", "--count", type=int, default=20)
    parser.add_argument("--size", type=int, default=40, help="ping padding bytes")
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--gap", type=float, default=1.0, help="seconds between pings")
    parser.add_argument("--to", type=lambda v: int(v, 0), default=protocol.BROADCAST,
                        help="peer address (default: anyone)")
    args = parser.parse_args()

    config = config_module.load()
    address, name = config.radio.address, config.identity.name
    ids = protocol.MessageIds(config.data_dir / "ids.json")
    link = open_link(config, args.port)
    print(f"this radio: {name} #{address:04X} on {args.port or config.radio.port}")
    try:
        if args.mode == "ping":
            return ping(link, address, ids, args.to, args.count, args.size,
                        args.timeout, args.gap)
        if args.mode == "hello":
            link.transmit(protocol.hello_packet(address, ids.next(), name))
            print("sent HELLO; listening for 10 s")
            listen(link, address, ids, name, seconds=10)
        else:
            listen(link, address, ids, name)
        return 0
    finally:
        link.stop()
        link.radio.close()


if __name__ == "__main__":
    raise SystemExit(main())
