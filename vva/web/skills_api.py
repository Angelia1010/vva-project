"""技能管理接口（网页 /skills 页面使用）。

  GET    /api/skills                                         技能列表（不含正文）
  POST   /api/skills                                         新建技能 {name, description, body}
  GET    /api/skills/{skill}                                 技能详情（含正文和目标）
  PUT    /api/skills/{skill}                                 修改 {description, body, enabled, always, cameras, hotwords, rules}
  DELETE /api/skills/{skill}                                 删除技能
  POST   /api/skills/{skill}/objects                         新增或修改目标
  DELETE /api/skills/{skill}/objects/{obj}                   删除目标
  POST   /api/skills/{skill}/objects/{obj}/images            上传参考图（请求体为图片；?from=snapshot 截取当前画面）
  GET    /api/skills/{skill}/objects/{obj}/images/{file}     查看参考图
  DELETE /api/skills/{skill}/objects/{obj}/images/{file}     删除参考图

配置了 web.admin_token 时，修改类接口需要在请求头 X-Token 中携带。网页不能上传 tools.py（代码只能在服务器上放置）。
"""
from __future__ import annotations

import os
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from vva.perception.imaging import decode_jpeg, encode_jpeg, resize_max_side
from vva.skills import SkillManager

REF_MAX_SIDE = 768


def _normalize_image(data: bytes) -> bytes:
    try:
        img = decode_jpeg(data)        # cv2 能解码 jpg/png/bmp 等常见格式
    except ValueError as e:
        raise HTTPException(400, "无法识别的图片") from e
    return encode_jpeg(resize_max_side(img, REF_MAX_SIDE), 90)


def register_skill_routes(api: FastAPI, mgr: SkillManager, snapshot: Callable[[], bytes | None],
                          admin_token: str, static_dir: str) -> None:
    store = mgr.store

    def check(request: Request) -> None:
        if admin_token and request.headers.get("X-Token") != admin_token:
            raise HTTPException(401, "需要管理口令")

    def listing() -> dict[str, Any]:
        return {"skills": [s.to_dict(full=False) for s in store.skills.values()], "camera": mgr.camera_id,
                "token_required": bool(admin_token)}

    def detail(name: str) -> JSONResponse:
        try:
            return JSONResponse({**store.get(name).to_dict(), "plugin_loaded": name in mgr.plugins})
        except KeyError as e:
            raise HTTPException(404, str(e)) from e

    def run(fn: Callable[[], Any]) -> Any:
        try:
            out = fn()
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'")) from e
        except (ValueError, TypeError) as e:
            raise HTTPException(400, str(e)) from e
        mgr.changed()
        return out

    @api.get("/skills")
    async def skills_page() -> FileResponse:
        return FileResponse(os.path.join(static_dir, "skills.html"))

    @api.get("/scene")
    async def old_scene_page() -> RedirectResponse:     # 旧地址
        return RedirectResponse("/skills")

    @api.get("/api/skills")
    async def list_skills() -> JSONResponse:
        return JSONResponse(listing())

    @api.post("/api/skills")
    async def create_skill(request: Request, body: dict) -> JSONResponse:
        check(request)
        name = str(body.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "技能名称不能为空")
        run(lambda: store.create(name, str(body.get("description") or ""), str(body.get("body") or "")))
        return detail(name)

    @api.get("/api/skills/{skill}")
    async def get_skill(skill: str) -> JSONResponse:
        return detail(skill)

    @api.put("/api/skills/{skill}")
    async def update_skill(request: Request, skill: str, body: dict) -> JSONResponse:
        check(request)
        fields = {k: body.get(k) for k in ("description", "body", "enabled", "always", "cameras", "hotwords", "rules")}
        run(lambda: store.update(skill, **fields))
        return detail(skill)

    @api.delete("/api/skills/{skill}")
    async def delete_skill(request: Request, skill: str) -> JSONResponse:
        check(request)
        if not run(lambda: store.delete(skill)):
            raise HTTPException(404, "没有这个技能")
        return JSONResponse(listing())

    @api.post("/api/skills/{skill}/objects")
    async def save_object(request: Request, skill: str, body: dict) -> JSONResponse:
        check(request)
        name = str(body.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "目标名称不能为空")
        run(lambda: store.save_object(skill, name, body.get("aliases") or [], str(body.get("description") or ""),
                                      str(body.get("normal") or ""), str(body.get("abnormal") or "")))
        return detail(skill)

    @api.delete("/api/skills/{skill}/objects/{obj}")
    async def delete_object(request: Request, skill: str, obj: str) -> JSONResponse:
        check(request)
        if not run(lambda: store.delete_object(skill, obj)):
            raise HTTPException(404, "没有这个目标")
        return detail(skill)

    @api.post("/api/skills/{skill}/objects/{obj}/images")
    async def add_image(request: Request, skill: str, obj: str) -> JSONResponse:
        check(request)
        if request.query_params.get("from") == "snapshot":
            data = snapshot()
            if not data:
                raise HTTPException(409, "暂时没有画面")
        else:
            data = await request.body()
        jpeg = _normalize_image(data)
        run(lambda: store.add_image(skill, obj, jpeg))
        return detail(skill)

    @api.get("/api/skills/{skill}/objects/{obj}/images/{filename}")
    async def get_image(skill: str, obj: str, filename: str) -> Response:
        data = store.read_image(skill, obj, filename)
        if data is None:
            raise HTTPException(404, "没有这张图")
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})

    @api.delete("/api/skills/{skill}/objects/{obj}/images/{filename}")
    async def delete_image(request: Request, skill: str, obj: str, filename: str) -> JSONResponse:
        check(request)
        if not run(lambda: store.delete_image(skill, obj, filename)):
            raise HTTPException(404, "没有这张图")
        return detail(skill)
