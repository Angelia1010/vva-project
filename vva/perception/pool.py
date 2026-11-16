"""每路摄像头一个后台观察，摄像头增删、启停时自动跟着增删。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from vva.perception.observer import Observer

log = logging.getLogger(__name__)


class ObserverPool:
    def __init__(self, hub: Any, make: Callable[[str], Observer]) -> None:
        """make(摄像头编号) → 这一路的 Observer。"""
        self.hub, self.make = hub, make
        self.observers: dict[str, Observer] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._running = False

    def get(self, cam_id: str) -> Observer | None:
        return self.observers.get(cam_id)

    @property
    def interval(self) -> float:
        return next(iter(self.observers.values())).interval if self.observers else 4.0

    def hold(self, seconds: float) -> None:
        for o in self.observers.values():
            o.hold(seconds)

    def sync(self) -> None:
        """按当前启用的摄像头增删观察任务。"""
        want = set(self.hub.ids())
        for cid in list(self.observers):
            if cid not in want:
                self.observers.pop(cid)
                t = self._tasks.pop(cid, None)
                if t:
                    t.cancel()
                log.info("已停止摄像头 %s 的后台观察", cid)
        for cid in self.hub.ids():
            if cid not in self.observers:
                self.observers[cid] = self.make(cid)
                if self._running:
                    self._tasks[cid] = asyncio.create_task(self.observers[cid].run(), name=f"observer-{cid}")

    def start(self) -> None:
        self._running = True
        self.sync()
        for cid, o in self.observers.items():
            if cid not in self._tasks:
                self._tasks[cid] = asyncio.create_task(o.run(), name=f"observer-{cid}")

    def stop(self) -> None:
        self._running = False
        for t in self._tasks.values():
            t.cancel()
        self._tasks.clear()
