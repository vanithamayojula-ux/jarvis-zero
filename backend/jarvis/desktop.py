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

# Apps where one wrong keystroke can run a command or change the system: no clicking or typing at all.
BLOCKED_PROCESSES = {"cmd.exe", "powershell.exe", "pwsh.exe", "windowsterminal.exe", "openconsole.exe", "conhost.exe",
                     "wsl.exe", "wslhost.exe", "bash.exe", "mintty.exe", "regedit.exe", "mmc.exe", "taskmgr.exe",
                     "sechealthui.exe", "consent.exe", "msiexec.exe", "credentialuibroker.exe", "lockapp.exe"}
BLOCKED_NAMES = {"cmd.exe": "Command Prompt", "powershell.exe": "PowerShell", "pwsh.exe": "PowerShell",
                 "windowsterminal.exe": "the terminal", "openconsole.exe": "the terminal", "conhost.exe": "the terminal",
                 "regedit.exe": "Registry Editor", "taskmgr.exe": "Task Manager", "sechealthui.exe": "Windows Security",
                 "mmc.exe": "that system console"}
# Clicking a button with one of these words is asked about first.
CONFIRM_WORDS = ("send", "post", "comment", "reply", "submit", "publish", "tweet", "share", "upload", "buy", "pay",
                 "purchase", "order", "checkout", "delete", "remove", "uninstall", "install", "format", "erase", "reset",
                 "sign out", "log out", "logout", "end task", "restart", "shut down", "accept", "agree", "unfriend",
                 "block", "unsubscribe", "subscribe")
CHAT_APPS = {"whatsapp.exe", "whatsapp.root.exe", "discord.exe", "telegram.exe", "ms-teams.exe", "teams.exe",
             "slack.exe", "outlook.exe", "olk.exe", "signal.exe", "messenger.exe"}
CLICKABLE = {"ButtonControl", "HyperlinkControl", "ListItemControl", "MenuItemControl", "TabItemControl", "DataItemControl",
             "TreeItemControl", "CheckBoxControl", "RadioButtonControl", "SplitButtonControl", "ComboBoxControl",
             "EditControl", "TextControl", "ImageControl", "MenuBarItemControl"}
KEYS = {"enter": 0x0D, "escape": 0x1B, "tab": 0x09, "space": 0x20, "backspace": 0x08, "down": 0x28, "up": 0x26,
        "left": 0x25, "right": 0x27, "page down": 0x22, "page up": 0x21, "home": 0x24, "end": 0x23, "delete": 0x2E}
MEDIA_KEYS = {"toggle": 0xB3, "play": 0xB3, "pause": 0xB3, "next": 0xB0, "previous": 0xB1, "mute": 0xAD, "unmute": 0xAD}
EXTENDED = {0x25, 0x26, 0x27, 0x28, 0x21, 0x22, 0x24, 0x23, 0x2E, 0xB3, 0xB0, 0xB1, 0xAD}


class DesktopError(RuntimeError):
    """Written to be spoken."""


def _user32():
    return ctypes.windll.user32


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

    def __init__(self, is_own_window=is_jarvis_window):
        self.is_own_window = is_own_window
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
        proc, title = window_process(hwnd), window_title(hwnd)
        if proc in BLOCKED_PROCESSES or (proc == "explorer.exe" and title == "Run"):
            what = BLOCKED_NAMES.get(proc, "the Run box" if title == "Run" else "that window")
            raise DesktopError(f"I won't click or type in {what}. One wrong key there could run a command or change "
                               f"system settings, so that one's yours.")
        if self.is_own_window(hwnd, proc, title):
            raise DesktopError("That's my own window, so I'll leave it alone.")

    def focus(self, hwnd: int) -> None:
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
                win = auto.ControlFromHandle(hwnd)
                scored, t0 = [], time.monotonic()
                for c, depth in auto.WalkControl(win, maxDepth=45):
                    if time.monotonic() - t0 > 4:
                        break
                    if c.ControlTypeName not in CLICKABLE:
                        continue
                    name = re.sub(r"\s+", " ", (c.Name or "")).strip()
                    low = name.lower()
                    if not low or len(low) > 300:
                        continue
                    if prefix:
                        s = 0 if low.startswith(want) and len(low) > len(want) else -1
                    else:
                        s = 0 if low == want else 1 if low.startswith(want) else 2 if want in low else -1
                    if s < 0:
                        continue
                    try:
                        if c.IsOffscreen:
                            continue
                        r = c.BoundingRectangle
                    except Exception:
                        continue
                    if r.width() < 3 or r.height() < 3:
                        continue
                    if c.ControlTypeName in ("TextControl", "ImageControl", "DataItemControl"):
                        s += 1
                    scored.append((s, len(low), len(scored), c, name, r))
                if scored or time.monotonic() >= deadline:
                    break
                time.sleep(0.5)
            if not scored:
                return None
            scored.sort(key=lambda x: x[:3])
            s, _, _, c, name, r = scored[min(nth, len(scored) - 1)]
            return {"name": name, "type": c.ControlTypeName[:-7], "x": (r.left + r.right) // 2,
                    "y": (r.top + r.bottom) // 2, "control": c,
                    "button": c.ControlTypeName in ("ButtonControl", "SplitButtonControl", "MenuItemControl")}

    # ---------- doing things ----------
    def click(self, hwnd: int, hit: dict) -> str:
        import uiautomation as auto
        self.check_allowed(hwnd)
        self.focus(hwnd)
        with auto.UIAutomationInitializerInThread():
            c = hit["control"]
            try:
                c.GetInvokePattern().Invoke()                   # the control's own "click": the mouse stays put
                return hit["name"]
            except Exception:
                pass
        self.mouse_click(hit["x"], hit["y"])
        return hit["name"]

    @staticmethod
    def mouse_click(x: int, y: int) -> None:
        u = _user32()
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
        self.focus(hwnd)
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
                password = bool(focused.IsPassword)
            except Exception:
                password = False
            if password:
                raise DesktopError("That's a password box, so I'll leave typing that one to you.")
            kind = focused.ControlTypeName[:-7].lower()
        self._send_unicode(text)
        return kind

    @staticmethod
    def _send_unicode(text: str) -> None:
        class KI(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        class INPUT(ctypes.Structure):
            class _U(ctypes.Union):
                _fields_ = [("ki", KI), ("pad", ctypes.c_byte * 32)]
            _anonymous_ = ("u",)
            _fields_ = [("type", wintypes.DWORD), ("u", _U)]
        # a few characters at a time: apps like Notepad drop keys that arrive in one big burst
        for i in range(0, len(text), 8):
            events = []
            for ch in text[i:i + 8]:
                for flags in (0x0004, 0x0004 | 0x0002):          # KEYEVENTF_UNICODE, then key up
                    ev = INPUT(type=1)
                    ev.ki = KI(0, ord(ch), flags, 0, 0)
                    events.append(ev)
            arr = (INPUT * len(events))(*events)
            _user32().SendInput(len(events), arr, ctypes.sizeof(INPUT))
            time.sleep(0.02)

    def press(self, hwnd: int | None, key: str) -> None:
        vk = KEYS.get(key.lower())
        if vk is None:
            raise DesktopError(f"I don't know the {key} key.")
        if hwnd:
            self.check_allowed(hwnd)
            self.focus(hwnd)
        self._tap(vk)

    @staticmethod
    def _tap(vk: int) -> None:
        flags = 0x0001 if vk in EXTENDED else 0
        _user32().keybd_event(vk, 0, flags, 0)
        _user32().keybd_event(vk, 0, flags | 0x0002, 0)

    def media_key(self, action: str) -> None:
        """The keyboard's media keys: whatever app is playing (Spotify, a browser, VLC…) reacts."""
        self._tap(MEDIA_KEYS[action])

    def scroll(self, hwnd: int, direction: str) -> None:
        self.check_allowed(hwnd)
        self.focus(hwnd)
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
