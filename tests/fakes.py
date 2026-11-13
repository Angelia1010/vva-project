"""测试用的假模型，实现与真实客户端相同的方法签名。"""
from __future__ import annotations

import json
from typing import Any

import numpy as np


class FakeLLM:
    """按脚本依次返回：每一项为 str（文字回答）或 list（工具调用）。"""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[list[dict]] = []

    async def stream(self, messages, tools=None):
        self.calls.append(messages)
        item = self.script.pop(0) if self.script else "好的。"
        if isinstance(item, list):
            yield "tool_calls", [{"id": f"c{i}", "name": n, "arguments": json.dumps(a, ensure_ascii=False)}
                                 for i, (n, a) in enumerate(item)]
        else:
            for ch in [item[i:i + 3] for i in range(0, len(item), 3)]:
                yield "text", ch


class FakeVLM:
    bbox_format = "0-1000"

    def __init__(self, answers: dict[str, str] | None = None, default: str = "画面中有一块表盘，读数约为 42。") -> None:
        self.answers = answers or {}
        self.default = default
        self.requests: list[tuple[int, str]] = []

    async def ask(self, images, prompt, system=None, json_mode=False, max_tokens=None):
        self.requests.append((len(images), prompt))
        for key, ans in self.answers.items():
            if key in prompt:
                return ans
        return self.default


class FakeASR:
    def __init__(self, text: str = "表上读数是多少") -> None:
        self.text = text
        self.received: list[int] = []

    async def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        self.received.append(len(pcm))
        return self.text


class FakeTTS:
    sample_rate = 24000

    def __init__(self) -> None:
        self.texts: list[str] = []

    async def stream(self, text: str):
        self.texts.append(text)
        for _ in range(10):
            yield b"\x01\x00" * 2400  # 100ms


def speech_pcm(seconds: float, sr: int = 16000, amp: int = 8000) -> bytes:
    t = np.arange(int(seconds * sr)) / sr
    return (amp * np.sin(2 * np.pi * 220 * t)).astype(np.int16).tobytes()


def silence_pcm(seconds: float, sr: int = 16000) -> bytes:
    return (np.random.default_rng(0).normal(0, 20, int(seconds * sr))).astype(np.int16).tobytes()


def voice_pcm(seconds: float, sr: int = 16000, f0: float = 150.0, amp: int = 9000) -> bytes:
    """带谐波的类人声信号（基频 f0，能量集中在 4kHz 以下）。"""
    t = np.arange(int(seconds * sr)) / sr
    x = sum((1.0 / k) * np.sin(2 * np.pi * f0 * k * t) for k in range(1, 15))
    return (x / np.abs(x).max() * amp).astype(np.int16).tobytes()


def typing_pcm(seconds: float, sr: int = 16000, keys_per_s: float = 10, seed: int = 1) -> bytes:
    """模拟敲键盘：一连串短促（约 40ms、快速衰减）的宽频脉冲。"""
    rng = np.random.default_rng(seed)
    x = rng.normal(0, 30, int(seconds * sr))
    n = int(0.04 * sr)
    env = np.exp(-np.arange(n) / (0.006 * sr))
    for k in range(int(seconds * keys_per_s)):
        st = int(k * sr / keys_per_s) + int(rng.integers(0, 500))
        seg = x[st:st + n]
        seg += rng.normal(0, 9000, n)[:len(seg)] * env[:len(seg)]
    return np.clip(x, -32767, 32767).astype(np.int16).tobytes()


def syllables_pcm(n: int, sr: int = 16000, syl_ms: int = 150, gap_ms: int = 60) -> bytes:
    """模拟正常说话：一个个音节之间有短暂的弱音（更接近真实语音的断续）。"""
    gap = (np.random.default_rng(2).normal(0, 30, int(gap_ms / 1000 * sr))).astype(np.int16).tobytes()
    out = b""
    for i in range(n):
        out += voice_pcm(syl_ms / 1000, sr, f0=140 + 15 * (i % 4)) + gap
    return out
