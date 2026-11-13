"""智能体：多轮对话 + 函数调用循环。

只依赖两个接口，方便替换和测试：
  llm.stream(messages, tools) -> 异步产出 ("text", str) / ("tool_calls", list)
  tools.schemas() / await tools.call(name, args) -> str
"""
from __future__ import annotations

import inspect
import json
import logging
import time
from typing import Any, Callable

from vva.agent import prompts

log = logging.getLogger(__name__)

TextCallback = Callable[[str], Any]


async def _maybe_await(v: Any) -> None:
    if inspect.isawaitable(v):
        await v


_WEEKDAYS = "一二三四五六日"


def now_text() -> str:
    """带星期的当前时间，便于模型换算“今天上午”“昨天下午”等口语时间。"""
    t = time.localtime()
    return time.strftime("%Y-%m-%d %H:%M:%S", t) + f" 星期{_WEEKDAYS[t.tm_wday]}"


class Agent:
    def __init__(self, llm: Any, tools: Any, max_history_turns: int = 8, max_tool_rounds: int = 6,
                 filler_text: str = "", scene_hint: str = "", max_answer_chars: int = 60,
                 notes_provider: Callable[[], list[str]] | None = None, force_tool_first: bool = True,
                 **_: Any) -> None:
        """notes_provider：返回操作员交代过的固定信息（如长期记忆中的备忘），会写进系统提示词。
        force_tool_first：每个问题的第一轮强制调用工具（先看画面或查记录），避免小模型不看就直接回答或拒答。
        """
        self.llm = llm
        self.tools = tools
        self.max_history_turns = int(max_history_turns)
        self.max_tool_rounds = int(max_tool_rounds)
        self.filler_text = filler_text
        self.scene_hint = scene_hint
        self.max_answer_chars = int(max_answer_chars)
        self.notes_provider = notes_provider
        self.force_tool_first = bool(force_tool_first)
        self.history: list[dict[str, Any]] = []

    def _system(self) -> dict[str, Any]:
        hint = f"\n## 场景说明\n{self.scene_hint}" if self.scene_hint else ""
        notes = self.notes_provider() if self.notes_provider else []
        notes_block = prompts.NOTES_BLOCK.format(notes="\n".join(f"- {n}" for n in notes)) if notes else ""
        # 提示词里给模型的字数目标比硬上限紧一半，硬上限只做兜底
        return {"role": "system", "content": prompts.AGENT_SYSTEM.format(
            now=now_text(), max_chars=max(10, self.max_answer_chars // 2),
            notes=notes_block, scene_hint=hint)}

    def _trim_history(self) -> None:
        """只保留最近 N 轮；按用户消息切分，避免工具消息失去对应的调用。"""
        user_idx = [i for i, m in enumerate(self.history) if m["role"] == "user"]
        if len(user_idx) > self.max_history_turns:
            self.history = self.history[user_idx[-self.max_history_turns]:]

    def add_note(self, text: str) -> None:
        """记录系统主动说过的话（如告警），用户可以接着追问。"""
        self.history.append({"role": "assistant", "content": text})

    def reset(self) -> None:
        self.history.clear()

    async def run(self, user_text: str, on_text: TextCallback,
                  on_tool: Callable[[str, dict], Any] | None = None) -> str:
        turn: list[dict[str, Any]] = [{"role": "user", "content": user_text}]
        spoken_filler = False
        final_text = ""
        for round_idx in range(self.max_tool_rounds + 1):
            # 最后一轮不再提供工具，强制模型给出回答
            tools = self.tools.schemas() if round_idx < self.max_tool_rounds else None
            choice = "required" if round_idx == 0 and self.force_tool_first and tools else "auto"
            parts: list[str] = []
            calls: list[dict[str, str]] = []
            async for kind, val in self.llm.stream([self._system(), *self.history, *turn], tools=tools,
                                                   tool_choice=choice):
                if kind == "text":
                    parts.append(val)
                    await _maybe_await(on_text(val))
                elif kind == "tool_calls":
                    calls = val
            text = "".join(parts)
            if not calls:
                turn.append({"role": "assistant", "content": text})
                final_text += text
                break
            if not spoken_filler and not text.strip() and self.filler_text:
                spoken_filler = True
                await _maybe_await(on_text(self.filler_text))
            final_text += text
            norm_calls = []
            for i, c in enumerate(calls):
                norm_calls.append({"id": c.get("id") or f"call_{round_idx}_{i}", "type": "function",
                                   "function": {"name": c["name"], "arguments": c.get("arguments") or "{}"}})
            turn.append({"role": "assistant", "content": text or None, "tool_calls": norm_calls})
            for c in norm_calls:
                name = c["function"]["name"]
                try:
                    args = json.loads(c["function"]["arguments"] or "{}")
                    if not isinstance(args, dict):
                        args = {}
                except json.JSONDecodeError:
                    args = {}
                if on_tool:
                    await _maybe_await(on_tool(name, args))
                log.info("调用工具 %s %s", name, args)
                result = await self.tools.call(name, args)
                turn.append({"role": "tool", "tool_call_id": c["id"], "content": result})
        self.history.extend(turn)
        self._trim_history()
        return final_text
