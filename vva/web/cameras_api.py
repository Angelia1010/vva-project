"""岗位与摄像头的接口（设置页、值守台使用）。

  GET    /api/cameras                    摄像头清单（含在线状态、今日告警数）+ 岗位清单
  POST   /api/cameras                    新增 {"name", "station_id", "area", "note", "source": {"type", "url"...}, "enabled"}
  PUT    /api/cameras/{id}               修改（视频地址里的密码显示为 ******，原样提交表示不改密码）
  DELETE /api/cameras/{id}               删除（至少保留一路）
  POST   /api/cameras/test               测试连接 {"source": {...}} 或 {"id"}，返回一帧截图
  GET    /api/cameras/{id}/snapshot.jpg  这一路的当前画面
  GET    /api/stations                   岗位清单
  POST   /api/stations                   新增岗位 {"name", "scene", "owner"}
  PUT    /api/stations/{id}              修改岗位（含暂停值守 {"paused": true}）
  DELETE /api/stations/{id}              删除岗位（下面没有摄像头时才能删；默认岗位不能删）

配置了 web.admin_token 时，新增、修改、删除需要请求头 X-Token。
"""
from __future__ import annotations

import asyncio
import base64
import os
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from vva.cameras import TYPES
from vva.core.bus import TOPIC_UI

MASK = ":******@"


def clean_source(src: Any, old: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(src, dict):
        raise HTTPException(400, "视频地址格式不对")
    vtype = str(src.get("type") or "")
    if vtype not in TYPES:
        raise HTTPException(400, "请选择接入方式")
    out: dict[str, Any] = {"type": vtype}
    if vtype == "stream":
        url = str(src.get("url") or "").strip()
        if MASK in url and old and old.get("url"):          # 密码没改：用原来的
            o = str(old["url"])
            if "@" in o and "://" in o:
                scheme, rest = o.split("://", 1)
                cred = rest.rsplit("@", 1)[0]
                url = url.replace(url.split("://", 1)[1].rsplit("@", 1)[0], cred, 1)
        if not url:
            raise HTTPException(400, "请填写视频地址，如 rtsp://admin:密码@192.168.1.64:554/Streaming/Channels/102")
        if not (url.startswith(("rtsp://", "rtsps://", "rtmp://", "http://", "https://")) or os.path.isfile(url)):
            raise HTTPException(400, "视频地址要以 rtsp://、rtmp:// 或 http(s):// 开头（也可以是服务器上的视频文件路径）")
        out["url"] = url
        out["rtsp_transport"] = "udp" if src.get("rtsp_transport") == "udp" else "tcp"
        if os.path.isfile(url):
            out["loop"] = bool(src.get("loop", True))
    elif vtype == "local":
        try:
            out["index"] = int(src.get("index") or 0)
        except (TypeError, ValueError) as e:
            raise HTTPException(400, "USB 摄像头编号要填数字，一般是 0") from e
        for k in ("width", "height", "fps"):
            if src.get(k):
                out[k] = int(src[k])
    elif vtype == "custom":
        out["class"] = str(src.get("class") or "").strip()
        out["params"] = src.get("params") if isinstance(src.get("params"), dict) else {}
        if not out["class"]:
            raise HTTPException(400, "请填写自定义接入的类，如 examples/custom_video_source.py:TestPatternSource")
    return out


def grab_one(source: dict[str, Any], timeout: float = 10.0) -> bytes:
    """按视频地址试拉一帧，返回 JPEG；失败抛 RuntimeError。"""
    import cv2

    vtype = source.get("type")
    if vtype == "stream":
        url = source["url"]
        if url.startswith("rtsp"):
            os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", f"rtsp_transport;{source.get('rtsp_transport', 'tcp')}")
        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG) if not os.path.isfile(url) else cv2.VideoCapture(url)
    elif vtype == "local":
        cap = cv2.VideoCapture(int(source.get("index", 0)))
    else:
        raise RuntimeError("这种接入方式不需要测试")
    try:
        try:
            cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, timeout * 1000)
            cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, timeout * 1000)
        except Exception:
            pass
        if not cap.isOpened():
            raise RuntimeError("打不开视频地址：请检查 IP、端口、用户名密码，以及服务器能否访问这台摄像头")
        t0 = time.time()
        while time.time() - t0 < timeout:
            ok, frame = cap.read()
            if ok and frame is not None:
                h, w = frame.shape[:2]
                scale = min(1.0, 960 / max(h, w))
                if scale < 1:
                    frame = cv2.resize(frame, (int(w * scale), int(h * scale)))
                return cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])[1].tobytes()
        raise RuntimeError("连上了但读不到画面：可能是码流格式不支持，试试子码流（如 /Channels/102）")
    finally:
        cap.release()


def camera_summaries(app: Any) -> list[dict[str, Any]]:
    """摄像头清单 + 今天的告警数、待处理数（值守台、设置页用）。"""
    hub = app.cameras
    counts = app.duties.alerts.camera_today()
    out = []
    for c in hub.list():
        d = hub.to_dict(c)
        n = counts.get(c.id, {})
        if c.id == hub.default_id():           # 升级前的告警没有摄像头编号，算在第一路
            old = counts.get("", {})
            n = {k: n.get(k, 0) + old.get(k, 0) for k in ("today", "open")}
        d.update(today=n.get("today", 0), open=n.get("open", 0))
        out.append(d)
    return out


def station_summaries(app: Any) -> list[dict[str, Any]]:
    cams = app.cameras.list()
    return [{**app.station.to_dict(s.id), "cameras": sum(c.station_id == s.id for c in cams),
             "default": s.id == app.station.id} for s in app.station.list()]


def register_camera_routes(api: FastAPI, app: Any, admin_token: str) -> None:
    hub, stations = app.cameras, app.station

    def check(request: Request) -> None:
        if admin_token and request.headers.get("X-Token") != admin_token:
            raise HTTPException(401, "需要管理口令")

    def changed() -> None:
        app.bus.publish(TOPIC_UI, {"type": "cameras_changed", "session": None})

    def station_list() -> list[dict[str, Any]]:
        return station_summaries(app)

    def camera_list() -> list[dict[str, Any]]:
        return camera_summaries(app)

    @api.get("/api/cameras")
    async def cameras() -> JSONResponse:
        return JSONResponse({"cameras": camera_list(), "stations": station_list(), "types": TYPES,
                             "max": hub.max_cameras, "focus": app.focus_camera, "default": hub.default_id(),
                             "token_required": bool(admin_token)})

    def fields_from(body: dict[str, Any], old: Any = None) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k in ("name", "area", "note"):
            if k in body:
                out[k] = str(body.get(k) or "").strip()[: 20 if k == "name" else 300]
        if "name" in out and not out["name"]:
            raise HTTPException(400, "请填写摄像头名称，如“大门”“装卸区”")
        if "station_id" in body:
            sid = str(body.get("station_id") or "")
            if stations.get(sid) is None:
                raise HTTPException(400, "请选择所属岗位")
            out["station_id"] = sid
        if "enabled" in body:
            out["enabled"] = bool(body["enabled"])
        if "source" in body:
            out["source"] = clean_source(body["source"], old.source if old else None)
        return out

    @api.post("/api/cameras")
    async def camera_add(request: Request, body: dict) -> JSONResponse:
        check(request)
        f = fields_from(body)
        f.setdefault("source", {"type": "push"})
        try:
            cam = hub.add(**f)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        changed()
        return JSONResponse(hub.to_dict(cam))

    @api.put("/api/cameras/{cam_id}")
    async def camera_update(cam_id: str, request: Request, body: dict) -> JSONResponse:
        check(request)
        old = hub.get(cam_id)
        if old is None:
            raise HTTPException(404, "没有这个摄像头")
        try:
            cam = hub.update(cam_id, **fields_from(body, old))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        changed()
        return JSONResponse(hub.to_dict(cam))

    @api.delete("/api/cameras/{cam_id}")
    async def camera_delete(cam_id: str, request: Request) -> JSONResponse:
        check(request)
        if hub.get(cam_id) is None:
            raise HTTPException(404, "没有这个摄像头")
        if len(hub.list()) <= 1:
            raise HTTPException(400, "至少要保留一路摄像头；可以修改它的视频地址")
        used = [d.name for d in app.duties.book.list() if cam_id in d.cameras]
        hub.remove(cam_id)
        if app.focus_camera == cam_id:
            app.focus_camera = None
        changed()
        return JSONResponse({"ok": True, "duties_affected": used})

    @api.post("/api/cameras/test")
    async def camera_test(request: Request, body: dict) -> JSONResponse:
        check(request)
        cam = hub.get(str(body.get("id") or "")) if body.get("id") else None
        source = clean_source(body["source"], cam.source if cam else None) if body.get("source") else (cam.source if cam else None)
        if source is None:
            raise HTTPException(400, "请填写视频地址")
        if source.get("type") == "push":
            return JSONResponse({"ok": True, "note": "推送方式不用测试：保存后在值守台选中这一路，点“开启摄像头”推送画面即可"})
        if source.get("type") == "custom":
            return JSONResponse({"ok": True, "note": "自定义接入保存后启动，状态显示“在线”即为正常"})
        try:
            jpeg = await asyncio.wait_for(asyncio.to_thread(grab_one, source), timeout=15)
        except asyncio.TimeoutError:
            return JSONResponse({"ok": False, "error": "15 秒内没有连上：请检查地址和网络"})
        except RuntimeError as e:
            return JSONResponse({"ok": False, "error": str(e)})
        return JSONResponse({"ok": True, "image": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()})

    @api.get("/api/cameras/{cam_id}/snapshot.jpg")
    async def camera_snapshot(cam_id: str) -> Response:
        buf = hub.buffer(cam_id) if hub.get(cam_id) else None
        bf = buf.latest_snapshot(max_side=960, quality=80) if buf else None
        if bf is None:
            raise HTTPException(404, "暂无画面")
        return Response(bf.jpeg, media_type="image/jpeg")

    # ---------------------------------------------------------------- 岗位
    @api.get("/api/stations")
    async def stations_list() -> JSONResponse:
        return JSONResponse({"stations": station_list()})

    def station_fields(body: dict[str, Any]) -> dict[str, Any]:
        out = {k: str(body[k] or "").strip()[:500] for k in ("name", "area", "scene", "owner") if k in body}
        if "paused" in body:
            out["paused"] = bool(body["paused"])
        if "name" in out and not out["name"]:
            raise HTTPException(400, "请填写岗位名称")
        return out

    @api.post("/api/stations")
    async def station_add(request: Request, body: dict) -> JSONResponse:
        check(request)
        try:
            st = stations.create(**station_fields(body))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return JSONResponse(stations.to_dict(st.id))

    @api.put("/api/stations/{station_id}")
    async def station_update(station_id: str, request: Request, body: dict) -> JSONResponse:
        check(request)
        try:
            st = stations.update(station_id, **station_fields(body))
        except KeyError as e:
            raise HTTPException(404, "没有这个岗位") from e
        return JSONResponse(stations.to_dict(st.id))

    @api.delete("/api/stations/{station_id}")
    async def station_delete(station_id: str, request: Request) -> JSONResponse:
        check(request)
        if stations.get(station_id) is None:
            raise HTTPException(404, "没有这个岗位")
        if any(c.station_id == station_id for c in hub.list()):
            raise HTTPException(400, "这个岗位下还有摄像头：先把摄像头删掉或移到别的岗位")
        if not stations.remove(station_id):
            raise HTTPException(400, "默认岗位不能删除，可以改名")
        return JSONResponse({"ok": True})
