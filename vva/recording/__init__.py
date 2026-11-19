"""录像：每路摄像头按分钟切片保存，按天数和磁盘使用率自动清理，告警前后的片段单独长期保留。

    rec = RecordingService(cfg["recording"], db, cameras, bus)
    rec.start()                     # 按摄像头启动录制；摄像头增删时调 rec.sync()
    rec.playback.clip(cam, t0, t1)  # 回放

只依赖“摄像头清单 + 每路的帧缓存”（CameraHub）和事件总线：
  - 订阅 TOPIC_ALERT，告警发生后截取前后片段；
  - 订阅 TOPIC_UI 的 alert_update，告警标为误报后片段改按普通录像天数清理；
  - 发 recording_status（每路录像状态变化）、storage_warning（磁盘快满）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from typing import Any

from vva.core.bus import TOPIC_ALERT, TOPIC_UI, EventBus
from vva.core.sqlite import SqliteDB
from vva.playback import NoRecording, Playback
from vva.recording.recorder import FrameRecorder, StreamRecorder, ffmpeg_bin
from vva.recording.store import RecordingStore

log = logging.getLogger(__name__)
DAY = 86400.0

DEFAULTS: dict[str, Any] = {
    "retention_days": 10,
    "alert_retention_days": 180,
    "clean_at": 85, "clean_to": 80, "stop_at": 95,
    "cameras": {},          # 摄像头编号 → {"enabled": bool, "url": "录像专用地址", "retention_days": n}
}


class RecordingService:
    def __init__(self, cfg: dict[str, Any], db: SqliteDB, cameras: Any, bus: EventBus) -> None:
        self.cfg = cfg
        self.root = cfg.get("dir") or "data/recordings"
        os.makedirs(self.root, exist_ok=True)
        self.store = RecordingStore(db)
        self.cameras, self.bus = cameras, bus
        self.segment_s = float(cfg.get("segment_s", 60))
        self.frame_fps = float(cfg.get("frame_fps", 5))
        self.playback = Playback(self.store, self.root, cfg.get("encoder", "auto"))
        self.recorders: dict[str, Any] = {}
        self.paused = False              # 磁盘到停止线时暂停全部录像
        self.guard_batch = 30            # 磁盘清理每次删几片后重新看一次使用率（每片约 1 分钟）
        self._started = False
        self._tasks: list[asyncio.Task] = []
        self._pending: set[asyncio.Task] = set()
        self._last_state: dict[str, str] = {}

    # ---------------------------------------------------------------- 设置
    def settings(self) -> dict[str, Any]:
        """配置文件的默认值 + 网页上改过的值。"""
        alert = self.cfg.get("alert_clip") or {}
        guard = self.cfg.get("disk_guard") or {}
        base = {**DEFAULTS, "retention_days": self.cfg.get("retention_days", DEFAULTS["retention_days"]),
                "alert_retention_days": alert.get("retention_days", DEFAULTS["alert_retention_days"]),
                "clean_at": guard.get("clean_at", 85), "clean_to": guard.get("clean_to", 80), "stop_at": guard.get("stop_at", 95)}
        base.update(self.store.settings())
        base["cameras"] = dict(base.get("cameras") or {})
        return base

    def update_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        keep = {k: v for k, v in values.items() if k in DEFAULTS}
        self.store.save_settings(keep)
        self.sync()
        return self.settings()

    def camera_settings(self, cam_id: str) -> dict[str, Any]:
        s = self.settings()
        c = s["cameras"].get(cam_id) or {}
        return {"enabled": c.get("enabled", True), "url": c.get("url", ""),
                "retention_days": c.get("retention_days") or s["retention_days"]}

    @property
    def alert_before(self) -> float:
        return float((self.cfg.get("alert_clip") or {}).get("before_s", 60))

    @property
    def alert_after(self) -> float:
        return float((self.cfg.get("alert_clip") or {}).get("after_s", 60))

    # ---------------------------------------------------------------- 录制
    def _on_segment(self, camera: str):
        def cb(path: str, start: float, end: float, size: int, codec: str, low_fps: bool) -> None:
            self.store.add_segment(camera, start, end, path, size, codec, low_fps)
        return cb

    def _make(self, cam: Any) -> Any:
        out = os.path.join(self.root, cam.id)
        cs = self.camera_settings(cam.id)
        if self.cameras.effective_type(cam) == "stream":
            url = cs["url"] or str(cam.source.get("url") or "")
            return StreamRecorder(cam.id, url, out, self._on_segment(cam.id), self.segment_s)
        return FrameRecorder(cam.id, self.cameras.buffer(cam.id), out, self._on_segment(cam.id), self.segment_s,
                             self.frame_fps)

    def _key(self, cam: Any) -> tuple:
        cs = self.camera_settings(cam.id)
        return (self.cameras.effective_type(cam), cs["url"] or cam.source.get("url"))

    def sync(self) -> None:
        """按摄像头清单和录像设置增删、重启录制。"""
        if not self._started:
            return
        want = {c.id: c for c in self.cameras.enabled() if self.camera_settings(c.id)["enabled"]} if not self.paused else {}
        for cid in list(self.recorders):
            rec, key = self.recorders[cid]
            if cid not in want or self._key(want[cid]) != key:
                rec.stop()
                del self.recorders[cid]
        for cid, cam in want.items():
            if cid not in self.recorders:
                rec = self._make(cam)
                rec.start()
                self.recorders[cid] = (rec, self._key(cam))

    def start(self) -> None:
        self._started = True
        if not ffmpeg_bin():
            log.warning("服务器没有安装 ffmpeg：网络摄像头无法录像，回放也不可用（sudo apt install ffmpeg）")
        self.sync()
        self._tasks = [asyncio.create_task(c) for c in (self._maintain_loop(), self._alert_loop(), self._ui_loop())]

    async def stop(self) -> None:
        self._started = False
        for t in self._tasks + list(self._pending):
            t.cancel()
        recs = [r for r, _ in self.recorders.values()]
        self.recorders.clear()
        await asyncio.gather(*(asyncio.to_thread(r.stop) for r in recs))

    # ---------------------------------------------------------------- 状态
    def status(self, cam_id: str) -> dict[str, Any]:
        cs = self.camera_settings(cam_id)
        cam = self.cameras.get(cam_id)
        if cam is None or not cam.enabled:
            return {"state": "disabled", "text": "摄像头已停用"}
        if not cs["enabled"]:
            return {"state": "disabled", "text": "未开启录像"}
        if self.paused:
            return {"state": "paused", "text": "磁盘快满，已暂停"}
        item = self.recorders.get(cam_id)
        if item is None:
            return {"state": "stopped", "text": "未启动"}
        rec = item[0]
        now = time.time()
        if rec.error:
            return {"state": "error", "text": rec.error}
        last = rec.last_segment or rec.started
        if now - last <= self.segment_s * 2 + 30:
            return {"state": "recording", "text": "录像中", "low_fps": isinstance(rec, FrameRecorder)}
        mins = int((now - last) // 60)
        return {"state": "interrupted", "text": f"中断 {mins} 分钟" if mins else "等待画面"}

    def overview(self) -> dict[str, Any]:
        now = time.time()
        disk = shutil.disk_usage(self.root)
        usage = self.store.usage()
        today = self.store.bytes_between(now - DAY, now)
        total = sum(int(u["bytes"] or 0) for u in usage.values())
        per_day = 0.0
        for cid, u in usage.items():                # 每天新增：录满一天按最近 24 小时，不满一天按已录时长折算
            span = min(DAY, max(60.0, now - float(u["first"])))
            per_day += today.get(cid, 0) * DAY / span
        s = self.settings()
        stop_free = disk.total * (1 - s["clean_at"] / 100)          # 留到清理线
        days_left = (disk.free - stop_free) / per_day if per_day > 0 else None
        cams = []
        for cam in self.cameras.list():
            u = usage.get(cam.id) or {}
            cs = self.camera_settings(cam.id)
            cams.append({"id": cam.id, "label": self.cameras.label(cam.id), "type": self.cameras.effective_type(cam),
                         "url_override": bool(cs["url"]), "enabled": cs["enabled"], "retention_days": cs["retention_days"],
                         "bytes": int(u.get("bytes") or 0), "today_bytes": today.get(cam.id, 0),
                         "first": u.get("first"), "last": u.get("last"), "low_fps": self.store.low_fps(cam.id),
                         "status": self.status(cam.id)})
        return {"disk": {"path": os.path.abspath(self.root), "total": disk.total, "used": disk.used, "free": disk.free,
                         "percent": round(disk.used / disk.total * 100, 1) if disk.total else 0},
                "recording_bytes": total, "per_day_bytes": int(per_day),
                "days_left": round(days_left, 1) if days_left is not None else None,
                "alert_clips": self.store.alert_clip_stats(), "paused": self.paused, "ffmpeg": bool(ffmpeg_bin()),
                "settings": s, "cameras": cams}

    # ---------------------------------------------------------------- 清理
    def _delete(self, rows: list[dict[str, Any]]) -> tuple[int, int]:
        freed = 0
        for r in rows:
            try:
                freed += os.path.getsize(r["path"])
                os.remove(r["path"])
            except OSError:
                pass
        self.store.delete_segments([r["id"] for r in rows])
        return len(rows), freed

    def cleanup_expired(self, now: float | None = None) -> int:
        """按天数删除普通录像和告警片段，返回删除的文件数。"""
        now = now or time.time()
        total = 0
        for cam in {*(c.id for c in self.cameras.list()), *self.store.usage().keys()}:
            days = self.camera_settings(cam)["retention_days"] if self.cameras.get(cam) else self.settings()["retention_days"]
            before = now - float(days) * DAY
            n = freed = 0
            while True:
                rows = self.store.oldest(500, before=before, camera=cam)
                if not rows:
                    break
                a, b = self._delete(rows)
                n, freed = n + a, freed + b
            if n:
                self.store.log_cleanup("超过保留天数", cam, f"删除 {days:g} 天以前的录像", n, freed)
                total += n
        for c in self.store.expired_alert_clips(now):
            try:
                size = os.path.getsize(c["path"])
                os.remove(c["path"])
            except OSError:
                size = 0
            self.store.delete_alert_clip(c["alert_id"])
            self.store.log_cleanup("告警片段到期", c["camera"], f"告警 #{c['alert_id']}", 1, size)
            total += 1
        return total

    def disk_percent(self) -> float:
        d = shutil.disk_usage(self.root)
        return d.used / d.total * 100 if d.total else 0.0

    def guard_disk(self) -> None:
        """磁盘到清理线：从最早的普通录像删到目标线；到停止线：暂停录像。告警片段不删。"""
        s = self.settings()
        pct = self.disk_percent()
        if pct >= s["clean_at"]:
            n = freed = 0
            while self.disk_percent() > s["clean_to"]:
                rows = self.store.oldest(self.guard_batch)
                if not rows:
                    break
                a, b = self._delete(rows)
                n, freed = n + a, freed + b
            if n:
                self.store.log_cleanup("磁盘空间不足", "", f"磁盘使用 {pct:.0f}%，删到 {s['clean_to']}% 以下", n, freed)
                self.bus.publish(TOPIC_UI, {"type": "storage_warning", "session": None, "percent": round(pct),
                                            "text": f"录像磁盘使用 {pct:.0f}%，已自动删除最早的 {n} 段录像"})
        pct = self.disk_percent()
        stop = pct >= s["stop_at"]
        if stop != self.paused:
            self.paused = stop
            if stop:
                log.error("录像磁盘使用 %.0f%%，已暂停录像", pct)
                self.bus.publish(TOPIC_UI, {"type": "storage_warning", "session": None, "percent": round(pct),
                                            "text": f"录像磁盘使用 {pct:.0f}%，已暂停录像，请清理磁盘"})
            self.sync()

    async def _maintain_loop(self) -> None:
        last_expire = 0.0
        while True:
            try:
                await asyncio.to_thread(self.guard_disk)
                if time.time() - last_expire > 600:
                    last_expire = time.time()
                    n = await asyncio.to_thread(self.cleanup_expired)
                    if n:
                        log.info("已清理 %d 个过期录像文件", n)
                    await asyncio.to_thread(self.playback.clean_cache)
                for cam in self.cameras.list():
                    st = self.status(cam.id)
                    if self._last_state.get(cam.id) != st["state"]:
                        self._last_state[cam.id] = st["state"]
                        self.bus.publish(TOPIC_UI, {"type": "recording_status", "session": None, "camera": cam.id, **st})
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("录像维护出错")
            await asyncio.sleep(30)

    # ---------------------------------------------------------------- 告警片段
    def clip_path(self, alert_id: int) -> str:
        return os.path.join(self.root, "_alerts", f"{int(alert_id)}.mp4")

    async def save_alert_clip(self, alert_id: int, camera: str, ts: float, wait: bool = True) -> dict[str, Any] | None:
        """等告警后的录像写完，截取 [ts - before, ts + after] 存成告警片段（统一转成 H.264，单独保留）。"""
        if wait:
            await asyncio.sleep(max(0.0, ts + self.alert_after + self.segment_s + 10 - time.time()))
        start, end = ts - self.alert_before, ts + self.alert_after
        out = self.clip_path(alert_id)
        try:
            await asyncio.to_thread(self.playback.clip, camera, start, end, out, True)
        except NoRecording:
            log.info("告警 #%s 没有对应的录像，不保存片段", alert_id)
            return None
        except Exception as e:
            log.warning("保存告警 #%s 的录像片段失败：%s", alert_id, e)
            return None
        keep = time.time() + float(self.settings()["alert_retention_days"]) * DAY
        self.store.add_alert_clip(alert_id, camera, start, end, out, os.path.getsize(out), keep)
        self.bus.publish(TOPIC_UI, {"type": "alert_update", "session": None, "id": alert_id, "clip": True})
        log.info("已保存告警 #%s 的录像片段", alert_id)
        return self.store.alert_clip(alert_id)

    async def _alert_loop(self) -> None:
        q = self.bus.subscribe(TOPIC_ALERT)
        try:
            while True:
                msg = await q.get()
                if msg.get("kind", "alert") != "alert" or not msg.get("alert_id"):
                    continue
                cam = msg.get("camera") or self.cameras.default_id()
                if not cam or not self.camera_settings(cam)["enabled"]:
                    continue
                t = asyncio.create_task(self.save_alert_clip(int(msg["alert_id"]), cam, float(msg.get("ts") or time.time())))
                self._pending.add(t)
                t.add_done_callback(self._pending.discard)
        finally:
            self.bus.unsubscribe(TOPIC_ALERT, q)

    async def _ui_loop(self) -> None:
        """告警标为误报：片段改按普通录像的天数保留。"""
        q = self.bus.subscribe(TOPIC_UI)
        try:
            while True:
                ev = await q.get()
                a = ev.get("alert") if ev.get("type") == "alert_update" else None
                if a and a.get("status") == "false":
                    c = self.store.alert_clip(int(a["id"]))
                    if c:
                        keep = float(c["created"]) + float(self.settings()["retention_days"]) * DAY
                        self.store.set_alert_clip_keep(int(a["id"]), min(float(c["keep_until"]), keep))
                for aid in ev.get("deleted") or []:             # 告警删除：片段一起删
                    c = self.store.alert_clip(int(aid))
                    if c:
                        try:
                            os.remove(c["path"])
                        except OSError:
                            pass
                        self.store.delete_alert_clip(int(aid))
        finally:
            self.bus.unsubscribe(TOPIC_UI, q)


__all__ = ["RecordingService", "RecordingStore"]
