"""Smart Turn: a pause that sounds unfinished doesn't end the turn; a finished one ends it as fast as before."""
import asyncio

import numpy as np

from conftest import FakeSTT, tone
from jarvis.cloud import CloudTTS, _wav_pcm
from test_av_engine import _collect, _engine, _users
from jarvis.testing import ScriptedMic


class FakeTurn:
    """Says 'not finished' while the audio is shorter than `done_after` seconds."""

    def __init__(self, done_after):
        self.done_after, self.calls = done_after, 0

    def prob(self, pcm):
        self.calls += 1
        return 0.9 if len(pcm) / 16000 >= self.done_after else 0.1


def run(settings, script, stt_texts, turn):
    settings.update({"mode": "hands_free", "activation": "voice", "silence_ms": 300, "smart_turn_max_ms": 2500})

    async def go():
        ScriptedMic.script = script
        eng = _engine(settings, FakeSTT(stt_texts))
        eng.turn_model = turn
        await eng.start()
        q = eng.subscribe()
        eng.start_listening()
        ev = await _collect(eng, q, 4.5)
        await eng.shutdown()
        return ev
    return asyncio.run(go())


def test_mid_sentence_pause_keeps_listening(settings):
    # "open WhatsApp and message" <0.9 s pause> "Rahul that I'm late": one request, not two
    script = [("silence", 0.3), ("say", tone(0.6)), ("silence", 0.9), ("say", tone(0.6)), ("silence", 2.5)]
    ev = run(settings, script, ["open whatsapp and message rahul that i'm late"], FakeTurn(done_after=1.8))
    assert _users(ev) == ["open whatsapp and message rahul that i'm late"]


def test_finished_sentence_ends_at_the_usual_pause(settings):
    script = [("silence", 0.3), ("say", tone(0.6)), ("silence", 2.5)]
    turn = FakeTurn(done_after=0.0)
    ev = run(settings, script, ["what's the time"], turn)
    assert _users(ev) == ["what's the time"] and turn.calls == 1


def test_never_waits_forever(settings):
    script = [("silence", 0.3), ("say", tone(0.6)), ("silence", 4)]
    ev = run(settings, script, ["so I was thinking"], FakeTurn(done_after=99))     # always "not finished"
    assert _users(ev) == ["so I was thinking"]                                     # answered after the 2.5 s cap


def test_without_the_model_nothing_changes(settings):
    script = [("silence", 0.3), ("say", tone(0.6)), ("silence", 0.9), ("say", tone(0.6)), ("silence", 2)]
    ev = run(settings, script, ["open whatsapp and message", "rahul that i'm late"], None)
    assert _users(ev)[:1] == ["open whatsapp and message"]                         # the old split, as before


class LocalVoice:
    sample_rate, voice_name = 22050, "piper-test"

    def synthesize(self, text):
        return np.full(22050, 1000, np.int16)                                      # one second at 22.05 kHz


def test_cloud_voice_for_chat_local_for_commands():
    import httpx
    sent = []
    kind = {"now": "tool"}
    tts = CloudTTS(LocalVoice(), "gsk_test", "troy", kind=lambda: kind["now"])

    def groq(req):
        sent.append(req)
        wav = b"RIFF\xff\xff\xff\xffWAVEfmt " + b"\x10\x00\x00\x00\x01\x00\x01\x00\xc0]\x00\x00\x80\xbb\x00\x00\x02\x00\x10\x00" \
              + b"data\xff\xff\xff\xff" + np.full(24000, 7, np.int16).tobytes()
        return httpx.Response(200, content=wav)
    tts.client = httpx.Client(transport=httpx.MockTransport(groq))
    a = tts.synthesize("Opening Spotify.")
    assert not sent and len(a) == 24000 and tts.last_source == "local"            # resampled 22.05 → 24 kHz
    kind["now"] = "chat"
    b = tts.synthesize("Pretty good, thanks! What are you up to?")
    assert len(sent) == 1 and tts.last_source == "cloud" and len(b) == 24000 and b[0] == 7


def test_cloud_voice_falls_back_when_the_daily_limit_is_used():
    import httpx
    tts = CloudTTS(LocalVoice(), "gsk_test", "troy", kind=lambda: "chat")
    tts.client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429, headers={"retry-after": "600"})))
    assert len(tts.synthesize("Hello there, nice to meet you.")) == 24000 and tts.last_source == "local"
    assert not tts.breaker.ok()


def test_streamed_wav_header_is_handled():
    body = np.arange(10, dtype=np.int16).tobytes()
    assert list(_wav_pcm(b"RIFF\xff\xff\xff\xffWAVEfmt " + b"\x00" * 20 + b"data\xff\xff\xff\xff" + body)) == list(range(10))
    assert _wav_pcm(b"not a wav") is None


import pytest  # noqa: E402
from jarvis.engine import correct_names  # noqa: E402

NAMES = ["Hanumankind", "Run It Up", "Big Dawgs", "Arijit Singh", "Rahul", "Spotify", "Valorant"]


@pytest.mark.parametrize("heard, fixed", [
    ("Play Run It Up by Hany Mankind on Spotify.", "Play Run It Up by Hanumankind on Spotify."),
    ("Play run it up by Honey Mankind.", "Play run it up by Hanumankind."),
    ("open valorent", "open Valorant"),
    ("Search for arjit sing songs", "Search for Arijit Singh songs"),
    ("songs of humankind", "songs of Hanumankind"),
])
def test_names_are_corrected(heard, fixed):
    assert correct_names(heard, NAMES) == fixed


@pytest.mark.parametrize("text", ["run it by me again", "Anything, then run it.", "what is the weather like today",
                                  "big dog in the park", "spot the difference", "I'm valuing it a lot"])
def test_everyday_words_are_left_alone(text):
    assert correct_names(text, NAMES) == text
