"""Cloud command interpreter: messy speech → a plain command → the same rules and confirmations. Fake cloud."""
import pytest

from conftest import FakeLLM
from jarvis.conversation import Conversation, route_all
from jarvis.tools import Reminders, ToolBox, parse_when

from test_conv_iterations import FakeEdge, say


class InterpretingLLM(FakeLLM):
    def __init__(self, rewrites, replies=None):
        super().__init__(replies)
        self.rewrites, self.asked = dict(rewrites), []

    async def cloud_only(self, messages, max_tokens=80):
        self.asked.append(messages[-1]["content"])
        return self.rewrites.get(messages[-1]["content"])


@pytest.fixture
def tb(settings, tmp_path):
    settings.update({"browser": "edge", "save_conversations": False})
    edge = FakeEdge()
    box = ToolBox(settings, Reminders(tmp_path / "r.json"), launcher=lambda a: None, browser=lambda u: None,
                  opener=lambda p: None, runner=lambda a: "ok", edge=edge)
    box.edge_log = edge.log
    return box


def test_messy_whatsapp_request_is_understood_and_still_asks_before_sending(settings, tb):
    llm = InterpretingLLM({"yo open that whatsapp thing and tell rahul I'm late": "message rahul on whatsapp that I'm late"})
    conv = Conversation(settings, llm, tb)
    out = say(conv, "yo open that whatsapp thing and tell rahul I'm late")
    assert "Should I send it" in out and ("open", "https://web.whatsapp.com") in tb.edge_log
    assert ("press", "enter") not in tb.edge_log                       # nothing sent without a yes


def test_chat_stays_chat(settings, tb):
    llm = InterpretingLLM({"open your mind a little, tell me a story": "CHAT"}, replies=["Once upon a time."])
    conv = Conversation(settings, llm, tb)
    assert "Once upon a time" in say(conv, "open your mind a little, tell me a story")
    assert not tb.edge_log


def test_unclear_asks_back(settings, tb):
    llm = InterpretingLLM({"do the thing with the music": "UNCLEAR: Should I play or pause the music?"})
    assert say(Conversation(settings, llm, tb), "do the thing with the music") == "Should I play or pause the music?"


def test_rules_first_cloud_only_when_needed(settings, tb):
    llm = InterpretingLLM({})
    say(Conversation(settings, llm, tb), "open youtube")
    assert llm.asked == []                                             # the rules knew it: no cloud call


def test_nonsense_rewrite_falls_back_safely(settings, tb):
    llm = InterpretingLLM({"chuck the tunes on mate": "frobnicate the tunes"}, replies=["Not sure what you mean."])
    assert "Not sure" in say(Conversation(settings, llm, tb), "chuck the tunes on mate")
    assert not tb.edge_log


def test_without_cloud_nothing_changes(settings, tb):
    conv = Conversation(settings, FakeLLM(), tb)                       # no cloud_only: old behaviour
    assert "not sure what you want me to open" in say(conv, "yo open that whatsapp thing and tell rahul I'm late")


def test_put_on_music_on_spotify_is_not_typing(settings):
    steps = route_all("can you put on some run it up by hanumankind on spotify", settings)
    assert steps[0] == ("web_search", {"query": "run it up by hanumankind", "site": "spotify web", "media": True})


def test_filler_in_reminder_times():
    assert parse_when("in like half an hour") and parse_when("at around 5 pm") and parse_when("in about 10 minutes")
