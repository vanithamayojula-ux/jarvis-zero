"""The kill switch: a latching stop for every click and key, from a hotkey, the API or the breaker; lifted only by the
person saying resume."""
import asyncio
import ctypes
import json
import threading
import time
import types

import pytest
from fastapi.testclient import TestClient

from conftest import EnergyVAD, FakeLLM, FakeSTT, FakeTTS
from gate_fakes import FakeCtrl, make_desktop
from jarvis import hotkey, server
from jarvis.audit import Audit
from jarvis.conversation import Conversation, route_all
from jarvis.desktop import DesktopError
from jarvis.gate import ABORT, Action, Gate
from jarvis.testing import ScriptedMic, SilentSpeaker
from jarvis.tools import Reminders, ToolBox, ToolResult
from test_browser_agent import FORM, FormEdge, PlanningLLM
from test_conv_iterations import say


# ---------- the chord ----------
def test_kill_chord_parses_with_the_new_keys():
    assert hotkey.parse("ctrl+alt+backspace") == (hotkey.MOD_CONTROL | hotkey.MOD_ALT, 0x08)
    assert hotkey.parse("ctrl+alt+pause") == (hotkey.MOD_CONTROL | hotkey.MOD_ALT, 0x13)
    assert hotkey.parse("ctrl+shift+esc")[1] == 0x1B
    assert hotkey.parse("ctrl+alt+space")[1] == 0x20                       # the old keys still work


@pytest.mark.parametrize("bad", ["backspace", "esc", "pause", "space", "ctrl+alt", "ctrl+alt+f13x"])
def test_a_chord_without_a_modifier_or_key_is_rejected(bad):
    with pytest.raises(ValueError):
        hotkey.parse(bad)


class FakeWinApi:
    """ctypes.windll stand-in: only RegisterHotKey / GetMessageW / UnregisterHotKey exist (no keyboard hook)."""

    def __init__(self, taken=(), fire=0):
        self.taken, self.registered, self.fire = set(taken), [], fire
        self.threads = []
        self.user32 = types.SimpleNamespace(RegisterHotKey=self._register, GetMessageW=self._get,
                                            UnregisterHotKey=lambda *a: 1, PostThreadMessageW=lambda *a: 1)
        self.kernel32 = types.SimpleNamespace(GetCurrentThreadId=lambda: 77)

    def _register(self, hwnd, hid, mods, key):
        if (mods & ~hotkey.MOD_NOREPEAT, key) in self.taken:
            return 0
        self.registered.append((hid, mods & ~hotkey.MOD_NOREPEAT, key))
        return 1

    def _get(self, ref, *a):
        if self.fire:
            self.fire -= 1
            ref._obj.message = hotkey.WM_HOTKEY
            self.threads.append(threading.current_thread().name)
            return 1
        return 0


def test_kill_switch_registers_hotkey_id_2_and_falls_back_when_taken(monkeypatch):
    mods = hotkey.MOD_CONTROL | hotkey.MOD_ALT
    api = FakeWinApi(taken={(mods, 0x08)})
    monkeypatch.setattr(ctypes, "windll", api, raising=False)
    ks = hotkey.KillSwitch("ctrl+alt+backspace", lambda: None).start()
    assert ks.active == "ctrl+alt+pause" and ks.fallback_used and "already used" in ks.error
    assert api.registered == [(2, mods, 0x13)]


def test_kill_switch_reports_when_every_chord_is_taken(monkeypatch):
    mods = hotkey.MOD_CONTROL | hotkey.MOD_ALT
    api = FakeWinApi(taken={(mods, 0x08), (mods, 0x13), (mods | hotkey.MOD_SHIFT, 0x08)})
    monkeypatch.setattr(ctypes, "windll", api, raising=False)
    ks = hotkey.KillSwitch("ctrl+alt+backspace", lambda: None).start()
    assert ks.active is None and ks.error


def test_the_chord_calls_back_on_its_own_thread(monkeypatch):
    api = FakeWinApi(fire=1)
    monkeypatch.setattr(ctypes, "windll", api, raising=False)
    hit = threading.Event()
    hotkey.KillSwitch("ctrl+alt+backspace", hit.set).start()
    assert hit.wait(2) and api.threads == ["jarvis-killswitch"]


def test_kill_switch_uses_register_hotkey_only_no_keyboard_hook():
    src = hotkey.KillSwitch.__mro__
    assert hotkey.GlobalHotkey in src and hotkey.KeyTap not in src         # RegisterHotKey, never the low-level hook


# ---------- typing stops within one chunk ----------
WINDOWS = {1: ("notepad.exe", "Untitled - Notepad")}


def test_typing_200_characters_stops_within_one_chunk(tmp_path, monkeypatch):
    gate = Gate(None, Audit(tmp_path / "a.log"))
    d, u = make_desktop(monkeypatch, gate, WINDOWS, focused=FakeCtrl(name="Text editor", ctype="DocumentControl"))
    u.on_send = lambda n: gate.kill() if n == 1 else None                  # the chord is pressed during the first chunk
    with pytest.raises(DesktopError, match="Stopped after typing 8 of 200 characters."):
        d.type_text(1, "x" * 200)
    assert len(u.sent("send")) == 1


def test_abort_flag_alone_also_stops_typing(tmp_path, monkeypatch):
    gate = Gate(None, Audit(tmp_path / "a.log"))
    d, u = make_desktop(monkeypatch, gate, WINDOWS)
    u.on_send = lambda n: ABORT.set() if n == 2 else None
    assert d._send_unicode("y" * 100) == 16
    assert len(u.sent("send")) == 2


def test_abort_stops_window_searches_and_control_searches(tmp_path, monkeypatch):
    gate = Gate(None, Audit(tmp_path / "a.log"))
    d, u = make_desktop(monkeypatch, gate, WINDOWS)
    ABORT.set()
    assert d.find_app_window("notepad", wait=5) is None                    # returns at once, EnumWindows never runs
    import uiautomation as auto
    auto.WalkControl = lambda c, maxDepth=0: iter([(FakeCtrl(name="Save", ctype="ButtonControl"), 1)])
    assert d.find(1, "save") is None


# ---------- the whole app ----------
@pytest.fixture
def client(settings):
    ScriptedMic.script = []
    server.S.no_hotkey = True
    server.build(settings, mic_factory=ScriptedMic, speaker_factory=SilentSpeaker, llm=FakeLLM(), stt=FakeSTT(),
                 tts=FakeTTS(), vad_factory=EnergyVAD)
    with TestClient(server.app) as c:
        yield c
    server.S.engine = None


class Edge:
    def __init__(self):
        self.log = []

    def auto_scroll(self, on, speed="normal"):
        self.log.append(("scroll", on))

    def banner(self, text):
        self.log.append(("banner", text))


def test_engine_kill_latches_clears_the_question_and_tells_everyone(client):
    eng = server.S.engine
    eng.tools.edge = Edge()
    ran = []
    eng.conversation.pending = ToolResult("x", True, "Send it?", lambda: ran.append(1) or ToolResult("Sent."), risk="high")
    q = eng.subscribe()
    eng.kill()
    assert eng.tools.gate.killed.is_set() and ABORT.is_set() and eng.conversation.pending is None
    assert ("scroll", False) in eng.tools.edge.log and ("banner", None) in eng.tools.edge.log
    seen = []
    for _ in range(100):                                                   # the event hops to the app's loop thread
        seen += [q.get_nowait() for _ in range(q.qsize())]
        if {"type": "kill"} in seen:
            break
        time.sleep(0.02)
    assert {"type": "kill"} in seen
    say(eng.conversation, "yes")
    assert ran == []                                                       # "yes" after a kill does nothing
    assert eng.status()["killed"] is True


def test_emergency_stop_and_resume_over_the_api(client):
    assert client.post("/api/emergency-stop").status_code == 200
    assert client.get("/api/status").json()["killed"] is True
    assert client.post("/api/resume").json() == {"ok": True, "was_stopped": True}
    assert client.get("/api/status").json()["killed"] is False and not ABORT.is_set()


def test_a_new_turn_clears_abort_but_not_the_latch(client):
    eng = server.S.engine
    eng.kill()
    assert ABORT.is_set()
    assert client.post("/api/text", json={"text": "hello"}).status_code == 200
    assert not ABORT.is_set() and eng.tools.gate.killed.is_set()


# ---------- resume comes from the person only ----------
@pytest.fixture
def box(settings, tmp_path):
    settings.update({"save_conversations": False})
    return ToolBox(settings, Reminders(tmp_path / "r.json"), launcher=lambda a: None, browser=lambda u: None,
                   opener=lambda p: None, runner=lambda a: "ok", gate=Gate(None, Audit(tmp_path / "a.log")))


def test_resume_routes_only_for_the_plain_phrases(settings):
    for t in ("start again", "you can act again", "Jarvis, resume automation", "okay resume actions"):
        assert route_all(t, settings) == [("resume_actions", {})], t
    assert route_all("resume", settings)[0][0] != "resume_actions"         # a bare "resume" is still media play


def test_user_resume_lifts_the_latch(settings, box):
    conv = Conversation(settings, FakeLLM(), box)
    box.gate.kill()
    assert "I'm back" in say(conv, "resume") and not box.gate.killed.is_set()
    assert not ABORT.is_set()
    assert "I wasn't stopped" in say(conv, "you can act again")


def test_bare_resume_is_media_play_when_nothing_is_stopped(settings, box):
    conv = Conversation(settings, FakeLLM(), box)
    assert route_all("resume", settings)[0][0] == "browser_media"
    assert box.gate.killed.is_set() is False


def test_interpreter_output_can_never_resume(settings, box):
    class Interp(FakeLLM):
        async def cloud_only(self, messages, max_tokens=80):
            return "start again"                                           # what a tricked model might answer
    text = "start moving again please"
    assert route_all(text, settings, box.find_app) is None
    conv = Conversation(settings, Interp(), box)
    box.gate.kill()
    say(conv, text)
    assert box.gate.killed.is_set()


def test_a_phone_caller_cannot_resume(settings, box):
    conv = Conversation(settings, FakeLLM(), box, channel="phone")
    box.gate.kill()
    assert "only start acting again" in say(conv, "start again")
    assert box.gate.killed.is_set()


# ---------- the form agent ----------
class GatedEdge(FormEdge):
    """FormEdge whose act() asks the real gate first, like EdgeController.act does."""

    def __init__(self, gate):
        super().__init__()
        self.gate, self.after = gate, None

    def act(self, el_id, action, value=None):
        from jarvis.browser import BrowserError
        d = self.gate.authorize(Action("browser_fill" if action == "fill" else "browser_click", None, "msedge.exe", "", "f"))
        if d.verdict == "deny":
            raise BrowserError(d.say)
        out = super().act(el_id, action, value)
        if self.after:
            self.after(len(self.acts))
        return out


def agent_box(settings, tmp_path, edge, gate):
    settings.update({"browser": "edge", "save_conversations": False})
    return ToolBox(settings, Reminders(tmp_path / "r.json"), launcher=lambda a: None, browser=lambda u: None,
                   opener=lambda p: None, runner=lambda a: "ok", edge=edge, gate=gate)


def test_agent_actions_are_capped_at_six_per_round(settings, tmp_path):
    gate = Gate(None, Audit(tmp_path / "a.log"))
    edge = GatedEdge(gate)
    llm = PlanningLLM([{"actions": [{"do": "fill", "id": 1, "value": f"v{i}"} for i in range(8)], "status": "continue"}])
    conv = Conversation(settings, llm, agent_box(settings, tmp_path, edge, gate))
    say(conv, "fill this form with v0 v1 v2 v3 v4 v5 v6 v7")
    assert len(edge.acts) == 6


def test_agent_loop_stops_when_the_kill_switch_is_pressed(settings, tmp_path):
    gate = Gate(None, Audit(tmp_path / "a.log"))
    edge = GatedEdge(gate)
    edge.after = lambda n: gate.kill() if n == 2 else None
    llm = PlanningLLM([{"actions": [{"do": "fill", "id": 1, "value": "a"}, {"do": "fill", "id": 1, "value": "b"},
                                    {"do": "fill", "id": 1, "value": "c"}], "status": "continue"}] * 3)
    conv = Conversation(settings, llm, agent_box(settings, tmp_path, edge, gate))
    out = say(conv, "fill this form with a b c")
    assert len(edge.acts) == 2 and out.startswith("Stopped") and "resume" in out


def test_agent_breaker_pauses_it_on_the_seventh_quick_action(settings, tmp_path):
    gate = Gate(None, Audit(tmp_path / "a.log"))
    edge = GatedEdge(gate)
    plan = {"actions": [{"do": "fill", "id": 1, "value": "z"}] * 6, "status": "continue"}
    conv = Conversation(settings, PlanningLLM([plan, plan]), agent_box(settings, tmp_path, edge, gate))
    out = say(conv, "fill this form with z")
    assert len(edge.acts) == 6 and gate.killed.is_set() and "I paused myself, say resume to continue." in out
    say(conv, "start again")
    assert not gate.killed.is_set()


def test_the_final_press_asks_for_a_whole_yes_and_checks_the_page(settings, tmp_path):
    gate = Gate(None, Audit(tmp_path / "a.log"))
    edge = GatedEdge(gate)
    llm = PlanningLLM([{"actions": [{"do": "click", "id": 6}], "status": "continue"}])
    conv = Conversation(settings, llm, agent_box(settings, tmp_path, edge, gate))
    assert "Should I press “Sign up”?" in say(conv, "fill this form with my name Yash")
    assert conv.pending.risk == "high" and conv.pending.fingerprint[0] == "edge"
    edge.url = "https://evil.example/other"                                # the page changed since it asked
    assert "window changed" in say(conv, "yes") and not edge.acts
