"""视频检索的接口（存储页搜索框、值守台结果面板、钉钉里的链接）。

  POST /api/search                  发起检索 {question, camera?, start?, end?}，立即返回 {search_id}
  GET  /api/search/{id}             状态、进度和结果（网页每秒轮询一次）
  POST /api/search/{id}/cancel      取消
  POST /api/search/close            值守台关掉了结果面板（语音“放大第二个”不再生效）
  GET  /api/search/history          最近的检索
  GET  /api/search/{id}/thumb/{i}   第 i 个结果的画面
  GET  /api/search/settings         {"enabled": true}      PUT 同路径修改（需管理口令）
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse


def register_search_routes(api: FastAPI, app: Any, admin_token: str) -> None:
    s = app.search

    def check(request: Request) -> None:
        if admin_token and request.headers.get("X-Token") != admin_token:
            raise HTTPException(401, "需要管理口令")

    def need(on: bool = True) -> Any:
        if s is None:
            raise HTTPException(404, "视频检索未开启（config.yaml 的 search.enabled）")
        if on and not s.enabled:
            raise HTTPException(403, "视频检索已在设置里关闭")
        return s

    @api.get("/api/search/settings")
    async def get_settings() -> JSONResponse:
        return JSONResponse({"available": s is not None, "enabled": bool(s and s.enabled),
                             "indexed": s.index.count() if s else 0, "recording": bool(s and s.playback is not None)})

    @api.put("/api/search/settings")
    async def put_settings(request: Request, body: dict) -> JSONResponse:
        check(request)
        need(False).set_enabled(bool(body.get("enabled")))
        return await get_settings()

    @api.post("/api/search")
    async def start(body: dict) -> JSONResponse:
        svc = need()
        q = str(body.get("question") or "").strip()
        if not q:
            raise HTTPException(400, "请输入要找什么，如“昨天下午有人抽烟”")
        cam = str(body.get("camera") or "")
        if cam and app.cameras.get(cam) is None:
            raise HTTPException(400, "没有这个摄像头")
        sid = svc.start_run(q[:200], "page:web", camera=cam, start=str(body.get("start") or ""), end=str(body.get("end") or ""),
                            focus=cam or None)
        return JSONResponse({"search_id": sid})

    @api.get("/api/search/history")
    async def history() -> JSONResponse:
        return JSONResponse({"history": need(False).history(20)})

    @api.get("/api/search/{sid}")
    async def get(sid: str) -> JSONResponse:
        r = need(False).get(sid)
        if r is None:
            raise HTTPException(404, "没有这次检索（可能已被清理）")
        return JSONResponse(r)

    @api.post("/api/search/close")
    async def close() -> JSONResponse:
        need(False).open_by_session.pop("web", None)
        return JSONResponse({"ok": True})

    @api.post("/api/search/{sid}/cancel")
    async def cancel(sid: str) -> JSONResponse:
        return JSONResponse({"ok": need(False).cancel(sid)})

    @api.get("/api/search/{sid}/thumb/{i}")
    async def thumb(sid: str, i: int) -> FileResponse:
        p = need(False).thumb_path(sid, i)
        if not os.path.isfile(p):
            raise HTTPException(404, "没有这张画面")
        return FileResponse(p, media_type="image/jpeg")
