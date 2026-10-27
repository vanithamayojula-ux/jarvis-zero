"""Integration tests against the real local models. Run with network-blocking env vars to prove offline use."""
import asyncio
import os

import numpy as np
import pytest

from jarvis.testing import load_wav16k
from conftest import ROOT

pytestmark = pytest.mark.slow
AUDIO = ROOT / "tests" / "audio"


@pytest.fixture(scope="module", autouse=True)
def offline():
    os.environ["HF_HUB_OFFLINE"] = "1"          # Whisper must load from the local cache
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    yield


def test_vad_detects_speech():
    from jarvis.components import SileroVAD
    vad, pcm = SileroVAD(), load_wav16k(AUDIO / "wake_time.wav")
    probs = [vad.prob(pcm[i:i + 512]) for i in range(0, len(pcm) - 511, 512)]
    assert sum(p > 0.5 for p in probs) > 10
    vad.reset()
    assert max(vad.prob(np.zeros(512, np.int16)) for _ in range(20)) < 0.2


def test_whisper_offline():
    from jarvis.components import WhisperSTT
    tr = WhisperSTT("base.en").transcribe(load_wav16k(AUDIO / "wake_time.wav"))
    assert "time" in tr.text.lower() and tr.confidence > 0.3


def test_piper_offline():
    from jarvis.components import PiperTTS
    tts = PiperTTS("en_GB-alan-medium")
    clip = tts.synthesize("Testing one two three.")
    assert len(clip) / tts.sample_rate > 0.8
    fast = PiperTTS("en_GB-alan-medium", rate=1.5).synthesize("Testing one two three.")
    assert len(fast) < len(clip)


def test_default_voice_offline():
    from jarvis.components import PiperTTS, make_tts
    from jarvis.config import DEFAULTS
    tts = make_tts(DEFAULTS["tts_voice"], DEFAULTS["speaking_rate"])
    assert isinstance(tts, PiperTTS) and tts.voice_name == DEFAULTS["tts_voice"]   # real file, not a fallback
    clip = tts.synthesize("It is 32°C & sunny, with wind at 10 km/h.")
    assert len(clip) / tts.sample_rate > 1.5


def test_kokoro_offline():
    import time

    from jarvis.components import KokoroTTS, kokoro_voices
    if not kokoro_voices():
        pytest.skip("Kokoro model files missing (run install.ps1)")
    tts = KokoroTTS("kokoro:am_michael")
    assert tts.sample_rate == 24000
    t = time.perf_counter()
    clip = tts.synthesize("Good evening, the weather looks fine.")
    took = time.perf_counter() - t
    assert clip.dtype == np.int16 and len(clip) / tts.sample_rate > 1.0
    assert np.abs(clip.astype(np.int32)).max() > 1000      # real audio, not silence
    assert took < 5.0                                       # generous: CPU may be shared with the LLM
    tts.set_rate(1.5)
    assert len(tts.synthesize("Good evening, the weather looks fine.")) < len(clip)
    assert len(tts.synthesize("🙂 ** `` —")) == 0  # nothing speakable (a bare link is now said as its site name)


def test_make_tts_falls_back_to_piper(monkeypatch, tmp_path):
    from jarvis import components
    monkeypatch.setattr(components, "KOKORO_DIR", tmp_path)   # no Kokoro files here
    tts = components.make_tts("kokoro:am_michael")
    assert isinstance(tts, components.PiperTTS)
    assert len(tts.synthesize("Fallback voice works.")) > 0


def test_ollama_local_streaming():
    from jarvis.components import OllamaLLM
    llm = OllamaLLM("http://127.0.0.1:11434", "llama3.2:3b")

    async def go():
        if not await llm.health():
            pytest.skip("Ollama not running or model missing")
        text = ""
        try:
            async for chunk in llm.stream([{"role": "user", "content": "Say the word ready."}]):
                text += chunk.text
        finally:
            await llm.unload()                  # don't leave 2.3 GB in RAM after a test run
        return text

    assert "ready" in asyncio.run(go()).lower()
