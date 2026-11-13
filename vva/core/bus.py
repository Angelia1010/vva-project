"""进程内的异步事件总线。

各模块之间不直接互相调用界面或输出，只通过“主题 + 字典”的事件通信，
例如 "ui"（推给前端的所有事件）、"alert"（监控规则触发）、
"observation"（后台观察的每条画面记录及其图片，供长期记忆保存）。
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any

log = logging.getLogger(__name__)

TOPIC_UI = "ui"
TOPIC_ALERT = "alert"
TOPIC_OBSERVATION = "observation"


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[str, list[asyncio.Queue]] = defaultdict(list)
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """绑定主事件循环，之后其他线程才能用 publish_threadsafe。"""
        self._loop = loop

    def subscribe(self, topic: str, maxsize: int = 1000) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._subs[topic].append(q)
        return q

    def unsubscribe(self, topic: str, q: asyncio.Queue) -> None:
        subs = self._subs.get(topic, [])
        if q in subs:
            subs.remove(q)

    def publish(self, topic: str, data: dict[str, Any]) -> None:
        """只能在事件循环线程中调用。"""
        for q in list(self._subs.get(topic, [])):
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                log.warning("订阅者队列已满，丢弃事件: topic=%s", topic)

    def publish_threadsafe(self, topic: str, data: dict[str, Any]) -> None:
        """可在任意线程中调用。"""
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self.publish, topic, data)
