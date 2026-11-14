"""告警服务：新建告警（落库 + 截图）、记录处理经过、更新处理状态、统计。

只负责“记下来、改状态”，不负责发给谁（见 vva.notify）。每次变化都向事件总线发 UI 事件，值守台实时刷新。
"""
from __future__ import annotations

import logging
import time
from typing import Any

from vva.alerts.model import ACTION_TEXT, ACTIONS, OPEN, Alert
from vva.alerts.store import AlertStore
from vva.core.bus import TOPIC_UI, EventBus

log = logging.getLogger(__name__)


def day_start(now: float | None = None) -> float:
    t = time.localtime(now or time.time())
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


class AlertService:
    def __init__(self, store: AlertStore, bus: EventBus) -> None:
        self.store = store
        self.bus = bus

    def create(self, duty: Any, evidence: str, images: list[tuple[str, bytes]], status: str = "pending",
               box: list[float] | None = None, kind: str | None = None, ts: float | None = None) -> Alert:
        alert = Alert(id=0, ts=ts or time.time(), duty_id=duty.id, duty_name=duty.name, condition=duty.condition,
                      kind=kind or duty.kind, level=duty.level, status=status, evidence=evidence, box=box,
                      targets=duty.effective_targets(), rule_ref=duty.rule_ref)
        self.store.insert(alert)
        for caption, jpeg in images:
            if jpeg:
                self.store.save_image(alert, jpeg, caption)
        self.store.save(alert)
        if status not in ("record",):
            self.bus.publish(TOPIC_UI, {"type": "alert_new", "session": None, "alert": alert.to_dict()})
        return alert

    def add_event(self, alert_id: int, text: str, actor: str = "系统") -> None:
        self.store.add_event(alert_id, actor, text)
        self.bus.publish(TOPIC_UI, {"type": "alert_update", "session": None, "id": alert_id})

    def act(self, alert_id: int, action: str, actor: str = "", note: str = "") -> Alert:
        alert = self.store.get(alert_id)
        if alert is None:
            raise KeyError(f"没有告警 {alert_id}")
        if action not in ACTIONS:
            raise ValueError(f"不支持的操作：{action}")
        alert.status = ACTIONS[action]
        if alert.status in ("resolved", "false"):
            alert.handler, alert.handled_ts = actor or "值班员", time.time()
        if note:
            alert.note = note
        self.store.save(alert)
        self.store.add_event(alert_id, actor or "值班员", ACTION_TEXT[action] + (f"：{note}" if note else ""))
        self.bus.publish(TOPIC_UI, {"type": "alert_update", "session": None, "id": alert_id, "alert": alert.to_dict()})
        return alert

    def delete(self, alert_ids: list[int]) -> int:
        """删除告警（含截图和处理经过），返回删除条数。"""
        n = sum(self.store.delete(int(i)) for i in alert_ids)
        if n:
            self.bus.publish(TOPIC_UI, {"type": "alert_update", "session": None, "deleted": list(alert_ids)})
        return n

    def detail(self, alert_id: int) -> dict[str, Any] | None:
        alert = self.store.get(alert_id)
        if alert is None:
            return None
        events = [{**e, "time": time.strftime("%H:%M:%S", time.localtime(e["ts"]))} for e in self.store.events(alert_id)]
        return {**alert.to_dict(), "events": events}

    def list(self, **kw: Any) -> list[dict[str, Any]]:
        return [a.to_dict() for a in self.store.query(**kw)]

    # ---------------------------------------------------------------- 统计
    def duty_today(self) -> dict[str, dict[str, Any]]:
        """每条职责今天的告警数（不含巡检正常记录）和最近一次。"""
        out: dict[str, dict[str, Any]] = {}
        for duty_id, by in self.store.counts(day_start()).items():
            out[duty_id] = {"count": sum(n for s, n in by.items() if s != "record"), "by_status": by}
        return out

    def stats(self) -> dict[str, Any]:
        today = day_start()
        by = {}
        for d in self.store.counts(today).values():
            for s, n in d.items():
                by[s] = by.get(s, 0) + n
        week = self.store.query(start=today - 6 * 86400, limit=5000)
        judged = [a for a in week if a.status in ("resolved", "false")]
        handled = [a for a in week if a.handled_ts and a.status == "resolved"]
        return {
            "today": sum(n for s, n in by.items() if s not in ("record", "trial")),
            "pending": by.get("pending", 0) + by.get("handling", 0),
            "false_today": by.get("false", 0),
            "trial_today": by.get("trial", 0),
            "false_rate_week": round(sum(a.status == "false" for a in judged) / len(judged), 3) if judged else None,
            "avg_response_s": round(sum(a.handled_ts - a.ts for a in handled) / len(handled)) if handled else None,
        }

    def open_alerts(self, limit: int = 20) -> list[dict[str, Any]]:
        return [a.to_dict() for a in self.store.query(statuses=OPEN, limit=limit)]
