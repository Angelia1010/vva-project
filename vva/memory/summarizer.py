"""分时段摘要：把每个已结束时段（默认 10 分钟）的画面记录浓缩成一段话，存入长期记忆。

查询“今天上午发生了什么”这类大范围问题时，先看摘要，比逐条看记录快得多。
服务重启后会补做最近 catchup_hours 小时内缺失的摘要。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from vva.agent import prompts
from vva.core.jsonutil import strip_think
from vva.memory.store import KIND_ALERT, KIND_OBSERVATION, MemoryStore, Summary
from vva.memory.timeutil import window_start

log = logging.getLogger(__name__)


async def complete_text(llm: Any, messages: list[dict]) -> str:
    """用 llm.stream 接口拿到一次完整的文字回答（不带工具）。"""
    parts = [val async for kind, val in llm.stream(messages, tools=None) if kind == "text"]
    return strip_think("".join(parts))


class Summarizer:
    def __init__(self, store: MemoryStore, llm: Any, summary_minutes: int = 10,
                 summary_catchup_hours: float = 24, summary_max_per_tick: int = 6,
                 summary_tick_s: float = 60, **_: Any) -> None:
        self.store, self.llm = store, llm
        self.minutes = max(1, int(summary_minutes))
        self.catchup = float(summary_catchup_hours) * 3600
        self.max_per_tick = int(summary_max_per_tick)
        self.tick = float(summary_tick_s)

    def pending_windows(self, now: float) -> list[float]:
        """已经结束、还没有摘要的时段起点（从近到远，最多 max_per_tick 个）。"""
        step = self.minutes * 60
        start = window_start(now, self.minutes) - step   # 最近一个已结束的时段
        oldest = now - self.catchup
        out: list[float] = []
        while start >= oldest and len(out) < self.max_per_tick:
            if not self.store.has_summary(start) and \
                    self.store.events_between(start, start + step - 1e-3, kinds=[KIND_OBSERVATION]):
                out.append(start)
            start -= step
        return out

    async def summarize(self, start: float) -> Summary | None:
        end = start + self.minutes * 60
        events = self.store.events_between(start, end - 1e-3, kinds=[KIND_OBSERVATION, KIND_ALERT])
        if not events:
            return None
        lines = "\n".join(f"{time.strftime('%H:%M:%S', time.localtime(e.ts))} {e.summary}"
                          + (f"（{e.people}人）" if e.people is not None else "") for e in events)
        fmt = "%Y-%m-%d %H:%M"
        prompt = prompts.SUMMARY_PROMPT.format(start=time.strftime(fmt, time.localtime(start)),
                                               end=time.strftime("%H:%M", time.localtime(end)), entries=lines)
        text = await complete_text(self.llm, [{"role": "system", "content": prompts.SUMMARY_SYSTEM},
                                              {"role": "user", "content": prompt}])
        if not text:
            return None
        s = Summary(start, end, text.strip())
        self.store.add_summary(s)
        log.info("生成时段摘要 %s：%s", time.strftime("%H:%M", time.localtime(start)), s.text)
        return s

    async def run(self) -> None:
        log.info("时段摘要已启动：每 %d 分钟一段", self.minutes)
        while True:
            try:
                for start in self.pending_windows(time.time()):
                    await self.summarize(start)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("生成时段摘要失败：%s", e)
            await asyncio.sleep(self.tick)
