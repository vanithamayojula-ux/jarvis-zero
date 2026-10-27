"""27 Oct: spidey's mini TV. "play lofi on the TV" (the top YouTube video, filling the window), "show a map of Goa on
the TV", "put youtube.com on the TV", "pause / mute / turn up the TV", "make the TV bigger", "what's on the TV",
"turn off the TV". Its own Edge app window: nothing here launches it (FakeTV stands in)."""
import asyncio
import json

import httpx
import pytest

from conftest import FakeLLM
from jarvis import tv as tvmod
from jarvis.browser import BrowserError, EdgeController
from jarvis.conversation import Conversation, route_all
from jarvis.tools import Reminders, ToolBox


# ---------- routing ----------
@pytest.mark.parametrize("text, step", [
    ("play lofi on the TV", ("tv_show", {"what": "lofi"})),
    ("Jarvis, can you put the news on the telly?", ("tv_show", {"what": "the news"})),
    ("put on some cricket highlights on the mini tv", ("tv_show", {"what": "some cricket highlights"})),
    ("show a map of Goa on the TV", ("tv_show", {"what": "a map of Goa"})),
    ("open youtube.com/watch?v=AbC_dEf-123 on the TV", ("tv_show", {"what": "youtube.com/watch?v=AbC_dEf-123"})),
    ("watch mr beast on the tv", ("tv_show", {"what": "mr beast"})),
    ("on the tv, play arijit singh songs", ("tv_show", {"what": "arijit singh songs"})),
    ("put this on the TV", ("tv_show", {"what": "this"})),
    ("turn on the TV", ("tv_on", {})),
    ("tv on", ("tv_on", {})),
    ("turn the tv off", ("tv_off", {})),
    ("close the mini TV", ("tv_off", {})),
    ("pause the TV", ("tv_control", {"action": "pause"})),
    ("resume the tv", ("tv_control", {"action": "play"})),
    ("mute the TV", ("tv_control", {"action": "mute"})),
    ("turn the TV up", ("tv_control", {"action": "louder"})),
    ("turn down the tv", ("tv_control", {"action": "quieter"})),
    ("make the TV bigger", ("tv_size", {"step": 1})),
    ("make the tv a bit smaller", ("tv_size", {"step": -1})),
    ("make the TV large", ("tv_size", {"step": "large"})),
    ("what's on the TV?", ("tv_what", {})),
])
def test_tv_routes(settings, text, step):
    assert route_all(text, settings) == [step]


@pytest.mark.parametrize("text", ["play lofi", "open youtube", "pause", "turn on the lights", "what's on my screen",
                                  "show me a picture of a tv", "draw a tv", "turn up the volume"])
def test_not_tv(settings, text):
    steps = route_all(text, settings) or []
    assert not any(n.startswith("tv_") for n, _ in steps)


# ---------- what to put on ----------
def _site(low):
    return ("YouTube", "https://www.youtube.com") if low == "youtube" else None


@pytest.mark.parametrize("q, kind, url_or_query", [
    ("lofi", "video", "lofi"),
    ("a video of cats on youtube", "video", "cats"),
    ("some cricket highlights", "video", "some cricket highlights"),
    ("youtube", "page", "https://www.youtube.com"),
    ("https://en.wikipedia.org/wiki/Narwhal", "page", "https://en.wikipedia.org/wiki/Narwhal"),
    ("youtube.com/watch?v=AbC_dEf-123", "page", "https://youtube.com/watch?v=AbC_dEf-123"),
    ("a map of Goa", "page", "https://www.google.com/maps/search/goa"),
    ("delhi on the map", "page", "https://www.google.com/maps/search/delhi"),
    ("pictures of red pandas", "page", "https://www.google.com/search?tbm=isch&q=red+pandas"),
    ("google best phones 2026", "page", "https://www.google.com/search?q=best+phones+2026"),
])
def test_plan(q, kind, url_or_query):
    p = tvmod.plan(q, _site)
    assert p["kind"] == kind and (p.get("url") or p.get("query")) == url_or_query


def test_plan_nothing():
    assert tvmod.plan("", _site) is None and tvmod.plan("  ?? ", _site) is None and tvmod.plan("x" * 400) is None


def test_find_video_takes_only_an_id_and_a_clean_title():
    page = ('<script>var ytInitialData = {"x":1,"videoRenderer":{"videoId":"lTRiuFIWV54","thumbnail":{},'
            '"title":{"runs":[{"text":"1 A.M Study Session \\ud83d\\udcda [lofi hip hop] \\u202eevil"}]}}};</script>')

    def handler(req):
        assert req.url.host == "www.youtube.com" and req.url.params["search_query"] == "lofi"
        return httpx.Response(200, text=page)
    hit = tvmod.find_video("lofi", httpx.Client(transport=httpx.MockTransport(handler)))
    assert hit["url"] == "https://www.youtube.com/watch?v=lTRiuFIWV54"
    assert hit["title"].startswith("1 A.M Study Session") and "\u202e" not in hit["title"]


def test_find_video_none_and_errors():
    empty = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>nothing</html>")))
    assert tvmod.find_video("qwzx", empty) is None
    down = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429)))
    with pytest.raises(httpx.HTTPStatusError):
        tvmod.find_video("lofi", down)


# ---------- where it goes ----------
WORK = (0, 0, 1920, 1032)


def test_rect_above_spidey_on_its_side():
    x, y, w, h = tvmod.tv_rect(WORK, 1.0, "medium", spidey=(1800, 900))
    assert (w, h) == (512, 288 + tvmod.TITLE_BAR)
    assert x + w == 1800 + tvmod.SPIDEY and y + h == 900 - tvmod.GAP      # right edges line up, just above
    x, y, w, h = tvmod.tv_rect(WORK, 1.0, "small", spidey=(40, 900))
    assert x == 40                                                         # spidey on the left: lined up on the left


def test_rect_below_when_no_room_and_kept_on_screen():
    x, y, w, h = tvmod.tv_rect(WORK, 1.25, "large", spidey=(1800, 20))
    assert y == 20 + int(tvmod.SPIDEY * 1.25) + int(tvmod.GAP * 1.25) and x + w <= 1920 and w == 900
    # bigger: the corner nearest the screen's corner stays put (top-right here, so it grows down and left, not
    # up over spidey); seen live on 27 Oct with spidey parked top-right and the TV just below it
    x, y, w, h = tvmod.tv_rect(WORK, 1.0, "large", anchor=(1272, 225, 1912, 625))
    assert (x + w, y) == (1912, 225) and (w, h) == (720, 437)
    x, y, w, h = tvmod.tv_rect(WORK, 1.0, "large", anchor=(100, 700, 612, 1020))   # bottom-left stays bottom-left
    assert (x, y + h) == (100, 1020)
    x, y, w, h = tvmod.tv_rect(WORK, 1.0, "large", anchor=(1500, 600, 2012, 920))   # still kept on screen
    assert x + w == 1920
    x, y, w, h = tvmod.tv_rect(WORK, 1.0, "medium")                        # no spidey: bottom-right corner
    assert x + w == 1920 - tvmod.GAP and y + h == 1032 - tvmod.GAP


# ---------- the Edge window (nothing is launched) ----------
def test_tv_edge_flags(tmp_path):
    assert EdgeController(tmp_path)._window_args() == ["--start-maximized", "about:blank"]
    t = tvmod.MiniTV(tmp_path / "edge-tv", spidey_file=tmp_path / "nope.json")
    args = t._window_args()
    assert any(a.startswith("--app=data:text/html") for a in args) and "--autoplay-policy=no-user-gesture-required" in args
    assert "--start-maximized" not in args and "CalculateNativeWinOcclusion" in t.FEATURES_OFF
    assert "msImplicitSignin" in t.FEATURES_OFF                            # still never signs itself in
    assert t.resize(1) == "large" and t.resize(1) == "large" and t.resize(-1) == "medium" and t.resize("small") == "small"


def test_blocked_site_never_reaches_the_tv(tmp_path):
    class Guard:
        def check(self, url):
            return "on a scam blocklist" if "evil" in url else None
    t = tvmod.MiniTV(tmp_path / "edge-tv", guard=Guard())
    t.ensure_running = lambda: pytest.fail("started Edge for a blocked site")
    with pytest.raises(BrowserError, match="blocked evil.example"):
        t.show("https://evil.example/x")


# ---------- JARVIS ----------
class FakeTV:
    def __init__(self, fail=None):
        self.is_on, self.shown, self.actions, self.size, self.fail = False, [], [], "medium", fail
        self.showing = {}

    def on(self):
        return self.is_on

    def show(self, url, video=False, title=""):
        if self.fail:
            raise self.fail
        self.is_on = True
        self.shown.append((url, video))
        self.showing = {"title": title}
        return title or url

    def warm_up(self):
        self.is_on = True                             # the window opens while the video is being found

    def off(self):
        self.is_on = False

    def media(self, action):
        self.actions.append(action)
        return {"paused": action == "pause", "muted": False, "volume": 60} if self.is_on else None

    def resize(self, step):
        self.size = {1: "large", -1: "small"}.get(step, step)
        return self.size

    def now_showing(self):
        return self.showing.get("title", "") if self.is_on else ""


def _conv(settings, tv=None, channel="local"):
    tools = ToolBox(settings, Reminders(settings.path.parent / "r.json"))
    tools.tv = tv
    conv = Conversation(settings, FakeLLM(), tools, channel)
    events = []
    conv.notify = events.append
    return conv, events


def _reply(conv, text):
    async def go():
        return [s async for s in conv.reply(text)]
    return " ".join(asyncio.run(go()))


@pytest.fixture
def youtube(monkeypatch):
    asked = []

    def fake(q, client=None):
        asked.append(q)
        return None if q == "qwzx" else {"id": "lTRiuFIWV54", "url": "https://www.youtube.com/watch?v=lTRiuFIWV54",
                                         "title": "1 A.M Study Session [lofi hip hop]"}
    monkeypatch.setattr(tvmod, "find_video", fake)
    return asked


def test_play_on_the_tv(settings, youtube):
    tv = FakeTV()
    conv, events = _conv(settings, tv)
    assert _reply(conv, "play lofi on the TV") == "On the TV: 1 A.M Study Session [lofi hip hop]."
    assert youtube == ["lofi"] and tv.shown == [("https://www.youtube.com/watch?v=lTRiuFIWV54", True)]
    assert events[-1] == {"type": "tv", "on": True, "title": "1 A.M Study Session [lofi hip hop]"}
    assert conv.tools.media_opened_at > 0
    assert "couldn't find a video of qwzx" in _reply(conv, "play qwzx on the tv")
    assert tv.is_on                                   # it was already on: left on


def test_nothing_found_leaves_no_empty_tv(settings, youtube, monkeypatch):
    tv = FakeTV()
    conv, events = _conv(settings, tv)
    assert "couldn't find a video of qwzx" in _reply(conv, "play qwzx on the tv") and not tv.is_on
    monkeypatch.setattr(tvmod, "find_video", lambda q, client=None: (_ for _ in ()).throw(httpx.ConnectError("offline")))
    assert "couldn't reach YouTube" in _reply(conv, "play lofi on the tv") and not tv.is_on and events == []


def test_pages_maps_and_controls(settings, youtube):
    tv = FakeTV()
    conv, events = _conv(settings, tv)
    assert _reply(conv, "show a map of Goa on the TV") == "Showing a map of goa on the TV."
    assert tv.shown[-1] == ("https://www.google.com/maps/search/goa", False) and youtube == []
    assert _reply(conv, "pause the tv") == "Paused." and tv.actions == ["pause"]
    assert _reply(conv, "turn the tv up") == "TV volume 60 percent."
    assert _reply(conv, "make the tv bigger") == "Okay, the TV's large now." and settings.get("tv_size") == "large"
    assert "On the TV: a map of goa" in _reply(conv, "what's on the tv")
    assert _reply(conv, "turn off the tv") == "TV off." and not tv.is_on and events[-1]["on"] is False
    assert "already off" in _reply(conv, "turn off the tv")
    assert "TV's off" in _reply(conv, "pause the tv")


def test_tv_on_shows_youtube(settings):
    tv = FakeTV()
    conv, _ = _conv(settings, tv)
    assert "TV's on" in _reply(conv, "turn on the tv") and tv.shown == [("https://www.youtube.com", False)]
    assert "already on" in _reply(conv, "turn on the tv")


def test_put_this_on_the_tv_uses_jarvis_edge(settings):
    class Edge:
        def running(self):
            return True

        def current(self):
            return {"url": "https://www.youtube.com/watch?v=XyZ12345678", "title": "x", "host": "www.youtube.com"}
    tv = FakeTV()
    conv, _ = _conv(settings, tv)
    conv.tools.edge = Edge()
    _reply(conv, "put this on the tv")
    assert tv.shown == [("https://www.youtube.com/watch?v=XyZ12345678", False)]
    conv.tools.edge = None
    assert "nothing open in my Edge" in _reply(conv, "put this on the tv")


def test_tv_honest_when_missing_off_or_failing(settings, youtube):
    conv, events = _conv(settings, None)
    assert "isn't available" in _reply(conv, "play lofi on the tv")
    conv, events = _conv(settings, FakeTV(), channel="phone")
    assert "only on the laptop" in _reply(conv, "play lofi on the tv")
    conv, events = _conv(settings, FakeTV(fail=BrowserError("I blocked evil.example: on a scam blocklist.")))
    assert _reply(conv, "open evil.example on the tv") == "I blocked evil.example: on a scam blocklist."
    conv, events = _conv(settings, FakeTV(fail=RuntimeError("boom")))
    assert "didn't work" in _reply(conv, "play lofi on the tv") and events == []
    settings.update({"mini_tv": False})
    conv, events = _conv(settings, FakeTV())
    assert "switched off" in _reply(conv, "play lofi on the tv")


def test_tv_size_setting_is_validated():
    from fastapi import HTTPException
    from jarvis.server import _validate_settings
    _validate_settings({"tv_size": "small"})
    with pytest.raises(HTTPException):
        _validate_settings({"tv_size": "huge"})
    json.dumps(tvmod.JS_MEDIA)                       # the page scripts are plain strings
