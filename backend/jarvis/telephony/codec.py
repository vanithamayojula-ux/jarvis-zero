"""Telephone audio: G.711 μ-law / A-law (8 kHz) and sample-rate conversion, pure numpy."""
from __future__ import annotations

import numpy as np

_BIAS = 0x84
_SEG_U = np.array([0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF])
_SEG_A = np.array([0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF])


def ulaw_encode(pcm: np.ndarray) -> bytes:
    """ITU-T G.711 μ-law (same algorithm as the CCITT reference / Python's audioop)."""
    v = pcm.astype(np.int32) >> 2
    mask = np.where(v < 0, 0x7F, 0xFF)
    v = np.minimum(np.abs(v), 8159) + 0x21
    seg = np.searchsorted(_SEG_U, v, side="left")
    uval = (np.minimum(seg, 7) << 4) | ((v >> (np.minimum(seg, 7) + 1)) & 0x0F)
    out = np.where(seg >= 8, 0x7F ^ mask, uval ^ mask)
    return (out & 0xFF).astype(np.uint8).tobytes()


def ulaw_decode(data: bytes) -> np.ndarray:
    u = ~np.frombuffer(data, np.uint8).astype(np.int32) & 0xFF
    sign, exponent, mantissa = u & 0x80, (u >> 4) & 0x07, u & 0x0F
    x = (((mantissa << 3) + _BIAS) << exponent) - _BIAS
    return np.where(sign, -x, x).astype(np.int16)


def alaw_encode(pcm: np.ndarray) -> bytes:
    """ITU-T G.711 A-law (CCITT reference algorithm)."""
    v = pcm.astype(np.int32) >> 3
    mask = np.where(v >= 0, 0xD5, 0x55)
    v = np.where(v >= 0, v, -v - 1)
    seg = np.searchsorted(_SEG_A, v, side="left")
    s7 = np.minimum(seg, 7)
    aval = (s7 << 4) | np.where(s7 < 2, (v >> 1) & 0x0F, (v >> s7) & 0x0F)
    out = np.where(seg >= 8, 0x7F ^ mask, aval ^ mask)
    return (out & 0xFF).astype(np.uint8).tobytes()


def alaw_decode(data: bytes) -> np.ndarray:
    a = np.frombuffer(data, np.uint8).astype(np.int32) ^ 0x55
    sign, exponent, mantissa = a & 0x80, (a >> 4) & 0x07, a & 0x0F
    x = np.where(exponent == 0, (mantissa << 4) + 8, ((mantissa << 4) + 0x108) << (exponent - 1))
    return np.where(sign, x, -x).astype(np.int16)


def resample(pcm: np.ndarray, src: int, dst: int) -> np.ndarray:
    """Band-limited resampling (windowed-sinc low-pass before linear interpolation when downsampling)."""
    if src == dst or len(pcm) == 0:
        return pcm.astype(np.int16)
    x = pcm.astype(np.float32)
    if dst < src:
        cutoff = 0.9 * dst / src / 2            # normalized to src Nyquist=0.5
        taps = 63
        n = np.arange(taps) - (taps - 1) / 2
        h = 2 * cutoff * np.sinc(2 * cutoff * n) * np.hamming(taps)
        x = np.convolve(x, h / h.sum(), mode="same")
    t_out = np.arange(int(len(x) * dst / src)) * (src / dst)
    y = np.interp(t_out, np.arange(len(x)), x)
    return np.clip(np.round(y), -32768, 32767).astype(np.int16)


def to_phone(pcm: np.ndarray, rate: int, codec: str = "PCMU") -> bytes:
    pcm8 = resample(pcm, rate, 8000)
    return ulaw_encode(pcm8) if codec == "PCMU" else alaw_encode(pcm8)


def from_phone(payload: bytes, codec: str = "PCMU", rate: int = 16000) -> np.ndarray:
    pcm8 = ulaw_decode(payload) if codec == "PCMU" else alaw_decode(payload)
    return resample(pcm8, 8000, rate)
