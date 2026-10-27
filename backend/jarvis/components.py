"""Local implementations: Silero VAD, faster-whisper STT, Ollama LLM, Kokoro/Piper TTS. All run on CPU, offline."""
from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
import unicodedata
from pathlib import Path
from typing import AsyncIterator

import httpx
import numpy as np

from .config import MODELS_DIR
from .interfaces import LLMChunk, Transcript

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
log = logging.getLogger("jarvis.tts")

KOKORO_DIR = MODELS_DIR / "kokoro"
KOKORO_MODEL = "kokoro-v1.0.int8.onnx"
KOKORO_VOICES = "voices-v1.0.bin"
KOKORO_DEFAULT = "kokoro:am_michael"
PIPER_FALLBACKS = ("en_GB-northern_english_male-medium", "en_GB-alan-medium")


def shrink_after_each_run(session) -> None:
    """ONNX keeps the scratch memory of the biggest sentence it ever spoke (Piper grew to ~520 MB process size after a
    few long replies). Give it back after every run instead: measured 23 Oct, ~226 MB and no slower."""
    import onnxruntime as ort
    opts = ort.RunOptions()
    opts.add_run_config_entry("memory.enable_memory_arena_shrinkage", "cpu:0")
    run = session.run
    session.run = lambda output_names, input_feed, run_options=None: run(output_names, input_feed, run_options or opts)


class SileroVAD:
    """Streaming Silero VAD v6 (ONNX). One call per 512-sample (32 ms) frame at 16 kHz."""

    frame_samples = 512

    def __init__(self):
        import faster_whisper
        import onnxruntime as ort

        path = Path(faster_whisper.__file__).parent / "assets" / "silero_vad_v6.onnx"
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        opts.log_severity_level = 4
        opts.enable_cpu_mem_arena = False          # a tiny model: no memory pool needed
        self.session = ort.InferenceSession(str(path), sess_options=opts, providers=["CPUExecutionProvider"])
        self.reset()

    def reset(self) -> None:
        self._h = np.zeros((1, 1, 128), np.float32)
        self._c = np.zeros((1, 1, 128), np.float32)
        self._ctx = np.zeros((1, 64), np.float32)

    def prob(self, frame: np.ndarray) -> float:
        f = (frame.astype(np.float32) / 32768.0).reshape(1, -1)
        out, self._h, self._c = self.session.run(
            None, {"input": np.concatenate([self._ctx, f], 1), "h": self._h, "c": self._c})
        self._ctx = f[:, -64:]
        return float(np.ravel(out)[0])


class WakeWord:
    """openWakeWord's pre-trained "hey jarvis" (Apache-2.0, ~4 MB of ONNX, github.com/dscripka/openWakeWord).
    Listens to the raw audio, so background talk that merely *transcribes* like "Travis" can't wake JARVIS.
    Tested 22 Oct on two voices: 'Hey Jarvis…' scored 1.00; 'Hey Travis…', 'Hey, can you pass the salt?' ≤ 0.10."""

    CHUNK = 1280                                       # 80 ms at 16 kHz, what the model expects

    def __init__(self, name: str = "hey_jarvis", threshold: float = 0.5):
        from openwakeword.model import Model
        self.name, self.threshold = name, threshold
        self.model = Model(wakeword_models=[name], inference_framework="onnx")
        self._buf = np.zeros(0, np.int16)

    def feed(self, frame: np.ndarray) -> float:
        """Add a mic frame (any length); returns the best score of the 80 ms chunks it completed."""
        self._buf = np.concatenate([self._buf, frame.astype(np.int16)])
        best = 0.0
        while len(self._buf) >= self.CHUNK:
            scores = self.model.predict(self._buf[:self.CHUNK])
            best = max(best, float(scores.get(self.name, max(scores.values(), default=0.0))))
            self._buf = self._buf[self.CHUNK:]
        return best

    def reset(self) -> None:
        self.model.reset()
        self._buf = np.zeros(0, np.int16)


class SmartTurn:
    """Pipecat Smart Turn v3.2 (BSD-2-Clause, 8 MB ONNX, github.com/pipecat-ai/smart-turn): from the sound of the
    last 8 s, has the speaker finished, or only paused mid-sentence? About 15-60 ms on this CPU.

    Preprocessing follows the project's inference.py: 16 kHz, keep the last 8 s (zero-padded at the front),
    zero-mean/unit-variance, 80-bin Whisper log-mel (faster-whisper's extractor computes the same features)."""

    PATH = MODELS_DIR / "smart-turn" / "smart-turn-v3.2-cpu.onnx"
    SAMPLES = 8 * 16000

    def __init__(self, path: Path | None = None):
        import onnxruntime as ort
        from faster_whisper.feature_extractor import FeatureExtractor

        opts = ort.SessionOptions()
        opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 2
        opts.log_severity_level = 4
        opts.enable_cpu_mem_arena = False          # runs once per pause: no memory pool needed
        self.session = ort.InferenceSession(str(path or self.PATH), opts, providers=["CPUExecutionProvider"])
        self.features = FeatureExtractor(feature_size=80, chunk_length=8)

    def prob(self, pcm16k: np.ndarray) -> float:
        """Probability (0..1) that the turn is complete."""
        a = pcm16k.astype(np.float32) / 32768.0
        a = a[-self.SAMPLES:] if len(a) >= self.SAMPLES else np.pad(a, (self.SAMPLES - len(a), 0))
        a = (a - a.mean()) / np.sqrt(a.var() + 1e-7)
        feats = self.features(a, padding=0)[:, : self.SAMPLES // 160]
        out = self.session.run(None, {"input_features": feats[None].astype(np.float32)})
        return float(np.ravel(out[0])[0])


class WhisperSTT:
    def __init__(self, model: str = "base.en", threads: int = 4):
        from faster_whisper import WhisperModel

        self.model_name = model
        self.model = WhisperModel(model, device="cpu", compute_type="int8", cpu_threads=threads)

    def transcribe(self, pcm16k: np.ndarray, hint: str = "") -> Transcript:
        t0 = time.perf_counter()
        audio = pcm16k.astype(np.float32) / 32768.0
        segments, _ = self.model.transcribe(
            audio, language="en", beam_size=1, vad_filter=False, condition_on_previous_text=False,
            initial_prompt=hint or None, temperature=0.0,
        )
        segs = list(segments)
        text = " ".join(s.text.strip() for s in segs).strip()
        if segs:
            logp = sum(s.avg_logprob for s in segs) / len(segs)
            no_speech = max(s.no_speech_prob for s in segs)
            confidence = max(0.0, min(1.0, math.exp(logp))) * (1.0 - no_speech * 0.5)
        else:
            confidence = 0.0
        return Transcript(text=text, confidence=confidence, seconds=time.perf_counter() - t0)


class LazyWhisperSTT:
    """WhisperSTT that loads on first use. Behind the cloud it's only a backup, and loading it costs ~160 MB of RAM
    plus ~1.1 GB of reserved memory, so wait until it's actually needed (the first local transcript takes ~1-2 s more)."""

    def __init__(self, model: str = "base.en", threads: int = 4):
        self.model_name, self.threads = model, threads
        self._stt: WhisperSTT | None = None
        self._lock = threading.Lock()                       # the mic and a phone call may both need it at once

    def transcribe(self, pcm16k: np.ndarray, hint: str = "") -> Transcript:
        with self._lock:
            if self._stt is None:
                self._stt = WhisperSTT(self.model_name, self.threads)
        return self._stt.transcribe(pcm16k, hint)


class OllamaLLM:
    """Streams from the local Ollama server. Nothing leaves the machine."""

    # Default: unload after 10 idle minutes. Never -1 ("forever"): a crash or a test run left 2.3 GB parked in RAM.
    def __init__(self, url: str, model: str, keep_alive: str | int = "10m", num_ctx: int = 2048):
        self.url = url.rstrip("/")
        self.model = model
        self.keep_alive = keep_alive
        # num_ctx must be identical on every request: a different value makes Ollama reload the whole model (~5-7 s).
        self.options = {"num_ctx": num_ctx, "temperature": 0.6, "num_predict": 220}
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=5.0))

    async def warm(self, system_prompt: str = "") -> None:
        """Load the model with the exact options chat uses, and pre-process the system prompt."""
        msgs = [{"role": "system", "content": system_prompt}] if system_prompt else []
        await self.client.post(f"{self.url}/api/chat", json={
            "model": self.model, "messages": msgs + [{"role": "user", "content": "hi"}], "stream": False,
            "keep_alive": self.keep_alive, "options": {**self.options, "num_predict": 1}})

    async def unload(self) -> None:
        """Free the model's RAM when JARVIS exits (it stays loaded while JARVIS runs)."""
        try:
            await self.client.post(f"{self.url}/api/generate", json={"model": self.model, "keep_alive": 0}, timeout=5)
        except Exception:
            pass

    async def stream(self, messages: list[dict], tools: list[dict] | None = None) -> AsyncIterator[LLMChunk]:
        body = {"model": self.model, "messages": messages, "stream": True,
                "keep_alive": self.keep_alive, "options": self.options}
        if tools:
            body["tools"] = tools
        async with self.client.stream("POST", f"{self.url}/api/chat", json=body) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue
                data = json.loads(line)
                msg = data.get("message") or {}
                calls = []
                for tc in msg.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    args = fn.get("arguments") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except ValueError:
                            args = {}
                    calls.append({"name": fn.get("name", ""), "arguments": args})
                if msg.get("content") or calls:
                    yield LLMChunk(text=msg.get("content", ""), tool_calls=calls)
                if data.get("done"):
                    break

    async def complete(self, messages: list[dict]) -> str:
        body = {"model": self.model, "messages": messages, "stream": False, "keep_alive": self.keep_alive,
                "options": {**self.options, "num_predict": 600}}
        resp = await self.client.post(f"{self.url}/api/chat", json=body)
        resp.raise_for_status()
        return resp.json()["message"]["content"]

    async def health(self) -> bool:
        try:
            r = await self.client.get(f"{self.url}/api/tags", timeout=3.0)
            return any(m["name"] == self.model for m in r.json().get("models", []))
        except Exception:
            return False


class PiperTTS:
    def __init__(self, voice: str = "en_GB-alan-medium", rate: float = 1.0):
        from piper import PiperVoice, SynthesisConfig

        path = MODELS_DIR / "piper" / f"{voice}.onnx"
        if not path.exists():
            raise FileNotFoundError(f"Piper voice not found: {path}. Run install.ps1 to download voices.")
        self.voice_name = voice
        self.voice = PiperVoice.load(str(path))
        shrink_after_each_run(self.voice.session)
        self.sample_rate = self.voice.config.sample_rate
        self._cfg_cls = SynthesisConfig
        self.set_rate(rate)

    def set_rate(self, rate: float) -> None:
        rate = max(0.5, min(2.0, float(rate)))
        self.config = self._cfg_cls(length_scale=1.0 / rate)

    def synthesize(self, text: str) -> np.ndarray:
        text = normalize_for_speech(text)
        if not text:
            return np.zeros(0, np.int16)
        chunks = list(self.voice.synthesize(text, syn_config=self.config))
        if not chunks:
            return np.zeros(0, np.int16)
        return np.concatenate([c.audio_int16_array for c in chunks])


class KokoroTTS:
    """Kokoro-82M (int8 ONNX, Apache-2.0) via kokoro-onnx: the most natural local voice, but ~10x slower than Piper.

    Opt-in: on an i5-1340P a short sentence takes ~0.7-1 s idle and ~2 s while the LLM is still streaming,
    so it adds 1-2 s before JARVIS starts speaking. Voice names look like "kokoro:am_michael"
    (prefix a = American, b = British; f/m = female/male).
    """

    sample_rate = 24000

    def __init__(self, voice: str = KOKORO_DEFAULT, rate: float = 1.0, threads: int = 4):
        import onnxruntime as ort
        from kokoro_onnx import Kokoro

        name = voice.split(":", 1)[-1]
        model, voices = KOKORO_DIR / KOKORO_MODEL, KOKORO_DIR / KOKORO_VOICES
        for f in (model, voices):
            if not f.exists():
                raise FileNotFoundError(f"Kokoro file not found: {f}. Run install.ps1 to download it.")
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads   # 4 (the P-cores) was as fast as 6-8 on an i5-1340P
        opts.inter_op_num_threads = 1
        # (no shrink_after_each_run here: it saved ~220 MB but made this already slow voice ~20% slower, 23 Oct)
        session = ort.InferenceSession(str(model), opts, providers=["CPUExecutionProvider"])
        self.engine = Kokoro.from_session(session, str(voices))
        if name not in self.engine.get_voices():
            raise ValueError(f"unknown Kokoro voice: {name}")
        self.voice_name = f"kokoro:{name}"
        self._voice = name
        self._lang = "en-gb" if name.startswith("b") else "en-us"
        self.set_rate(rate)
        self.engine.create("Ready.", voice=name, speed=1.0, lang=self._lang)  # warm-up so the first reply is fast

    def set_rate(self, rate: float) -> None:
        self.speed = max(0.5, min(2.0, float(rate)))

    def synthesize(self, text: str) -> np.ndarray:
        text = normalize_for_speech(text)
        if not text:
            return np.zeros(0, np.int16)
        audio, sr = self.engine.create(text, voice=self._voice, speed=self.speed, lang=self._lang)
        audio = np.asarray(audio, np.float32)
        if sr != self.sample_rate:  # not expected; keeps the declared rate honest
            n = int(len(audio) * self.sample_rate / sr)
            audio = np.interp(np.arange(n) * (sr / self.sample_rate), np.arange(len(audio)), audio)
        return (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)


def make_tts(voice: str, rate: float = 1.0):
    """Build the TTS for a voice name. Kokoro voices fall back to Piper if Kokoro's files are missing or fail."""
    if voice.startswith("kokoro:"):
        try:
            return KokoroTTS(voice, rate)
        except Exception as exc:  # missing files, broken install, unknown voice
            piper = sorted(p.stem for p in (MODELS_DIR / "piper").glob("*.onnx"))
            fallback = next((v for v in PIPER_FALLBACKS if v in piper), piper[0] if piper else PIPER_FALLBACKS[0])
            log.warning("Kokoro TTS unavailable (%s); falling back to Piper voice %s", exc, fallback)
            return PiperTTS(fallback, rate)
    return PiperTTS(voice, rate)


class LazyTTS:
    """make_tts() on first use. In chat mode nothing is ever spoken, so the voice model (~60-330 MB) never loads; in
    voice mode the server warms it in the background at start. The sample rate is known up front (from the voice's
    small .json file), so the speaker can be opened before the model is loaded."""

    def __init__(self, voice: str, rate: float = 1.0):
        self.voice_name, self._rate = voice, rate
        self._tts = None
        self._lock = threading.Lock()
        self.sample_rate = 24000 if voice.startswith("kokoro:") else _piper_rate(voice)

    @property
    def loaded(self) -> bool:
        return self._tts is not None

    def load(self):
        with self._lock:
            if self._tts is None:
                self._tts = make_tts(self.voice_name, self._rate)
        return self._tts

    def set_rate(self, rate: float) -> None:
        self._rate = rate
        if self._tts is not None and hasattr(self._tts, "set_rate"):
            self._tts.set_rate(rate)

    def synthesize(self, text: str) -> np.ndarray:
        tts = self.load()
        audio = tts.synthesize(text)
        if tts.sample_rate != self.sample_rate and len(audio):    # Kokoro fell back to Piper: match the speaker
            n = int(len(audio) * self.sample_rate / tts.sample_rate)
            audio = np.interp(np.arange(n) * (tts.sample_rate / self.sample_rate), np.arange(len(audio)),
                              audio).astype(np.int16)
        return audio


def _piper_rate(voice: str) -> int:
    try:
        cfg = json.loads((MODELS_DIR / "piper" / f"{voice}.onnx.json").read_text(encoding="utf-8"))
        return int(cfg["audio"]["sample_rate"])
    except (OSError, ValueError, KeyError, TypeError):
        return 22050                                       # every medium Piper voice


def kokoro_voices() -> list[str]:
    path = KOKORO_DIR / KOKORO_VOICES
    if not (path.exists() and (KOKORO_DIR / KOKORO_MODEL).exists()):
        return []
    try:
        with np.load(path) as z:
            names = list(z.files)
    except Exception:
        return []
    return [f"kokoro:{n}" for n in sorted(names) if n[:2] in ("am", "af", "bm", "bf")]  # English voices only


def available_voices() -> list[str]:
    return kokoro_voices() + sorted(p.stem for p in (MODELS_DIR / "piper").glob("*.onnx"))


# ---------- text normalisation, so the voice never reads symbols aloud ----------

# A link is said as its site name ("youtube.com"), never dropped mid-sentence ("Visit  for more").
_URL = re.compile(r"(?:\bhttps?://(?:www\.)?|\bwww\.)([\w-]+(?:\.[\w-]+)+)(?:[/?#:]\S*?)?(?=[.,!?;:)]*(?:\s|$))", re.I)
_EMAIL = re.compile(r"\b([\w.+-]+)@([\w-]+\.[\w.]*\w)")
_NUM = r"(\d+(?:,\d{2,3})*(?:\.\d+)?)"  # 1,200 / 1,00,000 / 4.5
_SCALE = r"(?:\s*(k|K|M|B|bn|million|billion|thousand|lakh|lakhs|crore|crores)\b)?"
_SCALES = {"k": "thousand", "K": "thousand", "M": "million", "B": "billion", "bn": "billion"}
_CURRENCY = [(re.compile(r"₹\s*" + _NUM + _SCALE), "rupees"), (re.compile(r"\bRs\.?\s*" + _NUM + _SCALE), "rupees"),
             (re.compile(r"\$\s*" + _NUM + _SCALE), "dollars"), (re.compile(r"€\s*" + _NUM + _SCALE), "euros"),
             (re.compile(r"£\s*" + _NUM + _SCALE), "pounds")]
_UNITS = [  # only after a number, so ordinary words are never touched
    (r"\s*°\s*C\b", " degrees"), (r"\s*°\s*F\b", " degrees Fahrenheit"), (r"\s*°", " degrees"),
    (r"\s*km/h\b", " kilometres per hour"), (r"\s*kmph\b", " kilometres per hour"), (r"\s*mph\b", " miles per hour"),
    (r"\s*m/s\b", " metres per second"), (r"\s*km\b", " kilometres"), (r"\s*kg\b", " kilograms"),
    (r"\s*cm\b", " centimetres"), (r"\s*mm\b", " millimetres"), (r"\s*ml\b", " millilitres"), (r"\s*mg\b", " milligrams"),
    (r"\s*GB\b", " gigabytes"), (r"\s*MB\b", " megabytes"), (r"\s*TB\b", " terabytes"), (r"\s*GHz\b", " gigahertz"),
    (r"\s*mAh\b", " milliamp hours"), (r"\s*%", " percent"),
]
_FRACTIONS = {"1/2": "one half", "1/3": "one third", "2/3": "two thirds", "1/4": "one quarter", "3/4": "three quarters"}


def _indian_number(m: re.Match) -> str:
    """1,00,000 → '1 lakh'; 2,50,00,000 → '2 crore 50 lakh' (espeak reads Indian grouping as a wrong number)."""
    n = int(m.group(0).replace(",", ""))
    crore, lakh, rest = n // 10**7, (n // 10**5) % 100, n % 10**5
    parts = ([f"{crore} crore"] if crore else []) + ([f"{lakh} lakh"] if lakh else []) + ([str(rest)] if rest else [])
    return " ".join(parts)


def _money(m: re.Match, unit: str) -> str:
    """$1.5M → '1.5 million dollars', ₹2 crore → '2 crore rupees', $15.99 → '15.99 dollars'."""
    scale = m.group(2)
    word = _SCALES.get(scale, (scale or "").lower().rstrip("s"))
    return f"{m.group(1)} {word} {unit}" if word else f"{m.group(1)} {unit}"
_UNIT_RES = [(re.compile(r"(?<=\d)" + pat), spoken) for pat, spoken in _UNITS]
_WORDS = [(re.compile(r"\be\.g\.,?", re.I), "for example"), (re.compile(r"\bi\.e\.,?", re.I), "that is"),
          (re.compile(r"\betc\.", re.I), "et cetera."), (re.compile(r"\bvs\.?(?=\s)", re.I), "versus"),
          (re.compile(r"\bw/(?=\s)"), "with"), (re.compile(r"\band/or\b", re.I), "or")]


def _drop_char(ch: str) -> bool:
    # emoji and pictographs (So), modifier symbols (Sk), private-use/surrogates, joiners and variation selectors
    return unicodedata.category(ch) in ("So", "Sk", "Cs", "Co") or ch in "‍️︎⃣"


def normalize_for_speech(text: str) -> str:
    """Light, fast clean-up so TTS says '32 degrees', 'and', 'kilometres per hour' instead of reading symbols."""
    text = _URL.sub(r"\1", text)
    text = _EMAIL.sub(r"\1 at \2", text)
    for rx, unit in _CURRENCY:
        text = rx.sub(lambda m, u=unit: _money(m, u), text)
    text = re.sub(r"\b\d{1,2}(?:,\d{2})+,\d{3}\b", _indian_number, text)       # 1,00,000 -> 1 lakh
    text = re.sub(r"\+(\d[\d -]{7,}\d)\b", lambda m: "plus " + " ".join(re.sub(r"\D", "", m.group(1))), text)  # +91 98765 43210
    text = re.sub(r"\b([CF])\+\+", r"\1 plus plus", text)
    text = re.sub(r"\b([CF])#", r"\1 sharp", text)
    for rx, spoken in _UNIT_RES:
        text = rx.sub(spoken, text)
    for rx, spoken in _WORDS:
        text = rx.sub(spoken, text)
    text = re.sub(r"(?<![\d/])\b([123])/([234])\b(?!/)", lambda m: _FRACTIONS.get(m.group(0), m.group(0)), text)
    text = re.sub(r"\b(\d+)/\1\b", r"\1 \1", text)                     # 50/50 -> 50 50
    text = re.sub(r"(?<=\d)\s*[-–]\s*(?=\d)", " to ", text)        # 3-5 -> 3 to 5
    text = re.sub(r"(?:(?<=^)|(?<=[\s(]))[-−](?=\d)", "minus ", text)   # -5 degrees -> minus 5 degrees
    text = re.sub(r"(?<=\d)\s*\+\s*(?=\d)", " plus ", text)
    text = re.sub(r"(?<=\d)\s*=\s*(?=\d)", " equals ", text)
    text = re.sub(r"(?<=\d)\s*[x×]\s*(?=\d)", " times ", text)
    text = re.sub(r"(?<=\w)\s*\^\s*(?=\d)", " to the power of ", text)
    text = re.sub(r"~\s*(?=\d)", "about ", text)
    text = re.sub(r"#\s*(?=\d)", "number ", text)
    # phone numbers / long codes: digit by digit, not "9 billion 876 million…"
    text = re.sub(r"(?<![\d.,])\d{7,}(?![\d.,]\d)", lambda m: " ".join(m.group(0)), text)
    text = text.replace("&", " and ").replace("@", " at ")
    text = re.sub(r"(?<=[A-Za-z])/(?=[A-Za-z])", " or ", text)    # and/or -> and or
    text = "".join(ch for ch in text if not _drop_char(ch))
    text = re.sub(r"[*_#`~<>|\[\]{}\\^]+", " ", text)              # markdown / code leftovers
    text = re.sub(r"\(\s*\)", " ", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([.,!?;:])", r"\1", text).strip()
    return text if re.search(r"[A-Za-z0-9]", text) else ""
