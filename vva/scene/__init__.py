"""场景包：用“参考图 + 文字描述 + 判断标准 + 规则模板 + 热词”适配具体现场，不需要训练模型。

对外入口：
    scene = SceneManager(cfg["scene"])
    agent.extra_prompt = scene.ctx.prompt_block        # 场景说明写进系统提示词
    tools = ToolSet(..., scene.tools(buffer, vlm, bus))
    scene.apply_rules(rules)                            # 加载规则模板
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable

from vva.core.bus import EventBus
from vva.perception.timeline import WatchRules
from vva.scene.context import SceneContext
from vva.scene.store import RuleTemplate, Scene, SceneObject, SceneStore, safe_name
from vva.scene.tools import SceneTools
from vva.video.frame_buffer import FrameBuffer

log = logging.getLogger(__name__)

__all__ = ["SceneManager", "SceneStore", "SceneContext", "SceneTools", "Scene", "SceneObject", "RuleTemplate",
           "safe_name"]


class SceneManager:
    def __init__(self, cfg: dict[str, Any]) -> None:
        root = cfg.get("dir") or "scenes/default"
        os.makedirs(root, exist_ok=True)
        self.store = SceneStore(root)
        self.ctx = SceneContext(self.store, cfg.get("max_ref_images", 3), cfg.get("max_objects_per_call", 2))
        self._listeners: list[Callable[[], None]] = []

    def tools(self, buffer: FrameBuffer, vlm: Any, bus: EventBus, **kw: Any) -> SceneTools:
        return SceneTools(self.ctx, buffer, vlm, bus, **kw)

    def on_change(self, fn: Callable[[], None]) -> None:
        """场景包被网页修改后通知（如更新语音识别热词）。"""
        self._listeners.append(fn)

    def changed(self) -> None:
        for fn in self._listeners:
            fn()

    def apply_rules(self, rules: WatchRules) -> int:
        """把场景包里启用的规则模板加入监控规则（已存在相同条件的不重复添加）。"""
        existing = {r.condition for r in rules.list()}
        n = 0
        for t in self.store.scene.rules:
            if t.enabled and t.condition not in existing:
                rules.add(t.condition, t.cooldown_s)
                n += 1
        if n:
            log.info("从场景包加载 %d 条监控规则", n)
        return n
