"""Typing mode: "type" on its own, then everything said or typed goes into the app word for word, until "end"."""
import pytest

from conftest import FakeLLM
from jarvis.conversation import TYPING_IDLE_S, Conversation, route_all
from jarvis.tools import Reminders, ToolBox

from test_conv_iterations import FakeEdge, say
from test_desktop_control import FakeDesktop


@pytest.fixture
def tb(settings, tmp_path, monkeypatch):
    monkeypatch.setattr("jarvis.tools.time.sleep", lambda s: None)
    settings.update({"browser": "edge", "save_conversations": False})
    edge, d = FakeEdge(), FakeDesktop()
    box = ToolBox(settings, Reminders(tmp_path / "r.json"), launcher=lambda a: None, browser=lambda u: None,
                  opener=lambda p: None, runner=lambda a: "ok", edge=edge, desktop=d)
    box.d, box.edge_log = d, edge.log
    return box


@pytest.fixture
def conv(settings, tb):
    c = Conversation(settings, FakeLLM(), tb)
    c.events = []
    c.notify = c.events.append
    return c


def typed(tb):
    return [e[2] for e in tb.d.log if e[0] == "type"]


@pytest.mark.parametrize("text", ["type", "Type.", "start typing", "typing mode", "typing mode on", "dictation",
                                  "Jarvis, type", "write", "type something"])
def test_starting_typing_mode(settings, text):
    assert route_all(text, settings) == [("typing_mode", {})]


def test_type_in_an_app_and_one_shot_type_still_work(settings):
    assert route_all("type in notepad", settings)[0] == ("typing_mode", {"app": "notepad", "raw": "in notepad"})
    assert route_all("type Hello there", settings) == [("browser_type", {"text": "Hello there", "submit": False})]
    assert route_all("can you type?", settings)[0][0] != "typing_mode"


def test_everything_is_typed_until_end(conv, tb):
    tb.d.use(1)
    assert say(conv, "type").startswith("Typing mode on. Everything you say now goes into Notepad")
    assert say(conv, "Dear Rahul,") == "Typed."
    say(conv, "open YouTube and delete everything")          # words, not a command
    say(conv, ", thanks!")
    assert typed(tb) == ["Dear Rahul,", " open YouTube and delete everything", ", thanks!"]
    assert not any(e[0] == "open" for e in tb.edge_log) and not any(e[0] == "press" for e in tb.d.log)
    assert say(conv, "end") == "Typing mode off. I typed 3 pieces into Notepad."
    assert conv.typing is None
    say(conv, "open youtube")                                 # commands work again
    assert any(e[0] == "open" for e in tb.edge_log)


@pytest.mark.parametrize("stop", ["end", "End.", "stop typing", "end typing", "Jarvis, end", "done typing"])
def test_ways_to_end(conv, tb, stop):
    tb.d.use(1)
    say(conv, "type")
    assert say(conv, stop) == "Typing mode off."
    assert typed(tb) == []


def test_by_voice_pieces_are_quiet_and_a_heard_and_ends_it(conv, tb):
    tb.d.use(1)
    conv.spoken_input = True
    say(conv, "type")
    assert say(conv, "Hello world.") == ""                  # nothing spoken over you while you dictate
    assert say(conv, "And.") .startswith("Typing mode off")  # speech-to-text often writes a lone "end" as "And."
    assert typed(tb) == ["Hello world."]


def test_typed_and_is_just_text(conv, tb):
    tb.d.use(1)
    say(conv, "type")
    say(conv, "and")
    assert typed(tb) == ["and"] and conv.typing is not None


def test_switches_itself_off_after_quiet_minutes(conv, tb):
    tb.d.use(1)
    now = [1000.0]
    conv.clock = lambda: now[0]
    say(conv, "type")
    now[0] += TYPING_IDLE_S + 1
    assert "switched itself off" in say(conv, "open youtube")
    assert typed(tb) == [] and conv.typing is None


def test_types_into_my_edge_when_that_was_used_last(conv, tb):
    assert "my Edge window" in say(conv, "type")
    say(conv, "best phones under 20000")
    assert ("type", "best phones under 20000") in tb.edge_log


def test_named_app(conv, tb):
    tb.d.use(5)
    assert "into Notepad" in say(conv, "type in notepad")
    say(conv, "hi")
    assert ("type", 1, "hi") in tb.d.log


def test_not_an_app_is_typed_as_words(conv, tb):
    tb.d.use(1)
    out = say(conv, "type in the morning")
    assert typed(tb) == ["in the morning"] and conv.typing is None, (out, tb.d.log, tb.edge_log)


def test_terminal_refused_up_front(conv, tb):
    tb.d.use(4)
    out = say(conv, "type")
    assert "won't" in out and conv.typing is None


def test_failed_type_ends_the_mode(conv, tb):
    tb.d.use(1)
    say(conv, "type")
    tb.d.password = True
    out = say(conv, "secret")
    assert "password box" in out and out.endswith("Typing mode is off.") and conv.typing is None


def test_kill_switch_ends_the_mode(conv, tb):
    tb.d.use(1)
    say(conv, "type")
    tb.gate.killed.set()
    try:
        assert say(conv, "more words").endswith("Typing mode is off.")
        assert conv.typing is None
    finally:
        tb.gate.killed.clear()


def test_spidey_is_told(conv, tb):
    tb.d.use(1)
    say(conv, "type")
    say(conv, "hi")
    say(conv, "end")
    kinds = [(e["on"], e["count"]) for e in conv.events if e["type"] == "typing"]
    assert kinds == [(True, 0), (True, 1), (False, 0)]
    assert all(e.get("where") in ("Notepad", "") for e in conv.events if e["type"] == "typing")


def test_dictated_words_never_reach_the_model_thread(conv, tb):
    tb.d.use(1)
    say(conv, "type")
    say(conv, "my secret plan")
    assert not any("secret plan" in m["content"] for m in conv.history)


def test_only_on_the_laptop(settings, tb):
    phone = Conversation(settings, FakeLLM(), tb, channel="phone")
    tb.d.use(1)
    assert "laptop" in say(phone, "type") and phone.typing is None


def _char_desktop(tmp_path, monkeypatch, answer=1):
    """A real Desktop on the fake Windows, whose focused box is a classic text box (WM_CHAR path)."""
    from jarvis.audit import Audit
    from jarvis.gate import Gate
    from gate_fakes import make_desktop
    gate = Gate(None, Audit(tmp_path / "a.log"))
    d, u = make_desktop(monkeypatch, gate, {1: ("notepad.exe", "Untitled - Notepad")})
    monkeypatch.setattr(d, "_char_box", lambda h: 77)
    u.chars = []

    def send(box, msg, code, lparam, flags, timeout, ref):
        u.chars.append((getattr(box, "value", box), msg, code))
        if u.on_char:
            u.on_char(len(u.chars))
        return answer
    u.on_char = None
    u.SendMessageTimeoutW = send
    return d, u, gate


def test_classic_text_boxes_get_exact_characters(tmp_path, monkeypatch):
    d, u, _ = _char_desktop(tmp_path, monkeypatch)
    assert d._send_unicode("Hi, (A+B)!\n🙂", 1) == 12
    assert all(box == 77 and msg == 0x0102 for box, msg, _ in u.chars) and not u.sent("send")
    codes = [c for _, _, c in u.chars]
    assert "".join(map(chr, codes[:10])) == "Hi, (A+B)!" and codes[10] == 13          # a new line is Enter
    assert codes[11:] == [0xD83D, 0xDE42]                                           # the emoji, as two halves


def test_kill_switch_stops_char_typing_at_once(tmp_path, monkeypatch):
    d, u, gate = _char_desktop(tmp_path, monkeypatch)
    u.on_char = lambda n: gate.kill() if n == 3 else None
    try:
        assert d._send_unicode("x" * 50, 1) == 3
    finally:
        gate.resume()


def test_a_hung_app_stops_char_typing(tmp_path, monkeypatch):
    d, u, _ = _char_desktop(tmp_path, monkeypatch, answer=0)
    assert d._send_unicode("hello", 1) == 0 and len(u.chars) == 1


def test_other_apps_still_get_key_presses(tmp_path, monkeypatch):
    d, u, _ = _char_desktop(tmp_path, monkeypatch)
    monkeypatch.setattr(d, "_char_box", lambda h: None)
    assert d._send_unicode("hello", 1) == 5 and u.sent("send") and not u.chars


def test_the_interpreter_cannot_start_it(settings):
    from jarvis.conversation import grounded  # noqa: F401  (the guard is in Conversation: typing_mode is excluded)
    import inspect
    from jarvis import conversation
    assert '"typing_mode") for s in again' in inspect.getsource(conversation.Conversation._reply)
