"""会话：一段独立的对话上下文。网页、每个飞书群、每个飞书私聊各有一个会话，互不串话。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

WRITE_TOOLS = frozenset({"add_task", "remove_task", "pause_duty", "resume_duty", "remember_note", "forget_note",
                         "run_action"})


@dataclass
class Conversation:
    session_id: str
    max_turns: int = 8
    readonly: bool = False              # 只读会话不能修改提醒规则和备忘
    messages: list[dict[str, Any]] = field(default_factory=list)
    updated: float = field(default_factory=time.time)

    def extend(self, turn: list[dict[str, Any]]) -> None:
        self.messages.extend(turn)
        self.trim()
        self.updated = time.time()

    def trim(self) -> None:
        """只保留最近 N 轮；按用户消息切分，避免工具消息失去对应的调用。"""
        user_idx = [i for i, m in enumerate(self.messages) if m["role"] == "user"]
        if len(user_idx) > self.max_turns:
            self.messages = self.messages[user_idx[-self.max_turns]:]

    def add_note(self, text: str) -> None:
        """记录系统主动说过的话（如告警），用户可以接着追问。"""
        self.messages.append({"role": "assistant", "content": text})
        self.updated = time.time()

    def replace_note(self, prefix: str, text: str) -> None:
        """更新一条系统主动告知的信息（如职责清单变化）：先删掉之前同类的，再追加最新的，避免旧信息误导模型。"""
        self.messages = [m for m in self.messages
                         if not (m["role"] == "assistant" and str(m.get("content") or "").startswith(prefix))]
        self.add_note(prefix + text)

    def reset(self) -> None:
        self.messages.clear()

    @property
    def excluded_tools(self) -> frozenset[str]:
        return WRITE_TOOLS if self.readonly else frozenset()


class ConversationStore:
    """按会话编号管理会话；长时间不用的会话自动清理。"""

    def __init__(self, max_turns: int = 8, idle_ttl_s: float = 6 * 3600) -> None:
        self.max_turns = int(max_turns)
        self.idle_ttl = float(idle_ttl_s)
        self._items: dict[str, Conversation] = {}

    def get(self, session_id: str, readonly: bool = False) -> Conversation:
        self._expire()
        conv = self._items.get(session_id)
        if conv is None:
            conv = self._items[session_id] = Conversation(session_id, self.max_turns, readonly)
        conv.readonly = readonly
        return conv

    def peek(self, session_id: str) -> Conversation | None:
        return self._items.get(session_id)

    def all(self) -> list[Conversation]:
        return list(self._items.values())

    def _expire(self) -> None:
        now = time.time()
        for sid in [s for s, c in self._items.items() if now - c.updated > self.idle_ttl]:
            del self._items[sid]
