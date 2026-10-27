"""Spidey's mini TV: a small always-on-top window beside spidey that shows anything a web page can, such as a YouTube
video, a live stream, a website, a map, a search or pictures. "play lofi on the TV", "put the news on the TV",
"show a map of Goa on the TV", "make the TV bigger", "pause the TV", "turn off the TV".

It is its own Edge app window with its own clean profile (data/edge-tv): no accounts, the same lockdown as JARVIS's
Edge (no camera, mic, pop-ups, passwords…), and the same site blocklists and watchdog. It never takes the keyboard: it
opens without focus (if Edge grabs it anyway it is handed straight back) and is marked "don't activate", so you keep
typing wherever you were. Turning the TV off closes that Edge entirely, so it uses no memory while off.

Tested 27 Oct: an off-screen probe played a YouTube video filling the window (ad first, as YouTube does). YouTube
blocks <style> tags it didn't write, so the "fill the window" look is a constructed stylesheet, which it allows."""
from __future__ import annotations

import contextlib
import ctypes
import json
import re
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path
from urllib.parse import quote, quote_plus, urlparse

import httpx

from .board import clean_text
from .browser import BrowserError, EdgeController

SIZES = {"small": (384, 216), "medium": (512, 288), "large": (720, 405)}   # the picture (16:9), at 100% scaling
ORDER = ["small", "medium", "large"]
TITLE_BAR = 32                       # Edge's app-window title bar, at 100% scaling
SPIDEY = 64                          # about how much room spidey takes, at 100% scaling
GAP = 10
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"
IDLE_PAGE = "data:text/html;charset=utf-8," + quote(
    "<title>Spidey TV</title><body style='margin:0;background:#000;color:#777;font:14px Segoe UI;display:grid;"
    "place-items:center;height:100vh'>Spidey TV</body>")

# A YouTube video filling the whole window: everything but the player hidden, the player stretched over the page.
# Re-applied every 0.7 s, so the next video (YouTube changes pages without reloading) looks the same.
_FILL_CSS = ("html,body{overflow:hidden!important;background:#000!important}"
             "#masthead-container,#secondary,#below,#chat,tp-yt-app-drawer,ytd-miniplayer{display:none!important}"
             "#movie_player{position:fixed!important;inset:0!important;width:100vw!important;height:100vh!important;"
             "z-index:2147483647!important;background:#000!important}"
             "#movie_player video{width:100vw!important;height:100vh!important;left:0!important;top:0!important;"
             "object-fit:contain!important}")
JS_FILL = ("(()=>{if(window.__jtv||!location.hostname.endsWith('youtube.com'))return true;window.__jtv=1;let sheet=null;"
           "const add=()=>{if(!location.pathname.startsWith('/watch'))return;"
           "if(!sheet){sheet=new CSSStyleSheet();sheet.replaceSync(%s)}"
           "if(!document.adoptedStyleSheets.includes(sheet)){document.adoptedStyleSheets=[...document.adoptedStyleSheets,"
           "sheet];setTimeout(()=>dispatchEvent(new Event('resize')),300)}};add();setInterval(add,700);return true})()"
           ) % json.dumps(_FILL_CSS)
# pause / play / mute / volume on whatever is playing (the page's own video or audio element)
JS_MEDIA = """(a => { const m = [...document.querySelectorAll('video, audio')].find(e => e.readyState > 0) ||
  document.querySelector('video, audio'); if (!m) return 'none';
  if (a === 'pause') m.pause(); else if (a === 'play') m.play();
  else if (a === 'mute') m.muted = true; else if (a === 'unmute') m.muted = false;
  else if (a === 'louder') { m.muted = false; m.volume = Math.min(1, m.volume + 0.2); }
  else if (a === 'quieter') m.volume = Math.max(0.05, m.volume - 0.2);
  return JSON.stringify({paused: m.paused, muted: m.muted, volume: Math.round(m.volume * 100)}); })(%s)"""

# ---- what to put on: a link or a site, a map, pictures, a search, or else the top YouTube video ----
_URL = re.compile(r"^(?:https?://\S+|(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/\S*)?)$", re.I)
_MAP = re.compile(r"^(?:(?:a|the)\s+)?(?:google\s+)?maps?\s+(?:of|for)\s+(?P<a>.+)$|^(?P<b>.+?)\s+on\s+(?:a|the)\s+map$")
_PICS = re.compile(r"^(?:(?:a|some|the)\s+)?(?:pictures?|pics?|photos?|images?)\s+of\s+(?:(?:a|an|the|some)\s+)?(?P<q>.+)$")
_SEARCH = re.compile(r"^(?:(?:a|the)\s+)?(?:google|search|web search|search results)\s+(?:for\s+|of\s+)?(?P<q>.+)$")
_VIDEO_WORDS = re.compile(r"^(?:(?:a|an|some|the)\s+)?(?:(?:youtube\s+)?videos?|clips?)\s+(?:of|about|on)\s+")
THIS = re.compile(r"^(?:it|this|that|this page|that page|this video|that video|the page|the video|what i'?m watching|"
                  r"what'?s open)$")


def plan(q: str, resolve_site=None) -> dict | None:
    """What "put <q> on the TV" means: {"kind": "page", "url", "label"} or {"kind": "video", "query", "label"}."""
    q = re.sub(r"\s+", " ", (q or "")).strip(" '\"“”.,!?")
    if not q or len(q) > 300:
        return None
    if _URL.match(q):
        url = q if re.match(r"^https?://", q, re.I) else f"https://{q}"
        return {"kind": "page", "url": url, "label": urlparse(url).hostname or url}
    low = q.lower()
    if (m := _MAP.match(low)):
        place = (m.group("a") or m.group("b")).strip()
        return {"kind": "page", "url": f"https://www.google.com/maps/search/{quote_plus(place)}", "label": f"a map of {place}"}
    if (m := _PICS.match(low)):
        return {"kind": "page", "url": f"https://www.google.com/search?tbm=isch&q={quote_plus(m.group('q'))}",
                "label": f"pictures of {m.group('q')}"}
    if (m := _SEARCH.match(low)):
        return {"kind": "page", "url": f"https://www.google.com/search?q={quote_plus(m.group('q'))}",
                "label": f"a search for {m.group('q')}"}
    site = resolve_site(low) if resolve_site and len(low) <= 40 else None
    if site:
        return {"kind": "page", "url": site[1], "label": site[0]}
    low = re.sub(r"\s+(?:on|from|off)\s+(?:youtube|you tube)$", "", _VIDEO_WORDS.sub("", low)).strip()
    return {"kind": "video", "query": low, "label": low} if low else None


_VIDEO_ID = re.compile(r'"videoRenderer":\{"videoId":"([\w-]{11})"')
_VIDEO_TITLE = re.compile(r'"title":\{"runs":\[\{"text":"((?:[^"\\]|\\.){1,300})"')


def find_video(query: str, client: httpx.Client | None = None) -> dict | None:
    """The top YouTube result for the words (only the words go out). The results page is data: only an 11-character
    video id and a cleaned-up title are taken from it."""
    c = client or httpx.Client(timeout=8.0)
    text, m = "", None
    try:
        with c.stream("GET", "https://www.youtube.com/results", params={"search_query": query[:150]},
                      headers={"user-agent": UA, "accept-language": "en-IN,en;q=0.8"}) as r:
            r.raise_for_status()
            for chunk in r.iter_text():          # ~2.6 MB in all; stop once the first video and its title are in
                text += chunk
                m = m or _VIDEO_ID.search(text, max(0, len(text) - len(chunk) - 64))    # only the new part
                if (m and len(text) > m.end() + 4000) or len(text) > 4_000_000:
                    break
    finally:
        if client is None:
            c.close()
    m = m or _VIDEO_ID.search(text)
    if not m:
        return None
    t = _VIDEO_TITLE.search(text, m.end(), m.end() + 4000)
    title = ""
    if t:
        try:
            title = json.loads(f'"{t.group(1)}"')
        except ValueError:
            title = t.group(1)
    return {"id": m.group(1), "url": f"https://www.youtube.com/watch?v={m.group(1)}", "title": clean_text(title, 90)}


def tv_rect(work: tuple[int, int, int, int], scale: float, size: str, spidey: tuple[int, int] | None = None,
            anchor: tuple[int, int, int, int] | None = None) -> tuple[int, int, int, int]:
    """Where the TV window goes (x, y, w, h), all in screen pixels. anchor = the window it is now (left, top, right,
    bottom): when it grows or shrinks, the corner nearest the screen's corner stays put, so it grows away from the
    edges (and never over spidey). Otherwise just above spidey (below it when there's no room), lined up with its
    outer side; otherwise the bottom-right corner of the screen."""
    l, t, r, b = work
    pw, ph = SIZES.get(size, SIZES["medium"])
    w, h = int(pw * scale), int((ph + TITLE_BAR) * scale)
    gap = int(GAP * scale)
    if anchor:
        al, at, ar, ab = anchor
        x = ar - w if (al + ar) / 2 > (l + r) / 2 else al
        y = ab - h if (at + ab) / 2 > (t + b) / 2 else at
    elif spidey:
        sx, sy = spidey
        sw = int(SPIDEY * scale)
        x = sx if sx + sw / 2 < (l + r) / 2 else sx + sw - w      # on spidey's side of the screen
        y = sy - h - gap
        if y < t:
            y = sy + sw + gap                                      # no room above: below it
    else:
        x, y = r - w - gap, b - h - gap
    x = max(l, min(x, r - w))
    y = max(t, min(y, b - h))
    return x, y, w, h


# ---------- Windows ----------
def _u32():
    return ctypes.windll.user32


@contextlib.contextmanager
def _physical_pixels():
    """This thread talks in real screen pixels (spidey and Edge both do), whatever the rest of JARVIS does."""
    old = None
    try:
        old = _u32().SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))    # per-monitor v2
    except Exception:
        pass
    try:
        yield
    finally:
        if old:
            try:
                _u32().SetThreadDpiAwarenessContext(ctypes.c_void_p(old))
            except Exception:
                pass


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD)]


def _work_area(point: tuple[int, int]) -> tuple[int, int, int, int]:
    u = _u32()
    u.MonitorFromPoint.restype = ctypes.c_void_p
    mon = u.MonitorFromPoint(wintypes.POINT(int(point[0]), int(point[1])), 2)   # nearest monitor
    mi = _MONITORINFO()
    mi.cbSize = ctypes.sizeof(mi)
    if mon and u.GetMonitorInfoW(ctypes.c_void_p(mon), ctypes.byref(mi)):
        w = mi.rcWork
        return w.left, w.top, w.right, w.bottom
    return 0, 0, u.GetSystemMetrics(0), u.GetSystemMetrics(1)


def _give_focus_back(prev: int) -> None:
    """Edge took the keyboard when its window appeared: put it back where you were. Nothing is typed or clicked;
    this only borrows the input state of the window that has the focus now, which Windows needs for the switch."""
    u = _u32()
    now = u.GetForegroundWindow()
    if not prev or now == prev or not u.IsWindow(prev):
        return
    mine = ctypes.windll.kernel32.GetCurrentThreadId()
    theirs = u.GetWindowThreadProcessId(now, None)
    attached = bool(theirs) and theirs != mine and u.AttachThreadInput(mine, theirs, True)
    try:
        u.SetForegroundWindow(prev)
    finally:
        if attached:
            u.AttachThreadInput(mine, theirs, False)


class MiniTV(EdgeController):
    FEATURES_OFF = EdgeController.FEATURES_OFF + ",CalculateNativeWinOcclusion"   # keeps playing when covered
    EX_NOACTIVATE = 0x08000000 | 0x80          # WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW: never takes the keyboard

    def __init__(self, profile_dir: Path, guard=None, spidey_file: Path | None = None, size: str = "medium",
                 edge_path: str | None = None):
        super().__init__(profile_dir, 0, edge_path, guard)
        self.spidey_file = spidey_file
        self.size = size if size in SIZES else "medium"
        self.hwnd = self._pid = 0
        self.showing: dict = {}                # {"kind", "title", "url"} of what's on
        self._busy = threading.RLock()         # one start at a time (warm_up and show may overlap)
        self._kept = ""                        # the page the keeper connection is holding ("" = none)

    # ---- the window ----
    def _window_args(self) -> list[str]:
        rect = self._rect()
        scale = rect[4] if rect else 1.0
        x, y, w, h = (rect[:4] if rect else (0, 0, 512, 320))
        return ["--autoplay-policy=no-user-gesture-required",          # it starts playing as soon as it's on
                f"--window-position={int(x / scale)},{int(y / scale)}", f"--window-size={int(w / scale)},{int(h / scale)}",
                f"--app={IDLE_PAGE}"]

    def _spidey(self) -> tuple[int, int] | None:
        try:
            p = json.loads(Path(self.spidey_file).read_text(encoding="utf-8"))
            return int(p["x"]), int(p["y"])
        except Exception:
            return None

    def _rect(self, keep_place: bool = False) -> tuple[int, int, int, int, float] | None:
        if sys.platform != "win32":
            return None
        with _physical_pixels():
            u = _u32()
            anchor = None
            if keep_place and self.hwnd and u.IsWindow(self.hwnd):
                r = wintypes.RECT()
                u.GetWindowRect(self.hwnd, ctypes.byref(r))
                anchor = (r.left, r.top, r.right, r.bottom)
            spidey = self._spidey()
            ref = (anchor[2], anchor[3]) if anchor else spidey or (u.GetSystemMetrics(0) - 1, u.GetSystemMetrics(1) - 1)
            work = _work_area(ref)
            try:
                scale = u.GetDpiForSystem() / 96
            except Exception:
                scale = 1.0
            return (*tv_rect(work, scale, self.size, spidey, anchor), scale)

    def _main_pid(self) -> int:
        """The TV's Edge browser process (not its helpers), remembered while it lives."""
        try:
            import psutil
            if self._pid and psutil.pid_exists(self._pid):
                return self._pid
            marker = str(self.profile_dir).lower()
            for p in psutil.process_iter(["name", "cmdline"]):
                cmd = p.info["cmdline"] or []
                if (p.info["name"] or "").lower() == "msedge.exe" and marker in " ".join(cmd).lower() \
                        and not any(a.startswith("--type=") for a in cmd):
                    self._pid = p.pid
                    return p.pid
        except Exception:
            pass
        self._pid = 0
        return 0

    def window(self) -> int:
        """The TV's window (0 = none): a visible top-level window of the TV's own Edge."""
        if sys.platform != "win32":
            return 0
        u = _u32()
        if self.hwnd and u.IsWindow(self.hwnd) and u.IsWindowVisible(self.hwnd):
            return self.hwnd
        pid = self._main_pid()
        if not pid:
            return 0
        found: list[int] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def each(h, _):
            owner = wintypes.DWORD()
            u.GetWindowThreadProcessId(h, ctypes.byref(owner))
            if owner.value == pid and u.IsWindowVisible(h) and not u.GetWindow(h, 4):    # no owner = a main window
                found.append(h)
            return True
        u.EnumWindows(each, 0)
        self.hwnd = found[0] if found else 0
        return self.hwnd

    def _hold_focus(self, prev: int, until: float) -> None:
        """While the TV starts: mark its window "don't activate" the moment it exists, and if Edge took the keyboard
        anyway, hand it straight back (checked every 30 ms)."""
        u = _u32()
        marked = 0
        while time.monotonic() < until:
            h = self.window()
            if h:
                if h != marked:
                    u.SetWindowLongPtrW(h, -20, u.GetWindowLongPtrW(h, -20) | self.EX_NOACTIVATE)   # GWL_EXSTYLE
                    marked = h
                if u.GetForegroundWindow() == h:
                    _give_focus_back(prev)
            time.sleep(0.03)

    def _fit(self, keep_place: bool = False) -> None:
        """Size it, put it beside spidey (or where you dragged it) and keep it above other windows, without focus."""
        h = self.window()
        rect = self._rect(keep_place) if h else None
        if not rect:
            return
        with _physical_pixels():
            u = _u32()
            u.SetWindowLongPtrW(h, -20, u.GetWindowLongPtrW(h, -20) | self.EX_NOACTIVATE)
            x, y, w, hh, _ = rect
            # HWND_TOPMOST; SWP_NOACTIVATE | SWP_SHOWWINDOW
            u.SetWindowPos(h, ctypes.c_void_p(-1), x, y, w, hh, 0x0010 | 0x0040)

    def ensure_running(self) -> None:
        if self.running():
            if self.window():
                return
            self.close_browser()               # you closed the TV window: start it clean
            time.sleep(0.5)
        self.hwnd = self._pid = 0
        if sys.platform != "win32":
            return super().ensure_running()
        prev = _u32().GetForegroundWindow()
        until = time.monotonic() + 8.0
        holder = threading.Thread(target=self._hold_focus, args=(prev, until), daemon=True, name="tv-focus")
        holder.start()
        super().ensure_running()
        for _ in range(50):                    # the window appears a moment after Edge answers
            if self.window():
                break
            time.sleep(0.1)
        self._fit()

    def warm_up(self) -> None:
        """Open the TV window while JARVIS is still looking for the video (saves ~2 s)."""
        with self._busy:
            self.ensure_running()

    # ---- what's on ----
    def _page(self) -> dict:
        pages = [t for t in self._http("/json/list") if t.get("type") == "page"]
        if not pages:
            raise BrowserError("The TV window isn't there.")
        return pages[0]

    def show(self, url: str, video: bool = False, title: str = "") -> str:
        """Put a page on the TV (turning it on if needed). A YouTube video fills the whole window."""
        self.check_url(url)                    # blocked sites never load, here as in JARVIS's Edge
        with self._busy:
            self.ensure_running()
            page = self._page()
            kept = self._keep(page)
            want = urlparse(url).hostname or ""
            # Once more if the first didn't take: a YouTube page still starting up can cancel a navigation that
            # arrives in its first second or so (seen 27 Oct: the map didn't come up; the second try took 0.3 s).
            for wait in (3.0, 8.0):
                self._cdp(page, [("Page.navigate", {"url": url})])
                if self._wait_for(page, want, wait):
                    break
            if video and not kept:             # no keeper: style this page once, without waiting long on a busy page
                with contextlib.suppress(Exception):
                    self._cdp(page, [("Runtime.evaluate", {"expression": JS_FILL})], timeout=2.0)
            self.showing = {"kind": "video" if video else "page", "url": url,
                            "title": clean_text(title, 90) or self._title(page) or want}
            return self.showing["title"]

    def _keep(self, page: dict) -> bool:
        """Hold one control connection to the TV page while it's on, with the fill-the-window look registered to run
        on every new page in it. Edge then puts it back by itself after YouTube reloads itself (it does on a fresh
        profile: "&themeRefresh=1") or moves on to the next video. Only JS_FILL is ever registered; it changes how a
        YouTube page looks and nothing else. True once Edge has it."""
        if self._kept == page.get("id"):
            return True
        from websockets.sync.client import connect
        ready = threading.Event()

        def hold():
            try:
                with connect(page["webSocketDebuggerUrl"], max_size=20_000_000, open_timeout=4) as ws:
                    ws.send(json.dumps({"id": 0, "method": "Page.enable", "params": {}}))
                    ws.send(json.dumps({"id": 1, "method": "Page.addScriptToEvaluateOnNewDocument",
                                        "params": {"source": JS_FILL}}))
                    while True:
                        msg = json.loads(ws.recv(timeout=5.0))
                        if msg.get("id") == 1:
                            break
                    self._kept = page.get("id", "")
                    ready.set()
                    while self._kept == page.get("id"):
                        with contextlib.suppress(TimeoutError):
                            ws.recv(timeout=1.0)   # nothing to read (no events were asked for): just stay connected
            except Exception:
                pass                           # Edge closed (TV off) or the page went away
            finally:
                if self._kept == page.get("id"):
                    self._kept = ""
                ready.set()
        threading.Thread(target=hold, daemon=True, name="tv-keeper").start()
        ready.wait(6.0)
        return self._kept == page.get("id")

    def _wait_for(self, page: dict, host: str, seconds: float = 8.0) -> bool:
        """Until the TV is really on that site. Asked of Edge itself, not the page: a busy page (YouTube still loading
        its scripts) can leave a question to the page unanswered for seconds. And not "finished loading" either:
        YouTube stays "loading" for 10 s or more."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            time.sleep(0.25)
            try:
                url = next((t.get("url", "") for t in self._http("/json/list") if t.get("id") == page.get("id")), "")
            except Exception:
                continue
            if (urlparse(url).hostname or "").endswith(host):
                return True
        return False

    def _title(self, page: dict) -> str:
        try:
            t = next((p.get("title", "") for p in self._http("/json/list") if p.get("id") == page.get("id")), "")
        except Exception:
            t = ""
        return clean_text(re.sub(r"\s+-\s+YouTube$", "", t), 90)

    def media(self, action: str) -> dict | None:
        """pause / play / mute / unmute / louder / quieter. None = nothing is playing on the TV."""
        if not self.on():
            return None
        page = self._page()
        out = self._eval(page, JS_MEDIA % json.dumps(action))
        return json.loads(out) if out and out != "none" else None

    def resize(self, step: int | str) -> str:
        """"bigger" / "smaller" (or a size name). It keeps its corner, so it grows away from the screen edge."""
        if isinstance(step, str):
            self.size = step if step in SIZES else self.size
        else:
            self.size = ORDER[max(0, min(len(ORDER) - 1, ORDER.index(self.size) + step))]
        if self.on():
            self._fit(keep_place=True)
        return self.size

    def on(self) -> bool:
        """The window is up and Edge answers (given 2 s: it's slow to answer while YouTube is loading)."""
        if not self.window():
            return False
        try:
            self._http("/json/version", timeout=2.0)
            return True
        except Exception:
            return False

    def now_showing(self) -> str:
        if not self.on():
            return ""
        try:
            return self._title(self._page()) or self.showing.get("title", "")
        except Exception:
            return self.showing.get("title", "")

    def off(self) -> None:
        self._watching = False                 # the site watchdog only runs while the TV is on
        self._kept = ""                        # lets the keeper connection go
        self.close_browser()
        self.hwnd = self._pid = 0
        self.showing = {}
