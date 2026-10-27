"""Confirmations that can't be tricked: a whole-sentence yes for anything risky, a 30 second memory, and a re-check that
the same window and button are still there when the yes arrives."""
import asyncio
import time

import pytest

from conftest import EnergyVAD, FakeLLM, FakeSTT, FakeTTS
from jarvis.audit import Audit
from jarvis.conversation import STRICT_YES, YES, Conversation
from jarvis.gate import Gate
from jarvis.tools import Reminders, ToolBox, ToolResult

from test_conv_iterations import FakeEdge, say
from test_desktop_control import FakeDesktop


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def box(settings, tmp_path):
    settings.update({"browser": "edge", "save_conversations": False})
    b = ToolBox(settings, Reminders(tmp_path / "r.json"), launcher=lambda a: None, browser=lambda u: None,
                opener=lambda p: None, runner=lambda a: "ok", edge=FakeEdge(), desktop=FakeDesktop(),
                gate=Gate(None, Audit(tmp_path / "a.log")))
    b.d = b.desktop
    return b


@pytest.fixture
def conv(settings, box):
    c = Conversation(settings, FakeLLM(), box)
    c.clock = Clock()
    return c


def ask_to_send(conv, box):
    box.d.use(5)                                               # WhatsApp
    out = say(conv, "click send")
    assert "That will press “Send” in WhatsApp. Go ahead?" in out, out
    assert conv.pending.risk == "high" and conv.pending.fingerprint == (5, "whatsapp.exe", "WhatsApp", "Send")
    return out


def clicks(box):
    return [e for e in box.d.log if e[0] == "click"]


# ---------- the yes itself ----------
@pytest.mark.parametrize("yes", ["yes", "Yes.", "yeah", "yep", "yup", "confirm", "do it", "go ahead", "please do",
                                 "send it", "post it", "press it", "  YES!  "])
def test_whole_utterance_yes_confirms_a_send(conv, box, yes):
    ask_to_send(conv, box)
    say(conv, yes)
    assert clicks(box) == [("click", 5, "Send")]


@pytest.mark.parametrize("not_yes", ["ok", "okay", "sure", "right, so anyway", "yes but not that one", "yes please do it now",
                                     "that one", "correct", "open it", "uh huh"])
def test_loose_words_never_confirm_a_risky_action(conv, box, not_yes):
    ask_to_send(conv, box)
    say(conv, not_yes)
    assert clicks(box) == [] and conv.pending is None          # and the question is gone: ask again if you mean it


def test_low_risk_offers_still_take_a_loose_yes(conv):
    conv.pending = ToolResult("x", True, "Open it?", lambda: ToolResult("Opened it."))
    assert say(conv, "okay") == "Opened it."
    assert YES.search("sure") and not STRICT_YES.match("sure")


def test_no_cancels_a_risky_action(conv, box):
    ask_to_send(conv, box)
    assert say(conv, "no") == "Okay, I won't." and clicks(box) == []


def test_one_yes_never_answers_a_second_risky_question(conv):
    ran = []
    second = ToolResult("again", True, "Really send it?", lambda: ran.append(1) or ToolResult("Sent."), risk="high")
    conv.pending = ToolResult("offer", True, "Want me to send it?", lambda: second)
    assert "Really send it?" in say(conv, "yes") and ran == []
    assert say(conv, "yes") == "Sent." and ran == [1]


# ---------- 30 seconds ----------
def test_the_question_expires_after_thirty_seconds(conv, box):
    ask_to_send(conv, box)
    conv.clock.t += 31
    assert "timed out, so I didn't do it" in say(conv, "yes")
    assert clicks(box) == [] and conv.pending is None


def test_a_yes_inside_thirty_seconds_still_works(conv, box):
    ask_to_send(conv, box)
    conv.clock.t += 29
    say(conv, "yes")
    assert clicks(box)


def test_an_expired_question_does_not_swallow_a_new_command(conv, box):
    ask_to_send(conv, box)
    conv.clock.t += 60
    out = say(conv, "what time is it")
    assert "timed out" not in out and conv.pending is None and clicks(box) == []


# ---------- the screen moved on ----------
def test_window_title_change_refuses_with_zero_clicks(conv, box):
    ask_to_send(conv, box)
    box.d.windows[5] = ("whatsapp.exe", "WhatsApp - a different chat")
    assert "That window changed since I asked, so I didn't press it." in say(conv, "yes")
    assert clicks(box) == []


def test_a_different_program_in_the_same_window_refuses(conv, box):
    ask_to_send(conv, box)
    box.d.windows[5] = ("cmd.exe", "WhatsApp")
    assert "window changed" in say(conv, "yes") and clicks(box) == []


def test_a_verify_that_crashes_refuses(conv, box):
    box.d.use(5)
    say(conv, "click send")
    conv.pending.verify = lambda: 1 / 0
    assert "window changed" in say(conv, "yes") and clicks(box) == []


def test_new_command_clears_the_question(conv, box):
    ask_to_send(conv, box)
    say(conv, "what time is it")
    assert conv.pending is None
    say(conv, "yes")
    assert clicks(box) == []


# ---------- the prompts say what and where ----------
def test_widened_words_ask_in_a_desktop_app(conv, box):
    box.d.use(1)
    out = say(conv, "click save")
    assert "That will press “Save” in Notepad. Go ahead?" in out and clicks(box) == []


def test_enter_after_typing_asks_with_app_and_first_40_characters(conv, box):
    box.d.use(1)
    long = "a" * 100
    out = say(conv, f"type {long} in notepad and press enter")
    assert f"“{'a' * 40}”" in out and "a" * 41 not in out and "Notepad" in out, out
    assert ("press", 1, "enter") not in box.d.log
    say(conv, "yes")
    assert ("press", 1, "enter") in box.d.log


def test_chat_app_send_prompt_names_the_app(conv, box):
    box.d.use(5)
    out = say(conv, "type hello there in whatsapp and send it")
    assert "typed “hello there” in WhatsApp" in out and conv.pending.risk == "high"
    say(conv, "okay")
    assert ("press", 5, "enter") not in box.d.log


def test_the_typed_yes_button_always_works(conv, box):
    ask_to_send(conv, box)
    say(conv, "yes")                                           # /api/confirm/yes sends exactly this text
    assert clicks(box)


# ---------- hands-free ----------
def test_hands_free_risky_yes_without_the_wake_word_is_ignored_once(settings, box, monkeypatch):
    from jarvis.engine import Engine
    from jarvis.testing import ScriptedMic, SilentSpeaker
    import numpy as np
    settings.update({"mode": "hands_free", "activation": "wake_word", "wake_engine": False})
    eng = Engine(settings, EnergyVAD(), FakeSTT(["yes", "yes"]), FakeLLM(), FakeTTS(), box,
                 mic_factory=ScriptedMic, speaker_factory=SilentSpeaker)
    eng.speaker = SilentSpeaker(16000, None)
    started = []
    monkeypatch.setattr(eng, "_start_turn", lambda *a: started.append(a[0]))
    monkeypatch.setattr(eng, "_media_playing", lambda: False)
    eng.conversation.pending = ToolResult("x", True, "Send it?", lambda: ToolResult("Sent."), risk="high")
    eng._followup_until = time.monotonic() + 60
    pcm = np.zeros(16000, dtype=np.int16)

    async def go():
        await eng._handle_utterance_inner(pcm, time.perf_counter(), True)
        assert started == []                                   # ignored: could be a TV
        await eng._handle_utterance_inner(pcm, time.perf_counter(), True)
        assert started == ["yes"]                              # said again: taken
    asyncio.run(go())
