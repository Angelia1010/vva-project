"""智能体可调用的职责工具：查看职责、添加临时任务、暂停/恢复、查告警。

值班员通过语音或钉钉/飞书只能添加“临时任务”（默认当天有效，通知回下达任务的地方）；
长期职责在网页上由管理员配置。
"""
from __future__ import annotations

import time
from typing import Any

from vva.agent.toolkit import ToolProvider, fn_schema as _fn
from vva.alerts.model import STATUS
from vva.alerts.service import day_start
from vva.core import turn
from vva.core.bus import EventBus
from vva.core.types import fmt_ts

WEB_SESSIONS = {"web", None}


def targets_for_session(session: str | None) -> list[str]:
    """临时任务的通知对象：网页下达的回到值守台并播报，钉钉/飞书下达的发回原会话。"""
    if session in WEB_SESSIONS or ":" not in str(session):
        return ["web", "voice"]
    return [str(session), "web"]


def end_of_task(hours: float = 0, until: str = "", now: float | None = None) -> float:
    now = now or time.time()
    if hours and float(hours) > 0:
        return now + float(hours) * 3600
    if until:
        try:
            h, m = (int(x) for x in until.split(":"))
            t = time.localtime(now)
            end = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, h, m, 0, 0, 0, -1))
            return end if end > now else end + 86400
        except ValueError:
            pass
    return day_start(now) + 86400          # 默认今天 24:00


class DutyTools(ToolProvider):
    def __init__(self, manager: Any, bus: EventBus) -> None:
        super().__init__(bus)
        self.m = manager

    def schemas(self) -> list[dict]:
        did = {"type": "string", "description": "职责编号，如 d3（先用 list_duties 查）"}
        return [
            _fn("list_duties", "列出岗位上的全部职责和临时任务，以及各自今天的告警次数。", {}, []),
            _fn("add_task", "添加一个临时任务：条件满足时提醒。用于“……就提醒我 / 就告诉我 / 就通知我”。默认今天有效。",
                {"condition": {"type": "string", "description": "能从单张画面判断的描述，如“有快递员或快递车到门口”"},
                 "name": {"type": "string", "description": "简短名称，4~8 个字，可选"},
                 "hours": {"type": "number", "description": "有效多少小时，可选；不填则到今天结束"},
                 "until": {"type": "string", "description": "有效到几点，如 18:00，可选"}},
                ["condition"]),
            _fn("pause_duty", "暂停一项职责或临时任务。", {"duty_id": did}, ["duty_id"]),
            _fn("resume_duty", "恢复一项已暂停的职责或临时任务。", {"duty_id": did}, ["duty_id"]),
            _fn("remove_task", "取消一个临时任务（长期职责只能在网页上删除）；传 all 取消全部临时任务。",
                {"duty_id": did}, ["duty_id"]),
            _fn("query_alerts", "查询告警记录：某项职责最近的告警次数、时间、依据、处理状态。",
                {"duty": {"type": "string", "description": "职责名称或编号，可选；不填查全部"},
                 "hours": {"type": "number", "description": "最近多少小时，默认查今天"}}, []),
        ]

    def _find(self, key: str) -> Any:
        book = self.m.book
        d = book.get(key)
        if d is None:
            d = next((x for x in book.list() if x.name == key or key in x.name), None)
        return d

    async def tool_list_duties(self) -> dict:
        items = self.m.status_list()
        return {"duties": [_brief(d) for d in items if not d["temporary"]],
                "temporary": [_brief(d) for d in items if d["temporary"]]}

    async def tool_add_task(self, condition: str, name: str = "", hours: float = 0, until: str = "", **_: Any) -> dict:
        session = turn.current_session()
        duty = self.m.book.create(actor=f"值班员（{session or '网页'}）", name=name or condition[:10], condition=condition,
                                  temporary=True, expires=end_of_task(hours, until), targets=targets_for_session(session),
                                  source="voice" if session in WEB_SESSIONS else str(session).split(":")[0],
                                  cooldown_s=300, verify=True)
        return {"ok": True, "id": duty.id, "name": duty.name, "condition": duty.condition,
                "until": fmt_ts(duty.expires), "notify": self.m.target_labels(duty.effective_targets())}

    async def _set_enabled(self, duty_id: str, enabled: bool) -> dict:
        d = self._find(duty_id)
        if d is None:
            return {"error": f"没有找到职责“{duty_id}”"}
        self.m.book.update(d.id, actor=f"值班员（{turn.current_session() or '网页'}）", enabled=enabled)
        return {"ok": True, "id": d.id, "name": d.name, "enabled": enabled}

    async def tool_pause_duty(self, duty_id: str) -> dict:
        return await self._set_enabled(duty_id, False)

    async def tool_resume_duty(self, duty_id: str) -> dict:
        return await self._set_enabled(duty_id, True)

    async def tool_remove_task(self, duty_id: str) -> dict:
        actor = f"值班员（{turn.current_session() or '网页'}）"
        if duty_id == "all":
            gone = [d for d in self.m.book.list() if d.temporary]
            for d in gone:
                self.m.book.remove(d.id, actor)
            return {"ok": True, "removed": [d.name for d in gone]}
        d = self._find(duty_id)
        if d is None:
            return {"error": f"没有找到“{duty_id}”"}
        if not d.temporary:
            return {"error": f"“{d.name}”是长期职责，需要管理员在网页上删除；可以先暂停"}
        self.m.book.remove(d.id, actor)
        return {"ok": True, "removed": [d.name]}

    async def tool_query_alerts(self, duty: str = "", hours: float = 0, **_: Any) -> dict:
        start = time.time() - float(hours) * 3600 if hours else day_start()
        d = self._find(duty) if duty else None
        if duty and d is None:
            return {"error": f"没有找到职责“{duty}”"}
        alerts = [a for a in self.m.alerts.store.query(start=start, duty_id=d.id if d else "", limit=200)
                  if a.status != "record"]
        return {"from": fmt_ts(start), "duty": d.name if d else "全部", "count": len(alerts),
                "recent": [{"time": fmt_ts(a.ts), "duty": a.duty_name, "evidence": a.evidence,
                            "status": STATUS.get(a.status, a.status)} for a in alerts[:5]]}


def _brief(d: dict[str, Any]) -> dict[str, Any]:
    return {"id": d["id"], "name": d["name"], "type": d["kind_label"], "when": d["schedule"],
            "notify": d["target_text"], "state": d["state_text"], "today": d["today_count"],
            **({"until": d["expires_text"]} if d["temporary"] else {})}
