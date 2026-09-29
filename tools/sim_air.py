#!/usr/bin/env python3
"""Pretend LoRa: pseudo-terminals that behave like E22 modules on one channel.

Lets two (or more) copies of the app talk on one machine with no radio
hardware at all:

    python3 tools/sim_air.py                 # prints two ports, e.g. /dev/pts/5 /dev/pts/6
    MESSENGER_RADIO_PORT=/dev/pts/5 MESSENGER_IDENTITY_NAME=RasPi \\
        MESSENGER_DATA_DIR=/tmp/a ./run.sh --headless
    MESSENGER_RADIO_PORT=/dev/pts/6 MESSENGER_IDENTITY_NAME=OrangePi \\
        MESSENGER_DATA_DIR=/tmp/b ./run.sh --headless

Like a real module in fixed-point mode, each simulated one strips the
3-byte addressing header from every write, puts the rest on the "air",
and appends an RSSI byte to every packet it delivers. `--loss 0.3` drops
30% of packets, to watch ACK/retry work.

A real module finds packet boundaries by UART idle time; a pty has no
such thing, so this one reads the packet's own LENGTH field instead.
"""

from __future__ import annotations

import argparse
import os
import pty
import random
import select
import sys
import time
import tty
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lora import packet as pk  # noqa: E402

ADDRESS_HEADER = 3


def take_packet(buffer: bytearray) -> bytes | None:
    """One host write's worth of air payload, or None if incomplete."""
    need = ADDRESS_HEADER + pk.HEADER_SIZE
    if len(buffer) < ADDRESS_HEADER + 1:
        return None
    if buffer[ADDRESS_HEADER] != pk.START:
        payload = bytes(buffer[ADDRESS_HEADER:])    # not ours: pass it on whole
        buffer.clear()
        return payload
    if len(buffer) < need:
        return None
    total = ADDRESS_HEADER + pk.OVERHEAD + buffer[ADDRESS_HEADER + 8]
    if len(buffer) < total:
        return None
    payload = bytes(buffer[ADDRESS_HEADER:total])
    del buffer[:total]
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--nodes", type=int, default=2)
    parser.add_argument("--loss", type=float, default=0.0, help="drop probability 0-1")
    parser.add_argument("--rssi", type=int, default=-72, help="reported dBm")
    parser.add_argument("--air-speed", type=int, default=9600,
                        help="bps, to delay packets like real airtime")
    args = parser.parse_args()

    nodes = []
    for _ in range(args.nodes):
        master, slave = pty.openpty()
        tty.setraw(slave)
        nodes.append({"master": master, "slave": slave, "path": os.ttyname(slave),
                      "buffer": bytearray()})
    print(" ".join(n["path"] for n in nodes), flush=True)
    rssi_byte = max(0, min(255, 256 + args.rssi))

    while True:
        ready, _, _ = select.select([n["master"] for n in nodes], [], [])
        for node in nodes:
            if node["master"] not in ready:
                continue
            try:
                node["buffer"].extend(os.read(node["master"], 4096))
            except OSError:
                continue
            while (payload := take_packet(node["buffer"])) is not None:
                time.sleep(len(payload) * 8 / args.air_speed)
                kind = pk.TYPE_NAMES.get(payload[1], "?") if len(payload) > 1 else "?"
                if random.random() < args.loss:
                    print(f"{node['path']}: {kind} {len(payload)} B -- dropped", flush=True)
                    continue
                print(f"{node['path']}: {kind} {len(payload)} B", flush=True)
                for other in nodes:
                    if other is not node:
                        os.write(other["master"], payload + bytes([rssi_byte]))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        pass
