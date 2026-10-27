"""Full desktop control: click buttons, type and press keys in any app on this laptop (turned on 22 Oct 2026).

Things are found by name through Windows UI Automation (the accessibility layer screen readers use), so "click play
in spotify" presses the button called Play, not a guessed spot on the screen. Clicks use the control's own Invoke
action when it has one (your mouse doesn't move); otherwise the mouse clicks the control's centre and is put back.

Safety rails (the reason this is a module of its own):
  * never in terminals, the Run box, Registry Editor, Task Manager, Windows Security or JARVIS itself;
  * never types into password boxes;
  * Send / Post / Buy / Delete / Uninstall … buttons and Enter in chat apps are asked about first (by the caller);
  * acts only on the window you were last using (or the app you named), never on a hidden one.
"""
from __future__ import annotations

import ctypes
import os
import re
import threading
import time
from ctypes import wintypes

import psutil

# The block lists, risk words and chat apps live in gate.py (the one place every action is checked); re-exported here.
from .gate import (ABORT, ALLOWED_COMBOS, BLOCKED_NAMES, BLOCKED_PROCESSES, BROWSER_PROCESSES, CHAT_APPS,  # noqa: E402,F401
                   CONFIRM_COMBOS, CONFIRM_WORDS, PRESSABLE_COMBOS, SAY_NO_COMBO, WINDOW_VERBS, Action, Gate,
                   normalize_combo, text_class)
from .highlight import validate_highlight

CLICKABLE = {"ButtonControl", "HyperlinkControl", "ListItemControl", "MenuItemControl", "TabItemControl", "DataItemControl",
             "TreeItemControl", "CheckBoxControl", "RadioButtonControl", "SplitButtonControl", "ComboBoxControl",
             "EditControl", "TextControl", "ImageControl", "MenuBarItemControl"}
KEYS = {"enter": 0x0D, "escape": 0x1B, "tab": 0x09, "space": 0x20, "backspace": 0x08, "down": 0x28, "up": 0x26,
        "left": 0x25, "right": 0x27, "page down": 0x22, "page up": 0x21, "home": 0x24, "end": 0x23, "delete": 0x2E}
MEDIA_KEYS = {"toggle": 0xB3, "play": 0xB3, "pause": 0xB3, "next": 0xB0, "previous": 0xB1, "mute": 0xAD, "unmute": 0xAD}
EXTENDED = {0x25, 0x26, 0x27, 0x28, 0x21, 0x22, 0x24, 0x23, 0x2E, 0xB3, 0xB0, 0xB1, 0xAD, 0x5B}
MODIFIER_VK = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B}   # win only ever for snap left/right and show desktop
# What "find the X button / link / box" means in UI Automation control types.
INTERACTIVE = frozenset({"ButtonControl", "SplitButtonControl", "HyperlinkControl", "MenuItemControl", "MenuBarItemControl",
                         "TabItemControl", "EditControl", "ComboBoxControl", "CheckBoxControl", "RadioButtonControl",
                         "ListItemControl", "TreeItemControl"})
CONTROL_KINDS = {"button": {"ButtonControl", "SplitButtonControl"}, "link": {"HyperlinkControl"},
                 "field": {"EditControl", "ComboBoxControl"}, "box": {"EditControl", "ComboBoxControl"},
                 "menu": {"MenuItemControl", "MenuBarItemControl"}, "tab": {"TabItemControl"},
                 "checkbox": {"CheckBoxControl"}, "radio": {"RadioButtonControl"},
                 "option": {"ButtonControl", "SplitButtonControl", "CheckBoxControl", "RadioButtonControl", "MenuItemControl",
                            "HyperlinkControl", "ListItemControl", "TabItemControl"},
                 "any": INTERACTIVE}
SW_MINIMIZE, SW_MAXIMIZE, SW_RESTORE = 6, 3, 9
WM_SYSCOMMAND, SC_CLOSE = 0x0112, 0xF060


def position_words(rect, window) -> tuple[str, str]:
    """('top'|'middle'|'bottom', 'left'|'centre'|'right'): where a control's centre is inside its window."""
    l, t, r, b = rect
    wl, wt, wr, wb = window
    w, h = max(1, wr - wl), max(1, wb - wt)
    fx, fy = ((l + r) / 2 - wl) / w, ((t + b) / 2 - wt) / h
    return ("top" if fy < 1 / 3 else "middle" if fy < 2 / 3 else "bottom",
            "left" if fx < 1 / 3 else "centre" if fx < 2 / 3 else "right")


def key_vk(key: str) -> int | None:
    """Virtual-key code for the last part of a shortcut ('s', 'tab', 'f5', 'left'), or None."""
    if len(key) == 1 and key.isalnum():
        return ord(key.upper())
    if re.fullmatch(r"f\d{1,2}", key) and 1 <= int(key[1:]) <= 12:
        return 0x70 + int(key[1:]) - 1
    return KEYS.get(key)


class DesktopError(RuntimeError):
    """Written to be spoken."""


def _user32():
    return ctypes.windll.user32


GA_ROOT = 2


def foreground_root() -> int | None:
    """The top-level window that really has the keyboard right now."""
    u = _user32()
    u.GetForegroundWindow.restype = ctypes.c_void_p
    u.GetAncestor.restype = ctypes.c_void_p
    fg = u.GetForegroundWindow()
    return (u.GetAncestor(ctypes.c_void_p(fg), GA_ROOT) or fg) if fg else None


def window_process(hwnd: int) -> str:
    pid = wintypes.DWORD()
    _user32().GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
    try:
        return psutil.Process(pid.value).name().lower()
    except Exception:
        return ""


def window_title(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _user32().GetWindowTextW(ctypes.c_void_p(hwnd), buf, 256)
    return buf.value


_EDGE_PIDS: dict[int, bool] = {}


def is_jarvis_edge(hwnd: int, profile_marker: str = "edge-agent") -> bool:
    """Is this window JARVIS's own Edge (its separate profile), as opposed to your normal Edge?"""
    pid = wintypes.DWORD()
    _user32().GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
    if pid.value not in _EDGE_PIDS:
        try:
            p = psutil.Process(pid.value)
            _EDGE_PIDS[pid.value] = p.name().lower() == "msedge.exe" and any(profile_marker in a for a in p.cmdline())
        except Exception:
            _EDGE_PIDS[pid.value] = False
    return _EDGE_PIDS[pid.value]


def is_jarvis_window(hwnd: int, proc: str, title: str) -> bool:
    """JARVIS's own dashboard tab, spidey and the server: never a target, and never 'the app you were using'."""
    if title.startswith("JARVIS Zero") or title == "JARVIS bot":
        return True
    if title == "Spidey" or title.startswith(("Spidey - ", "Spidey and ")):    # the dashboard tab, renamed 27 Oct
        return True
    if proc == "msedge.exe" and is_jarvis_edge(hwnd, "edge-tv"):     # spidey's mini TV
        return True
    if proc in ("python.exe", "pythonw.exe"):
        pid = wintypes.DWORD()
        _user32().GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
        try:
            return "jarvis" in " ".join(psutil.Process(pid.value).cmdline()).lower()
        except Exception:
            return False
    return False


class Desktop:
    """Remembers the app window you were last using, and clicks / types / presses keys in it."""

    # on_target(rect, label): the server draws the "this is where I will click" ring. Display only; None = no ring.
    on_target = None

    def __init__(self, is_own_window=is_jarvis_window, gate: Gate | None = None):
        self.is_own_window = is_own_window
        self.gate = gate or Gate.permissive()   # the real server always passes its strict gate
        self.gate.probe = self.window_facts     # lets Gate.read_allowed(hwnd) look at a window by itself
        self.last_hwnd: int | None = None       # last app you used (not JARVIS's dashboard or spidey)
        self.last_changed = 0.0                 # when you switched to it
        self._stop = False
        threading.Thread(target=self._track, daemon=True, name="desktop-focus").start()

    # ---------- which window ----------
    def _track(self) -> None:
        u = _user32()
        u.GetForegroundWindow.restype = ctypes.c_void_p
        while not self._stop:
            try:
                hwnd = u.GetForegroundWindow()
                if hwnd and hwnd != self.last_hwnd:
                    proc, title = window_process(hwnd), window_title(hwnd)
                    desktop_itself = proc == "explorer.exe" and title in ("Program Manager", "")
                    if title and not desktop_itself and not self.is_own_window(hwnd, proc, title):
                        self.last_hwnd, self.last_changed = hwnd, time.monotonic()
            except Exception:
                pass
            time.sleep(0.3)

    def find_app_window(self, name: str, wait: float = 0.0, visible: bool = True) -> int | None:
        """A top-level window whose program or title matches name ('spotify', 'notepad', 'valorant'). visible=False
        also finds one hidden in the tray (only for reading its title, e.g. Spotify's song name)."""
        want = re.sub(r"[^a-z0-9]", "", name.lower())
        if len(want) < 3:
            return None
        deadline = time.monotonic() + wait
        while True:
            if ABORT.is_set():
                return None
            hits = []

            def each(hwnd, _):
                if (not visible or _user32().IsWindowVisible(hwnd)) and window_title(hwnd):
                    proc = re.sub(r"[^a-z0-9]", "", window_process(hwnd).removesuffix(".exe"))
                    title = re.sub(r"[^a-z0-9]", "", window_title(hwnd).lower())
                    if proc.startswith(want) or want in proc or (len(want) >= 4 and want in title):
                        hits.append(hwnd)
                return True
            cb = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(each)
            _user32().EnumWindows(cb, 0)
            if hits or time.monotonic() >= deadline:
                return hits[0] if hits else None
            time.sleep(0.5)

    def describe(self, hwnd: int) -> str:
        proc = window_process(hwnd)
        title = window_title(hwnd)
        nice = {"spotify.exe": "Spotify", "notepad.exe": "Notepad", "msedge.exe": "Edge", "chrome.exe": "Chrome",
                "explorer.exe": "File Explorer", "code.exe": "VS Code", "cursor.exe": "Cursor"}
        return nice.get(proc) or (title.split(" - ")[-1].strip() if " - " in title else title) or proc.removesuffix(".exe")

    def check_allowed(self, hwnd: int) -> None:
        """A thin wrapper over the gate's window rules (gate.window_block) plus 'not JARVIS itself'."""
        proc, title = window_process(hwnd), window_title(hwnd)
        why = self.gate.window_block(proc, title, None, hwnd=hwnd)
        if why:
            raise DesktopError(why)
        if self.is_own_window(hwnd, proc, title):
            raise DesktopError("That's my own window, so I'll leave it alone.")

    # ---------- reading a window (never clicks or types; the gate's read_allowed decides whether it may be read) ----------
    def window_facts(self, hwnd: int) -> dict:
        """What Gate.read_allowed needs to know about a window: program, title, address bar (browsers), whether it is one of
        JARVIS's own, and whether it asked not to be captured. Names only, never contents."""
        try:
            proc, title = window_process(hwnd), window_title(hwnd)
            facts = {"proc": proc, "title": title,
                     "own": bool(self.is_own_window(hwnd, proc, title) or is_jarvis_edge(hwnd)),
                     "excluded": self._capture_excluded(hwnd), "address": None}
            if proc in BROWSER_PROCESSES:
                facts["address"] = self._address_bar(hwnd)
            return facts
        except Exception:
            return {"_error": True}

    @staticmethod
    def _capture_excluded(hwnd: int) -> bool:
        """True when the window asked not to be captured (SetWindowDisplayAffinity), or when that can't be checked."""
        try:
            aff = wintypes.DWORD(0)
            ok = _user32().GetWindowDisplayAffinity(ctypes.c_void_p(hwnd), ctypes.byref(aff))
            return (not ok) or aff.value != 0
        except Exception:
            return True

    def _address_bar(self, hwnd: int) -> str | None:
        """The browser's address bar text (UIA "Address and search bar"), or None if it can't be found."""
        import uiautomation as auto
        t0 = time.monotonic()
        try:
            with auto.UIAutomationInitializerInThread():
                for c, _depth in auto.WalkControl(auto.ControlFromHandle(hwnd), maxDepth=14):
                    if time.monotonic() - t0 > 1.5:
                        break
                    if c.ControlTypeName == "EditControl" and (
                            "address and search bar" in (c.Name or "").lower() or "omnibox" in (c.ClassName or "").lower()
                            or "address" in (c.AutomationId or "").lower()):
                        pat = c.GetValuePattern()
                        value = (pat.Value if pat else "") or ""
                        return value.strip()[:300] or None
        except Exception:
            return None
        return None

    def read_window(self, hwnd: int, max_chars: int = 4000, max_nodes: int = 1500, budget_s: float = 2.5) -> str:
        """The text a person would read in that window, from UI Automation: error and dialog text first, then reading
        order. Skips off-screen and zero-size controls, password boxes and anything labelled like a password, PIN, OTP,
        card or ID-number box (its name or its value). Stops at the node, character or time limit, or when the kill
        switch is pressed. The caller (ToolBox.screen_read) has already asked Gate.read_allowed, and redacts the result."""
        import uiautomation as auto
        from .safety import SENSITIVE_LABEL
        t0 = time.monotonic()
        error_word = re.compile(r"error|failed|warning|cannot|exception", re.I)
        found: list[tuple] = []                              # (is_error, top, left, order, text)
        seen: set[str] = set()
        nodes, dialog_depth = 0, None
        with auto.UIAutomationInitializerInThread():
            for c, depth in auto.WalkControl(auto.ControlFromHandle(hwnd), maxDepth=45):
                if ABORT.is_set() or nodes >= max_nodes or time.monotonic() - t0 > budget_s:
                    break
                nodes += 1
                try:
                    ctype = c.ControlTypeName
                    if dialog_depth is not None and depth <= dialog_depth:
                        dialog_depth = None
                    if ctype == "WindowControl" and depth > 0 and dialog_depth is None:
                        dialog_depth = depth                 # a dialog or pop-up inside the window: its text goes first
                    if c.IsOffscreen or c.IsPassword:
                        continue
                    name = re.sub(r"\s+", " ", c.Name or "").strip()
                    if SENSITIVE_LABEL.search(f"{name} {c.AutomationId or ''}".replace("_", " ").replace("-", " ")):
                        continue
                    r = c.BoundingRectangle
                    if r.width() < 3 or r.height() < 3:
                        continue
                    texts = []
                    if ctype in ("EditControl", "DocumentControl"):
                        body = self._control_text(c)
                        if body:
                            texts.append(body)
                        elif ctype == "DocumentControl" and name:
                            texts.append(name)               # a document with no readable body: at least its title
                    elif ctype in ("TextControl", "ListItemControl", "DataItemControl", "HyperlinkControl"):
                        texts.append(name)
                    for text in texts:
                        key = re.sub(r"\s+", " ", text).strip().lower()
                        if len(key) < 2 or key in seen:
                            continue
                        seen.add(key)
                        found.append((bool(error_word.search(text)) or dialog_depth is not None, r.top, r.left, len(found),
                                      text.strip()))
                except Exception:
                    continue                                 # a control that vanished or won't answer: skip it
        found.sort(key=lambda f: (not f[0], f[1] // 12, f[2], f[3]))
        out, used = [], 0
        for _err, _top, _left, _n, text in found:
            if used >= max_chars:
                break
            piece = text[:max_chars - used]
            out.append(piece)
            used += len(piece) + 1
        return "\n".join(out)

    @staticmethod
    def _control_text(c) -> str:
        """An editor's or text box's contents: TextPattern first (whole documents), else ValuePattern (500 chars)."""
        try:
            tp = c.GetTextPattern()
            text = tp.DocumentRange.GetText(2000) if tp else ""
            if text and text.strip():
                return text.strip()
        except Exception:
            pass
        try:
            vp = c.GetValuePattern()
            return ((vp.Value if vp else "") or "").strip()[:500]
        except Exception:
            return ""

    def _root_of(self, hwnd: int) -> int | None:
        u = _user32()
        u.GetAncestor.restype = ctypes.c_void_p
        return u.GetAncestor(ctypes.c_void_p(hwnd), GA_ROOT) or hwnd

    def focus(self, hwnd: int) -> bool:
        """Bring the window to the front; True only once Windows really shows it in front (polls up to half a second)."""
        import uiautomation as auto
        u = _user32()
        if u.IsIconic(ctypes.c_void_p(hwnd)):
            u.ShowWindow(ctypes.c_void_p(hwnd), 9)            # SW_RESTORE
        with auto.UIAutomationInitializerInThread():
            try:
                auto.ControlFromHandle(hwnd).SetActive()       # handles Windows' focus-stealing rules
            except Exception:
                pass
        time.sleep(0.15)
        deadline = time.monotonic() + 0.5
        want = self._root_of(hwnd)
        while True:
            if foreground_root() == want:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def hand_back(self, hwnd: int) -> None:
        """Typing mode from spidey's bar or the panel: after typing into your app, put the keyboard back in JARVIS's
        own window so you can type the next piece. Only ever JARVIS's own windows; nothing is typed or clicked."""
        try:
            if self.is_own_window(hwnd, window_process(hwnd), window_title(hwnd)):
                self.focus(hwnd)
        except Exception:
            pass

    def _ensure_foreground(self, hwnd: int) -> int:
        """Before any key or click: the window must REALLY be in front, and that real foreground window must pass the
        same rules (a different window can steal focus between our check and our keystroke)."""
        ok = self.focus(hwnd)
        real = foreground_root()
        if real:
            proc, title = window_process(real), window_title(real)
            why = self.gate.window_block(proc, title, None, hwnd=real)
            if why:
                raise DesktopError(why)
            if self.is_own_window(real, proc, title):
                raise DesktopError("That's my own window, so I'll leave it alone.")
        if not ok or not real or real != self._root_of(hwnd):
            raise DesktopError(f"I couldn't bring {self.describe(hwnd)} to the front, so I didn't type anything.")
        return real

    def _focused_info(self, ctrl, hwnd: int) -> dict:
        """What the gate needs to know about the control that has the keyboard (names only, never its contents)."""
        def get(attr):
            try:
                return str(getattr(ctrl, attr) or "")
            except Exception:
                return ""
        ancestors, p = [], ctrl
        for _ in range(8):
            try:
                p = p.GetParentControl()
            except Exception:
                break
            if not p:
                break
            ancestors.append(f"{p.ClassName} {p.Name}")
        try:
            password = bool(ctrl.IsPassword)
        except Exception:
            password = False
        return {"name": get("Name"), "automation_id": get("AutomationId"), "class_name": get("ClassName"),
                "help": get("HelpText"), "is_password": password, "ctype": get("ControlTypeName")[:-7],
                "ancestors": ancestors, "jarvis_edge": is_jarvis_edge(hwnd)}

    def _focused_now(self, hwnd: int) -> dict:
        import uiautomation as auto
        try:
            with auto.UIAutomationInitializerInThread():
                ctrl = auto.GetFocusedControl()
                return self._focused_info(ctrl, hwnd) if ctrl else {}
        except Exception:
            return {"_error": True}                            # can't tell what has the keyboard: the gate says no

    def focused_info(self, hwnd: int) -> dict:
        """Names (never contents) of the control that has the keyboard, for deciding whether Enter needs a yes."""
        return self._focused_now(hwnd)

    def recheck(self, hwnd: int, hit: dict) -> bool:
        """At the moment of a yes: is the control we asked about still there, with the same name, in the same place?"""
        import uiautomation as auto
        try:
            with auto.UIAutomationInitializerInThread():
                c = hit["control"]
                if not c.Exists(0, 0):
                    return False
                if re.sub(r"\s+", " ", c.Name or "").strip() != hit["name"] or c.IsOffscreen:
                    return False
                r = c.BoundingRectangle
                return abs((r.left + r.right) // 2 - hit["x"]) <= 8 and abs((r.top + r.bottom) // 2 - hit["y"]) <= 8
        except Exception:
            return False

    def _authorize(self, kind: str, hwnd: int | None, label: str = "", text: str = "", key: str = "",
                   ctype: str = "", focused: dict | None = None) -> None:
        """The one question every click, key and typed character must pass. Raises a spoken DesktopError on no."""
        proc, title = (window_process(hwnd), window_title(hwnd)) if hwnd else ("", "")
        d = self.gate.authorize(Action(kind, hwnd, proc, title, label, len(text), key, "user", ctype, text_class(text)),
                                focused)
        if d.verdict == "deny":
            raise DesktopError(d.say)
        if d.verdict == "confirm":
            raise DesktopError("That needs your yes first, so I didn't do it.")

    # ---------- finding things by name ----------
    def find(self, hwnd: int, target: str, nth: int = 0, wait: float = 0.0, prefix: bool = False) -> dict | None:
        """Best match for target among the window's named controls: exact name, then starts-with, then contains;
        real buttons and links before plain text. prefix=True: any name starting with target (e.g. 'Play ')."""
        import uiautomation as auto
        want = re.sub(r"\s+", " ", target.lower())
        want = want if prefix else want.strip()                 # "play " must not match "Playlists"
        deadline = time.monotonic() + wait
        with auto.UIAutomationInitializerInThread():
            while True:
                scored, _total = self._walk_named(hwnd, want, CLICKABLE, max(50, nth + 1), 4.0, prefix=prefix)
                if scored or time.monotonic() >= deadline or ABORT.is_set():
                    break
                time.sleep(0.5)
            if not scored:
                return None
            item = scored[min(nth, len(scored) - 1)]
            same = sum(1 for x in scored if x[0] == item[0])      # matches that are just as good as the one chosen
            return self._hit(hwnd, item, same, want.strip())

    def _hit(self, hwnd: int, item: tuple, count: int, want: str | None = None) -> dict:
        """One found control as the dict the callers use (click, highlight, recheck)."""
        s, _len, _order, c, name, r = item
        title = window_title(hwnd)
        return {"name": name, "type": c.ControlTypeName[:-7], "x": (r.left + r.right) // 2,
                "y": (r.top + r.bottom) // 2, "control": c,
                "button": c.ControlTypeName in ("ButtonControl", "SplitButtonControl", "MenuItemControl"),
                "rect": (r.left, r.top, r.right, r.bottom), "proc": window_process(hwnd), "title_prefix": title[:60],
                "quality": self._quality(name, want), "count": count}

    @staticmethod
    def _quality(name: str, want: str | None) -> str:
        low = name.lower()
        return "exact" if want is None or low == want else "prefix" if low.startswith(want) else "contains"

    def _walk_named(self, hwnd: int, query: str | None, kinds, limit: int, budget: float, prefix: bool = False,
                    skip_sensitive: bool = False) -> tuple[list[tuple], int]:
        """The one walk over a window's controls (the caller holds UIAutomationInitializerInThread). Returns
        (best `limit` matches, how many matched): tuples (rank, name length, order, control, name, rect).
        With a query: exact name (0), starts with (1), contains (2), plain text and pictures one step worse; best first.
        Without one: every named control in reading order. Off-screen and tiny controls never count. skip_sensitive drops
        password boxes and anything labelled like a password, PIN, OTP or card box, so they are never listed or offered."""
        import uiautomation as auto
        from .safety import SENSITIVE_LABEL
        want = query
        scored, t0, nodes = [], time.monotonic(), 0
        for c, depth in auto.WalkControl(auto.ControlFromHandle(hwnd), maxDepth=45):
            nodes += 1
            if time.monotonic() - t0 > budget or ABORT.is_set() or nodes > 4000:
                break
            if c.ControlTypeName not in kinds:
                continue
            name = re.sub(r"\s+", " ", (c.Name or "")).strip()
            low = name.lower()
            if not low or len(low) > 300:
                continue
            if want is None:
                s = 0
            elif prefix:
                s = 0 if low.startswith(want) and len(low) > len(want) else -1
            else:
                s = 0 if low == want else 1 if low.startswith(want) else 2 if want in low else -1
            if s < 0:
                continue
            try:
                if c.IsOffscreen:
                    continue
                if skip_sensitive and (c.IsPassword or SENSITIVE_LABEL.search(
                        f"{name} {c.AutomationId or ''}".replace("_", " ").replace("-", " "))):
                    continue
                r = c.BoundingRectangle
            except Exception:
                continue
            if r.width() < 3 or r.height() < 3:
                continue
            if c.ControlTypeName in ("TextControl", "ImageControl", "DataItemControl"):
                s += 1
            scored.append((s, len(low), len(scored), c, name, r))
        if want is None:
            # reading order, with the window's own Minimize / Maximize / Close buttons last (nobody means those)
            caption = {"minimize", "maximize", "restore", "restore down", "close"}
            scored.sort(key=lambda x: (x[4].lower() in caption and x[3].ControlTypeName == "ButtonControl",
                                       x[5].top // 12, x[5].left, x[2]))
        else:
            scored.sort(key=lambda x: x[:3])
        return scored[:limit], len(scored)

    def list_controls(self, hwnd: int, kinds=None, query: str | None = None, limit: int = 6) -> list[dict]:
        """The window's buttons / links / boxes (kinds: 'button', 'link', 'field', 'menu', 'tab', 'option'… or None for all),
        best match first when a query is given, else in reading order, at most `limit`. Goes through Gate.read_allowed like
        any other look at a window; password / PIN / OTP / card boxes are never listed. Each result says where it is
        (position words), and carries its control so a pick can click it later (through the gate again)."""
        import uiautomation as auto
        why = self.gate.read_allowed(hwnd, self.window_facts(hwnd))
        if why:
            raise DesktopError(why)
        if kinds is None:
            types = INTERACTIVE
        elif isinstance(kinds, str):
            types = CONTROL_KINDS.get(kinds.lower().rstrip("s"), INTERACTIVE)
        else:
            types = set().union(*[CONTROL_KINDS.get(str(k).lower(), {str(k)}) for k in kinds])
        want = re.sub(r"\s+", " ", query.lower()).strip() if query else None
        rect = wintypes.RECT()
        _user32().GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect))
        win = (rect.left, rect.top, rect.right, rect.bottom)
        with auto.UIAutomationInitializerInThread():
            found, total = self._walk_named(hwnd, want or None, types, max(1, min(int(limit), 12)), 4.0, skip_sensitive=True)
            out = []
            for item in found:
                hit = self._hit(hwnd, item, total, want)
                v, h = position_words(hit["rect"], win)
                kind = hit["type"].lower()
                out.append({**hit, "kind": {"hyperlink": "link", "edit": "box", "combobox": "box", "menuitem": "menu item",
                                            "tabitem": "tab", "checkbox": "checkbox", "radiobutton": "radio button",
                                            "splitbutton": "button", "listitem": "list item", "treeitem": "tree item",
                                            "menubaritem": "menu"}.get(kind, kind),
                            "vertical": v, "horizontal": h, "position": f"{v} {h}", "is_sensitive": False})
        return out

    def highlight(self, hwnd: int, hit: dict) -> None:
        """Show the ring on a control without touching it (display only). Skipped when the window can't be read."""
        if not self.on_target or not hit.get("rect"):
            return
        try:
            if self.gate.read_allowed(hwnd, self.window_facts(hwnd)):
                return
        except Exception:
            return
        self._emit_target(hit)

    def _emit_target(self, hit: dict) -> bool:
        """Tell the overlay where the control is. Never allowed to stop what the person asked for. Returns False when
        nothing was shown (no ring wanted or a bad rectangle)."""
        cb = self.on_target
        if not cb or not hit.get("rect"):
            return False
        v = validate_highlight(hit["rect"], hit.get("name", ""))
        if not v:
            return False
        try:
            return cb(v["rect"], v["label"]) is not False
        except Exception:
            return True                                           # the ring failed; the click goes ahead

    # ---------- doing things ----------
    def click(self, hwnd: int, hit: dict) -> str:
        import uiautomation as auto
        self.check_allowed(hwnd)
        self._ensure_foreground(hwnd)
        self._authorize("click", hwnd, label=hit["name"], ctype=hit.get("type") or ("Button" if hit.get("button") else ""))
        if self.on_target and self._emit_target(hit):        # the gate said yes: show the ring, give the eye a moment
            time.sleep(0.25)
        with auto.UIAutomationInitializerInThread():
            c = hit["control"]
            try:
                c.GetInvokePattern().Invoke()                   # the control's own "click": the mouse stays put
                return hit["name"]
            except Exception:
                pass
        self.mouse_click(hit["x"], hit["y"], hwnd)
        return hit["name"]

    def mouse_click(self, x: int, y: int, hwnd: int) -> None:
        """Only ever called after _authorize. Clicks only if that exact spot is really inside the window and is not
        covered by another one (a pop-up could have appeared since the control was found)."""
        u = _user32()
        if ABORT.is_set():
            raise DesktopError("Stopped.")
        rect = wintypes.RECT()
        u.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect))
        u.WindowFromPoint.restype = ctypes.c_void_p
        u.GetAncestor.restype = ctypes.c_void_p
        top = u.WindowFromPoint(wintypes.POINT(int(x), int(y)))
        owner = u.GetAncestor(ctypes.c_void_p(top), GA_ROOT) if top else None
        if not (rect.left <= x < rect.right and rect.top <= y < rect.bottom) or owner != hwnd:
            raise DesktopError("That spot is covered by another window.")
        old = wintypes.POINT()
        u.GetCursorPos(ctypes.byref(old))
        u.SetCursorPos(int(x), int(y))
        time.sleep(0.03)
        u.mouse_event(0x0002, 0, 0, 0, 0)                      # left down
        u.mouse_event(0x0004, 0, 0, 0, 0)                      # left up
        time.sleep(0.05)
        u.SetCursorPos(old.x, old.y)                           # put your mouse back where it was

    def type_text(self, hwnd: int, text: str) -> str:
        """Types where the text cursor is in that window; if nothing is focused, into its first visible text box."""
        import uiautomation as auto
        self.check_allowed(hwnd)
        self._ensure_foreground(hwnd)
        with auto.UIAutomationInitializerInThread():
            focused = auto.GetFocusedControl()
            editable = focused and focused.ControlTypeName in ("EditControl", "DocumentControl", "ComboBoxControl")
            if not editable:
                box = next((c for c, _ in auto.WalkControl(auto.ControlFromHandle(hwnd), maxDepth=45)
                            if c.ControlTypeName in ("EditControl", "DocumentControl") and not c.IsOffscreen), None)
                if box is None:
                    raise DesktopError("I can't find a text box in that window. Click into one and ask me again.")
                box.SetFocus()
                focused = box
            try:
                info = self._focused_info(focused, hwnd)
            except Exception:
                info = {"_error": True}                          # can't tell what this box is: the gate says no
            if info.get("is_password"):
                raise DesktopError("That's a password box, so I'll leave typing that one to you.")
            kind = focused.ControlTypeName[:-7].lower()
        self._authorize("type", hwnd, text=text, focused=info)
        typed = self._send_unicode(text, hwnd)
        if typed < len(text):
            raise DesktopError(f"Stopped after typing {typed} of {len(text)} characters.")
        return kind

    # Classic text boxes take characters straight from WM_CHAR. Windows 11 Notepad scrambles injected key presses
    # ("Hello from" became "Hello ,,,,,"), but WM_CHAR, sent one at a time and waited on, arrives exactly as written.
    _CHAR_CLASSES = ("richedit", "edit", "_wwg", "scintilla")

    def _char_box(self, hwnd: int | None) -> int | None:
        """The focused classic text box inside this window (it must belong to it), or None to use key presses."""
        if not hwnd:
            return None

        class GUITHREADINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD), ("hwndActive", wintypes.HWND),
                        ("hwndFocus", wintypes.HWND), ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                        ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND), ("rcCaret", wintypes.RECT)]
        try:
            u = _user32()
            tid = u.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), None)
            gi = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
            if not tid or not u.GetGUIThreadInfo(tid, ctypes.byref(gi)) or not gi.hwndFocus:
                return None
            box = gi.hwndFocus
            if self._root_of(box) != self._root_of(hwnd):
                return None
            buf = ctypes.create_unicode_buffer(64)
            u.GetClassNameW(ctypes.c_void_p(box), buf, 64)
            return box if buf.value.lower().startswith(self._CHAR_CLASSES) else None
        except Exception:
            return None                                   # can't tell: key presses, as before

    def _send_unicode(self, text: str, hwnd: int | None = None) -> int:
        """Only ever called after _authorize. Returns how many characters went in (the kill switch can cut it short)."""
        box = self._char_box(hwnd)
        if box:
            u = _user32()
            result = ctypes.c_size_t()
            sent = 0
            for ch in text.replace("\r\n", "\n"):
                if ABORT.is_set() or self.gate.killed.is_set():   # the kill chord: stop at once
                    break
                n = ord(ch)
                units = [13] if ch == "\n" else [n] if n <= 0xFFFF else \
                    [0xD800 + ((n - 0x10000) >> 10), 0xDC00 + ((n - 0x10000) & 0x3FF)]   # an emoji is two halves
                # WM_CHAR, waited on (max 1 s; a hung app stops the typing instead of hanging JARVIS)
                if not all(u.SendMessageTimeoutW(ctypes.c_void_p(box), 0x0102, code, 1, 0x0002, 1000,
                                                 ctypes.byref(result)) for code in units):
                    break
                sent += 1
            return sent

        class KI(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        class INPUT(ctypes.Structure):
            class _U(ctypes.Union):
                _fields_ = [("ki", KI), ("pad", ctypes.c_byte * 32)]
            _anonymous_ = ("u",)
            _fields_ = [("type", wintypes.DWORD), ("u", _U)]
        # a few characters at a time: apps like Notepad drop keys that arrive in one big burst
        sent = 0
        for i in range(0, len(text), 8):
            if ABORT.is_set() or self.gate.killed.is_set():       # the kill chord: stop within one chunk
                break
            events = []
            for ch in text[i:i + 8]:
                for flags in (0x0004, 0x0004 | 0x0002):          # KEYEVENTF_UNICODE, then key up
                    ev = INPUT(type=1)
                    ev.ki = KI(0, ord(ch), flags, 0, 0)
                    events.append(ev)
            arr = (INPUT * len(events))(*events)
            _user32().SendInput(len(events), arr, ctypes.sizeof(INPUT))
            sent += len(text[i:i + 8])
            time.sleep(0.02)
        return sent

    def press(self, hwnd: int | None, key: str) -> None:
        vk = KEYS.get(key.lower())
        if vk is None:
            raise DesktopError(f"I don't know the {key} key.")
        hwnd = hwnd or foreground_root()                        # no window named: whatever really has the keyboard
        if not hwnd:
            raise DesktopError("I can't tell which window has the keyboard, so I didn't press anything.")
        self.check_allowed(hwnd)
        self._ensure_foreground(hwnd)
        self._authorize("key", hwnd, key=key.lower(), focused=self._focused_now(hwnd))
        self._tap(vk)

    @staticmethod
    def _tap(vk: int) -> None:
        flags = 0x0001 if vk in EXTENDED else 0
        _user32().keybd_event(vk, 0, flags, 0)
        _user32().keybd_event(vk, 0, flags | 0x0002, 0)

    def press_combo(self, hwnd: int | None, combo: str) -> str:
        """A keyboard shortcut ('ctrl+s') in that window, only from the fixed list in gate.PRESSABLE_COMBOS. The risky ones
        (ctrl+w, alt+f4, print, cut…) need the person's yes, which arrives through gate.scope(confirmed=True). Returns the
        shortcut as it was pressed."""
        norm = normalize_combo(combo)
        if norm not in PRESSABLE_COMBOS:
            raise DesktopError(SAY_NO_COMBO)
        hwnd = hwnd or foreground_root()
        if not hwnd:
            raise DesktopError("I can't tell which window has the keyboard, so I didn't press anything.")
        self.check_allowed(hwnd)
        self._ensure_foreground(hwnd)
        *mods, key = norm.split("+")
        self._press_chord(hwnd, mods, key, "combo", norm, self._focused_now(hwnd), label=self.describe(hwnd))
        return norm

    def _press_chord(self, hwnd: int | None, mods: list[str], key: str, kind: str, gate_key: str,
                     focused: dict | None = None, label: str = "") -> None:
        """Modifiers down, the key, modifiers up (in the reverse order, always, even if something goes wrong in between).
        Only ever called with a shortcut from the fixed lists; the gate is asked first, here."""
        vk = key_vk(key)
        if vk is None or any(m not in MODIFIER_VK for m in mods):
            raise DesktopError(SAY_NO_COMBO)
        self._authorize(kind, hwnd, label=label, key=gate_key, focused=focused)
        if ABORT.is_set() or self.gate.killed.is_set():
            raise DesktopError("Stopped.")
        u, down = _user32(), []
        try:
            for m in mods:
                u.keybd_event(MODIFIER_VK[m], 0, 0x0001 if MODIFIER_VK[m] in EXTENDED else 0, 0)
                down.append(m)
            self._tap(vk)
        finally:
            for m in reversed(down):
                u.keybd_event(MODIFIER_VK[m], 0, (0x0001 if MODIFIER_VK[m] in EXTENDED else 0) | 0x0002, 0)

    def window_action(self, hwnd: int | None, verb: str) -> str:
        """minimize | maximize | restore | close | snap_left | snap_right | show_desktop | switch. Ordinary window verbs,
        never a process kill: 'close' sends the window its own close request (the app may ask to save) and needs a yes.
        The Windows key is used only for the three fixed ones (snap left, snap right, show desktop)."""
        if verb not in WINDOW_VERBS:
            raise DesktopError(SAY_NO_COMBO)
        if verb == "show_desktop":
            self._press_chord(None, ["win"], "d", "window", verb, label="the desktop")
            return "the desktop"
        if not hwnd or not window_title(hwnd):
            raise DesktopError("I can't find that window any more.")
        self.check_allowed(hwnd)
        name = self.describe(hwnd)
        u = _user32()
        if verb in ("snap_left", "snap_right"):
            self._ensure_foreground(hwnd)
            self._press_chord(hwnd, ["win"], "left" if verb == "snap_left" else "right", "window", verb, label=name)
            return name
        self._authorize("window", hwnd, label=name, key=verb)
        if verb == "switch":
            if not self.focus(hwnd):
                raise DesktopError(f"I couldn't bring {name} to the front.")
        elif verb == "close":
            u.PostMessageW(ctypes.c_void_p(hwnd), WM_SYSCOMMAND, SC_CLOSE, 0)      # its own close button, not a kill
        else:
            u.ShowWindow(ctypes.c_void_p(hwnd), {"minimize": SW_MINIMIZE, "maximize": SW_MAXIMIZE, "restore": SW_RESTORE}[verb])
        return name

    def media_key(self, action: str) -> None:
        """The keyboard's media keys: whatever app is playing (Spotify, a browser, VLC…) reacts."""
        d = self.gate.authorize(Action("media", None, key=action))
        if d.verdict != "allow":
            raise DesktopError(d.say or "That needs your yes first, so I didn't do it.")
        self._tap(MEDIA_KEYS[action])

    def scroll(self, hwnd: int, direction: str) -> None:
        self.check_allowed(hwnd)
        self._ensure_foreground(hwnd)
        self._authorize("scroll", hwnd, key=direction)
        # keys, not the mouse wheel: the wheel scrolls whatever is under the pointer, which may be another window
        self._tap(KEYS[{"top": "home", "bottom": "end", "up": "page up"}.get(direction, "page down")])

    def open_uri(self, uri: str) -> None:
        os.startfile(uri)

    def is_edge(self, hwnd: int) -> bool:
        return is_jarvis_edge(hwnd)

    def process(self, hwnd: int) -> str:
        return window_process(hwnd)

    def title(self, hwnd: int) -> str:
        return window_title(hwnd)

    @staticmethod
    def spotify_installed() -> bool:
        return any(os.path.exists(os.path.expandvars(p)) for p in (r"%APPDATA%\Spotify\Spotify.exe",
                                                                   r"%LOCALAPPDATA%\Microsoft\WindowsApps\Spotify.exe"))
