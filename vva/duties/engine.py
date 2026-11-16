"""职责引擎：接住后台观察的“疑似命中”，按职责的规则决定要不要告警。

  疑似命中 → 在生效时间内？→ 冷却期外？→ 持续够久？→ 放大复核（开了才走）→ 新建告警 → 按职责通知
试运行的职责只记录，不通知。同一条职责同一时间只处理一次（复核期间再次命中会被忽略）。

多路摄像头时，冷却、持续计时、复核都按“职责 + 摄像头”分开算：大门有人吸烟和仓库里有人吸烟是两件事。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from vva.alerts import AlertService
from vva.core.bus import TOPIC_UI, EventBus
from vva.duties.store import DutyBook
from vva.duties.verify import Verifier
from vva.notify import NotifyRouter

log = logging.getLogger(__name__)

SNOOZE_AFTER_REJECT = 30.0      # 复核不成立后，这条职责多少秒内不再判断


class DutyEngine:
    def __init__(self, book: DutyBook, alerts: AlertService, router: NotifyRouter, bus: EventBus,
                 verifier: Verifier | None = None, observe_interval: float = 8.0) -> None:
        self.book, self.alerts, self.router, self.bus, self.verifier = book, alerts, router, bus, verifier
        self.observe_interval = float(observe_interval)
        self._first_hit: dict[str, tuple[float, float]] = {}    # 职责@摄像头 → (首次命中, 最近命中)
        self._busy: set[str] = set()
        self._tasks: set[asyncio.Task] = set()
        # 由应用设置：摄像头编号 → {"id", "name", "label", "station"}
        self.camera_info: Callable[[str], dict[str, Any]] = lambda cam: {"id": cam, "name": "", "label": "", "station": ""}

    @staticmethod
    def _key(duty_id: str, camera: str) -> str:
        return f"{duty_id}@{camera}" if camera else duty_id

    # 后台观察在事件循环里同步调用
    def on_hit(self, duty_id: str, evidence: str, ts: float, jpeg: bytes | None, camera: str = "") -> bool:
        duty = self.book.get(duty_id)
        now = time.time()
        if duty is None:
            return False
        where = f"（{self.camera_info(camera).get('label') or camera}）" if camera else ""
        if not duty.active(now) or not duty.applies_to(camera):
            log.info("【%s】%s命中但不处理：已暂停、已到期、不在生效时间（%s）或不管这一路", duty.name, where, duty.schedule_text())
            return False
        key = self._key(duty_id, camera)
        if key in self._busy:
            return False
        if self.book.cooling(duty_id, now, camera):
            log.info("【%s】%s命中但在冷却期内（%d 分钟内只报一次），不重复通知", duty.name, where, duty.cooldown_s // 60)
            return False
        if duty.min_duration_s > 0:
            first, last = self._first_hit.get(key, (0.0, 0.0))
            if not first or now - last > max(duty.min_duration_s, self.observe_interval * 3):
                self._first_hit[key] = (now, now)       # 第一次看到（或中断太久重新计）
                log.info("【%s】%s开始计时，持续 %d 秒以上才算", duty.name, where, duty.min_duration_s)
                return False
            self._first_hit[key] = (first, now)
            if now - first < duty.min_duration_s:
                return False
            self._first_hit.pop(key, None)
        self._busy.add(key)
        task = asyncio.ensure_future(self._process(duty_id, evidence, ts, jpeg, camera))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def _process(self, duty_id: str, evidence: str, ts: float, jpeg: bytes | None, camera: str = "") -> None:
        key = self._key(duty_id, camera)
        try:
            duty = self.book.get(duty_id)
            if duty is None:
                return
            images = [("初次发现", jpeg)] if jpeg else []
            box = None
            if duty.verify and self.verifier:
                r = await self.verifier.check(duty, evidence, camera=camera)
                if not r.ok:
                    log.info("【%s】放大复核不成立，不通知：%s", duty.name, r.evidence)
                    self.book.snooze(duty_id, SNOOZE_AFTER_REJECT, camera)
                    self.bus.publish(TOPIC_UI, {"type": "duty_check", "session": None, "duty_id": duty_id, "ok": False,
                                                "camera": camera, "text": f"{duty.name}：复核未确认"})
                    return
                evidence, box = r.evidence or evidence, r.box
                images += r.images[-1:]
            if not self.book.mark_triggered(duty_id, camera=camera):
                return
            await self.raise_alert(duty, evidence, images, box, ts, camera=camera)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("处理职责命中失败")
        finally:
            self._busy.discard(key)

    async def raise_alert(self, duty: Any, evidence: str, images: list[tuple[str, bytes]],
                          box: list[float] | None = None, ts: float | None = None, kind: str | None = None,
                          camera: str = "") -> Any:
        """新建告警并按职责通知（试运行只记录）。巡检异常、摄像头离线也走这里。"""
        trial = duty.mode == "trial"
        info = self.camera_info(camera) if camera else {}
        alert = self.alerts.create(duty, evidence, images, "trial" if trial else "pending", box, kind, ts,
                                   camera=camera, camera_name=info.get("name", ""), station=info.get("station", ""))
        self.alerts.store.add_event(alert.id, "春雷问鉴", f"发现：{evidence}" if evidence else "发现")
        if trial:
            self.alerts.store.add_event(alert.id, "系统", "试运行中，只记录不通知")
            log.info("【%s】命中（试运行，只记录不通知）：%s", duty.name, evidence)
        else:
            self.router.dispatch_alert(alert, duty, lambda text: self.alerts.store.add_event(alert.id, "系统", text))
        self.bus.publish(TOPIC_UI, {"type": "duties_changed", "session": None})
        return alert

    async def close(self) -> None:
        for t in list(self._tasks):
            t.cancel()
