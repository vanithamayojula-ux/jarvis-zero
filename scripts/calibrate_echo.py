"""Measure how much of JARVIS's own voice the microphone hears (speaker→mic echo coupling).

Plays a short sentence through the speakers while recording, then saves the ratio to data/settings.json.
Usage: python scripts/calibrate_echo.py [--no-save]
"""
import sys
from pathlib import Path

import numpy as np
import sounddevice as sd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from jarvis.components import make_tts  # noqa: E402
from jarvis.config import Settings  # noqa: E402


def main() -> None:
    settings = Settings()
    tts = make_tts(settings.get("tts_voice"))
    clip = tts.synthesize("Calibrating echo. This is how loud my voice sounds to your microphone.")
    pad = np.zeros(int(tts.sample_rate * 0.6), np.int16)
    play = np.concatenate([pad, clip, pad])
    seconds = len(play) / tts.sample_rate
    # Separate output and input streams: full-duplex on one MME stream records silence on some drivers.
    chunks: list[np.ndarray] = []
    with sd.InputStream(samplerate=16000, channels=1, dtype="float32", blocksize=512,
                        device=settings.get("input_device"), callback=lambda d, f, t, st: chunks.append(d[:, 0].copy())):
        sd.play(play, tts.sample_rate, device=settings.get("output_device"))
        sd.wait()
        sd.sleep(300)
    mic = np.concatenate(chunks)
    # Express playback at 16 kHz to compare frame by frame.
    idx = np.linspace(0, len(play) - 1, int(len(play) * 16000 / tts.sample_rate)).astype(int)
    out = play[idx].astype(np.float32) / 32768.0
    lag = int(np.argmax(np.correlate(np.abs(mic[:16000 * 2]), np.abs(out[:16000 * 2]), "full")) - (16000 * 2 - 1))
    lag = max(0, min(lag, 4000))
    mic = mic[lag:]
    pad = np.zeros(int(16000 * 0.6), np.float32)
    noise = float(np.sqrt(np.mean(mic[: len(pad) // 2] ** 2)))
    out = out[len(pad):]
    frames = min(len(out), len(mic) - len(pad)) // 512
    ratios = []
    for i in range(frames):
        a = out[i * 512:(i + 1) * 512]
        b = mic[len(pad) + i * 512: len(pad) + (i + 1) * 512]
        pa = float(np.sqrt(np.mean(a ** 2)))
        if pa > 0.02:
            ratios.append(float(np.sqrt(np.mean(b ** 2))) / pa)
    coupling = float(np.percentile(ratios, 90)) if ratios else 0.5
    print(f"room noise rms {noise:.4f}; playback frames {len(ratios)}; echo coupling p50 {np.median(ratios):.3f} p90 {coupling:.3f}")
    if "--no-save" not in sys.argv:
        settings.update({"echo_coupling": round(coupling, 3), "noise_floor": round(max(noise, 0.002), 4)})
        print("saved to", settings.path)


if __name__ == "__main__":
    main()
