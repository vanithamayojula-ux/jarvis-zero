"""Test doubles: a microphone that 'speaks' WAV files in real time, and a silent speaker."""
from __future__ import annotations

import threading
import time
import wave
from pathlib import Path

import numpy as np

from .audio import FRAME


def load_wav16k(path: str | Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2, "need 16 kHz mono int16"
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


class ScriptedMic:
    """Feeds a script of [("say", pcm) | ("silence", seconds)] as 32 ms frames at real-time speed."""

    script: list = []

    def __init__(self, on_frame, device=None, on_error=None):
        self.on_frame = on_frame
        self.level = 0.0
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        rng = np.random.default_rng(0)
        next_t = time.perf_counter()
        for kind, value in list(self.script):
            pcm = value if kind == "say" else np.zeros(int(16000 * value), np.int16)
            for i in range(0, len(pcm) - FRAME + 1, FRAME):
                if self._stop.is_set():
                    return
                frame = pcm[i:i + FRAME].astype(np.int32) + rng.integers(-60, 60, FRAME)  # room noise
                frame = np.clip(frame, -32768, 32767).astype(np.int16)
                self.level = float(np.sqrt(np.mean((frame / 32768.0) ** 2)))
                self.on_frame(frame)
                next_t += FRAME / 16000
                time.sleep(max(0.0, next_t - time.perf_counter()))
        while not self._stop.wait(FRAME / 16000):
            self.on_frame(np.zeros(FRAME, np.int16))

    def stop(self):
        self._stop.set()


class SilentSpeaker:
    """Speaker stand-in that 'plays' clips in real time without making sound."""

    def __init__(self, sample_rate: int, device=None):
        self.sample_rate = sample_rate
        self.level = 0.0
        self.clips_started = 0
        self.played: list[np.ndarray] = []
        self._until = 0.0
        self._lock = threading.Lock()

    def play(self, clip):
        with self._lock:
            start = max(time.monotonic(), self._until)
            self._until = start + len(clip) / self.sample_rate
            self.clips_started += 1
            self.played.append(clip)
            self.level = 0.05

    @property
    def busy(self) -> bool:
        return time.monotonic() < self._until

    def stop(self):
        with self._lock:
            self._until = 0.0
            self.level = 0.0

    def wait_idle(self, timeout=None) -> bool:
        end = time.monotonic() + (timeout or 1e9)
        while self.busy and time.monotonic() < end:
            time.sleep(0.02)
        return not self.busy

    def close(self):
        self.stop()
