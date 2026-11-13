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
