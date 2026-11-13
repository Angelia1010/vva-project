"""后台观察：定时把画面交给多模态大模型，生成时间线，并判断监控规则。

为节省调用：画面没有明显变化时跳过；但为了不漏掉规则判断，
有监控规则时至少每 rule_check_interval 秒看一次，没有规则时至少每 max_idle 秒看一次。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

import cv2
import numpy as np

from vva.agent import prompts
from vva.core.bus import TOPIC_ALERT, TOPIC_OBSERVATION, TOPIC_UI, EventBus
from vva.core.jsonutil import parse_json
from vva.perception.imaging import encode_jpeg, resize_max_side
from vva.perception.timeline import Timeline, TimelineEntry, WatchRules
from vva.video.frame_buffer import FrameBuffer

log = logging.getLogger(__name__)


def _as_list(v: Any) -> list[str]:
    if isinstance(v, list):
        return [str(x) for x in v if str(x).strip()]
    if isinstance(v, str) and v.strip():
        return [v.strip()]
    return []


class Observer:
    def __init__(self, buffer: FrameBuffer, vlm: Any, timeline: Timeline, rules: WatchRules,
                 bus: EventBus, interval: float = 4.0, max_idle: float = 30.0,
                 rule_check_interval: float = 8.0, change_threshold: float = 6.0,
                 image_side: int = 1024, scene_hint: str = "",
                 hint_provider: Callable[[], str] | None = None,
                 references: Callable[[str], list[tuple[str, bytes]]] | None = None, **_: Any) -> None:
        """hint_provider：动态的场景说明（如常驻技能）；references：按规则文字取参考图 [(说明, jpeg)]。"""
        self.buffer, self.vlm, self.timeline, self.rules, self.bus = buffer, vlm, timeline, rules, bus
        self.interval = float(interval)
        self.max_idle = float(max_idle)
        self.rule_check_interval = float(rule_check_interval)
        self.change_threshold = float(change_threshold)
        self.image_side = int(image_side)
        self.scene_hint = scene_hint
        self.hint_provider = hint_provider
        self.references = references
        self._last_sig: np.ndarray | None = None
        self._last_frame_ts = 0.0
        self._last_described = 0.0
        self.paused = False

    @staticmethod
    def _signature(img: np.ndarray) -> np.ndarray:
        g = cv2.cvtColor(cv2.resize(img, (64, 36), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
        return g.astype(np.float32)

    def should_describe(self, img: np.ndarray, now: float) -> tuple[bool, np.ndarray]:
        sig = self._signature(img)
        if self._last_sig is None:
            return True, sig
        diff = float(np.mean(np.abs(sig - self._last_sig)))
        idle_limit = min(self.max_idle, self.rule_check_interval) if self.rules else self.max_idle
        return diff >= self.change_threshold or now - self._last_described >= idle_limit, sig

    def _reference_images(self) -> list[tuple[str, bytes]]:
        """有监控规则提到技能里的目标时，带上它们的参考图帮助辨认。"""
        if not (self.references and self.rules):
            return []
        return self.references(" ".join(r.condition for r in self.rules.list()))[:4]

    def build_prompt(self, refs: list[tuple[str, bytes]] | None = None) -> str:
        last = self.timeline.latest()
        prev = f"\n上一次的记录（供对比变化）：{last.summary}\n" if last else ""
        rules = ""
        if self.rules:
            lines = "\n".join(f"- {r.id}：{r.condition}" for r in self.rules.list())
            rules = prompts.OBSERVE_RULES_BLOCK.format(rules=lines)
        hints = [h for h in (self.scene_hint, self.hint_provider() if self.hint_provider else "") if h]
        hint = "\n场景说明：" + "\n".join(hints) + "\n" if hints else ""
        if refs:
            hint += prompts.OBSERVE_REFS_BLOCK.format(n_ref=len(refs), refs="；".join(
                f"图{i}是{label}" for i, (label, _) in enumerate(refs, 1)))
        return prompts.OBSERVE_PROMPT.format(prev=prev, rules=rules, scene_hint=hint)

    async def step(self) -> TimelineEntry | None:
        if self.paused:
            return None
        frame = self.buffer.latest_frame()
        if frame is None or frame.ts == self._last_frame_ts:
            return None
        now = time.time()
        need, sig = self.should_describe(frame.image, now)
        if not need:
            return None
        self._last_frame_ts = frame.ts
        jpeg = encode_jpeg(resize_max_side(frame.image, self.image_side), 85)
        refs = self._reference_images()
        text = await self.vlm.ask([*(img for _, img in refs), jpeg], self.build_prompt(refs),
                                  system=prompts.OBSERVER_SYSTEM, json_mode=True)
        data = parse_json(text)
        if not isinstance(data, dict) or not data.get("summary"):
            log.warning("观察结果无法解析：%s", (text or "")[:200])
            return None
        self._last_sig = sig
        self._last_described = now
        pc = data.get("people_count")
        entry = TimelineEntry(
            ts=frame.ts, summary=str(data.get("summary", "")).strip(),
            people_count=int(pc) if isinstance(pc, (int, float)) else None,
            objects=_as_list(data.get("objects")), activities=_as_list(data.get("activities")),
            anomalies=_as_list(data.get("anomalies")),
        )
        self.timeline.add(entry)
        self.bus.publish(TOPIC_UI, {"type": "timeline", **entry.to_dict()})
        self.bus.publish(TOPIC_OBSERVATION, {"entry": entry, "jpeg": jpeg})
        for hit in data.get("triggered_rules") or []:
            rid = hit.get("id") if isinstance(hit, dict) else str(hit)
            evidence = hit.get("evidence", "") if isinstance(hit, dict) else ""
            rule = self.rules.get(str(rid))
            if rule and self.rules.mark_triggered(rule.id, now):
                self.bus.publish(TOPIC_ALERT, {"rule_id": rule.id, "condition": rule.condition,
                                               "evidence": evidence, "ts": frame.ts})
        return entry

    async def run(self) -> None:
        log.info("后台观察已启动，间隔 %.1f 秒", self.interval)
        while True:
            t0 = time.time()
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("后台观察出错：%s", e)
            await asyncio.sleep(max(0.2, self.interval - (time.time() - t0)))
