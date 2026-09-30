"""The screen chrome every MFruit screen shares.

    status_bar   page name on the left; WiFi and battery on the right
    footer       gesture hints: [("tap", "next"), ("hold", "open")]
    draw_list    MFruit OS list rows with a highlighted selection
    toast        a short message above the footer
    message      a centred heading and body for empty and error states

Layout (``theme``): the status bar ends above ``CONTENT_TOP`` (40), content
runs to ``CONTENT_BOTTOM`` (248), the footer text starts at ``FOOTER_Y``
(256). Keep text ``CORNER_INSET`` (20) px from the left and right edges at
the top and bottom: the Whisplay LCD has rounded corners.
"""

from __future__ import annotations

from typing import Any, NamedTuple, Optional

from .theme import (CONTENT_BOTTOM, CONTENT_TOP, CORNER_INSET, FOOTER_Y, MARGIN, ROW_H,
                    ROW_H_SUB, SCREEN_W, STATUS_Y, tone_color)

# Standard hint labels, so every app words the gestures the same way.
TAP, TWICE, THRICE, FOUR, HOLD = "tap", "2×", "3×", "4×", "hold"
RELEASE = "release"


def _draw_wifi(c, x: int, y: int, level: int) -> None:
    """Three arcs, lit according to signal strength (0 = disconnected)."""
    t = c.theme
    cx, cy = x + 8, y + 13
    for index, radius in enumerate((4, 8, 12)):
        color = t.text if level >= index + 1 else t.surface_hi
        c.draw.arc((cx - radius, cy - radius, cx + radius, cy + radius), 225, 315, fill=color,
                   width=2)
    c.draw.ellipse((cx - 1, cy - 1, cx + 1, cy + 1), fill=t.text if level else t.text_faint)
    if level == 0:
        c.draw.line((x + 1, y + 2, x + 15, y + 14), fill=t.warning, width=2)


def _draw_battery(c, x: int, y: int, percent: int, charging: bool) -> int:
    """Battery outline, fill and percentage right-aligned at ``x``; returns the new x."""
    t = c.theme
    label = f"{percent}%"
    x -= c.text_width(label, 13, "medium")
    c.text(x, y + 1, label, 13, "medium", t.text)
    x -= 27
    body = (x, y + 4, x + 21, y + 14)
    c.rounded(body, 2, outline=t.text_muted, width=1)
    c.rect((body[2] + 1, y + 7, body[2] + 2, y + 11), t.text_muted)
    fill_w = max(1, int(17 * percent / 100))
    color = t.error if percent <= 15 else (t.success if charging else t.text)
    c.rect((x + 2, y + 6, x + 2 + fill_w, y + 12), color)
    if charging:
        c.bolt(x + 5, y + 3, 12, t.bg)
    return x


def status_bar(c, title: str = "", status=None, badge: Optional[tuple] = None,
               dot=None, reserve: int = 0, title_sizes: tuple = (17,)) -> int:
    """Page name on the left; WiFi signal and battery on the right.

    ``status`` is a ``mfruit_sdk.status.Status`` (or anything with
    ``wifi_level``, ``battery`` and ``charging``). Left of the WiFi icon an
    app may show its own state: ``dot`` (a colour) is a small status light,
    ``badge`` a short (text, colour) such as ("REC", theme.error). Keep it
    short: the page name gets whatever width is left. ``reserve`` keeps
    that many pixels free left of those for the app to draw its own
    indicator (e.g. a radio signal meter); the return value is the x where
    that slot starts. ``title_sizes`` (largest first, e.g. (17, 15, 13))
    lets a name that is data, such as another radio's, shrink before it
    is cut with an ellipsis.
    """
    t = c.theme
    x = SCREEN_W - CORNER_INSET
    wifi = getattr(status, "wifi_level", None)
    battery = getattr(status, "battery", None)
    if battery is not None:
        x = _draw_battery(c, x, STATUS_Y, battery, bool(getattr(status, "charging", False))) - 8
    if wifi is not None:
        x -= 17
        _draw_wifi(c, x, STATUS_Y, wifi)
        x -= 6
    if dot:
        x -= 8
        c.draw.ellipse((x, STATUS_Y + 5, x + 7, STATUS_Y + 12), fill=dot)
        x -= 7
    if badge:
        text, color = badge
        width = c.text_width(text, 11, "bold")
        x -= width
        c.text(x, STATUS_Y + 3, text, 11, "bold", color)
        x -= 6
    if reserve:
        x -= reserve
    slot = x
    if reserve:
        x -= 6
    if title:
        left = CORNER_INSET - 4
        room = max(20, x - left - 6)
        size = next((s for s in title_sizes if c.text_width(title, s, "bold") <= room),
                    title_sizes[-1])
        c.text(left, STATUS_Y - 1 + (17 - size) // 2, title, size, "bold", t.text,
               max_width=room)
    return slot


def footer(c, hints: list, y: int = FOOTER_Y) -> None:
    """``hints`` is [(gesture label, action label)], e.g. [("tap", "next")].

    Hints that do not fit are dropped from the end, so list the most
    important first.
    """
    t = c.theme
    if not hints:
        return
    parts = list(hints)
    size = 11
    widths = [c.text_width(g, size, "semibold") + 4 + c.text_width(a, size) for g, a in parts]
    gap = 12
    total = sum(widths) + gap * (len(parts) - 1)
    while total > SCREEN_W - 2 * CORNER_INSET and len(parts) > 1:
        parts.pop()
        widths.pop()
        total = sum(widths) + gap * (len(parts) - 1)
    x = (SCREEN_W - total) // 2
    c.hline(CORNER_INSET, SCREEN_W - CORNER_INSET, y - 5, t.separator)
    for (gesture, action), width in zip(parts, widths):
        gw = c.text(x, y, gesture, size, "semibold", t.accent)
        c.text(x + gw + 4, y, action, size, "regular", t.text_muted)
        x += width + gap


def menu_hints(select: str = "open", armed: bool = False, back: str = "back") -> list:
    """The standard hints for a list screen."""
    if armed:
        return [(RELEASE, f"to {select}")]
    return [(TAP, "next"), (HOLD, select), (FOUR, back)]


class Row(NamedTuple):
    """One list row. ``kind``: action | nav | toggle | info | back | danger."""
    label: str
    subtitle: Any = None
    value: Any = None            # right-hand text, or bool for toggles
    kind: str = "action"
    tone: str = ""               # success | warning | error | accent | muted
    enabled: bool = True


def row_height(row: Row) -> int:
    return ROW_H_SUB if row.subtitle else ROW_H


def draw_list(c, rows: list, selected: int, top: int = CONTENT_TOP,
              bottom: int = CONTENT_BOTTOM, empty: str = "Nothing here") -> None:
    t = c.theme
    if not rows:
        c.text(SCREEN_W // 2, (top + bottom) // 2, empty, 14, "medium", t.text_faint,
               anchor="mm")
        return
    selected = max(0, min(selected, len(rows) - 1))
    heights = [row_height(row) for row in rows]
    visible = bottom - top
    # Scroll in whole rows: the selected row and, when there is room, one
    # row of context above it.
    first = max(0, selected - 1)
    while first > 0 and sum(heights[first - 1:selected + 1]) <= visible:
        first -= 1
    while first < selected and sum(heights[first:selected + 1]) > visible:
        first += 1
    layer = c.layer(SCREEN_W, visible + 1)
    y = 0
    left, right = MARGIN - 4, SCREEN_W - MARGIN + 4
    for index in range(first, len(rows)):
        height = heights[index]
        if y > visible:
            break
        is_sel = index == selected
        if is_sel:
            layer.rounded((left, y + 1, right, y + height - 1), 12, fill=t.accent_dim)
        elif index > first and index - 1 != selected:
            layer.hline(left + 12, right - 12, y, t.separator)
        _draw_row(layer, rows[index], y, height, left, right, is_sel)
        y += height
    c.image.paste(layer.image, (0, top))
    total = sum(heights)
    if total > visible:
        track_x = SCREEN_W - 5
        shown = sum(heights[first:])
        bar_h = max(18, int(visible * visible / total))
        offset = sum(heights[:first])
        span = max(1, total - min(visible, shown))
        bar_y = top + int((visible - bar_h) * min(1.0, offset / span))
        c.rounded((track_x, top, track_x + 2, bottom), 1, fill=t.surface_hi)
        c.rounded((track_x, bar_y, track_x + 2, bar_y + bar_h), 1, fill=t.text_muted)


def _draw_row(c, row: Row, y: int, height: int, left: int, right: int, selected: bool) -> None:
    t = c.theme
    base = t.text if row.enabled else t.text_faint
    if row.kind == "danger" and row.enabled:
        base = t.error
    if row.kind == "back":
        base = t.text if selected else t.text_muted
    x = left + 10
    right_edge = right - 10
    value = row.value
    if row.kind == "toggle":
        c.toggle(right_edge - 30, y + (height - 16) // 2, bool(value))
        right_edge -= 38
    elif row.kind == "nav":
        c.chevron(right_edge - 10, y + (height - 12) // 2, 12)
        right_edge -= 16
        if value not in (None, ""):
            w = c.text(right_edge, y + height // 2, str(value), 13, "medium",
                       tone_color(t, row.tone, t.text_muted), anchor="rm",
                       max_width=int((right_edge - x) * 0.45))
            right_edge -= w + 8
    elif value not in (None, ""):
        w = c.text(right_edge, y + height // 2, str(value), 13, "medium",
                   tone_color(t, row.tone, t.text_muted), anchor="rm",
                   max_width=int((right_edge - x) * 0.42))
        right_edge -= w + 8
    max_w = right_edge - x
    weight = "semibold" if selected else "medium"
    if row.subtitle:
        c.text(x, y + 7, str(row.label), 15, weight, base, max_width=max_w)
        sub_tone = row.tone if value in (None, "") and row.kind != "nav" else ""
        c.text(x, y + 26, str(row.subtitle), 12, "regular",
               tone_color(t, sub_tone, t.text_muted), max_width=max_w)
    else:
        c.text(x, y + height // 2, str(row.label), 15, weight, base, anchor="lm",
               max_width=max_w)


def toast(c, text: str, tone: str = "", y: int = FOOTER_Y - 40) -> None:
    t = c.theme
    width = min(SCREEN_W - 40, c.text_width(text, 13, "semibold") + 28)
    x0 = (SCREEN_W - width) // 2
    c.rounded((x0, y, x0 + width, y + 30), 15, fill=t.surface_hi,
              outline=tone_color(t, tone, t.separator))
    c.text(SCREEN_W // 2, y + 15, text, 13, "semibold", t.text, anchor="mm",
           max_width=width - 20)


def message(c, heading: str, body: str = "", tone: str = "", top: int = CONTENT_TOP,
            bottom: int = CONTENT_BOTTOM) -> None:
    """A heading and wrapped body, centred vertically in the content area."""
    t = c.theme
    lines = c.wrap(body, 13, "regular", SCREEN_W - 2 * MARGIN - 8, max_lines=6) if body else []
    height = 24 + len(lines) * 18
    y = top + max(0, (bottom - top - height) // 2)
    c.text(SCREEN_W // 2, y, heading, 18, "bold", tone_color(t, tone, t.text), anchor="ma",
           max_width=SCREEN_W - 2 * MARGIN)
    y += 28
    for line in lines:
        c.text(SCREEN_W // 2, y, line, 13, "regular", t.text_muted, anchor="ma")
        y += 18


def text_field(c, text: str, y: int, placeholder: str = "Type…", cursor: bool = True,
               height: int = 34) -> None:
    """A single-line input box; long text scrolls so its end stays visible."""
    t = c.theme
    left, right = MARGIN - 4, SCREEN_W - MARGIN + 4
    c.rounded((left, y, right, y + height), 10, fill=t.surface, outline=t.accent)
    inner = right - left - 24
    shown = text
    while shown and c.text_width(shown, 15, "medium") > inner:
        shown = shown[1:]
    if shown != text:
        shown = "…" + shown[1:]
    if text:
        w = c.text(left + 12, y + height // 2, shown, 15, "medium", t.text, anchor="lm")
    else:
        w = 0
        c.text(left + 12, y + height // 2, placeholder, 15, "regular", t.text_faint, anchor="lm")
    if cursor:
        cx = left + 12 + w + 1
        c.rect((cx, y + 9, cx + 1, y + height - 9), t.accent)
