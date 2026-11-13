"""记忆记录器：订阅事件总线，把画面记录、提醒、操作员指令写进长期记忆，并按保留期限清理。

只依赖事件总线和存储，不知道观察器、智能体如何实现：
  TOPIC_OBSERVATION  {"entry": TimelineEntry, "jpeg": bytes}   后台观察的每条记录
  TOPIC_ALERT        {"condition", "evidence", "ts", ...}       监控提醒触发
  TOPIC_UI           {"type": "user", "text", "source"}         操作员的指令（其他 UI 事件忽略）
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from vva.core.bus import TOPIC_ALERT, TOPIC_OBSERVATION, TOPIC_UI, EventBus
from vva.memory.keyframes import KeyframeStore
from vva.memory.store import KIND_ALERT, KIND_COMMAND, KIND_OBSERVATION, MemoryEvent, MemoryStore
from vva.perception.imaging import decode_jpeg, encode_jpeg, resize_max_side

log = logging.getLogger(__name__)

DAY = 86400.0


class MemoryRecorder:
    def __init__(self, store: MemoryStore, keyframes: KeyframeStore, bus: EventBus,
                 save_keyframes: bool = True, keyframe_side: int = 640, keyframe_quality: int = 80,
                 retention_days: float = 30, keyframe_retention_days: float = 7,
                 cleanup_interval_s: float = 3600, **_: Any) -> None:
        self.store, self.keyframes, self.bus = store, keyframes, bus
        self.save_keyframes = bool(save_keyframes)
        self.keyframe_side = int(keyframe_side)
        self.keyframe_quality = int(keyframe_quality)
        self.retention_days = float(retention_days)
        self.keyframe_retention_days = float(keyframe_retention_days)
        self.cleanup_interval = float(cleanup_interval_s)

    # ---------------------------------------------------------------- 写入
    def _shrink(self, jpeg: bytes) -> bytes:
        img = decode_jpeg(jpeg)
        if max(img.shape[:2]) <= self.keyframe_side:
            return jpeg
        return encode_jpeg(resize_max_side(img, self.keyframe_side), self.keyframe_quality)

    async def record_observation(self, entry: Any, jpeg: bytes | None) -> MemoryEvent:
        frame = None
        if self.save_keyframes and jpeg:
            try:
                small = await asyncio.to_thread(self._shrink, jpeg)
                frame = await asyncio.to_thread(self.keyframes.save, entry.ts, small)
            except Exception as e:
                log.warning("保存关键帧失败：%s", e)
        ev = MemoryEvent(ts=entry.ts, kind=KIND_OBSERVATION, summary=entry.summary, people=entry.people_count,
                         details={"objects": entry.objects, "activities": entry.activities,
                                  "anomalies": entry.anomalies}, frame=frame)
        ev.id = self.store.add_event(ev)
        return ev

    def record_alert(self, alert: dict[str, Any]) -> None:
        text = f"提醒：{alert.get('condition', '')}" + (f"（{alert['evidence']}）" if alert.get("evidence") else "")
        self.store.add_event(MemoryEvent(ts=float(alert.get("ts") or time.time()), kind=KIND_ALERT,
                                         summary=text, details={"rule_id": alert.get("rule_id")}))

    def record_command(self, text: str, source: str = "") -> None:
        self.store.add_event(MemoryEvent(ts=time.time(), kind=KIND_COMMAND, summary=f"操作员指令：{text}",
                                         details={"source": source}))

    # ---------------------------------------------------------------- 清理
    def cleanup(self, now: float | None = None) -> None:
        now = now or time.time()
        frame_cutoff = now - self.keyframe_retention_days * DAY
        old_frames = self.store.frames_before(frame_cutoff)
        if old_frames:
            n = self.keyframes.delete(old_frames)
            self.store.clear_frames_before(frame_cutoff)
            log.info("清理过期关键帧 %d 张", n)
        removed = self.store.delete_events_before(now - self.retention_days * DAY)
        if removed:
            log.info("清理过期记录 %d 条", removed)

    # ---------------------------------------------------------------- 运行
    async def run(self) -> None:
        subs = {TOPIC_OBSERVATION: self.bus.subscribe(TOPIC_OBSERVATION),
                TOPIC_ALERT: self.bus.subscribe(TOPIC_ALERT),
                TOPIC_UI: self.bus.subscribe(TOPIC_UI)}
        tasks = [asyncio.create_task(self._consume(topic, q), name=f"memory-{topic}") for topic, q in subs.items()]
        tasks.append(asyncio.create_task(self._cleanup_loop(), name="memory-cleanup"))
        log.info("长期记忆已启动：%s", self.store.path)
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            for topic, q in subs.items():
                self.bus.unsubscribe(topic, q)

    async def _consume(self, topic: str, q: asyncio.Queue) -> None:
        while True:
            msg = await q.get()
            try:
                if topic == TOPIC_OBSERVATION:
                    await self.record_observation(msg["entry"], msg.get("jpeg"))
                elif topic == TOPIC_ALERT:
                    self.record_alert(msg)
                elif msg.get("type") == "user":
                    self.record_command(msg.get("text", ""), msg.get("source", ""))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("写入长期记忆失败：%s", e)

    async def _cleanup_loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.cleanup)
            except Exception as e:
                log.warning("清理长期记忆失败：%s", e)
            await asyncio.sleep(self.cleanup_interval)
