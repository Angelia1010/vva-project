"""主智能体用的检索工具：search_video（交给搜索子智能体）、control_search_result（语音操作打开的结果）。"""
from __future__ import annotations

import os
from typing import Any

from vva.agent.toolkit import ToolProvider
from vva.agent.toolkit import fn_schema as _fn
from vva.core import turn
from vva.core.bus import TOPIC_UI, EventBus

ACTIONS = {"play": "播放", "zoom": "放大目标", "unzoom": "恢复原大小", "close": "关闭结果面板"}


class SearchTools(ToolProvider):
    def __init__(self, service: Any, bus: EventBus) -> None:
        super().__init__(bus)
        self.s = service

    def schemas(self) -> list[dict]:
        session = turn.current_session() or "web"
        if not self.s.enabled or not self.s.channel_allowed(session):
            return []
        out = [_fn("search_video",
                   "在录像里检索过去发生的事，返回具体片段（哪一路、几点几分、画面里的位置），并在值守台打开结果。"
                   "用于“昨天下午有没有人抽烟”“找一下叉车经过的录像”“上次有人翻围栏是什么时候”。",
                   {"question": {"type": "string", "description": "要找什么，用用户的原话，可以带时间，如“昨天下午仓库有人抽烟”"},
                    "camera": {"type": "string", "description": "可选，只搜哪一路（名称或编号）"},
                    "start": {"type": "string", "description": "可选，开始时间 YYYY-MM-DD HH:MM；不填由问题推断"},
                    "end": {"type": "string", "description": "可选，结束时间 YYYY-MM-DD HH:MM"}},
                   ["question"])]
        if session == "web" and self.s.open_by_session.get(session):
            out.append(_fn("control_search_result", "操作值守台上已经打开的检索结果：播放第几个、放大看目标、恢复、关闭。",
                           {"index": {"type": "integer", "description": "第几个结果，从 1 开始；关闭时可不填"},
                            "action": {"type": "string", "enum": list(ACTIONS), "description": "play 播放 / zoom 放大 / unzoom 恢复 / close 关闭"}},
                           ["action"]))
        return out

    async def tool_search_video(self, question: str, camera: str = "", start: str = "", end: str = "", **_: Any) -> dict:
        session = turn.current_session() or "web"
        r = await self.s.run(question, session, camera=camera, start=start, end=end, focus=turn.current_camera())
        out: dict[str, Any] = {"summary": r["summary"], "found": r["found"],
                               "hits": [{"no": i + 1, "where": h["camera_label"], "time": h["time"], "evidence": h["evidence"]}
                                        for i, h in enumerate(r["hits"])]}
        if r.get("note"):
            out["note"] = r["note"]
        if not r["found"]:
            out["tip"] = "可以建议用户扩大时间范围或换个说法"
        elif session == "web":
            out["shown"] = "结果已在值守台右侧打开，第 1 个已开始播放"
        else:
            base = self.s.link_base(session)
            if base:
                out["link"] = f"{base.rstrip('/')}/recording?search={r['search_id']}"
            p = self.s.thumb_path(r["search_id"], 0)
            if os.path.isfile(p):                       # 钉钉、飞书里随回答发出第一处的画面
                with open(p, "rb") as f:
                    turn.attach(f.read(), f"{r['hits'][0]['camera_label']} {r['hits'][0]['time']}")
        return out

    async def tool_control_search_result(self, action: str, index: int = 1, **_: Any) -> dict:
        session = turn.current_session() or "web"
        sid = self.s.open_by_session.get(session)
        if not sid:
            return {"error": "现在没有打开的检索结果"}
        if action not in ACTIONS:
            return {"error": f"不支持的操作：{action}"}
        r = self.s.get(sid) or {}
        hits = (r.get("result") or {}).get("hits", [])
        i = int(index or 1)
        if action != "close" and not 1 <= i <= len(hits):
            return {"error": f"只有 {len(hits)} 个结果"}
        if action == "close":
            self.s.open_by_session.pop(session, None)
        self.bus.publish(TOPIC_UI, {"type": "search_control", "session": session, "search_id": sid, "index": i - 1, "action": action})
        return {"ok": True, "done": ACTIONS[action] + ("" if action == "close" else f"第 {i} 个")}
