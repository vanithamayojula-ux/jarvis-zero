"""Regressions from the 22 Oct log: Spotify, 'search X and play', 'most likes', 'can't you click', Post Malone."""
import pytest

from jarvis.conversation import claims_action, route_all, CLICK_TYPE
from jarvis.tools import Reminders, ToolBox

from test_conv_iterations import FakeEdge


@pytest.fixture
def tb(settings, tmp_path):
    settings.update({"browser": "edge", "save_conversations": False})
    edge, launched = FakeEdge(), []
    box = ToolBox(settings, Reminders(tmp_path / "r.json"), launcher=launched.append, browser=launched.append,
                  opener=launched.append, runner=lambda a: "ok", edge=edge)
    box.edge_log = edge.log
    return box


@pytest.mark.parametrize("text", ["cant you do clicks and typing", "can't you do clicks and typing?", 
                                  "can you click and type", "are you able to click", "you can't click", "do clicks"])
def test_click_and_type_question_gets_a_true_answer(settings, text):
    assert route_all(text, settings) == [("say", {"text": CLICK_TYPE})]


def test_click_commands_are_still_commands(settings):
    assert route_all("can you click the first video", settings)[0][0] == "browser_click"
    assert route_all("type hello", settings)[0][0] == "browser_type"


@pytest.mark.parametrize("text, site, query", [
    ("search for lofi song and play on e", None, "lofi song"),
    ("search for lofi song and play it", None, "lofi song"),
    ("search lofi and play it on spotify", "spotify web", "lofi"),
    ("play lofi beats on spotify", "spotify web", "lofi beats"),
    ("play random song in spotify", "spotify web", "today's top hits"),
])
def test_search_and_play_starts_the_top_result(settings, text, site, query):
    steps = route_all(text, settings)
    assert steps == [("web_search", {"query": query, "site": site, "media": True}),
                     ("browser_media", {"action": "play_result"})]


def test_open_youtube_search_and_play_it(settings):
    steps = route_all("open youtube and search for miles morales song and play it", settings)
    assert steps[0][0] == "web_search" and steps[0][1]["site"] == "youtube" and steps[0][1]["media"]
    assert steps[-1] == ("browser_media", {"action": "play_result"})


def test_video_with_high_likes_picks_most_viewed(settings):
    steps = route_all("then open youtube and search for miles morales song and click the video with high likes", settings)
    assert steps[-1] == ("browser_click", {"target": "most viewed video", "asked": "likes"})
    assert route_all("play the most viewed video", settings) == [
        ("browser_click", {"target": "most viewed video", "asked": "views"})]


def test_likes_answer_is_honest(tb):
    r = tb.call("browser_click", {"target": "most viewed video", "asked": "likes"})
    assert "don't show likes" in r.text and "most viewed" in r.text


def test_video_titled_post_malone_is_not_publishing(tb):
    tb.edge.find_clickable = lambda target, nth=0: {"x": 1, "y": 1, "button": False,
                                                    "text": "Post Malone, Swae Lee - Sunflower (Spider-Man)"}
    r = tb.call("browser_click", {"target": "first video"})
    assert not r.needs_confirmation and r.text.startswith("Clicked")


def test_real_post_button_still_asks(tb):
    tb.edge.find_clickable = lambda target, nth=0: {"x": 1, "y": 1, "button": True, "text": "Post"}
    assert tb.call("browser_click", {"target": "the blue button"}).needs_confirmation
    tb.edge.find_clickable = lambda target, nth=0: {"x": 1, "y": 1, "button": False, "text": "share"}
    assert tb.call("browser_click", {"target": "share"}).needs_confirmation   # you named it yourself


def test_media_search_on_youtube_clicks_first_video(tb):
    assert tb.call("web_search", {"query": "lofi", "media": True}).text.startswith("Searching on YouTube")
    r = tb.call("browser_media", {"action": "play_result"})
    assert r.text.startswith("Clicked") and ("click", "first video", 0) in tb.edge_log


def test_media_search_stays_on_spotify(tb):
    tb.edge.url = "https://open.spotify.com/"
    assert "Spotify" in tb.call("web_search", {"query": "lofi", "media": True}).text
    assert "open.spotify.com/search/lofi" in tb.edge.url


def test_spotify_play_checks_it_really_plays(tb, monkeypatch):
    monkeypatch.setattr("jarvis.tools.time.sleep", lambda s: None)
    tb.edge.url = "https://open.spotify.com/search/lofi"
    playing = {"now": False}
    tb.edge.media = lambda a: ("playing" if playing["now"] else "paused") if a == "is_playing" else "pressed"
    r = tb.call("browser_media", {"action": "play_result"})
    assert r.text.startswith("I can't get Spotify to start") and "log in" in r.text   # logged out: honest
    playing["now"] = True
    assert tb.call("browser_media", {"action": "play_result"}).text == "Playing it on Spotify."


def test_spotify_logged_out_plays_on_youtube_instead(tb):
    tb.edge.url = "https://open.spotify.com/"
    tb.call("web_search", {"query": "lofi beats", "media": True})
    real_media = tb.edge.media
    tb.edge.media = lambda a: "login" if "spotify" in tb.edge.url else real_media(a)
    r = tb.call("browser_media", {"action": "play_result"})
    assert r.text.startswith("Spotify needs a login") and "YouTube" in r.text and "Clicked" in r.text
    assert "youtube.com/results?search_query=lofi+beats" in tb.edge.url and ("click", "first video", 0) in tb.edge_log


def test_spotify_logged_out_is_honest(tb):
    tb.edge.url = "https://open.spotify.com/"
    tb.edge.media = lambda a: "login"
    assert "log in" in tb.call("browser_media", {"action": "pause"}).text


def test_spotify_nothing_found_is_honest(tb, monkeypatch):
    monkeypatch.setattr("jarvis.tools.time.sleep", lambda s: None)
    tb.edge.url = "https://open.spotify.com/"
    tb.edge.media = lambda a: "none"
    assert tb.call("browser_media", {"action": "play"}).text.startswith("I can't find anything to play on Spotify")


@pytest.mark.parametrize("text", ["Can you type in a website?", "can you click on websites", "can you type in other apps"])
def test_capability_questions_are_not_typed(settings, text):
    assert route_all(text, settings) == [("say", {"text": CLICK_TYPE})]


@pytest.mark.parametrize("s, claim", [
    ("You mean the volume? I've got it up now.", True),
    ("Found a random song on Spotify. It's \"Best Day of My Life\" by American Authors.", True),
    ("I found you a great lofi track.", True),
    ("I found that really funny.", False),
    ("Found it hard to sleep last night?", False),
])
def test_found_a_song_is_a_claim(s, claim):
    assert claims_action(s) is claim


@pytest.mark.parametrize("text", ["scroll", "scroll more", "scroll through the videos", "scrolling down", "go down a bit",
                                  "scroll down a little"])
def test_scroll_phrases_really_scroll(settings, text):
    assert route_all(text, settings) == [("browser_scroll", {"direction": "down"})]


@pytest.mark.parametrize("text", ["keep on scrolling", "keep scrolling", "continue scrolling", "keep scrolling down"])
def test_keep_scrolling_keeps_going(settings, text):
    assert route_all(text, settings) == [("browser_scroll", {"direction": "down", "continuous": True, "speed": "normal"})]


def test_do_that_again_repeats_the_last_scroll(settings, tb):
    from conftest import FakeLLM
    from jarvis.conversation import Conversation
    from test_conv_iterations import say
    conv = Conversation(settings, FakeLLM(["Scrolling down again."]), tb)
    say(conv, "scroll down")
    say(conv, "do that")
    say(conv, "a bit more")
    assert [e for e in tb.edge_log if e[0] == "scroll"] == [("scroll", "down")] * 3


def test_again_without_a_recent_action_is_just_chat(settings, tb):
    from conftest import FakeLLM
    from jarvis.conversation import Conversation
    from test_conv_iterations import say
    conv = Conversation(settings, FakeLLM(["Sure, what should we do again?"]), tb)
    say(conv, "again")
    assert not [e for e in tb.edge_log if e[0] == "scroll"]


@pytest.mark.parametrize("s, claim", [("Scrolling down the page.", True), ("Scrolling down again.", True),
                                      ("Pausing the video now.", True), ("Skipping breakfast isn't great.", False),
                                      ("Switching careers is hard.", False), ("Muting it for you.", True)])
def test_scroll_and_pause_claims_are_caught(s, claim):
    assert claims_action(s) is claim
