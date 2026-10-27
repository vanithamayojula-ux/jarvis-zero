"""JARVIS Zero setup: pick Chat or Voice mode, and (optionally) give JARVIS a web search key.

  setup.bat            (double-click), or:  .venv\\Scripts\\python.exe scripts\\setup.py

Chat  = you type (spidey's bar, Ctrl+Alt+0), JARVIS answers in text. No mic, no speaker, no voice models loaded.
Voice = you talk (Ctrl+Alt+J), JARVIS talks back.
The search key only fetches facts (news, prices, scores, weather); JARVIS's own model writes the answer.
Keys go into .env (git-ignored) and are never printed."""
from __future__ import annotations

import getpass
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from jarvis.config import Settings, api_token  # noqa: E402  (reads .env)
from jarvis.websearch import PROVIDERS, SIGNUP, SearchError, WebSearch  # noqa: E402

URL = "http://127.0.0.1:8600"
ENV = ROOT / ".env"


def ask(prompt: str, choices: dict[str, str], default: str) -> str:
    while True:
        got = input(f"{prompt} [{default}]: ").strip().lower() or default
        if got in choices:
            return choices[got]
        print("  Please type one of: " + ", ".join(choices))


def running() -> bool:
    try:
        req = urllib.request.Request(f"{URL}/api/status", headers={"x-jarvis-token": api_token()})
        urllib.request.urlopen(req, timeout=2).read()
        return True
    except Exception:
        return False


def apply_settings(changes: dict) -> None:
    """Running JARVIS keeps its settings in memory (and would overwrite the file), so tell it directly."""
    if running():
        req = urllib.request.Request(f"{URL}/api/settings", data=json.dumps({"changes": changes}).encode(), method="PATCH",
                                     headers={"x-jarvis-token": api_token(), "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()
    else:
        Settings().update(changes)


def save_key(var: str, key: str) -> None:
    lines = ENV.read_text(encoding="utf-8").splitlines() if ENV.exists() else []
    lines = [ln for ln in lines if not ln.startswith(f"{var}=")] + [f"{var}={key}"]
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[var] = key


def main() -> None:
    print("\n  Spidey setup\n  ------------\n")
    print("  1) Chat  - you type, Spidey answers in text. Silent and the lightest on the laptop")
    print("             (no microphone, no speaker, no voice models in memory).")
    print("  2) Voice - you talk (Ctrl+Alt+J) and Spidey talks back.\n")
    current = Settings().get("io_mode")
    mode = ask("  Which mode?  1 = Chat, 2 = Voice", {"1": "chat", "chat": "chat", "2": "voice", "voice": "voice"},
               "1" if current == "chat" else "2")
    apply_settings({"io_mode": mode})
    print(f"  -> {mode.title()} mode. You can switch any time: right-click spidey > Mode, or say/type "
          f"\"chat mode\" / \"voice mode\".\n")

    have = [p for p, var in PROVIDERS.items() if os.environ.get(var, "").strip()]
    print("  Internet lookups: Spidey can fetch facts it can't know (news, prices, scores, weather).")
    print("  Only your search words are sent to the search service. Free keys, no card needed:")
    print(f"    Tavily (recommended, 1000 searches/month): {SIGNUP['tavily']}")
    print(f"    Brave Search: {SIGNUP['brave']}      Serper (Google results): {SIGNUP['serper']}")
    if have:
        print(f"  A {have[0].title()} key is already set up.")
    choice = ask("  Add a key?  t = Tavily, b = Brave, s = Serper, n = skip",
                 {"t": "tavily", "b": "brave", "s": "serper", "n": "", "skip": ""}, "n")
    restart = False
    if choice:
        key = getpass.getpass(f"  Paste your {choice.title()} key (hidden), then Enter: ").strip()
        if not re.fullmatch(r"[\w\-.]{8,200}", key):
            print("  That doesn't look like a key, so nothing was saved.")
        else:
            save_key(PROVIDERS[choice], key)
            settings = Settings()
            print("  Testing it with one search...", end=" ", flush=True)
            try:
                os.environ.update({v: "" for p, v in PROVIDERS.items() if p != choice})   # test this key only
                hits = WebSearch(settings).search("current time in India")
                print(f"works ({len(hits)} results).")
            except SearchError as exc:
                print(f"failed ({exc}). It's saved anyway: check it on the provider's site.")
            apply_settings({"search_provider": choice, "web_search": True})
            restart = True
    print()
    if restart and running():
        if ask("  Spidey is running. Restart it now so it picks up the key? y/n", {"y": True, "n": False}, "y"):
            ps = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"]
            subprocess.run(ps + [str(ROOT / "stop-jarvis.ps1")], check=False)
            subprocess.run(ps + [str(ROOT / "launch.ps1")], check=False)
    print("  All set. Spidey's bar: click spidey or press Ctrl+Alt+0. Try \"type Hello World\" or "
          "\"look up today's top news\".\n")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\n  Setup cancelled; nothing else changed.")
