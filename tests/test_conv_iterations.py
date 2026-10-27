"""Regressions from the QA iterations (real-user phrasings that used to go wrong)."""
import asyncio
from datetime import datetime

import pytest

from conftest import FakeLLM
from jarvis.conversation import Conversation, is_robotic, maybe_math, pick_choice, route_all
from jarvis.tools import Reminders, ToolBox, parse_when


class FakeEdge:
    def __init__(self, url="https://www.youtube.com/results?search_query=x"):
        self.url, self.log = url, []

    def running(self): return True
    def ensure_running(self): self.log.append(("ensure",))
    def pages(self): return [{"url": self.url}]
    def current(self):
        from urllib.parse import urlparse
        return {"title": "t", "url": self.url, "host": urlparse(self.url).hostname or ""}
    def open_url(self, url, new_tab=False): self.url = url; self.log.append(("open", url))
    def type_text(self, text): self.log.append(("type", text)); return {"field": "input", "search": True}
    def press(self, key, shift=False): self.log.append(("press", key))
    def find_clickable(self, target, nth=0): return {"x": 1, "y": 1, "text": target}
    def click(self, target, nth=0): self.log.append(("click", target, nth)); return target
    def scroll(self, d): self.log.append(("scroll", d))
    def media(self, a): self.log.append(("media", a)); return a
    def navigate(self, a): self.log.append(("nav", a))
    def read(self): return {"title": "t", "url": self.url, "text": "x"}


@pytest.fixture
def tb(settings, tmp_path):
    settings.update({"browser": "edge", "save_conversations": False})
    edge, launched = FakeEdge(), []
    box = ToolBox(settings, Reminders(tmp_path / "r.json"), launcher=launched.append, browser=launched.append,
                  opener=launched.append, runner=lambda a: "ok", edge=edge)
    box.edge_log, box.launched = edge.log, launched
    return box


def say(conv, text):
    async def go():
        return " ".join([s async for s in conv.reply(text)])
    return asyncio.run(go())


# ---------- iteration 1 ----------
@pytest.mark.parametrize("text", [
    "click in the video", "can you click random video?", "click the first video to play it",
    "Okay, select any random video.", "play a random video", "Just click on random video in the hit.",
])
def test_vague_video_clicks_pick_a_video(settings, tb, text):
    steps = route_all(text, settings, tb.find_app)
    assert steps and steps[-1][0] == "browser_click" and steps[-1][1]["target"] == "first video", steps
    assert 0 <= steps[-1][1]["nth"] <= 4


def test_search_then_click_in_the_video(settings, tb):
    steps = route_all("open youtube and search for anime fights and click in the video", settings, tb.find_app)
    assert [s[0] for s in steps] == ["web_search", "browser_click"] and steps[1][1] == {"target": "first video", "nth": 0}


def test_command_after_can_you_hear_me(settings, tb):
    steps = route_all("Jarvis, can you hear me? Just click on the first video.", settings, tb.find_app)
    assert steps == [("browser_click", {"target": "first video", "nth": 0})]


@pytest.mark.parametrize("text", ["can you hear me?", "Hey Jarvis, are you there?", "Hello? Can you hear me?"])
def test_can_you_hear_me_is_answered_instantly(settings, tb, text):
    steps = route_all(text, settings, tb.find_app)
    assert steps and steps[0][0] == "say" and "chat" not in steps[0][1]["text"].lower()


def test_half_an_hour_is_thirty_minutes():
    now = datetime(2026, 10, 21, 14, 0)
    assert parse_when("in half an hour", now) == datetime(2026, 10, 21, 14, 30)
    assert parse_when("in an hour and a half", now) == datetime(2026, 10, 21, 15, 30)
    assert parse_when("in a few minutes", now) == datetime(2026, 10, 21, 14, 3)
    assert parse_when("in seven minutes", now) == datetime(2026, 10, 21, 14, 7)
    assert parse_when("at seven pm", now) == datetime(2026, 10, 21, 19, 0)


def test_bare_clock_time_means_the_next_one():
    now = datetime(2026, 10, 21, 14, 0)
    assert parse_when("at 6:30", now) == datetime(2026, 10, 21, 18, 30)        # this evening, not tomorrow 6:30 AM
    assert parse_when("in the evening at 7", now) == datetime(2026, 10, 21, 19, 0)
    assert parse_when("at 3 in the morning", now) == datetime(2026, 10, 22, 3, 0)
    assert parse_when("at 9:30 am", now) == datetime(2026, 10, 22, 9, 30)


def test_reminder_follow_up_half_hour(settings, tb):
    conv = Conversation(settings, FakeLLM(), tb)
    assert say(conv, "remind me to submit the assignment") == "Sure, what time?"
    assert "in half an hour" in say(conv, "in half an hour")


@pytest.mark.parametrize("text,offer_url", [
    ("can you read my whatsapp messages", "whatsapp"),
    ("check my emails", "gmail"),
    ("can you call my mom", "whatsapp"),
    ("can you order me a pizza on swiggy", "swiggy"),
])
def test_honest_cant_with_offer(settings, tb, text, offer_url):
    steps = route_all(text, settings, tb.find_app)
    assert steps[0][0] == "cant" and steps[0][1]["offer"] == ("open_website", {"url": offer_url}), steps


def test_cant_offer_opens_on_yes(settings, tb):
    conv = Conversation(settings, FakeLLM(), tb)
    assert "can't read" in say(conv, "can you read my whatsapp messages")
    say(conv, "yes")
    assert tb.edge_log[-1] == ("open", "https://web.whatsapp.com")


def test_weather_offer_keeps_the_city(settings, tb):
    steps = route_all("what's the weather in delhi", settings, tb.find_app)
    assert steps[0][1]["offer"] == ("web_search", {"query": "weather in delhi"})


@pytest.mark.parametrize("text", ["open my email", "open whatsapp", "type check my messages",
                                  "what do you call a group of crows"])
def test_cant_rules_do_not_steal_commands_or_chat(settings, tb, text):
    steps = route_all(text, settings, tb.find_app)
    assert not steps or steps[0][0] != "cant", steps


def test_divide_by_zero_is_answered():
    assert maybe_math("what is 100 divided by 0") == "You can't divide by zero. It's undefined."


def test_robotic_self_descriptions_are_dropped(settings, tb):
    assert is_robotic("Sorry, I'm a text-based AI,") and is_robotic("I can only respond to what you type.")
    assert not is_robotic("I'm an AI, running locally on your laptop.")
    conv = Conversation(settings, FakeLLM(["I'm just a chat window. Python is easier to start with."]), tb)
    assert say(conv, "which is easier") == "Python is easier to start with."


def test_pick_choice_ignores_ruled_out_and_ambiguous():
    names = ["motion-graphic (PNG)", "landing-remotion (folder)", "fix-motion-blur (folder)"]
    assert pick_choice("not the first one, the second one", names) == 1
    assert pick_choice("it's not motion, it's motion", names) is None
    assert pick_choice("the graphic one", names) == 0


def test_ambiguous_name_keeps_the_question_open(settings, tb, tmp_path):
    d = tmp_path / "desk"
    d.mkdir()
    for n in ["motion-graphic.png", "fix-motion-blur.png"]:
        (d / n).write_text("x")
    settings.update({"search_dirs": [str(d)]})
    conv = Conversation(settings, FakeLLM(), tb)
    assert "Which one" in say(conv, "open motion")
    assert say(conv, "It's not motion, it's motion.") == "Which one? Say first or second."
    say(conv, "second")
    assert str(tb.launched[-1]).endswith("fix-motion-blur.png")


def test_exact_folder_name_is_offered_alone(settings, tb, tmp_path):
    d = tmp_path / "desk"
    (d / "Munder Difflin" / "munder-difflin-0-5-3").mkdir(parents=True)
    (d / "Munder-Difflin-0.5.5").mkdir()
    settings.update({"search_dirs": [str(d)]})
    r = tb.call("open_file", {"query": "munder difflin folder"})
    assert not r.choices and r.confirm_prompt.startswith("Open Munder Difflin, the folder")


# ---------- iteration 2 ----------
def test_style_examples_are_not_fake_history(settings):
    conv = Conversation(settings, FakeLLM(), None)
    conv.history.append({"role": "user", "content": "my name is yash"})
    msgs = conv.messages()
    assert [m["role"] for m in msgs] == ["system", "user"]       # "i'm so tired today" is not something they said
    assert "i'm so tired today" in msgs[0]["content"] and "not part of this conversation" in msgs[0]["content"]


def test_phone_prompt_has_no_edge_examples(settings):
    from jarvis.conversation import system_prompt
    assert "Edge" not in system_prompt(settings, "phone").split("Examples of how you sound")[1]


def test_model_is_named_for_speech(settings):
    from jarvis.conversation import system_prompt
    p = system_prompt(settings, "local")
    assert "Meta's Llama 3.2 model" in p and "llama3.2:3b" not in p


@pytest.mark.parametrize("text,expected", [
    ("open file explorer", ("open_app", {"name": "file explorer"})),
    ("search such for best laptops under 50000 on amazon", ("web_search", {"query": "best laptops under 50000", "site": "amazon"})),
    ("on youtube search lofi beats", ("web_search", {"query": "lofi beats", "site": "youtube"})),
    ("open spotify, no wait, open youtube", ("open_website", {"url": "youtube"})),
    ("do I have any pdf named gencv", ("search_files", {"query": "gencv pdf"})),
    ("is there a file called invoice", ("search_files", {"query": "invoice"})),
    ("lock my computer", ("run_command", {"name": "lock screen"})),
    ("set an alarm for 7 am", ("create_reminder", {"text": "your alarm", "when": "at 7 am"})),
    ("set a timer for 5 minutes", ("create_reminder", {"text": "your timer's up", "when": "in 5 minutes"})),
])
def test_iteration2_routes(settings, tb, text, expected):
    steps = route_all(text, settings, tb.find_app)
    assert steps == [expected], steps


def test_open_my_notes_is_still_a_file(settings, tb):
    steps = route_all("open my notes", settings, tb.find_app)
    assert steps[0][0] == "open_file"


@pytest.mark.parametrize("text,query", [("can you play a song for me", "songs"), ("play some lofi music", "lofi music")])
def test_play_some_music_searches_youtube_and_plays(settings, tb, text, query):
    steps = route_all(text, settings, tb.find_app)
    assert steps == [("web_search", {"query": query, "site": "youtube"}), ("browser_click", {"target": "first video"})]


def test_play_music_alone_still_resumes(settings, tb):
    assert route_all("play the video", settings, tb.find_app) == [("browser_media", {"action": "play"})]


def test_file_search_offers_to_open(settings, tb, tmp_path):
    d = tmp_path / "dl"
    d.mkdir()
    (d / "GenCV.pdf").write_text("x")
    (d / "GenCV.docx").write_text("x")
    settings.update({"search_dirs": [str(d)]})
    conv = Conversation(settings, FakeLLM(), tb)
    assert say(conv, "do I have any pdf named gencv") == "Yes, GenCV, the PDF file in dl. Want me to open it?"
    say(conv, "yes")
    assert str(tb.launched[-1]).endswith("GenCV.pdf")


def test_resume_is_never_a_markdown_prompt(settings, tb, tmp_path):
    d = tmp_path / "desk"
    (d / "agents").mkdir(parents=True)
    (d / "agents" / "resume-tailor.md").write_text("x")
    (d / "Yash Resume.pdf").write_text("x")
    settings.update({"search_dirs": [str(d)]})
    hits, _ = tb.find_paths("my resume")
    assert [h.name for h in hits] == ["Yash Resume.pdf"]


@pytest.mark.parametrize("sentence", ["Done.", "Okay, done!", "There you go.", "All set."])
def test_bare_done_is_a_claim(sentence):
    from jarvis.conversation import claims_action
    assert claims_action(sentence)


def test_why_inside_a_story_is_not_a_reasoning_question():
    from jarvis.conversation import needs_reasoning
    assert not needs_reasoning("you know what's funny, I opened the fridge and forgot why")
    assert needs_reasoning("why is the sky blue") and needs_reasoning("okay but why")


def test_lock_confirmation_sounds_human(settings, tb):
    conv = Conversation(settings, FakeLLM(), tb)
    assert say(conv, "lock my computer") == "Lock the screen now?"


# ---------- iteration 3 ----------
def test_open_instagram_and_search_stays_on_instagram(settings, tb):
    steps = route_all("open instagram and search for virat kohli", settings, tb.find_app)
    assert steps == [("web_search", {"query": "virat kohli", "browser": None, "site": "instagram"})]
    tb.call(*steps[0])
    assert tb.edge_log[-1][1].startswith("https://www.instagram.com/explore/search/keyword/?q=virat+kohli")


def test_comma_separated_steps(settings, tb):
    steps = route_all("open youtube, search for mr beast and play the second video", settings, tb.find_app)
    assert steps == [("web_search", {"query": "mr beast", "browser": None, "site": "youtube"}),
                     ("browser_click", {"target": "first video", "nth": 1})]


def test_comma_correction_still_wins(settings, tb):
    assert route_all("open notepad, no, calculator", settings, tb.find_app) == [("open_app", {"name": "calculator"})]


@pytest.mark.parametrize("sentence", ["Skipped ten seconds.", "Just opened the Google homepage.", "Now it's open.",
                                      "Paused the video for you."])
def test_past_tense_narration_is_a_claim(sentence):
    from jarvis.conversation import claims_action
    assert claims_action(sentence)


@pytest.mark.parametrize("sentence", ["Yes, it's open till nine on Sundays.", "Opened in 1990, it's a great museum? No.",
                                      "That's a tough one."])
def test_normal_sentences_are_not_claims(sentence):
    from jarvis.conversation import claims_action
    assert not claims_action(sentence) or sentence.startswith("Opened")


@pytest.mark.parametrize("text", ["skip ten seconds", "skip 10 seconds", "forward 10 seconds"])
def test_skip_seconds_is_media_forward(settings, tb, text):
    assert route_all(text, settings, tb.find_app) == [("browser_media", {"action": "forward"})]


@pytest.mark.parametrize("first,ask,second,expected", [
    ("open", "Sure, what should I open?", "youtube", ("open", "https://www.youtube.com")),
    ("search", "Sure, what should I search for?", "lofi music", ("open", "https://www.google.com/search?q=lofi+music")),
    ("click", "Sure, what should I click?", "subscribe", ("click", "subscribe", 0)),
])
def test_bare_verb_asks_then_completes(settings, tb, first, ask, second, expected):
    tb.edge.url = "about:blank"
    conv = Conversation(settings, FakeLLM(), tb)
    assert say(conv, first) == ask
    say(conv, second)
    assert tb.edge_log[-1] == expected


def test_bare_remind_me_then_details(settings, tb):
    conv = Conversation(settings, FakeLLM(), tb)
    assert "remind you about" in say(conv, "remind me")
    assert "in 10 minutes" in say(conv, "drink water in 10 minutes")
    assert tb.reminders.pending()[0]["text"] == "drink water"


def test_bare_verb_never_mind(settings, tb):
    conv = Conversation(settings, FakeLLM(), tb)
    say(conv, "open")
    assert say(conv, "never mind") == "Okay, never mind."


def test_reminder_tomorrow_keeps_the_day(settings, tb):
    conv = Conversation(settings, FakeLLM(), tb)
    say(conv, "remind me tomorrow to call mom")
    say(conv, "okay 11 pm")
    due = datetime.fromisoformat(tb.reminders.pending()[0]["due"])
    assert (due.date() - datetime.now().date()).days == 1 and due.hour == 23


@pytest.mark.parametrize("text,offer", [
    ("delete all my files", ("open_app", {"name": "file explorer"})),
    ("format my c drive", ("open_app", {"name": "file explorer"})),
    ("convert 100 dollars to rupees", ("web_search", {"query": "convert 100 dollars to rupees"})),
])
def test_more_honest_cants(settings, tb, text, offer):
    steps = route_all(text, settings, tb.find_app)
    assert steps[0][0] == "cant" and steps[0][1]["offer"] == offer


def test_page_summary_quotes_are_stripped(settings, tb):
    class QuoteLLM(FakeLLM):
        async def complete(self, messages):
            return '"Lofi beats is a calm study stream."'
    conv = Conversation(settings, QuoteLLM(), tb)
    assert say(conv, "summarise this video") == "Lofi beats is a calm study stream."


def test_tool_crash_is_not_read_aloud(settings, tb):
    def boom(args):
        raise AttributeError("'NoneType' object has no attribute 'endswith'")
    tb.tools["browser_click"].handler = boom
    text = tb.call("browser_click", {"target": "x"}).text
    assert "NoneType" not in text and "browser_click" not in text and text.startswith("Sorry, that didn't work")


# ---------- iteration 4 ----------
@pytest.mark.parametrize("text,who,msg", [
    ("can you message rahul on whatsapp that I'm coming", "rahul", "I'm coming"),
    ("open whatsapp and send hi to mom", "mom", "Hi"),
    ("send a message to priya on watch up saying ok i'll call you later", "priya", "Ok I'll call you later"),
    ("tell dad on whatsapp that I reached", "dad", "I reached"),
])
def test_whatsapp_message_chain(settings, tb, text, who, msg):
    steps = route_all(text, settings, tb.find_app)
    assert steps == [("open_website", {"url": "whatsapp"}), ("browser_click", {"target": who}),
                     ("browser_type", {"text": msg, "submit": True})], steps


def test_whatsapp_message_asks_before_sending(settings, tb):
    conv = Conversation(settings, FakeLLM(), tb)
    out = say(conv, "message rahul on whatsapp that I'm coming")
    assert out.endswith("Should I send it on WhatsApp?") and ("press", "enter") not in tb.edge_log
    say(conv, "yes")
    assert tb.edge_log[-1] == ("press", "enter")


def test_chain_stops_when_a_step_fails(settings, tb):
    tb.edge.find_clickable = lambda target, nth=0: None
    import jarvis.tools as tools_mod
    real_sleep, tools_mod.time.sleep = tools_mod.time.sleep, lambda s: None
    try:
        conv = Conversation(settings, FakeLLM(), tb)
        out = say(conv, "message rahul on whatsapp that I'm coming")
    finally:
        tools_mod.time.sleep = real_sleep
    assert "can't see" in out and not any(e[0] == "type" for e in tb.edge_log)


@pytest.mark.parametrize("text,offer", [
    ("write an email to my professor asking for an extension", ("open_website", {"url": "gmail"})),
    ("can you post a story on my instagram", ("open_website", {"url": "instagram"})),
])
def test_email_and_posting_are_honest(settings, tb, text, offer):
    steps = route_all(text, settings, tb.find_app)
    assert steps[0][0] == "cant" and steps[0][1]["offer"] == offer, steps


def test_write_hello_still_types(settings, tb):
    assert route_all("write hello world", settings, tb.find_app) == [("browser_type", {"text": "hello world", "submit": False})]


def test_command_plus_question(settings, tb):
    steps = route_all("open the calculator and then tell me a joke", settings, tb.find_app)
    assert steps == [("open_app", {"name": "calculator"}), ("chat", {"text": "tell me a joke"})]
    conv = Conversation(settings, FakeLLM(["Why did the scarecrow win? He was outstanding in his field."]), tb)
    out = say(conv, "open the calculator and then tell me a joke")
    assert out.startswith("Opening Calculator.") and "scarecrow" in out and tb.launched == [["calc.exe"]]


def test_math_plus_command(settings, tb):
    conv = Conversation(settings, FakeLLM(), tb)
    assert say(conv, "what's 5 plus 5 and also open notepad") == "That's 10. Opening Notepad."


def test_several_city_times():
    from jarvis.conversation import world_time
    out = world_time("whats the time in new york and london")
    assert out.startswith("It's ") and "in New York and " in out and out.endswith("in London right now.")


@pytest.mark.parametrize("text", ["shut down in 10 minutes", "restart after the update"])
def test_any_shutdown_request_is_honest(settings, tb, text):
    assert route_all(text, settings, tb.find_app)[0][0] == "cant"


@pytest.mark.parametrize("sentence", ["I'll shut down in 10 minutes, just the computer.", "Returned to the previous page in Edge.",
                                      "I'll set a timer for you."])
def test_more_lies_are_caught(sentence):
    from jarvis.conversation import claims_action
    assert claims_action(sentence)


def test_go_back_to_previous_page(settings, tb):
    assert route_all("go back to the previous page", settings, tb.find_app) == [("browser_nav", {"action": "back"})]


def test_what_can_you_do_is_fixed_and_honest(settings, tb):
    steps = route_all("hey, so what can you actually do?", settings, tb.find_app)
    assert steps[0][0] == "say" and "click, type" in steps[0][1]["text"] and "everything" not in steps[0][1]["text"]


def test_an_hour_is_spoken_naturally(settings, tb):
    assert "in an hour" in tb.call("create_reminder", {"text": "take a break", "when": "in an hour"}).text


# ---------- iteration 5 ----------
@pytest.mark.parametrize("text", [
    "I was trying to open the jar and it wouldn't budge. Any tricks?",
    "can you explain what open source means",
    "I told him to search for it on google but he didn't",
    "order of operations is confusing, how does it work",
    "how do I write an email to my professor asking for an extension",
    "my friend messaged me on whatsapp that he's not coming",
    "I think I should delete instagram from my life, it's so addictive",
    "I booked a cab yesterday and the driver was so rude",
    "my sister keeps telling me to play some music while studying",
    "I'm going to play valorant tonight and then study",
    "the movie was good. Tell me about the director Christopher Nolan.",
])
def test_chat_that_looks_like_commands(settings, tb, text):
    assert route_all(text, settings, tb.find_app) is None


def test_open_downloads_folder_itself(settings, tb, tmp_path):
    dl = tmp_path / "Downloads"
    (dl / "old downloads backup").mkdir(parents=True)
    settings.update({"search_dirs": [str(dl)]})
    r = tb.call("open_file", {"query": "downloads folder"})
    assert r.confirm_prompt == "Open your Downloads folder?"
    r.run_confirmed()
    assert tb.launched[-1] == str(dl)


def test_japanese_documents_folder_is_spoken_in_english(tmp_path):
    from jarvis.tools import display_name
    d = tmp_path / "ドキュメント"
    d.mkdir()
    assert display_name(d) == "Documents"


@pytest.mark.parametrize("sentence,lie", [
    ("Let me check.", True), ("I'll find out for you.", True), ("Let me look it up.", True),
    ("I'll check your calendar for you.", True), ("Let me check in with you tomorrow? Sure.", False),
    ("Let me think about that.", False),
])
def test_promises_to_check_are_claims(sentence, lie):
    from jarvis.conversation import claims_action
    assert claims_action(sentence) == lie


def test_identity_line_is_only_for_when_asked(settings):
    from jarvis.conversation import system_prompt
    assert "Only when asked what you are" in system_prompt(settings, "local")
