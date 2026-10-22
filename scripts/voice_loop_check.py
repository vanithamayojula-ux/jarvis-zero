"""Phase 2 check: real VAD + Whisper + Ollama + Piper through the Engine, with recorded speech as the mic.

Usage: python scripts/voice_loop_check.py [--loud]   (--loud plays replies on the laptop speakers)
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
os.environ.setdefault("JARVIS_DATA_DIR", str(ROOT / "data" / "check"))

import psutil  # noqa: E402

from jarvis.audio import Speaker  # noqa: E402
from jarvis.components import OllamaLLM, SileroVAD, WhisperSTT, make_tts  # noqa: E402
from jarvis.config import Settings  # noqa: E402
from jarvis.engine import Engine  # noqa: E402
from jarvis.testing import ScriptedMic, SilentSpeaker, load_wav16k  # noqa: E402
from jarvis.tools import Reminders, ToolBox  # noqa: E402

AUDIO = ROOT / "tests" / "audio"


async def main() -> None:
    loud = "--loud" in sys.argv
    settings = Settings(Path(os.environ["JARVIS_DATA_DIR"]) / "settings.json")
    settings.update({"mode": "hands_free", "activation": "wake_word"})
    proc = psutil.Process()
    t = time.perf_counter()
    vad, stt = SileroVAD(), WhisperSTT(settings.get("stt_model"))
    llm = OllamaLLM(settings.get("ollama_url"), settings.get("llm_model"), settings.get("llm_keep_alive"))
    tts = make_tts(settings.get("tts_voice"), settings.get("speaking_rate"))
    await llm.warm()
    print(f"models loaded in {time.perf_counter() - t:.1f}s; backend RSS {proc.memory_info().rss / 1e6:.0f} MB")

    tools = ToolBox(settings, Reminders(Path(os.environ["JARVIS_DATA_DIR"]) / "reminders.json"),
                    launcher=lambda argv: print("   [would launch]", argv), browser=lambda u: print("   [would open]", u))
    ScriptedMic.script = [
        ("silence", 1.0), ("say", load_wav16k(AUDIO / "no_wake.wav")), ("silence", 2.5),
        ("say", load_wav16k(AUDIO / "wake_time.wav")), ("silence", 7.0),
        ("say", load_wav16k(AUDIO / "chat.wav")), ("silence", 9.0),
        ("say", load_wav16k(AUDIO / "followup.wav")), ("silence", 9.0),
        ("say", load_wav16k(AUDIO / "long_question.wav")), ("silence", 1.6),
        # barge-in: speak over the long answer (louder than playback)
        ("say", (load_wav16k(AUDIO / "wake_time.wav").astype("int32") * 3).clip(-32768, 32767).astype("int16")),
        ("silence", 8.0),
    ]
    engine = Engine(settings, vad, stt, llm, tts, tools, mic_factory=ScriptedMic,
                    speaker_factory=Speaker if loud else SilentSpeaker)
    await engine.start()
    q = engine.subscribe()
    engine.start_listening()
    t0 = time.perf_counter()
    peak = 0
    total = sum((len(v) / 16000 if k == "say" else v) for k, v in ScriptedMic.script)
    while time.perf_counter() - t0 < total + 3:
        try:
            ev = await asyncio.wait_for(q.get(), 0.5)
        except asyncio.TimeoutError:
            continue
        peak = max(peak, proc.memory_info().rss)
        stamp = f"[{time.perf_counter() - t0:5.1f}s]"
        if ev["type"] == "state":
            print(stamp, "state ->", ev["state"])
        elif ev["type"] in ("heard", "ignored", "transcript", "interrupted", "error"):
            print(stamp, ev["type"], {k: v for k, v in ev.items() if k != "type"})
        elif ev["type"] == "latency":
            print(stamp, "LATENCY", json.dumps({k: v for k, v in ev.items() if k != "type"}))
    print("\nmedian latency:", engine.latency_summary())
    print(f"peak backend RSS {peak / 1e6:.0f} MB")
    print("memory after barge-in, last assistant turn:", engine.conversation.history[-3:])
    await engine.shutdown()


asyncio.run(main())
