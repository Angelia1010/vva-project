"""后台观察：定时把画面交给多模态大模型，生成时间线，并判断监控规则。

为节省调用：画面没有明显变化时跳过；但为了不漏掉规则判断，
有监控规则时至少每 rule_check_interval 秒看一次，没有规则时至少每 max_idle 秒看一次。

规则（职责）命中后：设置了 on_hit 时交给它（职责引擎负责复核、告警、通知）；否则直接发 TOPIC_ALERT（旧行为）。
只判断当前生效的规则：rules 有 active() 方法时用它（职责清单），否则用 list()。

多路摄像头时每路一个 Observer（camera_id 不同），只判断适用于这一路的职责；视觉模型由调度器统一排队，
排队中被新任务替换（Superseded）时这一轮直接跳过。
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
from vva.perception.scheduler import Superseded
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
                 references: Callable[[str], list[tuple[str, bytes]]] | None = None,
                 on_hit: Callable[..., Any] | None = None, camera_id: str = "",
                 camera_name: Callable[[], str] | str = "", paused: Callable[[], bool] | None = None, **_: Any) -> None:
        """hint_provider：动态的场景说明（如常驻技能、岗位说明），多路时以摄像头编号为参数；
        references：按规则文字取参考图 [(说明, jpeg)]；
        on_hit(规则编号, 依据, 画面时间, 画面[, camera=摄像头])：命中后的处理，不设置则直接发提醒；
        paused()：这一路所在岗位暂停值守时为 True（照常记录时间线，不判断职责）。"""
        self.buffer, self.vlm, self.timeline, self.rules, self.bus = buffer, vlm, timeline, rules, bus
        self.camera_id = camera_id
        self._camera_name = camera_name
        self._paused_fn = paused
        self.interval = float(interval)
        self.max_idle = float(max_idle)
        self.rule_check_interval = float(rule_check_interval)
        self.change_threshold = float(change_threshold)
        self.image_side = int(image_side)
        self.scene_hint = scene_hint
        self.hint_provider = hint_provider
        self.references = references
        self.on_hit = on_hit
        self._hold_until = 0.0
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
        idle_limit = min(self.max_idle, self.rule_check_interval) if self._watching() else self.max_idle
        return diff >= self.change_threshold or now - self._last_described >= idle_limit, sig

    def _match_rule(self, key: str) -> Any:
        """按编号找规则；模型有时返回名称或条件原文，也要能对上。"""
        key = key.strip().strip("“”\"' ")
        if not key:
            return None
        rule = self.rules.get(key)
        if rule is not None:
            return rule
        watching = self._watching()
        for r in watching:
            if key in (getattr(r, "name", ""), r.condition):
                return r
        for r in watching:
            name = getattr(r, "name", "")
            if (name and (name in key or key in name)) or (len(key) >= 4 and key in r.condition):
                return r
        return None

    @property
    def camera_name(self) -> str:
        return self._camera_name() if callable(self._camera_name) else self._camera_name

    def _watching(self) -> list[Any]:
        if self._paused_fn and self._paused_fn():
            return []
        active = getattr(self.rules, "active", None)
        items = list(active(camera=self.camera_id) if self.camera_id else active()) if callable(active) else self.rules.list()
        if self.camera_id:
            items = [r for r in items if not hasattr(r, "applies_to") or r.applies_to(self.camera_id)]
        return items

    def _reference_images(self) -> list[tuple[str, bytes]]:
        """有监控规则提到技能里的目标时，带上它们的参考图帮助辨认。"""
        watching = self._watching()
        if not (self.references and watching):
            return []
        words = [w for r in watching for w in (r.condition, *getattr(r, "objects", []))]
        return self.references(" ".join(words))[:4]

    def build_prompt(self, refs: list[tuple[str, bytes]] | None = None) -> str:
        last = self.timeline.latest(self.camera_id or None)
        prev = f"\n上一次的记录（供对比变化）：{last.summary}\n" if last else ""
        rules = ""
        watching = self._watching()
        if watching:
            lines = "\n".join(f"- {r.id}：{r.condition}" for r in watching)
            rules = prompts.OBSERVE_RULES_BLOCK.format(rules=lines)
        dyn = ""
        if self.hint_provider:
            dyn = self.hint_provider(self.camera_id) if self.camera_id else self.hint_provider()
        hints = [h for h in (self.scene_hint, dyn) if h]
        hint = "\n场景说明：" + "\n".join(hints) + "\n" if hints else ""
        if refs:
            hint += prompts.OBSERVE_REFS_BLOCK.format(n_ref=len(refs), refs="；".join(
                f"图{i}是{label}" for i, (label, _) in enumerate(refs, 1)))
        return prompts.OBSERVE_PROMPT.format(prev=prev, rules=rules, scene_hint=hint)

    def hold(self, seconds: float) -> None:
        """有人提问时让出模型：这段时间内暂缓观察（有紧急职责生效时不让，避免漏报）。"""
        if any(getattr(r, "level", "") == "urgent" for r in self._watching()):
            return
        self._hold_until = max(self._hold_until, time.time() + seconds)

    async def step(self) -> TimelineEntry | None:
        if self.paused or time.time() < self._hold_until:
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
        try:
            text = await self.vlm.ask([*(img for _, img in refs), jpeg], self.build_prompt(refs),
                                      system=prompts.OBSERVER_SYSTEM, json_mode=True)
        except Superseded:
            return None
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
            anomalies=_as_list(data.get("anomalies")), camera=self.camera_id, camera_name=self.camera_name,
        )
        self.timeline.add(entry)
        self.bus.publish(TOPIC_UI, {"type": "timeline", **entry.to_dict()})
        self.bus.publish(TOPIC_OBSERVATION, {"entry": entry, "jpeg": jpeg})
        for hit in data.get("triggered_rules") or []:
            rid = hit.get("id") if isinstance(hit, dict) else str(hit)
            evidence = hit.get("evidence", "") if isinstance(hit, dict) else ""
            rule = self._match_rule(str(rid or ""))
            if rule is None:
                log.info("后台观察返回了未知的规则：%s（%s）", rid, evidence)
                continue
            log.info("疑似命中【%s】%s：%s", getattr(rule, "name", rule.condition),
                     f"（{self.camera_name or self.camera_id}）" if self.camera_id else "", evidence)
            if self.on_hit:
                if self.camera_id:
                    self.on_hit(rule.id, evidence, frame.ts, jpeg, camera=self.camera_id)
                else:
                    self.on_hit(rule.id, evidence, frame.ts, jpeg)
            elif self.rules.mark_triggered(rule.id, now):
                self.bus.publish(TOPIC_ALERT, {"rule_id": rule.id, "condition": rule.condition,
                                               "evidence": evidence, "ts": frame.ts})
        return entry

    async def run(self) -> None:
        log.info("后台观察已启动%s，间隔 %.1f 秒", f"（{self.camera_name or self.camera_id}）" if self.camera_id else "",
                 self.interval)
        while True:
            t0 = time.time()
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("后台观察出错：%s", e)
            await asyncio.sleep(max(0.2, self.interval - (time.time() - t0)))
