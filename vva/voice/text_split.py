"""流式文本处理：过滤思考内容、按句切分，供 TTS 边生成边播放。"""
from __future__ import annotations

import re

_SENT_END = "。！？!?；;\n"
_SOFT_BREAK = "，,、："
_MD_RE = re.compile(r"[*#`>|_~]+")


class ThinkFilter:
    """过滤流式输出中的 <think>...</think> 片段（部分推理模型会输出）。"""

    def __init__(self) -> None:
        self._buf = ""
        self._in_think = False

    def feed(self, delta: str) -> str:
        self._buf += delta
        out = []
        while self._buf:
            if self._in_think:
                end = self._buf.find("</think>")
                if end == -1:
                    self._buf = self._buf[-8:]  # 保留可能被截断的结束标签
                    break
                self._buf = self._buf[end + 8:]
                self._in_think = False
            else:
                start = self._buf.find("<think>")
                if start == -1:
                    # 末尾可能是不完整的 "<thi"，先留着
                    cut = self._buf.rfind("<")
                    if cut != -1 and "<think>".startswith(self._buf[cut:]):
                        out.append(self._buf[:cut])
                        self._buf = self._buf[cut:]
                    else:
                        out.append(self._buf)
                        self._buf = ""
                    break
                out.append(self._buf[:start])
                self._buf = self._buf[start + 7:]
                self._in_think = True
        return "".join(out)

    def flush(self) -> str:
        rest = "" if self._in_think else self._buf
        self._buf, self._in_think = "", False
        return rest


def clean_for_tts(text: str) -> str:
    return _MD_RE.sub("", text).strip()


class SentenceSplitter:
    """把流式文本切成适合合成的句子。句子过长时在逗号处提前切，降低首句延迟。"""

    def __init__(self, soft_limit: int = 40, first_soft_limit: int = 12) -> None:
        self._buf = ""
        self.soft_limit = soft_limit
        self.first_soft_limit = first_soft_limit
        self._emitted = 0

    def feed(self, delta: str) -> list[str]:
        self._buf += delta
        out: list[str] = []
        while True:
            idx = next((i for i, ch in enumerate(self._buf) if ch in _SENT_END), -1)
            limit = self.first_soft_limit if self._emitted == 0 else self.soft_limit
            if idx == -1 and len(self._buf) >= limit:
                idx = max((self._buf.rfind(ch) for ch in _SOFT_BREAK), default=-1)
                if idx < 4:
                    idx = -1
            if idx == -1:
                break
            sent, self._buf = self._buf[:idx + 1], self._buf[idx + 1:]
            sent = clean_for_tts(sent)
            if sent and any(ch.isalnum() for ch in sent):
                out.append(sent)
                self._emitted += 1
        return out

    def flush(self) -> list[str]:
        sent = clean_for_tts(self._buf)
        self._buf = ""
        return [sent] if sent and any(ch.isalnum() for ch in sent) else []


class AnswerLimiter:
    """回答长度兜底：累计超过 max_chars 后，只放行到下一个句末标点为止，之后的内容丢弃。

    超出 max_chars 的 1.5 倍仍没有句末标点时直接截断，并补一个句号。max_chars<=0 表示不限制。
    """

    def __init__(self, max_chars: int = 60) -> None:
        self.max_chars = int(max_chars)
        self._count = 0
        self.closed = False

    def feed(self, delta: str) -> str:
        if self.max_chars <= 0:
            return delta
        if self.closed:
            return ""
        out = []
        hard = int(self.max_chars * 1.5)
        for ch in delta:
            out.append(ch)
            self._count += 1
            if self._count >= self.max_chars and ch in _SENT_END:
                self.closed = True
                break
            if self._count >= hard:
                out.append("。")
                self.closed = True
                break
        return "".join(out)
