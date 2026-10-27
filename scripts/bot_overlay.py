"""A tiny spider-hero mask (red ball, black web, big white lenses) that floats on the desktop while JARVIS Zero is running.

It sits on top of other windows, hovers gently over its own little shadow, and shows what JARVIS is doing:
idle (slow blinks, glances), listening (lenses widen, a glow ring pulses), thinking (squint, dots orbit),
speaking (lenses flex). It hides itself when JARVIS isn't running and comes back when it starts.

Chat: click spidey once to open a little text bar. Type anything ("open youtube", "what's 15% of 2400", "hey how's
it going") and press Enter; JARVIS handles it exactly like the dashboard's text box, and spidey shows the reply in a
speech bubble. Esc or another click closes the bar.

Reminders: when one of your JARVIS reminders comes due ("remind me to drink water at 5"), it hops, shows a
speech bubble with the reminder and gives a soft chime, even if the dashboard is closed. Click it or the bubble
(or "Got it") to dismiss.

Quick actions: hover over it for a moment and four buttons pop out above its head: talk (like the hotkey), set a
timer, stop talking, open the dashboard.

Idle life: its eyes follow your mouse, and it flinches if you swipe at it fast. When nothing's going on it strolls a
little way, web-swings to a new spot or leaps up onto the top edge of the window you're using (riding along if you
move that window), then comes home. It dozes off when you've been away from the mouse and keyboard for 2 minutes, and
is happy to see you when you're back. "Wander around" in the right-click menu turns the roaming off.

Speech bubble: replies type themselves out as they arrive; long ones scroll with the mouse wheel; links in them open
through JARVIS (so its site blocklist checks them); Copy copies the reply. When JARVIS asks "shall I…?", the bubble
gets Yes / No (or the choices) as buttons.

Status pill (under spidey, only when there's something to show): listening / thinking / speaking, cloud or
on-this-laptop brain, a countdown to a timer or reminder due within the hour, and the song playing in the Spotify app
or JARVIS's Edge, with play/pause and next (the keyboard's media keys).

Drag and drop: drop a link on it (open it, or open and sum it up), a file (open it, show it in its folder, or for a
text file sum it up; programs, scripts and shortcuts are never opened), or some text (it goes into the chat bar).
A picture file can also be shown on the picture board.

Picture board (scripts/jarvis_board.py): "show me a picture of a red panda" (from Wikipedia), "show my photo called
goa", or "draw a rocket" puts it in a small window beside spidey, without taking the keyboard from what you're doing.
Flip back through the last few, save a copy to Downloads, or close it ("close the picture"). Right-click → Picture board.

Looks and tricks: right-click → Costume (Classic, Black suit, Miles, Iron Spider) and Tricks (shoot a web at the
mouse, hang upside down from the top of the screen). It sometimes does these by itself, and late at night (11 pm to
6 am) it's calmer, yawnier and dozes off sooner.

Drag it anywhere (that's its new home); flick it and it slides off, bouncing off the screen edges. Double-click opens
the JARVIS dashboard. Right-click for a menu.
Run:  .venv\\Scripts\\pythonw.exe scripts\\bot_overlay.py      (launch.ps1 and start-jarvis.bat do this for you)
Talks only to JARVIS on this laptop (127.0.0.1:8600): it listens for JARVIS's status and replies, and sends something
only when you type into the bar, press a bubble button or drop something on it. Reads data/reminders.json (never
changes it) and the Spotify window's title.
"""
from __future__ import annotations

import ctypes
import json
import math
import os
import queue
import re
import random
import sys
import threading
import time
import tkinter as tk
import urllib.request
import webbrowser
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
URL = os.environ.get("JARVIS_OVERLAY_URL", "http://127.0.0.1:8600")
DATA = Path(os.environ.get("JARVIS_DATA_DIR", ROOT / "data"))
POS_FILE = DATA / "overlay.json"
REMINDERS_FILE = DATA / "reminders.json"     # written by JARVIS; read here only

EGG_W, EGG_K = 1.0, 0.0    # shape: 1.0, 0.0 is a sphere (an egg was e.g. 0.80, 0.16)
SIZE = 44                  # sphere diameter in pixels at 100% scaling (window is a little bigger for the shadow)
KEY = "#010203"            # pixels of this colour become see-through
DEEP = (78, 4, 12)         # mask shading: core shadow → base red → lit red
MID = (205, 22, 32)
LIGHT = (255, 92, 84)
WEB = (14, 3, 5)           # web lines
RIM = (22, 2, 6)           # bold outline
EYE = "#111111"            # thick black frame around each lens
LENS = "#f4f8ff"           # white lenses
GLOW = "#9fe9ff"
ALERT = "#ffd166"          # ring colour while a reminder is showing
BUBBLE = "#fffaf0"
BUBBLE_EDGE = "#1a1a1a"
BUBBLE_INK = "#1a1a1a"
ACCENT = "#c0141f"         # spidey red for buttons and the bar's send arrow
HINT = "#8a8378"
SHADOW = "#1f2633"
BLUSH = "#ff6f9c"          # cheeks when happy or petted
HEART = "#ff4f86"
MOUTH = "#080808"          # black mouth
TONGUE = "#ff2e42"         # bright red tongue
SPARKLE = "#fff1a8"
ZZZ = "#bcd7ff"            # sleepy z's
THREAD = "#e8eef8"         # the web line spidey swings from
WEB_EDGE = "#3a4252"       # dark edge round the web it shoots, so it shows on white pages too
CONFETTI = ["#ff4f86", "#ffd166", "#7bdff2", "#b2f7a1", "#ffffff", "#c9a7ff"]

# Costumes (right-click → Costume): mask shading deep → mid → light, web lines, outline, and a mouth that shows up on it.
COSTUMES = {
    "classic": {"label": "Classic", "deep": DEEP, "mid": MID, "light": LIGHT, "web": WEB, "rim": RIM, "mouth": MOUTH},
    "black": {"label": "Black suit", "deep": (4, 4, 8), "mid": (30, 32, 40), "light": (92, 96, 112), "web": (52, 54, 64),
              "rim": (0, 0, 2), "mouth": "#e9edf5"},
    "miles": {"label": "Miles", "deep": (4, 4, 7), "mid": (28, 28, 34), "light": (84, 84, 96), "web": (214, 20, 32),
              "rim": (0, 0, 2), "mouth": "#e9edf5"},
    "iron": {"label": "Iron Spider", "deep": (84, 6, 12), "mid": (176, 18, 28), "light": (255, 96, 84),
             "web": (236, 184, 64), "rim": (40, 6, 4), "mouth": MOUTH},
}

ICON_FONT = "Segoe Fluent Icons"   # Windows 11 (Segoe MDL2 Assets on Windows 10 has the same glyphs)
GLYPH = {"mic": "", "think": "", "speak": "", "cloud": "", "local": "",
         "timer": "", "bell": "", "music": "", "play": "", "pause": "", "next": ""}
GLYPH["look"] = chr(0xE7B3)                           # an eye (Segoe MDL2 "View"): JARVIS is reading a window right now
GLYPH["type"] = chr(0xE765)                           # a keyboard: typing mode is on ("type" … "end")
LINK_RE = re.compile(r"https?://[^\s<>\"'()\[\]{}]+")
TEXT_FILES = {".txt", ".md", ".csv", ".log", ".json", ".py", ".html", ".htm", ".xml", ".ini", ".cfg", ".yaml", ".yml",
              ".tex", ".rtf", ".srt"}
# never opened from a drop (programs, scripts, shortcuts); the same list JARVIS's open_file uses
BLOCKED_EXT = {".exe", ".bat", ".cmd", ".ps1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".msi", ".scr", ".com", ".jar",
               ".reg", ".hta", ".cpl", ".lnk", ".url", ".psm1", ".pif"}


def _token() -> str:
    try:
        from jarvis.config import api_token  # reads .env like the server does
        return api_token()
    except Exception:
        return os.environ.get("JARVIS_API_TOKEN", "")


def single_instance() -> bool:
    """Only one floating bot at a time (launch.ps1 may run many times)."""
    try:
        ctypes.windll.kernel32.CreateMutexW(None, False, "JarvisZeroBotOverlay")
        return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS
    except Exception:
        return True


# ---- a few Windows lookups for idle life (each returns None if Windows won't say) ----
class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def idle_seconds() -> float | None:
    """How long since you last touched the mouse or keyboard (anywhere, not just spidey)."""
    class LII(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
    try:
        li = LII(ctypes.sizeof(LII), 0)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li)):
            return None
        ctypes.windll.kernel32.GetTickCount.restype = ctypes.c_uint32
        return ((ctypes.windll.kernel32.GetTickCount() - li.dwTime) & 0xFFFFFFFF) / 1000
    except Exception:
        return None


def work_area(x: float, y: float) -> tuple[int, int, int, int] | None:
    """The usable part (no taskbar) of the monitor nearest to screen point x, y."""
    class MI(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", _RECT), ("rcWork", _RECT), ("dwFlags", ctypes.c_ulong)]
    try:
        u = ctypes.windll.user32
        u.MonitorFromPoint.restype = ctypes.c_void_p
        u.MonitorFromPoint.argtypes = [wintypes.POINT, ctypes.c_uint]
        mon = u.MonitorFromPoint(wintypes.POINT(int(x), int(y)), 2)    # MONITOR_DEFAULTTONEAREST
        mi = MI()
        mi.cbSize = ctypes.sizeof(MI)
        if mon and u.GetMonitorInfoW(ctypes.c_void_p(mon), ctypes.byref(mi)):
            r = mi.rcWork
            return r.left, r.top, r.right, r.bottom
    except Exception:
        pass
    return None


SKIP_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}   # desktop and taskbar


def foreground_window() -> int | None:
    try:
        ctypes.windll.user32.GetForegroundWindow.restype = ctypes.c_void_p
        return ctypes.windll.user32.GetForegroundWindow() or None
    except Exception:
        return None


def window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """Visible edges of a normal, on-screen window of another app; None for maximised, minimised, hidden, closed,
    desktop/taskbar windows and spidey's own."""
    try:
        u, dwm, h = ctypes.windll.user32, ctypes.windll.dwmapi, ctypes.c_void_p(hwnd)
        if not u.IsWindowVisible(h) or u.IsIconic(h) or u.IsZoomed(h):
            return None
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(h, ctypes.byref(pid))
        if pid.value == os.getpid():
            return None
        cls = ctypes.create_unicode_buffer(64)
        u.GetClassNameW(h, cls, 64)
        if cls.value in SKIP_CLASSES:
            return None
        cloaked = ctypes.c_int(0)
        dwm.DwmGetWindowAttribute(h, 14, ctypes.byref(cloaked), 4)              # DWMWA_CLOAKED (hidden UWP)
        if cloaked.value:
            return None
        r = _RECT()
        if dwm.DwmGetWindowAttribute(h, 9, ctypes.byref(r), ctypes.sizeof(r)) != 0:   # DWMWA_EXTENDED_FRAME_BOUNDS
            if not u.GetWindowRect(h, ctypes.byref(r)):
                return None
        return r.left, r.top, r.right, r.bottom
    except Exception:
        return None


_exe_names: dict[int, str] = {}


def _exe_name(pid: int) -> str:
    """Program file name of a process ('spotify.exe'); cached, '' if Windows won't say."""
    if pid not in _exe_names:
        name = ""
        try:
            k = ctypes.windll.kernel32
            k.OpenProcess.restype = ctypes.c_void_p
            h = k.OpenProcess(0x1000, False, pid)                 # PROCESS_QUERY_LIMITED_INFORMATION
            if h:
                buf, n = ctypes.create_unicode_buffer(400), wintypes.DWORD(400)
                if k.QueryFullProcessImageNameW(ctypes.c_void_p(h), 0, buf, ctypes.byref(n)):
                    name = buf.value.rsplit("\\", 1)[-1].lower()
                k.CloseHandle(ctypes.c_void_p(h))
        except Exception:
            pass
        if len(_exe_names) > 500:
            _exe_names.clear()
        _exe_names[pid] = name
    return _exe_names[pid]


def spotify_title() -> str | None:
    """The Spotify app's window title: 'Artist - Song' while it plays, 'Spotify Free' / 'Spotify Premium' when
    paused; None when Spotify isn't running."""
    titles: list[str] = []
    try:
        u = ctypes.windll.user32
        proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def each(hwnd, _):
            n = u.GetWindowTextLengthW(hwnd)
            if n:
                pid = wintypes.DWORD()
                u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if _exe_name(pid.value) == "spotify.exe":
                    buf = ctypes.create_unicode_buffer(n + 1)
                    u.GetWindowTextW(hwnd, buf, n + 1)
                    titles.append(buf.value)
            return True
        u.EnumWindows(proc(each), 0)
    except Exception:
        return None
    song = next((t for t in titles if " - " in t), None)
    return song or next((t for t in titles if t.startswith("Spotify")), None)


def media_key(kind: str) -> None:
    """Press the keyboard's play/pause or next-track key (the app playing music, e.g. Spotify, reacts)."""
    vk = {"playpause": 0xB3, "next": 0xB0}[kind]
    try:
        u = ctypes.windll.user32
        u.keybd_event(vk, 0, 1, 0)                                 # KEYEVENTF_EXTENDEDKEY
        u.keybd_event(vk, 0, 3, 0)                                 # … | KEYEVENTF_KEYUP
    except Exception:
        pass


def click_through(win: tk.Toplevel) -> None:
    """Mouse clicks pass straight through this window to whatever is under it."""
    try:
        win.update_idletasks()                                     # the real top-level window exists only once shown
        u = ctypes.windll.user32
        u.GetAncestor.restype = ctypes.c_void_p
        top = u.GetAncestor(ctypes.c_void_p(win.winfo_id()), 2)    # GA_ROOT
        if top:
            hwnd = ctypes.c_void_p(top)
            u.SetWindowLongW(hwnd, -20, u.GetWindowLongW(hwnd, -20) | 0x20)   # GWL_EXSTYLE |= WS_EX_TRANSPARENT
    except Exception:
        pass


class TargetRing:
    """The "this is where I will click" ring: ONE reusable, borderless, outline-only window. Display only: no fill, no
    buttons, click-through (WS_EX_TRANSPARENT), never takes the focus (WS_EX_NOACTIVATE), hidden from Alt+Tab
    (WS_EX_TOOLWINDOW). It can only be a thin red frame, so it cannot imitate a dialog. The rectangle is checked again here
    (jarvis.highlight.validate_highlight: four numbers, on a screen, under a maximum size, at most 2 s), the label is
    drawn as plain canvas text (30 characters), and the process is per-monitor DPI aware so physical pixels line up."""

    EX_STYLE = 0x20 | 0x08000000 | 0x80          # WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
    PAD = 3

    def __init__(self, root: tk.Tk):
        self.root, self.win, self.canvas, self.after_id, self.styled = root, None, None, None, False

    def _make(self) -> None:
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.attributes("-transparentcolor", KEY)
        win.configure(bg=KEY)
        win.withdraw()
        self.canvas = tk.Canvas(win, bg=KEY, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.win = win
        self._style()                 # BEFORE the first show: a window shown first and styled after has already taken the focus

    def _style(self) -> None:
        try:
            self.win.update_idletasks()
            u = ctypes.windll.user32
            u.GetAncestor.restype = ctypes.c_void_p
            top = u.GetAncestor(ctypes.c_void_p(self.win.winfo_id()), 2)
            if top:
                hwnd = ctypes.c_void_p(top)
                u.SetWindowLongW(hwnd, -20, u.GetWindowLongW(hwnd, -20) | self.EX_STYLE)
                self.styled = True
        except Exception:
            pass

    def show(self, ev: dict) -> bool:
        """Draw the ring for a 'highlight' event; False when the event is not acceptable (nothing is drawn)."""
        try:
            from jarvis.highlight import to_window_geometry, validate_highlight
            v = validate_highlight(ev.get("rect"), ev.get("label"), ev.get("ttl"))
            if not v:
                return False
            if self.win is None:
                self._make()
            x, y, w, h = to_window_geometry(v["rect"], 1.0)
            p = self.PAD
            W, H = w + 2 * p, h + 2 * p
            self.win.geometry(f"{W}x{H}+{x - p}+{y - p}")
            c = self.canvas
            c.delete("all")
            c.create_rectangle(2, 2, W - 2, H - 2, outline="#ff2e42", width=4)          # outline only: no fill at all
            c.create_rectangle(5, 5, W - 5, H - 5, outline="#ffffff", width=1)
            if v["label"]:
                c.create_text(11, 11, anchor="nw", text=v["label"], fill="#000000", font=("Segoe UI", 10, "bold"))
                c.create_text(10, 10, anchor="nw", text=v["label"], fill="#ffffff", font=("Segoe UI", 10, "bold"))
            if not self.styled:
                self._style()
            self.win.deiconify()
            if self.after_id:
                self.root.after_cancel(self.after_id)
            self.after_id = self.root.after(int(v["ttl"] * 1000), self.hide)
            return True
        except Exception:
            return False

    def hide(self) -> None:
        self.after_id = None
        try:
            if self.win is not None:
                self.win.withdraw()
        except Exception:
            pass


def day_part(hour: int | None = None) -> str:
    """'night' (11 pm to 6 am), 'morning' (6 to 12), 'afternoon' or 'evening': spidey's mood follows the clock."""
    h = datetime.now().hour if hour is None else hour
    return "night" if h >= 23 or h < 6 else "morning" if h < 12 else "afternoon" if h < 17 else "evening"


def _ease(u: float) -> float:
    u = min(1.0, max(0.0, u))
    return u * u * (3 - 2 * u)   # smoothstep


GREETINGS = ["Welcome back!", "Oh hey, you're back!", "Missed you!", "There you are!"]


# Reminder texts that start with one of these read as "Time to …!"
VERBS = {"drink", "eat", "take", "call", "go", "check", "stretch", "study", "sleep", "walk", "submit", "send", "pay",
         "buy", "start", "stop", "finish", "join", "leave", "wake", "get", "do", "make", "read", "watch", "water", "feed",
         "clean", "charge", "meet", "pick", "drop", "book", "text", "message", "reply", "turn", "switch", "have", "attend"}


# Things you might say that earn heart eyes.
LOVE_WORDS = re.compile(r"\b(?:thanks?|thank you|thx|love you|love it|cute|awesome|good (?:job|boy|bot)|"
                                      r"you'?re the best|great job|well done|nice one)\b", re.I)


def _mixf(a, b, f):
    return tuple(int(round(x + (y - x) * f)) for x, y in zip(a, b))


class Link(threading.Thread):
    """Live link to JARVIS: its state and what it says arrive over the dashboard's websocket; a once-a-second status
    check covers the moments the socket is down. Runs in the background so the animation never stutters."""

    def __init__(self):
        super().__init__(daemon=True)
        self.state, self.online, self.ws_ok = "idle", False, False
        self.token = _token()
        self.events: queue.Queue = queue.Queue()      # transcripts and send errors, handled on the Tk thread
        self.cloud: bool | None = None                # JARVIS's brain: cloud (True), local only (False), unknown
        self.edge_media: dict = {}                    # what JARVIS's Edge is playing: {"playing", "title"}
        self.spotify: str | None = None               # the Spotify app's window title (see spotify_title)
        self.io_mode = "voice"                        # JARVIS's mode: "chat" (text only) or "voice"
        self.web = False                              # web lookups available (a search key is set)
        self.killed = False                           # the kill switch was pressed: JARVIS acts on nothing until "resume"
        self.kill_chord = ""                          # e.g. "ctrl+alt+backspace" (shown in the panel)
        self.looking_app, self.looking_until = "", 0.0   # JARVIS is reading this window (the "looking at …" pill, 3 s)
        self.screen_reading = True                    # may JARVIS read a window when asked (the menu / panel switch)
        self.typing_where = ""                        # typing mode is on, typing into this app ("" = off)
        self.tv_on = False                            # the mini TV window is showing (the right-click toggle)

    def note_looking(self, app: str, seconds: float = 3.0) -> None:
        self.looking_app, self.looking_until = str(app or "a window")[:30], time.monotonic() + max(0.0, min(seconds, 3.0))

    def run(self):
        threading.Thread(target=self._poll, daemon=True).start()
        threading.Thread(target=self._watch, daemon=True).start()
        while True:
            try:
                from websockets.sync.client import connect
                ws_url = URL.replace("http", "ws", 1) + "/ws" + (f"?token={quote(self.token)}" if self.token else "")
                with connect(ws_url, open_timeout=3, close_timeout=1) as ws:
                    self.ws_ok = self.online = True
                    for raw in ws:
                        ev = json.loads(raw)
                        kind = ev.get("type")
                        if kind in ("level", "state"):
                            self.state = ev.get("state", self.state)
                        elif kind == "hello":
                            self.state = (ev.get("status") or {}).get("state", self.state)
                            self.io_mode = (ev.get("status") or {}).get("io_mode", self.io_mode)
                            self.killed = bool((ev.get("status") or {}).get("killed"))
                        elif kind in ("kill", "resume"):
                            self.killed = kind == "kill"
                            self.events.put(ev)
                        elif kind == "kill_hotkey":
                            self.kill_chord = str(ev.get("combo") or "")
                            self.events.put(ev)
                        elif kind == "typing":
                            self.typing_where = str(ev.get("where") or "a window")[:30] if ev.get("on") else ""
                            self.events.put(ev)
                        elif kind == "looking":
                            self.note_looking(ev.get("app"), float(ev.get("expires_in") or 3.0))
                        elif kind == "highlight":
                            self.events.put({"type": "highlight", "rect": ev.get("rect"), "label": ev.get("label"),
                                             "ttl": ev.get("ttl")})
                        elif kind == "io_mode":
                            self.io_mode = ev.get("mode", self.io_mode)
                            self.events.put(ev)
                        elif kind in ("transcript", "confirm", "open_chat", "show_panel", "conversation", "board",
                                      "board_close"):
                            self.events.put(ev)
                        elif kind == "tv":
                            self.tv_on = bool(ev.get("on"))
                            self.events.put(ev)
            except Exception:
                pass
            self.ws_ok = False
            time.sleep(2.0)

    def _poll(self):
        while True:
            if not self.ws_ok:
                try:
                    req = urllib.request.Request(f"{URL}/api/status", headers=self._headers())
                    with urllib.request.urlopen(req, timeout=1.5) as r:
                        self.state = json.loads(r.read()).get("engine", {}).get("state", "idle")
                        self.online = True
                except Exception:
                    self.online = False
            time.sleep(1.0)

    def _watch(self):
        """Every couple of seconds: cloud or local brain and Edge media (from JARVIS), and the Spotify app's title."""
        while True:
            self.spotify = spotify_title()
            if self.online:
                try:
                    req = urllib.request.Request(f"{URL}/api/overlay", headers=self._headers())
                    with urllib.request.urlopen(req, timeout=3) as r:
                        info = json.loads(r.read())
                    self.cloud, self.edge_media = bool(info.get("cloud")), info.get("media") or {}
                    self.io_mode = info.get("io_mode", self.io_mode)
                    self.web = bool(info.get("web"))
                    self.killed = bool(info.get("killed", self.killed))
                    self.kill_chord = str(info.get("kill_hotkey") or self.kill_chord)
                    self.screen_reading = bool(info.get("screen_reading", self.screen_reading))
                    self.typing_where = str(info.get("typing", self.typing_where) or "")[:30]
                    tv = info.get("tv")
                    self.tv_on = bool(tv.get("on")) if isinstance(tv, dict) else False
                    look = info.get("looking")
                    if isinstance(look, dict) and look.get("app"):
                        self.note_looking(look["app"], float(look.get("expires_in") or 0.0))
                except Exception:
                    pass                              # an older JARVIS without /api/overlay: the pill just shows less
            time.sleep(2.0)

    def _headers(self) -> dict:
        return {"x-jarvis-token": self.token} if self.token else {}

    def post(self, path: str, body: dict | None = None, method: str = "POST") -> None:
        """POST to JARVIS in the background. Errors come back as a send_error event."""
        def go():
            try:
                req = urllib.request.Request(f"{URL}{path}", data=json.dumps(body or {}).encode(),
                                             headers={**self._headers(), "Content-Type": "application/json"}, method=method)
                urllib.request.urlopen(req, timeout=5).read()
            except Exception:
                self.events.put({"type": "send_error"})
        threading.Thread(target=go, daemon=True).start()

    def send(self, text: str) -> None:
        """Typed message → JARVIS (same as the dashboard's text box)."""
        self.post("/api/text", {"text": text})


class Bot:
    def __init__(self, root: tk.Tk, test_seconds: float = 0, force_state: str = ""):
        self.root = root
        try:
            self.s = max(1.0, root.winfo_fpixels("1i") / 96)  # crisp on high-DPI screens
        except Exception:
            self.s = 1.0
        self.d = int(SIZE * self.s)
        self.dw = math.ceil(self.d * EGG_W * 1.03)                       # egg image width (it's taller than wide)
        self.head_room = int(22 * self.s)                                  # space above for hearts, sparkles, z's
        self.w, self.h = self.dw + int(24 * self.s), self.d + int(34 * self.s) + self.head_room
        self.force_state = force_state
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-transparentcolor", KEY)
        root.configure(bg=KEY)
        self.c = tk.Canvas(root, width=self.w, height=self.h, bg=KEY, highlightthickness=0, bd=0)
        self.c.pack()
        self.wander = tk.BooleanVar(root, value=True)          # strolls, swings and perches on its own
        self.costume = tk.StringVar(root, value="classic")      # right-click → Costume
        self.rctrl = tk.BooleanVar(root, value=True)            # tap Right Ctrl on its own: open/close the chat bar
        self.panel = None                                       # JARVIS's small panel (jarvis_panel.Panel)
        self.board = None                                       # the picture board (jarvis_board.Board), made on first use
        self.target_ring = TargetRing(root)                     # the "where I will click" outline (made on first use)
        self.x, self.y = self._load_pos()
        self.home = (self.x, self.y)                            # where you put it; wandering always comes back here
        root.geometry(f"{self.w}x{self.h}+{self.x}+{self.y}")
        self.t0 = time.monotonic()
        self.status = Link()
        self.status.start()
        # Keyboard shortcut for the chat bar, so you don't have to find spidey with the mouse. Rarely-used combos with
        # a number; the first one no other app has taken wins.
        self.hotkey_q: queue.Queue = queue.Queue()
        self.council_seen: set | None = None            # council decision files already announced
        self.chat_hotkey = None
        try:
            from jarvis.hotkey import GlobalHotkey
            self.chat_hotkey = GlobalHotkey(["ctrl+alt+0", "ctrl+alt+9", "ctrl+shift+0"],
                                            lambda: self.hotkey_q.put("chat")).start()
            print(f"chat shortcut: {self.chat_hotkey.active or 'none (' + self.chat_hotkey.error + ')'}", flush=True)
        except Exception as exc:
            print(f"chat shortcut unavailable: {exc!r}", flush=True)
            self.chat_hotkey = None
        self.rctrl_tap = None
        if self.rctrl.get():
            self._start_rctrl()
        self.shown = True
        self.drag = None
        self.click_job = None
        self.c.bind("<ButtonPress-1>", self._press)
        self.c.bind("<B1-Motion>", self._move)
        self.c.bind("<ButtonRelease-1>", self._release)
        self.c.bind("<Double-Button-1>", self._double)
        self.menu = tk.Menu(root, tearoff=0)
        combo = "Right Ctrl" if self.rctrl_tap and self.rctrl_tap.active else \
            self.chat_hotkey.active.title().replace("+", " + ") if self.chat_hotkey and self.chat_hotkey.active else ""
        self.menu.add_command(label="Chat with me" + (f"   ({combo})" if combo else ""), command=self.open_bar)
        self.menu.add_command(label="Open JARVIS panel", command=self.open_panel)
        self.menu.add_command(label="Picture board", command=lambda: self.get_board().open_empty())
        self.tv_var = tk.BooleanVar(root, value=False)
        self.menu.add_checkbutton(label="Mini TV", variable=self.tv_var,
                                  command=lambda: self.status.send("turn on the tv" if self.tv_var.get() else "turn off the tv"))
        tricks = tk.Menu(self.menu, tearoff=0)
        tricks.add_command(label="Shoot a web at the mouse", command=lambda: self.shoot_web())
        tricks.add_command(label="Hang upside down", command=lambda: self.hang())
        tricks.add_command(label="Barrel roll", command=lambda: self.react("spin"))
        tricks.add_command(label="Show all animations", command=self.run_showcase)
        self.menu.add_cascade(label="Tricks", menu=tricks)
        self.mode_var = tk.StringVar(root, value=self.status.io_mode)
        modes = tk.Menu(self.menu, tearoff=0)
        modes.add_radiobutton(label="Chat: I type, JARVIS answers in text (lightest)", value="chat",
                              variable=self.mode_var, command=lambda: self.set_mode("chat"))
        modes.add_radiobutton(label="Voice: I talk, JARVIS talks back", value="voice",
                              variable=self.mode_var, command=lambda: self.set_mode("voice"))
        self.menu.add_cascade(label="Mode", menu=modes)
        self.screen_var = tk.BooleanVar(root, value=self.status.screen_reading)
        self.menu.add_checkbutton(label="Read a window when I ask (stays on this laptop)", variable=self.screen_var,
                                  command=self.toggle_screen_reading)
        self.menu.configure(postcommand=lambda: (self.mode_var.set(self.status.io_mode),
                                                 self.screen_var.set(self.status.screen_reading),
                                                 self.tv_var.set(self.status.tv_on)))
        looks = tk.Menu(self.menu, tearoff=0)
        for key, cos in COSTUMES.items():
            looks.add_radiobutton(label=cos["label"], value=key, variable=self.costume,
                                  command=lambda: self.set_costume(self.costume.get()))
        self.menu.add_cascade(label="Costume", menu=looks)
        self.menu.add_command(label="Full dashboard (browser)", command=self.open_dashboard)
        self.menu.add_command(label="Move back to the corner", command=self._reset_pos)
        self.menu.add_checkbutton(label="Wander around", variable=self.wander, command=self._wander_toggled)
        self.menu.add_checkbutton(label="Tap Right Ctrl to chat", variable=self.rctrl, command=self._rctrl_toggled)
        self.menu.add_separator()
        self.menu.add_command(label="Close the floating bot", command=root.destroy)
        self.c.bind("<Button-3>", lambda e: self.menu.tk_popup(e.x_root, e.y_root))
        self.frame_times: list[float] = []
        self.alert_text, self.alert_since = "", 0.0
        self.bubble = self.bubble_job = None
        self.bubble_left = True
        self.bub: dict | None = None                            # what the bubble shows (text, typing progress, …)
        self.turn_reply: list[str] = []                         # what JARVIS has said this turn (typed or spoken)
        self.pill = None                                        # status pill under spidey
        self.pill_sig, self.pill_next = None, 0.0
        self.last_song, self.song_seen_at = "", -999.0
        self.web = None                                         # the web line it shoots at the mouse
        self.hanging = False
        self.drop_hover = False
        self.mood = day_part()
        self.bar = self.entry = None
        self.chat_active, self.chat_reply, self.chat_sent_at = False, [], 0.0
        self.showcase = False
        self.ring = None                                        # quick-actions ring (hover a moment)
        self.hover_since: float | None = None
        self.fling: dict | None = None                          # thrown: sliding with momentum
        self.drag_samples: list[tuple[float, int, int]] = []
        self.perch: dict | None = None                          # sitting on top of another app's window
        self.next_wander = 40.0
        self.ptr, self.ptr_moved_at, self.ptr_t = (0, 0), -99.0, 0.0
        self.ptr_dist, self.flinch_cool = 1e9, 0.0
        self.away_for, self.present_at, self.last_yawn = 0.0, 0.0, -99.0
        self.seen: set[str] = {r["id"] for r in self._read_reminders() if self._due(r) <= datetime.now()}
        self.next_rem_check = 0.0
        if test_seconds:
            root.after(int(test_seconds * 1000), self._test_done)
        self._build()
        self.can_drop = self._setup_drop()
        self.t_start, self.frame = time.monotonic(), 0
        self.tick()

    # ---- position ----
    def _default_pos(self):
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        return sw - self.w - int(24 * self.s), sh - self.h - int(64 * self.s)   # bottom right, above the taskbar

    def _load_pos(self):
        try:
            p = json.loads(POS_FILE.read_text(encoding="utf-8"))
            self.wander.set(bool(p.get("wander", True)))
            self.rctrl.set(bool(p.get("rctrl", True)))
            if p.get("costume") in COSTUMES:
                self.costume.set(p["costume"])
            x, y = int(p["x"]), int(p["y"])
            if 0 <= x < self.root.winfo_screenwidth() - 20 and 0 <= y < self.root.winfo_screenheight() - 20:
                return x, y
        except Exception:
            pass
        return self._default_pos()

    def _save_pos(self):
        try:
            POS_FILE.parent.mkdir(parents=True, exist_ok=True)
            POS_FILE.write_text(json.dumps({"x": self.home[0], "y": self.home[1], "wander": self.wander.get(),
                                            "costume": self.costume.get(), "rctrl": self.rctrl.get()}), encoding="utf-8")
        except Exception:
            pass

    def _set_home(self):
        """Wherever spidey is now becomes home (after you drag or throw it)."""
        self.home = (self.x, self.y)
        self.perch = None
        self._save_pos()

    def _wander_toggled(self):
        self._save_pos()
        if not self.wander.get():
            self.perch = None
            if (self.x, self.y) != self.home:
                self.travel(*self.home, style="leap")

    def _reset_pos(self):
        self.fling = None
        if self.action and "to" in self.action:
            self.action = None
        self.x, self.y = self._default_pos()
        self.root.geometry(f"+{self.x}+{self.y}")
        self._set_home()
        self._place_popups()

    def _bounds(self) -> tuple[int, int, int, int]:
        """Where the whole window may go: the monitor spidey is on, minus the taskbar."""
        wa = work_area(self.x + self.w / 2, self.y + self.h / 2)
        if not wa:
            wa = (0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight())
        return wa[0], wa[1], wa[2] - self.w, wa[3] - self.h

    def _clamp(self, x: float, y: float) -> tuple[int, int]:
        l, t, r, b = self._bounds()
        return round(min(max(x, l), r)), round(min(max(y, t), b))

    def _goto(self, x: int, y: int):
        if (x, y) != (self.x, self.y):
            self.x, self.y = x, y
            self.root.geometry(f"+{x}+{y}")
            self._place_popups()

    def _press(self, e):
        self.drag = (e.x_root - self.x, e.y_root - self.y)
        self.press_at = (e.x_root, e.y_root)
        self.moved = False
        self.drag_dist = 0.0
        self.fling = None                                         # caught mid-slide
        self.drag_samples = [(time.monotonic(), self.x, self.y)]
        if self.action and "to" in self.action:                   # grabbed while walking somewhere
            self.action = None

    def _move(self, e):
        if self.drag:
            px, py = getattr(self, "press_at", (e.x_root, e.y_root))
            if abs(e.x_root - px) > 4 or abs(e.y_root - py) > 4:
                if not self.moved:
                    self.close_ring()
                    if self.hanging:                              # pulled down off its web
                        self.hanging, self.action = False, None
                self.moved = True
            nx, ny = e.x_root - self.drag[0], e.y_root - self.drag[1]
            self.drag_dist += abs(nx - self.x) + abs(ny - self.y)
            self.drag_samples = (self.drag_samples + [(time.monotonic(), nx, ny)])[-8:]
            self._goto(nx, ny)

    def _release(self, e):
        self.drag = None
        self.poke()
        if self.moved:
            self.moved = False
            # flicked hard: let go mid-motion, so it flies off and slides to a stop
            now = time.monotonic()
            recent = [smp for smp in self.drag_samples if now - smp[0] < 0.1]
            if len(recent) >= 2 and now - recent[-1][0] < 0.05:
                dt = max(0.012, recent[-1][0] - recent[0][0])
                vx, vy = (recent[-1][1] - recent[0][1]) / dt, (recent[-1][2] - recent[0][2]) / dt
                if math.hypot(vx, vy) > 1300 * self.s:
                    self.fling = {"vx": vx, "vy": vy, "fx": float(self.x), "fy": float(self.y), "t": now,
                                  "roll": self.tilt_now, "dist": 0.0, "bounces": 0}
                    return
            self._set_home()
            # dropped: a little squish, or dizzy if it was swung around a lot
            self.react("dizzy" if self.drag_dist > 900 * self.s else "land")
            return
        self._save_pos()
        px, py = getattr(self, "press_at", (e.x_root, e.y_root))
        if abs(e.x_root - px) < 4 and abs(e.y_root - py) < 4:   # a click, not a drag
            if self.hanging:                                     # tapped while hanging: drops back home
                self._end_hang()
                return
            self.react("hop", n=1)
            if self.alert_text:
                self.dismiss()
            else:  # wait a moment: a second click means "open the dashboard" instead
                self.click_job = self.root.after(260, self._single_click)

    def _single_click(self):
        self.click_job = None
        self.close_bar() if self.bar is not None else self.open_bar()

    def _double(self, e):
        if self.click_job:
            self.root.after_cancel(self.click_job)
            self.click_job = None
        self.open_panel()

    def open_dashboard(self):
        """The full dashboard in the browser, with JARVIS's secret token (it refuses requests without it)."""
        token = self.status.token
        webbrowser.open(f"{URL}/?token={quote(token)}" if token else URL)

    def open_panel(self, toggle: bool = False):
        """JARVIS's small panel beside spidey (double-click, the ring's home button, or right-click)."""
        if self.panel is not None and self.panel.alive:
            self.panel.close() if toggle else self.panel.lift()
            return
        from jarvis_panel import Panel
        self.close_bar()
        if not self.shown:
            self.root.deiconify()
            self.shown = True
        self.poke()
        self.react("hop", n=1)
        self.panel = Panel(self.root, self.status, URL, self.s, self._panel_spot, self._soon_reminders,
                           getattr(self, "icon_font", "Segoe MDL2 Assets"))

    def get_board(self):
        """The picture board (pictures and drawings beside spidey). Made once, then shown and hidden."""
        if self.board is None:
            from jarvis_board import Board
            self.board = Board(self.root, self.status, URL, self.s, self._board_spot)
        return self.board

    def _board_spot(self, w: int, h: int) -> tuple[int, int]:
        """Beside spidey like the panel; when the panel is open, beside the panel instead, so neither covers the other."""
        panel = self.panel if self.panel is not None and self.panel.alive else None
        if panel is None:
            return self._panel_spot(w, h)
        s = self.s
        px, py, pw, ph = panel.win.winfo_x(), panel.win.winfo_y(), panel.win.winfo_width(), panel.win.winfo_height()
        wa = work_area(px + pw / 2, py + ph / 2) or (0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight())
        x = px - w - int(6 * s) if px - w - int(6 * s) >= wa[0] else px + pw + int(6 * s)
        y = py + ph - h
        return int(min(max(x, wa[0]), wa[2] - w)), int(min(max(y, wa[1]), wa[3] - h))

    def _panel_spot(self, w: int, h: int) -> tuple[int, int]:
        """Beside spidey, on the side with room, bottoms level, kept on the screen it's on."""
        s = self.s
        wa = work_area(self.x + self.w / 2, self.y + self.h / 2) or \
            (0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight())
        x = self.x - w - int(6 * s) if self.x - w - int(6 * s) >= wa[0] else self.x + self.w + int(6 * s)
        y = self.y + self.h - h
        return int(min(max(x, wa[0]), wa[2] - w)), int(min(max(y, wa[1]), wa[3] - h))

    def _soon_reminders(self) -> list:
        now = datetime.now()
        items = [(self._due(r), str(r.get("text") or "a reminder")) for r in self._read_reminders() if not r.get("done")]
        return sorted((d, t) for d, t in items if d > now)

    def _start_rctrl(self):
        try:
            from jarvis.hotkey import KeyTap
            self.rctrl_tap = KeyTap(lambda: self.hotkey_q.put("chat")).start()
            print(f"right ctrl tap: {'on' if self.rctrl_tap.active else self.rctrl_tap.error}", flush=True)
        except Exception as exc:
            print(f"right ctrl tap unavailable: {exc!r}", flush=True)
            self.rctrl_tap = None

    def _rctrl_toggled(self):
        self._save_pos()
        if self.rctrl.get() and not self.rctrl_tap:
            self._start_rctrl()
        elif not self.rctrl.get() and self.rctrl_tap:
            self.rctrl_tap.stop()                      # no keyboard watching at all while it's off
            self.rctrl_tap = None

    def set_mode(self, mode: str):
        """Right-click → Mode. JARVIS answers with an io_mode event, which updates the menu and shows a bubble."""
        if not self.status.online:
            self.mode_var.set(self.status.io_mode)
            self.show_bubble("I can't reach JARVIS right now. Is it running?", hide_after=6)
            return
        self.status.post("/api/settings", {"changes": {"io_mode": mode}}, method="PATCH")

    def toggle_screen_reading(self):
        """Right-click → Read a window when I ask. It sends the same words you could type, so JARVIS (not this bot)
        changes the setting, and turning it back on goes through JARVIS like any other widening of its powers."""
        want = self.screen_var.get()
        if not self.status.online:
            self.screen_var.set(self.status.screen_reading)
            self.show_bubble("I can't reach JARVIS right now. Is it running?", hide_after=6)
            return
        self.status.send("you can look at the screen again" if want else "stop looking at the screen")

    # ---- reminders ----
    @staticmethod
    def _due(r: dict) -> datetime:
        try:
            return datetime.fromisoformat(r["due"])
        except Exception:
            return datetime.max

    def _read_reminders(self) -> list[dict]:
        try:
            items = json.loads(REMINDERS_FILE.read_text(encoding="utf-8"))
            return [r for r in items if isinstance(r, dict) and "id" in r and "due" in r]
        except Exception:
            return []

    def check_council(self):
        """The upgrade council meets in the background; when a new decision appears, say so (once)."""
        folder = DATA / "council"
        try:
            reports = {p.name: p for p in folder.glob("20??-??-??.md")}
        except OSError:
            return
        if self.council_seen is None:                     # reports that existed before spidey started count as read
            self.council_seen = set(reports)
            return
        for name in sorted(set(reports) - self.council_seen):
            self.council_seen.add(name)
            try:
                ideas = sum(1 for line in reports[name].read_text(encoding="utf-8").splitlines() if re.match(r"^## \d+\. ", line))
            except OSError:
                ideas = 0
            if not self.alert_text:
                self.poke()
                self.react("happy")
                self.show_bubble(f"The council met and picked {ideas} upgrade idea{'s' if ideas != 1 else ''}. "
                                 f"Nothing changes until you approve. It's in data\\council\\{name}.", hide_after=15)

    def check_reminders(self):
        self._council_tick = getattr(self, "_council_tick", 0) + 1
        if self._council_tick % 30 == 1:                  # every ~30 s is plenty
            self.check_council()
            self.mood = day_part()
        now = datetime.now()
        items = self._read_reminders()
        self.upcoming = [r for r in items if not r.get("done") and self._due(r) > now]   # for the pill's countdown
        for r in items:
            if r["id"] in self.seen:
                continue
            due = self._due(r)
            if due <= now:
                self.seen.add(r["id"])
                if (now - due).total_seconds() < 600:     # don't replay ones missed long ago
                    self.notify(str(r.get("text") or "reminder"))

    @staticmethod
    def phrase(text: str) -> str:
        t = text.strip().rstrip(".!")
        if not t:
            return "Time for your reminder!"
        if t.lower().startswith(("your ", "it's ", "time ")):
            return t[0].upper() + t[1:] + "!"
        return f"Time to {t}!" if t.split()[0].lower() in VERBS else f"Reminder: {t}!"

    def notify(self, text: str):
        self.poke()
        self.alert_text = self.phrase(text) if not self.alert_text else self.alert_text + "\n" + self.phrase(text)
        self.alert_since = time.monotonic() - self.t0
        if not self.shown:
            self.root.deiconify()
            self.shown = True
        self.root.attributes("-topmost", True)
        self.show_bubble(self.alert_text, button="Got it")
        try:
            import winsound
            winsound.MessageBeep(0x40)            # the soft Windows "asterisk" chime
        except Exception:
            pass

    def dismiss(self):
        self.alert_text = ""
        self.hide_bubble()
        self.react("celebrate")                                 # reminder done: confetti!

    # ---- chat bar ----
    def open_bar(self, prefill: str = ""):
        if self.bar is not None:
            if prefill:
                self.entry.delete(0, "end")
                self.entry.insert(0, prefill)
            self.entry.focus_force()
            return
        s = self.s
        b = tk.Toplevel(self.root)
        b.overrideredirect(True)
        b.attributes("-topmost", True)
        b.attributes("-transparentcolor", KEY)
        b.configure(bg=KEY)
        bw, bh = int(230 * s), int(34 * s)
        c = tk.Canvas(b, width=bw, height=bh, bg=KEY, highlightthickness=0, bd=0)
        c.pack()
        r = bh // 2 - 1
        c.create_polygon([r, 1, bw - r, 1, bw - 1, 1, bw - 1, bh - 2, bw - r, bh - 2, r, bh - 2, 1, bh - 2, 1, 1, r, 1],
                         smooth=True, fill=BUBBLE, outline=BUBBLE_EDGE, width=2)
        send_x = bw - int(17 * s)
        c.create_oval(send_x - int(11 * s), bh // 2 - int(11 * s), send_x + int(11 * s), bh // 2 + int(11 * s),
                      fill=ACCENT, outline="", tags="send")
        c.create_text(send_x + 1, bh // 2, text="➤", fill="white", font=("Segoe UI Symbol", 10), tags="send")
        c.tag_bind("send", "<Button-1>", lambda e: self._send())
        entry = tk.Entry(b, relief="flat", bd=0, bg=BUBBLE, fg=BUBBLE_INK, insertbackground=ACCENT,
                         font=("Segoe UI", 10), highlightthickness=0)
        c.create_window(int(14 * s), bh // 2, window=entry, anchor="w", width=send_x - int(14 * s) - int(16 * s))
        typing = self.status.typing_where
        hint = c.create_text(int(16 * s), bh // 2, text=f"Typing into {typing}… “end” stops" if typing else "Talk to me…",
                             anchor="w", fill=HINT, font=("Segoe UI", 10))
        c.tag_lower(hint)
        entry.bind("<Return>", lambda e: self._send())
        entry.bind("<Escape>", lambda e: self.close_bar())
        entry.bind("<KeyRelease>", lambda e: entry.configure(bg=BUBBLE))
        self._hint, self._hint_canvas = hint, c
        entry.bind("<FocusIn>", lambda e: c.itemconfigure(hint, state="hidden"))
        entry.bind("<FocusOut>", lambda e: c.itemconfigure(hint, state="normal" if not entry.get() else "hidden"))
        self.bar, self.entry, self.bar_size = b, entry, (bw, bh)
        if prefill:                                             # e.g. "Set a timer for " from the quick actions
            entry.insert(0, prefill)
            c.itemconfigure(hint, state="hidden")
        self._place_popups()
        b.after(50, lambda: (b.focus_force(), entry.focus_set()))

    def close_bar(self):
        if self.bar is not None:
            self.bar.destroy()
        self.bar = self.entry = None
        self._place_popups()

    def _send(self):
        if self.entry is None:
            return
        text = self.entry.get().strip()
        if not text:
            return
        self.entry.delete(0, "end")
        if not self.status.online:
            self.show_bubble("I can't reach JARVIS right now. Is it running?", hide_after=6)
            self.react("shake")
            return
        self.react("nod")                                       # got it
        if LOVE_WORDS.search(text):
            self.root.after(900, lambda: self.react("love"))     # "thanks spidey!" → heart eyes
        self._ask(text)

    def _ask(self, text: str, confirm: bool = False):
        """Send something to JARVIS for you (typed, a bubble button, a dropped link) and show its reply."""
        if not self.status.online:
            self.show_bubble("I can't reach JARVIS right now. Is it running?", hide_after=6)
            self.react("shake")
            return
        self.chat_active, self.chat_reply, self.chat_sent_at = True, [], time.monotonic()
        self.poke()
        self.show_bubble("…", hide_after=0)
        if confirm:
            self.status.post(f"/api/confirm/{text}")
        else:
            self.status.send(text)

    def handle_events(self):
        while True:
            try:
                ev = self.status.events.get_nowait()
            except queue.Empty:
                return
            if ev.get("type") == "highlight":                   # display only: a thin ring where JARVIS will click
                self.target_ring.show(ev)
                continue
            if ev.get("type") == "board":                       # a picture or a drawing for the picture board
                self.get_board().on_event(ev)
                if ev.get("kind") in ("image", "drawing"):
                    self.poke()
                    self.react("happy" if ev.get("kind") == "drawing" else "nod")
                continue
            if ev.get("type") == "board_close":
                if self.board is not None:
                    self.board.hide()
                continue
            if ev.get("type") == "tv":                          # the mini TV came on (it's JARVIS's own window)
                if ev.get("on"):
                    self.poke()
                    self.react("nod")
                continue
            panel = self.panel if self.panel is not None and self.panel.alive else None
            if panel:
                panel.on_event(ev)                              # the panel shows the whole conversation
            if ev.get("type") == "show_panel":                  # launch.ps1 / the start shortcut
                self.open_panel()
            elif panel and ev.get("type") == "transcript" and ev.get("role") == "assistant":
                self.chat_active = False                        # the reply is in the panel: no bubble on top
                self.turn_reply = (self.turn_reply + [str(ev.get("text", ""))])[-12:]
            elif ev.get("type") == "kill":                      # the kill chord: red pill until "resume"
                self.poke()
                self.react("shake")
                self.show_bubble("Stopped. I won't touch anything until you say resume.", hide_after=6)
            elif ev.get("type") == "resume":
                self.poke()
                self.react("nod")
                self.show_bubble("Okay, I'm back.", hide_after=3)
            elif ev.get("type") == "send_error":
                self.chat_active = False
                self.show_bubble("Hmm, that didn't reach JARVIS. Try again in a moment?", hide_after=6)
                self.react("shake")
            elif ev.get("type") == "open_chat":                 # talk shortcut in chat mode: type instead
                if panel:
                    panel.lift()
                elif self.bar is None:
                    self.poke()
                    self.react("hop", n=1)
                    self.open_bar()
            elif ev.get("type") == "typing":                    # typing mode on/off ("type" … "end")
                on, where = bool(ev.get("on")), str(ev.get("where") or "a window")[:30]
                if on and not int(ev.get("count") or 0):        # just switched on (not each piece typed)
                    if panel:
                        panel.on_event({"type": "note", "text": f"Typing mode: everything goes into {where} until “end”"})
                    elif self.status.io_mode == "chat" and self.bar is None:
                        self.open_bar()                          # chat mode: type the pieces here
                    self.poke()
                    self.react("nod")
                elif not on and panel:
                    panel.on_event({"type": "note", "text": "Typing mode off"})
                if self.bar is not None and self._hint_canvas is not None:
                    self._hint_canvas.itemconfigure(self._hint, text=f"Typing into {where}… “end” stops" if on
                                                    else "Talk to me…")
            elif ev.get("type") == "io_mode":
                self.mode_var.set(ev.get("mode", "voice"))
                if panel:
                    panel.on_event({"type": "note", "text": "Chat mode: answers in text, no voice" if ev.get("mode") == "chat"
                                    else "Voice mode: JARVIS talks again"})
                elif not self.chat_active:
                    self.poke()
                    self.react("nod")
                    self.show_bubble("Chat mode: I'll answer here, quietly." if ev.get("mode") == "chat" else
                                     "Voice mode: I'll talk out loud again.", hide_after=4)
            elif ev.get("type") == "transcript" and ev.get("role") == "assistant" and ev.get("text"):
                self.turn_reply = (self.turn_reply + [str(ev["text"])])[-12:]
                # Replies to a typed message always show; with the bar open, spoken replies show too. In chat mode
                # nothing is spoken, so everything shows (reminders, summed-up files…).
                if self.chat_active or self.bar is not None or self.status.io_mode == "chat":
                    if not self.chat_reply and not self.alert_text and not (self.action and self.action["name"] == "love"):
                        self.react("happy")                     # a reply! hearts
                    self.chat_reply.append(str(ev["text"]))
                    if not self.alert_text:
                        reply = " ".join(self.chat_reply[-12:])
                        self.show_bubble(reply, hide_after=max(8, min(25, len(reply) // 12)), typed=True, copy=True)
            elif ev.get("type") == "transcript" and ev.get("role") == "user":
                self.turn_reply = []
                if not self.chat_active:
                    self.chat_reply = []      # a new spoken turn starts a fresh bubble
                    if LOVE_WORDS.search(str(ev.get("text", ""))):
                        self.react("love")    # said "thank you" out loud → heart eyes
            elif ev.get("type") == "confirm" and not self.alert_text:
                # JARVIS asked "shall I…?": answer with a button (typing or saying it still works too)
                if ev.get("prompt"):
                    names = [str(n) for n in (ev.get("choices") or [])][:4]
                    choices = [(n, lambda n=n: self._ask(n)) for n in names] if names else \
                        [("Yes", lambda: self._ask("yes", confirm=True)), ("No", lambda: self._ask("no", confirm=True))]
                    said = " ".join(self.turn_reply) or str(ev["prompt"])
                    self.poke()
                    self.react("tilt")
                    self.show_bubble(said, choices=choices, typed=True, copy=True)
                elif self.bub and self.bub.get("choices") and not self.chat_active:
                    self.hide_bubble()

    # ---- speech bubble ----
    TYPE_CPS = 80                                               # how fast replies type themselves out (characters/s)

    @staticmethod
    def _pill_pts(x0: float, y0: float, x1: float, y1: float) -> list:
        """A rounded pill (draw with smooth=True)."""
        r = (y1 - y0) / 2
        return [x0 + r, y0, x1 - r, y0, x1, y0, x1, y1, x1 - r, y1, x0 + r, y1, x0, y1, x0, y0]

    def show_bubble(self, text: str, button: str | None = None, hide_after: float = 0, choices: list | None = None,
                    typed: bool = False, copy: bool = False):
        """Speech bubble beside spidey. typed: the text types itself out, and a longer version of the text already
        showing carries on where it got to, so a reply streams in sentence by sentence. Long text scrolls with the
        mouse wheel, links in it are clickable, choices are buttons [(label, fn)], copy adds a Copy button and button
        is the reminder's 'Got it'. hide_after counts from when the typing ends, and waits while the mouse is on it."""
        s, prev = self.s, self.bub
        shown = len(text)
        if typed:
            done = prev["text"][:prev["shown"]] if prev and prev["typed"] and self.bubble is not None else ""
            shown = len(done) if done and text.startswith(done) else 0
        if self.bubble_job:
            self.root.after_cancel(self.bubble_job)
            self.bubble_job = None
        if self.bubble is None:
            b = tk.Toplevel(self.root)
            b.overrideredirect(True)
            b.attributes("-topmost", True)
            b.attributes("-transparentcolor", KEY)
            b.configure(bg=KEY)
            self.bubble = b
        else:                                                   # same window: no flicker while a reply streams in
            b = self.bubble
            for child in b.winfo_children():
                child.destroy()
        alert = button is not None
        font_main = ("Segoe UI Semibold", 11) if alert else ("Segoe UI", 10)
        c = tk.Canvas(b, bg=KEY, highlightthickness=0, bd=0)
        c.pack()
        pad, wrap = int(12 * s), int((190 if alert else 220) * s)
        shape = c.create_polygon(0, 0, 0, 0, 0, 0, smooth=True, fill=BUBBLE, outline=BUBBLE_EDGE, width=2)
        tail_poly = c.create_polygon(0, 0, 0, 0, 0, 0, fill=BUBBLE, outline=BUBBLE_EDGE, width=2)
        tail_line = c.create_line(0, 0, 0, 0, fill=BUBBLE, width=3)
        probe = c.create_text(0, 0, text=text, anchor="nw", width=wrap, font=font_main)
        x0, y0, x1, y1 = c.bbox(probe)
        c.delete(probe)
        tw, th = max(1, x1 - x0), max(1, y1 - y0)
        vh = min(th, int(150 * s))                              # taller than this scrolls
        scroll = th > vh
        # the text sits in its own little canvas so it can scroll inside the bubble
        inner = tk.Canvas(c, width=tw, height=vh, bg=BUBBLE, highlightthickness=0, bd=0,
                          yscrollincrement=max(1, int(6 * s)))
        txt = inner.create_text(0, 0, text=text[:shown], anchor="nw", width=wrap, font=font_main, fill=BUBBLE_INK)
        c.create_window(pad, pad, window=inner, anchor="nw", tags="content")
        thumb = c.create_line(0, 0, 0, 0, fill=HINT, width=max(2, round(3 * s)), capstyle="round", state="hidden")
        w, y = pad + tw + (int(9 * s) if scroll else 0) + pad, pad + vh

        def hand(tag):                                          # pointer turns into a hand over buttons and links
            c.tag_bind(tag, "<Enter>", lambda e: c.configure(cursor="hand2"))
            c.tag_bind(tag, "<Leave>", lambda e: c.configure(cursor=""))

        for i, u in enumerate(list(dict.fromkeys(u.rstrip(".,;:!?") for u in LINK_RE.findall(text)))[:3]):
            short = re.sub(r"^https?://(?:www\.)?", "", u)
            short = short if len(short) <= 34 else short[:33] + "…"
            item = c.create_text(pad, y + int(6 * s), text="↗ " + short, anchor="nw", font=("Segoe UI", 9, "underline"),
                                 fill=ACCENT, tags=("content", "btn", f"link{i}"))
            c.tag_bind(f"link{i}", "<Button-1>", lambda e, u=u: self._open_link(u))
            hand(f"link{i}")
            bx0, by0, bx1, by1 = c.bbox(item)
            w, y = max(w, bx1 + pad), by1
        if choices:                                             # answer buttons; the first one is filled in
            x, row_y, row_h = pad, y + int(9 * s), 0
            for i, (label, fn) in enumerate(choices):
                tag = f"ch{i}"
                bg = c.create_polygon(0, 0, 0, 0, 0, 0, smooth=True, fill=ACCENT if i == 0 else BUBBLE, outline=ACCENT,
                                      width=2, tags=("content", "btn", tag))
                t = c.create_text(0, 0, text=label, anchor="nw", font=("Segoe UI Semibold", 9),
                                  fill=BUBBLE if i == 0 else ACCENT, tags=("content", "btn", tag))
                tx0, ty0, tx1, ty1 = c.bbox(t)
                bw_, bh_ = tx1 - tx0 + int(18 * s), ty1 - ty0 + int(6 * s)
                if x > pad and x + bw_ > pad + max(tw, wrap):  # no room left on this row
                    x, row_y = pad, row_y + bh_ + int(6 * s)
                c.coords(t, x + int(9 * s), row_y + int(3 * s))
                c.coords(bg, *self._pill_pts(x, row_y, x + bw_, row_y + bh_))
                c.tag_bind(tag, "<Button-1>", lambda e, fn=fn: (self.hide_bubble(), fn()))
                hand(tag)
                w, x, row_h = max(w, x + bw_ + pad), x + bw_ + int(6 * s), bh_
            y = row_y + row_h
        foot = ([(button, self.dismiss)] if button else []) + ([("Copy", None)] if copy else [])
        if foot:
            x, fy = pad, y + int(8 * s)
            for i, (label, fn) in enumerate(foot):
                tag = f"foot{i}"
                item = c.create_text(x, fy, text=label, anchor="nw", font=("Segoe UI", 9), fill=ACCENT if fn else HINT,
                                     tags=("content", "btn", tag))
                c.tag_bind(tag, "<Button-1>", (lambda e, fn=fn: fn()) if fn else
                           (lambda e, item=item: self._copy(text, c, item)))
                hand(tag)
                bx0, by0, bx1, by1 = c.bbox(item)
                x, w, y = bx1 + int(14 * s), max(w, bx1 + pad), by1
        h, tail = y + pad, int(10 * s)
        self.bubble_left = self.x > int(260 * s)          # bubble on the left of spidey unless it sits at the far left
        ox = 0 if self.bubble_left else tail                # leave room for the tail on spidey's side
        r = int(12 * s)
        c.coords(shape, ox + r, 1, ox + w - r, 1, ox + w, 1, ox + w, r, ox + w, h - r, ox + w, h, ox + w - r, h, ox + r, h,
                 ox, h, ox, h - r, ox, r, ox, 1)
        ty = min(h * 0.55, h - tail)
        if self.bubble_left:   # little tail pointing at spidey
            c.coords(tail_poly, ox + w - 1, ty - tail * 0.6, ox + w + tail, ty, ox + w - 1, ty + tail * 0.6)
            c.coords(tail_line, ox + w - 1, ty - tail * 0.6 + 2, ox + w - 1, ty + tail * 0.6 - 2)
        else:
            c.coords(tail_poly, ox + 1, ty - tail * 0.6, ox - tail, ty, ox + 1, ty + tail * 0.6)
            c.coords(tail_line, ox + 1, ty - tail * 0.6 + 2, ox + 1, ty + tail * 0.6 - 2)
        c.move("content", ox, 0)
        c.configure(width=w + tail + 2, height=h + 2)
        for widget in (c, inner):
            widget.bind("<MouseWheel>", self._bubble_wheel)
            widget.bind("<Button-1>", self._bubble_click)
        self.bub = {"text": text, "shown": shown, "typed": typed, "inner": inner, "txt": txt, "thumb": thumb, "c": c,
                    "scroll": scroll, "vh": vh, "tw": tw, "pad": pad, "ox": ox, "hide_after": hide_after,
                    "follow": True, "acc": 0.0, "choices": choices, "scheduled": False}
        self.bubble_size = (w + tail + 2, h + 2)
        self._place_popups()
        self._bubble_progress()

    def _bubble_progress(self):
        """After more text has typed itself out: keep the newest line in view, and start the hide timer once done."""
        b = self.bub
        inner = b["inner"]
        box = inner.bbox(b["txt"])
        inner.configure(scrollregion=(0, 0, b["tw"], max(b["vh"], box[3] if box else 0)))
        if b["follow"]:
            inner.yview_moveto(1.0)
        self._bubble_thumb()
        if b["shown"] >= len(b["text"]) and not b["scheduled"]:
            b["scheduled"] = True
            if b["hide_after"]:
                self.bubble_job = self.root.after(int(b["hide_after"] * 1000), self._bubble_autohide)

    def _type_step(self, dt: float):
        b = self.bub
        if not b or self.bubble is None or b["shown"] >= len(b["text"]):
            return
        b["acc"] += self.TYPE_CPS * dt
        n = int(b["acc"])
        if n:
            b["acc"] -= n
            b["shown"] = min(len(b["text"]), b["shown"] + n)
            b["inner"].itemconfigure(b["txt"], text=b["text"][:b["shown"]])
            self._bubble_progress()

    def _bubble_thumb(self):
        """The little scroll bar on the right of a long bubble."""
        b, s = self.bub, self.s
        if not b["scroll"]:
            return
        top, bot = b["inner"].yview()
        if bot - top >= 0.999:
            b["c"].itemconfigure(b["thumb"], state="hidden")
            return
        x = b["ox"] + b["pad"] + b["tw"] + round(5 * s)
        y0 = b["pad"] + b["vh"] * top
        b["c"].coords(b["thumb"], x, round(y0 + 2), x, round(max(y0 + 6 * s, b["pad"] + b["vh"] * bot - 2)))
        b["c"].itemconfigure(b["thumb"], state="normal")

    def _bubble_wheel(self, e):
        b = self.bub
        if not b or not b["scroll"]:
            return
        b["inner"].yview_scroll(-3 if e.delta > 0 else 3, "units")
        b["follow"] = b["inner"].yview()[1] >= 0.999           # scrolled back up to re-read: stop following
        self._bubble_thumb()

    def _bubble_click(self, e):
        w = e.widget
        cur = w.find_withtag("current")
        if cur and "btn" in w.gettags(cur[0]):
            return                                              # a button or link handles its own click
        b = self.bub
        if b and b["shown"] < len(b["text"]):                   # still typing: show it all at once
            b["shown"] = len(b["text"])
            b["inner"].itemconfigure(b["txt"], text=b["text"])
            self._bubble_progress()
        elif self.alert_text:
            self.dismiss()
        else:
            self.hide_bubble()

    def _bubble_autohide(self):
        self.bubble_job = None
        if self.bubble is None:
            return
        px, py = self.root.winfo_pointerxy()
        bx, by = self.bubble.winfo_rootx(), self.bubble.winfo_rooty()
        bw, bh = self.bubble_size
        if bx <= px < bx + bw and by <= py < by + bh:          # you're still reading it
            self.bubble_job = self.root.after(1500, self._bubble_autohide)
        else:
            self.hide_bubble()

    def _copy(self, text: str, c: tk.Canvas, item):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            c.itemconfigure(item, text="Copied ✓")
        except tk.TclError:
            pass

    def _open_link(self, url: str):
        """Links open through JARVIS, so its site blocklist checks them first."""
        self._ask(f"open {url}")

    def hide_bubble(self):
        if self.bubble_job:
            self.root.after_cancel(self.bubble_job)
            self.bubble_job = None
        if self.bubble is not None:
            self.bubble.destroy()
            self.bubble = None
        self.bub = None

    def _place_popups(self):
        """Bar beside spidey's lower half, bubble beside its upper half (above the bar when the bar is open)."""
        s = self.s
        left = self.x > int(260 * s)
        bar_y = None
        if self.bar is not None:
            bw, bh = self.bar_size
            bx = self.x - bw - int(2 * s) if left else self.x + self.w + int(2 * s)
            bar_y = self.y + self.h - bh - int(10 * s)
            self.bar.geometry(f"{bw}x{bh}+{bx}+{bar_y}")
        if self.bubble is not None:
            bw, bh = self.bubble_size
            y = self.y + int(self.h * 0.45) - bh // 2
            if bar_y is not None:
                y = min(y, bar_y - bh - int(4 * s))
            x = self.x - bw + int(4 * self.s) if self.bubble_left else self.x + self.w - int(4 * self.s)
            self.bubble.geometry(f"{bw}x{bh}+{x}+{max(0, y)}")
        if self.ring is not None:
            W, H, C, CY = self.ring_geo[:4]
            self.ring.geometry(f"{W}x{H}+{self.x + self.cx - C}+{round(self.y + self._rest_cy()) - CY}")
        if self.pill is not None:                                # status pill just under spidey's shadow
            pw, ph = self.pill_size
            px, py = self.x + self.cx - pw // 2, self.y + self.h - int(3 * s)
            wa = work_area(self.x + self.w / 2, self.y + self.h / 2)
            if wa and py + ph > wa[3]:                           # no room below: above its head instead
                py = self.y + self.top - ph - int(4 * s)
            if wa:
                px = min(max(px, wa[0]), wa[2] - pw)
            self.pill.geometry(f"{pw}x{ph}+{px}+{py}")

    # ---- quick actions: hover over spidey for a moment and four buttons pop out above its head ----
    RING = [("talk", "Talk to JARVIS"), ("timer", "Set a timer"), ("hush", "Stop talking"), ("home", "Open JARVIS panel")]
    RING_ANGLES = (-165, -115, -65, -15)                        # degrees round the head, left to right

    def _rest_cy(self) -> float:
        """Sphere centre in window pixels, halfway through the hover."""
        return self.top + self.r + self.amp / 2

    def open_ring(self, pinned: bool = False):
        """The ring's own window is see-through (and click-through) except the buttons, so spidey stays clickable."""
        if self.ring is not None:
            return
        s = self.s
        br, R = round(11 * s), round(self.r + 19 * s)
        C, L = R + br + round(4 * s), round(22 * s)
        W, H, CY = 2 * C, C + L + round(4 * s), C + L
        b = tk.Toplevel(self.root)
        b.overrideredirect(True)
        b.attributes("-topmost", True)
        b.attributes("-transparentcolor", KEY)
        b.configure(bg=KEY)
        c = tk.Canvas(b, width=W, height=H, bg=KEY, highlightthickness=0, bd=0)
        c.pack()
        self.ring, self.ring_c, self.ring_geo = b, c, (W, H, C, CY, R, br)
        self.ring_born, self.ring_hot, self.ring_far_since, self.ring_pinned = time.monotonic(), -1, 0.0, pinned
        self.ring_out = [0.0] * len(self.RING)                  # how far each button has popped out (0 … 1)
        lw = max(2, round(1.8 * s))
        for i, ang in enumerate(self.RING_ANGLES):
            tag = f"b{i}"
            x, y = C + R * math.cos(math.radians(ang)), CY + R * math.sin(math.radians(ang))
            c.create_oval(x - br, y - br, x + br, y + br, fill=BUBBLE, outline=BUBBLE_EDGE, width=2, tags=(tag, f"bg{i}"))
            self._ring_icon(c, self.RING[i][0], x, y, br, lw, (tag, f"ic{i}"))
            c.tag_bind(tag, "<Enter>", lambda e, i=i: self._ring_hover(i))
            c.tag_bind(tag, "<Leave>", lambda e, i=i: self._ring_hover(-1) if self.ring_hot == i else None)
            c.tag_bind(tag, "<Button-1>", lambda e, i=i: self._ring_do(i))
            c.itemconfigure(tag, state="hidden")
            c.move(tag, (C - x) * 0.45, (CY - y) * 0.45)       # starts tucked in towards the head
        self.ring_pill = c.create_polygon(0, 0, 0, 0, 0, 0, smooth=True, fill=BUBBLE, outline=BUBBLE_EDGE, width=2,
                                          state="hidden")
        self.ring_text = c.create_text(C, L // 2 + 1, text="", fill=BUBBLE_INK, font=("Segoe UI", 9), state="hidden")
        self._place_popups()

    def _ring_icon(self, c: tk.Canvas, kind: str, x: float, y: float, br: int, lw: int, tags):
        k = br / 11
        if kind == "talk":       # microphone
            c.create_line(x, y - 4.5 * k, x, y - 0.5 * k, width=round(5 * k), capstyle="round", fill=ACCENT, tags=tags)
            c.create_arc(x - 5 * k, y - 6 * k, x + 5 * k, y + 2.5 * k, start=200, extent=140, style="arc",
                         outline=ACCENT, width=lw, tags=tags)
            c.create_line(x, y + 2.5 * k, x, y + 5.5 * k, width=lw, fill=ACCENT, tags=tags)
            c.create_line(x - 3 * k, y + 5.5 * k, x + 3 * k, y + 5.5 * k, width=lw, fill=ACCENT, capstyle="round", tags=tags)
        elif kind == "timer":    # stopwatch
            c.create_oval(x - 5.5 * k, y - 4 * k, x + 5.5 * k, y + 7 * k, outline=ACCENT, width=lw, tags=tags)
            c.create_line(x, y + 1.5 * k, x, y - 1.5 * k, width=lw, fill=ACCENT, capstyle="round", tags=tags)
            c.create_line(x, y + 1.5 * k, x + 2.5 * k, y + 2.5 * k, width=lw, fill=ACCENT, capstyle="round", tags=tags)
            c.create_line(x - 2 * k, y - 6.5 * k, x + 2 * k, y - 6.5 * k, width=lw, fill=ACCENT, capstyle="round", tags=tags)
        elif kind == "hush":     # stop square
            c.create_rectangle(x - 4 * k, y - 4 * k, x + 4 * k, y + 4 * k, fill=ACCENT, outline=ACCENT, tags=tags)
        else:                    # house
            c.create_polygon(x - 6.5 * k, y - 0.5 * k, x, y - 6.5 * k, x + 6.5 * k, y - 0.5 * k, fill=ACCENT,
                             outline=ACCENT, tags=tags)
            c.create_rectangle(x - 4.5 * k, y - 1 * k, x + 4.5 * k, y + 6 * k, fill=ACCENT, outline=ACCENT, tags=tags)
            c.create_rectangle(x - 1.3 * k, y + 2 * k, x + 1.3 * k, y + 6 * k, fill=BUBBLE, outline=BUBBLE,
                               tags=(tags[0], tags[1].replace("ic", "door")))

    def _ring_hover(self, i: int):
        """Highlight the button under the mouse and say what it does in a little label above the ring."""
        if self.ring is None:
            return
        c, (W, H, C, CY, R, br) = self.ring_c, self.ring_geo
        for j in range(len(self.RING)):
            hot = j == i
            c.itemconfigure(f"bg{j}", fill=ACCENT if hot else BUBBLE)       # hot: red button, white icon
            c.itemconfigure(f"door{j}", fill=ACCENT if hot else BUBBLE, outline=ACCENT if hot else BUBBLE)
            col = BUBBLE if hot else ACCENT
            for item in c.find_withtag(f"ic{j}"):
                if c.type(item) in ("arc", "oval"):
                    c.itemconfigure(item, outline=col)
                elif c.type(item) == "line":
                    c.itemconfigure(item, fill=col)
                else:
                    c.itemconfigure(item, fill=col, outline=col)
        self.ring_hot = i
        if i < 0:
            c.itemconfigure(self.ring_text, state="hidden")
            c.itemconfigure(self.ring_pill, state="hidden")
            return
        c.itemconfigure(self.ring_text, text=self.RING[i][1], state="normal")
        x0, y0, x1, y1 = c.bbox(self.ring_text)
        p, ry = round(7 * self.s), (y1 - y0) // 2 + 2
        x0, x1 = max(2, x0 - p), min(W - 2, x1 + p)
        y0, y1 = (y0 + y1) // 2 - ry, (y0 + y1) // 2 + ry
        c.coords(self.ring_pill, x0 + ry, y0, x1 - ry, y0, x1, y0, x1, y1, x1 - ry, y1, x0 + ry, y1, x0, y1, x0, y0)
        c.itemconfigure(self.ring_pill, state="normal")
        c.tag_raise(self.ring_text)

    def _ring_do(self, i: int):
        kind = self.RING[i][0]
        self.close_ring()
        self.poke()
        if kind == "home":
            self.open_panel()
            return
        if kind == "timer":
            self.open_bar(prefill="Set a timer for ")
            return
        if not self.status.online:
            self.show_bubble("I can't reach JARVIS right now. Is it running?", hide_after=6)
            self.react("shake")
        elif kind == "talk" and self.status.io_mode == "chat":
            self.open_bar()                                     # chat mode: the mic stays off, so type instead
        elif kind == "talk":
            self.status.post("/api/listen/once")                # same as the talk hotkey: beep, then speak
        else:
            self.status.post("/api/interrupt")
            self.react("nod")

    def close_ring(self):
        if self.ring is not None:
            self.ring.destroy()
        self.ring = None
        self.hover_since = None                                 # needs a fresh hover to come back

    def _update_ring(self):
        """Pop the buttons out one after another, and close the ring once the mouse wanders away from it."""
        if self.ring is None:
            return
        now = time.monotonic()
        c, (W, H, C, CY, R, br) = self.ring_c, self.ring_geo
        for i, ang in enumerate(self.RING_ANGLES):
            want = _ease((now - self.ring_born - i * 0.04) / 0.16)
            if want > self.ring_out[i]:
                if self.ring_out[i] == 0:
                    c.itemconfigure(f"b{i}", state="normal")
                    if self.ring_hot >= 0:
                        c.tag_raise(self.ring_pill)
                        c.tag_raise(self.ring_text)
                dx, dy = -R * math.cos(math.radians(ang)), -R * math.sin(math.radians(ang))
                step = (want - self.ring_out[i]) * 0.45
                c.move(f"b{i}", -dx * step, -dy * step)
                self.ring_out[i] = want
        if self.ring_pinned:
            return
        px, py = self.root.winfo_pointerxy()
        rx, ry = self.x + self.cx, self.y + self._rest_cy()
        if math.hypot(px - rx, py - ry) > R + br + 12 * self.s or py > ry + self.r + 14 * self.s:
            self.ring_far_since = self.ring_far_since or now
            if now - self.ring_far_since > 0.35:
                self.close_ring()
        else:
            self.ring_far_since = 0.0

    # ---- drawing ----
    # The sphere is rendered pixel by pixel into images (real shading, no stacked circles that shimmer). Squashed,
    # stretched and tilted versions are rendered once and cached, a few per frame in the background, so bounces and
    # head tilts cost nothing later. Every frame only moves a fixed set of canvas items by whole pixels, so nothing
    # wobbles against anything else, and the loop runs at about 60 frames a second.
    def _render_sphere(self, sy: float = 1.0, tilt: float = 0.0) -> tk.PhotoImage:
        """A red spider-hero mask on a solid-looking ball: strong light and shadow, a bounce light underneath,
        a glossy hotspot plus a soft sheen, a bold dark outline, and a black web (12 spokes, sagging rings).
        sy squashes (< 1) or stretches (> 1) it, keeping its volume; tilt (degrees) turns the mask like a head tilt.
        (EGG_W/EGG_K can stretch it into an egg: half-width at height y is EGG_W * (1 + EGG_K * y) * sqrt(1 - y²).)"""
        sx = 1 / math.sqrt(sy)
        r = self.d / 2
        W, H = max(2, round(self.dw * sx)), max(2, round(self.d * sy))
        img = tk.PhotoImage(width=W, height=H)
        deep, mid, light, web, rim = (self.skin[k] for k in ("deep", "mid", "light", "web", "rim"))   # the costume
        lx, ly, lz = -0.45, -0.60, 0.66                       # light from the top left, towards the viewer
        n = math.sqrt(lx * lx + ly * ly + lz * lz)
        lx, ly, lz = lx / n, ly / n, lz / n
        hx, hy, hz = lx, ly, lz + 1                            # half vector for the specular highlight
        n = math.sqrt(hx * hx + hy * hy + hz * hz)
        hx, hy, hz = hx / n, hy / n, hz / n
        line = max(0.035, 1.1 / r)                             # web line thickness, about one pixel or a bit more
        ca, sa = math.cos(math.radians(-tilt)), math.sin(math.radians(-tilt))
        outline = 1.6 / (r * min(sx, sy))
        rows = []
        for py in range(H):
            row = []
            for px in range(W):
                y = (py + 0.5 - H / 2) / (r * sy)
                x = (px + 0.5 - W / 2) / (r * sx) / (EGG_W * (1 + EGG_K * y))   # map back onto a unit ball
                q = x * x + y * y
                if q > 1:
                    row.append(KEY)
                    continue
                z = math.sqrt(1 - q)
                diff = max(0.0, x * lx + y * ly + z * lz)
                shade = diff ** 0.8
                col = _mixf(deep, mid, min(1.0, shade * 1.5))                # clear core shadow on the lower right
                col = _mixf(col, light, max(0.0, shade - 0.6) / 0.4 * 0.55)
                bounce = max(0.0, y * 0.8 + x * 0.3) * (1 - z) * 0.9           # light bouncing up from below
                col = _mixf(col, (150, 30, 40), min(0.35, bounce))
                # web centred a little below the middle, between the eyes (like a nose bridge); turns with the tilt
                ux, uy = x * ca - y * sa, x * sa + y * ca
                wx, wy = ux, uy - 0.12
                dist = math.hypot(wx, wy)
                ang = math.atan2(wy, wx)
                spoke = abs(math.sin(6 * ang)) * dist < line * 0.9          # 12 spokes
                sag = 1 - 0.10 * abs(math.sin(6 * ang))                     # rings dip between spokes
                ring = any(abs(dist - k * sag) < line * 0.6 for k in (0.24, 0.48, 0.72, 0.96))
                if (spoke or ring) and dist > 0.05:
                    col = _mixf(col, web, 0.95)
                hv = max(0.0, x * hx + y * hy + z * hz)
                col = _mixf(col, (255, 200, 195), min(0.35, hv ** 12 * 0.5))  # soft glossy sheen
                col = _mixf(col, (255, 255, 255), min(1.0, hv ** 90 * 1.6))    # crisp hotspot
                edge = math.sqrt(q)
                if edge > 0.80:                                                # darkening towards the edge
                    col = _mixf(col, rim, (edge - 0.80) / 0.20 * 0.55)
                if edge > 1 - outline:                                         # bold outline, about 1.5 px
                    col = rim
                row.append("#%02x%02x%02x" % col)
            rows.append("{" + " ".join(row) + "}")
        img.put(" ".join(rows))
        return img

    @staticmethod
    def _key(sy: float, tilt: float) -> tuple[float, int]:
        tilt = ((tilt + 180) % 360) - 180                       # a full spin comes back to 0
        step = 3 if abs(tilt) <= 15 else 15                     # fine steps for tilts, coarser for spins
        return round(round(sy / 0.03) * 0.03, 2), int(round(tilt / step) * step)

    def _sphere(self, sy: float, tilt: float) -> tk.PhotoImage:
        k = self._key(sy, tilt)
        img = self.cache.get(k)
        if img is None:
            img = self.cache[k] = self._render_sphere(*k)
        return img

    def _prewarm(self):
        """Render one cached pose per call (called from the frame loop) so bounces and spins never hitch."""
        while self.warm_list:
            k = self._key(*self.warm_list.pop())
            if k not in self.cache:
                self.cache[k] = self._render_sphere(*k)
                return

    # Lens shape of the left eye in sphere units (x right, y down), pointed end towards the nose.
    EYE_SHAPE = [(-0.15, 0.05), (-0.20, -0.10), (-0.36, -0.26), (-0.58, -0.33), (-0.73, -0.24), (-0.74, -0.06),
                 (-0.60, 0.07), (-0.38, 0.11)]

    def _build(self):
        c, s, d = self.c, self.s, self.d
        self.r = d / 2
        self.rx = self.r * EGG_W * (1 + EGG_K * -0.1)            # egg half-width at eye level
        self.cx = round(self.w / 2)
        self.top = round(8 * s) + self.head_room               # sphere top at the highest point of the hover
        self.amp = 7 * s                                        # bigger, slower hover = more, evenly spaced 1 px steps
        self.skin = COSTUMES.get(self.costume.get(), COSTUMES["classic"])
        self.cache: dict = {}
        self._sphere(1.0, 0.0)
        self.warm_list = self._warm_poses()
        try:
            import tkinter.font as tkfont
            self.icon_font = ICON_FONT if ICON_FONT in tkfont.families() else "Segoe MDL2 Assets"
        except Exception:
            self.icon_font = ICON_FONT
        self.i_thread = c.create_line(0, 0, 0, 0, fill=THREAD, width=max(1, round(1.2 * s)), state="hidden")
        self.i_shadow = c.create_oval(0, 0, 0, 0, fill=SHADOW, outline="")
        self.i_ring = c.create_oval(0, 0, 0, 0, outline=GLOW, width=max(2, round(2 * s)), state="hidden")
        self.i_sphere = c.create_image(self.cx, 0, image=self.cache[self._key(1, 0)], anchor="s")
        self.i_blush = [c.create_oval(0, 0, 0, 0, fill=BLUSH, outline="", state="hidden") for _ in range(2)]
        self.i_frame = [c.create_polygon(0, 0, 0, 0, 0, 0, fill=EYE, outline=EYE, smooth=True,
                                         width=max(2, round(2.2 * s))) for _ in range(2)]
        self.i_lens = [c.create_polygon(0, 0, 0, 0, 0, 0, fill=LENS, outline="", smooth=True) for _ in range(2)]
        self.lens_fill = LENS
        mouth = self.skin["mouth"]
        self.i_mouth = c.create_polygon(0, 0, 0, 0, 0, 0, fill=mouth, outline=mouth, smooth=True,
                                        width=max(1, round(1.4 * s)), state="hidden")
        self.i_tongue = c.create_polygon(0, 0, 0, 0, 0, 0, fill=TONGUE, outline="", smooth=True, state="hidden")
        self.i_mline = c.create_line(0, 0, 0, 0, fill=mouth, width=max(2, round(2.6 * s)), smooth=True,
                                     capstyle="round", state="hidden")
        self.i_dots = [c.create_oval(0, 0, 0, 0, fill=GLOW, outline="", state="hidden") for _ in range(3)]
        self.look = [0.0, 0.0]                                  # eased eye direction
        self.next_glance, self.glance = 2.0, (0.0, 0.0)
        self.action: dict | None = None                         # current little performance (hop, tilt, spin, …)
        self.next_idle = 5.0
        self.particles: list[dict] = []
        self.last_activity = 0.0
        self.sleepy = False
        self.next_z = 0.0
        self.prev_state = "idle"
        self.hovered = self.moved = False
        self.drag_dist = 0.0
        self.tilt_now = 0.0
        self.c.bind("<Enter>", lambda e: self._hover(True))
        self.c.bind("<Leave>", lambda e: self._hover(False))

    @staticmethod
    def _warm_poses() -> list:
        """Poses to render ahead of time; most-used last (the list is popped from the end): squashes, then tilts, then
        the spin frames."""
        return [(1.0, a) for a in range(-165, 181, 15)] \
            + [(sy / 100, a) for sy in (91, 94, 106) for a in (-9, 9)] \
            + [(1.0, a) for a in (-12, -9, -6, -3, 3, 6, 9, 12)] + [(sy / 100, 0) for sy in range(82, 116, 3)]

    # ---- costumes ----
    def set_costume(self, key: str, announce: bool = True):
        """Quick change: a confetti puff and it's in the new suit (poses re-render in the background)."""
        if key not in COSTUMES:
            return
        self.costume.set(key)
        self._save_pos()
        self.skin = COSTUMES[key]
        self.cache = {}
        self.warm_list = self._warm_poses()
        self.c.itemconfigure(self.i_sphere, image=self._sphere(1.0, 0.0))
        self.c.itemconfigure(self.i_mouth, fill=self.skin["mouth"], outline=self.skin["mouth"])
        self.c.itemconfigure(self.i_mline, fill=self.skin["mouth"])
        if announce:
            self.poke()
            self.react("happy")
            for i in range(10):
                self.root.after(i * 30, lambda: self.spawn("confetti"))
            if not self.alert_text and not self.showcase:
                self.show_bubble(f"{self.skin['label']}!", hide_after=2)

    # ---- status pill: what JARVIS is doing, cloud or local brain, the next timer, the music that's playing ----
    def _next_due(self) -> tuple[dict, float] | None:
        """The next reminder or timer due within the hour, and the seconds left."""
        now, best = datetime.now(), None
        for r in getattr(self, "upcoming", []):
            left = (self._due(r) - now).total_seconds()
            if 0 < left <= 3600 and (best is None or left < best[1]):
                best = (r, left)
        return best

    def _song(self) -> tuple[str, bool] | None:
        """(title, playing) for the Spotify app or JARVIS's Edge; a Spotify song you paused stays for 10 minutes."""
        sp, edge, now = self.status.spotify, self.status.edge_media, time.monotonic()
        if sp and " - " in sp:
            self.last_song, self.song_seen_at = sp, now
            return sp, True
        if edge.get("playing"):
            return (edge.get("title") or "Something in Edge"), True
        if sp and self.last_song and now - self.song_seen_at < 600:
            return self.last_song, False
        return None

    def _pill_items(self, state: str) -> list[tuple]:
        """[(tag, glyph, text, colour, on_click)]; an empty list means no pill."""
        items: list[tuple] = []
        if self.status.killed:                               # the kill switch is on: a red "paused" pill, always first
            items.append(("paused", "", "paused", "#d93025", lambda: self.show_bubble(
                "Paused: I won't click or type anything. Say or type resume to let me act again.", hide_after=6)))
        if time.monotonic() < self.status.looking_until:     # the visible sign that a window is being read, 3 s per read
            items.append(("look", GLYPH["look"], f"looking at {self.status.looking_app}", ACCENT, lambda: self.show_bubble(
                "I only look at one window, when you ask. Nothing is saved and nothing leaves this laptop.", hide_after=5)))
        if self.status.typing_where:                         # typing mode: everything said or typed goes into that app
            items.append(("typing", GLYPH["type"], f"typing into {self.status.typing_where} · end", ACCENT,
                          lambda: self.status.send("end")))      # a click on the pill ends typing mode
        busy = {"listening": ("mic", "Listening"), "thinking": ("think", "Thinking"),
                "speaking": ("speak", "Speaking")}.get(state)
        if busy:
            items.append(("state", GLYPH[busy[0]], busy[1], ACCENT, None))
        due = self._next_due()
        if due:
            r, left = due
            m, sec = divmod(int(left), 60)
            glyph = GLYPH["timer" if "timer" in str(r.get("text", "")).lower() else "bell"]
            items.append(("due", glyph, f"{m}:{sec:02d}", BUBBLE_INK, lambda r=r: self._say_due(r)))
        song = self._song()
        if song:
            title, playing = song
            short = title if len(title) <= 18 else title[:17].rstrip() + "…"
            items.append(("song", GLYPH["music"], short, BUBBLE_INK,
                          lambda: self.show_bubble(("Playing: " if playing else "Paused: ") + title, hide_after=4)))
            items.append(("pp", GLYPH["pause" if playing else "play"], "", ACCENT, lambda: self._media("playpause")))
            items.append(("next", GLYPH["next"], "", ACCENT, lambda: self._media("next")))
        if (items or self.hovered) and self.status.online and self.status.cloud is not None:
            cloud = self.status.cloud
            items.insert(0, ("brain", GLYPH["cloud" if cloud else "local"], "", HINT if cloud else ACCENT, lambda: self.show_bubble(
                "Thinking in the cloud (Groq): quick replies." if cloud else
                "The cloud is off or resting, so I'm thinking on this laptop. Replies are a bit slower.", hide_after=5)))
        return items

    def _say_due(self, r: dict):
        due = self._due(r)
        self.show_bubble(f"At {due:%I:%M %p}".replace(" 0", " ") + f": {str(r.get('text') or 'a reminder')}", hide_after=5)

    def _media(self, kind: str):
        media_key(kind)
        self.poke()
        self.react("nod")

    def _update_pill(self, state: str):
        items = self._pill_items(state) if self.shown else []
        sig = tuple(i[:4] for i in items)
        if sig == self.pill_sig:
            return
        self.pill_sig = sig
        if not items:
            if self.pill is not None:
                self.pill.destroy()
                self.pill = None
            return
        s = self.s
        if self.pill is None:
            p = tk.Toplevel(self.root)
            p.overrideredirect(True)
            p.attributes("-topmost", True)
            p.attributes("-transparentcolor", KEY)
            p.configure(bg=KEY)
            self.pill_c = tk.Canvas(p, bg=KEY, highlightthickness=0, bd=0)
            self.pill_c.pack()
            self.pill = p
        c = self.pill_c
        c.delete("all")
        H = int(20 * s)
        paused = any(i[0] == "paused" for i in items)
        bg = c.create_polygon(0, 0, 0, 0, 0, 0, smooth=True, fill=BUBBLE, outline="#d93025" if paused else BUBBLE_EDGE,
                              width=max(1, round((3 if paused else 1.5) * s)))      # red outline while paused
        x, mid = int(9 * s), H // 2
        for tag, glyph, text, col, fn in items:
            hit = c.create_rectangle(0, 0, 0, 0, fill=BUBBLE, outline="", tags=(tag,))   # the whole item is clickable
            x0 = x
            if glyph:
                x = c.bbox(c.create_text(x, mid, text=glyph, anchor="w", font=(self.icon_font, 9), fill=col, tags=(tag,)))[2]
            if text:
                x = c.bbox(c.create_text(x + int(3 * s), mid, text=text, anchor="w", font=("Segoe UI Semibold", 8),
                                         fill=col, tags=(tag,)))[2]
            c.coords(hit, x0 - int(3 * s), 2, x + int(3 * s), H - 2)
            if fn:
                c.tag_bind(tag, "<Button-1>", lambda e, fn=fn: fn())
                c.tag_bind(tag, "<Enter>", lambda e: c.configure(cursor="hand2"))
                c.tag_bind(tag, "<Leave>", lambda e: c.configure(cursor=""))
            x += int(8 * s)
        W = x - int(8 * s) + int(9 * s)
        c.coords(bg, *self._pill_pts(1, 1, W - 1, H - 1))
        c.configure(width=W + 1, height=H + 1)
        self.pill_size = (W + 1, H + 1)
        self._place_popups()

    # ---- drag and drop: a file, a link or some text dropped on spidey ----
    def _setup_drop(self) -> bool:
        try:
            from tkinterdnd2 import DND_FILES, DND_TEXT, TkinterDnD
            TkinterDnD._require(self.root)
            self.c.drop_target_register(DND_FILES, DND_TEXT)
            self.c.dnd_bind("<<DropEnter>>", self._drop_enter)
            self.c.dnd_bind("<<DropPosition>>", lambda e: "copy")
            self.c.dnd_bind("<<DropLeave>>", self._drop_leave)
            self.c.dnd_bind("<<Drop>>", self._drop)
            return True
        except Exception as exc:
            print(f"drag and drop unavailable: {exc!r}", flush=True)
            return False

    def _drop_enter(self, e):
        self.drop_hover = True                                   # mouth open, eyes wide: gimme
        self.poke()
        if not self.action:
            self.react("surprised")
        return "copy"

    def _drop_leave(self, e):
        self.drop_hover = False
        return "copy"

    def _drop(self, e):
        self.drop_hover = False
        self.poke()
        data = e.data or ""
        try:
            parts = list(self.root.tk.splitlist(data))
        except tk.TclError:
            parts = []
        paths = [Path(p) for p in parts if p and os.path.exists(p)]
        if paths and len(paths) == len(parts):
            self._dropped_files(paths)
        else:
            text = data.strip()
            if text.startswith("{") and text.endswith("}"):
                text = text[1:-1].strip()
            if LINK_RE.fullmatch(text):
                self._dropped_link(text)
            elif text:
                self._dropped_text(text)
        return "copy"

    def _dropped_link(self, url: str):
        host = re.sub(r"^www\.", "", urlparse(url).hostname or "that site")
        self.react("nod")
        self.show_bubble(f"A link to {host}. What should I do with it?", choices=[
            ("Open it", lambda: self._ask(f"open {url}")),
            ("Sum it up", lambda: self._ask(f"open {url} and summarize it")),
            ("Never mind", lambda: None)])

    def _dropped_text(self, text: str):
        self.react("nod")
        self.open_bar(prefill=re.sub(r"\s+", " ", text)[:500])
        self.show_bubble("Want to ask JARVIS about this? Change it if you like, then press Enter.", hide_after=6)

    def _dropped_files(self, paths: list[Path]):
        try:
            from jarvis.tools import BLOCKED_EXTENSIONS
            blocked = BLOCKED_EXT | set(BLOCKED_EXTENSIONS)
        except Exception:
            blocked = BLOCKED_EXT
        self.react("nod")
        p = paths[0]
        never = ("Never mind", lambda: None)
        reveal = ("Show in folder", lambda: self._reveal(p))
        if len(paths) > 1:
            safe = [q for q in paths if q.is_dir() or q.suffix.lower() not in blocked][:5]
            text = f"{len(paths)} things: " + ", ".join(q.name for q in paths[:3]) + ("…" if len(paths) > 3 else ".")
            if len(safe) < len(paths):
                text += " I won't open programs, scripts or shortcuts" + (", or more than 5 at once." if len(paths) > 5 else ".")
            opener = ("Open them" if len(safe) == len(paths) else f"Open {len(safe)}",
                      lambda: [self._open_path(q) for q in safe])
            choices = ([opener] if safe else []) + [reveal, never]
        elif p.is_dir():
            text, choices = f"Your {p.name} folder.", [("Open it", lambda: self._open_path(p)), never]
        elif p.suffix.lower() in blocked:
            text = f"{p.name} is a program, script or shortcut. I don't open those. Double-click it yourself if you trust it."
            choices = [reveal, ("OK", lambda: None)]
        else:
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            human = f"{size / 1e6:.1f} MB" if size >= 1e6 else f"{max(1, round(size / 1e3))} KB"
            text = f"{p.name} ({human}). What should I do with it?"
            choices = [("Open it", lambda: self._open_path(p))]
            if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp") and 0 < size < 40_000_000:
                choices.insert(0, ("Show it here", lambda: self.get_board().show_file(p)))
            if p.suffix.lower() in TEXT_FILES and 0 < size < 2_000_000:
                choices.append(("Sum it up", lambda: self._summarize_file(p)))
            choices += [reveal, never]
        self.show_bubble(text, choices=choices)

    def _open_path(self, p: Path):
        try:
            os.startfile(str(p))                                 # opens with its usual app, like a double-click
        except OSError:
            self.show_bubble(f"Windows couldn't open {p.name}.", hide_after=5)
            self.react("shake")

    def _reveal(self, p: Path):
        import subprocess
        subprocess.Popen(f'explorer /select,"{p}"')

    def _summarize_file(self, p: Path):
        if not self.status.online:
            self.show_bubble("I can't reach JARVIS right now. Is it running?", hide_after=6)
            self.react("shake")
            return
        try:
            text = p.read_text(encoding="utf-8", errors="replace")[:6000]
        except OSError:
            text = ""
        if not text.strip():
            self.show_bubble(f"I couldn't read anything in {p.name}.", hide_after=5)
            return
        self.chat_active, self.chat_reply, self.chat_sent_at = True, [], time.monotonic()
        note = " (its text goes to the cloud brain, Groq)" if self.status.cloud else ""
        self.show_bubble(f"Reading {p.name}…{note}", typed=True)
        self.status.post("/api/summarize", {"name": p.name, "text": text})

    # ---- tricks: shoot a web at the mouse, hang upside down from the top of the screen ----
    def shoot_web(self, target: tuple[int, int] | None = None):
        """Thwip! A web line shoots out to the mouse pointer, sticks there for a moment and reels back in. It's drawn
        in its own click-through window, so it never gets in the way of a click."""
        if self.web is not None or not self.shown or self.hanging:
            return
        s = self.s
        px, py = target or self.root.winfo_pointerxy()
        sx0, sy0 = self.x + self.cx, round(self.y + self._rest_cy())
        if math.hypot(px - sx0, py - sy0) < self.r * 2.5:
            return
        m = int(14 * s)
        x0, y0 = min(px, sx0) - m, min(py, sy0) - m
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.attributes("-transparentcolor", KEY)
        win.configure(bg=KEY)
        win.geometry(f"{abs(px - sx0) + 2 * m}x{abs(py - sy0) + 2 * m}+{x0}+{y0}")
        c = tk.Canvas(win, width=abs(px - sx0) + 2 * m, height=abs(py - sy0) + 2 * m, bg=KEY, highlightthickness=0, bd=0)
        c.pack()
        click_through(win)
        lw = max(1, round(1.4 * s))

        def strand(**kw):        # a light thread with a dark edge, so it shows on white pages and dark ones alike
            return [c.create_line(0, 0, 0, 0, fill=WEB_EDGE, width=lw + 2, capstyle="round", **kw),
                    c.create_line(0, 0, 0, 0, fill=THREAD, width=lw, capstyle="round", **kw)]
        self.web = {"win": win, "c": c, "line": strand(smooth=True),
                    "splat": [strand(state="hidden") for _ in range(8)],
                    "from": (sx0 - x0, sy0 - y0), "to": (px - x0, py - y0), "start": time.monotonic()}
        side = 1 if px > sx0 else -1
        self.glance, self.next_glance = (0.09 * side, 0.07 * (1 if py > sy0 else -1)), time.monotonic() - self.t0 + 1.2
        self.poke()
        self.react("shoot", side=side)

    def _update_web(self):
        wb = self.web
        if not wb:
            return
        age, s = time.monotonic() - wb["start"], self.s
        if age > 1.0:
            wb["win"].destroy()
            self.web = None
            return
        (fx, fy), (tx, ty) = wb["from"], wb["to"]
        d = math.hypot(tx - fx, ty - fy) or 1
        fx, fy = fx + (tx - fx) / d * self.r * 0.8, fy + (ty - fy) / d * self.r * 0.8     # from the edge of its head
        k = _ease(age / 0.16) if age < 0.16 else 1.0 if age < 0.72 else 1 - _ease((age - 0.72) / 0.26)
        ex, ey = fx + (tx - fx) * k, fy + (ty - fy) * k
        sag = 0 if age < 0.72 else math.sin(math.pi * k) * 10 * s   # goes slack as it reels in
        c = wb["c"]
        for item in wb["line"]:
            c.coords(item, round(fx), round(fy), round((fx + ex) / 2), round((fy + ey) / 2 + sag), round(ex), round(ey))
        stuck = 0.16 <= age < 0.72
        g = min(1.0, (age - 0.16) / 0.08) if stuck else 0
        for i, pair in enumerate(wb["splat"]):                   # the splat where it sticks
            a = i / len(wb["splat"]) * 2 * math.pi + 0.3
            L = (5 + 2 * (i % 2)) * s * g
            for item in pair:
                if stuck:
                    c.coords(item, round(tx), round(ty), round(tx + L * math.cos(a)), round(ty + L * math.sin(a)))
                c.itemconfigure(item, state="normal" if stuck else "hidden")
        if stuck:
            for pair in wb["splat"]:                             # light strands over all the dark edges
                c.tag_raise(pair[1])

    def hang(self):
        """Leaps up to the top of the screen and hangs there upside down on a web line for a while (a tap, a drag
        or the time running out brings it home)."""
        if self.hanging or self.drag or self.fling:
            return
        l, t, r, b = self._bounds()
        self.perch = None
        self.travel(min(max(self.x, l), r), t, "leap", then=self._start_hang)

    def _start_hang(self):
        self.hanging = True
        dur = random.uniform(7, 11)
        self.react("hang", dur=dur)
        self.hang_job = self.root.after(int(dur * 1000), lambda: self.hanging and self._end_hang())

    def _end_hang(self):
        if getattr(self, "hang_job", None):
            self.root.after_cancel(self.hang_job)
            self.hang_job = None
        self.hanging = False
        if self.action and self.action["name"] == "hang":
            self.action = None
        self.travel(*self.home, style="leap")

    # ---- little performances ----
    DURATIONS = {"land": 0.45, "tilt": 1.7, "wiggle": 1.1, "happy": 1.6, "wake": 0.9, "surprised": 0.7, "spin": 0.9,
                 "swing": 3.2, "wink": 1.1, "shake": 0.6, "nod": 0.8, "dizzy": 2.2, "love": 2.2, "yawn": 1.8,
                 "celebrate": 1.3, "peek": 2.4, "blep": 1.8, "lick": 1.4, "flinch": 0.7, "shoot": 1.0}

    def react(self, name: str, **extra):
        """Start an animation: hop, land, tilt, wiggle, happy, wake, surprised, spin, swing, wink, shake, nod,
        dizzy, love, yawn, celebrate, peek."""
        t = time.monotonic() - self.t0
        dur = extra.get("dur") or (0.42 * extra.get("n", 2) if name == "hop" else self.DURATIONS.get(name, 1.0))
        self.action = {"name": name, "start": t, "dur": dur, "side": random.choice((-1, 1)), **extra}
        after = self.root.after
        if name == "happy":
            for i in range(3):
                after(i * 260, lambda: self.spawn("heart"))
        elif name == "love":
            for i in range(5):
                after(i * 300, lambda: self.spawn("heart"))
        elif name == "tilt":
            self.spawn("q")
        elif name in ("hop", "wake", "wink"):
            after(120, lambda: self.spawn("star"))
        elif name == "spin":
            after(700, lambda: [self.spawn("star") for _ in range(2)])
        elif name == "celebrate":
            for i in range(14):
                after(i * 25, lambda: self.spawn("confetti"))
        elif name == "yawn" and not extra.get("awake"):          # a night-time yawn while you're around: stays up
            after(int(dur * 1000), lambda: setattr(self, "sleepy", True))
        elif name == "flinch":
            self.spawn("!")
        elif name == "leap":
            after(int(dur * 850), lambda: self.spawn("star"))

    def poke(self):
        """Something happened (click, chat, JARVIS doing something): wake up if asleep."""
        self.last_activity = time.monotonic() - self.t0
        if self.sleepy:
            self.sleepy = False
            self.react("wake")

    def _hover(self, on: bool):
        self.hovered = on
        if on:
            self.poke()
            if self.hover_since is None:
                self.hover_since = time.monotonic()
        elif self.ring is None:
            self.hover_since = None

    def travel(self, x: float, y: float, style: str = "walk", then=None, clamp: bool = True):
        """Move the whole window somewhere: 'walk' (little hops), 'swing_to' (on a web line, dipping in the middle)
        or 'leap' (a big arc with a flip). then() runs on arrival."""
        tx, ty = self._clamp(x, y) if clamp else (round(x), round(y))
        dx, dist = tx - self.x, math.hypot(tx - self.x, ty - self.y)
        if dist < 3:
            if then:
                then()
            return
        hops = max(2, min(8, round(dist / (16 * self.s))))
        dur = {"walk": 0.34 * hops, "swing_to": 1.8, "leap": 1.2}[style]
        self.react(style, n=hops, dur=dur, frm=(self.x, self.y), to=(tx, ty), side=1 if dx >= 0 else -1, then=then)

    def _travel_step(self, t: float):
        """Window position along the current trip; on arrival snap to the target and run its then()."""
        a = self.action
        if not a or "to" not in a or self.drag:
            return
        p = min(1.0, (t - a["start"]) / a["dur"])
        (fx, fy), (tx, ty), s = a["frm"], a["to"], self.s
        if a["name"] == "walk":                                  # moves only while in the air
            n = a["n"]
            u = (min(n - 1, int(p * n)) + _ease(((p * n) % 1.0 if p < 1 else 1.0) / 0.88)) / n
            off = 0.0
        elif a["name"] == "swing_to":
            u = _ease(p)
            off = math.sin(math.pi * u) * 22 * s                 # swings low through the middle
        else:                                                    # leap
            u = _ease((p - 0.15) / 0.7)
            off = -math.sin(math.pi * u) * min(80 * s, 26 * s + 0.15 * abs(tx - fx))
        if p >= 1:
            u, off = 1.0, 0.0
        self._goto(round(fx + (tx - fx) * u), round(fy + (ty - fy) * u + off))
        if p >= 1 and not a.get("arrived"):
            a["arrived"] = True
            then = a.get("then")
            self.action = None
            if then:
                then()

    def spawn(self, kind: str):
        """A tiny heart, sparkle, confetti bit, '?', '!' or 'z' that floats up from spidey's head and shrinks away."""
        c, s = self.c, self.s
        x = self.cx + random.uniform(-0.5, 0.5) * self.r
        y = self.top - self.head_room * 0.2 + random.uniform(-2, 2) * s
        vx, vy, life, size = random.uniform(-6, 6) * s, -random.uniform(16, 24) * s, 1.6, 3.0 * s
        if kind in ("z", "q", "!"):
            item = c.create_text(0, 0, text={"z": "z", "q": "?", "!": "!"}[kind], fill=ZZZ if kind == "z" else ALERT,
                                 font=("Segoe UI Semibold", 8 if kind == "z" else 9))
            x = self.cx + self.r * (0.55 if kind == "z" else 0.65)
            life = 2.2 if kind == "z" else 1.4
        elif kind == "confetti":
            item = c.create_polygon(0, 0, 0, 0, 0, 0, fill=random.choice(CONFETTI), outline="")
            x, y = self.cx, self.top + self.r * 0.6
            a = random.uniform(-math.pi * 0.85, -math.pi * 0.15)
            sp = random.uniform(40, 70) * s
            vx, vy, life, size = math.cos(a) * sp, math.sin(a) * sp, 1.2, 2.2 * s
        else:
            item = c.create_polygon(0, 0, 0, 0, 0, 0, fill=HEART if kind == "heart" else SPARKLE, outline="",
                                    smooth=(kind == "heart"))
            size = (3.4 if kind == "heart" else 3.0) * s
        self.particles.append({"id": item, "kind": kind, "x": x, "y": y, "vx": vx, "vy": vy,
                               "born": time.monotonic(), "life": life, "size": size, "spin": random.uniform(-6, 6)})

    def _update_particles(self):
        c, now, s = self.c, time.monotonic(), self.s
        keep = []
        for p in self.particles:
            age = now - p["born"]
            if age > p["life"]:
                c.delete(p["id"])
                continue
            x = p["x"] + p["vx"] * age + (math.sin(age * 5) * 2 * s if p["kind"] in ("heart", "z") else 0)
            y = p["y"] + p["vy"] * age + (0.5 * 120 * s * age * age if p["kind"] == "confetti" else 0)  # gravity
            k = p["size"] * (1 - (age / p["life"]) ** 2) * (min(1.0, age * 6))   # pop in, shrink away
            X, Y = round(x), round(y)
            if p["kind"] in ("z", "q", "!"):
                c.coords(p["id"], X, Y)
            elif p["kind"] == "heart":
                c.coords(p["id"], *self._heart(X, Y, k))
            elif p["kind"] == "confetti":   # a little tumbling square
                a = age * p["spin"]
                pts = []
                for i in range(4):
                    b = a + i * math.pi / 2
                    pts += [round(X + k * math.cos(b)), round(Y + k * 0.6 * math.sin(b))]
                c.coords(p["id"], *pts)
            else:  # 4-point sparkle
                pts = []
                for i in range(8):
                    a = i / 8 * 2 * math.pi + age * 2
                    rr = k * (1.3 if i % 2 == 0 else 0.4)
                    pts += [round(X + rr * math.cos(a)), round(Y + rr * math.sin(a))]
                c.coords(p["id"], *pts)
            keep.append(p)
        self.particles = keep

    @staticmethod
    def _heart(X: float, Y: float, k: float, n: int = 14) -> list:
        pts = []
        for i in range(n):
            a = i / n * 2 * math.pi
            hx = 16 * math.sin(a) ** 3
            hy = 13 * math.cos(a) - 5 * math.cos(2 * a) - 2 * math.cos(3 * a) - math.cos(4 * a)
            pts += [round(X + hx / 16 * k), round(Y - hy / 16 * k)]
        return pts

    def _performance(self, t: float) -> dict:
        """What the current action does this frame: lift and dx in px, squash/stretch, tilt (deg), eye mode,
        eye size, blush, whether the tilt is exact (spins), a web thread, eye openness and dizzy stars."""
        out = {"lift": 0.0, "dx": 0.0, "sy": 1.0, "tilt": 0.0, "mode": "", "grow": 1.0, "blush": False,
               "raw_tilt": False, "thread": False, "open": 1.0, "dizzy": False, "hang": False}
        a, s = self.action, self.s
        if not a:
            return out
        p = (t - a["start"]) / a["dur"]
        if p >= 1:
            self.action = None
            return out
        name, side = a["name"], a["side"]
        ease = lambda u: u * u * (3 - 2 * u)  # noqa: E731  (smoothstep)
        fade = ease(min(1, p / 0.2)) * ease(min(1, (1 - p) / 0.2))  # in and out of a pose
        if name in ("hop", "wake"):
            n = a.get("n", 2) if name == "hop" else 1
            q = (p * n) % 1.0
            out["lift"] = 10 * s * 4 * q * (1 - q)
            if q < 0.12:
                out["sy"] = 1 - 0.14 * (1 - q / 0.12)      # crouch before take-off
            elif q > 0.88:
                out["sy"] = 1 - 0.14 * ((q - 0.88) / 0.12)  # squash on landing
            else:
                out["sy"] = 1 + 0.07 * math.sin(math.pi * (q - 0.12) / 0.76)   # stretch in the air
            out.update(mode="happy" if name == "hop" else "", grow=1.18 if name == "wake" else 1.0, blush=name == "hop")
        elif name == "land":
            out["sy"] = 1 - 0.16 * math.sin(math.pi * min(1, p * 1.6)) * (1 - p) + 0.05 * math.sin(math.pi * p * 3) * (1 - p)
        elif name == "tilt":
            out.update(tilt=11 * side * fade, grow=1.06)
        elif name == "wiggle":
            out.update(tilt=10 * math.sin(p * 4 * math.pi) * (1 - p), mode="happy", blush=True)
        elif name == "happy":
            out.update(lift=3 * s * abs(math.sin(p * 3 * math.pi)) * (1 - p), mode="happy", blush=True)
        elif name == "surprised":
            out.update(sy=1 + 0.06 * math.sin(math.pi * p), grow=1.25 - 0.2 * p)
            if p < 0.05 and not a.get("bang"):
                a["bang"] = True
                self.spawn("!")
        elif name == "spin":                                     # a full barrel roll with a little hop
            out.update(tilt=360 * side * ease(p), raw_tilt=True, lift=6 * s * math.sin(math.pi * p), mode="happy")
        elif name == "swing":                                    # hangs from a web line and swings like a pendulum
            amp = 6 * s * fade
            out.update(dx=amp * math.sin(p * 3 * 2 * math.pi), lift=5 * s * fade, thread=True,
                       tilt=-10 * fade * math.sin(p * 3 * 2 * math.pi), sy=1 + 0.04 * fade, blush=True)
        elif name == "wink":
            out.update(mode="wink", blush=True, tilt=4 * side * fade)
        elif name == "shake":                                    # "nope" / "that didn't work"
            out.update(dx=4 * s * math.sin(p * 6 * math.pi) * (1 - p), open=0.7)
        elif name == "nod":                                      # "got it"
            out.update(lift=-3 * s * abs(math.sin(p * 2 * math.pi)), sy=1 - 0.05 * abs(math.sin(p * 2 * math.pi)),
                       mode="happy" if p > 0.5 else "")
        elif name == "dizzy":                                    # after being swung around
            out.update(tilt=9 * math.sin(t * 9) * (1 - p), dizzy=True, open=0.55, grow=0.9)
        elif name == "love":                                     # heart lenses
            out.update(mode="love", blush=True, lift=2 * s * abs(math.sin(p * 4 * math.pi)) * (1 - p),
                       sy=1 + 0.03 * math.sin(p * 8 * math.pi))
        elif name == "yawn":                                     # a big stretch before dozing off
            k = math.sin(math.pi * p)
            out.update(sy=1 + 0.10 * k, open=max(0.15, 1 - 0.85 * k), lift=2 * s * k)
        elif name == "celebrate":
            q = min(1.0, p * 2)
            out.update(lift=12 * s * 4 * q * (1 - q), sy=1 + 0.08 * math.sin(math.pi * q), mode="happy", blush=True)
        elif name == "blep":                                     # tongue out, cheeky head tilt
            out.update(tilt=7 * side * fade, blush=True)
        elif name == "lick":
            out.update(lift=1.5 * s * fade)
        elif name == "walk":                                     # a stroll: little hops, leaning the way it goes
            n = a["n"]
            q = (p * n) % 1.0
            out["lift"] = 5 * s * 4 * q * (1 - q)
            out["sy"] = 1 - 0.08 * (1 - q / 0.12) if q < 0.12 else 1 - 0.08 * ((q - 0.88) / 0.12) if q > 0.88 \
                else 1 + 0.04 * math.sin(math.pi * (q - 0.12) / 0.76)
            out["tilt"] = 6 * side * fade
            self.glance, self.next_glance = (0.08 * side, 0.0), t + 0.6
        elif name == "swing_to":                                 # web-swinging to a new spot
            out.update(thread=True, tilt=-16 * side * math.sin(math.pi * p), sy=1 + 0.05 * fade, blush=True,
                       mode="happy" if 0.3 < p < 0.7 else "")
            self.glance, self.next_glance = (0.08 * side, 0.0), t + 0.6
        elif name == "leap":                                     # crouch, flip through the air, land
            if p < 0.15:
                out["sy"] = 1 - 0.14 * math.sin(math.pi * p / 0.3)
            elif p < 0.85:
                k = (p - 0.15) / 0.7
                out.update(sy=1 + 0.07 * math.sin(math.pi * k), tilt=360 * side * _ease(k), raw_tilt=True, mode="happy")
            else:
                out["sy"] = 1 - 0.15 * math.sin(math.pi * (p - 0.85) / 0.15)
        elif name == "flinch":                                   # something came flying at it: ducks back
            k = math.sin(math.pi * min(1.0, p * 2.2)) * (1 - p)
            out.update(dx=-a.get("away", side) * 7 * s * k, sy=1 - 0.12 * k, grow=1 + 0.3 * k, tilt=-8 * a.get("away", side) * k)
        elif name == "shoot":                                    # thwip! leans into the shot, with a little recoil
            k = math.sin(math.pi * min(1.0, p * 4))
            out.update(tilt=9 * side * fade, dx=-2.5 * s * side * k, sy=1 - 0.05 * k, grow=1.12)
        elif name == "hang":                                     # upside down on a web line, swaying; flips over
            el = t - a["start"]
            flip = _ease(el / 0.5) * _ease((a["dur"] - el) / 0.5)
            sway = math.sin(el * 2.1)
            out.update(tilt=(180 + 7 * sway) * flip, raw_tilt=True, dx=3 * s * sway * flip, thread=True, hang=True,
                       blush=False)
        elif name == "peek":                                     # looks one way, then the other, then at you
            g = (-0.10 * side) if p < 0.4 else (0.10 * side if p < 0.8 else 0.0)
            self.glance, self.next_glance = (g, -0.02 if p < 0.8 else 0.0), t + 0.5
            out.update(tilt=5 * side * (1 if p < 0.4 else -1 if p < 0.8 else 0) * fade)
        return out

    def _eye_points(self, side: int, cx: float, cy: float, open_: float, grow: float, lx: int, ly: int, pad: float,
                    sy: float = 1.0, tilt: float = 0.0, mode: str = ""):
        """Eye polygon for one side (-1 left, +1 right): blink/squint (open_), size (grow), frame padding (pad),
        squash/stretch (sy), head tilt (degrees) and expression ('happy' = ^ arch, 'sleepy' = droopy, 'love' = heart)."""
        r, rx = self.r, self.rx
        sx = 1 / math.sqrt(sy)
        pts = [(x * -side if side > 0 else x, y) for x, y in self.EYE_SHAPE]
        mx = sum(p[0] for p in pts) / len(pts)
        my = sum(p[1] for p in pts) / len(pts)
        hw = max(abs(x - mx) for x, _ in pts)
        if mode == "sleepy":
            open_, my = min(open_, 0.28), my + 0.05
        ca, sa = math.cos(math.radians(tilt)), math.sin(math.radians(tilt))
        fixed = mode in ("happy", "love")
        if mode == "happy":                     # closed, smiling eyes: a thick ^ arch instead of the lens
            arch, w, th = [], hw * 0.95 * grow, 0.10
            for i in range(9):
                u = -1 + i / 4
                arch.append((mx + u * w, my - 0.02 - 0.16 * (1 - u * u)))
            for i in range(9):
                u = 1 - i / 4
                arch.append((mx + u * w * 0.8, my - 0.02 - 0.16 * (1 - u * u) + th))
            pts, open_ = arch, 1.0
        elif mode == "love":                    # heart-shaped lenses
            k = hw * 0.85 * grow
            pts = []
            for i in range(16):
                a = i / 16 * 2 * math.pi
                hx = 16 * math.sin(a) ** 3
                hy = 13 * math.cos(a) - 5 * math.cos(2 * a) - 2 * math.cos(3 * a) - math.cos(4 * a)
                pts.append((mx + hx / 16 * k, my - 0.02 - hy / 16 * k))
            open_ = 1.0
        out = []
        for x, y in pts:
            dx, dy = (x - mx) * (1 if fixed else grow), (y - my) * (1 if fixed else grow * open_)
            ln = math.hypot(dx, dy) or 1
            dx, dy = dx + dx / ln * pad, dy + dy / ln * pad * max(open_, 0.3)
            ux, uy = (mx + dx) * sx, (my + dy) * sy        # squash with the ball
            ux, uy = ux * ca - uy * sa, ux * sa + uy * ca  # turn with the head
            out += [round(cx + ux * rx) + lx, round(cy + uy * r) + ly]
        return out

    MOUTH_FOR = {"surprised": "o", "wake": "o", "shake": "frown", "dizzy": "wavy", "yawn": "yawn", "hop": "grin",
                 "happy": "grin", "wiggle": "grin", "celebrate": "grin", "love": "kiss", "wink": "grin", "swing": "grin",
                 "spin": "grin", "nod": "smile", "tilt": "small", "peek": "small", "land": "o", "blep": "blep",
                 "lick": "lick", "flinch": "o", "walk": "smile", "swing_to": "grin", "leap": "grin", "shoot": "grin",
                 "hang": "grin"}

    def _draw_mouth(self, t: float, state: str, cx: float, cy: float, sy: float, tilt: float, mode: str, f: dict):
        """A little black mouth with a red tongue that moves with the mood: a breathing smile, a grin with a
        wagging tongue, chatter while talking (changing shapes like real vowels), 'o', hmm, frown, blep, lick…"""
        c, r, rx, rnd = self.c, self.r, self.rx, round
        sx = 1 / math.sqrt(sy)
        ca, sa = math.cos(math.radians(tilt)), math.sin(math.radians(tilt))

        def xf(pts):                       # sphere units → pixels, squashed and turned with the head
            out = []
            for u, v in pts:
                u, v = u * sx, v * sy
                u, v = u * ca - v * sa, u * sa + v * ca
                out += [rnd(cx + u * rx), rnd(cy + v * r)]
            return out

        def oval(w, h, y0, x0=0.0, n=12):
            return [(x0 + w * math.cos(a), y0 + h * math.sin(a)) for a in [i / n * 2 * math.pi for i in range(n)]]

        act = self.action
        name = act["name"] if act else ""
        p = min(1.0, max(0.0, (t - act["start"]) / act["dur"])) if act else 0.0
        if (self.drag and self.moved) or self.fling or self.drop_hover:
            kind = "o"
        elif state == "speaking" and not name:
            kind = "talk"
        elif name in self.MOUTH_FOR:
            kind = self.MOUTH_FOR[name]
        elif state == "alert":
            kind = "grin"
        elif state == "listening":
            kind = "o"
        elif state == "thinking":
            kind = "hmm"
        elif self.sleepy:
            kind = "sleep"
        elif self.hovered or mode == "happy":
            kind = "grin"
        else:
            kind = "smile"
        my = 0.45
        poly = line = tongue = None
        if kind == "smile":                     # breathes a little; now and then a lopsided smirk
            w = 0.19 + 0.015 * math.sin(t * 1.3)
            smirk = 0.05 * max(0.0, math.sin(t * 0.37)) ** 8
            line = [(-w + 2 * w * i / 6, my - 0.035 + 0.09 * (1 - ((i - 3) / 3) ** 2) - smirk * (i / 6)) for i in range(7)]
        elif kind == "small":
            line = [(-0.12 + 0.04 * i, my - 0.02 + 0.05 * (1 - ((i - 3) / 3) ** 2)) for i in range(7)]
        elif kind == "frown":                   # with a little quiver
            q = 0.01 * math.sin(t * 30)
            line = [(-0.15 + 0.05 * i, my + 0.04 - 0.08 * (1 - ((i - 3) / 3) ** 2) + q * (i % 2)) for i in range(7)]
        elif kind == "hmm":                     # a sideways line that slides back and forth while thinking
            off = 0.05 * math.sin(t * 2.2)
            line = [(0.0 + off, my + 0.015), (0.09 + off, my), (0.18 + off, my - 0.02)]
        elif kind == "wavy":
            line = [(-0.16 + i * 0.04, my + 0.03 * math.sin(i * 1.6 + t * 8)) for i in range(9)]
        elif kind in ("blep", "lick"):          # smile with the tongue poking out (blep) or licking the lips
            line = [(-0.17 + 0.17 * i / 3, my - 0.02 + 0.06 * (1 - ((i - 3) / 3) ** 2)) for i in range(7)]
            out_k = math.sin(math.pi * p)
            if kind == "blep":
                tongue = oval(0.095, 0.045 + 0.075 * out_k, my + 0.07 + 0.045 * out_k, n=12)
            else:
                tongue = oval(0.06, 0.04 * out_k + 0.01, my + 0.05, x0=0.13 * math.sin(p * 2 * math.pi) * out_k, n=12)
        elif kind == "grin":                    # open "D" smile; the tongue wags
            w, h = 0.21 + 0.015 * math.sin(t * 6), 0.18 + 0.02 * math.sin(t * 6)
            top = [(-w + 2 * w * i / 6, my - 0.02) for i in range(7)]
            bottom = [(w * math.cos(a), my - 0.02 + h * math.sin(a)) for a in [i / 8 * math.pi for i in range(9)]]
            poly = top + bottom
            wag = 0.03 * math.sin(t * 9)
            tongue = oval(0.095, 0.05, my + 0.10 + 0.01 * math.sin(t * 6), x0=wag, n=10)
        elif kind == "kiss":                    # a little puckered "3"-ish pout that pulses
            k = 1 + 0.15 * math.sin(t * 8)
            poly = oval(0.05 * k, 0.05 * k, my + 0.02, x0=0.02, n=10)
        elif kind == "talk":                    # chatter: open/close with changing shapes, the tongue shows when open
            open_k = (0.5 + 0.5 * math.sin(t * 12)) * (0.55 + 0.45 * abs(math.sin(t * 3.1)))
            wide = 0.5 + 0.5 * math.sin(t * 4.7 + 1)          # wide "ee/aa" vs round "oo"
            w = 0.07 + 0.08 * wide
            h = 0.03 + 0.11 * open_k
            poly = oval(w, h, my + 0.03)
            if h > 0.07:
                tongue = oval(w * 0.6, (h - 0.04) * 0.45, my + 0.03 + h * 0.55)
        elif kind == "yawn":
            k = math.sin(math.pi * p)
            w, h = 0.08 + 0.05 * k, 0.04 + 0.16 * k
            poly = oval(w, h, my + 0.03)
            if k > 0.4:
                tongue = oval(w * 0.65, h * 0.3, my + 0.03 + h * 0.6)
        elif kind == "sleep":
            w = 0.035 + 0.015 * math.sin(t * 2)
            poly = oval(w, w, my + 0.03, n=10)
        else:                                   # "o", pulsing a little
            k = 1 + 0.12 * math.sin(t * 7)
            poly = oval(0.085 * k, 0.10 * k, my + 0.03)
        for item, pts in ((self.i_mouth, poly), (self.i_tongue, tongue), (self.i_mline, line)):
            if pts:
                c.coords(item, *xf(pts))
                c.itemconfigure(item, state="normal")
            else:
                c.itemconfigure(item, state="hidden")

    IDLE_MOVES = (["hop", "tilt", "wiggle", "happy", "look", "spin", "swing", "wink", "peek", "nod", "blep", "lick", "web"],
                  [3, 3, 2, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2])
    NIGHT_MOVES = (["tilt", "look", "wink", "peek", "blep", "yawn"], [3, 3, 1, 2, 1, 2])   # late at night: calmer, yawny

    # ---- idle life: notices you, dozes while you're away, wanders off and comes home ----
    def _life(self, t: float, state: str):
        """Where the mouse is (eyes follow it, a fast swipe makes it flinch), whether you're away (dozes off) or back
        (happy to see you); and when nothing's going on it strolls, swings, perches on a window and comes home."""
        s = self.s
        px, py = self.root.winfo_pointerxy()
        scx, scy = self.x + self.cx, self.y + self._rest_cy()
        dist = math.hypot(px - scx, py - scy)
        speed = math.hypot(px - self.ptr[0], py - self.ptr[1]) / max(1e-3, t - self.ptr_t) if self.ptr_t else 0.0
        if (px, py) != self.ptr and self.ptr_t:
            self.ptr_moved_at = t
        if speed > 2200 * s and dist < self.ptr_dist and dist < self.r + 60 * s and t > self.flinch_cool \
                and not (self.hovered or self.drag or self.fling or self.action or self.sleepy):
            self.react("flinch", away=1 if px > scx else -1)       # ducks away from the side it came from
            self.flinch_cool = t + 4
        self.ptr, self.ptr_t, self.ptr_dist = (px, py), t, dist
        # away / back: your mouse and keyboard anywhere, or JARVIS doing something, means you're here
        if state != "idle" or self.drag or self.hovered or self.bar is not None:
            self.present_at = t
        idle = idle_seconds()
        away = min(idle if idle is not None else t - self.last_activity, t - self.present_at)
        if away >= 120:
            self.away_for = max(self.away_for, away)
        elif self.away_for and away < 1.5:
            self._welcome(self.away_for)
            self.away_for = 0.0
        doze = 60 if self.mood == "night" else 120               # nods off sooner late at night
        if state == "idle" and away >= doze and t - self.last_yawn > 10 \
                and not (self.sleepy or self.action or self.alert_text or self.showcase):
            self.react("yawn")                                   # nobody around: a big yawn, then dozes off
            self.last_yawn = t
        # perched on a window: ride along when it moves, hop home when it closes, minimises or maximises
        if self.perch and not (self.action and "to" in self.action) and not (self.drag or self.fling):
            rect = window_rect(self.perch["hwnd"])
            if rect is None or rect[2] - rect[0] < self.perch["off"] + self.w:
                self._leave_perch()
            elif rect != self.perch["rect"]:
                ny = rect[1] - self.h + round(5 * s)
                wa = work_area(rect[0] + self.perch["off"] + self.w / 2, rect[1])
                if wa and ny < wa[1]:
                    self._leave_perch()
                else:
                    if not self.action:
                        self.react("surprised")                  # whoa, going for a ride
                    self.perch["rect"] = rect
                    self._goto(rect[0] + self.perch["off"], ny)
        quiet = state == "idle" and not (self.action or self.alert_text or self.drag or self.fling or self.sleepy
                                         or self.showcase or self.hovered or self.bubble is not None
                                         or self.bar is not None or self.ring is not None)
        if quiet and self.wander.get() and away < 60 and t > self.next_wander:
            self.next_wander = t + random.uniform(30, 70)
            self._wander(t)

    def _wander(self, t: float):
        s = self.s
        hx, hy = self.home
        if self.perch:
            if t > self.perch["until"]:
                self._leave_perch()
            return
        if math.hypot(self.x - hx, self.y - hy) > 6:            # been out a while: back home
            near = abs(self.y - hy) < 4 and abs(self.x - hx) < 120 * s
            self.travel(hx, hy, "walk" if near else "swing_to" if abs(self.y - hy) < 30 * s else "leap")
            return
        roll = random.random()
        if roll < 0.3 and self._try_perch(t):
            return
        if roll > 0.9 and self.mood != "night":                 # now and then: hangs from the top of the screen
            self.hang()
            return
        style, reach = ("walk", random.uniform(50, 110)) if roll < 0.7 else ("swing_to", random.uniform(150, 260))
        side = random.choice((-1, 1))
        for d in (side, -side):                                  # at a screen edge: go the other way
            x, y = self._clamp(hx + d * reach * s, hy)
            if abs(x - hx) > 30 * s:
                self.travel(x, y, style)
                return

    def _try_perch(self, t: float) -> bool:
        """Leap up and sit on the top edge of the window you're using (if it's a normal, not maximised one)."""
        s, hwnd = self.s, foreground_window()
        rect = window_rect(hwnd) if hwnd else None
        if not rect or rect[2] - rect[0] < 320 * s or rect[3] - rect[1] < 200 * s:
            return False
        l, top, r, b = rect
        x = round(l + (r - l) * random.uniform(0.2, 0.55) - self.w / 2)
        y = round(top - self.h + 5 * s)                          # shadow sits right on the window's top edge
        wa = work_area(x + self.w / 2, top)
        if not wa or y < wa[1] or x < wa[0] or x + self.w > wa[2]:
            return False
        self.travel(x, y, "leap", then=lambda: self._perched(hwnd, rect, x - l), clamp=False)
        return True

    def _perched(self, hwnd: int, rect: tuple, off: int):
        self.perch = {"hwnd": hwnd, "rect": rect, "off": off, "until": time.monotonic() - self.t0 + random.uniform(25, 60)}
        self.react("wiggle")

    def _leave_perch(self):
        self.perch = None
        self.travel(*self.home, style="leap")

    def _welcome(self, away: float):
        """You're back after a while: wakes up if dozing, a happy bounce, and a hello after a long break."""
        asleep = self.sleepy
        self.poke()
        if self.alert_text:
            return
        self.root.after(950 if asleep else 0, lambda: self.react("happy"))
        if away >= 300 and self.bubble is None and self.bar is None:
            hello = {"morning": "Good morning!", "evening": "Good evening!",
                     "night": "Still up? Don't stay up too late!"}.get(self.mood) if random.random() < 0.6 else None
            self.root.after(1000 if asleep else 200, lambda: self.bubble is None and self.show_bubble(
                hello or random.choice(GREETINGS), hide_after=3.5))

    def _fling_step(self):
        """Thrown: slides with momentum, rolling as it goes, bounces off the screen edges, then stops (dizzy if it
        was a wild ride). Where it stops becomes home."""
        f, s, now = self.fling, self.s, time.monotonic()
        dt = min(0.05, now - f["t"])
        f["t"] = now
        decay = math.exp(-2.8 * dt)
        f["vx"], f["vy"] = f["vx"] * decay, f["vy"] * decay
        f["fx"] += f["vx"] * dt
        f["fy"] += f["vy"] * dt
        f["dist"] += math.hypot(f["vx"], f["vy"]) * dt
        f["roll"] += math.degrees(f["vx"] * dt / self.r)
        l, t, r, b = self._bounds()
        for ax, lo, hi in (("x", l, r), ("y", t, b)):
            if not lo <= f["f" + ax] <= hi:
                f["f" + ax] = min(max(f["f" + ax], lo), hi)
                f["v" + ax] *= -0.55
                f["bounces"] += 1
                self.spawn("star")
        self._goto(round(f["fx"]), round(f["fy"]))
        if math.hypot(f["vx"], f["vy"]) < 70 * s:
            self.fling = None
            self._set_home()
            self.react("dizzy" if f["dist"] > 1500 * s or f["bounces"] >= 3 else "land")

    def draw(self, t: float, state: str):
        c, s, r = self.c, self.s, self.r
        rnd = round
        self._life(t, state)
        self._travel_step(t)
        # what JARVIS is doing wakes spidey up and sometimes gets a reaction
        if state != self.prev_state:
            if state != "idle":
                self.poke()
            if state == "listening" and self.prev_state in ("idle", "speaking") and not self.action:
                self.react("surprised")                          # perks up when you start talking
            self.prev_state = state
        if self.hovered or self.drag:
            self.last_activity = t
        if self.sleepy and t > self.next_z:
            self.spawn("z")
            self.next_z = t + 1.7
        # an idle spidey does a little something now and then
        if state == "idle" and not self.action and not self.alert_text and not self.drag and not self.sleepy \
                and not self.showcase \
                and t > self.next_idle:
            night = self.mood == "night"
            pick = random.choices(*(self.NIGHT_MOVES if night else self.IDLE_MOVES))[0]
            if pick == "web":                                    # tags the mouse pointer if it's resting nearby
                px, py = self.ptr
                d = math.hypot(px - (self.x + self.cx), py - (self.y + self._rest_cy()))
                if t - self.ptr_moved_at > 1.5 and 90 * s < d < 520 * s and not self.perch:
                    self.shoot_web()
                else:
                    pick = "look"
            if pick == "look":
                self.glance = random.choice([(-0.09, 0.0), (0.09, 0.0)])
                self.next_glance = t + 1.4
            elif pick == "yawn":
                self.react("yawn", awake=True)
            elif pick != "web":
                self.react(pick, n=random.choice((1, 2, 3))) if pick == "hop" else self.react(pick)
            self.next_idle = t + (random.uniform(12, 22) if night else random.uniform(6, 12))
        if state == "alert" and not self.action:
            self.react("hop", n=3)                               # excited hops while a reminder is showing
        f = self._performance(t)
        lift, sy, tilt, mode, grow_extra, blush = f["lift"], f["sy"], f["tilt"], f["mode"], f["grow"], f["blush"]
        if self.drag and self.moved:                             # dangling while dragged: stretched, wide-eyed
            sy, grow_extra, lift, mode = 1.09, 1.2, 0.0, ""
        if self.fling:                                           # thrown: rolls along, eyes wide
            tilt, grow_extra, mode = self.fling["roll"], 1.15, ""
            f["raw_tilt"] = True
        if self.drop_hover:                                      # something's being dragged over it: wide-eyed
            grow_extra, mode, lift = 1.22, "", lift + 2 * s
        if self.hovered and not mode:
            mode, blush = "happy", True
        if self.sleepy and not mode:
            mode = "sleepy"
        if state == "speaking" and not self.action:
            lift += 1.4 * s * abs(math.sin(t * 7.5))             # bobs along while talking
        if f["raw_tilt"]:
            self.tilt_now = tilt                                 # spins follow the curve exactly
        else:
            self.tilt_now += (tilt - self.tilt_now) * 0.35      # extra smoothing so tilts never snap
        self.tilt_now = ((self.tilt_now + 180) % 360) - 180
        tilt = self.tilt_now
        cx = self.cx + rnd(f["dx"])
        # smooth hover (slower when sleepy), plus the action's lift; one whole-pixel offset shared by every part
        period = 5.5 if self.sleepy else 4.6 if self.mood == "night" else 3.8
        hover = (1 - math.cos(t * 2 * math.pi / period)) / 2   # 0 (up) … 1 (down), eased at both ends
        if self.drag and self.moved:
            hover = 0.0
        bottom = rnd(self.top + self.d + hover * self.amp - lift)
        img = self._sphere(sy, tilt)
        if c.itemcget(self.i_sphere, "image") != str(img):
            c.itemconfigure(self.i_sphere, image=img)
        c.coords(self.i_sphere, cx, bottom)
        ksy, ktilt = self._key(sy, tilt)                        # eyes follow the exact pose the image was drawn in
        ry = r * ksy
        cy = bottom - ry
        # web thread for the swing, hanging from the top of the window
        if f["thread"]:
            anchor = self.cx - (self.action["side"] * rnd(9 * s) if self.action and "to" in self.action else 0)
            c.coords(self.i_thread, anchor, 0, cx, rnd(cy - ry * 0.9))
            c.itemconfigure(self.i_thread, state="normal")
        else:
            c.itemconfigure(self.i_thread, state="hidden")
        # shadow: wider as the sphere comes down, smaller while it's up in a hop
        sw = self.rx * (0.62 + 0.22 * hover) * max(0.45, 1 - max(0.0, lift) / (16 * s)) * (1 / math.sqrt(sy))
        shy = self.h - rnd(6 * s)
        c.coords(self.i_shadow, rnd(cx - sw), shy - rnd(2.5 * s), rnd(cx + sw), shy + rnd(2.5 * s))
        c.itemconfigure(self.i_shadow, state="hidden" if f["hang"] else "normal")   # no floor under it up there
        # listening / reminder ring
        if state in ("listening", "alert"):
            c.itemconfigure(self.i_ring, outline=ALERT if state == "alert" else GLOW)
            g = 2 * s + 2 * s * (0.5 + 0.5 * math.sin(t * 5))
            gx = self.dw / 2 / math.sqrt(sy) + g
            c.coords(self.i_ring, rnd(cx - gx), rnd(cy - ry - g), rnd(cx + gx), rnd(cy + ry + g))
            c.itemconfigure(self.i_ring, state="normal")
        else:
            c.itemconfigure(self.i_ring, state="hidden")
        # eyes glance now and then and ease into place
        if state == "thinking":
            target = (0.05, -0.06)
        else:
            if t > self.next_glance:
                self.glance = random.choice([(0, 0), (0, 0), (-0.05, 0), (0.05, 0), (0.03, -0.03)])
                self.next_glance = t + random.uniform(1.8, 4.0)
            target = (self.glance[0] + (tilt / 150 if abs(tilt) < 20 else 0), self.glance[1])
            # watches the mouse while it's moving nearby (unless busy looking somewhere on purpose)
            px, py = self.ptr
            dx, dy = px - (self.x + cx), py - (self.y + cy)
            dist = math.hypot(dx, dy)
            busy = self.action and self.action["name"] in ("peek", "walk", "swing_to", "leap")
            if t - self.ptr_moved_at < 2.5 and 1 < dist < 700 * s and not busy and not self.sleepy:
                k = min(1.0, dist / (90 * s))
                target = (dx / dist * 0.09 * k, dy / dist * 0.07 * k)
        for k in (0, 1):
            self.look[k] += (target[k] - self.look[k]) * 0.12
        lx, ly = rnd(self.look[0] * r), rnd(self.look[1] * r)
        phase = t % 4.2
        open_ = 1.0 if phase > 0.16 else max(0.12, abs(phase - 0.08) / 0.08)   # quick smooth blink
        open_ = min(open_, f["open"])
        grow = 1.0
        if state == "thinking":
            open_ = min(open_, 0.62)                            # suspicious squint
        elif state == "alert":
            grow = 1.16                                         # big surprised lenses
        elif state == "listening":
            grow = 1.08                                         # lenses widen, all ears
        elif state == "speaking":
            grow = 1.0 + 0.07 * math.sin(t * 11)                # lenses flex like the comics while talking
        grow *= grow_extra
        if state != "idle" and mode == "sleepy":
            mode = ""
        fill = HEART if mode == "love" else LENS
        if fill != self.lens_fill:
            self.lens_fill = fill
            for item in self.i_lens:
                c.itemconfigure(item, fill=fill)
        wink_side = self.action["side"] if self.action else 1
        for i, side in enumerate((-1, 1)):
            m = ("happy" if side == wink_side else "") if mode == "wink" else mode
            c.coords(self.i_frame[i], *self._eye_points(side, cx, cy, open_, grow, lx, ly, 0.05, ksy, ktilt, m))
            c.coords(self.i_lens[i], *self._eye_points(side, cx, cy, open_, grow, lx, ly, 0.0, ksy, ktilt, m))
        self._draw_mouth(t, state, cx, cy, ksy, ktilt, mode, f)
        # rosy blush when happy or petted
        for i, side in enumerate((-1, 1)):
            if blush and abs(tilt) < 30:
                bx = cx + side * 0.46 * self.rx
                by = cy + 0.30 * ry
                c.coords(self.i_blush[i], rnd(bx - 0.14 * r), rnd(by - 0.07 * r), rnd(bx + 0.14 * r), rnd(by + 0.07 * r))
                c.itemconfigure(self.i_blush[i], state="normal")
            else:
                c.itemconfigure(self.i_blush[i], state="hidden")
        # thinking dots orbit above the head (sparkly stars when dizzy)
        dizzy = f["dizzy"]
        for i, dot in enumerate(self.i_dots):
            if state == "thinking" or dizzy:
                a = t * (6 if dizzy else 2.6) + i * 2.094
                x, y = cx + r * 0.75 * math.cos(a), cy - ry - r * 0.05 + r * 0.22 * math.sin(a)
                k = max(2, rnd(1.8 * s))
                c.coords(dot, rnd(x) - k, rnd(y) - k, rnd(x) + k, rnd(y) + k)
                c.itemconfigure(dot, state="normal", fill=SPARKLE if dizzy else GLOW)
            else:
                c.itemconfigure(dot, state="hidden")
        self._update_particles()
        if self.warm_list and self.frame % 3 == 0:
            self._prewarm()

    # ---- showcase: every animation once, with a caption ----
    def run_showcase(self):
        """Plays every animation and look once, in order, naming each in the speech bubble (about 70 seconds)."""
        if self.showcase:
            return
        self.showcase = True
        self.dismiss_quietly()
        sleep_on = lambda: setattr(self, "sleepy", True)  # noqa: E731
        hover = lambda on: setattr(self, "hovered", on)    # noqa: E731
        force = lambda st: setattr(self, "force_state", st)  # noqa: E731
        inward = -1 if self.x + self.w / 2 > self.root.winfo_screenwidth() / 2 else 1   # strolls towards the middle
        steps = [
            ("Hi! Here's everything I can do", lambda: self.react("happy"), 2.6),
            ("Hop hop hop", lambda: self.react("hop", n=3), 2.2),
            ("Squishy landing", lambda: self.react("land"), 1.4),
            ("Curious head tilt", lambda: self.react("tilt"), 2.2),
            ("Wiggle", lambda: self.react("wiggle"), 1.8),
            ("Surprised!", lambda: self.react("surprised"), 1.6),
            ("Barrel roll", lambda: self.react("spin"), 1.8),
            ("Web swing", lambda: self.react("swing"), 3.8),
            ("Wink", lambda: self.react("wink"), 1.8),
            ("Blep (tongue out)", lambda: self.react("blep"), 2.2),
            ("Licking lips", lambda: self.react("lick"), 1.8),
            ("Peekaboo, looking around", lambda: self.react("peek"), 3.0),
            ("Nod: got it", lambda: self.react("nod"), 1.6),
            ("Shake: nope", lambda: self.react("shake"), 1.4),
            ("Dizzy (after you swing me around)", lambda: self.react("dizzy"), 2.8),
            ("Heart eyes (say thanks!)", lambda: self.react("love"), 3.0),
            ("Celebrate (reminder done)", lambda: self.react("celebrate"), 2.4),
            ("Happy face when you hover over me", lambda: hover(True), 2.4),
            ("Hover a moment: quick actions (talk, timer, stop, panel)",
             lambda: (hover(False), self.open_ring(pinned=True)), 3.4),
            ("Flinch when you swipe at me", lambda: (self.close_ring(), self.react("flinch", away=1)), 1.6),
            ("Going for a stroll", lambda: self.travel(self.x + inward * 90 * self.s, self.y, "walk"), 3.2),
            ("Web-swinging to a new spot", lambda: self.travel(self.x + inward * 170 * self.s, self.y, "swing_to"), 2.6),
            ("Leaping back home", lambda: self.travel(*self.home, style="leap"), 2.2),
            ("Thwip! Shooting a web at your mouse",
             lambda: self.shoot_web((round(self.x + self.cx + inward * 220 * self.s), round(self.y - 40 * self.s))), 1.8),
            ("Hanging upside down from the top", lambda: self.hang(), 5.5),
            ("…and dropping back home (a tap does this too)", lambda: self._end_hang(), 2.2),
            ("Happy you're back (after a break)", lambda: self._welcome(600), 3.2),
            ("Listening: wide eyes and a glow ring", lambda: (hover(False), force("listening")), 3.0),
            ("Thinking: squint and orbiting dots", lambda: force("thinking"), 3.0),
            ("Speaking: lenses flex, little bobs", lambda: force("speaking"), 3.0),
            ("Reminder pop-up", lambda: (force(""), self.notify("drink some water")), 4.0),
            ("Reminder done: confetti!", lambda: self.dismiss(), 2.4),
            ("Getting sleepy… big yawn", lambda: self.react("yawn"), 2.2),
            ("Sleeping, zzz", lambda: sleep_on(), 3.4),
            ("Waking up!", lambda: self.poke(), 2.0),
            ("Costumes (right-click → Costume): Miles", lambda: self.set_costume("miles"), 2.2),
            ("Black suit", lambda: self.set_costume("black"), 2.2),
            ("Iron Spider", lambda: self.set_costume("iron"), 2.2),
            ("Drop a file or a link on me and I'll open it or sum it up", lambda: (
                self.set_costume(worn, announce=False), self.react("surprised")), 3.4),
            ("That's all! Click me to chat.", lambda: self.react("hop", n=1), 3.0),
        ]
        worn = self.costume.get()

        def play(i: int):
            if i >= len(steps):
                self.showcase = False
                self.force_state = ""
                self.hide_bubble()
                self.next_idle = time.monotonic() - self.t0 + 8
                return
            label, fn, dur = steps[i]
            self.action = None
            fn()
            if not self.alert_text:
                self.show_bubble(f"{label}" if i == 0 or i == len(steps) - 1 else f"{i}/{len(steps) - 2}  {label}",
                                 hide_after=dur)
            self.root.after(int(dur * 1000), lambda: play(i + 1))
        self.root.after(300, lambda: play(0))

    def dismiss_quietly(self):
        self.alert_text = ""
        self.hide_bubble()

    def _test_done(self):
        gaps = [b - a for a, b in zip(self.frame_times, self.frame_times[1:])][30:]
        if gaps:
            print(f"frames: {len(gaps)}  avg {1000 * sum(gaps) / len(gaps):.1f} ms  worst {1000 * max(gaps):.1f} ms")
        self.root.destroy()

    def tick(self):
        t = time.monotonic() - self.t0
        if len(self.frame_times) < 2000:
            self.frame_times.append(time.monotonic())
        if t >= self.next_rem_check:
            self.next_rem_check = t + 1.0
            self.check_reminders()
        self.handle_events()
        while not self.hotkey_q.empty():                  # Right Ctrl tap / Ctrl+Alt+0: open the bar, ready to type
            self.hotkey_q.get_nowait()
            if not self.shown:
                self.root.deiconify()
                self.shown = True
            if self.panel is not None and self.panel.alive:
                self.panel.lift()                         # the panel is open: type there
            elif self.bar is not None:
                self.close_bar()
            else:
                self.poke()
                self.react("hop", n=1)
                self.open_bar()
        if self.chat_active and time.monotonic() - self.chat_sent_at > 1.5 and (
                (self.chat_reply and self.status.state in ("idle", "listening")) or time.monotonic() - self.chat_sent_at > 60):
            self.chat_active = False                      # that turn is over
        online = self.status.online or bool(self.force_state) or bool(self.alert_text) or self.bar is not None
        if online and not self.shown:
            self.root.deiconify()
            self.root.attributes("-topmost", True)
            self.shown = True
        elif not online and self.shown and t > 3:  # give the first status check a moment
            self.root.withdraw()
            self.shown = False
            self.hide_bubble()
            self.close_ring()
        dt, self.last_tick = min(0.1, t - getattr(self, "last_tick", t)), t
        state = "alert" if self.alert_text else (self.force_state or (self.status.state if self.status.online else "idle"))
        if t >= self.pill_next:                          # the status pill changes slowly: 4 checks a second
            self.pill_next = t + 0.25
            self._update_pill(state)
        if self.shown:
            if self.fling:
                self._fling_step()
            self.draw(t, state)
            if self.ring is None and self.hovered and self.hover_since and time.monotonic() - self.hover_since > 0.5 \
                    and not (self.drag or self.fling or self.showcase or self.hanging or (self.action and "to" in self.action)):
                self.open_ring()                         # hovered a moment: quick actions pop out
            self._update_ring()
            self._type_step(dt)
            self._update_web()
        # schedule against the clock so frames stay evenly spaced (~60 fps)
        self.frame += 1
        delay = max(1, int((self.t_start + self.frame / 60 - time.monotonic()) * 1000))
        if delay > 50:  # fell far behind (sleep, drag): resync
            self.t_start, self.frame, delay = time.monotonic(), 0, 16
        self.root.after(delay, self.tick)


def main():
    arg = lambda name: sys.argv[sys.argv.index(name) + 1] if name in sys.argv else ""  # noqa: E731
    test = float(arg("--test") or 0)
    state, demo, chat = arg("--state"), arg("--demo-reminder"), arg("--demo-chat")
    if not test and not single_instance():
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # per-monitor: the highlight ring gets physical screen pixels
        ctypes.windll.winmm.timeBeginPeriod(1)   # 1 ms timer so frames land evenly (Windows default is ~16 ms)
    except Exception:
        pass
    root = tk.Tk()
    root.title("JARVIS bot")
    bot = Bot(root, test_seconds=test, force_state=state)
    if demo:   # try the reminder pop-up without waiting:  --demo-reminder "drink some water"
        root.after(800, lambda: bot.notify(demo))
    if "--showcase" in sys.argv:
        root.after(1500, bot.run_showcase)
    act = arg("--act")
    if act:    # testing: --act happy|hop|tilt|…|peek|sleepy, or ring|welcome|walk|swing_to|leap|perch|fling
        inward = -1 if bot.x > root.winfo_screenwidth() / 2 else 1
        if act == "sleepy":
            root.after(500, lambda: setattr(bot, "sleepy", True))
        elif act == "ring":
            root.after(800, lambda: bot.open_ring(pinned=True))
        elif act == "welcome":
            root.after(800, lambda: bot._welcome(600))
        elif act in ("walk", "swing_to", "leap"):
            root.after(1200, lambda: bot.travel(bot.x + inward * 160 * bot.s, bot.y - (120 * bot.s if act == "leap" else 0),
                                                act, then=lambda: root.after(1500, lambda: bot.travel(*bot.home, style=act))))
        elif act == "perch":
            root.after(1200, lambda: print("perch:", bot._try_perch(0), flush=True))
        elif act == "web":
            root.after(1200, lambda: bot.shoot_web((round(bot.x + bot.cx + inward * 240 * bot.s), round(bot.y - 80 * bot.s))))
        elif act == "hang":
            root.after(1200, bot.hang)
        elif act in COSTUMES:
            root.after(1200, lambda: bot.set_costume(act))
        elif act == "confirm":   # JARVIS asks "shall I…?" and the bubble gets Yes / No buttons
            root.after(1200, lambda: (bot.status.events.put({"type": "transcript", "role": "assistant", "text":
                                      "Send “on my way” to Rahul on WhatsApp?"}),
                                      bot.status.events.put({"type": "confirm", "prompt": "Send it?", "choices": None})))
        elif act == "long":      # a long reply that scrolls, with a link
            bot.chat_active, bot.chat_sent_at = True, time.monotonic() + 1
            text =("Here's what I found. " * 3 + "The Spider-Man page is at https://en.wikipedia.org/wiki/Spider-Man "
                    "and it has the whole history, from 1962's Amazing Fantasy #15 onwards. ") * 3
            root.after(1200, lambda: bot.status.events.put({"type": "transcript", "role": "assistant", "text": text}))
        elif act == "fling":
            def throw():
                bot.fling = {"vx": inward * 2600 * bot.s, "vy": -900 * bot.s, "fx": float(bot.x), "fy": float(bot.y),
                             "t": time.monotonic(), "roll": 0.0, "dist": 0.0, "bounces": 0}
            root.after(1200, throw)
        else:
            root.after(1200, lambda: bot.react(act, n=3))
    drop = arg("--drop")
    if drop:   # testing: as if this (a file path, a link or text) were dropped on spidey
        class Fake:
            data = drop if not os.path.exists(drop) else "{" + drop + "}"
        root.after(1200, lambda: bot._drop(Fake()))
    pick = arg("--pick")
    if pick:   # testing: press bubble button number N (from 0) once the bubble is up
        def press():
            label, fn = bot.bub["choices"][int(pick)]
            print("pressed:", label, flush=True)
            bot.hide_bubble()
            fn()
        root.after(2600, press)
    if chat:   # testing: open the bar, type this and press Enter
        def go():
            bot.open_bar()
            root.after(500, lambda: (bot.entry.insert(0, chat), bot._send()))
        root.after(1500, go)
    if "--panel" in sys.argv:   # started by launch.ps1 / start-jarvis.bat: show JARVIS's panel straight away
        root.after(1200, bot.open_panel)
    root.mainloop()


if __name__ == "__main__":
    main()
