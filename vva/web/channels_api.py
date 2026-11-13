"""接入入口的配置接口（网页 /feishu、/dingtalk 页面使用）。每个入口一组同样的接口：

  GET  /api/channels/<入口>              当前配置（密钥不返回，只告知是否已设置）与运行状态
  PUT  /api/channels/<入口>              保存配置并生效；连接参数变化时返回 restart_required
  POST /api/channels/<入口>/check        用填写的（或已保存的）凭证测试能否连上
  POST /api/channels/<入口>/send_test    向指定群发一条测试消息 {"chat_id"}
  POST /api/system/restart               重启服务（仅在 systemd 管理时可用，依赖 Restart=always 自动拉起）

配置保存在 data/channels/<入口>.yaml（权限 600），覆盖 config.yaml 中 channels.<入口> 的同名项。
配置了 web.admin_token 时，所有接口都需要在请求头 X-Token 中携带（页面会显示配置内容）。
新增入口：在 SPECS 里加一项（字段、凭证、依赖），再写一个配置页面。
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import shutil
import signal
from dataclasses import dataclass, field
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from vva.channels import ChannelManager

COMMON_LISTS = ("allowed_chats", "allowed_users", "admin_users", "alert_chats", "report_chats", "report_times")
COMMON_BOOLS = ("enabled", "require_mention", "send_images")
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


def _feishu_client(cfg: dict[str, Any]) -> Any:
    from vva.channels.feishu.api import FeishuAPI
    return FeishuAPI(cfg["app_id"], cfg["app_secret"], cfg.get("domain") or "https://open.feishu.cn")


def _dingtalk_client(cfg: dict[str, Any]) -> Any:
    from vva.channels.dingtalk.api import DingTalkAPI
    return DingTalkAPI(cfg["client_id"], cfg["client_secret"], cfg.get("robot_code") or "")


@dataclass
class ChannelSpec:
    name: str
    label: str
    id_field: str                     # 应用编号字段
    secret_field: str                 # 密钥字段（不回传给页面）
    texts: tuple[str, ...]            # 其他文本字段
    sdk_module: str                   # 接收消息需要的 SDK
    client: Callable[[dict[str, Any]], Any]
    defaults: dict[str, Any] = field(default_factory=dict)

    @property
    def text_fields(self) -> tuple[str, ...]:
        return (self.id_field, *self.texts, "web_url")


SPECS = {
    "feishu": ChannelSpec("feishu", "飞书", "app_id", "app_secret", ("domain",), "lark_oapi", _feishu_client,
                          {"domain": "https://open.feishu.cn"}),
    "dingtalk": ChannelSpec("dingtalk", "钉钉", "client_id", "client_secret", ("robot_code",), "dingtalk_stream",
                            _dingtalk_client),
}


def _clean(spec: ChannelSpec, body: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in spec.text_fields:
        if k in body:
            out[k] = str(body[k] or "").strip()
    for k in COMMON_BOOLS:
        if k in body:
            out[k] = bool(body[k])
    for k in COMMON_LISTS:
        if k in body:
            items = body[k] if isinstance(body[k], list) else re.split(r"[\s,，、]+", str(body[k] or ""))
            out[k] = list(dict.fromkeys(str(x).strip() for x in items if str(x).strip()))
    for t in out.get("report_times", []):
        if not _TIME_RE.match(t):
            raise HTTPException(400, f"报告时间格式不对：{t}（应为 HH:MM，如 18:00）")
    secret = str(body.get(spec.secret_field) or "").strip()
    if secret:                      # 留空表示不修改
        out[spec.secret_field] = secret
    if out.get("domain") and not out["domain"].startswith("https://"):
        raise HTTPException(400, "域名应以 https:// 开头")
    return out


def _has_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def register_channel_routes(api: FastAPI, channels: ChannelManager, admin_token: str, static_dir: str) -> None:
    def check(request: Request) -> None:
        if admin_token and request.headers.get("X-Token") != admin_token:
            raise HTTPException(401, "需要管理口令")

    for spec in SPECS.values():
        _register(api, channels, spec, check, static_dir)

    @api.post("/api/system/restart")
    async def restart(request: Request) -> JSONResponse:
        check(request)
        if not os.environ.get("INVOCATION_ID"):
            raise HTTPException(409, "当前不是由 systemd 管理的服务，请在服务器上手动重启")
        asyncio.get_running_loop().call_later(0.5, os.kill, os.getpid(), signal.SIGTERM)
        return JSONResponse({"ok": True, "message": "正在重启，大约 10 秒后刷新页面"})


def _register(api: FastAPI, channels: ChannelManager, spec: ChannelSpec, check: Callable[[Request], None],
              static_dir: str) -> None:
    name = spec.name

    def payload(restart_required: bool = False) -> dict[str, Any]:
        cfg = channels.config(name)
        settings = {k: cfg.get(k) for k in (*spec.text_fields, *COMMON_BOOLS, *COMMON_LISTS)}
        for k, v in spec.defaults.items():
            settings[k] = settings.get(k) or v
        settings["require_mention"] = cfg.get("require_mention", True)
        settings["send_images"] = cfg.get("send_images", True)
        for k in COMMON_LISTS:
            settings[k] = settings[k] or []
        ch = channels.get(name)
        return {
            "settings": {**settings, "secret_set": bool(cfg.get(spec.secret_field))},
            "status": ch.status() if ch and hasattr(ch, "status") else {"started": False, "receiver": "not_started",
                                                                         "error": "", "recent": []},
            "env": {"sdk": _has_module(spec.sdk_module), "ffmpeg": bool(shutil.which("ffmpeg")),
                    "restart_supported": bool(os.environ.get("INVOCATION_ID"))},
            "restart_required": restart_required,
        }

    def client_for(body: dict[str, Any]) -> Any:
        cfg = {**channels.config(name), **{k: v for k, v in _clean(spec, body).items() if v}}
        if not (cfg.get(spec.id_field) and cfg.get(spec.secret_field)):
            raise HTTPException(400, "请先填写应用凭证")
        return spec.client(cfg)

    async def page() -> FileResponse:
        return FileResponse(os.path.join(static_dir, f"{name}.html"))

    async def get_settings(request: Request) -> JSONResponse:
        check(request)
        return JSONResponse(payload())

    async def put_settings(request: Request, body: dict) -> JSONResponse:
        check(request)
        changes = _clean(spec, body)
        merged = {**channels.config(name), **changes}
        if changes.get("enabled") and not (merged.get(spec.id_field) and merged.get(spec.secret_field)):
            raise HTTPException(400, "开启前请填写应用凭证")
        restart = await channels.reconfigure(name, changes)
        return JSONResponse(payload(restart))

    async def check_credentials(request: Request, body: dict) -> JSONResponse:
        check(request)
        client = client_for(body)
        try:
            await client.check()
            return JSONResponse({"ok": True, "message": "连接成功：应用凭证正确"})
        except Exception as e:
            return JSONResponse({"ok": False, "message": f"连接失败：{e}"})
        finally:
            await client.close()

    async def send_test(request: Request, body: dict) -> JSONResponse:
        check(request)
        chat_id = str(body.get("chat_id") or "").strip()
        if not chat_id:
            raise HTTPException(400, "请填写群的 chat_id")
        ch = channels.get(name)
        if ch is not None and getattr(ch, "started", False):
            sender, client = ch, None
        else:                                   # 未开启时用填写的凭证临时发送
            from vva.channels.manager import BUILTIN
            from vva.core.plugin import load_class
            client = client_for({})
            sender = load_class(BUILTIN[name])({**channels.config(name)}, None, api=client, receiver=object())
        try:
            await sender.send_test(chat_id)
            return JSONResponse({"ok": True, "message": f"已发送，请到{spec.label}群里查看"})
        except Exception as e:
            return JSONResponse({"ok": False, "message": f"发送失败：{e}"})
        finally:
            if client is not None:
                await client.close()

    api.add_api_route(f"/{name}", page, methods=["GET"])
    api.add_api_route(f"/api/channels/{name}", get_settings, methods=["GET"])
    api.add_api_route(f"/api/channels/{name}", put_settings, methods=["PUT"])
    api.add_api_route(f"/api/channels/{name}/check", check_credentials, methods=["POST"])
    api.add_api_route(f"/api/channels/{name}/send_test", send_test, methods=["POST"])
