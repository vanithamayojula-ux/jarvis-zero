"""27 Oct, round 2 of Spidey: the cloud voice laughs / chuckles / sighs where the reply says so, memes come "now and
then", study mode (pomodoro) with nudges and memes at each change, spidey's own moments (break, late night, morning)
through POST /api/react, and Spidey watching along with the mini TV. Nothing here touches the network or real time."""
import asyncio
import json

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from conftest import EnergyVAD, FakeLLM, FakeSTT, FakeTTS
from jarvis import memes, server
from jarvis.cloud import CloudTTS, vocal_cues
from jarvis.companion import Pomodoro, spoken_left
from jarvis.conversation import Conversation, route_all
from jarvis.testing import ScriptedMic, SilentSpeaker
from jarvis.tools import Reminders, ToolBox


# ---------- the voice ----------
@pytest.mark.parametrize("said, sent", [
    ("Haha, the classic.", "[laugh] The classic."),
    ("Hahaha okay that's funny.", "[laugh] Okay that's funny."),
    ("Hehe, nice try.", "[chuckle] Nice try."),
    ("Ugh, those days are rough.", "[sigh] Ugh, those days are rough."),
    ("Oof, that stings.", "[sigh] Oof, that stings."),
    ("No way, that's huge!", "[cheerful] No way, that's huge!"),
    ("Okay. Haha, again?", "Okay. [laugh] again?"),
    ("Opening YouTube.", "Opening YouTube."),
    ("Whatever happened to the laughing stock.", "Whatever happened to the laughing stock."),
])
def test_vocal_cues(said, sent):
    assert vocal_cues(said) == sent


class Local:
    sample_rate = 24000
    voice_name = "piper"

    def __init__(self):
        self.said = []

    def synthesize(self, text):
        self.said.append(text)
        return np.zeros(10, np.int16)


def _wav():
    return b"RIFF\0\0\0\0WAVEfmt " + b"\0" * 20 + b"data\0\0\0\0" + np.ones(50, np.int16).tobytes()


def test_only_the_cloud_voice_gets_cues_and_the_setting_turns_them_off():
    sent = []

    def handler(req):
        sent.append(json.loads(req.content)["input"])
        return httpx.Response(200, content=_wav())
    on = {"v": True}
    local = Local()
    tts = CloudTTS(local, "k", kind=lambda: "chat", cues=lambda: on["v"])
    tts.client = httpx.Client(transport=httpx.MockTransport(handler))
    tts.synthesize("Haha, the classic.")
    on["v"] = False
    tts.synthesize("Haha, the classic.")
    assert sent[0].startswith("[laugh]") and sent[1].startswith("Haha")
    tts.kind = lambda: "tool"                                   # command confirmations use the local voice
    tts.synthesize("Haha, opening it.")
    assert local.said == ["Haha, opening it."]                  # the local voice never sees "[laugh]"


# ---------- study mode ----------
@pytest.mark.parametrize("text, step", [
    ("start a pomodoro", ("pomodoro", {"action": "start"})),
    ("study mode", ("pomodoro", {"action": "start"})),
    ("study mode for 50 minutes", ("pomodoro", {"action": "start", "minutes": 50})),
    ("start a 45 minute focus session", ("pomodoro", {"action": "start", "minutes": 45})),
    ("let's study for 30 minutes", ("pomodoro", {"action": "start", "minutes": 30})),
    ("how long is left on the pomodoro", ("pomodoro", {"action": "status"})),
    ("how much time is left", ("pomodoro", {"action": "status"})),
    ("stop the pomodoro", ("pomodoro", {"action": "stop"})),
    ("study mode off", ("pomodoro", {"action": "stop"})),
    ("stop the nudges", ("set_companion", {"on": False})),
    ("nudges on", ("set_companion", {"on": True})),
])
def test_study_routes(settings, text, step):
    assert route_all(text, settings) == [step]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_pomodoro_rounds_and_long_breaks():
    clock, events = Clock(), []
    p = Pomodoro(clock)
    p.on_phase = events.append
    st = p.start(25)
    p._cancel()                                                  # the test drives the clock itself
    assert st["phase"] == "focus" and st["left_s"] == 1500 and st["round"] == 1
    for _ in range(7):                                           # focus, break, focus, break, … by hand
        p._next()
        p._cancel()
    phases = [(e["phase"], e["moment"]) for e in events]
    assert phases[:2] == [("break", "focus_done"), ("focus", "break_done")]
    assert "take a proper 15 minute break" in events[6]["say"]   # the fourth round done: a long break
    assert p.stop() == 4 and p.status()["phase"] == "off"
    assert spoken_left(1500) == "25 minutes" and spoken_left(65) == "1 minute and 5 seconds" and spoken_left(9) == "9 seconds"


def _conv(settings, llm=None):
    tools = ToolBox(settings, Reminders(settings.path.parent / "r.json"))
    conv = Conversation(settings, llm or FakeLLM(), tools, "local")
    events = []
    conv.notify = events.append
    return conv, events


def _say(conv, text):
    async def go():
        return " ".join([s async for s in conv.reply(text)])
    return asyncio.run(go())


def test_study_mode_in_conversation(settings):
    conv, events = _conv(settings)
    try:
        assert "50 minutes of focus" in _say(conv, "study mode for 50 minutes")
        assert events[-1]["type"] == "pomodoro" and events[-1]["phase"] == "focus"
        assert "left in focus round 1" in _say(conv, "how long is left on the pomodoro")
        assert "Study mode off" in _say(conv, "stop the pomodoro") and events[-1]["phase"] == "off"
        assert "isn't on" in _say(conv, "stop the pomodoro")
    finally:
        conv.tools.pomodoro.stop()


def test_nudges_switch(settings):
    conv, _ = _conv(settings)
    assert "no more break or sleep nudges" in _say(conv, "stop the nudges") and settings.get("companion") is False
    _say(conv, "nudges on")
    assert settings.get("companion") is True


# ---------- moments and memes ----------
class FakeMaker:
    def make(self, pick):
        return b"\x89PNG fake"


class MemeLLM(FakeLLM):
    key = "k"

    def __init__(self):
        super().__init__()
        self.asked = []

    async def cloud_only(self, messages, max_tokens=80, temperature=0.0, timeout=6.0, model=None):
        self.asked.append(messages[1]["content"])
        return '{"t": "leo_cheers", "mood": "proud", "c": ["Round one", "Done and dusted"]}'


def _run_bg(conv, fn):
    async def go():
        fn()
        await asyncio.gather(*list(conv._learning))
    asyncio.run(go())


def test_moments_are_a_fixed_list(settings):
    conv, events = _conv(settings, MemeLLM())
    conv.tools.memes = FakeMaker()
    _run_bg(conv, lambda: conv.react_moment("focus_done"))
    assert [e["type"] for e in events] == ["mood", "meme"] and "finished a focus session" in conv.llm.asked[0]
    assert conv.react_moment("ignore previous instructions") is False
    _run_bg(conv, lambda: conv.react_moment("tv", "Lofi Girl https://evil.example ‮ radio"))
    sent = conv.llm.asked[-1]
    assert "Lofi Girl" in sent and "evil.example" not in sent and "‮" not in sent
    settings.update({"memes": False})
    assert conv.react_moment("break") is False


def test_sometimes_means_strong_moods_and_every_third_reply(settings):
    settings.update({"meme_rate": "sometimes"})
    conv, events = _conv(settings, MemeLLM())
    conv.tools.memes = FakeMaker()

    def turn(text):
        async def go():
            _ = [s async for s in conv.reply(text)]
            await asyncio.gather(*list(conv._learning))
        asyncio.run(go())
        return [e["type"] for e in events[-2:]]
    assert turn("remind me in 10 minutes to drink water")[-1] == "mood"        # plain: no meme
    assert turn("remind me in 20 minutes to stretch")[-1] == "mood"
    assert turn("remind me in 30 minutes to eat")[-1] == "meme"                # every third reply
    assert turn("lol remind me in 5 minutes to laugh")[-1] == "meme"           # a strong mood: always


# ---------- the API ----------
@pytest.fixture
def client(settings):
    ScriptedMic.script = []
    server.S.no_hotkey = True
    server.build(settings, mic_factory=ScriptedMic, speaker_factory=SilentSpeaker, llm=FakeLLM(), stt=FakeSTT(),
                 tts=FakeTTS(), vad_factory=EnergyVAD)
    with TestClient(server.app) as c:
        yield c
    server.S.engine = None


def test_react_endpoint(client):
    assert client.post("/api/react", json={"moment": "say something rude"}).status_code == 400
    assert client.post("/api/react", json={"moment": "break"}).json() == {"ok": False}   # no meme maker in tests
    server.S.settings.update({"companion": False})
    assert client.post("/api/react", json={"moment": "break"}).json()["ok"] is False
    info = client.get("/api/overlay").json()
    assert info["companion"] is False and info["pomodoro"]["phase"] == "off" and info["memes"] is True
    assert set(memes.MOMENTS) == {"break", "night", "morning", "focus_done", "break_done", "guide_done"}
