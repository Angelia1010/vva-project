"""执行页的接口。

  GET    /api/actions                 动作清单（含是否可用、最近一次执行）+ 最近的执行记录
  POST   /api/actions                 新增 {"name", "kind", "summary", "config", "danger", "code"?}
  PUT    /api/actions/{code}          修改
  DELETE /api/actions/{code}          删除
  POST   /api/actions/{code}/run      执行 {"text"?, "confirm"?, "actor"?}；高风险动作必须带 "confirm": true
  GET    /api/actions/runs            执行记录 ?limit=50&code=3
  GET    /api/actions/shot/{name}     抓拍存证的图片

配置了 web.admin_token 时，新增、修改、删除需要请求头 X-Token；执行不需要（值班员操作）。
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from vva.actions import KINDS


def clean_action(body: dict[str, Any], partial: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "name" in body or not partial:
        out["name"] = str(body.get("name") or "").strip()[:20]
        if not out["name"]:
            raise HTTPException(400, "请填写动作名称")
    if "kind" in body or not partial:
        out["kind"] = str(body.get("kind") or "webhook")
        if out["kind"] not in KINDS:
            raise HTTPException(400, "类型不对")
    if "summary" in body:
        out["summary"] = str(body.get("summary") or "").strip()[:80]
    for k in ("danger", "enabled"):
        if k in body:
            out[k] = bool(body[k])
    if "code" in body and body["code"] not in (None, ""):
        try:
            out["code"] = int(body["code"])
        except (TypeError, ValueError) as e:
            raise HTTPException(400, "编号要填数字") from e
        if not 1 <= out["code"] <= 999:
            raise HTTPException(400, "编号范围是 1 到 999")
    if "config" in body:
        cfg = body.get("config") or {}
        if not isinstance(cfg, dict):
            raise HTTPException(400, "配置格式不对")
        clean: dict[str, Any] = {}
        if cfg.get("text"):
            clean["text"] = str(cfg["text"]).strip()[:200]
        url = str(cfg.get("url") or "").strip()
        if url:
            if not url.startswith(("http://", "https://")):
                raise HTTPException(400, "接口地址要以 http:// 或 https:// 开头")
            clean["url"] = url
        if cfg.get("method"):
            clean["method"] = "GET" if str(cfg["method"]).upper() == "GET" else "POST"
        for k in ("body", "headers"):
            if cfg.get(k):
                if not isinstance(cfg[k], dict):
                    raise HTTPException(400, "附加参数要是 JSON 对象，如 {\"relay\": 2}")
                clean[k] = cfg[k]
        out["config"] = clean
    return out


def register_action_routes(api: FastAPI, app: Any, admin_token: str, static_dir: str) -> None:
    s = app.actions

    def check(request: Request) -> None:
        if admin_token and request.headers.get("X-Token") != admin_token:
            raise HTTPException(401, "需要管理口令")

    @api.get("/actions")
    async def actions_page() -> FileResponse:
        return FileResponse(os.path.join(static_dir, "actions.html"))

    @api.get("/api/actions")
    async def actions_list() -> JSONResponse:
        return JSONResponse({"actions": s.list(), "runs": s.store.runs(30), "kinds": KINDS,
                             "next_code": s.store.next_code(), "token_required": bool(admin_token)})

    @api.get("/api/actions/runs")
    async def actions_runs(limit: int = 50, code: int | None = None) -> JSONResponse:
        return JSONResponse({"runs": s.store.runs(min(max(limit, 1), 200), code)})

    @api.get("/api/actions/shot/{name}")
    async def action_shot(name: str) -> FileResponse:
        path = s.shot_path(name)
        if not path:
            raise HTTPException(404, "图片不存在")
        return FileResponse(path, media_type="image/jpeg")

    @api.post("/api/actions")
    async def action_create(request: Request, body: dict) -> JSONResponse:
        check(request)
        try:
            a = s.create(clean_action(body))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return JSONResponse(s.to_dict(a))

    @api.put("/api/actions/{code}")
    async def action_update(code: int, request: Request, body: dict) -> JSONResponse:
        check(request)
        fields = clean_action(body, partial=True)
        fields.pop("code", None)                # 编号定下来后不改，避免值班员记混
        try:
            return JSONResponse(s.to_dict(s.update(code, fields)))
        except KeyError as e:
            raise HTTPException(404, "没有这个动作") from e

    @api.delete("/api/actions/{code}")
    async def action_delete(code: int, request: Request) -> JSONResponse:
        check(request)
        if not s.store.remove(code):
            raise HTTPException(404, "没有这个动作")
        return JSONResponse({"ok": True})

    @api.post("/api/actions/{code}/run")
    async def action_run(code: int, body: dict) -> JSONResponse:
        a = s.store.get(code)
        if a is None:
            raise HTTPException(404, "没有这个动作")
        if a.danger and not body.get("confirm"):
            raise HTTPException(409, f"{a.code} 号“{a.name}”是高风险动作，需要确认后执行")
        r = await s.run(code, actor=str(body.get("actor") or "值班员")[:20], source="web", text=str(body.get("text") or "")[:200])
        return JSONResponse(r)
