"""Regression tests from end-to-end QA of the local voice loop (hold, hallucinations, interrupt memory, stalls)."""
import asyncio
import threading
import time

import numpy as np

from conftest import EnergyVAD, FakeLLM, FakeSTT, FakeTTS, tone
from jarvis.engine import Engine, is_hallucination
from jarvis.testing import ScriptedMic, SilentSpeaker


async def _collect(eng, q, seconds):
    events, end = [], time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            events.append(await asyncio.wait_for(q.get(), 0.05))
        except asyncio.TimeoutError:
            pass
    return events


def _engine(settings, stt, llm=None, mic=ScriptedMic):
    return Engine(settings, EnergyVAD(), stt, llm or FakeLLM(["Sounds good."]), FakeTTS(), None,
                  mic_factory=mic, speaker_factory=SilentSpeaker)


def _users(events):
    return [e["text"] for e in events if e["type"] == "transcript" and e["role"] == "user"]


def test_held_sentence_continuation_needs_no_wake_word(settings):
    """'Jarvis, so I was thinking and…' <pause> 'maybe a movie tonight' is ONE request; the second half has no
    wake word but must not be ignored (it used to be, and the first half was silently lost)."""
    settings.update({"mode": "hands_free", "activation": "wake_word", "silence_ms": 300, "hold_ms": 900})

    async def go():
        ScriptedMic.script = [("silence", 0.3), ("say", tone(0.5)), ("silence", 0.5), ("say", tone(0.5)), ("silence", 2)]
        eng = _engine(settings, FakeSTT(["Spidey, so I was thinking and", "maybe a movie tonight"]))
        await eng.start()
        q = eng.subscribe()
        eng.start_listening()
        ev = await _collect(eng, q, 3.2)
        await eng.shutdown()
        return ev

    ev = asyncio.run(go())
    assert not any(e["type"] == "ignored" for e in ev)
    assert _users(ev) == ["so I was thinking and maybe a movie tonight"]


def test_held_sentence_is_answered_when_the_tail_is_noise(settings):
    settings.update({"mode": "hands_free", "activation": "wake_word", "silence_ms": 300, "hold_ms": 900})

    async def go():
        ScriptedMic.script = [("silence", 0.3), ("say", tone(0.5)), ("silence", 0.5), ("say", tone(0.4)), ("silence", 2)]
        eng = _engine(settings, FakeSTT(["Spidey, tell me about the weather and", ""]))  # a cough → empty transcript
        await eng.start()
        q = eng.subscribe()
        eng.start_listening()
        ev = await _collect(eng, q, 3.2)
        await eng.shutdown()
        return ev, eng.state

    ev, state = asyncio.run(go())
    assert _users(ev) == ["tell me about the weather and"]
    assert state == "idle"


def test_repeated_sentence_hallucination_is_dropped():
    loop = "We have a lot of people who are going to have a certain amount of time. " * 8
    assert is_hallucination(loop)
    assert is_hallucination("Vavi, " * 12)
    assert not is_hallucination("Open YouTube and search for anime fights and click the first video so it plays "
                                "while I finish cooking dinner tonight.")
    assert not is_hallucination("Open new tab and open YouTube and search for songs and click the first video to play it.")


def test_beep_or_fixed_line_interrupt_keeps_previous_reply(settings):
    """Pressing the hotkey twice (the 2nd press interrupts the beep) must not rewrite the last reply as 'cut off'."""
    async def go():
        ScriptedMic.script = []
        eng = _engine(settings, FakeSTT(), FakeLLM(["First sentence. Second sentence."]))
        await eng.start()
        eng.submit_text("hello")
        await asyncio.sleep(0.8)  # reply fully played
        before = [dict(m) for m in eng.conversation.history]
        eng.listen_once()
        eng.listen_once()          # interrupts the first beep
        eng.speaker.play(np.zeros(16000, np.int16))
        eng.interrupt()            # e.g. barge-in over a reminder line
        after = eng.conversation.history
        await eng.shutdown()
        return before, after

    before, after = asyncio.run(go())
    assert before == after and after[-1]["content"] == "First sentence. Second sentence."


def test_interrupt_before_any_words_keeps_previous_reply(settings):
    class SlowLLM(FakeLLM):
        async def stream(self, messages, tools=None):
            if len(self.calls) == 0:
                async for c in super().stream(messages, tools):
                    yield c
                return
            self.calls.append(messages)
            await asyncio.sleep(5)
            yield

    async def go():
        ScriptedMic.script = []
        eng = _engine(settings, FakeSTT(), SlowLLM(["All done here."]))
        await eng.start()
        eng.submit_text("first")
        await asyncio.sleep(0.5)
        eng.submit_text("second")   # LLM still thinking
        await asyncio.sleep(0.1)
        eng.interrupt()
        hist = [m["content"] for m in eng.conversation.history if m["role"] == "assistant"]
        await eng.shutdown()
        return hist

    hist = asyncio.run(go())
    assert hist[0] == "All done here."


def test_stt_failure_does_not_leave_engine_thinking(settings):
    settings.update({"mode": "hands_free", "activation": "voice", "silence_ms": 300})

    class BrokenSTT:
        def transcribe(self, pcm, hint=""):
            raise RuntimeError("model file corrupt")

    async def go():
        ScriptedMic.script = [("silence", 0.2), ("say", tone(0.5)), ("silence", 1.0)]
        eng = _engine(settings, BrokenSTT())
        await eng.start()
        q = eng.subscribe()
        eng.start_listening()
        ev = await _collect(eng, q, 1.8)
        state = eng.state
        await eng.shutdown()
        return ev, state

    ev, state = asyncio.run(go())
    assert state == "idle"
    assert any(e["type"] == "error" and "Speech recognition failed" in e["message"] for e in ev)


class DyingMic(ScriptedMic):
    """Delivers the script, then stops delivering frames entirely (unplugged USB mic / stalled driver)."""

    def _run(self):
        from jarvis.audio import FRAME
        next_t = time.perf_counter()
        for kind, value in list(self.script):
            pcm = value if kind == "say" else np.zeros(int(16000 * value), np.int16)
            for i in range(0, len(pcm) - FRAME + 1, FRAME):
                if self._stop.is_set():
                    return
                self.on_frame(pcm[i:i + FRAME])
                next_t += FRAME / 16000
                time.sleep(max(0.0, next_t - time.perf_counter()))
        self._stop.wait()


def test_mic_dies_mid_sentence_is_not_stuck_listening(settings):
    settings.update({"mode": "hands_free", "activation": "voice", "silence_ms": 400})

    async def go():
        DyingMic.script = [("silence", 0.2), ("say", tone(1.0))]  # speech, then the stream just stops
        eng = _engine(settings, FakeSTT(["what is the time"]), mic=DyingMic)
        await eng.start()
        q = eng.subscribe()
        eng.start_listening()
        ev = await _collect(eng, q, 3.0)
        state = eng.state
        await eng.shutdown()
        return ev, state

    ev, state = asyncio.run(go())
    assert _users(ev) == ["what is the time"]
    assert state == "idle"


def test_hotkey_oneshot_ends_if_mic_dies(settings):
    settings.update({"mode": "push_to_talk", "silence_ms": 400})

    async def go():
        DyingMic.script = [("silence", 0.2), ("say", tone(0.3))]
        eng = _engine(settings, FakeSTT([""]), mic=DyingMic)
        await eng.start()
        q = eng.subscribe()
        eng.listen_once()
        ev = await _collect(eng, q, 2.6)
        state, oneshot = eng.state, eng._oneshot
        await eng.shutdown()
        return ev, state, oneshot

    ev, state, oneshot = asyncio.run(go())
    assert state == "idle" and oneshot is False
    assert {"type": "oneshot", "listening": False} in ev


def test_hotkey_while_transcribing_drops_the_stale_request(settings):
    """Hotkey pressed while the previous sentence is still in speech recognition: only the new request is answered."""
    settings.update({"mode": "hands_free", "activation": "voice", "silence_ms": 300})

    class SlowSTT(FakeSTT):
        def transcribe(self, pcm, hint=""):
            time.sleep(0.6)
            return super().transcribe(pcm, hint)

    async def go():
        ScriptedMic.script = [("silence", 0.2), ("say", tone(0.4)), ("silence", 0.9), ("say", tone(0.5)), ("silence", 2)]
        eng = _engine(settings, SlowSTT(["old request", "new request"]), FakeLLM(["Old.", "New."]))
        await eng.start()
        q = eng.subscribe()
        eng.start_listening()
        await asyncio.sleep(1.1)       # first utterance ended (~0.9 s) and is being transcribed
        eng.listen_once()
        ev = await _collect(eng, q, 3.0)
        await eng.shutdown()
        return ev

    ev = asyncio.run(go())
    assert _users(ev) == ["new request"]


def test_reminder_waits_for_the_reply_to_finish(settings):
    async def go():
        ScriptedMic.script = []
        eng = _engine(settings, FakeSTT(), FakeLLM(["One. Two. Three."]))
        await eng.start()
        q = eng.subscribe()
        eng.submit_text("count to three")
        await asyncio.sleep(0.05)
        eng.say_now("Reminder: stretch.")      # fires mid-reply
        ev = await _collect(eng, q, 1.5)
        state = eng.state
        await eng.shutdown()
        return ev, state

    ev, state = asyncio.run(go())
    said = [e["text"] for e in ev if e["type"] == "transcript" and e["role"] == "assistant"]
    assert said == ["One.", "Two.", "Three.", "Reminder: stretch."]
    assert state == "idle"


def test_hotkey_with_mic_off_turns_it_back_off_after_the_reply(settings):
    """Hands-free with the mic switched off: the hotkey opens it for ONE request, then it goes back off
    (it used to stay on, silently listening for the wake word)."""
    settings.update({"mode": "hands_free", "activation": "wake_word", "silence_ms": 300})

    async def go():
        ScriptedMic.script = [("silence", 0.3), ("say", tone(0.5)), ("silence", 3)]
        eng = _engine(settings, FakeSTT(["what's the weather"]), FakeLLM(["Sunny."]))
        await eng.start()
        q = eng.subscribe()
        eng.listen_once()
        assert eng.listening
        ev = await _collect(eng, q, 2.2)
        listening_after = eng.listening
        eng.start_listening()          # explicit "start listening" keeps it on
        eng.listen_once()
        await asyncio.sleep(0.2)
        still = eng.listening
        await eng.shutdown()
        return ev, listening_after, still

    ev, listening_after, still = asyncio.run(go())
    assert _users(ev) == ["what's the weather"]
    assert listening_after is False and {"type": "mic", "listening": False} in ev
    assert still is True


def test_hotkey_pauses_playing_video(settings):
    """Ctrl+Alt+J while a video plays in JARVIS's Edge: pause it so the mic can hear the user."""
    import asyncio as _asyncio

    from conftest import EnergyVAD, FakeLLM, FakeSTT, FakeTTS
    from jarvis.engine import Engine
    from jarvis.testing import ScriptedMic, SilentSpeaker

    class Edge:
        def __init__(self):
            self.actions = []

        def is_playing(self):
            return not self.actions

        def media(self, action):
            self.actions.append(action)
            return "paused"

    class Tools:
        edge = Edge()
        media_opened_at = 0.0

        def app_names(self):
            return []

    async def go():
        ScriptedMic.script = [("silence", 1.0)]
        tools = Tools()
        eng = Engine(settings, EnergyVAD(), FakeSTT(), FakeLLM(), FakeTTS(), None,
                     mic_factory=ScriptedMic, speaker_factory=SilentSpeaker)
        eng.tools = tools
        await eng.start()
        eng.listen_once()
        await _asyncio.sleep(0.5)
        await eng.shutdown()
        return tools.edge.actions

    assert asyncio_run(go()) == ["pause"]


def asyncio_run(coro):
    import asyncio as _asyncio
    return _asyncio.run(coro)
