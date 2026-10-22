"""Microphone watchdog: a stalled/unplugged mic is reconnected, reported once (no flood), and recovery is announced.
Uses a fake PortAudio stream: no real audio device is opened."""
import threading
import time

import numpy as np

from jarvis import audio


class FakeStream:
    opened = 0
    plan: list = []  # per open(): "stall" (delivers briefly then stops), "fail" (raises), "ok"

    def __init__(self, samplerate, channels, dtype, blocksize, device, callback):
        kind = FakeStream.plan.pop(0) if FakeStream.plan else "ok"
        if kind == "fail":
            raise RuntimeError("Error opening InputStream: Device unavailable [PaErrorCode -9985]")
        FakeStream.opened += 1
        self.kind, self.callback, self.active, self._stop = kind, callback, False, threading.Event()

    def start(self):
        self.active = True

        def run():
            n = 0
            while not self._stop.wait(0.032):
                if self.kind == "stall" and n >= 10:
                    continue  # driver hiccup: stream "active" but no more callbacks
                self.callback(np.full((512, 1), 200, np.int16), 512, None, None)
                n += 1
        threading.Thread(target=run, daemon=True).start()

    def stop(self):
        self._stop.set()

    def close(self):
        self.active = False
        self._stop.set()


class FastMic(audio.Microphone):
    WATCH_INTERVAL, STALL_S, RETRY_S = 0.1, 0.4, 0.1


def test_mic_watchdog_reconnects_and_reports_once(monkeypatch):
    monkeypatch.setattr(audio.sd, "InputStream", FakeStream)
    FakeStream.opened, FakeStream.plan = 0, ["stall", "fail", "fail", "fail", "ok"]
    frames, errors, recovered = [], [], []
    mic = FastMic(frames.append, on_error=errors.append)
    mic.on_recovered = recovered.append
    mic.start()
    try:
        deadline = time.monotonic() + 5
        while not recovered and time.monotonic() < deadline:
            time.sleep(0.05)
        n = len(frames)
        time.sleep(0.3)
        assert len(frames) > n, "frames flow again after the reconnect"
    finally:
        mic.stop()
    assert recovered == ["Microphone reconnected."]
    assert errors.count("Microphone stopped responding, reconnecting…") == 1, errors   # not once per retry
    assert sum("unavailable" in e for e in errors) == 1, errors
    assert mic.reconnects >= 4 and FakeStream.opened == 2
