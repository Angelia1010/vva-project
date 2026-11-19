"""视觉调度器：所有“把画面交给视觉大模型”的调用统一排队。

多路摄像头时，每路的后台观察、告警复核、巡检、用户提问都要用同一个视觉大模型。
各自直接调用会互相拖慢，所以统一放进一个优先级队列，同时调用的数量有上限：

  USER     用户提问（看画面、定位、回看）        —— 最先
  VERIFY   告警复核
  SEARCH   录像检索（搜索子智能体看候选画面）
  DUTY     职责检查（有生效职责的摄像头的后台观察、巡检）
  TIMELINE 画面时间线描述（没有职责的摄像头）      —— 最后

同一个 key（如“cam2 的后台观察”）在排队时又来了新任务，旧的直接作废（抛 Superseded），不去看过时的画面。

各模块拿到的是 scheduler.client(优先级)：用法和原来的视觉模型客户端一样（ask(images, prompt, **kw)），
所以不用改调用方式。
"""
from __future__ import annotations

import asyncio
import heapq
import itertools
import logging
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable

log = logging.getLogger(__name__)


class Priority(IntEnum):
    USER = 0
    VERIFY = 1
    SEARCH = 2
    DUTY = 3
    TIMELINE = 4


LABELS = {Priority.USER: "用户提问", Priority.VERIFY: "告警复核", Priority.SEARCH: "录像检索", Priority.DUTY: "职责检查",
          Priority.TIMELINE: "时间线"}


class Superseded(Exception):
    """排队中被同一 key 的新任务替换。"""


@dataclass(order=True)
class _Job:
    priority: int
    seq: int
    images: list = field(compare=False)
    prompt: str = field(compare=False)
    kw: dict = field(compare=False)
    key: str | None = field(compare=False, default=None)
    camera: str = field(compare=False, default="")
    future: asyncio.Future | None = field(compare=False, default=None)
    queued: float = field(compare=False, default_factory=time.time)
    cancelled: bool = field(compare=False, default=False)


class VisionScheduler:
    def __init__(self, vlm: Any, max_concurrency: int = 6) -> None:
        self.vlm = vlm
        self.max_concurrency = max(1, int(max_concurrency))
        self._heap: list[_Job] = []
        self._by_key: dict[str, _Job] = {}
        self._seq = itertools.count()
        self._active = 0
        self._tasks: set[asyncio.Task] = set()
        # 统计：每个优先级、每路摄像头最近的排队时间
        self.calls = {p: 0 for p in Priority}
        self.wait_s = {p: 0.0 for p in Priority}
        self.last_by_camera: dict[str, float] = {}
        self.dropped = 0

    @property
    def bbox_format(self) -> str:
        return getattr(self.vlm, "bbox_format", "0-1000")

    def client(self, priority: Priority | Callable[[], Priority], key: str | Callable[[], str | None] | None = None,
               camera: str | Callable[[], str] = "") -> "SchedulerClient":
        return SchedulerClient(self, priority, key, camera)

    async def ask(self, images: list[bytes], prompt: str, *, priority: int = Priority.DUTY, key: str | None = None,
                  camera: str = "", **kw: Any) -> str:
        loop = asyncio.get_running_loop()
        job = _Job(int(priority), next(self._seq), list(images), prompt, kw, key, camera, loop.create_future())
        if key:
            old = self._by_key.get(key)
            if old is not None and not old.future.done():
                old.cancelled = True
                old.future.set_exception(Superseded(key))
                self.dropped += 1
            self._by_key[key] = job
        heapq.heappush(self._heap, job)
        self._pump()
        return await job.future

    def _pump(self) -> None:
        while self._active < self.max_concurrency and self._heap:
            job = heapq.heappop(self._heap)
            if job.cancelled or job.future.done():
                continue
            if job.key and self._by_key.get(job.key) is job:
                del self._by_key[job.key]
            self._active += 1
            task = asyncio.ensure_future(self._run(job))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _run(self, job: _Job) -> None:
        p = Priority(job.priority)
        wait = time.time() - job.queued
        self.calls[p] += 1
        self.wait_s[p] = round(self.wait_s[p] * 0.8 + wait * 0.2, 2) if self.calls[p] > 1 else round(wait, 2)
        if job.camera:
            self.last_by_camera[job.camera] = time.time()
        try:
            result = await self.vlm.ask(job.images, job.prompt, **job.kw)
            if not job.future.done():
                job.future.set_result(result)
        except Exception as e:
            if not job.future.done():
                job.future.set_exception(e)
        finally:
            self._active -= 1
            self._pump()

    def stats(self) -> dict[str, Any]:
        return {"max_concurrency": self.max_concurrency, "active": self._active,
                "queued": sum(1 for j in self._heap if not j.cancelled), "dropped": self.dropped,
                "avg_wait_s": {LABELS[p]: self.wait_s[p] for p in Priority},
                "calls": {LABELS[p]: self.calls[p] for p in Priority}}


class SchedulerClient:
    """长得像视觉模型客户端：ask(images, prompt, **kw)。优先级、key、摄像头可以是固定值或函数。"""

    def __init__(self, scheduler: VisionScheduler, priority: Any, key: Any = None, camera: Any = "") -> None:
        self.scheduler, self._priority, self._key, self._camera = scheduler, priority, key, camera

    @property
    def bbox_format(self) -> str:
        return self.scheduler.bbox_format

    @staticmethod
    def _v(x: Any) -> Any:
        return x() if callable(x) else x

    async def ask(self, images: list[bytes], prompt: str, **kw: Any) -> str:
        return await self.scheduler.ask(images, prompt, priority=self._v(self._priority), key=self._v(self._key),
                                        camera=self._v(self._camera) or "", **kw)

    def __getattr__(self, name: str) -> Any:        # 其他属性（如 model 名）照原客户端
        return getattr(self.scheduler.vlm, name)
