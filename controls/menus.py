"""The lists behind "Pair a radio", "Paired radios" and SOS.

A ``Menu`` is a list the same single button drives everywhere in MFruit OS:
tap next, 2x previous, hold-and-release select, 4x back. Its title, info
lines and rows may be functions, so a screen follows live state (a radio
found, a code, a countdown) without being rebuilt.

The builders take the running ``Messenger`` (``app``) and only call its
public actions; nothing here touches the radio directly.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

ALARM_BEEP_SECONDS = 6.0
REPLIES = ("On my way", "I'm calling for help")


@dataclass
class Menu:
    kind: str                  # pair | paired | confirm | sos | countdown | alarm
    title: object              # str, or a function returning one
    items: object              # [(label, action or None)], or a function returning it
    lines: object = ()         # information shown above the rows
    index: int = 0
    on_back: object = None     # four clicks / Esc; default: close the menu
    tone: str = "idle"         # "alarm": the red SOS look
    data: object = None        # e.g. the Alarm being shown


def resolve(value):
    return value() if callable(value) else value


# --------------------------------------------------------------- pairing
def pair_menu(app) -> Menu:
    pairing = app.pairing
    pairing.start()

    def title():
        if pairing.incoming:
            return f"Pair with {pairing.incoming.name}?"
        if pairing.outgoing:
            return f"Pairing {pairing.outgoing.name}"
        return "Pair a radio"

    def lines():
        if pairing.incoming:
            return [f"Code {pairing.incoming.code}", "Same code on the other", "radio? Then say yes."]
        if pairing.outgoing:
            return [f"Code {pairing.outgoing.code}", "Waiting for the other", "radio to accept…"]
        found = pairing.candidates()
        out = (["Pick the radio to pair:"] if found
               else ["Open Pair a radio on the", "other radio too. Searching…"])
        if pairing.result:
            out.append(pairing.result)
        return out

    def items():
        if pairing.incoming:
            return [("Yes, same code", pairing.accept), ("No, refuse", pairing.reject)]
        if pairing.outgoing:
            return [("Cancel", pairing.cancel_request)]
        rows = []
        for radio in pairing.candidates():
            signal = f"  {radio.rssi} dBm" if radio.rssi is not None else ""
            rows.append((f"{radio.name}{signal}", lambda a=radio.address: pairing.request(a)))
        return rows + [("Cancel", app.close_menu)]

    return Menu("pair", title, items, lines)


def paired_menu(app) -> Menu:
    security = app.security

    def items():
        names = security.contacts.all() if security.contacts is not None else {}
        rows = [(names.get(address) or f"Radio {address}",
                 lambda a=address: app.open_menu(unpair_menu(app, a)))
                for address in security.paired]
        return rows + [("Back", app.close_menu)]

    return Menu("paired", "Paired radios",
                items, lambda: [] if security.paired else ["No radios paired yet."])


def unpair_menu(app, address: int) -> Menu:
    security = app.security
    name = (security.contacts.all().get(address) if security.contacts is not None else "") \
        or f"Radio {address}"

    def unpair():
        security.keyring.remove_peer(address)
        if security.contacts is not None:
            security.contacts.remove(address)
        app.flash(f"Unpaired {name}", "ok")
        app.close_menu()

    return Menu("confirm", f"Unpair {name}?", [("Keep", app.close_menu), ("Unpair", unpair)],
                ["Its messages are no longer", "readable, and yours to it", "go out in the clear."],
                on_back=app.close_menu)


# ------------------------------------------------------------------- SOS
def sos_menu(app) -> Menu:
    sos = app.emergency
    if sos.active:
        def stop():
            sos.clear()
            app.close_menu()
            app.flash("Telling everyone you're OK", "ok", 4.0)

        def lines():
            heard = sos.heard_by
            sent = sos.outgoing.transmissions if sos.outgoing else 0
            return [f"Heard by {heard} radio{'s' if heard != 1 else ''}" if heard
                    else "Nobody has answered yet", f"Sent {sent} time{'s' if sent != 1 else ''}",
                    "Keep this app open."]
        return Menu("sos", "SOS is active", [("Keep sending", app.close_menu),
                                             ("I'm OK - stop SOS", stop)], lines, tone="alarm")
    seconds = app.config.emergency.countdown_seconds
    return Menu("sos", "Send SOS?",
                [("Cancel", app.close_menu), (f"Send SOS in {seconds} s", app.begin_sos_countdown)],
                ["Calls for help on every", "radio in range. This is not", "a certified emergency",
                 "service: carry a phone too."], on_back=app.close_menu)


def countdown_menu(app, until: float) -> Menu:
    def title():
        return f"SOS in {max(0, math.ceil(until - time.monotonic()))}"
    return Menu("countdown", title, [("Cancel", app.cancel_sos_countdown)],
                ["Sending a call for help", "to every radio in range."],
                on_back=app.cancel_sos_countdown, tone="alarm")


# ----------------------------------------------------------- an SOS heard
def alarm_lines(alarm) -> list:
    age = max(0, int(time.monotonic() - alarm.first_heard))
    when = f"{age} s ago" if age < 120 else f"{age // 60} min ago"
    heard = f"Heard {when}" + (f" · {alarm.rssi} dBm" if alarm.rssi is not None else "")
    lines = [alarm.message]
    if alarm.place:
        lines.append(alarm.place)
    if alarm.battery is not None:
        lines.append(f"Battery {alarm.battery}%")
    lines.append(heard)
    return lines


def alarm_menu(app, alarm) -> Menu:
    def reply(text):
        def action():
            app.send(text, dst=alarm.src)
            app.emergency.dismiss(alarm)
            app.flash(f"Sent: {text}", "ok", 4.0)
        return action

    def dismiss():
        app.emergency.dismiss(alarm)

    items = [(text, reply(text)) for text in REPLIES] + [("Dismiss alarm", dismiss)]
    return Menu("alarm", lambda: f"SOS: {alarm.name or 'a radio'}", items,
                lambda: alarm_lines(alarm), on_back=dismiss, tone="alarm", data=alarm)
