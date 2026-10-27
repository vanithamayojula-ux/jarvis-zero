"""The clean Edge profile, continuous scrolling, filling fields by label, and the multi-step form agent. Fakes only."""
import asyncio
import json

import pytest

from conftest import FakeLLM
from jarvis.browser import BrowserError, EdgeController
from jarvis.conversation import Conversation, route_all
from jarvis.tools import Reminders, ToolBox

from test_conv_iterations import FakeEdge, say

FORM = [{"id": 1, "kind": "field", "tag": "input", "type": "text", "label": "Full name", "value": ""},
        {"id": 2, "kind": "field", "tag": "input", "type": "email", "label": "Email address", "value": "", "required": True},
        {"id": 3, "kind": "field", "tag": "select", "type": "", "label": "Country", "options": ["Choose", "India", "Nepal"]},
        {"id": 4, "kind": "field", "tag": "input", "type": "checkbox", "label": "I agree to the terms", "checked": False},
        {"id": 5, "kind": "field", "tag": "input", "type": "password", "label": "Password", "value": ""},
        {"id": 6, "kind": "button", "tag": "button", "type": "submit", "label": "Sign up"}]


class FormEdge(FakeEdge):
    def __init__(self):
        super().__init__("https://example.com/signup")
        self.scrolling, self.acts = False, []

    def snapshot(self):
        return {"title": "Sign up", "url": self.url, "text": "Create your account", "elements": FORM}

    def act(self, el_id, action, value=None):
        self.acts.append((el_id, action, value))
        el = next(e for e in FORM if e["id"] == el_id)
        if action == "select":
            return {"ok": value in el.get("options", []), "label": value}
        if action == "fill" and el.get("type") == "password":         # what the real EdgeController does
            return {"ok": False, "sensitive": True, "label": el["label"]}
        return {"ok": True, "label": el["label"], "password": el.get("type") == "password"}

    def banner(self, text):
        self.banner_text = text

    def auto_scroll(self, on, speed="normal"):
        self.scrolling, self.scroll_speed = on, speed
        return on

    def auto_scrolling(self):
        return self.scrolling


@pytest.fixture
def tb(settings, tmp_path):
    settings.update({"browser": "edge", "save_conversations": False})
    box = ToolBox(settings, Reminders(tmp_path / "r.json"), launcher=lambda a: None, browser=lambda u: None,
                  opener=lambda p: None, runner=lambda a: "ok", edge=FormEdge())
    return box


# ---------- continuous scrolling ----------
def test_keep_scrolling_until_stop(settings, tb):
    conv = Conversation(settings, FakeLLM(), tb)
    assert "Say stop" in say(conv, "keep on scrolling")
    assert tb.edge.scrolling
    assert say(conv, "faster") == "Faster." and tb.edge.scroll_speed == "fast"
    assert say(conv, "stop") == "Stopped scrolling." and not tb.edge.scrolling   # bare "stop" stops the scroll


def test_stop_scrolling_phrases(settings):
    for t in ["stop scrolling", "that's enough", "stop there"]:
        assert route_all(t, settings) == [("browser_scroll", {"direction": "stop"})], t


# ---------- one field at a time ----------
@pytest.mark.parametrize("text, step", [
    ("type Yash in the name field", ("browser_fill", {"field": "name", "value": "Yash"})),   # capitals kept
    ("fill the email field with yash@example.com", ("browser_fill", {"field": "email", "value": "yash@example.com"})),
    ("choose India from the country dropdown", ("browser_fill", {"field": "country", "value": "india"})),
    ("tick the terms checkbox", ("browser_check", {"label": "terms", "on": True})),
])
def test_field_phrases(settings, text, step):
    assert route_all(text, settings) == [step]


@pytest.mark.parametrize("text, first", [("type hello in notepad", "browser_type"), ("type lofi in the search box", "browser_type"),
                                         ("set the volume to 40", "system_volume"), ("check my messages", "cant")])
def test_field_rules_leave_other_commands_alone(settings, text, first):
    assert route_all(text, settings)[0][0] == first


def test_fill_by_label(tb):
    assert tb.call("browser_fill", {"field": "email", "value": "a@b.c"}).text == "Typed “a@b.c” into Email address."
    assert tb.edge.acts[-1] == (2, "fill", "a@b.c")
    assert tb.call("browser_fill", {"field": "country", "value": "India"}).text == "Picked India for Country."
    assert "Please type that one yourself" in tb.call("browser_fill", {"field": "password", "value": "hunter2"}).text
    assert "can't see" in tb.call("browser_fill", {"field": "favourite colour", "value": "red"}).text


# ---------- the multi-step agent ----------
class PlanningLLM(FakeLLM):
    def __init__(self, plans):
        super().__init__()
        self.plans, self.prompts = list(plans), []

    async def cloud_only(self, messages, max_tokens=80):
        self.prompts.append(messages[0]["content"])
        return json.dumps(self.plans.pop(0)) if self.plans else None


def test_form_agent_fills_then_asks_before_signing_up(settings, tb, tmp_path):
    from jarvis.memory import Memory
    mem = Memory(tmp_path / "m.json")
    mem.add("Their email is yash@example.com", "you")          # memory must NOT reach websites
    llm = PlanningLLM([{"actions": [{"do": "fill", "id": 1, "value": "Yash"}, {"do": "fill", "id": 2, "value": "yash@example.com"},
                                    {"do": "select", "id": 3, "value": "India"}, {"do": "check", "id": 4, "value": True},
                                    {"do": "click", "id": 6}], "status": "continue"}])
    conv = Conversation(settings, llm, tb, memory=mem)
    out = say(conv, "fill this form with my name Yash, I live in India")
    assert "Should I press “Sign up”?" in out and (6, "click", None) not in tb.edge.acts      # not pressed yet
    assert "yash@example.com" not in llm.prompts[0] and "fill this form with my name Yash" in llm.prompts[0]
    say(conv, "yes")
    assert tb.edge.acts[-1] == (6, "click", None)


def test_form_agent_asks_for_missing_details_and_carries_on(settings, tb):
    llm = PlanningLLM([{"actions": [{"do": "fill", "id": 1, "value": "Yash"}], "status": "ask",
                        "say": "What email should I use?"},
                       {"actions": [{"do": "fill", "id": 2, "value": "y@x.in"}], "status": "done", "say": "All filled in."}])
    conv = Conversation(settings, llm, tb)
    assert say(conv, "fill this form with my name Yash") == "What email should I use?"
    assert say(conv, "use y@x.in") == "All filled in."
    assert (2, "fill", "y@x.in") in tb.edge.acts and "More details from them: use y@x.in" in llm.prompts[-1]
    assert "fill Full name = Yash" in llm.prompts[-1]                                         # remembers what it did


def test_form_agent_never_shows_passwords_in_its_notes(settings, tb):
    llm = PlanningLLM([{"actions": [{"do": "fill", "id": 5, "value": "hunter2"}], "status": "done", "say": "Done."}])
    conv = Conversation(settings, llm, tb)
    say(conv, "on this page, log in with password hunter2")
    assert not any("hunter2" in m.get("content", "") for m in conv.history if m["role"] == "assistant")


def test_without_cloud_the_agent_says_so(settings, tb):
    assert "without the cloud brain" in say(Conversation(settings, FakeLLM(), tb), "fill this form")


# ---------- the clean profile ----------
def test_profile_is_locked_out_of_accounts(tmp_path):
    edge = EdgeController(tmp_path / "edge-agent")
    edge._lock_out_accounts()
    prefs = json.loads((tmp_path / "edge-agent" / "Default" / "Preferences").read_text(encoding="utf-8"))
    assert prefs["signin"]["allowed"] is False and prefs["sync"]["requested"] is False
    assert edge.signed_in_account() == ""
    (tmp_path / "edge-agent" / "Local State").write_text(json.dumps(
        {"profile": {"info_cache": {"Default": {"user_name": "someone@outlook.com"}}}}), encoding="utf-8")
    closed = []
    edge.close_browser = lambda: closed.append(True)
    with pytest.raises(BrowserError, match="signed itself into someone@outlook.com"):
        edge._check_signed_out()
    assert closed and edge.account_alert == "someone@outlook.com"
