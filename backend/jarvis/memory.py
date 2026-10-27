"""What JARVIS remembers about you across conversations. A plain JSON file on this laptop (data/memory.json).

* facts: short lines about you and your life ("Their friend Rahul lives in Pune", "They like Hanumankind's music"),
  added when you say "remember that…", or picked up from chat by the cloud model (only things *you* said).
  The newest ones are put in front of every reply, so you don't have to repeat yourself.
* recent: the last few exchanges, so a restart or a crash doesn't lose the thread you were on.

"What do you remember about me?" lists the facts; "forget that…" removes one; "forget everything about me" wipes them.
"""
from __future__ import annotations

import difflib
import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


class Memory:
    MAX_FACTS = 80          # kept on disk
    IN_PROMPT = 25          # put in front of a reply (the free tier counts tokens per minute)
    RECENT = 12             # messages carried over a restart
    RECENT_MAX_AGE_H = 12

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.data = {"facts": [], "recent": [], "recent_at": 0.0}
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self.data.update({k: loaded[k] for k in ("facts", "recent", "recent_at") if k in loaded})
        except (OSError, ValueError):
            pass
        self.data["facts"] = [f for f in self.data["facts"] if isinstance(f, dict) and f.get("text")]

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass

    # ---- facts ----
    def facts(self) -> list[str]:
        return [f["text"] for f in self.data["facts"]]

    def _similar(self, a: str, b: str) -> float:
        return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()

    def add(self, text: str, source: str = "you") -> bool:
        """Store a fact; a near-duplicate replaces the older wording instead of piling up."""
        text = re.sub(r"\s+", " ", text).strip(" .")
        if len(text) < 3 or len(text) > 200:
            return False
        text = text[0].upper() + text[1:] + "."
        with self._lock:
            facts = self.data["facts"]
            for f in facts:
                if self._similar(f["text"], text) >= 0.85:
                    f.update(text=text, added=datetime.now().isoformat(timespec="seconds"), source=source)
                    break
            else:
                facts.append({"text": text, "added": datetime.now().isoformat(timespec="seconds"), "source": source})
            del facts[:-self.MAX_FACTS]
            self._save()
        return True

    def remove(self, query: str) -> list[str]:
        """Forget the facts that match what was said ("forget that Rahul lives in Pune", "forget about my exam")."""
        q = _norm(query)
        words = [w for w in q.split() if len(w) > 2 and w not in ("that", "the", "about", "my", "you", "remember")]
        if not words:
            return []
        with self._lock:
            gone = [f for f in self.data["facts"]
                    if self._similar(f["text"], query) >= 0.75 or all(w in _norm(f["text"]) for w in words)]
            self.data["facts"] = [f for f in self.data["facts"] if f not in gone]
            if gone:
                self._save()
        return [f["text"] for f in gone]

    def clear(self) -> int:
        with self._lock:
            n = len(self.data["facts"])
            self.data["facts"], self.data["recent"] = [], []
            self._save()
        return n

    def prompt_block(self, user: str = "the user") -> str:
        facts = self.facts()[-self.IN_PROMPT:]
        if not facts:
            return ""
        return (f"\nWhat you remember about {user} from earlier conversations. Use it naturally when it's relevant, "
                f"don't recite it, and don't bring it up out of nowhere:\n" + "\n".join(f"- {f}" for f in facts))

    # ---- the thread you were on ----
    def save_recent(self, history: list[dict]) -> None:
        msgs = [{"role": m["role"], "content": str(m.get("content", ""))[:600]} for m in history
                if m.get("role") in ("user", "assistant") and m.get("content") and not m.get("screen")]
        with self._lock:
            self.data["recent"], self.data["recent_at"] = msgs[-self.RECENT:], time.time()
            self._save()

    def recent(self) -> list[dict]:
        if time.time() - float(self.data.get("recent_at") or 0) > self.RECENT_MAX_AGE_H * 3600:
            return []
        return [dict(m) for m in self.data.get("recent", [])]

    def drop_recent(self) -> None:
        with self._lock:
            self.data["recent"] = []
            self._save()


EXTRACT_PROMPT = """You keep a small memory for a voice assistant about the person using it.
Look at what the USER just said (and the assistant's reply for context). Pick out at most two facts worth
remembering for weeks: their name, people in their life (friends, family) and who they are, things they like or
dislike, their studies, work or projects, plans with dates, and how they want the assistant to behave.
Ignore: commands and requests, questions, small talk, jokes, anything the assistant said, and anything already known.
Write each fact as one short third-person line under 15 words, like "Their friend Rahul lives in Pune".
If the new message shows a known fact is now wrong, list that old fact under "remove".
Reply with JSON only: {{"add": [...], "remove": [...]}}

Already known:
{known}"""


def parse_extraction(text: str) -> tuple[list[str], list[str]]:
    """The model's JSON reply → (facts to add, facts to remove). Anything malformed → nothing."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return [], []
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return [], []
    add = [str(x) for x in data.get("add", []) if isinstance(x, str) and x.strip()][:2]
    remove = [str(x) for x in data.get("remove", []) if isinstance(x, str) and x.strip()][:3]
    return add, remove
