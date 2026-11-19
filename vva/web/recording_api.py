"""录像页和回放的接口。

  GET  /recording                              录像页
  GET  /api/recording/overview                 磁盘、占用、每天新增、还能存几天、每路状态、设置
  GET  /api/recording/timeline?cam=&day=       某路某天的录像时段（day 为 YYYY-MM-DD，默认今天）
  GET  /api/recording/settings   PUT …         保留设置（PUT 需管理口令）
  GET  /api/recording/cleanups                 清理记录
  GET  /api/playback/clip?cam=&start=&end=     片段 MP4（start、end 为秒级时间戳；最长 10 分钟）
  GET  /api/playback/frame?cam=&t=             那一秒的画面
  GET  /api/alerts/{id}/clip                   告警片段 MP4    GET /api/alerts/{id}/clip/info 是否已生成
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from vva.playback import NoRecording


def _day_start(day: str) -> float:
    if not day:
        t = time.localtime()
        return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
    try:
        return time.mktime(time.strptime(day, "%Y-%m-%d"))
    except ValueError as e:
        raise HTTPException(400, "日期格式应为 YYYY-MM-DD") from e


def clean_settings(body: dict[str, Any], cameras: set[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}

    def num(k: str, lo: float, hi: float) -> None:
        if k in body:
            try:
                v = float(body[k])
            except (TypeError, ValueError) as e:
                raise HTTPException(400, f"{k} 要填数字") from e
            if not lo <= v <= hi:
                raise HTTPException(400, f"{k} 要在 {lo:g} 到 {hi:g} 之间")
            out[k] = int(v) if v == int(v) else v
    num("retention_days", 1, 3650)
    num("alert_retention_days", 1, 3650)
    num("clean_at", 50, 99)
    num("clean_to", 30, 98)
    num("stop_at", 60, 100)
    if {"clean_at", "clean_to", "stop_at"} & out.keys():
        ca, ct, sa = out.get("clean_at", 85), out.get("clean_to", 80), out.get("stop_at", 95)
        if not ct < ca < sa:
            raise HTTPException(400, "磁盘阈值要满足：清理到 < 开始清理 < 暂停录像")
    if "cameras" in body:
        cams: dict[str, Any] = {}
        for cid, c in (body.get("cameras") or {}).items():
            if cid not in cameras or not isinstance(c, dict):
                continue
            item: dict[str, Any] = {"enabled": bool(c.get("enabled", True))}
            url = str(c.get("url") or "").strip()
            if url:
                if not url.startswith(("rtsp://", "rtsps://", "rtmp://", "http://", "https://")):
                    raise HTTPException(400, "录像地址要以 rtsp:// 等开头")
                item["url"] = url
            if c.get("retention_days") not in (None, "", 0):
                try:
                    d = int(c["retention_days"])
                except (TypeError, ValueError) as e:
                    raise HTTPException(400, "保留天数要填整数") from e
                if not 1 <= d <= 3650:
                    raise HTTPException(400, "保留天数要在 1 到 3650 之间")
                item["retention_days"] = d
            cams[cid] = item
        out["cameras"] = cams
    return out


def mask(url: str) -> str:
    if "@" in url and "://" in url:
        scheme, rest = url.split("://", 1)
        cred, host = rest.rsplit("@", 1)
        if ":" in cred:
            return f"{scheme}://{cred.split(':', 1)[0]}:******@{host}"
    return url


def register_recording_routes(api: FastAPI, app: Any, admin_token: str, static_dir: str) -> None:
    rec = app.recording

    def check(request: Request) -> None:
        if admin_token and request.headers.get("X-Token") != admin_token:
            raise HTTPException(401, "需要管理口令")

    def need() -> Any:
        if rec is None:
            raise HTTPException(404, "录像未开启（config.yaml 的 recording.enabled）")
        return rec

    def cam_ok(cam: str) -> str:
        if not app.cameras.get(cam):
            raise HTTPException(404, "没有这个摄像头")
        return cam

    @api.get("/recording")
    async def recording_page() -> FileResponse:
        return FileResponse(os.path.join(static_dir, "recording.html"))

    @api.get("/api/recording/overview")
    async def overview() -> JSONResponse:
        r = need()
        data = await asyncio.to_thread(r.overview)
        for c in data["settings"].get("cameras", {}).values():
            if c.get("url"):
                c["url"] = mask(c["url"])
        data["token_required"] = bool(admin_token)
        return JSONResponse(data)

    @api.get("/api/recording/timeline")
    async def timeline(cam: str, day: str = "") -> JSONResponse:
        r = need()
        cam_ok(cam)
        start = _day_start(day)
        spans = r.store.spans(cam, start, start + 86400)
        return JSONResponse({"camera": cam, "day": time.strftime("%Y-%m-%d", time.localtime(start)), "start": start,
                             "spans": [[round(a - start, 1), round(b - start, 1)] for a, b in spans],
                             "seconds": round(sum(b - a for a, b in spans))})

    @api.get("/api/recording/settings")
    async def get_settings() -> JSONResponse:
        s = need().settings()
        for c in s.get("cameras", {}).values():
            if c.get("url"):
                c["url"] = mask(c["url"])
        return JSONResponse(s)

    @api.put("/api/recording/settings")
    async def put_settings(request: Request, body: dict) -> JSONResponse:
        check(request)
        r = need()
        values = clean_settings(body, {c.id for c in app.cameras.list()})
        old = r.settings().get("cameras", {})
        for cid, c in values.get("cameras", {}).items():          # 密码没改（******）：用原来的地址
            if "******" in c.get("url", "") and old.get(cid, {}).get("url"):
                c["url"] = old[cid]["url"]
        s = await asyncio.to_thread(r.update_settings, values)
        for c in s.get("cameras", {}).values():
            if c.get("url"):
                c["url"] = mask(c["url"])
        return JSONResponse(s)

    @api.get("/api/recording/cleanups")
    async def cleanups() -> JSONResponse:
        rows = need().store.cleanups(30)
        return JSONResponse({"cleanups": [{**x, "time": time.strftime("%m-%d %H:%M", time.localtime(x["ts"]))} for x in rows]})

    @api.get("/api/playback/clip")
    async def clip(cam: str, start: float, end: float) -> FileResponse:
        r = need()
        cam_ok(cam)
        try:
            path = await asyncio.to_thread(r.playback.clip, cam, start, end)
        except NoRecording as e:
            raise HTTPException(404, "这段时间没有录像") from e
        except (ValueError, RuntimeError) as e:
            raise HTTPException(400, str(e)) from e
        name = f"{cam}-{time.strftime('%Y%m%d-%H%M%S', time.localtime(start))}.mp4"
        return FileResponse(path, media_type="video/mp4", headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(name)}"})

    @api.get("/api/playback/frame")
    async def frame(cam: str, t: float) -> Response:
        r = need()
        cam_ok(cam)
        try:
            data = await asyncio.to_thread(r.playback.frame, cam, t)
        except NoRecording as e:
            raise HTTPException(404, "这个时间没有录像") from e
        except RuntimeError as e:
            raise HTTPException(400, str(e)) from e
        return Response(data, media_type="image/jpeg")

    @api.get("/api/alerts/{alert_id}/clip/info")
    async def alert_clip_info(alert_id: int) -> JSONResponse:
        if rec is None:
            return JSONResponse({"ready": False, "reason": "录像未开启"})
        c = rec.store.alert_clip(alert_id)
        if c:
            return JSONResponse({"ready": True, "url": f"/api/alerts/{alert_id}/clip", "start": c["start"], "end": c["end"],
                                 "keep_until": time.strftime("%Y-%m-%d", time.localtime(c["keep_until"]))})
        a = app.duties.alerts.store.get(alert_id)
        if a is None:
            raise HTTPException(404, "没有这条告警")
        wait = a.ts + rec.alert_after + rec.segment_s + 15 - time.time()
        return JSONResponse({"ready": False, "reason": f"录像片段约 {int(wait)} 秒后生成" if wait > 0 else "这段时间没有录像",
                             "pending": wait > 0})

    @api.get("/api/alerts/{alert_id}/clip")
    async def alert_clip(alert_id: int) -> FileResponse:
        c = need().store.alert_clip(alert_id)
        if not c or not os.path.isfile(c["path"]):
            raise HTTPException(404, "这条告警没有录像片段")
        return FileResponse(c["path"], media_type="video/mp4",
                            headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(f'告警{alert_id}.mp4')}"})
