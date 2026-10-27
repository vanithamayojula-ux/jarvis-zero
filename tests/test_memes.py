"""27 Oct: the assistant is Spidey now, and it reacts to every turn: a mood at once (spidey acts it out), then a meme
for the moment in a panel beside spidey. The meme model is a different Groq model from the chat one (its own free
limits); offline it falls back to a plain reaction picture for the mood. Nothing here touches the network."""
import asyncio
import io
import json

import httpx
import pytest
from PIL import Image

from conftest import FakeLLM
from jarvis import memes
from jarvis.board import STORE
from jarvis.cloud import CloudLLM
from jarvis.config import PERSONALITIES
from jarvis.conversation import Conversation, route_all, system_prompt
from jarvis.engine import find_wake_word
from jarvis.tools import Reminders, ToolBox


def _png(w=600, h=400):
    out = io.BytesIO()
    Image.new("RGB", (w, h), (90, 120, 200)).save(out, "PNG")
    return out.getvalue()


# ---------- the model's pick ----------
def test_parse_pick_cleans_and_fits_the_template():
    p = memes.parse_pick('Sure! {"t": "drake", "mood": "smug", "c": ["Coffee at 2am https://x.co", '
                         '"Tea #peace \\u202e", "extra"]} bye')
    assert p == {"t": "drake", "mood": "smug", "c": ["Coffee at 2am", "Tea"]}
    p = memes.parse_pick('{"t": "panik_kalm", "mood": "nervous", "c": ["Exam tomorrow"]}')
    assert p["mood"] == "scared" and p["c"] == ["Exam tomorrow", "", ""]        # unknown mood → the template's own
    assert memes.parse_pick('{"t": "rickroll", "c": []}') is None
    assert memes.parse_pick("no json here") is None
    assert len(memes.parse_pick('{"t": "leo_cheers", "c": ["' + "a" * 300 + '"]}')["c"][0]) <= memes.MAX_CAPTION


def test_prompt_lists_every_template_and_avoids_recent_ones():
    import random
    text = memes.prompt(["drake", "not_a_template"], random.Random(3))
    assert all(k in text for k in memes.TEMPLATES)
    assert text.endswith("Don't use these, they were just shown: drake.")
    assert "Don't use" not in memes.prompt()
    assert sum(moment in text for moment, _ in memes.EXAMPLES) == 2         # two random style examples, not all


def test_copied_examples_are_caught():
    assert memes.is_copy({"t": "roll_safe", "c": ["Can't be bored!", "If YouTube is always one word away"]})   # seen live
    assert memes.is_copy({"t": "drake", "c": ["coffee jitters at 2AM", "something new"]})
    assert not memes.is_copy({"t": "drake", "c": ["Cold brew", "Chai at the tapri"]})


@pytest.mark.parametrize("rate, mood, turn, shown", [
    ("always", "happy", 1, True), ("strong", "happy", 3, False), ("strong", "laughing", 1, True),
    ("sometimes", "happy", 1, False), ("sometimes", "happy", 2, False), ("sometimes", "happy", 3, True),
    ("sometimes", "sad", 1, True),
])
def test_how_often(rate, mood, turn, shown):
    assert memes.should_show(rate, mood, turn) is shown


@pytest.mark.parametrize("said, mood", [
    ("lol i fell asleep in class", "laughing"), ("I got the internship!! i did it", "proud"),
    ("i got selected for the internship!! No way, that's huge!", "proud"),     # seen live: was "surprised"
    ("my exam is tomorrow and i'm nervous", "scared"), ("ugh this is so annoying", "annoyed"),
    ("i'm so tired today", "tired"), ("open youtube", "happy"), ("why is the sky blue", "thinking"),
])
def test_guess_mood(said, mood):
    assert memes.guess_mood(said) == mood


def test_fallback_is_a_plain_reaction_picture():
    import random
    for mood in memes.MOODS:
        p = memes.fallback_pick(mood, random.Random(1))
        assert memes.TEMPLATES[p["t"]][1] == "tb" and not any(p["c"])


@pytest.mark.parametrize("layout", ["tb", "right2", "left3", "left4"])
def test_render_every_layout(layout):
    caps = ["A caption that is long enough to wrap onto two lines", "Short"] + ["Row"] * 2
    im = Image.open(io.BytesIO(memes.render(_png(1200, 900), layout, caps[:memes.SLOTS[layout]])))
    assert max(im.size) == memes.OUT_MAX and im.format == "PNG"


def test_template_addresses():
    assert memes.allowed_template_url("https://i.imgflip.com/30b1gx.jpg")
    for bad in ("http://i.imgflip.com/30b1gx.jpg", "https://i.imgflip.com.evil.com/a.jpg", "https://evil.com/a.jpg",
                "https://i.imgflip.com/../x.svg", "https://i.imgflip.com/a.svg"):
        assert not memes.allowed_template_url(bad)


def test_maker_downloads_once_and_only_pictures(tmp_path):
    asked = []

    def handler(req):
        asked.append(str(req.url))
        if req.url.host == "api.imgflip.com":
            return httpx.Response(200, json={"data": {"memes": [
                {"id": "181913649", "url": "https://i.imgflip.com/30b1gx.jpg"},
                {"id": "5496396", "url": "https://evil.example/x.jpg"},                 # not Imgflip's picture server
                {"id": "55311130", "url": "https://i.imgflip.com/wxica.jpg"}]}})
        if req.url.path == "/wxica.jpg":
            return httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"})
        return httpx.Response(200, content=_png(), headers={"content-type": "image/jpeg"})
    m = memes.MemeMaker(tmp_path, httpx.Client(transport=httpx.MockTransport(handler)))
    png = m.make({"t": "drake", "mood": "smug", "c": ["Coffee", "Tea"]})
    assert Image.open(io.BytesIO(png)).size[0] <= memes.OUT_MAX
    m.make({"t": "drake", "mood": "smug", "c": ["again", ""]})
    assert len(asked) == 2                                       # the list and the picture, once each
    with pytest.raises(LookupError):
        m.picture("leo_cheers")                                  # its address wasn't Imgflip's: never fetched
    with pytest.raises(ValueError):
        m.picture("this_is_fine")                                # not a picture: not kept
    assert not list(tmp_path.glob("55311130.*"))


# ---------- the side model never pauses the chat ----------
def test_meme_model_failures_never_pause_the_chat_model():
    sent = []

    def handler(req):
        body = json.loads(req.content)
        sent.append(body)
        if body["model"] == "openai/gpt-oss-20b":
            return httpx.Response(429, headers={"retry-after": "30"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    llm = CloudLLM("http://127.0.0.1:1", "llama3.2:3b", "10m", "k")
    llm.cloud = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    assert asyncio.run(llm.cloud_only(msgs, model="openai/gpt-oss-20b")) is None
    assert llm.breaker.ok()                                       # the chat model is untouched
    assert sent[0]["reasoning_effort"] == "low" and sent[0]["include_reasoning"] is False
    assert asyncio.run(llm.cloud_only(msgs)) == "ok" and sent[1]["model"] == llm.cloud_model


# ---------- Spidey ----------
def test_the_assistant_is_called_spidey(settings):
    assert all(p.startswith("You are Spidey") for p in PERSONALITIES.values())
    prompt = system_prompt(settings, "local")
    assert "You are Spidey" in prompt and "JARVIS" not in prompt and "oof" in prompt


@pytest.mark.parametrize("heard, woke", [("hey spidey open youtube", True), ("Spidy, what time is it", True),
                                         ("okay speedy play lofi", True), ("there's a spider in my room", False),
                                         ("something spicy for dinner", False)])
def test_spidey_wake_word(heard, woke):
    assert find_wake_word(heard, "spidey")[0] is woke


@pytest.mark.parametrize("text, on", [("stop the memes", False), ("no more memes", False), ("turn off memes", False),
                                      ("memes on", True), ("show memes again", True), ("turn the memes back on", True)])
def test_meme_switch_routes(settings, text, on):
    assert route_all(text, settings) == [("set_memes", {"on": on})]


# ---------- every turn gets a mood and a meme ----------
class FakeMaker:
    def __init__(self):
        self.made = []

    def make(self, pick):
        self.made.append(pick)
        return _png(60, 40)


class MemeLLM(FakeLLM):
    key = "k"

    def __init__(self, answer):
        super().__init__()
        self.answer, self.asked = answer, []

    async def cloud_only(self, messages, max_tokens=80, temperature=0.0, timeout=6.0, model=None):
        self.asked.append((messages, model))
        return self.answer


def _conv(settings, llm, channel="local", rate="always"):
    settings.update({"meme_rate": rate})
    tools = ToolBox(settings, Reminders(settings.path.parent / "r.json"))
    tools.memes = FakeMaker()
    conv = Conversation(settings, llm, tools, channel)
    events = []
    conv.notify = events.append
    return conv, events


def _turn(conv, text):
    async def go():
        said = [s async for s in conv.reply(text)]
        await asyncio.gather(*list(conv._learning))        # the meme is made in the background
        return said
    return asyncio.run(go())


def test_a_command_gets_a_mood_then_a_meme(settings):
    llm = MemeLLM('{"t": "roll_safe", "mood": "smug", "c": ["Can\'t forget water", "If reminders do the remembering"]}')
    conv, events = _conv(settings, llm)
    _turn(conv, "remind me in 10 minutes to drink water")
    kinds = [e["type"] for e in events]
    assert kinds == ["mood", "meme"]
    meme = events[-1]
    assert meme["template"] == "roll_safe" and meme["mood"] == "smug" and STORE.get(meme["id"]) is not None
    (messages, model), = llm.asked
    assert model == "openai/gpt-oss-20b" and "THEY SAID: remind me in 10 minutes" in messages[1]["content"]
    _turn(conv, "remind me in 20 minutes to stretch")
    assert "Don't use these, they were just shown: roll_safe." in llm.asked[-1][0][0]["content"]


def test_without_the_cloud_a_reaction_picture_for_the_mood(settings):
    conv, events = _conv(settings, MemeLLM("not json"))
    _turn(conv, "remind me in 10 minutes to drink water")
    assert events[-1]["type"] == "meme" and conv.tools.memes.made[-1]["c"] == ["", ""]


def test_no_memes_when_off_on_calls_or_typing(settings):
    settings.update({"memes": False})
    conv, events = _conv(settings, MemeLLM("{}"))
    _turn(conv, "remind me in 10 minutes to drink water")
    assert [e["type"] for e in events] == ["mood"] and not conv.llm.asked     # spidey still acts it out; no meme
    assert "no more memes" in " ".join(_turn(conv, "stop the memes")).lower() or settings.get("memes") is False
    settings.update({"memes": True})
    conv, events = _conv(settings, MemeLLM("{}"), channel="phone")
    _turn(conv, "hello there friend")
    assert not [e for e in events if e["type"] in ("mood", "meme")]
    conv, events = _conv(settings, MemeLLM("{}"))
    conv.typing = {"hwnd": None, "where": "Notepad", "at": 0, "n": 0}
    llm = conv.llm
    try:
        _turn(conv, "my secret diary entry")
    except Exception:
        pass
    assert not llm.asked and not [e for e in events if e["type"] in ("mood", "meme")]


def test_switch_memes_off_and_on(settings):
    conv, events = _conv(settings, MemeLLM("{}"))
    _turn(conv, "stop the memes")
    assert settings.get("memes") is False and {"type": "memes", "on": False} in events
    _turn(conv, "memes on")
    assert settings.get("memes") is True
