"""Spidey's guide, shown in the floating island at the top of the screen: "guide me to change my wallpaper".

Spidey looks at the window you're using, works out the ONE thing to use next, and points at it: the step in the island,
a web from spidey, a ring around it. When you click it (or the window changes), it looks again for the next step,
until the goal is done. It never clicks or types anything itself.

It keeps nothing: each look is held in memory for one step and dropped; nothing goes to the log, the conversation or
memory except the goal and the step sentences. What goes to the cloud (the person chose "labels only", 27 Oct;
setting guide_cloud_labels): the goal, the steps so far, the app's name and the window's title, and the names, kinds
and positions of its buttons, menus, links, tabs, boxes and list items, each name cut to 40 characters and redacted.
Never what's typed in a box (UI Automation names are labels, not values), never password / PIN / OTP / card boxes
(skipped by the walk), never the window's plain text, and never a window the gate keeps private (banking, passwords…).
The labels are fenced as data: the model is told never to follow them."""
from __future__ import annotations

import ctypes
import json
import re
import threading
import time
from ctypes import wintypes

from .untrusted import FENCE_RULE, clean_label, redact, strip_urls, untrusted

MAX_CONTROLS = 70        # per look (the window's own, then a few taskbar buttons)
MAX_NAME = 40            # list items can hold message previews: keep only the start of a label
MAX_STEPS = 15
WAIT_CLICK_S = 180       # no click this long: the guide stops looking and waits for "next"

GUIDE_PROMPT = (
    "You are Spidey, guiding someone step by step on their Windows laptop. You never click for them: you point at the "
    "ONE thing they should use next and say what to do in one short, friendly sentence (at most 14 words).\n"
    "You get their GOAL, the STEPS they have already done, the app they're in, and the controls visible right now as "
    "numbered lines: [n] kind \"name\" (where on screen).\n"
    "Rules:\n"
    "- Point at the control that moves them toward the goal right now.\n"
    "- If the right control isn't visible, point at what reveals it (a menu, a tab, a 'More' or '...' button, the "
    "Start button or search on the taskbar), or say how to get there and use target null. If no taskbar controls are "
    "listed, the taskbar is hidden: tell them to press the Windows key to open Start.\n"
    "- If the window they're in has nothing to do with the goal, tell them which app to open (by name) first.\n"
    "- If the goal is already done on screen, set done true and say so warmly in a few words.\n"
    "- If you can't tell, say so honestly and use target null. Never guess a number that isn't in the list.\n"
    "- Never ask them to type a password, PIN, code or card number.\n"
    + FENCE_RULE + "\n"
    'Reply with ONLY JSON: {"step": "<what to do>", "target": <n or null>, "done": <true or false>}')


def where(rect, screen) -> str:
    """'top left' … 'bottom right' on the screen (helps the model tell two "Settings" apart)."""
    l, t, r, b = rect
    sw, sh = screen
    cx, cy = (l + r) / 2, (t + b) / 2
    v = "top" if cy < sh / 3 else "bottom" if cy > 2 * sh / 3 else "middle"
    h = "left" if cx < sw / 3 else "right" if cx > 2 * sw / 3 else "centre"
    return f"{v} {h}"


def snapshot(desktop, hwnd: int) -> dict:
    """One look at the window (and the taskbar): {"app", "title", "controls": [{"n", "kind", "name", "rect"}]}.
    Raises the gate's reason when the window is private."""
    import uiautomation as auto
    from .desktop import INTERACTIVE, window_title
    why = desktop.gate.read_allowed(hwnd, desktop.window_facts(hwnd))
    if why:
        raise PermissionError(why)
    u = ctypes.windll.user32
    screen = (u.GetSystemMetrics(78) or u.GetSystemMetrics(0), u.GetSystemMetrics(79) or u.GetSystemMetrics(1))
    controls: list[dict] = []
    with auto.UIAutomationInitializerInThread():
        found, _ = desktop._walk_named(hwnd, None, INTERACTIVE, MAX_CONTROLS - 8, 3.0, skip_sensitive=True)
        tray = u.FindWindowW("Shell_TrayWnd", None)
        extra = []
        if tray and tray != hwnd:
            try:
                extra, _ = desktop._walk_named(tray, None, {"ButtonControl"}, 8, 1.0, skip_sensitive=True)
            except Exception:
                extra = []
        for group, items in (("", found), ("taskbar", extra)):
            for _s, _len, _order, c, name, r in items:
                rect = (r.left, r.top, r.right, r.bottom)
                # masked whole first (a card number cut in half wouldn't be recognised any more), then cut short
                label = clean_label(strip_urls(redact(clean_label(name, 300))), MAX_NAME)
                if not label:
                    continue
                kind = c.ControlTypeName[:-7].lower().replace("hyperlink", "link").replace("edit", "box")
                controls.append({"n": len(controls) + 1, "kind": kind, "name": label, "rect": rect,
                                 "where": "taskbar" if group else where(rect, screen)})
    return {"app": desktop.describe(hwnd), "title": clean_label(window_title(hwnd), 60), "controls": controls}


def prompt_for(goal: str, steps: list[str], snap: dict) -> list[dict]:
    lines = "\n".join(f'[{c["n"]}] {c["kind"]} "{c["name"]}" ({c["where"]})' for c in snap["controls"])
    done = "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1)) or "(none yet)"
    screen = untrusted(f"App: {snap['app']}\nWindow: {snap['title']}\nControls:\n{lines or '(none found)'}",
                       "screen labels", limit=4000)
    return [{"role": "system", "content": GUIDE_PROMPT},
            {"role": "user", "content": f"GOAL: {clean_label(goal, 160)}\nSTEPS SO FAR:\n{done}\n{screen}"}]


def parse_step(text: str, snap: dict) -> dict | None:
    """The model's answer → {"step", "target" (a control from this look, or None), "done"}; None if unusable."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return None
    step = strip_urls(clean_label(str(d.get("step") or ""), 140)).strip()
    if not step:
        return None
    target = d.get("target")
    hit = None
    if isinstance(target, int) and not isinstance(target, bool):
        hit = next((c for c in snap["controls"] if c["n"] == target), None)
    return {"step": step, "target": hit, "done": bool(d.get("done"))}


# ---------- waiting for the click ----------
def _foreground() -> int:
    u = ctypes.windll.user32
    u.GetAncestor.restype = ctypes.c_void_p
    h = u.GetForegroundWindow()
    return int(u.GetAncestor(ctypes.c_void_p(h), 2) or 0) if h else 0


def wait_for_move(rect, hwnd: int, stop: threading.Event, advance: threading.Event, timeout: float = WAIT_CLICK_S,
                  ring=None, pad: int = 6, ignore=None) -> str:
    """Until the person clicks inside rect ("clicked"), the window in front or its title changes ("changed"), they say
    "next" ("next"), the guide is stopped ("stop") or nothing happens for `timeout` ("timeout"). Reads only the mouse
    button state and the cursor position: nothing is clicked or typed. ring() is called every 1.5 s to keep the ring up.
    ignore(hwnd) -> True for windows that don't count as a change (Spidey's own: typing "next" in its chat bar)."""
    from .desktop import window_title
    u = ctypes.windll.user32
    title0, fg0 = window_title(hwnd), _foreground()
    end, next_ring, was_down = time.monotonic() + timeout, 0.0, False
    pt = wintypes.POINT()
    while time.monotonic() < end:
        if stop.is_set():
            return "stop"
        if advance.is_set():
            advance.clear()
            return "next"
        now = time.monotonic()
        if ring and rect and now >= next_ring:
            ring()
            next_ring = now + 1.5
        down = bool(u.GetAsyncKeyState(0x01) & 0x8000)
        if was_down and not down and rect:                         # a click just finished: where?
            u.GetCursorPos(ctypes.byref(pt))
            l, t, r, b = rect
            if l - pad <= pt.x <= r + pad and t - pad <= pt.y <= b + pad:
                return "clicked"
        was_down = down
        fg = _foreground()
        if fg and fg != fg0 and not (ignore and ignore(fg)):
            return "changed"
        if int(now * 10) % 5 == 0 and window_title(hwnd) != title0:
            return "changed"
        time.sleep(0.04)
    return "timeout"
