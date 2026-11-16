"""摄像头：多个岗位、每个岗位多个角度。每路摄像头有自己的视频源、帧缓存和在线状态。

    hub = CameraHub(CameraStore(db), stations, buffer_cfg)
    hub.start()
    hub.buffer("cam1")            # 某一路的帧缓存（不传则为第一路）
    hub.resolve("装卸区")          # 按编号、名称、“2 号”找摄像头
    hub.push_jpeg("cam2", jpeg)   # 浏览器、外部程序推送画面

其他模块只需要“按摄像头取帧缓存”，测试和旧代码可以用 SingleFrames 把一个 FrameBuffer 包成同样的接口。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Protocol

from vva.core import turn
from vva.core.sqlite import SqliteDB
from vva.video.base import VideoSource
from vva.video.factory import create_video_source
from vva.video.frame_buffer import FrameBuffer

log = logging.getLogger(__name__)

TYPES = {"stream": "网络摄像头（RTSP / HTTP）", "push": "浏览器或外部程序推送", "local": "本机 USB 摄像头", "custom": "自定义接入"}
SCHEMA = "CREATE TABLE IF NOT EXISTS cameras (id TEXT PRIMARY KEY, data TEXT NOT NULL);"


@dataclass
class Camera:
    id: str
    name: str
    station_id: str = "default"
    area: str = ""                      # 看哪里
    note: str = ""                      # 这一路的额外说明
    source: dict[str, Any] = field(default_factory=lambda: {"type": "push"})
    enabled: bool = True
    sort: int = 0
    created: float = field(default_factory=time.time)

    @property
    def type(self) -> str:
        return str(self.source.get("type") or "push")

    @classmethod
    def from_record(cls, d: dict[str, Any]) -> "Camera":
        known = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in d.items() if k in known})

    def public_source(self) -> dict[str, Any]:
        """给网页看的视频地址：隐藏 RTSP 地址里的密码。"""
        src = dict(self.source)
        url = str(src.get("url") or "")
        if "@" in url and "://" in url:
            scheme, rest = url.split("://", 1)
            cred, host = rest.rsplit("@", 1)
            if ":" in cred:
                src["url"] = f"{scheme}://{cred.split(':', 1)[0]}:******@{host}"
        return src


class CameraStore:
    def __init__(self, db: SqliteDB) -> None:
        self.db = db
        db.script(SCHEMA)

    def load(self) -> list[Camera]:
        return [Camera.from_record(json.loads(r["data"])) for r in self.db.query("SELECT data FROM cameras ORDER BY rowid")]

    def save(self, cam: Camera) -> None:
        self.db.execute("INSERT OR REPLACE INTO cameras(id, data) VALUES (?, ?)",
                        (cam.id, json.dumps(asdict(cam), ensure_ascii=False)))

    def delete(self, cam_id: str) -> None:
        self.db.execute("DELETE FROM cameras WHERE id = ?", (cam_id,))


def source_config(cam: Camera, browser_mode: bool = False) -> dict[str, Any]:
    """摄像头的视频地址 → create_video_source 用的配置。浏览器模式下本机摄像头改由网页推送。"""
    src = dict(cam.source)
    vtype = str(src.pop("type", "push") or "push")
    if browser_mode and vtype == "local":
        vtype = "push"
    cfg: dict[str, Any] = {"type": vtype, "source_id": cam.id}
    if vtype == "custom":
        cfg["custom"] = {"class": src.get("class", ""), "params": src.get("params") or {}}
    else:
        cfg[vtype] = src
    return cfg


@dataclass
class CameraRuntime:
    camera: Camera
    buffer: FrameBuffer
    source: VideoSource | None = None
    error: str = ""
    started: bool = False
    online: bool = False
    ever_online: bool = False
    since: float = field(default_factory=time.time)        # 上一次上线 / 离线的时间


class Frames(Protocol):
    """按摄像头取帧缓存。"""
    def buffer(self, cam_id: str | None = None) -> FrameBuffer | None: ...
    def ids(self) -> list[str]: ...
    def label(self, cam_id: str | None) -> str: ...


class SingleFrames:
    """把一个帧缓存包成 Frames 接口（单摄像头、测试用）。"""

    def __init__(self, buffer: FrameBuffer, cam_id: str = "cam0", name: str = "") -> None:
        self._buffer, self.cam_id, self.name = buffer, cam_id, name

    def buffer(self, cam_id: str | None = None) -> FrameBuffer | None:
        return self._buffer if cam_id in (None, "", self.cam_id) else None

    def ids(self) -> list[str]:
        return [self.cam_id]

    def label(self, cam_id: str | None) -> str:
        return self.name

    def resolve(self, key: Any) -> Any:
        return None

    def default_id(self) -> str:
        return self.cam_id


def scene_for(fn: Callable[..., str] | None, camera: str | None) -> str:
    """场景说明函数可能带摄像头参数，也可能不带（旧代码、测试）。"""
    if fn is None:
        return ""
    if camera:
        try:
            return fn(camera)
        except TypeError:
            pass
    return fn()


def as_frames(x: Any) -> Any:
    return SingleFrames(x) if isinstance(x, FrameBuffer) else x


class CurrentBuffer:
    """看起来像一个帧缓存，实际取“本轮对话正在看的那一路”（没有就第一路）。给技能插件等只认一个帧缓存的代码用。"""

    def __init__(self, frames: Any) -> None:
        self.frames = frames

    def _buf(self) -> FrameBuffer | None:
        return self.frames.buffer(turn.current_camera() or None)

    def __getattr__(self, name: str) -> Any:
        buf = self._buf()
        if buf is None:
            raise AttributeError(name)
        return getattr(buf, name)

    def latest_snapshot(self, *a: Any, **kw: Any) -> Any:
        buf = self._buf()
        return buf.latest_snapshot(*a, **kw) if buf else None

    def latest_frame(self) -> Any:
        buf = self._buf()
        return buf.latest_frame() if buf else None


class CameraHub:
    def __init__(self, store: CameraStore, stations: Any, buffer_cfg: dict[str, Any] | None = None,
                 max_cameras: int = 4, offline_after: float = 15.0, browser_mode: bool = False,
                 factory: Callable[[dict[str, Any]], VideoSource] = create_video_source) -> None:
        self.store, self.stations = store, stations
        self.buffer_cfg = dict(buffer_cfg or {})
        self.max_cameras = max(1, int(max_cameras))
        self.offline_after = float(offline_after)
        self.browser_mode = browser_mode
        self.factory = factory
        self._rt: dict[str, CameraRuntime] = {}
        self._lock = threading.Lock()
        self._started = False
        self._listeners: list[Callable[[], None]] = []
        for cam in self.store.load():
            self._rt[cam.id] = CameraRuntime(cam, FrameBuffer(**self.buffer_cfg))

    # ---------------------------------------------------------------- 初始化
    def seed(self, cameras: list[dict[str, Any]], legacy_video: dict[str, Any] | None = None,
             legacy_area: str = "") -> int:
        """首次启动：按 config.yaml 的 cameras 清单建摄像头；没有清单时把旧的 video 配置变成第一路。"""
        if self._rt:
            return 0
        items = [dict(c) for c in cameras or [] if isinstance(c, dict)]
        if not items and legacy_video:
            vtype = legacy_video.get("type", "local")
            src = {"type": vtype, **(legacy_video.get(vtype) or {})} if vtype != "custom" else \
                {"type": "custom", **(legacy_video.get("custom") or {})}
            items = [{"id": legacy_video.get("source_id") or "cam0", "name": "主画面", "area": legacy_area, "source": src}]
        for i, c in enumerate(items[: self.max_cameras]):
            self.add(id=c.get("id") or f"cam{i}", name=c.get("name") or f"摄像头 {i + 1}",
                     station_id=c.get("station") or c.get("station_id") or "default",
                     area=c.get("area", ""), note=c.get("note", ""), source=c.get("source") or {"type": "push"},
                     enabled=c.get("enabled", True), sort=i)
        return len(items)

    # ---------------------------------------------------------------- 读
    def list(self) -> list[Camera]:
        return sorted((r.camera for r in self._rt.values()), key=lambda c: (c.sort, c.created))

    def enabled(self) -> list[Camera]:
        return [c for c in self.list() if c.enabled]

    def ids(self) -> list[str]:
        return [c.id for c in self.enabled()]

    def get(self, cam_id: str | None) -> Camera | None:
        r = self._rt.get(cam_id or "")
        return r.camera if r else None

    def runtime(self, cam_id: str) -> CameraRuntime | None:
        return self._rt.get(cam_id)

    def default_id(self) -> str:
        cams = self.enabled() or self.list()
        return cams[0].id if cams else ""

    def buffer(self, cam_id: str | None = None) -> FrameBuffer | None:
        r = self._rt.get(cam_id or self.default_id())
        return r.buffer if r else None

    def resolve(self, key: Any) -> Camera | None:
        """“cam1”“装卸区”“2 号”“2”都能认；“2 号”按摄像头的排列顺序。"""
        s = str(key or "").strip().strip("“”\"'")
        if not s:
            return None
        cams = self.list()
        for c in cams:
            if s in (c.id, c.name):
                return c
        for c in cams:
            st = self.stations.get(c.station_id)
            full = f"{st.name}{c.name}" if st else c.name
            if s in full or (len(c.name) >= 2 and c.name in s):
                return c
        num = s.rstrip("号路").replace("第", "").strip()
        if num.isdigit() and 1 <= int(num) <= len(cams):
            return cams[int(num) - 1]
        return None

    def effective_type(self, cam: Camera) -> str:
        """实际的接入方式：浏览器模式下本机摄像头由网页推送。"""
        return "push" if self.browser_mode and cam.type == "local" else cam.type

    def station_of(self, cam_id: str | None) -> Any:
        cam = self.get(cam_id)
        return self.stations.get(cam.station_id if cam else None) or self.stations.station

    def label(self, cam_id: str | None) -> str:
        """给人看的名称：只有一个岗位时就是摄像头名，否则“岗位·摄像头”。"""
        cam = self.get(cam_id)
        if cam is None:
            return ""
        if len(self.stations.list()) <= 1:
            return cam.name
        return f"{self.station_of(cam.id).name}·{cam.name}"

    def info(self, cam_id: str | None) -> dict[str, Any]:
        cam = self.get(cam_id)
        st = self.station_of(cam_id)
        return {"id": cam.id if cam else "", "name": cam.name if cam else "", "label": self.label(cam_id),
                "station_id": st.id, "station": st.name, "area": cam.area if cam else st.area}

    def status(self, cam_id: str) -> dict[str, Any]:
        r = self._rt.get(cam_id)
        if r is None:
            return {"state": "missing", "text": "不存在"}
        bf = r.buffer.latest_frame()
        last = bf.ts if bf else 0.0
        if not r.camera.enabled:
            state, text = "disabled", "已停用"
        elif r.error:
            state, text = "error", r.error
        elif last and time.time() - last <= self.offline_after:
            state, text = "online", "在线"
        elif self.effective_type(r.camera) == "push" and not last:
            state, text = "waiting", "等待推送画面"
        elif not last:
            state, text = "connecting", "连接中"
        else:
            state, text = "offline", f"离线 {int((time.time() - last) // 60)} 分钟"
        return {"state": state, "text": text, "last_frame": last, "buffer": r.buffer.stats()}

    def to_dict(self, cam: Camera) -> dict[str, Any]:
        d = asdict(cam)
        d["source"] = cam.public_source()
        st = self.station_of(cam.id)
        d.update(type=cam.type, push=self.effective_type(cam) == "push", type_label=TYPES.get(cam.type, cam.type),
                 station=st.name, label=self.label(cam.id),
                 status=self.status(cam.id))
        return d

    # ---------------------------------------------------------------- 管理
    def on_change(self, fn: Callable[[], None]) -> None:
        self._listeners.append(fn)

    def _changed(self) -> None:
        for fn in self._listeners:
            try:
                fn()
            except Exception:
                log.exception("摄像头变更通知失败")

    def _new_id(self) -> str:
        n = 0
        while f"cam{n}" in self._rt:
            n += 1
        return f"cam{n}"

    def add(self, **fields_: Any) -> Camera:
        if len(self._rt) >= self.max_cameras:
            raise ValueError(f"最多接入 {self.max_cameras} 路摄像头（可在 config.yaml 的 cameras_max 调整）")
        name = str(fields_.get("name") or "").strip()
        if not name:
            raise ValueError("请填写摄像头名称")
        cid = str(fields_.get("id") or "").strip() or self._new_id()
        if cid in self._rt:
            raise ValueError(f"编号 {cid} 已存在")
        if self.stations.get(fields_.get("station_id") or "default") is None:
            raise ValueError("岗位不存在")
        cam = Camera(id=cid, name=name, sort=int(fields_.get("sort", len(self._rt))),
                     **{k: fields_[k] for k in ("station_id", "area", "note", "source", "enabled") if k in fields_})
        self.store.save(cam)
        with self._lock:
            self._rt[cid] = CameraRuntime(cam, FrameBuffer(**self.buffer_cfg))
        if self._started and cam.enabled:
            self._start_one(self._rt[cid])
        self._changed()
        return cam

    def update(self, cam_id: str, **changes: Any) -> Camera:
        r = self._rt.get(cam_id)
        if r is None:
            raise KeyError(cam_id)
        if "station_id" in changes and self.stations.get(changes["station_id"]) is None:
            raise ValueError("岗位不存在")
        restart = any(k in changes and changes[k] != getattr(r.camera, k) for k in ("source", "enabled"))
        for k in ("name", "station_id", "area", "note", "source", "enabled", "sort"):
            if k in changes:
                setattr(r.camera, k, changes[k])
        self.store.save(r.camera)
        if restart and self._started:
            self._stop_one(r)
            if r.camera.enabled:
                self._start_one(r)
        self._changed()
        return r.camera

    def remove(self, cam_id: str) -> bool:
        r = self._rt.pop(cam_id, None)
        if r is None:
            return False
        self._stop_one(r)
        self.store.delete(cam_id)
        self._changed()
        return True

    # ---------------------------------------------------------------- 运行
    def _start_one(self, r: CameraRuntime) -> None:
        r.error = ""
        try:
            src = self.factory(source_config(r.camera, self.browser_mode))
            src.on_frame(r.buffer.add)
            src.start()
            r.source, r.started = src, True
            log.info("摄像头 %s（%s）已启动：%s", r.camera.id, r.camera.name, src.describe())
        except Exception as e:
            r.source, r.error = None, f"启动失败：{e}"
            log.warning("摄像头 %s 启动失败：%s", r.camera.id, e)

    def _stop_one(self, r: CameraRuntime) -> None:
        if r.source is not None:
            try:
                r.source.stop()
            except Exception as e:
                log.warning("停止摄像头 %s 失败：%s", r.camera.id, e)
        r.source, r.started = None, False

    def start(self) -> None:
        self._started = True
        for r in list(self._rt.values()):
            if r.camera.enabled:
                self._start_one(r)

    def stop(self) -> None:
        self._started = False
        for r in list(self._rt.values()):
            self._stop_one(r)

    def push_ids(self) -> list[str]:
        from vva.video.push_source import PushVideoSource
        return [cid for cid, r in self._rt.items() if isinstance(r.source, PushVideoSource)]

    def push_jpeg(self, cam_id: str | None, data: bytes, ts: float | None = None) -> bool:
        from vva.video.push_source import PushVideoSource
        cid = cam_id or next(iter(self.push_ids()), "")
        r = self._rt.get(cid)
        if r is None or not isinstance(r.source, PushVideoSource):
            raise KeyError(cid)
        return r.source.push_jpeg(data, ts)

    def check_health(self, now: float | None = None) -> list[tuple[Camera, bool]]:
        """在线状态有变化的摄像头 [(摄像头, 现在是否在线)]。定时调用。"""
        now = now or time.time()
        changed = []
        for r in list(self._rt.values()):
            if not r.camera.enabled:
                continue
            online = self.status(r.camera.id)["state"] == "online"
            if online != r.online:
                r.online, r.since = online, now
                if online:
                    r.ever_online = True
                changed.append((r.camera, online))
        return changed


__all__ = ["Camera", "CameraHub", "CameraRuntime", "CameraStore", "CurrentBuffer", "Frames", "SingleFrames", "TYPES",
           "as_frames", "scene_for", "source_config"]
