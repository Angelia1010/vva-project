"""长期记忆：把画面记录、提醒、指令、备忘、监控规则持久化，支持按时间段回溯。

对外只需要 LongTermMemory 这一个入口：
    memory = LongTermMemory(cfg["memory"], bus, llm, vlm)
    memory.attach_rules(rules)            # 规则持久化，重启后恢复
    tools = ToolSet(vision_tools, memory.tools)
    agent = Agent(..., notes_provider=memory.tools.note_lines)
    tasks = memory.background_tasks()     # 记录器 + 时段摘要
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Coroutine

from vva.core.bus import EventBus
from vva.memory.keyframes import KeyframeStore
from vva.memory.recorder import MemoryRecorder
from vva.memory.store import MemoryStore
from vva.memory.summarizer import Summarizer
from vva.memory.tools import MemoryTools
from vva.perception.timeline import WatchRules

log = logging.getLogger(__name__)

__all__ = ["LongTermMemory", "MemoryStore", "KeyframeStore", "MemoryRecorder", "Summarizer", "MemoryTools"]


class LongTermMemory:
    def __init__(self, cfg: dict[str, Any], bus: EventBus, llm: Any, vlm: Any) -> None:
        root = cfg.get("dir") or "data/memory"
        os.makedirs(root, exist_ok=True)
        self.cfg = cfg
        self.store = MemoryStore(os.path.join(root, "memory.db"))
        self.keyframes = KeyframeStore(root)
        self.recorder = MemoryRecorder(self.store, self.keyframes, bus, **cfg)
        self.summarizer = Summarizer(self.store, llm, **cfg) if cfg.get("summary_minutes") else None
        self.tools = MemoryTools(self.store, self.keyframes, vlm, bus, **cfg)

    def attach_rules(self, rules: WatchRules) -> None:
        """从记忆中恢复监控规则，并在规则变化时自动保存。"""
        saved = self.store.load_rules()
        if saved:
            rules.load(saved)
            log.info("已恢复 %d 条监控规则", len(saved))
        rules.on_change(lambda items: self.store.save_rules([r.to_dict() for r in items]))

    def background_tasks(self) -> list[Coroutine[Any, Any, None]]:
        tasks = [self.recorder.run()]
        if self.summarizer:
            tasks.append(self.summarizer.run())
        return tasks

    def stats(self) -> dict[str, Any]:
        return {"events": self.store.count_events(), "path": self.store.path}

    async def close(self) -> None:
        await asyncio.to_thread(self.store.close)
