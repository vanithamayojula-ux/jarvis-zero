"""The "this is where I will click" ring: what a highlight event may contain, checked the same way on both ends.

The server only forwards what passes validate_highlight, and the floating bot checks again before drawing, so a bad or
hostile event can never make a big window-sized shape or spoof a dialog. The ring is display only: outline, no fill, no
buttons, click-through (see scripts/bot_overlay.py).

Rectangles are physical screen pixels (what UI Automation reports).
"""
from __future__ import annotations

import re

MAX_W, MAX_H = 3840, 2200          # a ring bigger than this is not a control: dropped
MIN_SIDE = 4
MAX_TTL = 2.0                      # seconds on screen, whatever was asked
DEFAULT_TTL = 0.55
MAX_LABEL = 30
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ​-‏‪-‮⁦-⁩]")


def virtual_screen() -> tuple[int, int, int, int]:
    """(left, top, width, height) of all monitors together, in physical pixels."""
    try:
        import ctypes
        u = ctypes.windll.user32
        return (u.GetSystemMetrics(76), u.GetSystemMetrics(77), u.GetSystemMetrics(78), u.GetSystemMetrics(79))
    except Exception:
        return (0, 0, 1920, 1080)


def clean_label(label, n: int = MAX_LABEL) -> str:
    """Plain text only: control and invisible characters removed, spaces squeezed, at most n characters."""
    return re.sub(r"\s+", " ", _CONTROL.sub(" ", str(label or ""))).strip()[:n]


def _int(v) -> int | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or abs(v) > 10**7:
        return None
    return int(v)


def validate_highlight(rect, label="", ttl=None, virtual: tuple[int, int, int, int] | None = None) -> dict | None:
    """{'rect': (l, t, r, b), 'label': str, 'ttl': float} clamped to the screen, or None if it must not be drawn.
    Needs four numbers, right > left and bottom > top, a size under MAX_W x MAX_H and some part on a screen."""
    try:
        if not isinstance(rect, (list, tuple)) or len(rect) != 4:
            return None
        l, t, r, b = (_int(v) for v in rect)
    except Exception:
        return None
    if None in (l, t, r, b) or r - l < MIN_SIDE or b - t < MIN_SIDE or r - l > MAX_W or b - t > MAX_H:
        return None
    vx, vy, vw, vh = virtual or virtual_screen()
    l, t, r, b = max(l, vx), max(t, vy), min(r, vx + vw), min(b, vy + vh)
    if r - l < MIN_SIDE or b - t < MIN_SIDE:
        return None                                    # entirely off every screen
    try:
        seconds = DEFAULT_TTL if ttl is None else float(ttl)
    except (TypeError, ValueError):
        seconds = DEFAULT_TTL
    if seconds != seconds:
        seconds = DEFAULT_TTL
    return {"rect": (l, t, r, b), "label": clean_label(label), "ttl": max(0.1, min(seconds, MAX_TTL))}


def to_window_geometry(rect, dpi_scale: float = 1.0) -> tuple[int, int, int, int]:
    """(x, y, w, h) for a borderless window. dpi_scale is how many window units one physical pixel is: 1.0 when the
    program is per-monitor DPI aware (the floating bot is), so physical pixels are used as they are."""
    l, t, r, b = rect
    s = dpi_scale or 1.0
    return round(l * s), round(t * s), max(1, round((r - l) * s)), max(1, round((b - t) * s))
