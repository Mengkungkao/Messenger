"""MFruit OS visual tokens and screen geometry (the same values the launcher uses)."""

from __future__ import annotations

from typing import NamedTuple, Tuple

Color = Tuple[int, int, int]

SCREEN_W = 240
SCREEN_H = 280
# The Whisplay LCD has rounded corners; keep text clear of them.
CORNER_INSET = 20
MARGIN = 14

# Screen layout shared by every MFruit screen.
STATUS_Y = 9            # status bar text top: page name left, WiFi + battery right
CONTENT_TOP = 40        # first pixel below the status bar
CONTENT_BOTTOM = 248    # last pixel above the footer
FOOTER_Y = 256          # footer hint text top (a separator sits 5 px above)
ROW_H = 36
ROW_H_SUB = 46


class Theme(NamedTuple):
    name: str
    bg: Color
    surface: Color
    surface_hi: Color
    separator: Color
    text: Color
    text_muted: Color
    text_faint: Color
    accent: Color
    accent_dim: Color
    accent_text: Color
    success: Color
    warning: Color
    error: Color


DARK = Theme(
    name="dark",
    bg=(10, 12, 16),
    surface=(24, 27, 33),
    surface_hi=(34, 38, 46),
    separator=(40, 45, 54),
    text=(244, 246, 250),
    text_muted=(150, 158, 170),
    text_faint=(96, 104, 116),
    accent=(46, 140, 255),
    accent_dim=(16, 42, 82),
    accent_text=(255, 255, 255),
    success=(52, 199, 110),
    warning=(255, 176, 32),
    error=(255, 77, 79),
)

LIGHT = Theme(
    name="light",
    bg=(242, 244, 247),
    surface=(255, 255, 255),
    surface_hi=(232, 236, 242),
    separator=(218, 223, 230),
    text=(17, 20, 24),
    text_muted=(94, 103, 115),
    text_faint=(150, 158, 170),
    accent=(10, 108, 255),
    accent_dim=(214, 230, 255),
    accent_text=(255, 255, 255),
    success=(26, 158, 80),
    warning=(204, 126, 0),
    error=(214, 40, 40),
)


def tone_color(theme: Theme, tone: str, default: Color) -> Color:
    return {"success": theme.success, "warning": theme.warning, "error": theme.error,
            "accent": theme.accent, "muted": theme.text_faint}.get(tone or "", default)
