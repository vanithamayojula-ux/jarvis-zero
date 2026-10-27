import os
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
os.environ["JARVIS_DATA_DIR"] = tempfile.mkdtemp(prefix="jarvis-test-")
os.environ["JARVIS_API_TOKEN"] = ""   # blank, not removed: .env must not fill the real token into tests
os.environ["GROQ_API_KEY"] = ""   # tests never call the real cloud (and .env can't fill it back in)
for _k in ("TAVILY_API_KEY", "BRAVE_API_KEY", "SERPER_API_KEY"):
    os.environ[_k] = ""           # nor a real web search

from jarvis import config as _config  # noqa: E402
from jarvis.interfaces import LLMChunk, Transcript  # noqa: E402

_config.DEFAULTS["insecure_no_token"] = True   # the API's empty-token mode exists for the test suite only


class FakeLLM:
    """Streams scripted replies word by word."""

    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.calls: list[list[dict]] = []

    async def stream(self, messages, tools=None):
        self.calls.append(messages)
        text = self.replies.pop(0) if self.replies else "Sure. Happy to help with that."
        for word in text.split(" "):
            yield LLMChunk(text=word + " ")

    async def complete(self, messages):
        return "- Talked about testing."


class FakeSTT:
    def __init__(self, texts=None):
        self.texts = list(texts or [])

    def transcribe(self, pcm, hint=""):
        return Transcript(self.texts.pop(0) if self.texts else "", 0.9, 0.01)


class FakeTTS:
    sample_rate = 16000

    def synthesize(self, text):
        return (np.sin(np.arange(1600) / 5) * 3000).astype(np.int16)  # 0.1 s tone


class EnergyVAD:
    frame_samples = 512

    def prob(self, frame):
        return 1.0 if np.abs(frame.astype(np.int32)).mean() > 500 else 0.0

    def reset(self):
        pass


def tone(seconds: float, amp: int = 8000) -> np.ndarray:
    return (np.sin(np.arange(int(16000 * seconds)) / 3) * amp).astype(np.int16)


@pytest.fixture(autouse=True)
def _clear_abort():
    """The kill switch's abort flag is process-wide: no test may leak it into the next."""
    from jarvis.gate import ABORT
    ABORT.clear()
    yield
    ABORT.clear()


@pytest.fixture
def settings(tmp_path):
    from jarvis.config import Settings
    return Settings(tmp_path / "settings.json")


# CI: the downloaded voice/speech models (models/, whisper cache) are not in git, so on a fresh
# checkout (GitHub Actions) the tests that need the real files are skipped instead of failing.
_NEEDS_MODELS = {
    "test_local_models.py::test_whisper_offline",
    "test_local_models.py::test_piper_offline",
    "test_local_models.py::test_default_voice_offline",
    "test_local_models.py::test_make_tts_falls_back_to_piper",
    "test_low_memory.py::test_piper_gives_scratch_memory_back_after_each_sentence",
    "test_tts_text.py::test_available_voices_lists_both_engines",
}


def pytest_collection_modifyitems(items):
    if (ROOT / "models" / "piper" / "en_GB-alan-medium.onnx").exists():
        return
    skip = pytest.mark.skip(reason="local voice models not installed (run install.ps1)")
    for item in items:
        if item.nodeid.replace("tests/", "", 1) in _NEEDS_MODELS:
            item.add_marker(skip)
