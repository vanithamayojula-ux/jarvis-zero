"""Full desktop control: which window a command goes to, and the safety rails. A fake desktop: nothing real is touched."""
import time

import pytest

from jarvis.conversation import route_all
from jarvis.desktop import BLOCKED_NAMES, BLOCKED_PROCESSES, DesktopError
from jarvis.tools import Reminders, ToolBox

from test_conv_iterations import FakeEdge


class FakeDesktop:
    """Windows by handle: {hwnd: (process, title)}; controls by name per window."""

    def __init__(self):
        self.windows = {1: ("notepad.exe", "Untitled - Notepad"), 2: ("spotify.exe", "Spotify Free"),
                        3: ("msedge.exe", "YouTube - JARVIS Edge"), 4: ("cmd.exe", "Command Prompt"),
                        5: ("whatsapp.exe", "WhatsApp")}
        self.controls = {1: ["File", "Edit", "Save"], 2: ["Play", "Next", "Play lofi chill", "Play Jazz Vibes"],
                         4: [], 5: ["Send", "Rahul"]}
        self.last_hwnd, self.last_changed, self.log = None, 0.0, []
        self.password = False

    def use(self, hwnd):                       # you click into that window
        self.last_hwnd, self.last_changed = hwnd, time.monotonic()

    def find_app_window(self, name, wait=0.0, visible=True):
        return next((h for h, (p, t) in self.windows.items() if name.lower() in p or name.lower() in t.lower()), None)

    def is_edge(self, hwnd): return hwnd == 3
    def process(self, hwnd): return self.windows[hwnd][0]
    def title(self, hwnd): return self.windows[hwnd][1]
    def describe(self, hwnd): return {1: "Notepad", 2: "Spotify", 5: "WhatsApp"}.get(hwnd, self.windows[hwnd][0])
    @staticmethod
    def spotify_installed(): return True

    def check_allowed(self, hwnd, kind=""):
        proc = self.windows[hwnd][0]
        if proc in BLOCKED_PROCESSES:
            raise DesktopError(f"I won't click or type in {BLOCKED_NAMES.get(proc, 'that window')}.")

    def find(self, hwnd, target, nth=0, wait=0.0, prefix=False):
        names = self.controls.get(hwnd, [])
        want = target.lower()
        hits = [n for n in names if (n.lower().startswith(want) and len(n) > len(want)) if prefix] or \
            [n for n in names if n.lower() == want] + [n for n in names if want in n.lower() and n.lower() != want]
        hits = hits if prefix else hits
        return {"name": hits[nth], "button": True, "x": 1, "y": 1} if hits else None

    def click(self, hwnd, hit):
        self.check_allowed(hwnd)
        self.log.append(("click", hwnd, hit["name"]))
        if hwnd == 2 and hit["name"].startswith("Play"):
            self.windows[2] = ("spotify.exe", "Lofi Girl - Snowman")
        return hit["name"]

    def type_text(self, hwnd, text):
        self.check_allowed(hwnd)
        if self.password:
            raise DesktopError("That's a password box, so I'll leave typing that one to you.")
        self.log.append(("type", hwnd, text))
        return "edit"

    def press(self, hwnd, key):
        self.check_allowed(hwnd)
        self.log.append(("press", hwnd, key))

    def media_key(self, action):
        self.log.append(("media", action))
        if action in ("play", "toggle", "next"):
            self.windows[2] = ("spotify.exe", "Lofi Girl - Snowman")
        elif action == "pause":
            self.windows[2] = ("spotify.exe", "Spotify Free")

    def scroll(self, hwnd, d):
        self.check_allowed(hwnd)
        self.log.append(("scroll", hwnd, d))

    def open_uri(self, uri): self.log.append(("uri", uri))


@pytest.fixture
def tb(settings, tmp_path, monkeypatch):
    monkeypatch.setattr("jarvis.tools.time.sleep", lambda s: None)
    settings.update({"browser": "edge", "save_conversations": False})
    edge, d = FakeEdge(), FakeDesktop()
    box = ToolBox(settings, Reminders(tmp_path / "r.json"), launcher=lambda a: None, browser=lambda u: None,
                  opener=lambda p: None, runner=lambda a: "ok", edge=edge, desktop=d)
    box.d, box.edge_log = d, edge.log
    return box


def test_click_goes_to_the_app_you_were_using(tb):
    tb.d.use(1)
    r = tb.call("browser_click", {"target": "save"})
    assert r.text == "Clicked Save in Notepad." and ("click", 1, "Save") in tb.d.log


def test_after_jarvis_uses_edge_clicks_stay_in_edge(tb):
    tb.d.use(1)
    tb.call("web_search", {"query": "lofi", "site": "youtube"})          # JARVIS just acted in Edge
    r = tb.call("browser_click", {"target": "save"})
    assert ("click", "save", 0) in tb.edge_log and not tb.d.log


def test_switching_to_an_app_after_edge_moves_clicks_there(tb):
    tb.call("web_search", {"query": "lofi", "site": "youtube"})
    tb.edge_used_at -= 1                     # (the clock ticks every ~15 ms on Windows: make "afterwards" explicit)
    tb.d.use(1)                                                           # you clicked into Notepad afterwards
    assert tb.call("browser_click", {"target": "edit"}).text == "Clicked Edit in Notepad."


def test_web_only_targets_stay_in_edge(tb):
    tb.d.use(1)
    tb.call("browser_click", {"target": "first video"})
    assert ("click", "first video", 0) in tb.edge_log


def test_named_app_wins(tb):
    tb.d.use(1)
    assert tb.call("browser_click", {"target": "play in spotify"}).text == "Clicked Play in Spotify."
    assert tb.call("browser_type", {"text": "hello in notepad"}).text == "Typed “hello” in Notepad."


def test_not_an_app_name_is_kept_as_text(tb):
    tb.d.use(1)
    assert tb.call("browser_type", {"text": "I'm in college"}).text == "Typed “I'm in college” in Notepad."


def test_terminal_is_off_limits(tb):
    tb.d.use(4)
    for name, args in [("browser_type", {"text": "del /s *"}), ("browser_click", {"target": "x"}),
                       ("browser_key", {"key": "enter"})]:
        r = tb.call(name, args)
        assert r.text.startswith("I won't click or type in Command Prompt"), (name, r.text)
    assert not tb.d.log


def test_password_box_refused(tb):
    tb.d.use(1)
    tb.d.password = True
    assert "password box" in tb.call("browser_type", {"text": "hunter2"}).text


def test_send_button_and_enter_in_chat_apps_ask_first(tb):
    tb.d.use(5)
    r = tb.call("browser_click", {"target": "send"})
    assert r.needs_confirmation and "Send" in r.confirm_prompt and not tb.d.log
    r = tb.call("browser_type", {"text": "on my way", "submit": True})
    assert r.needs_confirmation and ("press", 5, "enter") not in tb.d.log
    assert r.run_confirmed().text == "Sent." and ("press", 5, "enter") in tb.d.log


def test_enter_in_normal_app_just_happens(tb):
    tb.d.use(1)
    r = tb.call("browser_type", {"text": "notes", "submit": True})
    assert r.text == "Typed “notes” and pressed Enter in Notepad."


def test_spotify_search_opens_the_app_and_plays_top_result(tb, settings):
    steps = route_all("play lofi on spotify", settings)
    texts = [tb.call(*s).text for s in steps]
    assert ("uri", "spotify:search:lofi") in tb.d.log
    assert texts[0] == "Searching Spotify for lofi."
    assert texts[1] == "Playing lofi chill on Spotify: Lofi Girl - Snowman."
    assert ("click", 2, "Play lofi chill") in tb.d.log


def test_pause_spotify_from_anywhere(tb, settings):
    tb.d.windows[2] = ("spotify.exe", "Lofi Girl - Snowman")
    steps = route_all("pause spotify", settings)
    assert steps == [("browser_media", {"action": "pause", "app": "spotify"})]
    assert tb.call(*steps[0]).text == "Paused Spotify."
    assert tb.call("browser_media", {"action": "pause", "app": "spotify"}).text == "Spotify's already paused."


def test_play_with_nothing_in_edge_uses_media_keys(tb):
    tb.edge.media = lambda a: "none"
    tb.call("web_search", {"query": "x", "site": "youtube"})              # Edge was the last thing used
    assert tb.call("browser_media", {"action": "play"}).text.startswith("Playing Lofi Girl")


def test_open_app_then_click_targets_that_app(tb):
    tb.settings.update({"approved_apps": {**tb.settings.get("approved_apps"), "notepad": "notepad.exe"}})
    tb.call("open_app", {"name": "notepad"})
    assert tb.call("browser_click", {"target": "file"}).text == "Clicked File in Notepad."


def test_desktop_control_can_be_switched_off(tb):
    tb.settings.update({"desktop_control": False})
    tb.d.use(1)
    tb.call("browser_click", {"target": "save"})
    assert not tb.d.log and ("click", "save", 0) in tb.edge_log
