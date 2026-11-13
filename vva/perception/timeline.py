"""画面时间线与监控规则（纯数据，不依赖模型）。"""
from __future__ import annotations

import itertools
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any

from vva.core.types import fmt_ts


@dataclass
class TimelineEntry:
    ts: float
    summary: str
    people_count: int | None = None
    objects: list[str] = field(default_factory=list)
    activities: list[str] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)

    def to_text(self) -> str:
        parts = [f"[{fmt_ts(self.ts)}] {self.summary}"]
        if self.people_count is not None:
            parts.append(f"人数:{self.people_count}")
        if self.activities:
            parts.append("动作:" + "、".join(self.activities))
        if self.anomalies:
            parts.append("异常:" + "、".join(self.anomalies))
        return " | ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["time"] = fmt_ts(self.ts)
        return d


class Timeline:
    def __init__(self, max_minutes: float = 120) -> None:
        self.max_seconds = max_minutes * 60
        self._items: deque[TimelineEntry] = deque()

    def add(self, entry: TimelineEntry) -> None:
        self._items.append(entry)
        cutoff = entry.ts - self.max_seconds
        while self._items and self._items[0].ts < cutoff:
            self._items.popleft()

    def query(self, minutes: float) -> list[TimelineEntry]:
        cutoff = time.time() - minutes * 60
        return [e for e in self._items if e.ts >= cutoff]

    def latest(self) -> TimelineEntry | None:
        return self._items[-1] if self._items else None

    def __len__(self) -> int:
        return len(self._items)


@dataclass
class WatchRule:
    id: str
    condition: str
    cooldown_s: float = 60.0
    created: float = field(default_factory=time.time)
    last_triggered: float = 0.0
    trigger_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "condition": self.condition, "cooldown_s": self.cooldown_s,
                "trigger_count": self.trigger_count,
                "last_triggered": fmt_ts(self.last_triggered) if self.last_triggered else None}


class WatchRules:
    def __init__(self) -> None:
        self._rules: dict[str, WatchRule] = {}
        self._ids = itertools.count(1)

    def add(self, condition: str, cooldown_s: float = 60.0) -> WatchRule:
        rid = f"r{next(self._ids)}"
        rule = WatchRule(id=rid, condition=condition.strip(), cooldown_s=max(5.0, float(cooldown_s)))
        self._rules[rid] = rule
        return rule

    def remove(self, rule_id: str) -> bool:
        return self._rules.pop(rule_id, None) is not None

    def get(self, rule_id: str) -> WatchRule | None:
        return self._rules.get(rule_id)

    def list(self) -> list[WatchRule]:
        return list(self._rules.values())

    def mark_triggered(self, rule_id: str, now: float | None = None) -> bool:
        """命中后返回 True；冷却期内重复命中返回 False，避免反复提醒。"""
        rule = self._rules.get(rule_id)
        if rule is None:
            return False
        now = now or time.time()
        if now - rule.last_triggered < rule.cooldown_s:
            return False
        rule.last_triggered = now
        rule.trigger_count += 1
        return True

    def __bool__(self) -> bool:
        return bool(self._rules)
