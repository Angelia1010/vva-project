"""场景包管理接口（网页 /scene 页面使用）。

  GET    /api/scene                                   场景包内容
  PUT    /api/scene                                   更新场景说明、区域、热词、判断标准、规则模板
  POST   /api/scene/objects                           新增或修改目标
  DELETE /api/scene/objects/{name}                    删除目标
  POST   /api/scene/objects/{name}/images             上传参考图（请求体为图片；?from=snapshot 截取当前画面）
  GET    /api/scene/objects/{name}/images/{file}      查看参考图
  DELETE /api/scene/objects/{name}/images/{file}      删除参考图

配置了 web.admin_token 时，修改类接口需要在请求头 X-Token 中携带。
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from vva.perception.imaging import decode_jpeg, encode_jpeg, resize_max_side
from vva.scene import SceneManager

REF_MAX_SIDE = 768


def _normalize_image(data: bytes) -> bytes:
    try:
        img = decode_jpeg(data)        # cv2 能解码 jpg/png/bmp 等常见格式
    except ValueError as e:
        raise HTTPException(400, "无法识别的图片") from e
    return encode_jpeg(resize_max_side(img, REF_MAX_SIDE), 90)


def register_scene_routes(api: FastAPI, scene: SceneManager, snapshot: Any, admin_token: str,
                          static_dir: str) -> None:
    def check(request: Request) -> None:
        if admin_token and request.headers.get("X-Token") != admin_token:
            raise HTTPException(401, "需要管理口令")

    def done() -> JSONResponse:
        scene.changed()
        return JSONResponse(scene.store.scene.to_dict())

    @api.get("/scene")
    async def scene_page() -> FileResponse:
        return FileResponse(os.path.join(static_dir, "scene.html"))

    @api.get("/api/scene")
    async def get_scene() -> JSONResponse:
        return JSONResponse({**scene.store.scene.to_dict(), "token_required": bool(admin_token)})

    @api.put("/api/scene")
    async def put_scene(request: Request, body: dict) -> JSONResponse:
        check(request)
        try:
            scene.store.save_scene(**{k: body.get(k) for k in ("description", "areas", "hotwords", "standards", "rules")})
        except (TypeError, ValueError) as e:
            raise HTTPException(400, f"格式错误：{e}") from e
        return done()

    @api.post("/api/scene/objects")
    async def save_object(request: Request, body: dict) -> JSONResponse:
        check(request)
        name = str(body.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "目标名称不能为空")
        old = str(body.get("old_name") or "").strip()
        if old and old != name:
            raise HTTPException(400, "暂不支持改名：请新建目标后删除旧目标")
        scene.store.save_object(name, body.get("aliases") or [], str(body.get("description") or ""),
                                str(body.get("normal") or ""), str(body.get("abnormal") or ""))
        return done()

    @api.delete("/api/scene/objects/{name}")
    async def delete_object(request: Request, name: str) -> JSONResponse:
        check(request)
        if not scene.store.delete_object(name):
            raise HTTPException(404, "没有这个目标")
        return done()

    @api.post("/api/scene/objects/{name}/images")
    async def add_image(request: Request, name: str) -> JSONResponse:
        check(request)
        if request.query_params.get("from") == "snapshot":
            data = snapshot()
            if not data:
                raise HTTPException(409, "暂时没有画面")
        else:
            data = await request.body()
        try:
            scene.store.add_image(name, _normalize_image(data))
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        return done()

    @api.get("/api/scene/objects/{name}/images/{filename}")
    async def get_image(name: str, filename: str) -> Response:
        data = scene.store.read_image(name, filename)
        if data is None:
            raise HTTPException(404, "没有这张图")
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})

    @api.delete("/api/scene/objects/{name}/images/{filename}")
    async def delete_image(request: Request, name: str, filename: str) -> JSONResponse:
        check(request)
        if not scene.store.delete_image(name, filename):
            raise HTTPException(404, "没有这张图")
        return done()
