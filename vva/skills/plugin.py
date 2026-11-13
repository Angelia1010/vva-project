"""技能专用工具的基类。技能目录里的 tools.py 定义一个名为 Tools 的类：

    from vva.skills.plugin import SkillToolProvider, fn_schema

    class Tools(SkillToolProvider):
        def schemas(self):
            return [fn_schema("pressure_status", "按本技能的标准判断压力是否正常",
                              {"mpa": {"type": "number"}}, ["mpa"])]

        async def tool_pressure_status(self, mpa: float) -> dict:
            return {"status": "正常" if 0.2 <= mpa <= 0.5 else "异常"}

这些工具只有在智能体用 load_skill 加载了该技能之后才可见。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from vva.agent.toolkit import ToolProvider, fn_schema
from vva.core.bus import EventBus
from vva.perception.imaging import decode_jpeg, encode_jpeg, resize_max_side
from vva.perception.look import ask_look

__all__ = ["SkillContext", "SkillToolProvider", "fn_schema"]


@dataclass
class SkillContext:
    skill: str                 # 技能名称
    skill_dir: str             # 技能目录（可放自己的数据文件）
    buffer: Any                # 帧缓存（FrameBuffer）
    vlm: Any                   # 看图模型
    bus: EventBus

    def current_frame(self, max_side: int = 1280) -> bytes | None:
        bf = self.buffer.latest_snapshot(quality=92) if self.buffer else None
        return encode_jpeg(resize_max_side(decode_jpeg(bf.jpeg), max_side), 90) if bf else None

    async def look(self, images: list[bytes], question: str) -> dict[str, str]:
        """用看图模型回答问题，返回 {evidence, answer, confidence}。"""
        return await ask_look(self.vlm, images, question)


class SkillToolProvider(ToolProvider):
    def __init__(self, ctx: SkillContext) -> None:
        super().__init__(ctx.bus)
        self.ctx = ctx
