"""The apps on this laptop, and what Spidey may do with each one (you choose, in the panel's Apps list).

Three levels per app, stored in settings["app_permissions"] by program name (spotify.exe → "full"):
  off   Spidey never opens it, never clicks or types in it, and won't read its window;
  open  Spidey may open it and move its window (minimise, snap, switch to it), nothing inside it;
  full  Spidey may also click, type and press keys in it. Every gate rule still applies on top: no password / card / OTP
        boxes, Send / Buy / Delete ask first, the kill switch, the rate breaker.

An app with no choice yet counts as "open" when it was already openable (approved_apps or a Desktop shortcut) and "off"
for clicking and typing. Some programs can never be given control (terminals, Registry Editor, remote-control tools,
password managers…): they show up locked, with the reason.

The list comes from Windows itself (Get-StartApps, the Start menu shortcuts and the Store apps' manifests), found once in
the background and kept in data/apps.json. Nothing about it leaves the laptop.
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
import threading
import time
from pathlib import Path

from .config import DATA_DIR
from .gate import BLOCKED_PROCESSES

LEVELS = ("off", "open", "full")
RANK = {"off": 0, "open": 1, "full": 2}
CATALOG_PATH = DATA_DIR / "apps.json"

# Where an app's real window process differs from the program Windows lists for it.
EXE_FIX = {"spotifymigrator.exe": "spotify.exe", "stickynotesstub.exe": "microsoft.notes.exe",
           "blender-launcher.exe": "blender.exe", "copilotapp_proxy.exe": "copilot.exe",
           "riotclientservices.exe": "riotclientux.exe"}
# Ids Get-StartApps gives without any path (browsers registered by name, Office): the program behind them.
ID_EXE = {"chrome": "chrome.exe", "msedge": "msedge.exe", "brave": "brave.exe", "microsoft.windows.explorer": "explorer.exe",
          "microsoft.visualstudiocode": "code.exe", "anysphere.cursor": "cursor.exe",
          "microsoft.skydrive.desktop": "onedrive.exe", "microsoft.windows.mediaplayer32": "wmplayer.exe"}

# Never controllable, whatever is chosen: these can run commands, change the system, hand the laptop to someone else,
# or type for Spidey into any window. Opening them is left to you as well.
LOCKED_EXES = BLOCKED_PROCESSES | {
    "git-bash.exe", "git-cmd.exe", "python.exe", "pythonw.exe", "py.exe", "py-manager.exe", "node.exe", "osk.exe",
    "voiceaccess.exe", "quickassist.exe", "parsecd.exe", "pad.console.host.exe", "psr.exe", "msconfig.exe",
    "recoverydrive.exe", "cleanmgr.exe", "dfrgui.exe", "mdsched.exe", "iscsicpl.exe", "odbcad32.exe", "appverif.exe",
    "rundll32.exe", "regsvr32.exe", "msinfo32.exe", "perfmon.exe", "eventvwr.exe", "dpinst-x86.exe", "dpinst-amd64.exe",
    "uninstall.exe", "setup.exe", "pg_ctl.exe", "javacpl.exe", "comexp.msc", "clicktodo.exe", "wsl.exe", "guimain.exe"}
AGENT_EXES = {"claude.exe", "opencode.exe", "antigravity.exe", "antigravity ide.exe", "codex.exe", "windsurf.exe"}
AGENT_REASON = "is an AI agent that runs commands, so typing into it could run anything; it stays yours"
EDITOR_NOTE = "has a terminal inside; I never type in that part"
_LOCKED_NAME = re.compile(r"command prompt|powershell|terminal|\bcmd\b|registry|task manager|windows security|firewall|"
                          r"remote desktop|quick assist|\bservices\b|computer management|task scheduler|event viewer|"
                          r"system configuration|recovery|power automate|on-screen keyboard|voice access|\brun\b|"
                          r"control panel|^settings$|windows tools|disk cleanup|steps recorder|python|node\.js|git (?:bash|cmd)",
                          re.I)
LOCK_REASON = "can run commands or change the system, so it stays yours"
# Not apps at all: help files, web links, read-me files, installers.
_HIDE_NAME = re.compile(r"\b(?:uninstall|install|help|documentation|manual|release notes|readme|licen[cs]e|what is new|"
                        r"faq|samples?|tools for|website|online documentation|module docs|visit|support center|"
                        r"migration guide|telemetry|upload center|language preferences|check for updates)\b", re.I)
_DOC_EXT = (".txt", ".chm", ".htm", ".html", ".rtf", ".pdf", ".url", ".msi", ".ico", ".bat", ".cmd", ".ps1", ".vbs",
            ".msc", ".lnk")
# A shell:AppsFolder id we're willing to hand to explorer.exe: letters, digits and the punctuation real ids use.
_SAFE_ID = re.compile(r"^[\w .{}()\-\\!~+,＆]+$")

_SCAN = r"""
$ErrorActionPreference = 'SilentlyContinue'
$sh = New-Object -ComObject WScript.Shell
$links = @{}
foreach ($root in @("$env:ProgramData\Microsoft\Windows\Start Menu\Programs", "$env:APPDATA\Microsoft\Windows\Start Menu\Programs")) {
  Get-ChildItem -LiteralPath $root -Recurse -Filter *.lnk | ForEach-Object {
    $t = $sh.CreateShortcut($_.FullName).TargetPath
    if ($t -and -not $links.ContainsKey($_.BaseName)) { $links[$_.BaseName] = $t }
  }
}
$pk = @{}
Get-AppxPackage | ForEach-Object { $pk[$_.PackageFamilyName] = $_.InstallLocation }
$out = foreach ($a in Get-StartApps) {
  $exe = ''; $web = $false; $id = $a.AppID
  if ($id -match '^(?<fam>[^!]+)!(?<app>.+)$') {
    if ($pk.ContainsKey($matches.fam)) {
      $app = $matches.app
      try {
        [xml]$m = Get-Content -LiteralPath (Join-Path $pk[$matches.fam] 'AppxManifest.xml') -Raw
        $node = $m.Package.Applications.Application | Where-Object { $_.Id -eq $app } | Select-Object -First 1
        if ($node) { $exe = [string]$node.Executable; if (-not $exe -and $node.StartPage) { $web = $true } }
      } catch {}
    } else { $web = $true }
  } elseif ($links.ContainsKey($a.Name)) { $exe = $links[$a.Name] }
  elseif ($id -match '\.exe$') { $exe = $id }
  [pscustomobject]@{ name = [string]$a.Name; id = [string]$id; exe = [string]$exe; web = $web }
}
@($out) | ConvertTo-Json -Compress
"""


def classify(raw: dict) -> dict | None:
    """One Get-StartApps row → {name, id, exe, kind, note} or None for things that aren't apps (help files, links…).
    kind: app | web (a site installed as an app: it runs inside a browser) | locked."""
    name = re.sub(r"\s+", " ", str(raw.get("name") or "")).strip()
    app_id = str(raw.get("id") or "").strip()
    path = str(raw.get("exe") or "").strip()
    if not name or not app_id or len(name) > 80:
        return None
    low_id = app_id.lower()
    if re.match(r"^(?:https?|file|steam|ms-[a-z-]+):", low_id):
        return None
    exe = Path(path.replace("\\", "/")).name.lower() if path else ID_EXE.get(low_id, "")
    if not exe:
        m = re.match(r"^microsoft\.office\.([a-z0-9]+\.exe)\.\d+$", low_id)
        exe = m.group(1) if m else ""
    exe = EXE_FIX.get(exe, exe)
    if exe.endswith(_DOC_EXT) or (path and "." not in exe and not raw.get("web")):
        return None                                   # a document, a folder or a script, not a program
    if _HIDE_NAME.search(name):
        return None
    entry = {"name": name, "id": app_id, "exe": exe, "kind": "app", "note": ""}
    if exe in AGENT_EXES:
        entry.update(kind="locked", note=AGENT_REASON)
    elif exe in LOCKED_EXES or _LOCKED_NAME.search(name):
        entry.update(kind="locked", note=LOCK_REASON)
    elif exe in ("code.exe", "cursor.exe", "code - insiders.exe"):
        entry["note"] = EDITOR_NOTE
    elif raw.get("web") or (not exe and "!" in app_id):
        entry.update(kind="web", exe="", note="a website installed as an app; clicking inside follows its browser")
    elif not exe:
        entry["note"] = "I can open it, but can't tell which program it runs, so no control inside it"
    return entry


def launch_argv(entry: dict) -> list[str] | None:
    """How to start a listed app without any shell in between: explorer.exe shell:AppsFolder\\<id>."""
    app_id = str(entry.get("id") or "")
    if not app_id or len(app_id) > 200 or not _SAFE_ID.match(app_id) or ".." in app_id:
        return None
    return ["explorer.exe", f"shell:AppsFolder\\{app_id}"]


def perm_key(entry: dict) -> str:
    """What a choice is stored under: the program (what the click/type gate sees), else the app's id."""
    return entry["exe"] or f"id:{entry['id']}"


class Catalog:
    """The list of apps (found by Windows, cached in data/apps.json, refreshed in the background)."""

    def __init__(self, path: Path = CATALOG_PATH, runner=None):
        self.path, self._runner = path, runner or _run_scan
        self._lock = threading.Lock()
        self._apps: list[dict] = []
        self.scanned_at = 0.0
        self.scanning = False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self._apps, self.scanned_at = list(data.get("apps") or []), float(data.get("at") or 0)
        except (OSError, ValueError, AttributeError):
            pass

    def apps(self) -> list[dict]:
        with self._lock:
            return [dict(a) for a in self._apps]

    def refresh(self) -> list[dict]:
        """Ask Windows again (about 4 seconds). Keeps the old list if that fails."""
        self.scanning = True
        try:
            rows = self._runner()
            found, seen = [], set()
            for raw in rows or []:
                e = classify(raw)
                if e and (e["name"].lower(), e["exe"]) not in seen:
                    seen.add((e["name"].lower(), e["exe"]))
                    found.append(e)
            found.sort(key=lambda e: (e["kind"] == "locked", e["name"].lower()))
            if found:
                with self._lock:
                    self._apps, self.scanned_at = found, time.time()
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    self.path.write_text(json.dumps({"at": self.scanned_at, "apps": found}, indent=1), encoding="utf-8")
                except OSError:
                    pass
            return self.apps()
        finally:
            self.scanning = False

    def refresh_soon(self, max_age_s: float = 6 * 3600) -> None:
        if self.scanning or (self._apps and time.time() - self.scanned_at < max_age_s):
            return
        self.scanning = True
        threading.Thread(target=self.refresh, daemon=True, name="app-scan").start()

    def by_name(self, name: str) -> dict | None:
        want = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
        if not want:
            return None
        apps = self.apps()
        norm = lambda s: re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()  # noqa: E731
        return next((a for a in apps if norm(a["name"]) == want), None) or \
            next((a for a in apps if norm(a["name"]).startswith(want + " ") or norm(a["name"]).removeprefix("microsoft ") == want),
                 None)

    def by_exe(self, exe: str) -> dict | None:
        exe = exe.lower()
        return next((a for a in self.apps() if a["exe"] == exe), None)


def _run_scan() -> list[dict]:
    script = base64.b64encode(_SCAN.encode("utf-16-le")).decode()
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", script],
                         capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    data = json.loads(out.stdout.strip().lstrip("﻿") or "[]")
    return data if isinstance(data, list) else [data]


def level_of(settings, key: str, openable: bool = False) -> str:
    """The level chosen for a program / app id; with no choice: "open" if it was already openable, else "off"."""
    perms = (settings.get("app_permissions") if settings is not None else None) or {}
    level = perms.get(key.lower()) if key else None
    if level in RANK:
        return level
    return "open" if openable else "off"


def tightening(old: dict, new: dict) -> bool:
    """True if every change only lowers a level (that needs no question; widening waits for your yes)."""
    for key, level in (new or {}).items():
        if level not in RANK:
            return False
        before = (old or {}).get(key)
        if before is None or RANK[level] > RANK.get(before, 0):
            if not (before is None and level == "off"):
                return False
    return True


CATALOG: Catalog | None = None


def catalog() -> Catalog:
    global CATALOG
    if CATALOG is None:
        CATALOG = Catalog()
    return CATALOG
