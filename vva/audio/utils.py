"""PCM / WAV 转换与重采样。"""
from __future__ import annotations

import io
import wave

import numpy as np


def pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def wav_to_pcm(data: bytes) -> tuple[bytes, int]:
    """返回 (单声道 PCM16, 采样率)。多声道会取平均。"""
    with wave.open(io.BytesIO(data), "rb") as w:
        sr, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError(f"仅支持 16-bit WAV，收到 {width * 8}-bit")
    if ch > 1:
        arr = np.frombuffer(raw, np.int16).reshape(-1, ch).mean(axis=1).astype(np.int16)
        raw = arr.tobytes()
    return raw, sr


def resample_pcm16(pcm: bytes, src_sr: int, dst_sr: int) -> bytes:
    if src_sr == dst_sr or not pcm:
        return pcm
    x = np.frombuffer(pcm, np.int16).astype(np.float32)
    n = int(round(len(x) * dst_sr / src_sr))
    if n <= 0:
        return b""
    y = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
    return np.clip(y, -32768, 32767).astype(np.int16).tobytes()


def rms_dbfs(pcm: bytes) -> float:
    x = np.frombuffer(pcm, np.int16).astype(np.float32)
    if x.size == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(x * x)))
    return 20.0 * np.log10(max(rms, 1e-3) / 32768.0)
