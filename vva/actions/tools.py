"""智能体的执行工具：查看有哪些动作、按编号执行。"""
from __future__ import annotations

from typing import Any

from vva.agent.toolkit import ToolProvider
from vva.agent.toolkit import fn_schema as _fn
from vva.core import turn
from vva.core.bus import EventBus


class ActionTools(ToolProvider):
    def __init__(self, service: Any, bus: EventBus) -> None:
        super().__init__(bus)
        self.s = service

    def schemas(self) -> list[dict]:
        return [
            _fn("list_actions", "列出可以执行的动作及其编号、是否可用。", {}, []),
            _fn("run_action", "执行一个动作。用于“执行 3 号”“3 号”“喊话让他们离开”“抓拍一张”等明确的要求。",
                {"code": {"type": "string", "description": "动作编号（如 3）或名称"},
                 "text": {"type": "string", "description": "喇叭要播报或通知要发送的内容，可选；不填用预设内容"},
                 "confirm": {"type": "boolean", "description": "高风险动作在用户明确确认之后才传 true"}},
                ["code"]),
        ]

    async def tool_list_actions(self) -> dict:
        return {"actions": [{"code": a["code"], "name": a["name"], "what": a["summary"], "needs_confirm": a["danger"],
                             "ready": a["ready"], "problem": a["problem"]} for a in self.s.list() if a["enabled"]]}

    async def tool_run_action(self, code: Any, text: str = "", confirm: bool = False, **_: Any) -> dict:
        a = self.s.find(code)
        if a is None:
            return {"error": f"没有“{code}”这个动作，可用的编号用 list_actions 查"}
        session = turn.current_session() or "web"
        problem = self.s.problem(a)
        if not problem and self.s.needs_confirm(a, session, turn.current_turn(), bool(confirm)):
            return {"need_confirm": True, "code": a.code, "name": a.name,
                    "say": f"{a.code} 号“{a.name}”是高风险动作（{a.summary or a.name}）。请向用户复述并询问是否确认执行；"
                           "用户明确确认后再带 confirm=true 调用。现在还没有执行。"}
        source = "voice" if session == "web" else session.split(":")[0]
        return await self.s.run(a.code, actor=f"值班员（{'语音/网页' if session == 'web' else session}）", source=source, text=text)
