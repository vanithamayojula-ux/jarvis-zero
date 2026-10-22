"""Microphone capture and interruptible speaker playback (sounddevice / PortAudio)."""
from __future__ import annotations

import collections
import threading
import time
from typing import Callable

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16000
FRAME = 512  # 32 ms at 16 kHz — matches Silero VAD


def list_devices() -> dict:
    devices = sd.query_devices()
    host_apis = sd.query_hostapis()
    mme = next((i for i, h in enumerate(host_apis) if "MME" in h["name"]), None)
    pick = lambda kind: [
        {"index": i, "name": d["name"]}
        for i, d in enumerate(devices)
        if d[f"max_{kind}_channels"] > 0 and (mme is None or d["hostapi"] == mme)
    ]
    default_in, default_out = sd.default.device
    return {"inputs": pick("input"), "outputs": pick("output"), "default_input": default_in, "default_output": default_out}


class Microphone:
    """Delivers int16 mono 16 kHz frames of 512 samples to a callback. Restarts itself after device errors."""

    WATCH_INTERVAL, STALL_S, RETRY_S = 1.0, 3.0, 2.0

    def __init__(self, on_frame: Callable[[np.ndarray], None], device: int | None = None,
                 on_error: Callable[[str], None] | None = None):
        self.on_frame = on_frame
        self.on_error = on_error or (lambda msg: None)
        self.device = device
        self.stream: sd.InputStream | None = None
        self.level = 0.0
        self._want = False
        self._watchdog: threading.Thread | None = None
        self._last_frame = 0.0
        self.on_recovered: Callable[[str], None] = lambda msg: None  # set by the engine: "mic is back"
        self.reconnects = 0
        self._last_report = 0.0
        self._failing = False
        self._frames_since_open = 0

    def _callback(self, indata, frames, time_info, status):
        frame = indata[:, 0].copy()
        self.level = float(np.sqrt(np.mean((frame.astype(np.float32) / 32768.0) ** 2)))
        self._last_frame = time.monotonic()
        self._frames_since_open += 1
        self.on_frame(frame)

    def _open(self) -> None:
        self.stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=FRAME,
                                     device=self.device, callback=self._callback)
        self._frames_since_open = 0
        self.stream.start()
        self._last_frame = time.monotonic()

    def start(self) -> None:
        self._want = True
        self._open()
        if not self._watchdog or not self._watchdog.is_alive():
            self._watchdog = threading.Thread(target=self._watch, daemon=True)
            self._watchdog.start()

    def _watch(self) -> None:
        # If the device disappears or stalls (unplugged headset, driver hiccup), reopen it.
        # Reports the problem once (then at most every 30 s while it lasts) and says when the mic is back,
        # instead of flooding the dashboard with an error every 3 seconds.
        while self._want:
            time.sleep(self.WATCH_INTERVAL)
            now = time.monotonic()
            if self._failing and self._frames_since_open > 15 and now - self._last_frame < 1.0:  # real audio again
                self._failing = False
                self.on_recovered("Microphone reconnected.")
            stalled = now - self._last_frame > self.STALL_S
            if self._want and (stalled or not (self.stream and self.stream.active)):
                self.level = 0.0
                report = not self._failing or now - self._last_report > 30
                if report:
                    self.on_error("Microphone stopped responding, reconnecting…")
                    self._last_report = now
                self._failing = True
                self.reconnects += 1
                try:
                    self._close()
                    self._open()
                except Exception as exc:
                    if report:
                        self.on_error(f"Microphone unavailable: {exc}")
                    time.sleep(self.RETRY_S)

    def _close(self) -> None:
        if self.stream:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None

    def stop(self) -> None:
        self._want = False
        self._close()
        self.level = 0.0


class Speaker:
    """Plays queued int16 clips; stop() cuts playback instantly (for barge-in)."""

    def __init__(self, sample_rate: int, device: int | None = None):
        self.sample_rate = sample_rate
        self.device = device
        self._buf: collections.deque[np.ndarray] = collections.deque()
        self._cur = np.zeros(0, np.int16)
        self._lock = threading.Lock()
        self.level = 0.0                     # recent output loudness (for the echo guard)
        self.clips_started = 0               # how many queued clips have begun playing
        self._idle_event = threading.Event()
        self._idle_event.set()
        self.stream = sd.OutputStream(samplerate=sample_rate, channels=1, dtype="int16", blocksize=1024,
                                      device=device, callback=self._callback)
        self.stream.start()

    def _callback(self, outdata, frames, time_info, status):
        out = np.zeros(frames, np.int16)
        filled = 0
        with self._lock:
            while filled < frames:
                if not len(self._cur):
                    if not self._buf:
                        break
                    self._cur = self._buf.popleft()
                    self.clips_started += 1
                take = min(frames - filled, len(self._cur))
                out[filled:filled + take] = self._cur[:take]
                self._cur = self._cur[take:]
                filled += take
            playing = filled > 0 or bool(self._buf) or bool(len(self._cur))
        outdata[:, 0] = out
        self.level = float(np.sqrt(np.mean((out.astype(np.float32) / 32768.0) ** 2))) if filled else self.level * 0.6
        if playing:
            self._idle_event.clear()
        else:
            self._idle_event.set()

    def play(self, clip: np.ndarray) -> None:
        with self._lock:
            self._buf.append(clip.astype(np.int16))
        self._idle_event.clear()

    @property
    def busy(self) -> bool:
        return not self._idle_event.is_set()

    def stop(self) -> None:
        with self._lock:
            self._buf.clear()
            self._cur = np.zeros(0, np.int16)
        self.level = 0.0
        self._idle_event.set()

    def wait_idle(self, timeout: float | None = None) -> bool:
        return self._idle_event.wait(timeout)

    def close(self) -> None:
        self.stop()
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass
