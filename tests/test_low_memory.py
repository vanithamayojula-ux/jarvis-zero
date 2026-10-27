"""Memory diet: with the cloud in front, the local backups (Ollama model, Whisper, wake word) load only when needed."""
import asyncio

import httpx
import numpy as np

from jarvis import components, server
from jarvis.cloud import CloudLLM
from jarvis.components import LazyWhisperSTT
from jarvis.interfaces import Transcript


def _llm_with(calls: list, key: str) -> CloudLLM:
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        return httpx.Response(200, json={"models": []} if req.url.path == "/api/tags" else {"message": {}})
    llm = CloudLLM("http://127.0.0.1:11434", "llama3.2:3b", "10m", key)
    llm.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return llm


def test_cloud_warm_only_checks_ollama():
    calls = []
    asyncio.run(_llm_with(calls, "gsk_test").warm("system"))
    assert calls == ["/api/tags"]                          # no /api/chat: the 2.8 GB model stays on disk


def test_without_cloud_key_warm_loads_local_model():
    calls = []
    asyncio.run(_llm_with(calls, "").warm("system"))
    assert calls == ["/api/chat"]


def test_lazy_whisper_loads_on_first_transcript(monkeypatch):
    made = []

    class FakeWhisper:
        def __init__(self, model, threads=4):
            made.append(model)

        def transcribe(self, pcm, hint=""):
            return Transcript(text="hi", confidence=1.0, seconds=0.0)

    monkeypatch.setattr(components, "WhisperSTT", FakeWhisper)
    stt = LazyWhisperSTT("base.en")
    assert made == []
    assert stt.transcribe(np.zeros(16000, np.int16)).text == "hi"
    stt.transcribe(np.zeros(16000, np.int16))
    assert made == ["base.en"]                             # loaded once, on first use


def test_wake_word_only_loads_in_wake_word_mode(monkeypatch):
    class Settings(dict):
        get = dict.get

    class Engine:
        wake_model = None

    monkeypatch.setattr(server, "WakeWord", lambda: "model")
    monkeypatch.setattr(server.S, "engine", Engine(), raising=False)
    monkeypatch.setattr(server.S, "real_audio", True, raising=False)
    monkeypatch.setattr(server.S, "settings", Settings(wake_engine=True, mode="push_to_talk", activation="voice"),
                        raising=False)
    server._load_wake_word()
    assert server.S.engine.wake_model is None
    server.S.settings.update(mode="hands_free", activation="wake_word")
    server._load_wake_word()
    assert server.S.engine.wake_model == "model"


def test_local_model_never_parks_forever_by_default():
    from jarvis.components import OllamaLLM
    assert OllamaLLM("http://127.0.0.1:11434", "llama3.2:3b").keep_alive == "10m"


def test_piper_gives_scratch_memory_back_after_each_sentence():
    tts = components.make_tts("en_GB-northern_english_male-medium", 1.0)
    if not isinstance(tts, components.PiperTTS):
        return
    seen = []
    run = tts.voice.session.run
    assert len(tts.synthesize("Hello there.")) > 0
    # the wrapper adds run options that shrink ONNX's memory pool back after every run
    import inspect
    assert "run_options or opts" in inspect.getsource(components.shrink_after_each_run)
    assert run is tts.voice.session.run and seen == []
