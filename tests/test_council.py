"""The upgrade council: agents only ever receive the brief, the Sentinel's veto wins, the daily cap holds."""
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "council"))
import council as C  # noqa: E402


def fake_groq(sent):
    """Scripted agents: everyone proposes; the Sentinel vetoes P2 ('send logs to a server'); all vote for P2 anyway."""
    def handler(req):
        body = json.loads(req.content)
        sent.append(body)
        task, who = body["messages"][1]["content"], body["messages"][0]["content"].split(",")[0]
        if task.startswith("Round 1"):
            title = "Upload logs to a cloud dashboard" if "Architect" in who else f"Idea from {who[8:20]}"
            out = {"proposals": [{"title": title, "change": "x", "security_risk": "y", "test": "z"}]}
        elif task.startswith("Round 2"):
            out = {"reviews": [{"id": "P1", "score": 4, "objection": "fine"}, {"id": "P2", "score": 5, "objection": "bold"}],
                   "veto": [{"id": "P2", "reason": "sends personal logs off the laptop"}] if "Sentinel" in who else []}
        elif task.startswith("Round 3"):
            out = {"responses": []}
        else:
            out = {"ranking": ["P2", "P1", "P3"], "reason": "because"}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(out)}}]})
    return httpx.MockTransport(handler)


def make(tmp_path, sent, cap=60):
    budget = C.Budget(tmp_path / "usage.json", cap=cap)
    return C.Council("BRIEF-ONLY-TEXT", "gsk_test", transport=fake_groq(sent), budget=budget, out=tmp_path, pause=0)


def test_agents_only_ever_see_the_brief(tmp_path):
    sent = []
    make(tmp_path, sent).run()
    assert sent and all("BRIEF-ONLY-TEXT" in b["messages"][0]["content"] for b in sent)
    assert {b["model"] for b in sent} == {a["model"] for a in C.AGENTS}          # five agents, three different models


def test_sentinel_veto_beats_the_votes(tmp_path):
    sent = []
    result = make(tmp_path, sent).run()
    assert "P2" not in [w["id"] for w in result["winners"]]                      # most votes, but vetoed
    assert result["vetoed"][0]["id"] == "P2" and "personal logs" in result["vetoed"][0]["reason"]


def test_daily_cap(tmp_path):
    sent = []
    make(tmp_path, sent, cap=3).run()
    assert len(sent) == 3                                                         # stops asking at the cap


def test_report_says_nothing_was_built(tmp_path):
    sent = []
    c = make(tmp_path, sent)
    report = c.write_report(c.run()).read_text(encoding="utf-8")
    assert "Nothing here has been built" in report and "Vetoed by the Sentinel" in report
    assert (tmp_path / "live" / "sentinel.log").read_text(encoding="utf-8").count("VETO") >= 1
