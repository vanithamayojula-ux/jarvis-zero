"""Regression tests from end-to-end QA of phone calls (noise hallucinations, LLM failure, barge-in memory)."""
import asyncio

from conftest import EnergyVAD, FakeLLM, FakeSTT, FakeTTS, tone
from jarvis.telephony.base import CallState
from jarvis.telephony.manager import TelephonyManager


def _mgr(settings, llm, stt):
    events = []
    return TelephonyManager(settings, llm, stt, FakeTTS(), EnergyVAD, events.append), events


async def _answered(mgr):
    call = mgr.simulate_incoming("Tester")
    for _ in range(40):
        await asyncio.sleep(0.05)
        rt = mgr.calls.get(call["id"])
        if rt and rt.call.state == CallState.ACTIVE and rt.call.transcript:
            return call["id"], rt
    raise AssertionError("call was not answered")


def test_line_noise_hallucination_is_not_answered(settings):
    async def go():
        mgr, _ = _mgr(settings, FakeLLM(["You're welcome!"]), FakeSTT(["Thank you.", "Vavi, " * 10]))
        await mgr.start()
        cid, rt = await _answered(mgr)
        mgr.simulate_audio(cid, tone(0.5))
        await asyncio.sleep(0.3)
        mgr.simulate_audio(cid, tone(0.5))
        await asyncio.sleep(0.3)
        roles = [t["role"] for t in rt.call.transcript]
        await mgr.stop()
        return roles

    assert asyncio.run(go()) == ["jarvis"]  # only the greeting


def test_llm_failure_on_call_speaks_an_apology(settings):
    class DownLLM(FakeLLM):
        async def stream(self, messages, tools=None):
            raise ConnectionError("Ollama is not running")
            yield  # pragma: no cover

    async def go():
        mgr, events = _mgr(settings, DownLLM(), FakeSTT())
        await mgr.start()
        cid, rt = await _answered(mgr)
        await mgr.caller_said(cid, "are you there?")
        await asyncio.sleep(0.3)
        said = [t["text"] for t in rt.call.transcript if t["role"] == "jarvis"]
        await mgr.stop()
        return said, events

    said, events = asyncio.run(go())
    assert "trouble" in said[-1]
    assert any(e.get("type") == "error" and "Call reply failed" in e["message"] for e in events)


def test_call_barge_in_remembers_only_what_the_caller_heard(settings):
    long_reply = " ".join(f"Sentence number {i}." for i in range(6))

    async def go():
        mgr, _ = _mgr(settings, FakeLLM([long_reply, "Okay."]), FakeSTT(["stop for a second"]))
        await mgr.start()
        cid, rt = await _answered(mgr)
        await asyncio.sleep(0.6)  # greeting done
        await mgr.caller_said(cid, "tell me a long story")
        await asyncio.sleep(0.15)          # ~2 of 6 sentences (0.1 s each) have started playing
        mgr.simulate_audio(cid, tone(0.4))  # caller talks over JARVIS
        await asyncio.sleep(0.3)
        hist = [m["content"] for m in rt.conversation.history if m["role"] == "assistant"]
        await mgr.stop()
        return hist

    hist = asyncio.run(go())
    cut = [h for h in hist if h.endswith("—")]
    assert cut, hist
    assert cut[0].startswith("Sentence number 0.") and "Sentence number 5." not in cut[0]


def test_inbound_call_during_emergency_lock_is_rejected_with_reason(settings):
    async def go():
        mgr, events = _mgr(settings, FakeLLM(), FakeSTT())
        await mgr.start()
        await mgr.emergency_stop()
        mgr.simulate_incoming("Tester")
        await asyncio.sleep(0.2)
        recent = mgr.status()["recent_calls"]
        await mgr.stop()
        return mgr.calls, recent

    calls, recent = asyncio.run(go())
    assert not calls
    assert recent[-1]["state"] == "ended" and "emergency stop" in recent[-1]["error"]
