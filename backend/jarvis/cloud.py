"""Free cloud brains (Groq's free tier, no card): Whisper large for hearing you, Qwen for answering.

Both are optional and wrap the local models: when there's no key, no internet, Groq is down or the free limits run out,
JARVIS quietly uses the local faster-whisper / Ollama models instead, then retries the cloud a little later.
What goes to Groq: the audio of what you said after the hotkey (or wake word) and the chat messages. Nothing else.
"""
from __future__ import annotations

import io
import json
import logging
import math
import os
import time
import wave
from typing import AsyncIterator

import httpx
import numpy as np

from .components import OllamaLLM
from .interfaces import LLMChunk, Transcript

log = logging.getLogger("jarvis.cloud")
GROQ = "https://api.groq.com/openai/v1"


def groq_key() -> str:
    return os.environ.get("GROQ_API_KEY", "").strip()


class _Breaker:
    """After a failure, skip the cloud for a while (longer when the free limit is used up)."""

    def __init__(self):
        self.until = 0.0
        self.reason = ""

    def ok(self) -> bool:
        return time.monotonic() >= self.until

    def trip(self, reason: str, seconds: float) -> None:
        self.until, self.reason = time.monotonic() + seconds, reason
        log.warning("cloud off for %.0fs: %s", seconds, reason)

    def wait_for(self, resp: httpx.Response) -> float:
        if resp.status_code == 429:                       # free limit: wait as long as Groq says
            try:
                return min(3600.0, float(resp.headers.get("retry-after", "30")))
            except ValueError:
                return 30.0
        return 20.0 if resp.status_code >= 500 else 300.0  # 401/403/400: probably the key or the request


class CloudSTT:
    """Groq Whisper (large-v3-turbo): much better with names ("Hanumankind", not "humankind"). Local on failure."""

    def __init__(self, local, key: str, model: str = "whisper-large-v3-turbo"):
        self.local, self.key, self.model_name = local, key, model
        self.model = getattr(local, "model", None)
        self.client = httpx.Client(timeout=httpx.Timeout(8.0, connect=3.0))
        self.breaker = _Breaker()
        self.last_source = "local"

    def transcribe(self, pcm16k: np.ndarray, hint: str = "") -> Transcript:
        if self.key and self.breaker.ok() and len(pcm16k) > 1600:
            t0 = time.perf_counter()
            try:
                buf = io.BytesIO()
                with wave.open(buf, "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(16000)
                    w.writeframes(pcm16k.astype(np.int16).tobytes())
                data = {"model": self.model_name, "language": "en", "response_format": "verbose_json", "temperature": "0"}
                if hint:
                    data["prompt"] = hint[:800]
                r = self.client.post(f"{GROQ}/audio/transcriptions", headers={"Authorization": f"Bearer {self.key}"},
                                     data=data, files={"file": ("speech.wav", buf.getvalue(), "audio/wav")})
                if r.status_code == 200:
                    body = r.json()
                    segs = body.get("segments") or []
                    text = (body.get("text") or "").strip()
                    if segs:
                        logp = sum(s.get("avg_logprob", 0.0) for s in segs) / len(segs)
                        no_speech = max(s.get("no_speech_prob", 0.0) for s in segs)
                        confidence = max(0.0, min(1.0, math.exp(logp))) * (1.0 - no_speech * 0.5)
                    else:
                        confidence = 0.9 if text else 0.0
                    self.last_source = "cloud"
                    return Transcript(text=text, confidence=confidence, seconds=time.perf_counter() - t0)
                self.breaker.trip(f"STT HTTP {r.status_code}", self.breaker.wait_for(r))
            except Exception as exc:                       # offline, timeout…
                self.breaker.trip(f"STT {type(exc).__name__}", 30.0)
        self.last_source = "local"
        return self.local.transcribe(pcm16k, hint)


class CloudTTS:
    """Groq's Orpheus voice (much more natural than Piper) for chat replies; the local voice for short command
    confirmations ("Opening Spotify."), which must be instant, and whenever the free limit (~100 a day) runs out.

    kind() says what's being spoken right now: "chat" (the model's reply) or "tool" (a command result)."""

    sample_rate = 24000                                    # Orpheus' rate; the local voice is resampled to match

    def __init__(self, local, key: str, voice: str = "troy", model: str = "canopylabs/orpheus-v1-english", kind=None):
        self.local, self.key, self.voice, self.model = local, key, voice, model
        self.voice_name = getattr(local, "voice_name", "")
        self.kind = kind or (lambda: "chat")
        self.client = httpx.Client(timeout=httpx.Timeout(10.0, connect=3.0))
        self.breaker = _Breaker()
        self.last_source = "local"

    def set_rate(self, rate: float) -> None:
        if hasattr(self.local, "set_rate"):
            self.local.set_rate(rate)

    def synthesize(self, text: str) -> np.ndarray:
        from .components import normalize_for_speech
        spoken = normalize_for_speech(text)
        if not spoken:
            return np.zeros(0, np.int16)
        if self.key and self.breaker.ok() and self.kind() == "chat":
            try:
                r = self.client.post(f"{GROQ}/audio/speech", headers={"Authorization": f"Bearer {self.key}"},
                                     json={"model": self.model, "input": spoken[:1000], "voice": self.voice,
                                           "response_format": "wav"})
                if r.status_code == 200:
                    pcm = _wav_pcm(r.content)
                    if pcm is not None and len(pcm):
                        self.last_source = "cloud"
                        return pcm
                    self.breaker.trip("TTS returned no audio", 60.0)
                else:
                    self.breaker.trip(f"TTS HTTP {r.status_code}: {r.text[:120]}", self.breaker.wait_for(r))
            except Exception as exc:
                self.breaker.trip(f"TTS {type(exc).__name__}", 30.0)
        self.last_source = "local"
        audio = self.local.synthesize(text)
        rate = getattr(self.local, "sample_rate", self.sample_rate)
        if rate != self.sample_rate and len(audio):          # Piper is 22.05 kHz: match the speaker's 24 kHz
            n = int(len(audio) * self.sample_rate / rate)
            audio = np.interp(np.arange(n) * (rate / self.sample_rate), np.arange(len(audio)),
                              audio.astype(np.float32)).astype(np.int16)
        return audio


def _wav_pcm(data: bytes) -> np.ndarray | None:
    """16-bit PCM samples from a WAV. Groq streams it, so the header's length fields are bogus: find 'data' and
    take everything after it."""
    if data[:4] != b"RIFF":
        return None
    i = data.find(b"data", 12)
    if i < 0:
        return None
    body = data[i + 8:]
    return np.frombuffer(body[: len(body) // 2 * 2], np.int16).copy()


def _openai_messages(messages: list[dict]) -> list[dict]:
    """JARVIS keeps Ollama-style history (tool calls without ids); Groq wants OpenAI style. Convert."""
    out, pending, n = [], [], 0
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            calls = []
            for tc in m["tool_calls"]:
                fn = tc.get("function") or {}
                cid = f"call_{n}"
                n += 1
                pending.append(cid)
                args = fn.get("arguments") or {}
                calls.append({"id": cid, "type": "function",
                              "function": {"name": fn.get("name", ""),
                                           "arguments": args if isinstance(args, str) else json.dumps(args)}})
            out.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": calls})
        elif m.get("role") == "tool":
            if pending:
                out.append({"role": "tool", "tool_call_id": pending.pop(0), "content": m.get("content", "")})
            else:                                          # a tool result with no call: pass it on as plain context
                out.append({"role": "user", "content": f"(tool result) {m.get('content', '')}"})
        else:
            out.append({"role": m.get("role", "user"), "content": m.get("content", "")})
    return out


class CloudLLM(OllamaLLM):
    """Groq chat (Qwen3.8-27B by default) with the local Ollama model as the automatic fallback.

    It *is* an OllamaLLM (the fallback), so warming, health checks and model switching keep working as before."""

    # Tested on 22 Oct: without this, "who is hanumankind?" got "an Indian streetwear brand". With it, both Qwen and
    # GPT-OSS say they're not sure and offer a search, and still answer plain facts ("Canberra").
    HONESTY = ("You have no internet access and your memory of specific people, artists, songs, brands, products and "
               "recent events is often wrong. Only state such a fact if you are certain. Otherwise say plainly that "
               "you're not sure and offer: \"Want me to search Google for it?\" Never invent details. "
               "Never claim you did something on the computer: only tools do things.")

    def __init__(self, url: str, model: str, keep_alive, key: str, cloud_model: str = "qwen/qwen3.8-27b"):
        super().__init__(url, model, keep_alive)
        self.key, self.cloud_model = key, cloud_model
        self.cloud = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=4.0))
        self.breaker = _Breaker()
        self.last_source = "local"

    async def warm(self, system_prompt: str = "") -> None:
        """The cloud answers, so don't park the local model in RAM (~2.8 GB) just in case: only check Ollama is up.
        If the cloud ever fails, the first local reply loads it (~10 s) and keep_alive unloads it again later."""
        if not self.key:
            return await super().warm(system_prompt)
        (await self.client.get(f"{self.url}/api/tags", timeout=5.0)).raise_for_status()

    def _body(self, messages: list[dict], tools, stream: bool, max_tokens: int) -> dict:
        msgs = _openai_messages(messages)
        if msgs and msgs[0]["role"] == "system":
            msgs[0] = {**msgs[0], "content": f"{msgs[0]['content']}\n\n{self.HONESTY}"}
        body = {"model": self.cloud_model, "messages": msgs, "stream": stream, "max_tokens": max_tokens,
                "temperature": 0.6}
        if self.cloud_model.startswith("qwen/"):
            body["reasoning_effort"] = "none"              # no hidden "thinking": the first words come fast
        elif self.cloud_model.startswith("openai/gpt-oss"):
            body.update(reasoning_effort="low", include_reasoning=False)
        if tools:
            body["tools"] = tools
        return body

    async def stream(self, messages: list[dict], tools: list[dict] | None = None) -> AsyncIterator[LLMChunk]:
        if not (self.key and self.breaker.ok()):
            self.last_source = "local"
            async for chunk in super().stream(messages, tools):
                yield chunk
            return
        for attempt in (1, 2):
            state = {"yielded": False}
            try:
                async for chunk in self._cloud_stream(messages, tools, state):
                    yield chunk
                if state.get("done"):
                    return
                break                                      # an HTTP error reply: the breaker is already set
            except httpx.TransportError as exc:
                # Usually a kept-alive connection Groq had already closed ("Server disconnected"): one retry on a
                # fresh connection almost always works, and is far quicker than loading the local model (~10-20 s).
                if state["yielded"]:
                    self.breaker.trip(f"chat {type(exc).__name__}", 5.0)
                    return                                 # cut off mid-reply: don't start a second, different one
                if attempt == 2:
                    self.breaker.trip(f"chat {type(exc).__name__}", 8.0)
            except Exception as exc:
                self.breaker.trip(f"chat {type(exc).__name__}", 30.0)
                if state["yielded"]:
                    return
                break
        self.last_source = "local"
        async for chunk in super().stream(messages, tools):
            yield chunk

    async def _cloud_stream(self, messages: list[dict], tools, state: dict) -> AsyncIterator[LLMChunk]:
        """One streamed Groq request. state["yielded"]: output has gone out; state["done"]: finished normally."""
        async with self.cloud.stream("POST", f"{GROQ}/chat/completions", headers={"Authorization": f"Bearer {self.key}"},
                                     json=self._body(messages, tools, True, 260)) as resp:
            if resp.status_code != 200:
                await resp.aread()
                self.breaker.trip(f"chat HTTP {resp.status_code}: {resp.text[:160]}", self.breaker.wait_for(resp))
                return
            self.last_source = "cloud"
            calls: dict[int, dict] = {}
            async for line in resp.aiter_lines():
                if not line.startswith("data: ") or line.strip() == "data: [DONE]":
                    continue
                delta = (json.loads(line[6:]).get("choices") or [{}])[0].get("delta") or {}
                for tc in delta.get("tool_calls") or []:   # tool calls arrive in pieces
                    c = calls.setdefault(tc.get("index", 0), {"name": "", "arguments": ""})
                    fn = tc.get("function") or {}
                    c["name"] += fn.get("name") or ""
                    c["arguments"] += fn.get("arguments") or ""
                if delta.get("content"):
                    state["yielded"] = True
                    yield LLMChunk(text=delta["content"])
            if calls:
                out = []
                for c in calls.values():
                    try:
                        args = json.loads(c["arguments"] or "{}")
                    except ValueError:
                        args = {}
                    out.append({"name": c["name"], "arguments": args})
                state["yielded"] = True
                yield LLMChunk(tool_calls=out)
            state["done"] = True

    async def _post(self, body: dict, timeout: float | None = None) -> httpx.Response:
        """POST to Groq, retrying once on a dropped connection (a stale kept-alive one, usually)."""
        kw = {"timeout": timeout} if timeout else {}
        try:
            return await self.cloud.post(f"{GROQ}/chat/completions", headers={"Authorization": f"Bearer {self.key}"},
                                         json=body, **kw)
        except (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError, httpx.ConnectError):
            return await self.cloud.post(f"{GROQ}/chat/completions", headers={"Authorization": f"Bearer {self.key}"},
                                         json=body, **kw)

    async def complete(self, messages: list[dict]) -> str:
        if self.key and self.breaker.ok():
            try:
                r = await self._post(self._body(messages, None, False, 700))
                if r.status_code == 200:
                    self.last_source = "cloud"
                    return r.json()["choices"][0]["message"]["content"] or ""
                self.breaker.trip(f"complete HTTP {r.status_code}", self.breaker.wait_for(r))
            except Exception as exc:
                self.breaker.trip(f"complete {type(exc).__name__}", 8.0 if isinstance(exc, httpx.TransportError) else 30.0)
        self.last_source = "local"
        return await super().complete(messages)

    async def local_complete(self, messages: list[dict]) -> str:
        """Like complete(), but only ever the model on this laptop (used for anything read from the screen)."""
        self.last_source = "local"
        return await super().complete(messages)

    async def cloud_only(self, messages: list[dict], max_tokens: int = 80, temperature: float = 0.0,
                         timeout: float = 6.0, model: str | None = None) -> str | None:
        """One quick cloud answer, or None (never the local model: the 3B is too slow and too wrong for this job).
        model: another Groq model for a side job (memes). Groq counts each model's free limits separately, so the side
        job never uses up the chat model's minute, and its failures never pause the chat."""
        side = bool(model) and model != self.cloud_model
        if not self.key or (not side and not self.breaker.ok()):
            return None
        try:
            body = self._body(messages, None, False, max_tokens)
            body["messages"][0]["content"] = messages[0]["content"]          # no honesty note: this isn't chat
            body["temperature"] = temperature
            if side:
                body["model"] = model
                body.pop("reasoning_effort", None)
                body.pop("include_reasoning", None)
                if model.startswith("openai/gpt-oss"):
                    body.update(reasoning_effort="low", include_reasoning=False)
            r = await self._post(body, timeout=timeout)
            if r.status_code == 200:
                return (r.json()["choices"][0]["message"]["content"] or "").strip()
            if side:
                return None
            self.breaker.trip(f"rewrite HTTP {r.status_code}", self.breaker.wait_for(r))
        except httpx.TransportError:
            pass        # a background helper (memory, rewording) failing must not push the next chat to the slow local model
        except Exception as exc:
            if not side:
                self.breaker.trip(f"rewrite {type(exc).__name__}", 30.0)
        return None

    def status(self) -> dict:
        return {"cloud": bool(self.key), "cloud_model": self.cloud_model, "using": self.last_source,
                "cloud_paused_for_s": max(0, round(self.breaker.until - time.monotonic())), "reason": self.breaker.reason}
