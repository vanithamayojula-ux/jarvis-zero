r"""JARVIS upgrade council: five free Groq agents argue about how to improve JARVIS and pick the best upgrades.

SAFE BY DESIGN (the person's rule: security first):
  * The agents see ONE thing: scripts/council/brief.md (written for them, no personal data). No chats, memory,
    logs, files, screen or anything else from the laptop.
  * They can't do anything. This script only sends text to Groq and writes text files. Nothing they say is ever run,
    and JARVIS's code is never changed by the council: the report waits for the person's approval.
  * The Sentinel (security lead) can veto any proposal; vetoed ideas never win.
  * Capped at MAX_REQUESTS_PER_DAY Groq requests, so JARVIS keeps most of the free daily allowance.

Run: scripts\council\council.ps1   (in the background, no windows; also scheduled daily at 9 PM)
     .venv\Scripts\python.exe scripts\council\council.py   (the same, showing the chair's notes)
Output: data/council/<date>.md (the decision), data/council/<date>-transcript.md, per-agent logs in data/council/live/
(colour-coded, readable with: Get-Content data\council\live\sentinel.log). Spidey announces a new decision.
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
BRIEF = Path(__file__).with_name("brief.md")
OUT = ROOT / "data" / "council"
LIVE = OUT / "live"
GROQ = "https://api.groq.com/openai/v1/chat/completions"
MAX_REQUESTS_PER_DAY = 60
RESET, BOLD, DIM = "\x1b[0m", "\x1b[1m", "\x1b[2m"

AGENTS = [
    {"key": "sentinel", "name": "Sentinel", "color": "\x1b[91m", "model": "openai/gpt-oss-120b",
     "role": "the SECURITY LEAD. You have a veto. You protect the person's data, accounts and laptop above everything. "
             "You distrust anything that adds network access, permissions, autonomy, stored secrets or attack surface. "
             "You also propose security hardening yourself."},
    {"key": "architect", "name": "Architect", "color": "\x1b[94m", "model": "qwen/qwen3.8-27b",
     "role": "the SOFTWARE ARCHITECT. You care about clean design, small focused changes, and things that last. "
             "You push back on hacks and on big rewrites."},
    {"key": "tester", "name": "Tester", "color": "\x1b[92m", "model": "openai/gpt-oss-20b",
     "role": "the RELIABILITY AND QA ENGINEER. You care about what breaks in real use: flaky behaviour, edge cases, "
             "honest error messages and testability. Every idea must come with a concrete test."},
    {"key": "voice", "name": "Voice", "color": "\x1b[95m", "model": "qwen/qwen3.8-27b",
     "role": "the VOICE AND EXPERIENCE DESIGNER. You care about how JARVIS feels to talk to: speed, understanding "
             "messy speech and Indian names, natural replies, and the spidey bot."},
    {"key": "skeptic", "name": "Skeptic", "color": "\x1b[93m", "model": "openai/gpt-oss-20b",
     "role": "the SKEPTIC AND DEVIL'S ADVOCATE. You attack every idea: is it really needed, does it fit the free "
             "limits and this laptop, what could go wrong, is there a simpler way. You argue hard but fairly."},
]
SENTINEL = "sentinel"

SYSTEM = """You are {name}, {role}
You sit on a five-member council deciding the next upgrades for JARVIS Zero, a voice assistant. You argue with
the others (Sentinel, Architect, Tester, Voice, Skeptic): disagree when you disagree, be direct, be specific.
Everything you know about the project is in the brief below. Don't invent features, files or facts beyond it.
Never ask for, mention or guess any personal data about the person.
Hard rules: security and privacy first; zero cost; no telemetry; JARVIS only acts when asked; every change testable.
Reply with JSON only, exactly the shape asked for.

=== BRIEF ===
{brief}"""


# ---------------- plumbing ----------------
def groq_key() -> str:
    env = ROOT / ".env"
    for line in env.read_text(encoding="utf-8-sig").splitlines() if env.exists() else []:
        if line.startswith("GROQ_API_KEY="):
            return line.split("=", 1)[1].strip()
    return ""


class Budget:
    """Requests used today, kept in data/council/usage.json."""

    def __init__(self, path: Path, cap: int = MAX_REQUESTS_PER_DAY):
        self.path, self.cap, self.day = path, cap, datetime.now().strftime("%Y-%m-%d")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        self.used = data.get(self.day, 0)

    def take(self) -> bool:
        if self.used >= self.cap:
            return False
        self.used += 1
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({self.day: self.used}), encoding="utf-8")
        return True


def parse_json(text: str | None) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


class Council:
    def __init__(self, brief: str, key: str, transport=None, budget: Budget | None = None, out: Path = OUT, pause: float = 2.0):
        self.brief, self.key, self.out, self.pause = brief, key, out, pause
        self.client = httpx.Client(timeout=90, transport=transport)
        self.budget = budget or Budget(out / "usage.json")
        self.live = out / "live"
        self.live.mkdir(parents=True, exist_ok=True)
        self.transcript: list[str] = []

    # ---- output: one coloured log per agent (their terminal), plus the chair ----
    def say(self, who: str, text: str) -> None:
        agent = next((a for a in AGENTS if a["key"] == who), None)
        color, name = (agent["color"], agent["name"]) if agent else ("\x1b[97m", "Chair")
        stamp = datetime.now().strftime("%H:%M:%S")
        line = f"{DIM}{stamp}{RESET} {color}{BOLD}{name}:{RESET} {color}{text}{RESET}\n"
        for log in {f"{who}.log", "chair.log"}:            # the agent's own terminal, and the chair's overview
            with open(self.live / log, "a", encoding="utf-8") as f:
                f.write(line)
        self.transcript.append(f"**{name}:** {text}")
        if who == "chair":
            print(line, end="", flush=True)

    def ask(self, agent: dict, task: str, max_tokens: int = 900) -> dict:
        """One agent thinks about one task. Returns its JSON (or {} if the free limit says no)."""
        if not self.budget.take():
            self.say("chair", f"Daily council budget ({self.budget.cap} requests) used up; {agent['name']} sits this one out.")
            return {}
        if agent["model"].startswith("openai/gpt-oss"):
            max_tokens += 2500                               # its hidden thinking counts against the reply budget
        body = {"model": agent["model"], "max_tokens": max_tokens, "temperature": 0.7,
                "messages": [{"role": "system", "content": SYSTEM.format(name=agent["name"], role=agent["role"],
                                                                         brief=self.brief)},
                             {"role": "user", "content": task}]}
        if agent["model"].startswith("qwen/"):
            body["reasoning_effort"] = "none"
        elif agent["model"].startswith("openai/gpt-oss"):
            body.update(reasoning_effort="low", include_reasoning=False)
        for attempt in range(4):
            try:
                r = self.client.post(GROQ, headers={"Authorization": f"Bearer {self.key}"}, json=body)
            except httpx.HTTPError as exc:
                self.say("chair", f"{agent['name']} lost the connection ({type(exc).__name__}), retrying.")
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code == 429:                        # free per-minute limit: wait as Groq asks
                wait = min(60.0, float(r.headers.get("retry-after", "20") or 20))
                self.say("chair", f"{agent['name']} is waiting {wait:.0f}s for the free rate limit.")
                time.sleep(wait)
                continue
            if r.status_code != 200:
                self.say("chair", f"{agent['name']} couldn't answer (HTTP {r.status_code}).")
                return {}
            time.sleep(self.pause)
            return parse_json(r.json()["choices"][0]["message"].get("content", ""))
        return {}

    # ---------------- the meeting ----------------
    def run(self) -> dict:
        self.say("chair", "Council in session. Brief handed out. Security first; the Sentinel holds a veto.")
        # Round 1: proposals
        proposals: list[dict] = []
        for a in AGENTS:
            out = self.ask(a, 'Round 1. Propose at most two upgrades. JSON: {"proposals": [{"title": "...", "problem": "...", '
                              '"change": "what exactly to build, concretely", "why_best": "...", '
                              '"security_risk": "honest risk and how to contain it", "test": "how to prove it works"}]}')
            for p in (out.get("proposals") or [])[:2]:
                if isinstance(p, dict) and p.get("title"):
                    p = {k: str(v)[:700] for k, v in p.items() if k in
                         ("title", "problem", "change", "why_best", "security_risk", "test")}
                    p["id"], p["author"] = f"P{len(proposals) + 1}", a["key"]
                    proposals.append(p)
                    self.say(a["key"], f"[{p['id']}] PROPOSE: {p['title']}. {p.get('change', '')} "
                                       f"Why: {p.get('why_best', '')} Risk: {p.get('security_risk', '')}")
        if not proposals:
            self.say("chair", "No proposals came in (cloud unavailable or budget used). Meeting closed.")
            return {"winners": [], "vetoed": [], "proposals": []}
        listing = "\n".join(f"{p['id']} by {p['author']}: {p['title']}. Change: {p.get('change', '')} "
                            f"Risk: {p.get('security_risk', '')} Test: {p.get('test', '')}" for p in proposals)

        # Round 2: critique (the Sentinel's vetoes count)
        reviews: dict[str, list[dict]] = {p["id"]: [] for p in proposals}
        vetoed: dict[str, str] = {}
        for a in AGENTS:
            veto_note = ('You may veto proposals that endanger security or privacy: "veto": [{"id": "P3", "reason": "..."}]. '
                         if a["key"] == SENTINEL else 'Flag security worries in your objection; only the Sentinel can veto. ')
            out = self.ask(a, f"Round 2. Critique the proposals below. Be tough and specific. {veto_note}"
                              'JSON: {"reviews": [{"id": "P1", "score": 1-5, "objection": "..."}], "veto": []}\n\n' + listing)
            for rv in out.get("reviews") or []:
                if isinstance(rv, dict) and rv.get("id") in reviews:
                    try:
                        score = max(1, min(5, int(rv.get("score", 3))))
                    except (TypeError, ValueError):
                        score = 3
                    reviews[rv["id"]].append({"by": a["key"], "score": score, "objection": str(rv.get("objection", ""))[:500]})
                    self.say(a["key"], f"on {rv['id']} ({score}/5): {rv.get('objection', '')}")
            if a["key"] == SENTINEL:
                for v in out.get("veto") or []:
                    if isinstance(v, dict) and v.get("id") in reviews:
                        vetoed[v["id"]] = str(v.get("reason", "security"))[:400]
                        self.say(a["key"], f"VETO {v['id']}: {v.get('reason', '')}")

        # Round 3: authors answer the objections (and may revise)
        for a in AGENTS:
            mine = [p for p in proposals if p["author"] == a["key"] and p["id"] not in vetoed]
            if not mine:
                continue
            objections = "\n".join(f"{p['id']} ({p['title']}): " + " | ".join(
                f"{r['by']}: {r['objection']}" for r in reviews[p["id"]] if r["by"] != a["key"]) for p in mine)
            out = self.ask(a, "Round 3. Answer the objections to YOUR proposals. Concede where they're right and revise "
                              'the change if needed. JSON: {"responses": [{"id": "P1", "answer": "...", '
                              '"revised_change": "... or empty"}]}\n\n' + objections)
            for resp in out.get("responses") or []:
                p = next((p for p in mine if isinstance(resp, dict) and p["id"] == resp.get("id")), None)
                if p:
                    if resp.get("revised_change"):
                        p["change"] = str(resp["revised_change"])[:700]
                    p["answer"] = str(resp.get("answer", ""))[:500]
                    self.say(a["key"], f"defends {p['id']}: {resp.get('answer', '')}"
                                       + (f" Revised: {resp['revised_change']}" if resp.get("revised_change") else ""))

        # Round 4: final vote (Borda count) plus a last Sentinel veto
        alive = [p for p in proposals if p["id"] not in vetoed]
        listing = "\n".join(f"{p['id']}: {p['title']}. Change: {p.get('change', '')} Defence: {p.get('answer', '')}"
                            for p in alive)
        points = {p["id"]: 0.0 for p in alive}
        for a in AGENTS:
            extra = ' Add "veto": [{"id": ..., "reason": ...}] if anything is still unsafe.' if a["key"] == SENTINEL else ""
            out = self.ask(a, f"Round 4, final vote. Rank your top three (best first) and say why in one sentence.{extra} "
                              'JSON: {"ranking": ["P2", "P5", "P1"], "reason": "...", "veto": []}\n\n' + listing,
                           max_tokens=500)
            ranking = [r for r in (out.get("ranking") or []) if r in points][:3]
            for i, pid in enumerate(ranking):
                points[pid] += 3 - i
            if ranking:
                self.say(a["key"], f"VOTES {', '.join(ranking)}: {out.get('reason', '')}")
            if a["key"] == SENTINEL:
                for v in out.get("veto") or []:
                    if isinstance(v, dict) and v.get("id") in points:
                        vetoed[v["id"]] = str(v.get("reason", "security"))[:400]
                        self.say(a["key"], f"FINAL VETO {v['id']}: {v.get('reason', '')}")
        return self.decide(proposals, reviews, points, vetoed)

    @staticmethod
    def decide(proposals, reviews, points, vetoed) -> dict:
        """Votes first, then the average critique score; vetoed proposals can never win."""
        def avg(pid):
            s = [r["score"] for r in reviews.get(pid, [])]
            return sum(s) / len(s) if s else 0.0
        ranked = sorted((p for p in proposals if p["id"] not in vetoed and p["id"] in points),
                        key=lambda p: (points[p["id"]], avg(p["id"])), reverse=True)
        winners = [{**p, "points": points[p["id"]], "avg_score": round(avg(p["id"]), 2),
                    "objections": [r for r in reviews.get(p["id"], []) if r["objection"]]} for p in ranked[:3]]
        return {"winners": winners, "vetoed": [{"id": k, "reason": v, "title": next((p["title"] for p in proposals
                                                                                   if p["id"] == k), "")}
                                               for k, v in vetoed.items()], "proposals": proposals}

    def write_report(self, result: dict) -> Path:
        day = datetime.now().strftime("%Y-%m-%d")
        self.out.mkdir(parents=True, exist_ok=True)
        lines = [f"# JARVIS council decision, {day}", "",
                 "Nothing here has been built. These are recommendations; JARVIS only changes after you approve one.", ""]
        for i, w in enumerate(result["winners"], 1):
            author = next(a["name"] for a in AGENTS if a["key"] == w["author"])
            lines += [f"## {i}. {w['title']}  ({w['points']:.0f} votes, critique {w['avg_score']}/5, proposed by {author})",
                      f"- **Problem:** {w.get('problem', '')}", f"- **Change:** {w.get('change', '')}",
                      f"- **Security:** {w.get('security_risk', '')}", f"- **Test:** {w.get('test', '')}"]
            if w.get("answer"):
                lines.append(f"- **Answer to objections:** {w['answer']}")
            for o in w["objections"][:4]:
                lines.append(f"  - objection ({o['by']}, {o['score']}/5): {o['objection']}")
            lines.append("")
        if result["vetoed"]:
            lines += ["## Vetoed by the Sentinel", ""] + [f"- {v['id']} {v['title']}: {v['reason']}" for v in result["vetoed"]] + [""]
        lines += [f"Requests used today: {self.budget.used}/{self.budget.cap}."]
        report = self.out / f"{day}.md"
        report.write_text("\n".join(lines), encoding="utf-8")
        (self.out / f"{day}-transcript.md").write_text("\n\n".join(self.transcript), encoding="utf-8")
        return report


def main() -> int:
    key = groq_key()
    if not key:
        print("No GROQ_API_KEY in .env, so the council can't meet.")
        return 1
    LIVE.mkdir(parents=True, exist_ok=True)
    for a in AGENTS + [{"key": "chair"}]:
        (LIVE / f"{a['key']}.log").write_text("", encoding="utf-8")      # fresh terminals for today's meeting
    council = Council(BRIEF.read_text(encoding="utf-8"), key)
    result = council.run()
    report = council.write_report(result)
    council.say("chair", "DECISION: " + ("; ".join(f"{i}. {w['title']}" for i, w in enumerate(result["winners"], 1))
                                         or "no winner today"))
    council.say("chair", f"Report written to {report}. Nothing in JARVIS changes until you approve it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
