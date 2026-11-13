"""技能相关的智能体工具：load_skill（加载技能）、compare_with_reference（对照参考图），以及各技能的专用工具。"""
from __future__ import annotations

import json
from typing import Any

from vva.agent import prompts
from vva.agent.toolkit import ToolProvider, fn_schema as _fn
from vva.core import turn
from vva.core.bus import EventBus
from vva.core.types import fmt_ts
from vva.perception.imaging import decode_jpeg, encode_jpeg, resize_max_side
from vva.perception.look import ask_look
from vva.skills.manager import LOADED_KEY, SkillManager
from vva.video.frame_buffer import FrameBuffer


class SkillTools(ToolProvider):
    def __init__(self, manager: SkillManager, buffer: FrameBuffer, vlm: Any, bus: EventBus,
                 look_max_side: int = 1280, look_max_tokens: int = 256, **_: Any) -> None:
        super().__init__(bus)
        self.mgr, self.buffer, self.vlm = manager, buffer, vlm
        self.look_max_side = int(look_max_side)
        self.look_max_tokens = int(look_max_tokens)

    def schemas(self) -> list[dict]:
        active = self.mgr.active()
        out: list[dict] = []
        loadable = [s.name for s in active if not s.always]
        if loadable:
            out.append(_fn("load_skill", "加载一个技能，读取它的做法、判断标准和特定目标说明。可用技能：" + "、".join(loadable),
                           {"name": {"type": "string", "description": "技能名称"}}, ["name"]))
        objects = [o.name for _, o in self.mgr.objects()]
        if objects:
            out.append(_fn("compare_with_reference",
                           "对照技能里的参考图，判断当前画面中某个特定目标的情况（是否出现、状态是否正常等）。可用目标："
                           + "、".join(objects),
                           {"target": {"type": "string", "description": "目标名称"},
                            "question": {"type": "string", "description": "要判断的具体问题，如“指示灯是什么颜色，是否正常”"}},
                           ["target", "question"]))
        for s in active:                       # 专用工具：是否可见由智能体按会话判断（加载后才可见）
            plugin = self.mgr.plugins.get(s.name)
            if plugin:
                out += plugin.schemas()
        return out

    def names(self) -> set[str]:
        # 工具集合在运行中会变化，这里返回全部可能的名称，供 ToolSet 检查重名
        return {"load_skill", "compare_with_reference",
                *(n for p in self.mgr.plugins.values() for n in p.names())}

    async def call(self, name: str, args: dict[str, Any]) -> str:
        owner = self.mgr.plugin_owner(name)
        if owner:
            if self.mgr.get_active(owner) is None:
                return json.dumps({"error": f"技能“{owner}”未启用"}, ensure_ascii=False)
            return await self.mgr.plugins[owner].call(name, args)
        return await super().call(name, args)

    # ------------------------------------------------------------------ tools
    async def tool_load_skill(self, name: str) -> dict:
        s = self.mgr.get_active(name)
        if s is None:
            names = "、".join(x.name for x in self.mgr.active()) or "无"
            return {"error": f"没有已启用的技能“{name}”，可用技能：{names}"}
        result: dict[str, Any] = {LOADED_KEY: s.name, "instructions": s.body or "（这个技能没有写正文）"}
        if s.objects:
            result["objects"] = [o.to_text() for o in s.objects.values()]
            result["note"] = "判断这些目标时用 compare_with_reference 对照参考图"
        plugin = self.mgr.plugins.get(s.name)
        if plugin:
            result["tools"] = sorted(plugin.names())
        return result

    async def tool_compare_with_reference(self, target: str, question: str) -> dict:
        found = self.mgr.find_object(target)
        if found is None:
            names = "、".join(o.name for _, o in self.mgr.objects()) or "无"
            return {"error": f"已启用的技能里没有“{target}”，可用目标：{names}"}
        skill, obj = found
        refs = self.mgr.references(skill, obj)
        bf = self.buffer.latest_snapshot(quality=92)
        if bf is None:
            return {"error": "暂时没有画面，请检查摄像头"}
        frame = encode_jpeg(resize_max_side(decode_jpeg(bf.jpeg), self.look_max_side), 90)
        turn.attach(frame, f"对照“{obj.name}”参考图")
        desc = obj.to_text()
        if refs:
            q = prompts.COMPARE_PROMPT.format(n_ref=len(refs), name=obj.name, desc=desc, question=question,
                                              refs="、".join(f"图{i}" for i in range(1, len(refs) + 1)))
        else:
            q = f"目标说明：{desc}\n问题：{question}"
        result = await ask_look(self.vlm, [*refs, frame], q, max_tokens=self.look_max_tokens)
        return {"time": fmt_ts(bf.ts), "target": obj.name, "skill": skill.name, "reference_images": len(refs), **result}
