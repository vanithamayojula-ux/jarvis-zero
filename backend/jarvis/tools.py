"""Explicit, allowlisted tools. No tool can run an arbitrary shell command."""
from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import quote, quote_plus, urlparse

from .browser import MESSAGING_HOSTS, PUBLISH_WORDS, BrowserError
from .config import DATA_DIR, Settings


@dataclass
class ToolResult:
    text: str                                  # what the model is told
    needs_confirmation: bool = False
    confirm_prompt: str = ""                   # what JARVIS asks the user
    run_confirmed: Callable[[], ToolResult] | None = None
    choices: list[tuple[str, Callable[[], ToolResult]]] | None = None   # "which one?" options

# Never opened directly: these run code. Apps go through approved_apps or Desktop shortcuts instead.
BLOCKED_EXTENSIONS = {".exe", ".bat", ".cmd", ".ps1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".msi", ".scr",
                      ".com", ".jar", ".reg", ".hta", ".cpl", ".lnk", ".url", ".psm1", ".pif"}


DOC_TYPES = {"pdf": {"pdf"}, "word": {"doc", "docx"}, "docx": {"doc", "docx"}, "excel": {"xls", "xlsx", "csv"},
             "spreadsheet": {"xls", "xlsx", "csv"}, "powerpoint": {"ppt", "pptx"}, "slides": {"ppt", "pptx"},
             "photo": {"jpg", "jpeg", "png", "heic", "webp"}, "image": {"jpg", "jpeg", "png", "webp", "gif"},
             "video": {"mp4", "mkv", "mov", "avi"}, "zip": {"zip", "rar", "7z"}, "text": {"txt", "md"}}
CODE_EXTENSIONS = {".ts", ".tsx", ".js", ".jsx", ".py", ".json", ".css", ".scss", ".html", ".map", ".java", ".c",
                   ".cpp", ".h", ".go", ".rs", ".lua", ".dat", ".lock", ".yml", ".yaml", ".toml", ".xml", ".log"}
PERSONAL_DOC_EXTENSIONS = {".pdf", ".doc", ".docx", ".odt", ".rtf", ".txt", ".png", ".jpg", ".jpeg", ".ppt", ".pptx", ".xls", ".xlsx"}
SKIP_DIRS = {"node_modules", "__pycache__", ".git", "venv", ".venv", "dist", "build", "site-packages", "appdata"}


SEARCH_URLS = {
    "google": "https://www.google.com/search?q={q}",
    "youtube": "https://www.youtube.com/results?search_query={q}",
    "amazon": "https://www.amazon.in/s?k={q}",
    "flipkart": "https://www.flipkart.com/search?q={q}",
    "wikipedia": "https://en.wikipedia.org/w/index.php?search={q}",
    "github": "https://github.com/search?q={q}",
    "spotify web": "https://open.spotify.com/search/{q}",
    "google maps": "https://www.google.com/maps/search/{q}",
    "reddit": "https://www.reddit.com/search/?q={q}",
    "instagram": "https://www.instagram.com/explore/search/keyword/?q={q}",
    "swiggy": "https://www.swiggy.com/search?query={q}",
}

MEDIA_HOSTS = ("youtube.com", "netflix.com", "spotify.com", "hotstar.com", "primevideo.com", "twitch.tv")

# Other names people (or speech recognition) use for apps.
APP_ALIASES = {"chroma": "chrome", "crome": "chrome", "google chrome": "chrome", "chrome browser": "chrome",
               "chrome bed": "chrome", "microsoft edge": "edge", "edge browser": "edge", "visual studio code": "vs code",
               "vscode": "vs code", "v s code": "vs code", "file manager": "file explorer", "explorer": "file explorer",
               "my files": "file explorer", "calc": "calculator", "notes": "notepad", "note pad": "notepad"}

# Web apps people ask for by name. "aliases" include how speech recognition commonly mishears them.
WEB_APPS: dict[str, dict] = {
    "whatsapp": {"name": "WhatsApp", "url": "https://web.whatsapp.com",
                 "aliases": ["whats app", "what's app", "watsapp", "whatapp", "watch up", "watchup", "whatsup",
                             "whats up web", "what sap", "watts app", "whatsapp web", "watchup web", "watch up web"]},
    "youtube": {"name": "YouTube", "url": "https://www.youtube.com", "aliases": ["you tube", "utube"]},
    "gmail": {"name": "Gmail", "url": "https://mail.google.com", "aliases": ["g mail", "google mail", "email", "my email", "mail"]},
    "google": {"name": "Google", "url": "https://www.google.com", "aliases": []},
    "instagram": {"name": "Instagram", "url": "https://www.instagram.com", "aliases": ["insta"]},
    "facebook": {"name": "Facebook", "url": "https://www.facebook.com", "aliases": ["fb"]},
    "linkedin": {"name": "LinkedIn", "url": "https://www.linkedin.com", "aliases": ["linked in"]},
    "twitter": {"name": "X", "url": "https://x.com", "aliases": ["x", "twitter x"]},
    "chatgpt": {"name": "ChatGPT", "url": "https://chatgpt.com", "aliases": ["chat gpt", "chat g p t"]},
    "claude": {"name": "Claude", "url": "https://claude.ai", "aliases": ["claude ai"]},
    "github": {"name": "GitHub", "url": "https://github.com", "aliases": ["git hub"]},
    "spotify web": {"name": "Spotify", "url": "https://open.spotify.com", "aliases": []},
    "netflix": {"name": "Netflix", "url": "https://www.netflix.com", "aliases": []},
    "amazon": {"name": "Amazon", "url": "https://www.amazon.in", "aliases": []},
    "flipkart": {"name": "Flipkart", "url": "https://www.flipkart.com", "aliases": ["flip kart"]},
    "google maps": {"name": "Google Maps", "url": "https://maps.google.com", "aliases": ["maps"]},
    "google drive": {"name": "Google Drive", "url": "https://drive.google.com", "aliases": ["drive", "my drive"]},
    "google calendar": {"name": "Google Calendar", "url": "https://calendar.google.com", "aliases": ["calendar"]},
    "google meet": {"name": "Google Meet", "url": "https://meet.google.com", "aliases": ["meet"]},
    "telegram": {"name": "Telegram", "url": "https://web.telegram.org", "aliases": []},
    "reddit": {"name": "Reddit", "url": "https://www.reddit.com", "aliases": ["read it"]},
    "wikipedia": {"name": "Wikipedia", "url": "https://www.wikipedia.org", "aliases": []},
    "leetcode": {"name": "LeetCode", "url": "https://leetcode.com", "aliases": ["leet code"]},
    "hotstar": {"name": "Hotstar", "url": "https://www.hotstar.com", "aliases": ["jio hotstar", "disney hotstar"]},
    "swiggy": {"name": "Swiggy", "url": "https://www.swiggy.com", "aliases": ["swigy"]},
    "zomato": {"name": "Zomato", "url": "https://www.zomato.com", "aliases": []},
}


def resolve_web_app(text: str) -> str | None:
    """'watch up web' → 'whatsapp'. Exact names/aliases first, then a close fuzzy match on the squashed text."""
    t = normalize_name(text)
    t = re.sub(r"\b(the|my|website|site|web site|page|app|application|please|of|up)\b", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return None
    for key, app in WEB_APPS.items():
        if t == key or t in app["aliases"] or t == normalize_name(app["name"]):
            return key
    squashed = t.replace(" ", "")
    best, score = None, 0.0
    for key, app in WEB_APPS.items():
        for cand in [key, *app["aliases"]]:
            c = cand.replace(" ", "")
            if len(c) < 5:
                continue
            r = difflib.SequenceMatcher(None, squashed, c).ratio()
            if r > score:
                best, score = key, r
    return best if score >= 0.82 else None


def spoken_when(due: datetime, now: datetime | None = None) -> str:
    """'at 7:13 PM today' / 'tomorrow at 9 AM' / 'on Friday at 6:30 PM' / 'in 5 minutes'."""
    now = now or datetime.now()
    secs = (due - now).total_seconds()
    if 0 < secs < 3630:   # "in an hour" said a moment ago is still "in an hour"
        mins = max(1, round(secs / 60))
        if mins >= 59:
            return "in an hour"
        return "in half an hour" if mins == 30 else f"in {mins} minute{'s' if mins != 1 else ''}"
    clock = due.strftime("%I:%M %p").lstrip("0").replace(":00 ", " ")
    days = (due.date() - now.date()).days
    if days == 0:
        return f"at {clock} today"
    if days == 1:
        return f"tomorrow at {clock}"
    return f"on {due.strftime('%A')} at {clock}"


def speak_command_output(name: str, out: str) -> str:
    """Turn raw command output into one natural sentence."""
    if name == "ip address":
        ips = [ip for ip in re.findall(r"IPv4[^:]*:\s*([\d.]+)", out) if not ip.startswith("169.254")]
        return f"Your IP address is {ips[0]}." if len(ips) == 1 else \
            (f"You've got a few: {', '.join(ips[:3])}." if ips else "I couldn't find an IP address. Are you connected?")
    if name == "disk space":
        rows = re.findall(r"^\s*([A-Z])\s+(\d+)\s+(\d+)\s*$", out, re.M)
        if rows:
            parts = [f"{d} drive has {int(free) / 1e9:.0f} gigabytes free" for d, used, free in rows if int(free) + int(used) > 0]
            return "Your " + " and ".join(parts[:3]) + "."
    if name == "flush dns":
        return "Done, the DNS cache is flushed."
    if name == "lock screen":
        return "Locking the screen."
    return f"Done. {out.strip().splitlines()[0][:120]}" if out.strip() else "Done."


def display_name(p: Path) -> str:
    """How to say a file name out loud: 'RUN_VISIBLE_BROWSER.bat' → 'RUN VISIBLE BROWSER'."""
    name = p.name if p.is_dir() else p.stem
    if name == "ドキュメント":                    # OneDrive's Japanese-locale Documents folder
        return "Documents"
    return re.sub(r"\s+", " ", re.sub(r"[_]+", " ", name)).strip()


def normalize_name(name: str) -> str:
    """'Blender 5.2' → 'blender', 'VaVi Lantern · Eternal Flame' → 'vavi lantern eternal flame'."""
    n = re.sub(r"\b\d+(\.\d+)*\b", " ", name.lower())
    n = re.sub(r"[^a-z0-9+#]+", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def _where(path: Path, roots: list[str]) -> str:
    for r in roots:
        try:
            rel = path.parent.relative_to(r)
            label = Path(r).name if Path(r).name.lower() != "ドキュメント" else "Documents"
            return label if str(rel) == "." else f"{label}, inside {rel.parts[0]}"
        except ValueError:
            continue
    return path.parent.name


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable[[dict], ToolResult]

    def schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters}}


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


# ---------------- reminders ----------------

class Reminders:
    """Local reminders stored in data/reminders.json; a background thread fires them."""

    def __init__(self, path: Path = DATA_DIR / "reminders.json"):
        self.path = path
        self._lock = threading.Lock()
        self.on_due: Callable[[dict], None] = lambda r: None
        self.items: list[dict] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        self._stop = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.items, indent=2), encoding="utf-8")

    def add(self, text: str, due: datetime) -> dict:
        item = {"id": uuid.uuid4().hex[:8], "text": text, "due": due.isoformat(timespec="seconds"), "done": False}
        with self._lock:
            self.items.append(item)
            self._save()
        return item

    def pending(self) -> list[dict]:
        with self._lock:
            return [r for r in self.items if not r["done"]]

    def _loop(self) -> None:
        while not self._stop.wait(5.0):
            now = datetime.now()
            fired = []
            with self._lock:
                for r in self.items:
                    if not r["done"] and datetime.fromisoformat(r["due"]) <= now:
                        r["done"] = True
                        fired.append(r)
                if fired:
                    self._save()
            for r in fired:
                self.on_due(r)

    def stop(self) -> None:
        self._stop.set()


_WORD_NUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
             "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "twenty five": 25,
             "thirty": 30, "forty": 40, "forty five": 45, "fifty": 50, "sixty": 60, "ninety": 90,
             "a couple of": 2, "couple of": 2, "a few": 3, "few": 3}


def parse_when(when: str, now: datetime | None = None) -> datetime | None:
    """Understands 'in 10 minutes', 'in an hour', 'at 3:30 pm', '15:45', 'tomorrow at 9'."""
    now = now or datetime.now()
    s = when.lower().strip()
    # "in like half an hour", "at around 5", "in about 10 minutes": the filler isn't part of the time
    s = re.sub(r"\b(in|at)\s+(?:like|about|around|roughly|approximately|maybe|say|just)\s+", r"\1 ", s)
    # "in half an hour" is 30 minutes, not 30 hours; "an hour and a half" is 90 minutes.
    s = re.sub(r"\b(?:an?|one)\s+hour\s+and\s+a\s+half\b", "90 minutes", s)
    s = re.sub(r"\bhalf\s+(?:an\s+)?hour\b", "30 minutes", s)
    s = re.sub(r"\b(?:a\s+)?quarter\s+(?:of\s+)?(?:an\s+)?hour\b", "15 minutes", s)
    for word, n in sorted(_WORD_NUM.items(), key=lambda kv: -len(kv[0])):
        s = re.sub(rf"\b{word}\b", str(n), s)
    m = re.search(r"in\s+(\d+)\s*(second|sec|minute|min|hour|hr|day)s?", s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"second": timedelta(seconds=n), "sec": timedelta(seconds=n), "minute": timedelta(minutes=n),
                 "min": timedelta(minutes=n), "hour": timedelta(hours=n), "hr": timedelta(hours=n),
                 "day": timedelta(days=n)}[unit]
        return now + delta
    m = re.search(r"(\d{1,2})(?::|\.)?(\d{2})?\s*(am|pm|a\.m\.|p\.m\.)?", s)
    if m and ("at" in s or ":" in s or m.group(3)):
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        ampm = (m.group(3) or "").replace(".", "")
        if ampm == "pm" and hour < 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
        if hour > 23 or minute > 59:
            return None
        if not ampm and 1 <= hour < 12 and re.search(r"\b(?:evening|night|afternoon|tonight)\b", s):
            hour += 12
        due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if not ampm and 1 <= hour < 12 and due <= now and "tomorrow" not in s and not re.search(r"\bmorning\b", s) \
                and due + timedelta(hours=12) > now:
            due += timedelta(hours=12)          # "at 6:30" said at 2 PM means this evening, not tomorrow morning
        if "tomorrow" in s or due <= now:
            due += timedelta(days=1)
        return due
    return None


# ---------------- registry ----------------

class ToolBox:
    def __init__(self, settings: Settings, reminders: Reminders,
                 summarize: Callable[[], Awaitable[str]] | None = None,
                 launcher: Callable[[list[str]], None] | None = None,
                 browser: Callable[[str], None] | None = None,
                 runner: Callable[[list[str]], str] | None = None,
                 opener: Callable[[str], None] | None = None,
                 edge=None, desktop=None, system=None):
        self.settings = settings
        self.reminders = reminders
        self.summarize = summarize
        # Side effects are injectable so tests never open real windows or run commands.
        self.launch = launcher or (lambda argv: subprocess.Popen(argv, shell=False, close_fds=True))
        self.browse = browser or webbrowser.open
        self.run = runner or self._run_command
        self.open_path = opener or os.startfile
        self.edge = edge                     # EdgeController for JARVIS's own Edge window (None = no browser control)
        self.media_opened_at = 0.0           # when a video/music site was last opened (hands-free gets stricter)
        self.desktop = desktop               # Desktop: click/type/keys in any app (None = only inside JARVIS's Edge)
        self.system = system                 # SystemControls: volume and screen brightness (None = not available)
        self.edge_used_at = 0.0              # when JARVIS last acted in its Edge window …
        self.app_hint: tuple[str, float] | None = None   # … or last opened an app ("open valorant and click play")
        self.spotify_app_at = 0.0            # when a search was opened in the Spotify desktop app
        self.tools: dict[str, Tool] = {t.name: t for t in self._build()}

    def schemas(self) -> list[dict]:
        return [t.schema() for t in self.tools.values()]

    def call(self, name: str, args: dict) -> ToolResult:
        tool = self.tools.get(name)
        if not tool:
            return ToolResult(f"No tool named {name}.")
        try:
            return tool.handler(args or {})
        except Exception as exc:
            # Browser errors are written to be spoken; anything else is a bug, and its text isn't for the user's ears.
            if isinstance(exc, BrowserError) and str(exc):
                return ToolResult(str(exc))
            return ToolResult("Sorry, that didn't work. Something went wrong on my side.")

    # -- tool definitions --
    def _build(self) -> list[Tool]:
        return [
            Tool("get_time", "Get the current local date and time.", _obj({}), self._time),
            Tool("open_app", "Open an approved Windows application by name.",
                 _obj({"name": {"type": "string", "description": "App name, e.g. notepad"}}, ["name"]), self._open_app),
            Tool("open_file", "Find a file or folder by name in the approved folders and open it (asks first).",
                 _obj({"query": {"type": "string", "description": "Name or part of the name"}}, ["query"]), self._open_file),
            Tool("open_website", "Open a website or web app (e.g. WhatsApp, Gmail), optionally in a given browser.",
                 _obj({"url": {"type": "string", "description": "Domain, URL or web app name"},
                       "browser": {"type": "string", "description": "chrome, edge… (optional)"}}, ["url"]), self._open_site),
            Tool("web_search", "Search Google, YouTube, Amazon… in the browser.",
                 _obj({"query": {"type": "string"}, "site": {"type": "string"}, "browser": {"type": "string"}}, ["query"]),
                 self._web_search),
            Tool("browser_type", "Type text into the current page in JARVIS's Edge (submit=true presses Enter).",
                 _obj({"text": {"type": "string"}, "submit": {"type": "boolean"}}, ["text"]), self._b_type),
            Tool("browser_key", "Press a key in JARVIS's Edge.", _obj({"key": {"type": "string"}}, ["key"]), self._b_key),
            Tool("browser_click", "Click something visible on the page by its text, e.g. 'first video'.",
                 _obj({"target": {"type": "string"}, "nth": {"type": "integer"}}, ["target"]), self._b_click),
            Tool("browser_scroll", "Scroll the page.", _obj({"direction": {"type": "string"}}, ["direction"]), self._b_scroll),
            Tool("browser_media", "Play, pause, mute or skip the video in Edge.",
                 _obj({"action": {"type": "string"}}, ["action"]), self._b_media),
            Tool("browser_fill", "Fill one form field in Edge by its label (dropdowns and checkboxes too).",
                 _obj({"field": {"type": "string"}, "value": {"type": "string"}}, ["field", "value"]), self._b_fill),
            Tool("browser_check", "Tick or untick a checkbox in Edge by its label.",
                 _obj({"label": {"type": "string"}, "on": {"type": "boolean"}}, ["label"]), self._b_check),
            Tool("browser_nav", "Back, forward, reload, close tab or new tab in Edge.",
                 _obj({"action": {"type": "string"}}, ["action"]), self._b_nav),
            Tool("system_volume", "Laptop volume: up, down, set (value 0-100), mute, unmute, get.",
                 _obj({"action": {"type": "string"}, "value": {"type": "integer"}}, ["action"]), self._volume),
            Tool("system_brightness", "Screen brightness: up, down, set (value 0-100), get.",
                 _obj({"action": {"type": "string"}, "value": {"type": "integer"}}, ["action"]), self._brightness),
            Tool("create_reminder", "Create a local reminder.",
                 _obj({"text": {"type": "string"}, "when": {"type": "string", "description": "e.g. 'in 10 minutes' or 'at 5 pm'"}},
                      ["text", "when"]), self._reminder),
            Tool("list_reminders", "List upcoming reminders.", _obj({}), self._list_reminders),
            Tool("search_files", "Search approved folders for files whose name contains a phrase.",
                 _obj({"query": {"type": "string"}}, ["query"]), self._search),
            Tool("run_command", "Run one of the allowlisted system commands by its name.",
                 _obj({"name": {"type": "string", "description": "Allowlisted command name"}}, ["name"]), self._command),
            Tool("summarize_conversation", "Summarize this conversation so far.", _obj({}), self._summary),
        ]

    def _time(self, args: dict) -> ToolResult:
        now = datetime.now()
        day = now.day
        suffix = "th" if 11 <= day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
        return ToolResult(f"It's {now.strftime('%I:%M %p').lstrip('0')} on {now.strftime('%A')}, the {day}{suffix} of {now.strftime('%B')}.")

    # -- apps: approved_apps from settings + shortcuts on your Desktop --
    def shortcuts(self) -> dict[str, Path]:
        """Desktop shortcuts are treated as apps you approved by putting them there (minus the blocklist)."""
        blocked = {normalize_name(b) for b in self.settings.get("blocked_shortcuts")}
        found: dict[str, Path] = {}
        for folder in self.settings.get("shortcut_dirs"):
            for lnk in sorted(Path(folder).glob("*.lnk")) if Path(folder).is_dir() else []:
                key = normalize_name(lnk.stem)
                if key and key not in blocked:
                    found.setdefault(key, lnk)
        return found

    def app_names(self) -> list[str]:
        return list(self.settings.get("approved_apps")) + list(self.shortcuts())

    def find_app(self, name: str) -> tuple[str, str | Path] | None:
        want = normalize_name(name)
        want = APP_ALIASES.get(want, want)
        if not want:
            return None
        apps = {k.lower(): v for k, v in self.settings.get("approved_apps").items()}
        links = self.shortcuts()
        for table in (apps, links):
            if want in table:
                return want, table[want]
        for table in (apps, links):
            # "k in want" only for short requests: "open of whatsapp in a chrome" must not just open Chrome.
            hit = next((k for k in table if want in k.split() or k.startswith(want) or
                        (len(k) > 3 and len(want.split()) <= 3 and k in want)), None)
            if hit:
                return hit, table[hit]
        if len(want.split()) <= 2 and len(want) >= 4:  # misheard short names: "crome", "valorent", "blendar"
            names = list(apps) + list(links)
            close = difflib.get_close_matches(want, names, n=1, cutoff=0.8)
            if close:
                return close[0], apps.get(close[0]) or links[close[0]]
        return None

    def _open_app(self, args: dict) -> ToolResult:
        name = str(args.get("name", "")).strip()
        if self.edge and APP_ALIASES.get(normalize_name(name), normalize_name(name)) in ("edge", "browser", "jarvis edge"):
            try:
                self.edge.ensure_running()
                if self.edge.pages() and self.edge.current()["url"] not in ("about:blank", ""):
                    self.edge.navigate("new tab")
            except BrowserError as exc:
                return ToolResult(str(exc))
            return ToolResult("Opening Edge.")
        hit = self.find_app(name)
        if not hit:
            return ToolResult(f"'{name}' isn't one of your approved apps or Desktop shortcuts.")
        key, target = hit
        if isinstance(target, Path):
            self.open_path(str(target))          # a Desktop shortcut
        elif target.lower().endswith(".exe") or "\\" in target:
            self.launch([target])
        else:
            self.launch(["cmd", "/c", "start", "", target])
        self.app_hint = (key, time.monotonic())
        return ToolResult(f"Opening {key.title() if key.islower() else key}.")

    # -- files & folders inside the approved search folders --
    def find_paths(self, query: str, limit: int = 6) -> tuple[list[Path], list[Path]]:
        """Returns (openable matches, matches that are programs/scripts). Best first."""
        q = normalize_name(query)
        want_dir = bool(re.search(r"\bfolders?\b", q))
        want_ext = next((e for e in DOC_TYPES if re.search(rf"\b{e}\b", q)), None)
        q = re.sub(r"\b(files?|folders?|documents?|docs?|my|the|a|called|named|" + "|".join(DOC_TYPES) + r")\b", " ", q)
        words = q.split()
        if not words:
            return [], []
        q = " ".join(words)
        personal_doc = bool(re.search(r"\b(resume|cv|portfolio|certificate|marksheet|invoice|report|notes)\b", q))
        scored: list[tuple[tuple, Path]] = []
        # "open downloads folder" / "open my desktop": the search folders themselves count too.
        for root in self.settings.get("search_dirs"):
            label = "documents" if Path(root).name == "ドキュメント" else normalize_name(Path(root).name)
            if Path(root).is_dir() and q in (label, label.rstrip("s")) and not want_ext:
                scored.append(((0, -1, 0, 0, 0), Path(root)))
        deadline = time.monotonic() + 3.0
        for root_i, root in enumerate(self.settings.get("search_dirs")):
            if not Path(root).is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                depth = len(Path(dirpath).relative_to(root).parts)
                dirnames[:] = [d for d in dirnames if not d.startswith((".", "~$", "$")) and d.lower() not in SKIP_DIRS]
                if depth >= 4:
                    dirnames[:] = []
                candidates = [(d, True) for d in dirnames] + [(f, False) for f in filenames
                                                              if not f.startswith(("~$", ".")) and not f.lower().endswith(".tmp")]
                for entry, is_dir in candidates:
                    if want_dir and not is_dir:
                        continue
                    ext = "" if is_dir else Path(entry).suffix.lower()
                    if want_ext and (is_dir or ext.lstrip(".") not in DOC_TYPES[want_ext]):
                        continue
                    stem = normalize_name(entry if is_dir else Path(entry).stem)
                    tokens = stem.split()
                    if not all(any(tok.startswith(w) for tok in tokens) or (len(w) >= 5 and w in stem) for w in words):
                        continue
                    exact = 0 if stem == q else (1 if stem.startswith(q) else 2)
                    code = 1 if ext in CODE_EXTENSIONS else 0          # documents and folders before source files
                    if personal_doc and not is_dir and (code or ext not in PERSONAL_DOC_EXTENSIONS) and not (ext == ".md" and "notes" in q):
                        continue                                        # "my resume" is never a .tsx or .md file
                    scored.append(((code, exact, root_i, depth, len(entry)), Path(dirpath) / entry))
                if time.monotonic() > deadline:
                    break
        scored.sort(key=lambda s: s[0])
        openable, blocked, seen = [], [], set()
        for _, p in scored:
            if p in seen:
                continue
            seen.add(p)
            (blocked if p.suffix.lower() in BLOCKED_EXTENSIONS else openable).append(p)
        return openable[:limit], blocked[:limit]

    def _open_file(self, args: dict) -> ToolResult:
        query = str(args.get("query", "")).strip()
        roots = self.settings.get("search_dirs")
        hits, programs = self.find_paths(query)
        if not hits and programs:
            p = programs[0]
            if p.suffix.lower() == ".lnk":
                return ToolResult(f"{display_name(p)} is a shortcut; ask me to open it as an app instead.")
            return ToolResult(f"{display_name(p)} is a program or script, so I won't run it. "
                              f"Add it to your approved apps if you trust it.")
        if not hits:
            return ToolResult(f"I couldn't find an app, file or folder called {query} on your Desktop, Documents or Downloads.")

        def opener(p: Path) -> Callable[[], ToolResult]:
            def go() -> ToolResult:
                self.open_path(str(p))
                return ToolResult(f"Opening {display_name(p)}.")
            return go

        def label(p: Path) -> str:
            if str(p) in [str(Path(r)) for r in roots]:
                return f"your {'Documents' if p.name == 'ドキュメント' else display_name(p)} folder"
            kind = "folder" if p.is_dir() else (p.suffix[1:].upper() + " file" if p.suffix else "file")
            return f"{display_name(p)}, the {kind} in {_where(p, roots)}"

        # "open the Munder Difflin folder": one item named exactly that beats folders that merely contain the words.
        words = lambda s: re.findall(r"[a-z0-9]+", s.lower())  # noqa: E731  (digits count here: "project 01" ≠ "project")
        q = [w for w in words(query) if not re.fullmatch(r"files?|folders?|documents?|docs?|my|the|a|called|named|"
                                                         + "|".join(DOC_TYPES), w)]
        exact = [p for p in hits if words(p.name if p.is_dir() else p.stem) == q]
        if len(exact) == 1:
            hits = exact
        if len(hits) == 1:
            p = hits[0]
            return ToolResult(f"Found {p}.", True, f"Open {label(p)}?", opener(p))
        options = hits[:3]
        spoken = "; ".join(f"{i}: {label(p)}" for i, p in enumerate(options, 1))
        more = " Or say a more specific name." if len(hits) > 3 else ""
        return ToolResult(f"Several matches for {query}.", True, f"I found a few. {spoken}. Which one?{more}",
                          choices=[(f"{display_name(p)} ({'folder' if p.is_dir() else p.suffix[1:].upper() or 'file'})", opener(p))
                                   for p in options])

    def _browse_in(self, url: str, browser: str | None) -> str:
        """Open a URL in JARVIS Edge (default), or in another approved browser if one is named."""
        if self.edge and getattr(self.edge, "guard", None):
            self.edge.check_url(url)          # blocked sites are refused in every browser, before anything loads
        if any(m in url for m in MEDIA_HOSTS) and not (self.edge and (browser or self.settings.get("browser")) in (None, "", "edge")):
            self.media_opened_at = time.time()
        name = normalize_name(browser or self.settings.get("browser") or "")
        name = {"google chrome": "chrome", "chroma": "chrome", "microsoft edge": "edge", "jarvis edge": "edge"}.get(name, name)
        if name in ("edge", "browser", "") and self.edge:
            self.edge.open_url(url)
            self.edge_used_at = time.monotonic()
            return "Edge"
        hit = self.find_app(name) if name and name not in ("default", "browser") else None
        if hit and not isinstance(hit[1], Path):
            target = hit[1]
            self.launch([target, url] if target.lower().endswith(".exe") else ["cmd", "/c", "start", "", target, url])
            return hit[0].title()
        self.browse(url)
        return "your browser"

    def _open_site(self, args: dict) -> ToolResult:
        raw = str(args.get("url", "")).strip()
        browser = args.get("browser")
        app = WEB_APPS.get(resolve_web_app(raw) or "")
        if app:
            raw = app["url"]
        url = raw if re.match(r"^https?://", raw) else f"https://{raw}"
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in ("http", "https") or not host or "." not in host:
            return ToolResult(f"'{raw}' doesn't look like a website address.")
        label = app["name"] if app else host
        guard = getattr(self.edge, "guard", None) if self.edge else None
        reason = guard.check(url) if guard else None
        if reason:                                # refused outright: no "open it anyway?" for a blocked site
            self.edge.blocked.append((time.time(), url, reason))
            return ToolResult(f"I blocked {host}: {reason}.")
        approved = app is not None or any(host == d or host.endswith("." + d) for d in self.settings.get("approved_sites"))
        # JARVIS's own Edge is a clean profile with none of your accounts in it: any site may open there without asking
        in_jarvis_edge = self.edge and normalize_name(browser or self.settings.get("browser") or "") in ("", "edge", "browser")
        approved = approved or bool(in_jarvis_edge and self.settings.get("edge_open_any_site", True))

        def go() -> ToolResult:
            where = self._browse_in(url, browser)
            return ToolResult(f"Opening {label} in {where}." if where != "your browser" else f"Opening {label}.")
        if approved:
            return go()
        return ToolResult(f"{host} is not on the approved list; asked the user to confirm.", True,
                          f"{host} isn't on your approved list. Open it anyway?", go)

    def _web_search(self, args: dict) -> ToolResult:
        query = str(args.get("query", "")).strip(" .")
        if not query:
            return ToolResult("What should I search for?")
        site = resolve_web_app(args.get("site") or "") or (args.get("site") or "").lower()
        if not site and self.edge and self.edge.running() and not args.get("browser"):
            host = self.edge.current().get("host", "")
            site = next((k for k, a in WEB_APPS.items() if k in SEARCH_URLS and urlparse(a["url"]).hostname == host), "")
            if args.get("media") and site not in ("youtube", "spotify web"):
                site = ""                               # "search X and play it": somewhere that can play it
        site = site or ("youtube" if args.get("media") else "google")
        if args.get("media"):
            self._media_query = query                   # for the YouTube fallback when Spotify isn't logged in
        if site == "spotify web" and not args.get("browser") and self._desktop_on() and self.desktop.spotify_installed():
            self.desktop.open_uri(f"spotify:search:{quote(query)}")   # your signed-in Spotify app, not the web player
            self.spotify_app_at = time.monotonic()
            self.app_hint = ("spotify", self.spotify_app_at)
            return ToolResult(f"Searching Spotify for {query}.")
        pattern = SEARCH_URLS.get(site, SEARCH_URLS["google"])
        where = self._browse_in(pattern.format(q=quote_plus(query)), args.get("browser"))
        on = f" on {WEB_APPS[site]['name']}" if site in WEB_APPS and site != "google" else ""
        return ToolResult(f"Searching{on} for {query}." if where == "your browser" else f"Searching{on} for {query} in {where}.")

    # -- controlling JARVIS's Edge window --
    def _need_edge(self) -> ToolResult | None:
        if not self.edge:
            return ToolResult("Browser control isn't available.")
        if not self.edge.running():
            return ToolResult("JARVIS's Edge window isn't open yet. Say “open YouTube” or “open Edge” first.")
        self.edge_used_at = time.monotonic()        # the next "click"/"type" stays in Edge until you switch apps
        return None

    def _publishing(self, host: str) -> bool:
        return any(host == h or host.endswith("." + h) for h in MESSAGING_HOSTS)

    def _on_site(self, host: str) -> str:
        name = next((a["name"] for a in WEB_APPS.values() if urlparse(a["url"]).hostname == host), host)
        return f" on {name}" if name else ""

    # -- full desktop control: the same commands, in whatever app you were using (or named) --
    EDGE_ONLY = {"first video", "video", "first result", "result", "search box", "search bar", "search",
                 "most viewed video", "skip ad", "message box", "comment box"}

    def _desktop_on(self) -> bool:
        return bool(self.desktop) and self.settings.get("desktop_control") is not False

    def _desktop_target(self, args: dict, field: str | None = None) -> tuple[int | None, str]:
        """(window, value) when this command is for a desktop app rather than JARVIS's Edge, else (None, value).
        A named app wins ("play in spotify", "hello in notepad"); then an app JARVIS just opened; then whichever of
        JARVIS's Edge or your last app window you used most recently."""
        value = str(args.get(field, "")) if field else ""
        if not self._desktop_on():
            return None, value
        d = self.desktop
        m = re.match(r"^(?P<v>.+?)\s+(?:in|on|inside)\s+(?:the\s+)?(?P<app>[a-z0-9][a-z0-9 .+-]{2,30}?)(?:\s+app)?$", value, re.I) \
            if field else None
        app = args.get("app") or (m.group("app") if m else "")
        if app and normalize_name(app) not in ("edge", "browser", "jarvis edge", "the page", "this page", "page", "screen"):
            hwnd = d.find_app_window(app)
            if hwnd and not d.is_edge(hwnd):
                return hwnd, (m.group("v") if m and not args.get("app") else value)
        if self.edge and value.lower() in self.EDGE_ONLY:
            return None, value
        hint = self.app_hint
        if hint and time.monotonic() - hint[1] < 90 and hint[1] > self.edge_used_at:
            hwnd = d.find_app_window(hint[0], wait=15)       # an app that's still starting up (games take a while)
            if hwnd:
                return hwnd, value
        last = d.last_hwnd
        if last and not d.is_edge(last) and (d.last_changed > self.edge_used_at or not self.edge):
            return last, value
        return None, value

    def _app_name(self, hwnd: int) -> str:
        return self.desktop.describe(hwnd)

    def _d_click(self, hwnd: int, target: str, nth: int, args: dict) -> ToolResult:
        from .desktop import CONFIRM_WORDS, DesktopError
        try:
            self.desktop.check_allowed(hwnd)
            hit = self.desktop.find(hwnd, target, nth, wait=3)
        except DesktopError as exc:
            return ToolResult(str(exc))
        where = self._app_name(hwnd)
        if not hit:
            return ToolResult(f"I can't see “{target}” in {where}.")
        label = hit["name"]

        def go() -> ToolResult:
            try:
                return ToolResult(f"Clicked {self.desktop.click(hwnd, hit)} in {where}.")
            except DesktopError as exc:
                return ToolResult(str(exc))
        low = label.lower()
        risky = any(re.search(rf"\b{w}\b", low) for w in CONFIRM_WORDS) and hit["button"] and len(low.split()) <= 4
        if risky or target.lower() in CONFIRM_WORDS:
            return ToolResult("Waiting for confirmation before pressing it.", True,
                              f"That will press “{label}” in {where}. Go ahead?", go)
        return go()

    def _d_type(self, hwnd: int, text: str, submit: bool) -> ToolResult:
        from .desktop import CHAT_APPS, DesktopError
        where = self._app_name(hwnd)
        try:
            self.desktop.type_text(hwnd, text)
        except DesktopError as exc:
            return ToolResult(str(exc))
        if not submit:
            return ToolResult(f"Typed “{text}” in {where}.")
        if self.desktop.process(hwnd) in CHAT_APPS:
            def go() -> ToolResult:
                self.desktop.press(hwnd, "enter")
                return ToolResult("Sent.")
            return ToolResult("Typed; waiting for confirmation before sending.", True,
                              f"I've typed “{text}” in {where}. Should I send it?", go)
        self.desktop.press(hwnd, "enter")
        return ToolResult(f"Typed “{text}” and pressed Enter in {where}.")

    def _d_media(self, hwnd: int | None, action: str) -> ToolResult:
        """Media keys work for any player. Spotify's window title is the song while it plays, so that's checked."""
        d = self.desktop
        if action in ("forward", "back", "skip ad"):
            return ToolResult("I can't skip by seconds outside my Edge window. I can play, pause, go to the next or "
                              "previous song, or mute.")
        spotify = d.find_app_window("spotify", visible=False) if (hwnd is None or d.process(hwnd) == "spotify.exe") else None
        if spotify and d.process(spotify) != "spotify.exe":
            spotify = None                          # e.g. a browser tab titled "…Spotify…", not the app
        if spotify:
            playing = not d.title(spotify).startswith("Spotify")
            if action == "pause" and not playing:
                return ToolResult("Spotify's already paused.")
            if action == "play" and playing:
                return ToolResult(f"It's already playing: {d.title(spotify)}.")
        d.media_key(action)
        if spotify and action in ("play", "pause", "toggle", "next", "previous"):
            for _ in range(8):
                time.sleep(0.3)
                now = d.title(spotify)
                if action == "pause" and now.startswith("Spotify"):
                    return ToolResult("Paused Spotify.")
                if action != "pause" and not now.startswith("Spotify"):
                    return ToolResult(f"Playing {now} on Spotify.")
        return ToolResult({"pause": "Pressed pause.", "play": "Pressed play.", "toggle": "Pressed play/pause.",
                           "next": "Skipped to the next one.", "previous": "Went back one.", "mute": "Pressed mute.",
                           "unmute": "Pressed mute again to bring the sound back."}.get(action, "Done."))

    def _spotify_app_play(self) -> ToolResult:
        """Press play on the top result of the search JARVIS opened in your Spotify app, then check it plays."""
        d = self.desktop
        hwnd = d.find_app_window("spotify", wait=8)
        if not hwnd:
            return ToolResult("I can't find the Spotify window.")
        time.sleep(1.2)                                       # let the new search replace the old page
        hit = d.find(hwnd, "play ", prefix=True, wait=6)
        if not hit:
            return ToolResult("I opened the search in Spotify, but I can't see anything to play on it yet.")
        name = hit["name"][5:].strip()
        d.click(hwnd, hit)
        for _ in range(12):
            time.sleep(0.4)
            title = d.title(hwnd)
            if not title.startswith("Spotify"):
                return ToolResult(f"Playing {name} on Spotify: {title}.")
        return ToolResult(f"I can't tell if it's playing: I pressed play on {name} in Spotify, but the song hasn't "
                          f"started yet.")

    def _b_type(self, args: dict) -> ToolResult:
        hwnd, text = self._desktop_target(args, "text")
        if hwnd:
            return self._d_type(hwnd, text.strip(), bool(args.get("submit")))
        if (err := self._need_edge()):
            return err
        text = str(args.get("text", "")).strip()
        submit = bool(args.get("submit"))
        if not text:
            return ToolResult("What should I type?")
        try:
            field = self.edge.type_text(text)
        except BrowserError as exc:
            if self.edge.current()["host"] == "web.whatsapp.com":
                return ToolResult("WhatsApp isn't linked in my Edge window yet, or no chat is open. Link it once: on your "
                                  "phone open WhatsApp, Linked devices, and scan the QR code in my Edge window. "
                                  "Then say “click” and the contact's name to open their chat.")
            return ToolResult(str(exc))
        if not submit:
            return ToolResult(f"Typed “{text}”.")
        host = self.edge.current()["host"]
        if field["search"] and not self._publishing(host):
            self.edge.press("enter")
            return ToolResult(f"Searching for {text}.")

        def go() -> ToolResult:
            self.edge.press("enter")
            return ToolResult("Sent.")
        return ToolResult("Typed; waiting for confirmation before sending.", True,
                          f"I've typed “{text}”. Should I send it{self._on_site(host)}?", go)

    def _b_key(self, args: dict) -> ToolResult:
        key = str(args.get("key", "enter")).lower()
        hwnd, _ = self._desktop_target(args)
        if hwnd:
            from .desktop import CHAT_APPS, DesktopError
            where = self._app_name(hwnd)

            def press() -> ToolResult:
                try:
                    self.desktop.press(hwnd, key)
                except DesktopError as exc:
                    return ToolResult(str(exc))
                return ToolResult("Sent." if key == "enter" and self.desktop.process(hwnd) in CHAT_APPS else
                                  f"Pressed {key} in {where}.")
            if key == "enter" and self.desktop.process(hwnd) in CHAT_APPS:
                return ToolResult("Waiting for confirmation before sending.", True, f"Send it in {where}?", press)
            return press()
        if (err := self._need_edge()):
            return err
        host = self.edge.current()["host"]
        if key == "enter" and self._publishing(host):
            def go() -> ToolResult:
                self.edge.press("enter")
                return ToolResult("Sent.")
            return ToolResult("Waiting for confirmation before sending.", True, f"Send it{self._on_site(host)}?", go)
        try:
            self.edge.press(key)
        except BrowserError as exc:
            return ToolResult(str(exc))
        return ToolResult(f"Pressed {key}.")

    def _b_click(self, args: dict) -> ToolResult:
        nth = int(args.get("nth") or 0)
        hwnd, target = self._desktop_target(args, "target")
        if hwnd:
            return self._d_click(hwnd, target.strip(), nth, args)
        if (err := self._need_edge()):
            return err
        target = str(args.get("target", "")).strip()
        hit = None
        for _ in range(12):  # the page may still be loading
            try:
                hit = self.edge.find_clickable(target, nth)
            except BrowserError:
                hit = None
            if hit:
                break
            time.sleep(0.5)
        if not hit:
            return ToolResult(f"I can't see “{target}” on this page.")
        label = (hit.get("text") or target).strip()
        host = self.edge.current()["host"]

        def go() -> ToolResult:
            try:
                clicked = self.edge.click(target, nth)
            except BrowserError as exc:
                return ToolResult(str(exc))
            if args.get("asked") == "likes":    # be straight about what "most likes" turned into
                return ToolResult(f"YouTube's results don't show likes, so I clicked the most viewed one: {clicked}.")
            return ToolResult(f"Clicked {clicked}.")
        # Ask first only for a real Send/Post/Share… button (or when you named one), not for a video called
        # "Post Malone - Sunflower" or a link whose title happens to say "share".
        words = set(re.findall(r"[a-z]+", label.lower()))
        button = hit.get("button", True) and len(words) <= 4
        if target.lower() in PUBLISH_WORDS or (button and words & set(PUBLISH_WORDS)) or \
                (self._publishing(host) and button and "send" in words):
            return ToolResult("Waiting for confirmation before publishing.", True,
                              f"That will {label.lower() or 'post'}{self._on_site(host)}. Go ahead?", go)
        return go()

    def _b_scroll(self, args: dict) -> ToolResult:
        direction = str(args.get("direction", "down"))
        hwnd, _ = self._desktop_target(args)
        if hwnd:
            from .desktop import DesktopError
            if direction == "stop":
                return ToolResult("I wasn't scrolling.")
            try:
                self.desktop.scroll(hwnd, "down" if args.get("continuous") else direction)
            except DesktopError as exc:
                return ToolResult(str(exc))
            extra = " I can only keep scrolling on my own in my Edge window." if args.get("continuous") else ""
            return ToolResult(f"Scrolled {'down' if args.get('continuous') else direction} in {self._app_name(hwnd)}.{extra}")
        if (err := self._need_edge()):
            return err
        if direction == "stop" or args.get("speed") in ("faster", "slower"):
            if args.get("speed") in ("faster", "slower") and self.edge.auto_scrolling():
                order = ["slow", "normal", "fast"]
                now = order.index(getattr(self.edge, "scroll_speed", "normal"))
                speed = order[min(2, now + 1)] if args["speed"] == "faster" else order[max(0, now - 1)]
                self.edge.auto_scroll(True, speed)
                return ToolResult("Faster." if args["speed"] == "faster" else "Slower.")
            was = self.edge.auto_scrolling()
            self.edge.auto_scroll(False)
            return ToolResult("Stopped scrolling." if was else "I wasn't scrolling.")
        if args.get("continuous"):
            self.edge.auto_scroll(True, str(args.get("speed") or "normal"))
            return ToolResult("Scrolling. Say stop when you want me to stop.")
        self.edge.scroll(direction)
        return ToolResult({"top": "Back to the top.", "bottom": "At the bottom."}.get(direction, f"Scrolled {direction}."))

    def _b_media(self, args: dict) -> ToolResult:
        action = str(args.get("action", "toggle"))
        if self._desktop_on():
            spotify_search = action == "play_result" and self.spotify_app_at > self.edge_used_at
            if spotify_search:                      # the search JARVIS just opened in your Spotify app
                return self._spotify_app_play()
            hwnd, _ = self._desktop_target(args)
            if action != "play_result" and (hwnd or args.get("app") or not (self.edge and self.edge.running())):
                return self._d_media(hwnd, action)
        if (err := self._need_edge()):
            return err
        if action == "pause" and getattr(self.edge, "auto_scrolling", lambda: False)():  # "stop" while scrolling
            self.edge.auto_scroll(False)
            return ToolResult("Stopped scrolling.")
        if action == "skip ad":
            return self._b_click({"target": "skip ad"})
        spotify = self.edge.current()["host"].endswith("spotify.com")
        if action == "play_result" and not spotify:          # "search X and play it" on YouTube
            return self._b_click({"target": "first video"})
        state = "none"
        for _ in range(12 if action == "play_result" else 1):  # a fresh search page may still be loading
            state = self.edge.media(action)
            if state != "none":
                break
            time.sleep(0.5)
        if spotify and state == "login" and action == "play_result":
            return self._youtube_instead()
        if spotify:
            return self._spotify_result(action, state)
        if state == "none" and self._desktop_on() and action in ("play", "pause", "toggle", "next", "mute", "unmute"):
            return self._d_media(None, action)      # nothing in my Edge window: the music is in some other app
        if state == "none":
            return ToolResult("There's no video playing in Edge.")
        return ToolResult({"paused": "Paused.", "playing": "Playing.", "muted": "Muted.", "unmuted": "Sound's back on.",
                           "forward": "Skipped ahead ten seconds.", "back": "Went back ten seconds.",
                           "next": "Next video."}.get(state, "Done."))

    def _youtube_instead(self) -> ToolResult:
        """Spotify plays nothing without an account; YouTube plays without one. Say so, then play it there."""
        query = getattr(self, "_media_query", "") or "songs"
        if query == "today's top hits":
            query = "top hits songs"
        self._web_search({"query": query, "site": "youtube"})
        clicked = self._b_click({"target": "first video"})
        if not clicked.text.startswith("Clicked"):
            return clicked
        return ToolResult(f"Spotify needs a login to play anything, so I played it on YouTube instead. "
                          f"{clicked.text}")

    def _spotify_result(self, action: str, state: str) -> ToolResult:
        """Spotify only plays for a logged-in account, so after pressing play, check that it really started."""
        if state == "none":
            return ToolResult("I can't find anything to play on Spotify's page yet. If it's asking you to log in, "
                              "log in once in my Edge window and I'll take it from there.")
        if state == "unsupported":
            return ToolResult("I can't do that on Spotify. I can play, pause, skip to the next song or mute it.")
        if state == "login":
            return ToolResult("I can't control Spotify yet: it wants you to log in first. Log in once in my Edge window "
                              "and I'll take it from there.")
        if state != "pressed":
            return ToolResult({"paused": "Paused.", "next": "Next song.", "muted": "Muted.",
                               "unmuted": "Sound's back on."}.get(state, "Done."))
        for _ in range(8):
            time.sleep(0.4)
            if self.edge.media("is_playing") == "playing":
                return ToolResult("Playing it on Spotify." if action == "play_result" else "Playing.")
        return ToolResult("I can't get Spotify to start playing: I pressed play, but nothing's playing. Spotify only plays "
                          "for a logged-in account, so log in once in my Edge window and ask me again.")

    def _find_field(self, field: str, kinds=("field",)) -> dict | None:
        """The page element whose label best matches what you called it ('email', 'first name', 'country')."""
        want = normalize_name(field)
        best, best_score = None, 0.0
        for el in self.edge.snapshot().get("elements", []):
            if el.get("kind") not in kinds or el.get("disabled"):
                continue
            label = normalize_name(el.get("label", ""))
            if not label:
                continue
            score = 1.0 if label == want else 0.9 if want in label.split() or label.startswith(want) else \
                0.8 if want in label else difflib.SequenceMatcher(None, want, label).ratio() * 0.8
            if score > best_score:
                best, best_score = el, score
        return best if best_score >= 0.5 else None

    def _b_fill(self, args: dict) -> ToolResult:
        """Type into one field by its label (a dropdown gets the matching option, a checkbox gets ticked)."""
        if (err := self._need_edge()):
            return err
        field, value = str(args.get("field", "")).strip(), args.get("value", "")
        el = self._find_field(field)
        if not el:
            return ToolResult(f"I can't see a “{field}” field on this page.")
        if el["tag"] == "select":
            hit = self.edge.act(el["id"], "select", value)
            return ToolResult(f"Picked {hit.get('label', value)} for {el['label']}." if hit.get("ok") else
                              f"I can't find “{value}” in the {el['label']} list.")
        if el.get("type") in ("checkbox", "radio", "switch"):
            self.edge.act(el["id"], "check", value)
            return ToolResult(f"{'Ticked' if str(value).lower() not in ('false', 'no', 'off') else 'Unticked'} {el['label']}.")
        hit = self.edge.act(el["id"], "fill", value)
        if hit.get("sensitive"):
            return ToolResult(f"I don't type into {el['label'] or 'that box'}: it's a password, PIN, OTP, card or "
                              f"ID-number field. Please type that one yourself.")
        if not hit.get("ok"):
            return ToolResult(f"I couldn't type into {el['label']}: {hit.get('why', 'the page changed')}.")
        shown = "your password" if hit.get("password") else f"“{value}”"
        return ToolResult(f"Typed {shown} into {el['label'] or field}.")

    def _b_check(self, args: dict) -> ToolResult:
        if (err := self._need_edge()):
            return err
        el = self._find_field(str(args.get("label", "")), kinds=("field", "button"))
        if not el:
            return ToolResult(f"I can't see a “{args.get('label')}” checkbox on this page.")
        on = bool(args.get("on", True))
        self.edge.act(el["id"], "check", on)
        return ToolResult(f"{'Ticked' if on else 'Unticked'} {el['label']}.")

    def _b_nav(self, args: dict) -> ToolResult:
        action = str(args.get("action", "back"))
        if action == "new tab" and self.edge:
            self.edge.ensure_running()
        elif (err := self._need_edge()):
            return err
        self.edge.navigate(action)
        return ToolResult({"back": "Going back.", "forward": "Going forward.", "reload": "Reloading.",
                           "close": "Closed the tab.", "new tab": "New tab open."}.get(action, "Done."))

    def browser_read(self) -> dict | None:
        """Title, URL and visible text of the current Edge page (the conversation summarizes it)."""
        if self._need_edge():
            return None
        return self.edge.read()

    # -- volume & brightness --
    def _volume(self, args: dict) -> ToolResult:
        from .system import SystemError_
        if not self.system:
            return ToolResult("I can't change the volume on this setup.")
        action, s = str(args.get("action", "get")), self.system
        try:
            if action == "mute":
                s.set_mute(True)
                return ToolResult("Muted the laptop.")
            if action == "unmute":
                s.set_mute(False)
                return ToolResult(f"Sound's back on, at {s.volume()} percent.")
            if action == "get":
                return ToolResult(f"The volume is at {s.volume()} percent{', but it is muted' if s.muted() else ''}.")
            now = s.volume()
            step = int(args.get("value") or 10)
            target = {"up": now + step, "down": now - step}.get(action, args.get("value"))
            if target is None:
                return ToolResult("What should I set the volume to? Say a number, like 40 percent.")
            got = s.set_volume(target)
            if action == "up" and now >= 100:
                return ToolResult("It's already at full volume.")
            return ToolResult(f"Volume's at {got} percent." if got else "Volume's at zero now.")
        except SystemError_ as exc:
            return ToolResult(str(exc))

    def _brightness(self, args: dict) -> ToolResult:
        from .system import SystemError_
        if not self.system:
            return ToolResult("I can't change the brightness on this setup.")
        action, s = str(args.get("action", "get")), self.system
        try:
            now = s.brightness()
            if action == "get":
                return ToolResult(f"The screen brightness is at {now} percent.")
            step = int(args.get("value") or 15)
            target = {"up": now + step, "down": now - step}.get(action, args.get("value"))
            if target is None:
                return ToolResult("What should I set the brightness to? Say a number, like 60 percent.")
            got = s.set_brightness(target)
            if action == "up" and now >= 100:
                return ToolResult("The screen's already as bright as it goes.")
            return ToolResult(f"Brightness is at {got} percent.")
        except SystemError_ as exc:
            return ToolResult(str(exc))

    def _reminder(self, args: dict) -> ToolResult:
        due = parse_when(str(args.get("when", "")))
        if not due:
            return ToolResult("What time should I set it for? You can say something like in 10 minutes, or at 5 PM.")
        item = self.reminders.add(str(args.get("text", "reminder")), due)
        return ToolResult(f"Okay, I'll remind you {spoken_when(due)}: {item['text']}.")

    def _list_reminders(self, args: dict) -> ToolResult:
        items = self.reminders.pending()
        if not items:
            return ToolResult("No upcoming reminders.")
        listed = [f"{r['text']} {spoken_when(datetime.fromisoformat(r['due']))}" for r in items[:5]]
        intro = "You've got one reminder: " if len(listed) == 1 else f"You've got {len(items)} reminders. "
        return ToolResult(intro + ". ".join(x[0].upper() + x[1:] for x in listed) + ".")

    def _search(self, args: dict) -> ToolResult:
        q = str(args.get("query", "")).lower().strip()
        if len(q) < 2:
            return ToolResult("Ask for a longer search phrase.")
        # Same ranking as opening a file (documents before source code, no node_modules), spoken briefly, and
        # offered to open: a list of ten code files read aloud helps nobody.
        r = self._open_file({"query": q})
        if r.needs_confirmation and not r.choices:
            return ToolResult(r.text, True, "Yes, " + r.confirm_prompt[len("Open "):-1] + ". Want me to open it?",
                              r.run_confirmed)
        if not r.needs_confirmation and r.text.startswith("I couldn't find"):
            return ToolResult(f"I couldn't find a file called {q} on your Desktop, Documents or Downloads.")
        return r

    def _command(self, args: dict) -> ToolResult:
        cmds: dict = self.settings.get("allowed_commands")
        name = str(args.get("name", "")).lower().strip()
        spec = cmds.get(name)
        if not spec:
            return ToolResult(f"'{name}' is not an allowlisted command. Allowed: {', '.join(cmds)}.")
        argv = list(spec["argv"])

        def go() -> ToolResult:
            return ToolResult(speak_command_output(name, self.run(argv)))
        if spec.get("confirm"):
            ask = {"lock screen": "Lock the screen now?", "flush dns": "Flush the DNS cache?"}.get(name, f"Should I run {name}?")
            return ToolResult(f"Asked the user to confirm running {name}.", True, ask, go)
        return go()

    def _summary(self, args: dict) -> ToolResult:
        return ToolResult("__SUMMARY__")  # handled by the engine, which owns the conversation

    @staticmethod
    def _run_command(argv: list[str]) -> str:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=20, shell=False)
        return (out.stdout or out.stderr).strip()
