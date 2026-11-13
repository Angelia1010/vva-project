"""飞书配置接口（网页 /feishu 页面使用）。

  GET  /api/channels/feishu              当前配置（密钥不返回，只告知是否已设置）与运行状态
  PUT  /api/channels/feishu              保存配置并生效；连接参数变化时返回 restart_required
  POST /api/channels/feishu/check        用填写的（或已保存的）App ID / Secret 测试能否连上飞书
  POST /api/channels/feishu/send_test    向指定群发一条测试消息 {"chat_id"}
  POST /api/system/restart               重启服务（仅在 systemd 管理时可用，依赖 Restart=always 自动拉起）

配置保存在 data/channels/feishu.yaml（权限 600），覆盖 config.yaml 中 channels.feishu 的同名项。
配置了 web.admin_token 时，所有接口都需要在请求头 X-Token 中携带（页面会显示配置内容）。
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from vva.channels import ChannelManager

NAME = "feishu"
LIST_FIELDS = ("allowed_chats", "allowed_users", "admin_users", "alert_chats", "report_chats", "report_times")
BOOL_FIELDS = ("enabled", "require_mention", "send_images")
TEXT_FIELDS = ("app_id", "domain", "web_url")
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


def _clean(body: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in TEXT_FIELDS:
        if k in body:
            out[k] = str(body[k] or "").strip()
    for k in BOOL_FIELDS:
        if k in body:
            out[k] = bool(body[k])
    for k in LIST_FIELDS:
        if k in body:
            items = body[k] if isinstance(body[k], list) else re.split(r"[\s,，、]+", str(body[k] or ""))
            out[k] = list(dict.fromkeys(str(x).strip() for x in items if str(x).strip()))
    for t in out.get("report_times", []):
        if not _TIME_RE.match(t):
            raise HTTPException(400, f"报告时间格式不对：{t}（应为 HH:MM，如 18:00）")
    secret = str(body.get("app_secret") or "").strip()
    if secret:                      # 留空表示不修改
        out["app_secret"] = secret
    if out.get("domain") and not out["domain"].startswith("https://"):
        raise HTTPException(400, "域名应以 https:// 开头")
    return out


def register_channel_routes(api: FastAPI, channels: ChannelManager, admin_token: str, static_dir: str) -> None:
    def check(request: Request) -> None:
        if admin_token and request.headers.get("X-Token") != admin_token:
            raise HTTPException(401, "需要管理口令")

    def payload(restart_required: bool = False) -> dict[str, Any]:
        cfg = channels.config(NAME)
        settings = {k: cfg.get(k) for k in (*TEXT_FIELDS, *BOOL_FIELDS, *LIST_FIELDS)}
        settings["domain"] = settings.get("domain") or "https://open.feishu.cn"
        settings["require_mention"] = cfg.get("require_mention", True)
        settings["send_images"] = cfg.get("send_images", True)
        for k in LIST_FIELDS:
            settings[k] = settings[k] or []
        ch = channels.get(NAME)
        try:
            import lark_oapi  # noqa: F401
            lark = True
        except ImportError:
            lark = False
        return {
            "settings": {**settings, "app_secret_set": bool(cfg.get("app_secret"))},
            "status": ch.status() if ch and hasattr(ch, "status") else {"started": False, "receiver": "not_started",
                                                                         "error": "", "recent": []},
            "env": {"lark_oapi": lark, "ffmpeg": bool(shutil.which("ffmpeg")),
                    "restart_supported": bool(os.environ.get("INVOCATION_ID"))},
            "restart_required": restart_required,
        }

    async def api_client(body: dict[str, Any]):
        from vva.channels.feishu.api import FeishuAPI
        cfg = channels.config(NAME)
        app_id = str(body.get("app_id") or cfg.get("app_id") or "").strip()
        secret = str(body.get("app_secret") or cfg.get("app_secret") or "").strip()
        if not (app_id and secret):
            raise HTTPException(400, "请先填写 App ID 和 App Secret")
        return FeishuAPI(app_id, secret, str(body.get("domain") or cfg.get("domain") or "https://open.feishu.cn"))

    @api.get("/feishu")
    async def feishu_page() -> FileResponse:
        return FileResponse(os.path.join(static_dir, "feishu.html"))

    @api.get("/api/channels/feishu")
    async def get_settings(request: Request) -> JSONResponse:
        check(request)
        return JSONResponse(payload())

    @api.put("/api/channels/feishu")
    async def put_settings(request: Request, body: dict) -> JSONResponse:
        check(request)
        changes = _clean(body)
        if changes.get("enabled") and not ((changes.get("app_id") or channels.config(NAME).get("app_id"))
                                           and (changes.get("app_secret") or channels.config(NAME).get("app_secret"))):
            raise HTTPException(400, "开启前请填写 App ID 和 App Secret")
        restart = await channels.reconfigure(NAME, changes)
        return JSONResponse(payload(restart))

    @api.post("/api/channels/feishu/check")
    async def check_credentials(request: Request, body: dict) -> JSONResponse:
        check(request)
        client = await api_client(body)
        try:
            await client.check()
            return JSONResponse({"ok": True, "message": "连接成功：App ID 和 App Secret 正确"})
        except Exception as e:
            return JSONResponse({"ok": False, "message": f"连接失败：{e}"})
        finally:
            await client.close()

    @api.post("/api/channels/feishu/send_test")
    async def send_test(request: Request, body: dict) -> JSONResponse:
        check(request)
        chat_id = str(body.get("chat_id") or "").strip()
        if not chat_id:
            raise HTTPException(400, "请填写群的 chat_id")
        from vva.channels.feishu import cards
        client = await api_client({})
        try:
            await client.send(chat_id, "interactive", cards.report_card(
                "监控助手测试消息", "飞书配置正常，告警和报告会发到这里。", channels.config(NAME).get("web_url") or ""))
            return JSONResponse({"ok": True, "message": "已发送，请到飞书群里查看"})
        except Exception as e:
            return JSONResponse({"ok": False, "message": f"发送失败：{e}"})
        finally:
            await client.close()

    @api.post("/api/system/restart")
    async def restart(request: Request) -> JSONResponse:
        check(request)
        if not os.environ.get("INVOCATION_ID"):
            raise HTTPException(409, "当前不是由 systemd 管理的服务，请在服务器上手动重启")
        asyncio.get_running_loop().call_later(0.5, os.kill, os.getpid(), signal.SIGTERM)
        return JSONResponse({"ok": True, "message": "正在重启，大约 10 秒后刷新页面"})
