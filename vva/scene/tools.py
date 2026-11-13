"""场景包相关的智能体工具：对照参考图判断现场的特定目标。"""
from __future__ import annotations

from typing import Any

from vva.agent import prompts
from vva.agent.toolkit import ToolProvider, fn_schema as _fn
from vva.core import turn
from vva.core.bus import EventBus
from vva.core.types import fmt_ts
from vva.perception.imaging import decode_jpeg, encode_jpeg, resize_max_side
from vva.perception.look import ask_look
from vva.scene.context import SceneContext
from vva.video.frame_buffer import FrameBuffer


class SceneTools(ToolProvider):
    def __init__(self, ctx: SceneContext, buffer: FrameBuffer, vlm: Any, bus: EventBus,
                 look_max_side: int = 1280, look_max_tokens: int = 256, **_: Any) -> None:
        super().__init__(bus)
        self.ctx, self.buffer, self.vlm = ctx, buffer, vlm
        self.look_max_side = int(look_max_side)
        self.look_max_tokens = int(look_max_tokens)

    def schemas(self) -> list[dict]:
        if not self.ctx.scene.objects:      # 场景包里没有目标时不提供这个工具
            return []
        names = "、".join(self.ctx.scene.objects)
        return [_fn("compare_with_reference",
                    f"对照场景包里的参考图，判断当前画面中某个特定目标的情况（是否出现、状态是否正常等）。可用目标：{names}",
                    {"target": {"type": "string", "description": "场景包里的目标名称"},
                     "question": {"type": "string", "description": "要判断的具体问题，如“现在指示灯是什么颜色，是否正常”"}},
                    ["target", "question"])]

    async def tool_compare_with_reference(self, target: str, question: str) -> dict:
        obj = self.ctx.find(target)
        if obj is None:
            return {"error": f"场景包里没有“{target}”，可用目标：{'、'.join(self.ctx.scene.objects) or '无'}"}
        refs = self.ctx.references(obj)
        bf = self.buffer.latest_snapshot(quality=92)
        if bf is None:
            return {"error": "暂时没有画面，请检查摄像头"}
        frame = encode_jpeg(resize_max_side(decode_jpeg(bf.jpeg), self.look_max_side), 90)
        turn.attach(frame, f"对照“{obj.name}”参考图")
        desc = obj.to_text()
        if refs:
            q = prompts.COMPARE_PROMPT.format(n_ref=len(refs), name=obj.name, desc=desc, question=question,
                                              refs="、".join(f"图{i}" for i in range(1, len(refs) + 1)))
        else:   # 没有参考图时只用文字描述
            q = f"目标说明：{desc}\n问题：{question}"
        result = await ask_look(self.vlm, [*refs, frame], q, max_tokens=self.look_max_tokens)
        return {"time": fmt_ts(bf.ts), "target": obj.name, "reference_images": len(refs), **result}
