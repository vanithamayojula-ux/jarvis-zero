"""Memory across conversations: facts about you, the thread over a restart, learning from chat, forgetting."""
import asyncio

import pytest

from conftest import FakeLLM
from jarvis.conversation import Conversation, route_all
from jarvis.memory import Memory, parse_extraction
from jarvis.tools import Reminders, ToolBox

from test_conv_iterations import FakeEdge, say


@pytest.fixture
def mem(tmp_path):
    return Memory(tmp_path / "memory.json")


@pytest.fixture
def tb(settings, tmp_path):
    settings.update({"browser": "edge", "save_conversations": False})
    return ToolBox(settings, Reminders(tmp_path / "r.json"), launcher=lambda a: None, browser=lambda u: None,
                   opener=lambda p: None, runner=lambda a: "ok", edge=FakeEdge())


def test_remember_recall_forget(settings, tb, mem):
    conv = Conversation(settings, FakeLLM(), tb, memory=mem)
    assert say(conv, "remember that my exam is on Friday") == "Got it, I'll remember that."
    assert mem.facts() == ["Their exam is on Friday."]
    assert "Your exam is on Friday" in say(conv, "what do you remember about me")
    assert say(conv, "forget that my exam is on Friday") == "Okay, I've forgotten that."
    assert mem.facts() == []


def test_facts_are_in_front_of_every_reply(settings, tb, mem):
    mem.add("Their friend Rahul lives in Pune", "you")
    llm = FakeLLM(["Pune's lovely this time of year."])
    say(Conversation(settings, llm, tb, memory=mem), "should I visit my friend this weekend?")
    assert "Their friend Rahul lives in Pune." in llm.calls[0][0]["content"]


def test_thread_survives_a_restart(settings, tb, mem):
    say(Conversation(settings, FakeLLM(["Nice, what's it about?"]), tb, memory=mem), "I'm writing a story")
    again = Conversation(settings, FakeLLM(), tb, memory=Memory(mem.path))    # a new process, same file
    assert [m["content"] for m in again.history] == ["I'm writing a story", "Nice, what's it about?"]


def test_new_conversation_drops_the_thread_but_keeps_facts(settings, tb, mem):
    mem.add("They like Hanumankind", "you")
    conv = Conversation(settings, FakeLLM(["Cool."]), tb, memory=mem)
    say(conv, "hello there my friend")
    say(conv, "start a new conversation")
    carried = " ".join(m["content"] for m in Memory(mem.path).recent())
    assert "hello there" not in carried and mem.facts() == ["They like Hanumankind."]


def test_forget_everything_asks_first(settings, tb, mem):
    mem.add("They like Hanumankind", "you")
    conv = Conversation(settings, FakeLLM(), tb, memory=mem)
    assert "can't be undone" in say(conv, "forget everything about me")
    assert mem.facts()                                     # nothing gone yet
    assert "forgotten everything" in say(conv, "yes")
    assert mem.facts() == []


class LearningLLM(FakeLLM):
    def __init__(self, replies, extraction):
        super().__init__(replies)
        self.extraction, self.asked = extraction, []

    async def cloud_only(self, messages, max_tokens=80):
        self.asked.append(messages)
        return self.extraction


def test_learns_facts_from_chat_in_the_background(settings, tb, mem):
    llm = LearningLLM(["Ah, Rahul sounds like a good friend."], '{"add": ["Their best friend is Rahul"], "remove": []}')
    conv = Conversation(settings, llm, tb, memory=mem)

    async def go():
        out = " ".join([s async for s in conv.reply("my best friend Rahul is coming over tonight")])
        await asyncio.gather(*conv._learning)               # let the background note-taking finish
        return out
    asyncio.run(go())
    assert mem.facts() == ["Their best friend is Rahul."]
    assert "USER: my best friend Rahul" in llm.asked[0][1]["content"]


def test_commands_teach_nothing(settings, tb, mem):
    llm = LearningLLM([], '{"add": ["They opened YouTube"], "remove": []}')
    conv = Conversation(settings, llm, tb, memory=mem)
    say(conv, "open youtube and search for lofi beats please")
    assert llm.asked == [] and mem.facts() == []


def test_duplicates_merge_and_bad_json_is_ignored(mem):
    mem.add("Their friend Rahul lives in Pune", "you")
    mem.add("Their friend Rahul lives in Pune now", "learned")
    assert len(mem.facts()) == 1
    assert parse_extraction("not json at all") == ([], [])
    assert parse_extraction('{"add": ["a", "b", "c"], "remove": []}')[0] == ["a", "b"]   # at most two per turn


def test_phone_callers_never_see_your_memory(settings, tb, mem):
    mem.add("Their exam is on Friday", "you")
    conv = Conversation(settings, FakeLLM(), None, channel="phone", memory=mem)
    assert conv.memory is None and "exam" not in conv.messages()[0]["content"]


@pytest.mark.parametrize("text, step", [
    ("remember that rahul is my best friend", "remember"), ("can you remember that I hate mornings", "remember"),
    ("what do you know about me", "recall"), ("forget about my exam", "forget"),
    ("wipe your memory", "forget_all"), ("remember to buy milk at 5", None)])
def test_memory_phrases(settings, text, step):
    steps = route_all(text, settings)
    assert (steps[0][0] if steps else None) == step or (step is None and steps[0][0] != "remember")
