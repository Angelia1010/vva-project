"""长期记忆相关的智能体工具：按时间段查询记录、查看当时保存的画面、备忘的增删。"""
from __future__ import annotations

import time
from typing import Any

from vva.agent.toolkit import ToolProvider, fn_schema as _fn
from vva.core import turn
from vva.core.bus import EventBus
from vva.memory.keyframes import KeyframeStore
from vva.memory.store import KIND_OBSERVATION, MemoryEvent, MemoryStore
from vva.memory.timeutil import fmt_dt, parse_time
from vva.perception.look import ask_look

_TIME_DESC = "格式 YYYY-MM-DD HH:MM，例如 2026-11-13 08:45；今天的时间也可以只写 HH:MM"


def _sample(items: list, n: int) -> list:
    """均匀抽取 n 条，保留首尾，控制返回给模型的长度。"""
    if len(items) <= n:
        return items
    step = (len(items) - 1) / (n - 1)
    return [items[round(i * step)] for i in range(n)]


def _event_text(e: MemoryEvent, now: float) -> str:
    people = f" | {e.people}人" if e.people is not None else ""
    return f"[{fmt_dt(e.ts, now)}] {e.summary}{people}"


class MemoryTools(ToolProvider):
    def __init__(self, store: MemoryStore, keyframes: KeyframeStore, vlm: Any, bus: EventBus,
                 look_max_gap_s: float = 300, max_events: int = 40, max_notes: int = 50,
                 look_max_tokens: int = 256, scene_hint: str = "", **_: Any) -> None:
        super().__init__(bus)
        self.store, self.keyframes, self.vlm = store, keyframes, vlm
        self.max_gap = float(look_max_gap_s)
        self.max_events = max(2, int(max_events))
        self.max_notes = int(max_notes)
        self.look_max_tokens = int(look_max_tokens)
        self.scene_hint = scene_hint

    def note_lines(self) -> list[str]:
        """给智能体系统提示词用的备忘列表。"""
        return [f"[n{n.id}] {n.content}" for n in self.store.list_notes(self.max_notes)]

    # ------------------------------------------------------------------ schema
    def schemas(self) -> list[dict]:
        t = {"type": "string", "description": _TIME_DESC}
        return [
            _fn("search_memory", "按时间段查询长期监控记录（可查到几天前），返回时段摘要、画面记录、提醒和操作员指令。"
                "适合回答“今天上午9点左右发生了什么”“昨天下午有没有人来过”。",
                {"start": t, "end": t,
                 "keyword": {"type": "string", "description": "可选，只看包含该关键词的记录，如“快递”“离开”"}},
                ["start", "end"]),
            _fn("look_at_time", "查看长期记忆中某个时刻保存的画面并回答问题，用于记录里的文字不够详细时。",
                {"time": t, "question": {"type": "string", "description": "要在那张画面里弄清楚的问题"}},
                ["time", "question"]),
            _fn("remember_note", "记下操作员交代需要长期记住的信息，以后回答时会自动参考。",
                {"content": {"type": "string", "description": "要记住的内容，如“3 号工位是张工的”"}}, ["content"]),
            _fn("forget_note", "删除一条备忘。",
                {"note_id": {"type": "string", "description": "备忘编号，如 n3"}}, ["note_id"]),
        ]

    # ------------------------------------------------------------------ tools
    async def tool_search_memory(self, start: str, end: str, keyword: str | None = None) -> dict:
        now = time.time()
        try:
            t0, t1 = parse_time(start, now), parse_time(end, now)
        except ValueError as e:
            return {"error": str(e)}
        if t1 < t0:
            t0, t1 = t1, t0
        summaries = [] if keyword else self.store.summaries_between(t0, t1)
        events = self.store.events_between(t0, t1, keyword=keyword or None)
        result: dict[str, Any] = {"range": f"{fmt_dt(t0, now)} ~ {fmt_dt(t1, now)}"}
        if summaries:
            result["summaries"] = [f"{fmt_dt(s.start, now)[:-3]}-{time.strftime('%H:%M', time.localtime(s.end))} "
                                   f"{s.text}" for s in summaries]
        others = [e for e in events if e.kind != KIND_OBSERVATION]   # 提醒、指令全部保留
        observations = _sample([e for e in events if e.kind == KIND_OBSERVATION],
                               max(2, self.max_events - len(others)))
        merged = sorted(others + observations, key=lambda e: e.ts)
        result["records"] = [_event_text(e, now) for e in merged]
        if not merged and not summaries:
            span = self.store.time_range()
            result["note"] = "这段时间没有记录" + (
                f"；现有记录范围 {fmt_dt(span[0], now)} ~ {fmt_dt(span[1], now)}" if span else "；长期记忆暂无记录")
        elif len(events) > len(merged):
            result["note"] = f"共 {len(events)} 条记录，已均匀抽取 {len(merged)} 条"
        return result

    async def tool_look_at_time(self, time: str, question: str) -> dict:  # noqa: A002  参数名与工具定义一致
        now = _now()
        try:
            ts = parse_time(time, now)
        except ValueError as e:
            return {"error": str(e)}
        ev = self.store.nearest_frame(ts, self.max_gap)
        if ev is None:
            return {"error": f"{fmt_dt(ts, now)} 前后 {int(self.max_gap // 60)} 分钟内没有保存画面"}
        jpeg = self.keyframes.load(ev.frame) if ev.frame else None
        if jpeg is None:
            return {"error": "当时的画面文件已被清理"}
        turn.attach(jpeg, f"{fmt_dt(ev.ts, now)} 的画面")
        result = await ask_look(self.vlm, [jpeg], question, self.scene_hint, self.look_max_tokens)
        return {"time": fmt_dt(ev.ts, now), "record": ev.summary, **result}

    async def tool_remember_note(self, content: str) -> dict:
        if not content or not content.strip():
            return {"error": "备忘内容为空"}
        note = self.store.add_note(content)
        return {"ok": True, "note_id": f"n{note.id}", "content": note.content}

    async def tool_forget_note(self, note_id: str) -> dict:
        digits = str(note_id).lstrip("nN")
        if not digits.isdigit():
            return {"error": f"备忘编号格式不对：{note_id}"}
        return {"ok": self.store.delete_note(int(digits))}


def _now() -> float:
    return time.time()
