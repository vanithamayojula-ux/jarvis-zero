import pytest
from fastapi.testclient import TestClient

from conftest import EnergyVAD, FakeLLM, FakeSTT, FakeTTS
from jarvis import server
from jarvis.testing import ScriptedMic, SilentSpeaker


@pytest.fixture
def client(settings):
    ScriptedMic.script = []
    server.S.no_hotkey = True  # never grab a real system-wide hotkey in tests
    server.build(settings, mic_factory=ScriptedMic, speaker_factory=SilentSpeaker, llm=FakeLLM(), stt=FakeSTT(),
                 tts=FakeTTS(), vad_factory=EnergyVAD)
    with TestClient(server.app) as c:
        yield c
    server.S.engine = None


def test_status_and_settings(client):
    assert client.get("/api/status").json()["telephony"]["pstn_available"] is False
    r = client.patch("/api/settings", json={"changes": {"silence_ms": 600}})
    assert r.status_code == 200 and r.json()["silence_ms"] == 600
    assert client.patch("/api/settings", json={"changes": {"api_key": "x"}}).status_code == 400


def test_rejects_foreign_host_and_origin(client):
    assert client.get("/api/status", headers={"host": "evil.example:8600"}).status_code == 403
    assert client.post("/api/listen/start", headers={"origin": "https://evil.example"}).status_code == 403
    assert client.get("/api/status", headers={"origin": "http://localhost:5173"}).status_code == 200


def test_token_enforced_when_configured(client, monkeypatch):
    monkeypatch.setenv("JARVIS_API_TOKEN", "s3cret")
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/status", headers={"x-jarvis-token": "s3cret"}).status_code == 200


def test_text_turn_and_websocket(client):
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        client.post("/api/text", json={"text": "hi"})
        seen = []
        for _ in range(30):
            ev = ws.receive_json()
            if ev["type"] == "transcript":
                seen.append(ev["role"])
            if seen.count("assistant") >= 1:
                break
        assert seen[0] == "user" and "assistant" in seen


def test_listen_once_like_the_hotkey(client):
    r = client.post("/api/listen/once")
    assert r.status_code == 200 and r.json()["listening"] is True


def test_real_call_blocked_via_api(client):
    r = client.post("/api/telephony/call", json={"provider": "mock", "destination": "+919876543210", "consent": False})
    assert r.json()["allowed"] is False
    r = client.post("/api/telephony/call", json={"provider": "pstn", "destination": "+919876543210", "consent": True})
    assert r.json()["allowed"] is False


def test_emergency_stop(client):
    r = client.post("/api/emergency-stop").json()
    assert r["telephony_locked"] is True
    r = client.post("/api/telephony/call", json={"provider": "mock", "destination": "sim", "consent": True}).json()
    assert r["allowed"] is False
    client.post("/api/emergency-reset")
    assert client.post("/api/telephony/call", json={"provider": "mock", "destination": "sim", "consent": True}).json()["allowed"]


@pytest.mark.parametrize("changes", [
    {"silence_ms": "abc"}, {"silence_ms": -5}, {"mode": "banana"}, {"vad_threshold": 7}, {"speaking_rate": "fast"},
    {"wake_word": ""}, {"history_turns": 10**9}, {"input_device": 9999}, {"telephony": {"sip_port": "x"}},
    {"telephony": {"real_calls_enabled": True}}, {"telephony": {"sip_allowed_ips": "192.168.137.0/24"}},
    {"barge_in": "yes"}, {"stt_model": "../../evil"}, {"listen_hotkey": "space"},
])
def test_settings_rejects_values_that_break_the_engine(client, changes):
    before = client.get("/api/settings").json()
    r = client.patch("/api/settings", json={"changes": changes})
    assert r.status_code == 400 and r.json()["detail"]
    assert client.get("/api/settings").json() == before  # nothing was half-applied


def test_settings_accepts_dashboard_values(client):
    for ch in [{"mode": "hands_free"}, {"activation": "voice"}, {"wake_word": "friday"}, {"silence_ms": 900},
               {"speaking_rate": 1.25}, {"barge_in": False}, {"user_name": "Yash"}, {"input_device": None},
               {"telephony": {"sip_allowed_ips": ["192.168.137.0/24", "10.0.0.7"], "owner_ips": []}}]:
        assert client.patch("/api/settings", json={"changes": ch}).status_code == 200, ch


def test_history_survives_a_corrupt_log_line(client):
    from jarvis.config import DATA_DIR
    folder = DATA_DIR / "conversations"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "20000101-000000-test.jsonl").write_text(
        '{"t": "x", "channel": "local", "role": "user", "text": "hi there"}\n{"t": "x", "chan', encoding="utf-8")
    items = client.get("/api/conversations")
    assert items.status_code == 200
    assert any(i["id"] == "20000101-000000-test" and i["preview"] == "hi there" for i in items.json()["conversations"])
    r = client.get("/api/conversations/20000101-000000-test")
    assert r.status_code == 200 and [m["text"] for m in r.json()["messages"]] == ["hi there"]
    assert client.get("/api/conversations/does-not-exist").status_code == 404


def test_bad_input_is_rejected_clearly(client):
    assert client.post("/api/text", json={"text": "   "}).status_code == 400
    assert client.post("/api/text", json={"text": "x" * 5000}).status_code == 400
    assert client.post("/api/confirm/maybe").status_code == 404
    assert client.post("/api/ptt/sideways").status_code == 404
    for path in ["hangup", "handoff", "resume-ai"]:
        assert client.post(f"/api/telephony/{path}", json={"call_id": "gone"}).status_code == 404
    assert client.post("/api/telephony/simulate/say", json={"call_id": "gone", "text": "hi"}).status_code == 404


def test_websocket_rejects_foreign_origin(client):
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws", headers={"origin": "http://evil.example"}) as ws:
            ws.receive_json()
