"""Edge control rules, routing of browser commands, honesty guard and hands-free noise filters."""
import asyncio

import pytest

from conftest import FakeLLM
from jarvis.conversation import Conversation, claims_action, route_all
from jarvis.engine import is_hallucination
from jarvis.tools import Reminders, ToolBox


class FakeEdge:
    def __init__(self, url="https://www.youtube.com/results?search_query=x", search_field=True):
        self.url, self.search_field = url, search_field
        self.log = []

    def running(self): return True
    def ensure_running(self): self.log.append(("ensure",))
    def pages(self): return [{"url": self.url}]
    def current(self):
        from urllib.parse import urlparse
        return {"title": "t", "url": self.url, "host": urlparse(self.url).hostname}
    def open_url(self, url, new_tab=False): self.url = url; self.log.append(("open", url))
    def type_text(self, text): self.log.append(("type", text)); return {"field": "input", "search": self.search_field}
    def press(self, key, shift=False): self.log.append(("press", key))
    def find_clickable(self, target, nth=0): return {"x": 1, "y": 1, "text": target.title()}
    def click(self, target, nth=0): self.log.append(("click", target, nth)); return target
    def scroll(self, d): self.log.append(("scroll", d))
    def media(self, a): self.log.append(("media", a)); return {"toggle": "paused"}.get(a, a if a != "pause" else "paused")
    def navigate(self, a): self.log.append(("nav", a))
    def read(self): return {"title": "Weather", "url": self.url, "text": "Sunny, 32 degrees."}


@pytest.fixture
def edge_tb(settings, tmp_path):
    settings.update({"browser": "edge"})
    edge = FakeEdge()
    return ToolBox(settings, Reminders(tmp_path / "r.json"), launcher=lambda a: None, browser=lambda u: None,
                   opener=lambda p: None, edge=edge), edge


def run(conv, text):
    async def go():
        return [s async for s in conv.reply(text)]
    return asyncio.run(go())


@pytest.mark.parametrize("text,expected", [
    ("open youtube and search for anime fights", [("web_search", "anime fights", "youtube")]),
    ("search for videos of code with harry on youtube", [("web_search", "code with harry", "youtube")]),
    ("search youtube for lofi", [("web_search", "lofi", "youtube")]),
    ("type lofi music and search", [("browser_type", "lofi music", True)]),
    ("type hello", [("browser_type", "hello", False)]),
    ("play the first video", [("browser_click", "first video", 0)]),
    ("play the second video", [("browser_click", "first video", 1)]),
    ("click on subscribe", [("browser_click", "subscribe", None)]),
    ("pause the video", [("browser_media", "pause")]),
    ("skip the ad", [("browser_media", "skip ad")]),
    ("scroll down", [("browser_scroll", "down")]),
    ("go back", [("browser_nav", "back")]),
    ("summarize this page", [("browser_read", True)]),
    ("let's start a new conversation, forget everything", [("reset_conversation",)]),
])
def test_browser_routing(settings, edge_tb, text, expected):
    tb, _ = edge_tb
    steps = route_all(text, settings, tb.find_app)
    assert len(steps) == len(expected), steps
    for (name, args), (exp_name, *exp_values) in zip(steps, expected):
        assert name == exp_name, steps
        values = list(args.values())
        assert all(v is None or v in values for v in exp_values), (steps, expected)


def test_open_the_browser_means_jarvis_edge(settings, edge_tb):
    tb, edge = edge_tb
    steps = route_all("I want you to open the browser and open WhatsApp", settings, tb.find_app)
    assert steps == [("open_website", {"url": "whatsapp", "browser": "edge"})]
    tb.call(*steps[0])
    assert edge.log[-1] == ("open", "https://web.whatsapp.com")


def test_search_box_typing_submits(settings, edge_tb):
    tb, edge = edge_tb
    r = tb.call("browser_type", {"text": "lofi", "submit": True})
    assert not r.needs_confirmation and edge.log[-1] == ("press", "enter")


def test_sending_on_whatsapp_needs_confirmation(settings, edge_tb):
    tb, edge = edge_tb
    edge.url, edge.search_field = "https://web.whatsapp.com/", False
    r = tb.call("browser_type", {"text": "hi mom", "submit": True})
    assert r.needs_confirmation and "WhatsApp" in r.confirm_prompt and ("press", "enter") not in edge.log
    r.run_confirmed()
    assert edge.log[-1] == ("press", "enter")
    assert tb.call("browser_key", {"key": "enter"}).needs_confirmation


def test_clicking_post_or_send_needs_confirmation(settings, edge_tb):
    tb, edge = edge_tb
    for target in ["Comment", "Send", "Post"]:
        r = tb.call("browser_click", {"target": target})
        assert r.needs_confirmation, target
    assert not tb.call("browser_click", {"target": "first video"}).needs_confirmation


def test_page_summary_via_llm(settings, edge_tb):
    tb, _ = edge_tb
    conv = Conversation(settings, FakeLLM(), tb)  # FakeLLM.complete returns a fixed summary
    out = run(conv, "summarize this page")
    assert out and "testing" in out[0].lower()


@pytest.mark.parametrize("sentence,lie", [
    ("You've opened the YouTube comments section, now what's the comment?", True),
    ("I'll type: EPIC FIGHT ALERT!", True),
    ("I've navigated to your WhatsApp chat history and messages, sir.", True),
    ("Searching on YouTube for lofi music.", True),
    ("Cats have scent glands on their faces.", False),
    ("Good evening. How can I help?", False),
])
def test_honesty_guard(sentence, lie):
    assert claims_action(sentence) == lie


def test_llm_lie_is_replaced(settings, edge_tb):
    tb, _ = edge_tb
    conv = Conversation(settings, FakeLLM(["You've opened the YouTube comments section. Nice."]), tb)
    out = run(conv, "tell me something")
    from jarvis.conversation import HONEST_CANTS
    assert out[0] in HONEST_CANTS and len(out) == 1


def test_reset_conversation(settings, edge_tb):
    tb, _ = edge_tb
    conv = Conversation(settings, FakeLLM(["Hi there."]), tb)
    run(conv, "hello")
    assert conv.history
    assert run(conv, "start a new conversation") == ["Okay, fresh start.", "What do you need?"]
    assert len(conv.history) == 1  # only the reply to the reset


def test_whisper_loop_is_dropped():
    assert is_hallucination("Okay, " * 40)
    assert not is_hallucination("Okay, open YouTube and search for lofi music.")


@pytest.mark.parametrize("q,a", [
    ("what is 17 times 23", "That's 391."),
    ("15 percent of 2400", "15 percent of 2,400 is 360."),
    ("what is two hundred and fifty plus seventy five", "That's 325."),
    ("what's 1250 divided by 5", "That's 250."),
    ("square root of 144", "The square root of 144 is 12."),
])
def test_exact_math(q, a):
    from jarvis.conversation import maybe_math
    assert maybe_math(q) == a


@pytest.mark.parametrize("q", ["how are you", "i have 2 cats", "tell me about world war 2", "what time is it"])
def test_math_ignores_chat(q):
    from jarvis.conversation import maybe_math
    assert maybe_math(q) is None


def test_role_label_leak_is_cleaned():
    from jarvis.conversation import speakable
    assert speakable("assistant For DSA, I'd pick Python.") == "For DSA, I'd pick Python."


def test_style_examples_precede_history(settings):
    conv = Conversation(settings, FakeLLM(), None)
    msgs = conv.messages()
    # Examples sit inside the system prompt, labelled as made up, never as fake chat turns the model could recall.
    assert msgs[0]["role"] == "system" and len(msgs) == 1 and "not part of this conversation" in msgs[0]["content"]
    assert "sir" not in msgs[0]["content"].split("Never call")[0]
