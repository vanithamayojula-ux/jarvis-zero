"""A secret-free record of what JARVIS did with your keyboard and mouse: data/actions.log.

One JSON line per decision: when, what kind of action, which app and window, the control's name, allowed / asked / denied.
For typing it keeps only HOW MUCH was typed and whether it was letters, digits or a mix, never the text. Clipboard, page
and screen text never come here. Each line carries the hash of the line before it, so an edited or deleted line shows up
(Audit.verify). The file rotates at 1 MB and keeps two older copies.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")
GENESIS = "0" * 64


def _clean(s: str, n: int) -> str:
    return _CONTROL.sub("", s or "")[:n]


class Audit:
    def __init__(self, path: Path, max_bytes: int = 1_000_000, keep: int = 2, clock=time.time):
        self.path, self.max_bytes, self.keep, self.clock = Path(path), max_bytes, keep, clock
        self._lock = threading.Lock()
        self._prev = self._last_hash()

    def _last_hash(self) -> str:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return GENESIS
        return hashlib.sha256(lines[-1].encode("utf-8")).hexdigest() if lines else GENESIS

    def _rotate(self) -> None:
        for i in range(self.keep, 0, -1):
            src = self.path if i == 1 else self.path.with_name(f"{self.path.name}.{i - 1}")
            dst = self.path.with_name(f"{self.path.name}.{i}")
            if src.exists():
                dst.unlink(missing_ok=True)
                src.replace(dst)

    def record(self, action, decision, outcome: str = "") -> dict:
        """Append one line. `action` is a gate.Action: its text never reaches here, only text_len and text_class."""
        entry = {"time": datetime.fromtimestamp(self.clock()).isoformat(timespec="seconds"),
                 "kind": _clean(action.kind, 20), "app": _clean(action.proc, 40),
                 "title": _clean(action.title, 60), "label": _clean(action.label, 60),
                 "verdict": getattr(decision, "verdict", ""), "outcome": _clean(outcome, 40),
                 "source": _clean(action.source, 10), "text_len": int(action.text_len or 0),
                 "text_class": _clean(action.text_class, 10)}
        with self._lock:
            try:
                if self.path.exists() and self.path.stat().st_size >= self.max_bytes:
                    self._rotate()
            except OSError:
                pass
            entry["prev"] = self._prev
            line = json.dumps(entry, sort_keys=True, ensure_ascii=True)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
            self._prev = hashlib.sha256(line.encode("utf-8")).hexdigest()
        return entry

    def _read(self, path: Path) -> list[dict]:
        out = []
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if isinstance(e, dict):
                    out.append(e)
        except OSError:
            pass
        return out

    def tail(self, n: int = 5) -> list[dict]:
        with self._lock:
            rows = self._read(self.path)
            if len(rows) < n:                              # just rotated: older lines are in the .1 copy
                rows = self._read(self.path.with_name(self.path.name + ".1")) + rows
        return rows[-n:] if n > 0 else []

    def verify(self, path: Path | None = None) -> int | None:
        """None if the chain is intact, else the (0-based) number of the first line that doesn't follow its
        predecessor. A file that starts mid-chain (after a rotation) is checked from its second line."""
        try:
            lines = (path or self.path).read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        prev = None
        for i, line in enumerate(lines):
            try:
                e = json.loads(line)
            except ValueError:
                return i
            if prev is not None and e.get("prev") != prev:
                return i
            prev = hashlib.sha256(line.encode("utf-8")).hexdigest()
        return None

    def speak(self, n: int = 5) -> str:
        """The last few actions as a sentence or two, for 'what did you just do?'."""
        rows = [r for r in self.tail(n * 3) if r.get("kind") not in ("resume",)][-n:]
        if not rows:
            return "I haven't done anything with your keyboard or mouse yet."
        verbs = {"click": "clicked", "type": "typed", "key": "pressed a key", "scroll": "scrolled", "media": "pressed a media key",
                 "combo": "pressed a shortcut", "browser_click": "clicked on a page", "browser_fill": "filled a field",
                 "browser_key": "pressed a key on a page", "window": "switched window", "setting": "changed a setting",
                 "read": "read a window"}
        parts = []
        for r in rows:
            what = verbs.get(r.get("kind", ""), r.get("kind", "did something"))
            if r.get("kind") == "type":
                what += f" {r.get('text_len', 0)} characters"
            elif r.get("label"):
                what += f" {r['label']}"
            where = f" in {r['app'].removesuffix('.exe')}" if r.get("app") else ""
            verdict = {"deny": " (refused)", "confirm": " (asked first)"}.get(r.get("verdict"), "")
            when = (r.get("time") or "")[11:16]
            parts.append(f"{when} {what}{where}{verdict}".strip())
        return "Lately: " + "; ".join(parts) + "."
