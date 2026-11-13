"""基于长期记忆生成一段时间的监控报告（供飞书定时报告等使用）。"""
from __future__ import annotations

import time
from typing import Any

from vva.agent import prompts
from vva.memory.store import KIND_ALERT, KIND_COMMAND, KIND_OBSERVATION, MemoryStore
from vva.memory.summarizer import complete_text


def _hm(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


async def build_report(store: MemoryStore, llm: Any, start: float, end: float, max_chars: int = 300) -> str:
    summaries = store.summaries_between(start, end)
    events = store.events_between(start, end, kinds=[KIND_ALERT, KIND_COMMAND])
    if not summaries:
        # 摘要还没生成（如时段太短）：用画面记录代替，均匀取 20 条
        obs = store.events_between(start, end, kinds=[KIND_OBSERVATION])
        step = max(1, len(obs) // 20)
        summary_lines = [f"{_hm(e.ts)} {e.summary}" for e in obs[::step]]
    else:
        summary_lines = [f"{_hm(s.start)}-{_hm(s.end)} {s.text}" for s in summaries]
    if not summary_lines and not events:
        return "这段时间没有监控记录。"
    fmt = "%m-%d %H:%M"
    prompt = prompts.REPORT_PROMPT.format(
        start=time.strftime(fmt, time.localtime(start)), end=time.strftime(fmt, time.localtime(end)),
        summaries="\n".join(summary_lines) or "无", events="\n".join(f"{_hm(e.ts)} {e.summary}" for e in events) or "无",
        max_chars=max_chars)
    return await complete_text(llm, [{"role": "system", "content": prompts.REPORT_SYSTEM},
                                     {"role": "user", "content": prompt}])
