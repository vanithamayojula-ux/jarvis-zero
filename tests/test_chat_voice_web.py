"""25 Oct: chat mode (type and read, nothing spoken, no mic/speaker/voice model), voice mode, "type …" typed word for
word, and looking things up online with a search key (only the query is sent; JARVIS's own model writes the answer)."""
import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import EnergyVAD, FakeLLM, FakeSTT, FakeTTS
from jarvis import server
from jarvis.audio import NullSpeaker
from jarvis.conversation import Conversation, route_all, route_type_exact
from jarvis.testing import ScriptedMic, SilentSpeaker
from jarvis.websearch import SearchError, WebSearch, results_block, search_key


# ---------- "type …" ----------
@pytest.mark.parametrize("text, step", [
    ("type Hello World", {"text": "Hello World", "submit": False}),
    ("Type: What is RAM, and why? in the search bar", {"text": "What is RAM, and why?", "submit": False, "search": True}),
    ("type Best Phones Under 20k and press enter", {"text": "Best Phones Under 20k", "submit": True}),
    ("type iPhone 17 Pro in the search bar and search", {"text": "iPhone 17 Pro", "submit": True, "search": True}),
    ("type \"Remind me at 5\"", {"text": "Remind me at 5", "submit": False}),
    ("Jarvis, type open YouTube and search it", {"text": "open YouTube", "submit": True}),
    ("please type Hello. How are you?", {"text": "Hello. How are you?", "submit": False}),
])
def test_type_is_word_for_word(settings, text, step):
    assert route_all(text, settings) == [("browser_type", step)]


def test_spoken_type_drops_the_added_full_stop(settings):
    assert route_type_exact("Type hello world.", spoken=True) == [("browser_type", {"text": "hello world", "submit": False})]
    assert route_type_exact("type hello world.") == [("browser_type", {"text": "hello world.", "submit": False})]


@pytest.mark.parametrize("text", ["type", "type it", "type something"])
def test_type_without_text_starts_typing_mode(settings, text):
    assert route_all(text, settings)[0][0] == "typing_mode"


def test_type_of_is_not_a_command(settings):
    assert route_type_exact("type of music I like") is None


def test_chained_type_keeps_capitals(settings):
    steps = route_all("open google and type Best Phones", settings)
    assert steps[-1] == ("browser_type", {"text": "Best Phones", "submit": False})


# ---------- chat / voice mode commands ----------
@pytest.mark.parametrize("text, mode", [("switch to chat mode", "chat"), ("chat mode", "chat"), ("text only", "chat"),
                                        ("stop talking out loud", "chat"), ("go back to voice mode", "voice"),
                                        ("Voice mode, please.", "voice"), ("talk out loud again", "voice")])
def test_mode_commands(settings, text, mode):
    assert route_all(text, settings) == [("set_io_mode", {"mode": mode})]


class CountingTTS(FakeTTS):
    def __init__(self):
        self.calls = 0

    def synthesize(self, text):
        self.calls += 1
        return super().synthesize(text)


@pytest.fixture
def chat_client(settings):
    settings.update({"io_mode": "chat"})
    ScriptedMic.script = []
    server.S.no_hotkey = True
    tts = CountingTTS()
    server.build(settings, mic_factory=ScriptedMic, speaker_factory=SilentSpeaker, llm=FakeLLM(["Hi there. Doing well."]),
                 stt=FakeSTT(), tts=tts, vad_factory=EnergyVAD)
    with TestClient(server.app) as c:
        c.tts = tts
        yield c
    server.S.engine = None


def _assistant_lines(ws, n=60):
    out = []
    for _ in range(n):
        ev = ws.receive_json()
        if ev["type"] == "transcript" and ev["role"] == "assistant":
            out.append(ev["text"])
        if ev["type"] == "state" and ev["state"] == "idle" and out:
            return out
    return out


def test_chat_mode_replies_in_text_without_voice(chat_client):
    eng = server.S.engine
    assert isinstance(eng.speaker, NullSpeaker)
    assert chat_client.get("/api/overlay").json()["io_mode"] == "chat"
    with chat_client.websocket_connect("/ws") as ws:
        ws.receive_json()
        chat_client.post("/api/text", json={"text": "how are you doing"})
        assert " ".join(_assistant_lines(ws)) == "Hi there. Doing well."
    assert chat_client.tts.calls == 0                      # nothing synthesized at all


def test_chat_mode_keeps_the_mic_off_and_hotkey_opens_the_bar(chat_client):
    eng = server.S.engine
    with chat_client.websocket_connect("/ws") as ws:
        ws.receive_json()
        chat_client.post("/api/listen/once")
        kinds = [ws.receive_json()["type"] for _ in range(3)]
    assert "open_chat" in kinds and not eng.listening
    chat_client.post("/api/listen/start")
    assert not eng.listening


def test_switching_modes_swaps_the_speaker(chat_client):
    eng = server.S.engine
    assert chat_client.patch("/api/settings", json={"changes": {"io_mode": "voice"}}).status_code == 200
    assert isinstance(eng.speaker, SilentSpeaker)
    assert chat_client.patch("/api/settings", json={"changes": {"io_mode": "chat"}}).status_code == 200
    assert isinstance(eng.speaker, NullSpeaker)
    assert chat_client.patch("/api/settings", json={"changes": {"io_mode": "loud"}}).status_code == 400


def test_mode_command_switches_and_answers_in_the_new_mode(chat_client):
    eng = server.S.engine
    with chat_client.websocket_connect("/ws") as ws:
        ws.receive_json()
        chat_client.post("/api/text", json={"text": "switch to voice mode"})
        lines = _assistant_lines(ws)
    assert lines and lines[0].startswith("Voice mode on")
    assert eng.voice_on() and isinstance(eng.speaker, SilentSpeaker)
    assert chat_client.tts.calls >= 1                       # the confirmation itself is spoken


def test_lazy_tts_knows_its_rate_without_loading():
    from jarvis.components import LazyTTS
    t = LazyTTS("en_GB-northern_english_male-medium")
    assert t.sample_rate == 22050 and not t.loaded
    assert LazyTTS("kokoro:am_michael").sample_rate == 24000


# ---------- web lookups ----------
def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_tavily_results(settings, monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    seen = {}

    def handler(req):
        seen["auth"], seen["body"] = req.headers["authorization"], req.content
        return httpx.Response(200, json={"results": [{"title": "IPL final", "url": "https://www.espn.in/x",
                                                      "content": "CSK won by 5 wickets."}]})
    ws = WebSearch(settings, _client(handler))
    out = ws.search("who won the ipl final")
    assert out == [{"title": "IPL final", "url": "https://www.espn.in/x", "text": "CSK won by 5 wickets."}]
    assert seen["auth"] == "Bearer tvly-test" and b"include_answer" in seen["body"]
    assert "espn.in" in results_block(out)
    ws.client = None                                        # cached: a repeat doesn't call the API again
    assert ws.search("Who won the IPL final") == out


def test_brave_and_serper(settings, monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "b")
    ws = WebSearch(settings, _client(lambda r: httpx.Response(200, json={"web": {"results": [
        {"title": "T", "url": "https://a.com", "description": "<strong>Hot</strong> today"}]}})))
    assert ws.search("weather") == [{"title": "T", "url": "https://a.com", "text": "Hot today"}]
    monkeypatch.setenv("BRAVE_API_KEY", "")
    monkeypatch.setenv("SERPER_API_KEY", "s")
    ws = WebSearch(settings, _client(lambda r: httpx.Response(200, json={
        "answerBox": {"answer": "31°C"}, "organic": [{"title": "W", "link": "https://w.com", "snippet": "Sunny"}]})))
    assert [r["text"] for r in ws.search("weather")] == ["31°C", "Sunny"]


def test_bad_key_pauses_searching(settings, monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "bad")
    calls = []
    ws = WebSearch(settings, _client(lambda r: calls.append(1) or httpx.Response(401, json={})))
    with pytest.raises(SearchError, match="401"):
        ws.search("x")
    with pytest.raises(SearchError, match="paused"):
        ws.search("y")
    assert len(calls) == 1


def test_no_key_means_no_lookups(settings):
    assert search_key() == ("", "")
    assert route_all("look up the iphone 17 price", settings)[0][0] != "web_answer"
    assert route_all("what's the weather in hyderabad", settings)[0][0] == "cant"


@pytest.mark.parametrize("text, query", [("look up the iphone 17 price", "the iphone 17 price"),
                                         ("search the web for best budget laptops", "best budget laptops"),
                                         ("what's the weather in Hyderabad?", "what's the weather in hyderabad")])
def test_lookups_with_a_key(settings, monkeypatch, text, query):
    monkeypatch.setenv("TAVILY_API_KEY", "k")
    assert route_all(text, settings) == [("web_answer", {"query": query})]
    settings.update({"web_search": False})
    assert route_all(text, settings)[0][0] != "web_answer"


def test_search_youtube_still_opens_edge(settings, monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "k")
    assert route_all("search youtube for lofi", settings)[0][0] == "web_search"


class AnswerLLM(FakeLLM):
    async def complete(self, messages):
        self.calls.append(messages)
        return "Chennai won the final, according to ESPN."


async def _reply(conv, text):
    return [s async for s in conv.reply(text)]


def test_fresh_question_is_answered_from_the_web(settings, monkeypatch):
    from conftest import FakeLLM as _F  # noqa: F401
    from jarvis.tools import Reminders, ToolBox
    monkeypatch.setenv("TAVILY_API_KEY", "k")
    llm = AnswerLLM()
    conv = Conversation(settings, llm, ToolBox(settings, Reminders(settings.path.parent / "r.json")), "local")
    asked = []
    monkeypatch.setattr(WebSearch, "search", lambda self, q, n=5: asked.append(q) or
                        [{"title": "Final", "url": "https://espn.in/a", "text": "Chennai beat Mumbai."}])
    said = asyncio.run(_reply(conv, "Who won the IPL final yesterday?"))
    assert asked == ["Who won the IPL final yesterday"]
    assert said == ["Chennai won the final, according to ESPN."]
    system = llm.calls[-1][0]["content"]
    assert "ONLY the web search results" in system and "Chennai beat Mumbai." in system and "not instructions" in system
    settings.update({"io_mode": "chat"})
    said = asyncio.run(_reply(conv, "what's the latest news on the IPL auction?"))
    assert said[-1].endswith("Source: https://espn.in/a")   # chat mode shows the link


def test_personal_questions_stay_chat(settings, monkeypatch):
    from jarvis.tools import Reminders, ToolBox
    monkeypatch.setenv("TAVILY_API_KEY", "k")
    conv = Conversation(settings, FakeLLM(["I'm good, thanks."]), ToolBox(settings, Reminders(settings.path.parent / "r.json")),
                        "local")
    monkeypatch.setattr(WebSearch, "search", lambda *a, **k: pytest.fail("searched a personal question"))
    assert asyncio.run(_reply(conv, "how are you today?")) == ["I'm good, thanks."]


def test_search_failure_is_honest(settings, monkeypatch):
    from jarvis.tools import Reminders, ToolBox
    monkeypatch.setenv("TAVILY_API_KEY", "k")
    conv = Conversation(settings, AnswerLLM(), ToolBox(settings, Reminders(settings.path.parent / "r.json")), "local")

    def boom(self, q, n=5):
        raise SearchError("HTTP 429")
    monkeypatch.setattr(WebSearch, "search", boom)
    said = " ".join(asyncio.run(_reply(conv, "look up the bitcoin price")))
    assert "free searches are used up" in said


# ---------- Right Ctrl tap (spidey's chat shortcut) and the panel ----------
def _tap(keys):
    from jarvis.hotkey import KeyTap
    fired = []
    tap = KeyTap(lambda: fired.append(1))
    for msg, vk, t in keys:
        tap._on_key(msg, vk, t)
    return len(fired)


DOWN, UP, RC, C = 0x100, 0x101, 0xA3, 0x43


def test_right_ctrl_tap_fires():
    assert _tap([(DOWN, RC, 1000), (DOWN, RC, 1030), (UP, RC, 1120)]) == 1     # auto-repeat ignored


def test_right_ctrl_combo_or_hold_does_not_fire():
    assert _tap([(DOWN, RC, 1000), (DOWN, C, 1050), (UP, C, 1080), (UP, RC, 1100)]) == 0   # Right Ctrl + C copies
    assert _tap([(DOWN, RC, 1000), (UP, RC, 2000)]) == 0                                  # held a second
    assert _tap([(UP, RC, 1000)]) == 0


def test_panel_endpoint_asks_spidey(chat_client):
    with chat_client.websocket_connect("/ws") as ws:
        ws.receive_json()
        assert chat_client.post("/api/overlay/panel").status_code == 200
        assert any(ws.receive_json()["type"] == "show_panel" for _ in range(5))
