"""从大模型的回答中稳健地提取 JSON。"""
from __future__ import annotations

import json
import re
from typing import Any

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)
_THINK_RE = re.compile(r"<think>.*?</think>", re.S)


def strip_think(text: str) -> str:
    return _THINK_RE.sub("", text or "").strip()


def parse_json(text: str, default: Any = None) -> Any:
    if default is None:
        default = {}
    if not text:
        return default
    text = strip_think(text)
    candidates = [m.group(1) for m in _FENCE_RE.finditer(text)] + [text]
    for c in candidates:
        c = c.strip()
        try:
            return json.loads(c)
        except Exception:
            pass
        for open_ch, close_ch in (("{", "}"), ("[", "]")):
            s, e = c.find(open_ch), c.rfind(close_ch)
            if s != -1 and e > s:
                try:
                    return json.loads(c[s:e + 1])
                except Exception:
                    pass
    return default
