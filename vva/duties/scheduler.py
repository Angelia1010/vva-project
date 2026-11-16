"""定时执行巡检类、汇报类职责，并清理到期的临时任务。每 30 秒检查一次。

  巡检：到点看一次画面，按职责里的标准判断；结果记成一条“记录”，异常时按告警处理并通知。
  汇报：到点基于长期记忆生成这段时间的报告，发给职责的通知对象。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from vva.core.bus import TOPIC_UI, EventBus
from vva.core.jsonutil import parse_json
from vva.duties import prompts
from vva.duties.engine import DutyEngine
from vva.duties.model import Duty, _hm
from vva.duties.store import DutyBook
from vva.notify import NotifyRouter
from vva.perception.imaging import decode_jpeg, encode_jpeg, resize_max_side
from vva.cameras import as_frames, scene_for
from vva.video.frame_buffer import FrameBuffer

log = logging.getLogger(__name__)


def due_time(duty: Duty, now: float) -> float | None:
    """now 时刻是否刚过某个执行时间点（且这个时间点还没执行过），返回该时间点。"""
    if not duty.enabled or not duty.times or duty.expired(now):
        return None
    t = time.localtime(now)
    if duty.weekdays and (t.tm_wday + 1) not in duty.weekdays:
        return None
    for hm in duty.times:
        m = _hm(hm)
        if m is None:
            continue
        target = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, m // 60, m % 60, 0, 0, 0, -1))
        if duty.last_run < target <= now and now - target < 600:      # 错过 10 分钟以上的时间点不补跑
            return target
    return None


class DutyScheduler:
    def __init__(self, book: DutyBook, engine: DutyEngine, router: NotifyRouter, bus: EventBus, buffer: FrameBuffer,
                 vlm: Any, report: Callable[[float, float], Awaitable[str]], scene: Callable[[], str] | None = None,
                 max_side: int = 1280, interval: float = 30.0) -> None:
        self.book, self.engine, self.router, self.bus, self.vlm = book, engine, router, bus, vlm
        self.frames = as_frames(buffer)
        self.report, self.scene = report, scene
        self.max_side, self.interval = int(max_side), float(interval)

    def cameras_for(self, duty: Duty) -> list[str]:
        """巡检要看的摄像头：职责指定的（且还在用的），没指定就是全部。"""
        ids = self.frames.ids()
        picked = [c for c in duty.cameras if c in ids] if duty.cameras else ids
        return picked or ids[:1]

    async def inspect(self, duty: Duty, camera: str = "") -> dict[str, Any]:
        """在一路摄像头上执行一次巡检，返回 {result, abnormal, evidence, jpeg}。"""
        buf = self.frames.buffer(camera or None)
        bf = buf.latest_snapshot(quality=92) if buf else None
        if bf is None:
            return {"result": "没有画面", "abnormal": False, "evidence": ""}
        jpeg = encode_jpeg(resize_max_side(decode_jpeg(bf.jpeg), self.max_side), 90)
        scene = scene_for(self.scene, camera)
        data = parse_json(await self.vlm.ask([jpeg], prompts.INSPECT_PROMPT.format(
            scene=f"场景：{scene}\n" if scene else "", condition=duty.condition), json_mode=True))
        if not isinstance(data, dict):
            data = {"result": "无法判断", "abnormal": False, "evidence": ""}
        data["jpeg"] = jpeg
        return data

    async def run_duty(self, duty: Duty, now: float | None = None) -> None:
        now = now or time.time()
        if duty.kind == "inspect":
            cams = self.cameras_for(duty)
            results = []
            for cam in cams:                     # 每一路各查一次，各自出记录或告警
                r = await self.inspect(duty, cam)
                result = str(r.get("result") or "")
                info = self.engine.camera_info(cam) if cam else {}
                name = info.get("name", "")
                results.append(f"{name}：{result}" if name and len(cams) > 1 else result)
                images = [("巡检画面", r["jpeg"])] if r.get("jpeg") else []
                if r.get("abnormal"):
                    await self.engine.raise_alert(duty, f"{result}。{r.get('evidence') or ''}".strip("。"), images,
                                                  kind="inspect", camera=cam)
                else:
                    self.engine.alerts.create(duty, result, images, "record", None, "inspect", camera=cam,
                                              camera_name=name, station=info.get("station", ""))
            duty.last_result = "；".join(results)
        elif duty.kind == "report":
            start = duty.last_run or now - 86400
            text = await self.report(start, now)
            fmt = "%m-%d %H:%M"
            title = f"{duty.name} {time.strftime(fmt, time.localtime(start))} ~ {time.strftime('%H:%M', time.localtime(now))}"
            self.router.dispatch_report(duty, title, text)
            duty.last_result = f"{time.strftime('%H:%M', time.localtime(now))} 已发送"
        duty.last_run = now
        self.book.touch(duty)
        self.bus.publish(TOPIC_UI, {"type": "duties_changed", "session": None})

    async def tick(self, now: float | None = None) -> None:
        now = now or time.time()
        self.book.purge_expired(now)
        for duty in self.book.list():
            if duty.kind in ("inspect", "report") and due_time(duty, now):
                try:
                    await self.run_duty(duty, now)
                except Exception:
                    log.exception("执行职责 %s 失败", duty.name)

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("职责定时执行出错")
            await asyncio.sleep(self.interval)
