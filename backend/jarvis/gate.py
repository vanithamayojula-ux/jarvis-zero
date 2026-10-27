"""The one choke point for everything JARVIS does with your keyboard and mouse.

Every click, key press and typed character (in any app, and in JARVIS's own Edge) is put through Gate.authorize first.
The gate says allow, confirm (ask the person first) or deny, and writes one secret-free line to the action log.

  * a latching kill switch: once tripped (hotkey, button, "stop" breaker) nothing acts until the person says "resume";
  * never in terminals, password managers, remote-desktop tools, Windows Security, sign-in or banking pages, the
    Explorer address bar, VS Code's terminal, or in card / OTP / password boxes;
  * Send / Buy / Delete / Allow / Install … buttons and Enter outside search boxes need the person's yes;
  * a rate breaker for anything JARVIS does on its own (the browser form agent): too many actions too fast pauses it.

It fails closed: if the gate cannot work out what a window or control is, the answer is deny.
No imports beyond the standard library and safety.py, so every package can use it.
"""
from __future__ import annotations

import contextlib
import re
import threading
import time
from collections import deque
from dataclasses import dataclass

# Set by the kill switch; every long loop (typing, searching a window, the form agent) looks at it. Cleared at the
# start of the next turn only.
ABORT = threading.Event()

# Apps where one wrong keystroke can run a command, change the system or expose a secret: no clicking or typing at all.
BLOCKED_PROCESSES = {"cmd.exe", "powershell.exe", "pwsh.exe", "windowsterminal.exe", "openconsole.exe", "conhost.exe",
                     "wsl.exe", "wslhost.exe", "bash.exe", "mintty.exe", "regedit.exe", "mmc.exe", "taskmgr.exe",
                     "sechealthui.exe", "consent.exe", "msiexec.exe", "credentialuibroker.exe", "lockapp.exe",
                     "wt.exe", "powershell_ise.exe", "wscript.exe", "cscript.exe", "mshta.exe", "mstsc.exe",
                     "systemsettings.exe", "control.exe", "searchhost.exe", "startmenuexperiencehost.exe",
                     "anydesk.exe", "teamviewer.exe", "keepass.exe", "keepassxc.exe", "bitwarden.exe",
                     "1password.exe", "lastpass.exe", "dashlane.exe", "nordpass.exe", "enpass.exe", "roboform.exe"}
BLOCKED_NAMES = {"cmd.exe": "Command Prompt", "powershell.exe": "PowerShell", "pwsh.exe": "PowerShell",
                 "windowsterminal.exe": "the terminal", "wt.exe": "the terminal", "openconsole.exe": "the terminal",
                 "conhost.exe": "the terminal", "regedit.exe": "Registry Editor", "taskmgr.exe": "Task Manager",
                 "sechealthui.exe": "Windows Security", "mmc.exe": "that system console",
                 "systemsettings.exe": "Windows Settings", "control.exe": "Control Panel",
                 "mstsc.exe": "Remote Desktop", "anydesk.exe": "remote control software",
                 "teamviewer.exe": "remote control software"}
# Clicking a button with one of these words is asked about first.
CONFIRM_WORDS = ("send", "post", "comment", "reply", "submit", "publish", "tweet", "share", "upload", "buy", "pay",
                 "purchase", "order", "checkout", "delete", "remove", "uninstall", "install", "format", "erase", "reset",
                 "sign out", "log out", "logout", "end task", "restart", "shut down", "accept", "agree", "unfriend",
                 "block", "unsubscribe", "subscribe")
RISK_EXTRA = ("allow", "authorize", "approve", "confirm", "continue", "grant", "enable", "download", "save", "overwrite",
              "transfer", "withdraw", "empty", "clear")
CHAT_APPS = {"whatsapp.exe", "whatsapp.root.exe", "discord.exe", "telegram.exe", "ms-teams.exe", "teams.exe",
             "slack.exe", "outlook.exe", "olk.exe", "signal.exe", "messenger.exe"}
BROWSER_PROCESSES = {"msedge.exe", "chrome.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe"}
EDITOR_PROCESSES = {"code.exe", "cursor.exe", "code - insiders.exe"}

SAY_KILLED = "Automation is stopped. Say resume to continue."
SAY_TRIPPED = "I paused myself, say resume to continue."
SAY_SECRET_BOX = "That looks like a card/OTP/password box, so that one is yours to type."
SAY_NO_COMBO = "I don't press that one."

# Keyboard shortcuts JARVIS will press, spelled the one canonical way (modifiers in the order ctrl, shift, alt, then the key).
# Nothing with the Windows key, no ctrl+alt+delete, ctrl+shift+esc, Run or system-menu keys: those are simply not in the list.
ALLOWED_COMBOS = frozenset({"ctrl+s", "ctrl+c", "ctrl+v", "ctrl+x", "ctrl+z", "ctrl+y", "ctrl+a", "ctrl+f", "ctrl+n", "ctrl+t",
                            "ctrl+w", "ctrl+p", "ctrl+tab", "ctrl+shift+t", "alt+left", "alt+right", "f5", "f11"})
# These can lose work or throw something away, so the person's whole-sentence yes comes first.
CONFIRM_COMBOS = frozenset({"ctrl+w", "alt+f4", "ctrl+p", "ctrl+shift+delete", "ctrl+x", "ctrl+shift+n"})
PRESSABLE_COMBOS = ALLOWED_COMBOS | CONFIRM_COMBOS
PASTE_COMBOS = frozenset({"ctrl+v"})                  # never into a password / card / OTP box
WINDOW_VERBS = frozenset({"minimize", "maximize", "restore", "close", "snap_left", "snap_right", "show_desktop", "switch"})

_MODIFIER_WORDS = {"ctrl": "ctrl", "control": "ctrl", "shift": "shift", "alt": "alt", "win": "win", "windows": "win",
                   "cmd": "win", "command": "win", "super": "win"}
_KEY_ALIASES = {"esc": "escape", "del": "delete", "return": "enter", "pgdn": "page down", "pgup": "page up",
                "arrow left": "left", "arrow right": "right", "left arrow": "left", "right arrow": "right"}
_NAMED_KEYS = {"escape", "tab", "enter", "delete", "left", "right", "up", "down", "space", "backspace", "home", "end"}


def normalize_combo(text: str) -> str | None:
    """'Control S' / 'ctrl plus shift plus t' / 'alt+F4' → 'ctrl+s' / 'ctrl+shift+t' / 'alt+f4'. None if it isn't shaped like a
    shortcut at all. Says nothing about whether it is allowed (see PRESSABLE_COMBOS)."""
    t = re.sub(r"\bplus\b|\band\b", "+", str(text or "").lower().strip())
    t = re.sub(r"\b(?:arrow )?(left|right) arrow\b|\barrow (left|right)\b", lambda m: m.group(1) or m.group(2), t)
    parts = [p for p in re.split(r"[\s+]+", t) if p]
    if len(parts) < 2 and not re.fullmatch(r"f\d{1,2}", t):
        return None
    mods, key = [], ""
    for i, p in enumerate(parts):
        p = _KEY_ALIASES.get(p, p)
        if p in _MODIFIER_WORDS and i < len(parts) - 1:
            mods.append(_MODIFIER_WORDS[p])
        elif i == len(parts) - 1 and (len(p) == 1 and p.isalnum() or p in _NAMED_KEYS or re.fullmatch(r"f\d{1,2}", p)):
            key = p
        else:
            return None
    order = [m for m in ("ctrl", "shift", "alt", "win") if m in mods]
    return "+".join([*order, key]) if key else None
SAY_FAILED_CLOSED = "I couldn't tell what that window is, so I didn't do anything."

_ALWAYS_DENY_TITLE = re.compile(r"windows security|user account control|credential|enter your pin|windows hello|"
                                r"inprivate|incognito", re.I)
_SIGNIN_WORDS = (r"sign[ -]?in|log[ -]?in|login|net ?banking|checkout|payment|upi|otp|sbi|hdfc|icici|axis bank|kotak|"
                 r"paytm|phonepe")
_SIGNIN_TITLE = re.compile(rf"\b(?:{_SIGNIN_WORDS})\b", re.I)
_SIGNIN_PREFIX = re.compile(r"^\s*(?:sign[ -]?in|log[ -]?in|login)\b", re.I)    # a dialog called "Sign in", not a file name
SAY_READING_OFF = "I'm not looking at the screen right now. Say “you can look at the screen again” to turn that back on."
SAY_OWN_WINDOW = "That's one of my own windows, so I won't read it as a screen."
SAY_PROTECTED = "That window is protected from capture, so I won't read it."
SAY_READ_PRIVATE = "That looks like a sign-in, payment, banking or private window, so I won't read it."

# Reading is refused for: password managers, Windows Security / UAC / credential prompts, remote-control tools. Terminals,
# Task Manager and the like may be READ (the text stays on this laptop and is redacted), though never clicked or typed in.
READ_DENY_PROCESSES = {"keepass.exe", "keepassxc.exe", "bitwarden.exe", "1password.exe", "lastpass.exe", "dashlane.exe",
                       "nordpass.exe", "enpass.exe", "roboform.exe", "keeper.exe", "protonpass.exe", "sechealthui.exe",
                       "consent.exe", "credentialuibroker.exe", "lockapp.exe", "logonui.exe", "mstsc.exe", "anydesk.exe",
                       "teamviewer.exe", "authenticator.exe"}
_PASSWORD_MANAGER_TITLE = re.compile(r"keepass|bitwarden|1password|lastpass|dashlane|nordpass|enpass|roboform|proton pass|"
                                     r"password manager|authenticator|windows security", re.I)
# banks, wallets and one-time codes by name (Indian banks included), whatever program shows them
_READ_FINANCE = re.compile(r"\b(?:sbi|state bank|hdfc|icici|axis bank|kotak|yes bank|pnb|punjab national|bank of baroda|"
                           r"canara|idfc|indusind|federal bank|union bank|paytm|phonepe|gpay|google pay|bhim|net ?banking|"
                           r"internet banking|mobile banking|upi|otp|one[- ]time (?:password|code)|paypal|razorpay|"
                           r"groww|zerodha|upstox|credit card|debit card)\b", re.I)
_READ_BROWSER_EXTRA = re.compile(r"\b(?:bank|banking|payments?|billing|wallet|account statement|2fa|two[- ]factor|"
                                 r"verify your identity|reset (?:your )?password)\b", re.I)
# a short list of address-bar hosts that are sign-in or money pages
_SENSITIVE_HOSTS = ("accounts.google.com", "login.microsoftonline.com", "login.live.com", "account.microsoft.com",
                    "signin.aws.amazon.com", "appleid.apple.com", "id.apple.com", "paypal.com", "stripe.com", "razorpay.com",
                    "paytm.com", "phonepe.com", "pay.google.com", "onlinesbi.sbi", "onlinesbi.com", "sbi.co.in", "hdfcbank.com",
                    "icicibank.com", "axisbank.com", "kotak.com", "yesbank.in", "pnbindia.in", "bankofbaroda.in",
                    "canarabank.com", "idfcfirstbank.com", "indusind.com", "unionbankofindia.co.in", "federalbank.co.in",
                    "zerodha.com", "groww.in", "upstox.com", "npci.org.in", "incometax.gov.in", "uidai.gov.in")
_SENSITIVE_HOST_PREFIX = re.compile(r"^(?:login|signin|sign-in|accounts?|auth|sso|secure|netbanking|onlinebanking|ibanking|"
                                    r"checkout|pay|payments?|wallet|id|idp|passport)\d?\.")
_READ_PREFIX = re.compile(r"^\s*(?:sign[ -]?in|log[ -]?in|login|checkout|secure checkout|payment|pay now)\b(?!\.\w)", re.I)
_CLICKABLE_BUTTONS = {"button", "splitbutton", "menuitem", "hyperlink"}
_LEGACY_BUTTONS = {"button", "splitbutton", "menuitem"}


def _host_of(address: str) -> str:
    """The host of an address-bar value (Edge and Chrome show it without 'https://')."""
    from urllib.parse import urlparse
    a = address.strip()
    try:
        return (urlparse(a if "://" in a else f"https://{a}").hostname or "").lower().strip(".")
    except ValueError:
        return ""


def _sensitive_host(host: str) -> bool:
    return any(host == h or host.endswith("." + h) for h in _SENSITIVE_HOSTS) or bool(_SENSITIVE_HOST_PREFIX.match(host + "."))


@dataclass
class Action:
    kind: str                  # click|type|key|scroll|media|combo|window|read|browser_click|browser_fill|browser_key|browser_tab|browser_find
    hwnd: int | None = None
    proc: str = ""
    title: str = ""
    label: str = ""
    text_len: int = 0          # how much would be typed; the text itself never comes here
    key: str = ""
    source: str = "user"       # user | agent (the form agent acting on its own plan)
    ctype: str = ""            # control type: Button, Hyperlink, ListItem …
    text_class: str = ""       # letters | digits | mixed (never the text)


@dataclass
class Decision:
    verdict: str               # allow | confirm | deny
    say: str = ""
    fingerprint: tuple | None = None


def text_class(text: str) -> str:
    """What kind of text it is, without keeping any of it."""
    letters, digits = any(c.isalpha() for c in text), any(c.isdigit() for c in text)
    return "mixed" if letters and digits else "digits" if digits else "letters" if letters else ""


def _blob(focused: dict | None) -> str:
    f = focused or {}
    parts = [f.get("name"), f.get("automation_id"), f.get("class_name"), f.get("help"), *(f.get("ancestors") or [])]
    return " ".join(str(p) for p in parts if p).lower()


class Gate:
    def __init__(self, settings=None, audit=None, clock=time.monotonic, strict: bool = True):
        self.settings, self.audit, self.clock, self.strict = settings, audit, clock, strict
        self.killed = threading.Event()
        self._lock = threading.Lock()
        self._acts: deque = deque()          # when the agent acted (breaker)
        self._enters: deque = deque()
        self._cache: dict = {}
        self._tls = threading.local()
        self.probe = None                    # hwnd -> facts about a window; Desktop sets it (read_allowed)

    @classmethod
    def permissive(cls, clock=time.monotonic) -> "Gate":
        """For tests and fakes: the old rules only (blocked apps, the old risk words). Kill switch still works."""
        return cls(None, None, clock, strict=False)

    # ---------- kill switch ----------
    def kill(self) -> None:
        self.killed.set()
        ABORT.set()

    def resume(self) -> None:
        with self._lock:
            self._acts.clear()
            self._enters.clear()
        self.killed.clear()
        ABORT.clear()
        self._note("resume")

    def _note(self, kind: str, label: str = "") -> None:
        if self.audit:
            try:
                self.audit.record(Action(kind=kind, label=label), Decision("allow"), kind)
            except Exception:
                pass

    # ---------- who is asking, and has the person said yes ----------
    @contextlib.contextmanager
    def scope(self, source: str | None = None, confirmed: bool | None = None):
        """with gate.scope(confirmed=True): the person just said yes to this exact action.
        with gate.scope(source='agent'): this thread is the form agent acting on its own plan."""
        old = (getattr(self._tls, "source", None), getattr(self._tls, "confirmed", False))
        if source is not None:
            self._tls.source = source
        if confirmed is not None:
            self._tls.confirmed = confirmed
        try:
            yield self
        finally:
            self._tls.source, self._tls.confirmed = old

    # ---------- the window / control rules ----------
    def window_block(self, proc: str, title: str, focused: dict | None = None, kind: str = "", key: str = "",
                     hwnd: int | None = None) -> str | None:
        """None if this window may be acted in; else a short spoken reason. Fails closed."""
        try:
            return self._window_block(proc, title, focused, kind, key, hwnd)
        except Exception:
            return SAY_FAILED_CLOSED

    def _window_block(self, proc, title, focused, kind, key, hwnd) -> str | None:
        proc, title = (proc or "").lower(), title or ""
        if focused and focused.get("_error"):
            return SAY_FAILED_CLOSED
        typing = kind == "type" or (kind == "combo" and key.lower() in PASTE_COMBOS)    # a paste is typing too
        ck = (hwnd, title, proc, typing)
        now = self.clock()
        hit = self._cache.get(ck)
        if hit and now - hit[0] < 2.0:
            static = hit[1]
        else:
            static = self._static_block(proc, title, typing)
            self._cache = {k: v for k, v in self._cache.items() if now - v[0] < 2.0}
            self._cache[ck] = (now, static)
        if static:
            return static
        blob = _blob(focused)
        enter = key.lower() == "enter"
        if proc == "explorer.exe" and (kind == "type" or (kind in ("key", "combo") and enter)):
            if "addressband" in blob or ("address" in blob and "edit" in blob):
                return "That's the Explorer address bar, so I'll leave it to you."
        if proc in EDITOR_PROCESSES and kind in ("type", "key", "combo") and ("terminal" in blob or "xterm" in blob):
            return "That's the terminal inside the editor, so I won't type there."
        return None

    def _static_block(self, proc: str, title: str, typing: bool) -> str | None:
        if proc in BLOCKED_PROCESSES or (proc == "explorer.exe" and title == "Run"):
            what = BLOCKED_NAMES.get(proc, "the Run box" if title == "Run" else "that window")
            return (f"I won't click or type in {what}. One wrong key there could run a command or change "
                    f"system settings, so that one's yours.")
        if _ALWAYS_DENY_TITLE.search(title):
            return "That's a sign-in, security or private window, so I'll leave it to you."
        if typing and self.strict:
            bad = _SIGNIN_TITLE.search(title) if proc in BROWSER_PROCESSES else _SIGNIN_PREFIX.search(title)
            if bad:
                return "That looks like a sign-in, payment or banking page, so typing there is yours."
        return None

    # ---------- may JARVIS READ this window? (screen_read; clicking and typing have their own rules above) ----------
    def read_allowed(self, hwnd: int | None, facts: dict | None = None, probe=None) -> str | None:
        """None if the window may be read (UI Automation text or OCR), else a short spoken reason. Deny by default for
        sensitive windows, fails closed, and there is no "read it anyway" phrase.

        facts = {proc, title, address, own, excluded} (what Desktop.window_facts finds out); with none given the
        probe (self.probe, set by Desktop) is asked. Terminals ARE readable (local text, redacted afterwards) even
        though they are never clicked or typed in."""
        try:
            if self.settings is not None and self.settings.get("screen_reading") is False:
                return SAY_READING_OFF
            if facts is None:
                probe = probe or getattr(self, "probe", None)
                if probe is None or hwnd is None:
                    return SAY_FAILED_CLOSED
                facts = probe(hwnd)
            return self._read_block(facts or {})
        except Exception:
            return SAY_FAILED_CLOSED

    def _read_block(self, f: dict) -> str | None:
        proc, title = str(f.get("proc") or "").lower(), str(f.get("title") or "").strip()
        if f.get("_error") or not title:
            return SAY_FAILED_CLOSED                          # unreadable title: no way to tell what this is
        if f.get("own"):
            return SAY_OWN_WINDOW
        if f.get("excluded"):
            return SAY_PROTECTED
        if proc in READ_DENY_PROCESSES or _ALWAYS_DENY_TITLE.search(title) or _PASSWORD_MANAGER_TITLE.search(title):
            return SAY_READ_PRIVATE
        browser = proc in BROWSER_PROCESSES
        if browser:
            addr = f.get("address")
            if not addr:
                return SAY_FAILED_CLOSED                      # a browser whose address bar can't be read: could be a bank
            host = _host_of(str(addr))
            if not host or _sensitive_host(host):
                return SAY_READ_PRIVATE
            if _SIGNIN_TITLE.search(title) or _READ_FINANCE.search(title) or _READ_BROWSER_EXTRA.search(title):
                return SAY_READ_PRIVATE
        if _READ_PREFIX.search(title) or _READ_FINANCE.search(title):
            return SAY_READ_PRIVATE
        return None

    # ---------- the decision ----------
    def authorize(self, a: Action, focused: dict | None = None, confirmed: bool | None = None,
                  dry: bool = False) -> Decision:
        """dry=True only asks (no audit line, no breaker count): used to decide whether to ask the person first."""
        try:
            d = self._decide(a, focused, confirmed, dry)
        except Exception:
            d = Decision("deny", SAY_FAILED_CLOSED)
        if not dry and self.audit:
            try:
                self.audit.record(a, d, d.verdict)
            except Exception:
                pass
        return d

    def _decide(self, a: Action, focused, confirmed, dry) -> Decision:
        fp = (a.hwnd, a.proc, (a.title or "")[:60], a.label)
        if self.killed.is_set():
            return Decision("deny", SAY_KILLED)
        if confirmed is None:
            confirmed = bool(getattr(self._tls, "confirmed", False))
        agent = a.source == "agent" or getattr(self._tls, "source", None) == "agent"
        if a.kind in ("click", "type", "key", "scroll", "combo", "window"):
            why = self.window_block(a.proc, a.title, focused, a.kind, a.key, a.hwnd)
            if why:
                return Decision("deny", why)
        if a.kind == "combo" and a.key not in PRESSABLE_COMBOS:
            return Decision("deny", SAY_NO_COMBO)
        if a.kind == "window" and a.key not in WINDOW_VERBS:
            return Decision("deny", SAY_NO_COMBO)
        if (a.kind in ("type", "browser_fill") or (a.kind == "combo" and a.key in PASTE_COMBOS)) and focused:
            from .safety import sensitive_field
            f = focused
            if f.get("is_password") or sensitive_field(label=f"{f.get('name') or ''} {f.get('help') or ''}",
                                                       name=f"{f.get('automation_id') or ''} {f.get('class_name') or ''}"):
                return Decision("deny", SAY_SECRET_BOX)
        if a.kind == "browser_fill" and a.label:
            from .safety import sensitive_field
            if sensitive_field(label=a.label):
                return Decision("deny", SAY_SECRET_BOX)
        ask = self._needs_yes(a, focused)
        if ask and not confirmed:
            return Decision("confirm", ask, fp)
        if agent and not dry:
            enter = a.key.lower() == "enter" and a.kind in ("key", "browser_key")
            now = self.clock()
            with self._lock:
                for q, span in ((self._acts, 60.0), (self._enters, 30.0)):
                    while q and now - q[0] > span:
                        q.popleft()
                recent = sum(1 for t in self._acts if now - t <= 10.0)
                if recent >= 6 or len(self._acts) >= 40 or (enter and len(self._enters) >= 3):
                    tripped = True
                else:
                    tripped = False
                    self._acts.append(now)
                    if enter:
                        self._enters.append(now)
            if tripped:
                self.kill()
                return Decision("deny", SAY_TRIPPED)
        return Decision("allow", "", fp)

    def _needs_yes(self, a: Action, focused: dict | None) -> str:
        """A spoken reason to ask first, or ''."""
        if a.kind == "click":
            if self._risky_click(a):
                return f"That will press “{a.label}”."
        elif a.kind == "combo":
            if a.key in CONFIRM_COMBOS:
                return f"That will press {a.key}."
        elif a.kind == "window":
            if a.key == "close":
                return f"That will close {a.label or 'the window'}."
        elif a.kind == "key" and a.key.lower() == "enter" and self.strict:
            f = focused or {}
            blob = _blob(focused)
            ctype = str(f.get("ctype") or "").lower()
            if a.proc in CHAT_APPS:
                return "That will send it."
            if re.search(r"search|find", f"{f.get('name') or ''} {f.get('automation_id') or ''}", re.I):
                return ""
            if f.get("jarvis_edge") and "address" in blob:
                return ""
            if a.proc not in BROWSER_PROCESSES and ctype == "document":
                return ""                               # Enter in a text editor is just a new line
            return "Enter can submit or confirm things."
        return ""

    def _risky_click(self, a: Action) -> bool:
        label = re.sub(r"\s+", " ", a.label.lower()).strip()
        words = label.split()
        if not words or len(words) > (4 if not self.strict else 8) or (words[0] == "play" and len(words) > 1):   # "Play Delete Me" plays a song
            return False
        ctype = a.ctype.lower()
        legacy = CONFIRM_WORDS + RISK_EXTRA
        hit = lambda w: re.search(rf"\b{re.escape(w)}\b", label)  # noqa: E731
        if not self.strict:
            return ctype in _LEGACY_BUTTONS and any(hit(w) for w in CONFIRM_WORDS)
        if ctype in _CLICKABLE_BUTTONS:
            return any(hit(w) for w in legacy)
        return label in legacy                         # list items / text: only a label that IS the word ("Post Malone" is a song)
