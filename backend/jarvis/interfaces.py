"""Swappable component contracts. Each engine part depends only on these."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol

import numpy as np


class VAD(Protocol):
    frame_samples: int

    def prob(self, frame: np.ndarray) -> float: ...
    def reset(self) -> None: ...


@dataclass
class Transcript:
    text: str
    confidence: float = 1.0   # 0..1, derived from the recogniser's log-probabilities
    seconds: float = 0.0


class STT(Protocol):
    def transcribe(self, pcm16k: np.ndarray, hint: str = "") -> Transcript: ...


@dataclass
class LLMChunk:
    text: str = ""
    tool_calls: list[dict] = field(default_factory=list)   # [{"name": str, "arguments": dict}]


class LLM(Protocol):
    async def stream(self, messages: list[dict], tools: list[dict] | None = None) -> AsyncIterator[LLMChunk]: ...
    async def complete(self, messages: list[dict]) -> str: ...


class TTS(Protocol):
    sample_rate: int

    def synthesize(self, text: str) -> np.ndarray: ...
