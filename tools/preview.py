#!/usr/bin/env python3
"""Render the Messenger's screens to PNG without a Pi, from sample data.

    python3 tools/preview.py                     # -> /tmp/messenger-preview
    python3 tools/preview.py --out DIR

Writes one PNG per state, plus all-screens.png, a contact sheet. On a
machine without MFruit OS installed, set MFRUIT_FONT_DIR=~/MFruitOS/assets/fonts
to preview with MFruit OS's font.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mfruit_sdk.status import Status  # noqa: E402
from PIL import Image  # noqa: E402

import main as app_main  # noqa: E402
from display.whisplay import HEIGHT, WIDTH, Bubble, Picker, View, render  # noqa: E402

DEVICE = Status(3, 82, False)
CHAT = [
    Bubble("Where are you?", False, "17:02 · -63 dBm"),
    Bubble("On my way", True, "17:03 ✓", "ok"),
    Bubble("Meet me at five o'clock by the old bridge", False, "17:04 · -64 dBm"),
    Bubble("OK", True, "17:05 · sending…", "busy"),
]
REPLIES = ["OK", "Yes", "No", "On my way", "Call me", "Where are you?"]

SHOTS = [
    ("1-chat", View(title="orangepizero2w", bubbles=CHAT, hints=app_main.HINTS_TALK,
                    device=Status(3, 100, True))),
    ("2-empty", View(title="Messenger", device=DEVICE, hints=app_main.HINTS_TALK,
                     empty_hint=["No messages yet", "Hold to talk", "2 clicks: quick replies",
                                 "or type, and Enter sends"])),
    ("3-scrolled", View(title="jarvis", device=DEVICE, position="2/4", anchor=1,
                        bubbles=[Bubble(b.text, b.mine, b.meta, b.meta_tone,
                                        selected=i == 1) for i, b in enumerate(CHAT)],
                        hints=[("3×", "read aloud"), ("tap", "older"), ("4×", "exit")])),
    ("4-listening", View(title="jarvis", device=DEVICE, bubbles=CHAT,
                         status="Listening… 2.4s", status_tone="listen", level=0.55)),
    ("5-typing", View(title="jarvis", device=DEVICE, bubbles=CHAT,
                      compose="See you there, bring the", hints=app_main.HINTS_TYPING)),
    ("6-replies", View(title="jarvis", device=DEVICE, bubbles=CHAT,
                       picker=Picker(REPLIES, index=3), hints=app_main.HINTS_PICKER)),
    ("7-replies-armed", View(title="jarvis", device=DEVICE, bubbles=CHAT,
                             picker=Picker(REPLIES, index=1), hints=[("release", "to send")])),
    ("8-failed", View(title="jarvis", device=DEVICE,
                      bubbles=CHAT[:3] + [Bubble("Running late", True, "17:06 ✗ not confirmed",
                                                 "error", selected=True, failed=True)],
                      anchor=3, position="4/4",
                      hints=[("2×", "resend"), ("tap", "older"), ("4×", "exit")])),
    ("9-busy", View(title="jarvis", device=DEVICE, bubbles=CHAT,
                    status="Radio busy: quit WalkieTalkie", status_tone="error")),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", default="/tmp/messenger-preview")
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    tiles = []
    for name, view in SHOTS:
        image = render(view)
        image.save(out / f"{name}.png")
        tiles.append(image)
        print(f"  wrote {out / name}.png")
    columns = 5
    rows = (len(tiles) + columns - 1) // columns
    sheet = Image.new("RGB", (8 + columns * (WIDTH + 8), 8 + rows * (HEIGHT + 8)), (24, 26, 34))
    for index, tile in enumerate(tiles):
        row, column = divmod(index, columns)
        sheet.paste(tile, (8 + column * (WIDTH + 8), 8 + row * (HEIGHT + 8)))
    sheet.save(out / "all-screens.png")
    print(f"  wrote {out / 'all-screens.png'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
