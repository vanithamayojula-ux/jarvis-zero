"""Groq cloud layer: used when it works, local fallback when it doesn't. Fake network only (httpx.MockTransport)."""
import asyncio
import json

import httpx
import numpy as np

from jarvis.cloud import CloudLLM, CloudSTT, _openai_messages
from jarvis.interfaces import Transcript


def sse(*parts):
    lines = [f"data: {json.dumps({'choices': [{'delta': p}]})}" for p in parts] + ["data: [DONE]"]
    return "\n\n".join(lines).encode()


def make_llm(groq_handler, ollama_text="local answer"):
    llm = CloudLLM("http://ollama", "llama3.2:3b", -1, "gsk_test")
    llm.cloud = httpx.AsyncClient(transport=httpx.MockTransport(groq_handler))

    def ollama(req):
        return httpx.Response(200, content=(json.dumps({"message": {"content": ollama_text}, "done": True}) + "\n").encode())
    llm.client = httpx.AsyncClient(transport=httpx.MockTransport(ollama))
    return llm


def collect(llm, msgs=None, tools=None):
    async def go():
        return [c async for c in llm.stream(msgs or [{"role": "system", "content": "be nice"},
                                                     {"role": "user", "content": "hi"}], tools)]
    return asyncio.run(go())


def test_cloud_answer_is_used():
    seen = {}

    def groq(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, content=sse({"content": "Hello "}, {"content": "there!"}))
    llm = make_llm(groq)
    assert "".join(c.text for c in collect(llm)) == "Hello there!" and llm.last_source == "cloud"
    assert seen["model"] == "qwen/qwen3.8-27b" and seen["reasoning_effort"] == "none"
    assert "not sure" in seen["messages"][0]["content"]          # the honesty note rides on the system prompt


def test_rate_limit_falls_back_to_local_and_pauses_cloud():
    calls = []

    def groq(req):
        calls.append(1)
        return httpx.Response(429, headers={"retry-after": "42"}, json={"error": "limit"})
    llm = make_llm(groq)
    assert "".join(c.text for c in collect(llm)) == "local answer" and llm.last_source == "local"
    assert 30 <= llm.status()["cloud_paused_for_s"] <= 42
    collect(llm)
    assert len(calls) == 1                                       # paused: the next turn doesn't even try


def test_offline_falls_back_to_local():
    def groq(req):
        raise httpx.ConnectError("no internet")
    assert "".join(c.text for c in collect(make_llm(groq))) == "local answer"


def test_tool_calls_are_assembled_from_pieces():
    def groq(req):
        return httpx.Response(200, content=sse(
            {"tool_calls": [{"index": 0, "function": {"name": "open_app", "arguments": '{"na'}}]},
            {"tool_calls": [{"index": 0, "function": {"arguments": 'me": "notepad"}'}}]}))
    chunks = collect(make_llm(groq), tools=[{"type": "function", "function": {"name": "open_app"}}])
    assert chunks[-1].tool_calls == [{"name": "open_app", "arguments": {"name": "notepad"}}]


def test_history_converts_to_openai_tool_format():
    out = _openai_messages([
        {"role": "user", "content": "open notepad"},
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "open_app", "arguments": {"name": "notepad"}}}]},
        {"role": "tool", "content": "Opening Notepad."}])
    assert out[1]["tool_calls"][0] == {"id": "call_0", "type": "function",
                                       "function": {"name": "open_app", "arguments": '{"name": "notepad"}'}}
    assert out[2] == {"role": "tool", "tool_call_id": "call_0", "content": "Opening Notepad."}


class LocalSTT:
    def transcribe(self, pcm, hint=""):
        return Transcript("play run it up by humankind", 0.7, 1.0)


def test_cloud_stt_used_and_falls_back():
    stt = CloudSTT(LocalSTT(), "gsk_test")

    def ok(req):
        assert b"whisper-large-v3-turbo" in req.content and b"RIFF" in req.content      # a real WAV goes up
        return httpx.Response(200, json={"text": " Play Run It Up by Hanumankind.",
                                         "segments": [{"avg_logprob": -0.1, "no_speech_prob": 0.01}]})
    stt.client = httpx.Client(transport=httpx.MockTransport(ok))
    t = stt.transcribe(np.zeros(16000, np.int16))
    assert t.text == "Play Run It Up by Hanumankind." and t.confidence > 0.85 and stt.last_source == "cloud"

    def down(req):
        return httpx.Response(503)
    stt = CloudSTT(LocalSTT(), "gsk_test")
    stt.client = httpx.Client(transport=httpx.MockTransport(down))
    assert stt.transcribe(np.zeros(16000, np.int16)).text == "play run it up by humankind" and stt.last_source == "local"


def test_no_key_means_local_only():
    def groq(req):
        raise AssertionError("must not call the cloud without a key")
    llm = make_llm(groq)
    llm.key = ""
    assert "".join(c.text for c in collect(llm)) == "local answer"


def test_not_sure_offer_then_yes_really_searches(settings, tmp_path):
    from conftest import FakeLLM
    from test_conv_iterations import FakeEdge, say
    from jarvis.conversation import Conversation
    from jarvis.tools import Reminders, ToolBox
    settings.update({"browser": "edge", "save_conversations": False})
    edge = FakeEdge()
    tb = ToolBox(settings, Reminders(tmp_path / "r.json"), launcher=lambda a: None, browser=lambda u: None,
                 opener=lambda p: None, runner=lambda a: "ok", edge=edge)
    conv = Conversation(settings, FakeLLM(["I'm not sure who Hanumankind is. Want me to search Google for it?"]), tb)
    assert "not sure" in say(conv, "who is hanumankind?")
    assert "Searching" in say(conv, "yes")
    assert edge.log[-1] == ("open", "https://www.google.com/search?q=who+is+hanumankind")


# 25 Oct: typed chats sometimes took 15-20 s. A kept-alive connection Groq had closed raised RemoteProtocolError,
# the cloud was switched off for 30 s, and the local model had to load. Now one retry on a fresh connection.
def test_dropped_connection_is_retried_not_sent_to_local():
    calls = []

    def groq(req):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        return httpx.Response(200, content=sse({"content": "Hey!"}))
    llm = make_llm(groq)
    assert "".join(c.text for c in collect(llm)) == "Hey!" and llm.last_source == "cloud"
    assert len(calls) == 2 and llm.status()["cloud_paused_for_s"] == 0


def test_offline_pauses_the_cloud_only_briefly():
    def groq(req):
        raise httpx.ConnectError("no internet")
    llm = make_llm(groq)
    collect(llm)
    assert 0 < llm.status()["cloud_paused_for_s"] <= 8


def test_complete_and_background_helpers_retry_too():
    calls = []

    def groq(req):
        calls.append(1)
        if len(calls) % 2:
            raise httpx.RemoteProtocolError("Server disconnected")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    llm = make_llm(groq)
    msgs = [{"role": "system", "content": "x"}, {"role": "user", "content": "y"}]
    assert asyncio.run(llm.complete(msgs)) == "ok" and llm.last_source == "cloud"
    assert asyncio.run(llm.cloud_only(msgs)) == "ok"
    assert llm.status()["cloud_paused_for_s"] == 0


def test_background_helper_failure_never_pauses_chat():
    def groq(req):
        raise httpx.ReadTimeout("slow")
    llm = make_llm(groq)
    assert asyncio.run(llm.cloud_only([{"role": "system", "content": "x"}, {"role": "user", "content": "y"}])) is None
    assert llm.status()["cloud_paused_for_s"] == 0
