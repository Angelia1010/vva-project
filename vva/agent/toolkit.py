"""工具的公共基础：工具提供者基类 + 把多个提供者合并成一个工具集。

智能体只依赖 `schemas()` 和 `await call(name, args)` 两个方法。
新增一组工具：继承 ToolProvider，在 schemas() 里声明，并实现同名的 `tool_<name>` 异步方法。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from vva.core.bus import TOPIC_UI, EventBus

log = logging.getLogger(__name__)


def fn_schema(name: str, desc: str, props: dict, required: list[str]) -> dict:
    """生成 OpenAI 函数调用格式的工具定义。"""
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required}}}


class ToolProvider:
    """一组工具。子类实现 schemas() 和若干 `tool_<name>` 方法，返回可 JSON 序列化的结果。"""

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus

    def schemas(self) -> list[dict]:
        raise NotImplementedError

    def names(self) -> set[str]:
        return {s["function"]["name"] for s in self.schemas()}

    async def call(self, name: str, args: dict[str, Any]) -> str:
        fn = getattr(self, f"tool_{name}", None)
        if fn is None:
            return json.dumps({"error": f"未知工具 {name}"}, ensure_ascii=False)
        self.bus.publish(TOPIC_UI, {"type": "tool", "name": name, "args": args})
        t0 = time.time()
        try:
            result = await fn(**args)
        except TypeError as e:
            result = {"error": f"参数错误：{e}"}
        except Exception as e:
            log.exception("工具 %s 执行失败", name)
            result = {"error": f"执行失败：{e}"}
        log.info("工具 %s 耗时 %.2fs", name, time.time() - t0)
        return json.dumps(result, ensure_ascii=False)


class ToolSet:
    """把多个 ToolProvider 合并成一个对外接口，按工具名分发。"""

    def __init__(self, *providers: ToolProvider) -> None:
        self.providers = [p for p in providers if p is not None]
        seen: set[str] = set()
        for p in self.providers:
            dup = seen & p.names()
            if dup:
                raise ValueError(f"工具名重复：{'、'.join(dup)}")
            seen |= p.names()

    def schemas(self) -> list[dict]:
        return [s for p in self.providers for s in p.schemas()]

    async def call(self, name: str, args: dict[str, Any]) -> str:
        # 每次按当前的工具定义分发：有的工具会随配置变化出现或消失（如场景包里新增了目标）
        p = next((p for p in self.providers if name in p.names()), None)
        if p is None:
            return json.dumps({"error": f"未知工具 {name}"}, ensure_ascii=False)
        return await p.call(name, args)
