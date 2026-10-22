"""Volume & brightness, the 'hey jarvis' wake word, and the daily briefing. Fakes only: nothing real changes."""
import asyncio
import time
from datetime import datetime, timedelta

import numpy as np
import pytest

from conftest import FakeLLM, FakeSTT, tone
from jarvis.conversation import Conversation, route_all
from jarvis.memory import Memory
from jarvis.testing import ScriptedMic
from jarvis.tools import Reminders, ToolBox

from test_av_engine import _collect, _engine, _users
from test_conv_iterations import FakeEdge, say


class FakeSystem:
    def __init__(self, volume=50, brightness=40):
        self.vol, self.mute, self.bright = volume, False, brightness

    def volume(self): return self.vol
    def muted(self): return self.mute
    def set_mute(self, on): self.mute = on

    def set_volume(self, pct):
        self.vol = int(max(0, min(100, pct)))
        return self.vol

    def brightness(self): return self.bright

    def set_brightness(self, pct):
        self.bright = int(max(0, min(100, pct)))
        return self.bright


@pytest.fixture
def tb(settings, tmp_path):
    settings.update({"browser": "edge", "save_conversations": False})
    box = ToolBox(settings, Reminders(tmp_path / "r.json"), launcher=lambda a: None, browser=lambda u: None,
                  opener=lambda p: None, runner=lambda a: "ok", edge=FakeEdge(), system=FakeSystem())
    return box


@pytest.mark.parametrize("text, step", [
    ("turn the volume up", ("system_volume", {"action": "up", "value": 10})),
    ("turn it up a lot", ("system_volume", {"action": "up", "value": 25})),
    ("make it louder", ("system_volume", {"action": "up", "value": 10})),
    ("volume down", ("system_volume", {"action": "down", "value": 10})),
    ("set the volume to 40", ("system_volume", {"action": "set", "value": 40})),
    ("volume 30 percent", ("system_volume", {"action": "set", "value": 30})),
    ("full volume", ("system_volume", {"action": "set", "value": 100})),
    ("mute the laptop", ("system_volume", {"action": "mute"})),
    ("unmute the laptop", ("system_volume", {"action": "unmute"})),
    ("what's the volume", ("system_volume", {"action": "get"})),
    ("set brightness to 70", ("system_brightness", {"action": "set", "value": 70})),
    ("dim the screen", ("system_brightness", {"action": "down", "value": 15})),
    ("make the screen brighter", ("system_brightness", {"action": "up", "value": 15})),
    ("brightness down a lot", ("system_brightness", {"action": "down", "value": 30})),
    ("what's the brightness", ("system_brightness", {"action": "get"})),
])
def test_volume_and_brightness_phrases(settings, text, step):
    assert route_all(text, settings) == [step]


@pytest.mark.parametrize("text", ["mute", "pause the video", "that sounds good", "read it out loud",
                                  "what's on this screen", "open spotify"])
def test_other_things_are_not_volume(settings, text):
    steps = route_all(text, settings) or [("chat", {})]
    assert steps[0][0] not in ("system_volume", "system_brightness")


def test_volume_tool(tb):
    assert tb.call("system_volume", {"action": "up", "value": 10}).text == "Volume's at 60 percent."
    assert tb.call("system_volume", {"action": "set", "value": 150}).text == "Volume's at 100 percent."
    assert tb.call("system_volume", {"action": "mute"}).text == "Muted the laptop."
    assert "but it is muted" in tb.call("system_volume", {"action": "get"}).text
    assert tb.call("system_volume", {"action": "set", "value": 30}).text == "Volume's at 30 percent."
    assert tb.call("system_brightness", {"action": "down", "value": 15}).text == "Brightness is at 25 percent."


def test_chained_with_other_commands(settings):
    steps = route_all("open spotify and turn the volume down", settings)
    assert steps[-1] == ("system_volume", {"action": "down", "value": 10})


# ---------- briefing ----------
@pytest.mark.parametrize("text", ["good morning", "Good morning, Jarvis!", "brief me", "what's my plan for today",
                                  "what do I have today", "morning briefing"])
def test_briefing_phrases(settings, text):
    assert route_all(text, settings) == [("briefing", {})]


def test_briefing_uses_only_real_facts(settings, tb, tmp_path):
    tb.reminders.add("submit the DBMS assignment", datetime.now() + timedelta(minutes=30))
    mem = Memory(tmp_path / "m.json")
    mem.add("Their exam is on Friday", "you")
    llm = FakeLLM()

    async def complete(messages):
        llm.prompt = messages[0]["content"]
        return "Morning! You've got the DBMS assignment due soon, and your exam's on Friday, so maybe revise tonight."
    llm.complete = complete
    conv = Conversation(settings, llm, tb, memory=mem)
    out = say(conv, "good morning")
    assert "DBMS assignment" in out and "submit the DBMS assignment" in llm.prompt and "exam is on Friday" in llm.prompt
    assert "Never invent" in llm.prompt and conv.reply_kind == "chat"


def test_briefing_falls_back_if_the_model_drops_a_reminder(settings, tb):
    tb.reminders.add("call mom", datetime.now() + timedelta(minutes=20))
    llm = FakeLLM()

    async def complete(messages):
        return "Morning! Nothing much on today, enjoy it."             # forgot the reminder
    llm.complete = complete
    out = say(Conversation(settings, llm, tb), "good morning")
    assert "call mom" in out and "1 reminder" in out


# ---------- wake word ----------
class FakeWake:
    threshold = 0.5

    def __init__(self, fire_after_s):
        self.fire_after, self.heard = fire_after_s, 0

    def feed(self, frame):
        self.heard += len(frame)
        return 0.95 if self.fire_after is not None and abs(self.heard / 16000 - self.fire_after) < 0.04 else 0.0

    def reset(self):
        pass


def run_wake(settings, stt_text, fire_after):
    settings.update({"mode": "hands_free", "activation": "wake_word", "wake_word": "jarvis", "silence_ms": 300,
                     "followup_s": 0})

    async def go():
        ScriptedMic.script = [("silence", 0.3), ("say", tone(0.8)), ("silence", 1.5)]
        eng = _engine(settings, FakeSTT([stt_text]))
        eng.wake_model = FakeWake(fire_after)
        await eng.start()
        q = eng.subscribe()
        eng.start_listening()
        ev = await _collect(eng, q, 3.0)
        await eng.shutdown()
        return ev
    return asyncio.run(go())


def test_hey_jarvis_heard_in_the_audio_wakes_it(settings):
    ev = run_wake(settings, "Hey Jarvis, open YouTube", fire_after=0.6)
    assert _users(ev) == ["open YouTube"] and any(e["type"] == "wake" for e in ev)


def test_sound_alike_in_the_transcript_no_longer_wakes_it(settings):
    ev = run_wake(settings, "Travis, open YouTube", fire_after=None)    # the old fuzzy match would have accepted it
    assert _users(ev) == [] and any(e["type"] == "ignored" for e in ev)


def test_exact_jarvis_still_works_as_backup(settings):
    ev = run_wake(settings, "Jarvis, what time is it", fire_after=None)
    assert _users(ev) == ["what time is it"]
