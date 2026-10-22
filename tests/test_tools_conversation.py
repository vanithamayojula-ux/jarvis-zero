import asyncio
from datetime import datetime

import pytest

from conftest import FakeLLM
from jarvis.conversation import Conversation, SentenceChunker, looks_incomplete, route_intent, speakable
from jarvis.tools import Reminders, ToolBox, parse_when


@pytest.fixture
def toolbox(settings, tmp_path):
    launched, opened, ran = [], [], []
    tb = ToolBox(settings, Reminders(tmp_path / "rem.json"), launcher=launched.append,
                 browser=opened.append, runner=lambda argv: ran.append(argv) or "ok")
    tb.launched, tb.opened, tb.ran = launched, opened, ran
    return tb


def collect(agen):
    async def run():
        return [s async for s in agen]
    return asyncio.run(run())


# ---------- tools ----------
def test_only_approved_apps_open(toolbox):
    assert "Opening Notepad" in toolbox.call("open_app", {"name": "Notepad"}).text
    assert toolbox.launched == [["notepad.exe"]]
    r = toolbox.call("open_app", {"name": "regedit"})
    assert "approved apps" in r.text and len(toolbox.launched) == 1


def test_unapproved_site_requires_confirmation(settings, toolbox):
    settings.update({"browser": "default"})
    assert "Opening YouTube" in toolbox.call("open_website", {"url": "youtube.com"}).text
    r = toolbox.call("open_website", {"url": "example.org"})
    assert r.needs_confirmation and toolbox.opened == ["https://www.youtube.com"]
    r.run_confirmed()
    assert toolbox.opened[-1] == "https://example.org"
    assert "doesn't look like" in toolbox.call("open_website", {"url": "javascript:alert(1)"}).text


def test_commands_are_allowlisted_and_gated(toolbox):
    assert "not an allowlisted" in toolbox.call("run_command", {"name": "del /s c:"}).text
    assert toolbox.ran == []
    r = toolbox.call("run_command", {"name": "flush dns"})
    assert r.needs_confirmation and toolbox.ran == []
    r.run_confirmed()
    assert toolbox.ran == [["ipconfig", "/flushdns"]]
    toolbox.call("run_command", {"name": "ip address"})
    assert toolbox.ran[-1] == ["ipconfig"]


def test_parse_when():
    now = datetime(2026, 10, 21, 14, 0)
    assert parse_when("in 10 minutes", now) == datetime(2026, 10, 21, 14, 10)
    assert parse_when("in an hour", now) == datetime(2026, 10, 21, 15, 0)
    assert parse_when("at 5 pm", now) == datetime(2026, 10, 21, 17, 0)
    assert parse_when("at 9:30 am", now) == datetime(2026, 10, 22, 9, 30)
    assert parse_when("tomorrow at 8", now).day == 22
    assert parse_when("whenever", now) is None


def test_reminder_tool_and_firing(toolbox):
    fired = []
    toolbox.reminders.on_due = fired.append
    r = toolbox.call("create_reminder", {"text": "stretch", "when": "in 1 second"})
    assert "remind you" in r.text
    assert toolbox.reminders.pending()[0]["text"] == "stretch"


def test_search_stays_in_approved_dirs(settings, tmp_path, toolbox):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "Invoice_March.pdf").write_text("x")
    settings.update({"search_dirs": [str(tmp_path / "docs")]})
    assert "Invoice_March.pdf" in toolbox.call("search_files", {"query": "invoice"}).text


# ---------- router ----------
@pytest.mark.parametrize("text,expected", [
    ("What time is it?", "get_time"),
    ("Can you open notepad please", "open_app"),
    ("open youtube", "open_website"),
    ("remind me to call mom in 10 minutes", "create_reminder"),
    ("what are my reminders", "list_reminders"),
    ("check my ip address", "run_command"),
    ("find a file called invoice", "search_files"),
    ("summarize our conversation", "summarize_conversation"),
    ("tell me a joke", None),
    ("I want to start a business", None),
    ("open the door to the garden", "clarify_open"),
])
def test_route_intent(settings, text, expected):
    r = route_intent(text, settings)
    assert (r[0] if r else None) == expected


# ---------- conversation ----------
def test_chunker_splits_sentences_and_early_comma():
    c = SentenceChunker()
    out = []
    for word in "Well, that's an interesting idea, and honestly it could work. Next part here.".split(" "):
        out += c.feed(word + " ")  # tokens arrive one by one, as from the model
    out += c.flush()
    assert out[0] == "Well, that's an interesting idea,"
    assert out[1:] == ["and honestly it could work.", "Next part here."]
    # a lonely one-word tail is never split off ("…idea," + "honestly.")
    c2, out2 = SentenceChunker(), []
    for word in "Well, that's an interesting idea, honestly. Next.".split(" "):
        out2 += c2.feed(word + " ")
    assert out2[0] == "Well, that's an interesting idea, honestly."


def test_speakable_strips_markup():
    assert speakable("**Hi**... there `x`") == "Hi. there x"
    assert speakable("...") == ""


def test_incomplete_detection():
    assert looks_incomplete("so I was thinking and")
    assert not looks_incomplete("Open notepad.")


def test_reply_streams_and_remembers(settings, toolbox):
    conv = Conversation(settings, FakeLLM(["Hello there. Nice to meet you."]), toolbox)
    out = collect(conv.reply("hi"))
    assert out == ["Hello there.", "Nice to meet you."]
    assert conv.history[-1] == {"role": "assistant", "content": "Hello there. Nice to meet you."}


def test_tool_route_needs_no_llm(settings, toolbox):
    llm = FakeLLM()
    conv = Conversation(settings, llm, toolbox)
    out = collect(conv.reply("open notepad"))
    assert out == ["Opening Notepad."] and llm.calls == []


def test_confirmation_flow(settings, toolbox):
    conv = Conversation(settings, FakeLLM(["Done, it's flushed."]), toolbox)
    assert collect(conv.reply("flush dns please")) == ["Flush the DNS cache?"]
    assert toolbox.ran == []
    collect(conv.reply("yes"))
    assert toolbox.ran == [["ipconfig", "/flushdns"]]
    collect(conv.reply("flush dns"))
    assert collect(conv.reply("no")) == ["Okay, I won't."] and len(toolbox.ran) == 1


def test_interruption_keeps_only_heard_part(settings):
    conv = Conversation(settings, FakeLLM(["One. Two. Three."]), None)
    collect(conv.reply("count"))
    conv.mark_interrupted(["One."])
    assert conv.history[-1]["content"] == "One. —"


def test_phone_channel_cannot_use_tools(settings, toolbox):
    conv = Conversation(settings, FakeLLM(["I can't do that from a call."]), None, channel="phone")
    out = collect(conv.reply("open notepad"))
    assert toolbox.launched == [] and "can't" in out[0]
    assert "Never say you did any of these" in conv.messages()[0]["content"]


def test_prompt_is_transparent_about_ai(settings):
    conv = Conversation(settings, FakeLLM(), None)
    assert "You are an AI" in conv.messages()[0]["content"]


# ---------- desktop apps + files ----------
@pytest.fixture
def desktop(settings, tmp_path):
    d = tmp_path / "Desktop"
    d.mkdir()
    for name in ["Antigravity.lnk", "Blender 5.2.lnk", "ShutDown.lnk", "Roblox Studio.lnk"]:
        (d / name).write_text("")
    (d / "GenCV.docx").write_text("x")
    (d / "DSA").mkdir()
    (d / "project 01").mkdir()
    (d / "project notes.txt").write_text("x")
    (d / "RUN_VISIBLE_BROWSER.bat").write_text("echo hi")
    settings.update({"shortcut_dirs": [str(d)], "search_dirs": [str(d)]})
    return d


@pytest.fixture
def tb(settings, tmp_path, desktop):
    opened, launched = [], []
    box = ToolBox(settings, Reminders(tmp_path / "r.json"), launcher=launched.append, browser=lambda u: None,
                  runner=lambda a: "", opener=opened.append)
    box.opened, box.launched = opened, launched
    return box


def test_desktop_shortcuts_are_apps(tb, desktop):
    assert "Opening Blender" in tb.call("open_app", {"name": "blender"}).text
    assert tb.opened == [str(desktop / "Blender 5.2.lnk")]
    tb.call("open_app", {"name": "Roblox Studio"})
    assert tb.opened[-1].endswith("Roblox Studio.lnk")
    r = tb.call("open_app", {"name": "shutdown"})
    assert "isn't one of your approved apps" in r.text and len(tb.opened) == 2


def test_open_file_confirms_first(tb, desktop):
    r = tb.call("open_file", {"query": "GenCV"})
    assert r.needs_confirmation and "GenCV" in r.confirm_prompt and tb.opened == []
    assert r.run_confirmed().text == "Opening GenCV."
    assert tb.opened == [str(desktop / "GenCV.docx")]


def test_open_folder(tb, desktop):
    r = tb.call("open_file", {"query": "DSA folder"})
    assert "folder" in r.confirm_prompt
    r.run_confirmed()
    assert tb.opened == [str(desktop / "DSA")]


def test_never_runs_scripts(tb):
    r = tb.call("open_file", {"query": "run visible browser"})
    assert not r.needs_confirmation and "won't run it" in r.text and tb.opened == []
    assert "RUN VISIBLE BROWSER" in r.text  # spoken naturally, no underscores


def test_several_matches_ask_which(settings, tb, desktop):
    conv = Conversation(settings, FakeLLM(), tb)
    out = collect(conv.reply("open project"))
    assert "Which one?" in out[-1] and tb.opened == []
    collect(conv.reply("the second one"))
    assert len(tb.opened) == 1 and tb.opened[0].startswith(str(desktop / "project"))
    collect(conv.reply("open project"))
    assert collect(conv.reply("none of them")) == ["Okay, never mind."] and len(tb.opened) == 1


@pytest.mark.parametrize("text,expected", [
    ("open antigravity", ("open_app", "antigravity")),
    ("launch blender", ("open_app", "blender")),
    ("open my GenCV", ("open_file", "gencv")),
    ("open the DSA folder", ("open_file", "dsa folder")),
    ("show me my resume", ("open_file", "resume")),
    ("open youtube", ("open_website", "youtube")),
    ("start a business plan", None),
])
def test_router_apps_files(settings, tb, text, expected):
    r = route_intent(text, settings, tb.find_app)
    assert (r and (r[0], list(r[1].values())[0])) == (expected or None) or (expected is None and r is None)


def test_file_type_words_filter(tb, desktop):
    (desktop / "DSA.pdf").write_text("x")
    r = tb.call("open_file", {"query": "DSA folder"})
    assert not r.choices and "folder" in r.confirm_prompt
    r = tb.call("open_file", {"query": "DSA pdf"})
    assert not r.choices and "PDF" in r.confirm_prompt


def test_office_temp_files_hidden(tb, desktop):
    (desktop / "~$GenCV.docx").write_text("lock")
    r = tb.call("open_file", {"query": "GenCV"})
    assert not r.choices  # only the real GenCV.docx


def test_yes_to_which_one_asks_again(settings, tb):
    conv = Conversation(settings, FakeLLM(), tb)
    collect(conv.reply("open project"))
    assert collect(conv.reply("yes")) == ["Which one? Say first or second."]
    collect(conv.reply("first"))
    assert len(tb.opened) == 1


# ---------- "be reasonable": real misheard commands from the voice log ----------
@pytest.mark.parametrize("heard", [
    "So, okay. So, can you open WhatsApp?",
    "No, just to open the watch up in the chrome itself.",
    "So, open up of WhatsApp in a Chrome.",
    "Yeah, cool. Can you open and open up Chroma and open WatchUp web in the.",
])
def test_whatsapp_in_chrome(settings, tb, heard):
    from jarvis.conversation import route_all
    steps = route_all(heard, settings, tb.find_app)
    assert steps and steps[-1][0] == "open_website" and steps[-1][1]["url"] == "whatsapp"
    assert len(steps) == 1, "chrome + whatsapp should merge into one step"


def test_long_garbled_open_asks_instead_of_guessing(settings, tb):
    from jarvis.conversation import route_all
    steps = route_all("So, feel that you have open the chrome bed. I can't be able to receive the WhatsApp", settings, tb.find_app)
    assert steps and steps[0][0] == "clarify_open"


def test_site_opens_in_requested_browser(settings, tb):
    tb.call("open_website", {"url": "whatsapp", "browser": "chrome"})
    assert tb.launched[-1][-1] == "https://web.whatsapp.com" and "chrome" in " ".join(tb.launched[-1]).lower()


def test_chain_two_apps(settings, tb):
    from jarvis.conversation import route_all
    assert [s[0] for s in route_all("open notepad and then open calculator", settings, tb.find_app)] == ["open_app", "open_app"]
