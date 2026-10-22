"""Regression tests from the end-user testing session (real phrasings that used to fail)."""
import asyncio
from pathlib import Path

import pytest

from conftest import FakeLLM
from jarvis.conversation import Conversation, route_all, world_time
from jarvis.tools import Reminders, ToolBox


@pytest.fixture
def box(settings, tmp_path):
    ran, launched = [], []
    tb = ToolBox(settings, Reminders(tmp_path / "r.json"), launcher=launched.append, browser=lambda u: launched.append(u),
                 opener=lambda p: launched.append(p), runner=lambda a: ran.append(a) or "IPv4 Address. . : 192.168.1.7")
    tb.ran, tb.launched = ran, launched
    return tb


def say(conv, text):
    async def go():
        return " ".join([s async for s in conv.reply(text)])
    return asyncio.run(go())


@pytest.mark.parametrize("text,first_step", [
    ("Shut down the laptop.", "cant"),
    ("Turn the volume up.", "system_volume"),     # JARVIS can do this since 22 Oct
    ("Turn on bluetooth.", "cant"),
    ("What's the weather today?", "cant"),
    ("Close it.", "cant"),
    ("Start a timer for 5 minutes.", "create_reminder"),
    ("Lock my screen.", "run_command"),
    ("Remind me tomorrow to call mom.", "ask_reminder_time"),
    ("Open notepad, no, calculator.", "open_app"),
    ("Open VS Code. Actually no, open Cursor.", "open_app"),
    ("So, now, that's such for anime fights in the YouTube.", "web_search"),
    ("What's the time difference between India and London?", "say"),
])
def test_routes(settings, box, text, first_step):
    steps = route_all(text, settings, box.find_app)
    assert steps and steps[0][0] == first_step, steps


@pytest.mark.parametrize("text", [
    "How do I open a bank account in India?",
    "Okay, I think I'll open up to my friend about it.",
    "Summarize the French revolution in two lines.",
    "What model are you?",
])
def test_chat_is_not_a_command(settings, box, text):
    assert route_all(text, settings, box.find_app) is None


def test_correction_opens_the_last_one(settings, box):
    steps = route_all("Open notepad, no, calculator.", settings, box.find_app)
    assert steps == [("open_app", {"name": "calculator"})]


def test_shutdown_offer_locks_on_yes(settings, box):
    conv = Conversation(settings, FakeLLM(), box)
    assert "lock the screen" in say(conv, "shut down the laptop")
    say(conv, "yes")
    assert box.ran == [["rundll32.exe", "user32.dll,LockWorkStation"]]


def test_reminder_asks_for_time_then_sets_it(settings, box):
    conv = Conversation(settings, FakeLLM(), box)
    assert say(conv, "Remind me tomorrow to call mom.") == "Sure, what time tomorrow?"
    assert "tomorrow at 9 AM" in say(conv, "9 am")
    assert box.reminders.pending()[0]["text"] == "call mom"


def test_i_meant_reruns_last_search(settings, box):
    settings.update({"browser": "default"})
    conv = Conversation(settings, FakeLLM(), box)
    say(conv, "search for modi speeches on youtube")
    say(conv, "No, I meant Rahul Gandhi speeches.")
    assert box.launched[-1].endswith("search_query=Rahul+Gandhi+speeches")


def test_ip_address_is_spoken_naturally(settings, box):
    conv = Conversation(settings, FakeLLM(), box)
    assert say(conv, "What's my IP address?") == "Your IP address is 192.168.1.7."


def test_dsa_does_not_match_inside_words(settings, box, tmp_path):
    d = tmp_path / "docs"
    d.mkdir()
    (d / "VerdantWildsAtmosphere.ts").write_text("x")
    (d / "DSA notes.pdf").write_text("x")
    settings.update({"search_dirs": [str(d)]})
    hits, _ = box.find_paths("DSA")
    assert [h.name for h in hits] == ["DSA notes.pdf"]


def test_pdf_one_picks_the_pdf(settings, box, tmp_path):
    d = tmp_path / "desk"
    d.mkdir()
    (d / "GenCV.docx").write_text("x")
    (d / "GenCV.pdf").write_text("x")
    settings.update({"search_dirs": [str(d)]})
    conv = Conversation(settings, FakeLLM(), box)
    assert "Which one" in say(conv, "open my GenCV")
    say(conv, "the PDF one")
    assert str(box.launched[-1]).endswith("GenCV.pdf")


def test_world_time_is_exact():
    assert world_time("time difference between india and london").startswith("India is")
    assert "ahead of London" in world_time("time difference between india and london")
    assert world_time("what time is it in tokyo").startswith("It's")
    assert world_time("what time is it") is None
