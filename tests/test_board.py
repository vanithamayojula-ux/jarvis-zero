"""27 Oct: spidey's picture board. "show me a picture of …" (Wikipedia's lead picture; only the words go out),
"show my photo called …" (your own files), "draw …" (the model's SVG, turned into checked plain shapes), and
"close the picture". Display only: nothing here can click, open or save anything by itself."""
import asyncio
import io
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from conftest import EnergyVAD, FakeLLM, FakeSTT, FakeTTS
from jarvis import board, server
from jarvis.conversation import Conversation, route_all
from jarvis.testing import ScriptedMic, SilentSpeaker
from jarvis.tools import Reminders, ToolBox


def _png(w=40, h=30, colour=(200, 30, 40)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (w, h), colour).save(out, "PNG")
    return out.getvalue()


async def _collect(conv, text):
    return [s async for s in conv.reply(text)]


def _reply(conv, text):
    return asyncio.run(_collect(conv, text))


# ---------- routing ----------
@pytest.mark.parametrize("text, step", [
    ("show me a picture of a red panda", ("show_picture", {"query": "red panda"})),
    ("Jarvis, can you show me a photo of the Eiffel Tower?", ("show_picture", {"query": "eiffel tower"})),
    ("find me some images of lamborghini aventador", ("show_picture", {"query": "lamborghini aventador"})),
    ("what does a narwhal look like", ("show_picture", {"query": "narwhal"})),
    ("let me see a picture of the taj mahal", ("show_picture", {"query": "taj mahal"})),
    ("draw a cat", ("draw", {"what": "a cat"})),
    ("Can you draw me a picture of a rocket going to the moon?", ("draw", {"what": "a rocket going to the moon"})),
    ("sketch the water cycle for me", ("draw", {"what": "the water cycle"})),
    ("make a diagram of how a bill becomes law", ("draw", {"what": "how a bill becomes law"})),
    ("show my photo called goa trip", ("show_my_picture", {"query": "goa trip"})),
    ("close the picture", ("board_close", {})),
    ("hide the drawing", ("board_close", {})),
])
def test_board_routes(settings, text, step):
    assert route_all(text, settings) == [step]


@pytest.mark.parametrize("text", ["what does it look like", "what do you look like", "what does my code look like",
                                  "show me a picture of my screen", "draw", "can you draw?", "open paint",
                                  "show me youtube"])
def test_not_board(settings, text):
    steps = route_all(text, settings) or []
    assert not any(n in ("show_picture", "draw", "show_my_picture") for n, _ in steps)


# ---------- the checks (both ends) ----------
def test_validate_drawing_rejects_junk_and_clamps():
    d = board.validate_drawing({"title": "x\u202e\x00evil", "bg": "red", "shapes": [
        {"k": "poly", "p": [0, 0, 10, 10, 9999, -9999], "fill": "#ff0000", "line": "", "w": 2, "closed": True},
        {"k": "oval", "p": [1, 2, 3], "fill": "#ff0000"},                 # wrong count
        {"k": "rect", "p": [0, 0, float("nan"), 5], "fill": "#ff0000"},   # NaN
        {"k": "rect", "p": [0, 0, True, 5], "fill": "#ff0000"},           # a bool is not a number
        {"k": "rect", "p": [0, 0, 5, 5], "fill": "url(#x)"},              # not a plain colour: nothing visible
        {"k": "window", "p": [0, 0, 5, 5], "fill": "#ff0000"},            # not a shape kind
        {"k": "text", "p": [5, 5], "text": "a" * 500, "size": 999, "fill": "#000000", "a": "evil"},
    ]})
    assert d["bg"] == "#ffffff" and "\u202e" not in d["title"] and "\x00" not in d["title"]
    assert [s["k"] for s in d["shapes"]] == ["poly", "text"]
    assert d["shapes"][0]["p"][4:] == [440.0, -40.0]
    t = d["shapes"][1]
    assert len(t["text"]) == board.MAX_TEXT and t["size"] == 48.0 and t["a"] == "start"
    assert board.validate_drawing({"shapes": []}) is None
    assert board.validate_drawing({"shapes": "nope"}) is None
    assert board.validate_drawing(None) is None


def test_validate_drawing_caps_size():
    many = {"shapes": [{"k": "rect", "p": [0, 0, 5, 5], "fill": "#000000"}] * 5000}
    assert len(board.validate_drawing(many)["shapes"]) == board.MAX_SHAPES
    huge = {"shapes": [{"k": "poly", "p": [1.0] * (board.MAX_NUMS + 2), "fill": "#000000"}]}
    assert board.validate_drawing(huge) is None


def test_colours():
    assert board.colour("Red") == "#ff0000" and board.colour("#abc") == "#aabbcc" and board.colour("none") == ""
    assert board.colour("rgb(255, 0, 10)") == "#ff000a" and board.colour("javascript:alert(1)") is None


# ---------- SVG → shapes ----------
SVG = """Sure! Here it is:
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 200 150">
  <defs><linearGradient id="g"><stop offset="0" stop-color="#112233"/></linearGradient></defs>
  <rect width="200" height="150" fill="#eef"/>
  <g transform="translate(100,75) scale(2)">
    <circle r="10" fill="url(#g)" stroke="black" stroke-width="1"/>
  </g>
  <path d="M10 10 L 30 10 q 10 10 20 0 a 5 5 0 0 1 10 0 Z" fill="none" stroke="#f00"/>
  <polyline points="0,0 10,10 20,0" style="fill:none;stroke:blue;stroke-width:3"/>
  <text x="100" y="140" text-anchor="middle" font-size="10">Fish &amp; chips & peas</text>
  <image href="http://evil.example/x.png"/><script>alert(1)</script><a href="http://x"><rect width="1" height="1"/></a>
</svg> enjoy"""


def test_svg_to_drawing():
    d = board.svg_to_drawing(SVG, "a fish")
    kinds = [s["k"] for s in d["shapes"]]
    assert d["bg"] == "#eeeeff" and d["title"] == "a fish"
    assert kinds[:5] == ["rect", "oval", "poly", "poly", "text"]
    circle = d["shapes"][1]
    assert circle["fill"] == "#112233" and circle["p"] == [160.0, 110.0, 240.0, 190.0]   # fitted 200x150 → 400x300
    path = d["shapes"][2]
    assert path["closed"] and path["line"] == "#ff0000" and path["fill"] == ""
    assert d["shapes"][4]["text"] == "Fish & chips & peas" and d["shapes"][4]["a"] == "middle"
    assert not any("http" in json.dumps(s) for s in d["shapes"])


def test_svg_salvages_a_cut_off_reply():
    d = board.svg_to_drawing('<svg viewBox="0 0 400 300"><g><rect width="50" height="50" fill="red"/><circle cx="5" cy')
    assert d and d["shapes"][0]["k"] == "rect"


@pytest.mark.parametrize("bad", ["no svg here", "<svg><rect", '<!DOCTYPE x [<!ENTITY a "aaa">]><svg>&a;</svg>',
                                 "<svg viewBox='0 0 10 10'></svg>"])
def test_svg_bad_input(bad):
    assert board.svg_to_drawing(bad) is None


def test_render_png():
    d = board.svg_to_drawing(SVG)
    im = Image.open(io.BytesIO(board.render_png(d, 1.0)))
    assert im.size == (board.W, board.H)


# ---------- pictures ----------
def test_image_addresses():
    assert board.allowed_image_url("https://upload.wikimedia.org/wikipedia/commons/a/ab/X.jpg")
    assert board.allowed_image_url("https://thumb.wikimedia.org/wikipedia/commons/thumb/x/960px-X.jpg?utm=1")
    for bad in ("http://upload.wikimedia.org/x.jpg", "https://upload.wikimedia.org.evil.com/x.jpg",
                "https://user@upload.wikimedia.org/x.jpg", "https://upload.wikimedia.org:8443/x.jpg", "file:///c:/x.png"):
        assert not board.allowed_image_url(bad)


def test_to_png_shrinks_and_refuses_junk():
    big = _png(2000, 1000)
    im = Image.open(io.BytesIO(board.to_png(big)))
    assert im.size == (900, 450)
    with pytest.raises(board.PictureError):
        board.to_png(b"<html>not a picture</html>")


def _wiki(handler):
    return board.Pictures(httpx.Client(transport=httpx.MockTransport(handler)))


def test_find_prefers_exact_title_and_skips_disambiguation():
    def handler(req):
        assert req.url.host == "en.wikipedia.org" and req.url.params["gsrsearch"] == "mercury"
        return httpx.Response(200, json={"query": {"pages": [
            {"title": "Mercury (disambiguation)", "index": 1, "pageprops": {"disambiguation": ""},
             "thumbnail": {"source": "https://upload.wikimedia.org/a.png"}},
            {"title": "Mercury Records", "index": 2, "thumbnail": {"source": "https://evil.example/b.png"}},
            {"title": "Freddie Mercury", "index": 3, "thumbnail": {"source": "https://upload.wikimedia.org/c.jpg"}},
            {"title": "Mercury", "index": 4},                                  # no picture
        ]}})
    hit = _wiki(handler).find("mercury")
    assert hit["title"] == "Freddie Mercury" and hit["page"] == "https://en.wikipedia.org/wiki/Freddie_Mercury"


def test_fetch_checks_host_type_size_and_redirects():
    png = _png()

    def handler(req):
        path = req.url.path
        if path == "/ok.png":
            return httpx.Response(200, content=png, headers={"content-type": "image/png"})
        if path == "/hop.png":
            return httpx.Response(302, headers={"location": "https://upload.wikimedia.org/ok.png"})
        if path == "/away.png":
            return httpx.Response(302, headers={"location": "https://evil.example/x.png"})
        if path == "/x.svg":
            return httpx.Response(200, content=b"<svg/>", headers={"content-type": "image/svg+xml"})
        if path == "/huge.png":
            return httpx.Response(200, content=b"\0" * (board.MAX_BYTES + 10), headers={"content-type": "image/png"})
        return httpx.Response(404)
    p = _wiki(handler)
    assert Image.open(io.BytesIO(p.fetch("https://upload.wikimedia.org/ok.png"))).size == (40, 30)
    assert p.fetch("https://upload.wikimedia.org/hop.png")
    for bad in ("https://upload.wikimedia.org/away.png", "https://upload.wikimedia.org/x.svg",
                "https://upload.wikimedia.org/huge.png", "https://evil.example/ok.png"):
        with pytest.raises(board.PictureError):
            p.fetch(bad)


def test_store_keeps_a_few_and_checks_ids():
    st = board.BoardStore(keep=2)
    a, b, c = st.put(b"1"), st.put(b"2"), st.put(b"3")
    assert st.get(a) is None and st.get(b) == b"2" and st.get(c) == b"3"
    assert st.get("../../etc") is None and st.get(c.upper()) is None


# ---------- JARVIS ----------
class FakePictures:
    def __init__(self, hit=None, fail=False):
        self.hit, self.fail, self.asked = hit, fail, []

    def find(self, q):
        self.asked.append(q)
        if self.fail:
            raise httpx.ConnectError("offline")
        return self.hit

    def fetch(self, url):
        return _png()


def _conv(settings, llm=None, channel="local"):
    conv = Conversation(settings, llm or FakeLLM(), ToolBox(settings, Reminders(settings.path.parent / "r.json")), channel)
    events = []
    conv.notify = events.append
    return conv, events


def test_show_picture(settings):
    conv, events = _conv(settings)
    conv._pictures = FakePictures({"title": "Red panda", "img": "https://upload.wikimedia.org/x.jpg",
                                   "page": "https://en.wikipedia.org/wiki/Red_panda", "desc": ""})
    assert _reply(conv, "show me a picture of a red panda") == ["Here's Red panda, from Wikipedia."]
    assert conv._pictures.asked == ["red panda"]
    assert [e["kind"] for e in events] == ["busy", "image"]
    shown = events[-1]
    assert shown["source"] == "Wikipedia" and board.STORE.get(shown["id"]) is not None


def test_show_picture_not_found_and_offline(settings):
    conv, events = _conv(settings)
    conv._pictures = FakePictures(None)
    assert "couldn't find a picture of qwzx" in _reply(conv, "show me a picture of qwzx")[0]
    conv._pictures = FakePictures(fail=True)
    assert "couldn't get a picture from Wikipedia" in " ".join(_reply(conv, "show me a picture of a cat"))
    assert events[-1]["kind"] == "failed"


class DrawLLM(FakeLLM):
    key = "k"                                            # a cloud key is set

    def __init__(self, svg):
        super().__init__()
        self.svg, self.asked = svg, []

    async def cloud_only(self, messages, max_tokens=80, temperature=0.0, timeout=6.0):
        self.asked.append((messages, max_tokens, temperature))
        return self.svg


def test_draw(settings):
    llm = DrawLLM(SVG)
    conv, events = _conv(settings, llm)
    assert _reply(conv, "draw a fish") == ["Here's my drawing of a fish."]
    msgs, max_tokens, temp = llm.asked[0]
    assert "SVG" in msgs[0]["content"] and msgs[1]["content"] == "Draw: a fish" and max_tokens <= 2000
    assert [e["kind"] for e in events] == ["busy", "drawing"]
    assert board.validate_drawing(events[-1]["drawing"]) == events[-1]["drawing"]
    assert _reply(conv, "draw it again") == ["Here's my drawing of a fish."]
    assert llm.asked[-1][2] > temp                       # "again" asks for a different one


def test_draw_failure_is_honest(settings):
    conv, events = _conv(settings, DrawLLM("I can't draw, sorry."))
    said = " ".join(_reply(conv, "draw a dragon"))
    assert said.startswith("Sorry, I couldn't draw a dragon") and events[-1]["kind"] == "failed"


def test_draw_when_the_cloud_is_busy_waits_instead_of_the_slow_local_model(settings):
    llm = DrawLLM(None)

    async def no_local(messages):
        pytest.fail("fell back to the model on this laptop")
    llm.complete = no_local
    conv, events = _conv(settings, llm)
    assert "Try again in about a minute" in " ".join(_reply(conv, "draw a dragon"))


def test_draw_without_any_cloud_uses_the_local_model(settings):
    class LocalOnly(FakeLLM):
        async def complete(self, messages):
            return SVG
    conv, events = _conv(settings, LocalOnly())
    assert _reply(conv, "draw a fish") == ["Here's my drawing of a fish."]


def test_board_only_on_the_laptop_and_can_be_switched_off(settings):
    conv, events = _conv(settings, DrawLLM(SVG), channel="phone")
    assert "only show pictures on the laptop" in " ".join(_reply(conv, "draw a cat"))
    settings.update({"picture_board": False})
    conv, events = _conv(settings, DrawLLM(SVG))
    assert "switched off" in " ".join(_reply(conv, "draw a cat"))
    assert events == []


def test_close(settings):
    conv, events = _conv(settings)
    _reply(conv, "close the picture")
    assert events == [{"type": "board_close"}]


def test_show_my_picture(settings, tmp_path):
    (tmp_path / "goa trip.png").write_bytes(_png())
    (tmp_path / "goa trip.exe").write_bytes(b"MZ")
    (tmp_path / "beach 1.jpg").write_bytes(_png())
    (tmp_path / "beach 2.jpg").write_bytes(_png())
    settings.update({"search_dirs": [str(tmp_path)]})
    conv, events = _conv(settings)
    assert _reply(conv, "show my photo called goa trip") == ["Here's goa trip."]
    assert events[-1]["source"] == "your files"
    said = " ".join(_reply(conv, "show my photo called beach"))
    assert "Which one?" in said
    assert _reply(conv, "the second one")[0].startswith("Here's beach")
    assert "couldn't find a picture called zebra" in _reply(conv, "show my photo called zebra")[0]


# ---------- the API ----------
@pytest.fixture
def client(settings):
    ScriptedMic.script = []
    server.S.no_hotkey = True
    server.build(settings, mic_factory=ScriptedMic, speaker_factory=SilentSpeaker, llm=FakeLLM(), stt=FakeSTT(),
                 tts=FakeTTS(), vad_factory=EnergyVAD)
    with TestClient(server.app) as c:
        yield c
    server.S.engine = None


def test_board_endpoint(client):
    bid = board.STORE.put(_png())
    r = client.get(f"/api/board/{bid}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.headers["cache-control"] == "no-store"
    assert client.get("/api/board/0123456789abcdef").status_code == 404
    assert client.get(f"/api/board/{bid.upper()}").status_code == 404
    assert client.get("/api/board/x.png").status_code == 404
