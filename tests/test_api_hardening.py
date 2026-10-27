"""API hardening: constant-time token check, no silent no-token mode, settings that widen JARVIS's powers wait for a yes
in JARVIS, the action log endpoint, and spoken error messages."""
import json
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from conftest import EnergyVAD, FakeLLM, FakeSTT, FakeTTS
from jarvis import server
from jarvis.gate import Action
from jarvis.testing import ScriptedMic, SilentSpeaker
from jarvis.tools import Reminders, ToolBox


@pytest.fixture
def client(settings):
    ScriptedMic.script = []
    server.S.no_hotkey = True
    server.build(settings, mic_factory=ScriptedMic, speaker_factory=SilentSpeaker, llm=FakeLLM(), stt=FakeSTT(),
                 tts=FakeTTS(), vad_factory=EnergyVAD)
    with TestClient(server.app) as c:
        yield c
    server.S.engine = None


GOOD = {"my ip": {"argv": ["ipconfig", "/all"], "confirm": False}}


def patch(client, changes):
    return client.patch("/api/settings", json={"changes": changes})


def wait_for(fn, seconds=3.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if fn():
            return True
        time.sleep(0.03)
    return False


# ---------- the token ----------
def test_token_is_compared_in_constant_time(client, monkeypatch):
    monkeypatch.setenv("JARVIS_API_TOKEN", "s3cret")
    seen = []
    real = server.hmac.compare_digest
    monkeypatch.setattr(server.hmac, "compare_digest", lambda a, b: seen.append((a, b)) or real(a, b))
    assert client.get("/api/status", headers={"x-jarvis-token": "s3cret"}).status_code == 200
    assert client.get("/api/status", headers={"x-jarvis-token": "s3cre"}).status_code == 401
    assert client.get("/api/status").status_code == 401
    assert seen and all(isinstance(a, bytes) and isinstance(b, bytes) for a, b in seen)


def test_empty_token_is_refused_unless_the_test_flag_is_on(client, settings, monkeypatch):
    monkeypatch.setenv("JARVIS_API_TOKEN", "")
    assert client.get("/api/status").status_code == 200                 # conftest turned the test flag on
    settings.update({"insecure_no_token": False})
    r = client.get("/api/status")
    assert r.status_code == 503 and "JARVIS_API_TOKEN" in r.json()["detail"]
    assert client.get("/api/actions").status_code == 503


def test_websocket_checks_the_token_too(client, monkeypatch):
    monkeypatch.setenv("JARVIS_API_TOKEN", "s3cret")
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws?token=wrong"):
            pass
    with client.websocket_connect("/ws?token=s3cret") as ws:
        assert ws.receive_json()["type"] == "hello"


def test_the_flag_cannot_be_flipped_from_the_api(client):
    r = patch(client, {"insecure_no_token": True})
    assert r.status_code == 400 and "can't be changed" in r.json()["detail"]


# ---------- allowed_commands ----------
@pytest.mark.parametrize("bad", [
    {"x": {"argv": [r"C:\evil\run.exe"], "confirm": True}},          # a path, not a bare system name
    {"x": {"argv": ["cmd", "/c", "calc"], "confirm": True}},
    {"x": {"argv": ["..\\ipconfig"], "confirm": True}},
    {"x": {"argv": "ipconfig", "confirm": True}},
    {"x": {"argv": [], "confirm": True}},
    {"x": {"argv": ["ipconfig", 5], "confirm": True}},
    {"x": {"argv": ["ipconfig"], "confirm": "yes"}},
    {"x": "ipconfig"},
    {f"c{i}": {"argv": ["ipconfig"], "confirm": True} for i in range(21)},
    ["ipconfig"],
])
def test_bad_allowed_commands_are_rejected_and_nothing_is_parked(client, bad):
    before = server.S.settings.get("allowed_commands")
    assert patch(client, {"allowed_commands": bad}).status_code == 400
    assert server.S.settings.get("allowed_commands") == before
    assert server.S.engine.conversation.pending is None


def test_allowed_commands_are_parked_not_applied(client):
    before = server.S.settings.get("allowed_commands")
    r = patch(client, {"allowed_commands": GOOD})
    assert r.status_code == 202
    body = r.json()
    assert body["pending"] == ["allowed_commands"] and "Say yes to Spidey to allow it" in body["say"]
    assert server.S.settings.get("allowed_commands") == before          # not applied
    p = server.S.engine.conversation.pending
    assert p and p.risk == "high" and "Say yes to Spidey to allow it" in p.confirm_prompt
    rows = server.S.audit.tail(5)
    assert rows[-1]["kind"] == "setting" and rows[-1]["label"] == "allowed_commands" and rows[-1]["outcome"] == "proposed"
    assert "ipconfig" not in json.dumps(rows)                           # what was asked for stays out of the log


def test_the_typed_yes_button_applies_a_parked_change_and_it_is_audited(client):
    patch(client, {"allowed_commands": GOOD})
    assert client.post("/api/confirm/yes").status_code == 200
    assert wait_for(lambda: "my ip" in server.S.settings.get("allowed_commands"))
    assert server.S.audit.tail(5)[-1]["outcome"] == "applied"


def test_a_loose_yes_or_a_no_does_not_apply_it(client):
    patch(client, {"allowed_commands": GOOD})
    assert client.post("/api/confirm/no").status_code == 200
    time.sleep(0.5)
    assert "my ip" not in server.S.settings.get("allowed_commands")
    patch(client, {"allowed_commands": GOOD})
    client.post("/api/text", json={"text": "okay sure"})
    time.sleep(0.5)
    assert "my ip" not in server.S.settings.get("allowed_commands")


# ---------- the other protected settings ----------
def test_protected_settings_wait_but_ordinary_ones_apply(client):
    r = patch(client, {"silence_ms": 640, "site_allowlist": ["example.com"], "approved_sites": ["example.org"]})
    assert r.status_code == 200
    body = r.json()
    assert body["silence_ms"] == 640 and body["pending"] == ["approved_sites", "site_allowlist"]
    assert server.S.settings.get("silence_ms") == 640
    assert server.S.settings.get("site_allowlist") == [] and "example.org" not in server.S.settings.get("approved_sites")


def test_turning_things_off_is_immediate_turning_them_on_asks(client):
    assert patch(client, {"desktop_control": False}).status_code == 200
    assert server.S.settings.get("desktop_control") is False
    r = patch(client, {"desktop_control": True})
    assert r.status_code == 202 and server.S.settings.get("desktop_control") is False
    client.post("/api/confirm/yes")
    assert wait_for(lambda: server.S.settings.get("desktop_control") is True)
    assert patch(client, {"edge_open_any_site": False}).status_code == 200
    assert patch(client, {"edge_open_any_site": True}).status_code == 202
    assert patch(client, {"screen_to_cloud": True}).status_code == 202
    assert server.S.settings.get("screen_to_cloud") is False                 # off by default, only ever by your say-so


def test_bad_domains_in_the_allowlists_are_rejected(client):
    assert patch(client, {"site_allowlist": ["not a domain!"]}).status_code == 400
    assert patch(client, {"approved_sites": "example.com"}).status_code == 400


# ---------- the action log endpoint ----------
def test_actions_endpoint_needs_the_token_and_returns_the_last_50(client, monkeypatch):
    for i in range(60):
        server.S.audit.record(Action("click", 1, "notepad.exe", "x", f"B{i}"), type("D", (), {"verdict": "allow"})(), "ok")
    r = client.get("/api/actions")
    assert r.status_code == 200 and len(r.json()["actions"]) == 50 and r.json()["actions"][-1]["label"] == "B59"
    monkeypatch.setenv("JARVIS_API_TOKEN", "s3cret")
    assert client.get("/api/actions").status_code == 401
    assert client.get("/api/actions", headers={"x-jarvis-token": "s3cret"}).status_code == 200


def test_what_did_you_do_comes_straight_from_the_log(client):
    from test_conv_iterations import say
    server.S.audit.record(Action("click", 1, "spotify.exe", "Spotify", "Play"), type("D", (), {"verdict": "allow"})(), "ok")
    out = say(server.S.engine.conversation, "what did you do")
    assert "clicked Play in spotify" in out
    assert "clicked Play in spotify" in say(server.S.engine.conversation, "show me the action log")
    assert "clicked Play in spotify" in say(server.S.engine.conversation, "what did you click today?")


# ---------- spoken errors ----------
@pytest.fixture
def box(settings, tmp_path):
    return ToolBox(settings, Reminders(tmp_path / "r.json"), launcher=lambda a: None, browser=lambda u: None,
                   opener=lambda p: None, runner=lambda a: "ok")


def raising(exc):
    def boom(args):
        raise exc
    return boom


def test_error_mapping_is_spoken_and_never_leaks_details(box):
    from _ctypes import COMError
    cases = [(COMError(-2147220991, "secret detail", (None,) * 5), "That app stopped responding or closed while I was working"),
             (PermissionError("C:\\secret\\path"), "Windows wouldn't let me do that, the app may be running as administrator"),
             (OSError(5, "Access is denied"), "Windows wouldn't let me do that, the app may be running as administrator"),
             (ValueError("my password is x"), "Sorry, that didn't work")]
    for exc, said in cases:
        box.tools["get_time"].handler = raising(exc)
        text = box.call("get_time", {}).text
        assert said in text and "secret" not in text and "password" not in text


def test_desktop_errors_are_spoken_as_they_are(box):
    from jarvis.desktop import DesktopError
    box.tools["get_time"].handler = raising(DesktopError("That spot is covered by another window."))
    assert box.call("get_time", {}).text == "That spot is covered by another window."


# ---------- logs hold lengths, not typed text ----------
def test_conversation_log_lines_hide_typed_text(settings, box, tmp_path, monkeypatch):
    from conftest import FakeLLM as LLM
    from jarvis import conversation as convmod
    from jarvis.conversation import Conversation, _safe_args
    assert json.loads(_safe_args({"text": "hunter2", "value": "abc", "target": "Play"})) == {
        "text": "<7 chars>", "value": "<3 chars>", "target": "Play"}
    monkeypatch.setattr(convmod, "DATA_DIR", tmp_path)
    settings.update({"save_conversations": True})
    from test_conv_iterations import say
    conv = Conversation(settings, LLM(), box)
    say(conv, "type hunter2 in the search bar")
    log = "".join(p.read_text(encoding="utf-8") for p in (tmp_path / "conversations").glob("*.jsonl"))
    tool_lines = [json.loads(x) for x in log.splitlines() if json.loads(x)["role"] == "tool"]
    assert tool_lines and all("hunter2" not in x["text"].split(") ->")[0] for x in tool_lines)
