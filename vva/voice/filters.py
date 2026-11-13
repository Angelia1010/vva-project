"""语音识别结果过滤：挡住噪声被误识别出的短词、语气词和常见幻觉文字。"""
from __future__ import annotations

import re

# 语音识别模型在噪声、静音上常见的误识别结果
DEFAULT_IGNORE_PHRASES = [
    "嗯", "啊", "呃", "哦", "噢", "唉", "哎", "嗯嗯", "啊啊", "哈", "哈哈", "呵呵", "嘿",
    "谢谢观看", "谢谢大家", "感谢观看", "请不吝点赞订阅转发打赏支持明镜与点点栏目", "字幕由", "字幕志愿者",
    "Thank you", "Thanks for watching",
]

_PUNCT_RE = re.compile(r"[\s\W_]+", re.UNICODE)


def normalize(text: str) -> str:
    """去掉空白和标点，只留文字和数字，便于比较。"""
    return _PUNCT_RE.sub("", text or "")


class UtteranceFilter:
    """判断一条识别结果是否应当作为指令处理。"""

    def __init__(self, min_chars: int = 2, ignore_phrases: list[str] | None = None) -> None:
        self.min_chars = int(min_chars)
        phrases = DEFAULT_IGNORE_PHRASES if ignore_phrases is None else ignore_phrases
        self._ignore = {normalize(p).lower() for p in phrases if normalize(p)}

    def reason_to_drop(self, text: str) -> str | None:
        """返回丢弃原因；应当保留时返回 None。"""
        core = normalize(text)
        if not core:
            return "无有效文字"
        if len(core) < self.min_chars:
            return "太短"
        low = core.lower()
        if low in self._ignore:
            return "语气词或常见误识别"
        if any(p in low for p in self._ignore if len(p) >= 4):
            return "常见误识别"
        return None
