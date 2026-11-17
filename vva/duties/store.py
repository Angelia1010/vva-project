"""职责的持久化（data/ops.db 的 duties、duty_changes 两张表）与内存中的职责清单 DutyBook。

DutyBook 同时提供旧“监控规则”的接口（add / remove / get / list / mark_triggered / on_change），
后台观察、技能、旧代码不用改就能使用。
"""
from __future__ import annotations

import itertools
import json
import logging
import time
from collections.abc import Callable
from typing import Any

from vva.core.sqlite import SqliteDB
from vva.duties.model import Duty

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS duties (
    id   TEXT PRIMARY KEY,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS duty_changes (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    duty_id TEXT NOT NULL,
    actor   TEXT NOT NULL,
    action  TEXT NOT NULL,
    detail  TEXT
);
"""

EDITABLE = ("name", "condition", "kind", "enabled", "mode", "temporary", "expires", "hours", "weekdays", "times",
            "level", "targets", "voice_text", "verify", "min_duration_s", "cooldown_s", "objects", "rule_ref", "negatives")


class DutyStore:
    def __init__(self, db: SqliteDB) -> None:
        self.db = db
        db.script(SCHEMA)

    def load(self) -> list[Duty]:
        return [Duty.from_record(json.loads(r["data"])) for r in self.db.query("SELECT data FROM duties ORDER BY rowid")]

    def save(self, duty: Duty) -> None:
        self.db.execute("INSERT OR REPLACE INTO duties(id, data) VALUES (?, ?)",
                        (duty.id, json.dumps(duty.to_record(), ensure_ascii=False)))

    def delete(self, duty_id: str) -> None:
        self.db.execute("DELETE FROM duties WHERE id = ?", (duty_id,))

    def log_change(self, duty_id: str, actor: str, action: str, detail: str = "") -> None:
        self.db.execute("INSERT INTO duty_changes(ts, duty_id, actor, action, detail) VALUES (?, ?, ?, ?, ?)",
                        (time.time(), duty_id, actor or "系统", action, detail))

    def changes(self, duty_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if duty_id:
            return self.db.query("SELECT * FROM duty_changes WHERE duty_id = ? ORDER BY id DESC LIMIT ?", (duty_id, limit))
        return self.db.query("SELECT * FROM duty_changes ORDER BY id DESC LIMIT ?", (limit,))


class DutyBook:
    """职责清单（内存 + 可选持久化）。修改后自动保存并通知监听者。"""

    def __init__(self, store: DutyStore | None = None) -> None:
        self.store = store
        self._duties: dict[str, Duty] = {}
        self._listeners: list[Callable[[list[Duty]], None]] = []
        self._snooze: dict[str, float] = {}           # 复核不成立后短暂不再判断
        self.paused = False                           # 岗位暂停值守时，所有职责都不判断
        max_id = 0
        if store:
            for d in store.load():
                self._duties[d.id] = d
                if d.id[1:].isdigit():
                    max_id = max(max_id, int(d.id[1:]))
        self._ids = itertools.count(max_id + 1)

    # ---------------------------------------------------------------- 监听
    def on_change(self, fn: Callable[[list[Duty]], None]) -> None:
        self._listeners.append(fn)

    def _changed(self) -> None:
        for fn in self._listeners:
            try:
                fn(self.list())
            except Exception:
                log.exception("职责变更通知失败")

    def _save(self, duty: Duty) -> None:
        if self.store:
            self.store.save(duty)

    def _log(self, duty_id: str, actor: str, action: str, detail: str = "") -> None:
        if self.store:
            self.store.log_change(duty_id, actor, action, detail)

    # ---------------------------------------------------------------- 增删改查
    def create(self, actor: str = "", **fields: Any) -> Duty:
        fields = {k: v for k, v in fields.items() if k in EDITABLE or k in ("source", "created_by")}
        name = str(fields.pop("name", "") or "").strip()
        condition = str(fields.pop("condition", "") or "").strip()
        if not condition:
            raise ValueError("请填写要看什么")
        duty = Duty(id=f"d{next(self._ids)}", name=name or condition[:12], condition=condition,
                    created_by=fields.pop("created_by", actor), **fields)
        self._duties[duty.id] = duty
        self._save(duty)
        self._log(duty.id, actor, "添加", f"{duty.name}：{duty.condition}")
        self._changed()
        return duty

    def update(self, duty_id: str, actor: str = "", **changes: Any) -> Duty:
        duty = self._duties.get(duty_id)
        if duty is None:
            raise KeyError(f"没有职责 {duty_id}")
        diff = []
        for k, v in changes.items():
            if k in EDITABLE and getattr(duty, k) != v:
                diff.append(k)
                setattr(duty, k, v)
        if diff:
            self._save(duty)
            self._log(duty_id, actor, "修改", "、".join(diff))
            self._changed()
        return duty

    def remove(self, duty_id: str, actor: str = "") -> bool:
        duty = self._duties.pop(duty_id, None)
        if duty is None:
            return False
        if self.store:
            self.store.delete(duty_id)
        self._log(duty_id, actor, "删除", duty.name)
        self._changed()
        return True

    def get(self, duty_id: str) -> Duty | None:
        return self._duties.get(duty_id)

    def list(self) -> list[Duty]:
        return list(self._duties.values())

    def active(self, now: float | None = None) -> list[Duty]:
        """后台观察这一轮要判断的盯守类职责。"""
        if self.paused:
            return []
        now = now or time.time()
        return [d for d in self._duties.values() if d.active(now) and self._snooze.get(d.id, 0) <= now]

    def purge_expired(self, now: float | None = None) -> list[Duty]:
        """删除到期的临时任务，返回被删除的。"""
        gone = [d for d in self._duties.values() if d.expired(now)]
        for d in gone:
            self._duties.pop(d.id, None)
            if self.store:
                self.store.delete(d.id)
            self._log(d.id, "系统", "到期", d.name)
        if gone:
            self._changed()
        return gone

    def snooze(self, duty_id: str, seconds: float) -> None:
        self._snooze[duty_id] = time.time() + seconds

    def cooling(self, duty_id: str, now: float | None = None) -> bool:
        d = self._duties.get(duty_id)
        return bool(d and (now or time.time()) - d.last_triggered < d.cooldown_s)

    def mark_triggered(self, duty_id: str, now: float | None = None) -> bool:
        """命中后返回 True；冷却期内重复命中返回 False。"""
        d = self._duties.get(duty_id)
        if d is None:
            return False
        now = now or time.time()
        if now - d.last_triggered < d.cooldown_s:
            return False
        d.last_triggered = now
        d.trigger_count += 1
        self._save(d)
        return True

    def touch(self, duty: Duty) -> None:
        """保存运行状态（巡检结果、执行时间），不记变更、不通知。"""
        self._save(duty)

    # ---------------------------------------------------------------- 兼容旧的监控规则接口
    def add(self, condition: str, cooldown_s: float = 60.0, **fields: Any) -> Duty:
        fields.setdefault("source", "skill")
        return self.create(actor=fields.pop("actor", "系统"), condition=condition,
                           cooldown_s=max(5.0, float(cooldown_s)), **fields)

    def load(self, items: list[dict[str, Any]]) -> int:
        """导入旧版监控规则（只在职责清单为空时使用），返回导入条数。"""
        n = 0
        for r in items:
            if r.get("condition"):
                self.create(actor="系统", condition=str(r["condition"]), cooldown_s=float(r.get("cooldown_s") or 60),
                            source="legacy")
                n += 1
        return n

    def __bool__(self) -> bool:
        return bool(self.active())

    def __len__(self) -> int:
        return len(self._duties)
