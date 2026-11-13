"""飞书开放平台接口（只用到消息相关的几个）：获取访问凭证、发消息、回复、上传图片、下载消息里的文件。

用 httpx 直接调用 HTTP 接口；接收消息（长连接）见 receiver.py。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

log = logging.getLogger(__name__)


class FeishuError(RuntimeError):
    pass


class FeishuAPI:
    def __init__(self, app_id: str, app_secret: str, domain: str = "https://open.feishu.cn",
                 http: Any = None, timeout: float = 30) -> None:
        """http：可注入的异步 HTTP 客户端（需有 request(method, url, **kw)），默认使用 httpx.AsyncClient。"""
        self.app_id, self.app_secret = app_id, app_secret
        self.base = domain.rstrip("/")
        if http is None:
            import httpx
            http = httpx.AsyncClient(timeout=timeout)
        self.http = http
        self._token = ""
        self._token_expire = 0.0

    async def close(self) -> None:
        close = getattr(self.http, "aclose", None)
        if close:
            await close()

    # ---------------------------------------------------------------- 基础
    async def _tenant_token(self) -> str:
        if self._token and time.time() < self._token_expire - 300:
            return self._token
        resp = await self.http.request("POST", f"{self.base}/open-apis/auth/v3/tenant_access_token/internal",
                                       json={"app_id": self.app_id, "app_secret": self.app_secret})
        data = resp.json()
        if data.get("code") != 0:
            raise FeishuError(f"获取访问凭证失败：{data.get('msg')}（请检查 app_id / app_secret）")
        self._token = data["tenant_access_token"]
        self._token_expire = time.time() + float(data.get("expire", 7200))
        return self._token

    async def _call(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {await self._tenant_token()}"}
        resp = await self.http.request(method, f"{self.base}{path}", headers=headers, **kw)
        data = resp.json()
        if data.get("code") != 0:
            raise FeishuError(f"{path} 调用失败：{data.get('code')} {data.get('msg')}")
        return data.get("data") or {}

    # ---------------------------------------------------------------- 消息
    async def send(self, receive_id: str, msg_type: str, content: dict[str, Any],
                   receive_id_type: str = "chat_id") -> dict[str, Any]:
        return await self._call("POST", f"/open-apis/im/v1/messages?receive_id_type={receive_id_type}",
                                json={"receive_id": receive_id, "msg_type": msg_type,
                                      "content": json.dumps(content, ensure_ascii=False)})

    async def reply(self, message_id: str, msg_type: str, content: dict[str, Any]) -> dict[str, Any]:
        return await self._call("POST", f"/open-apis/im/v1/messages/{message_id}/reply",
                                json={"msg_type": msg_type, "content": json.dumps(content, ensure_ascii=False)})

    async def upload_image(self, jpeg: bytes) -> str:
        data = await self._call("POST", "/open-apis/im/v1/images", data={"image_type": "message"},
                                files={"image": ("image.jpg", jpeg, "image/jpeg")})
        return data["image_key"]

    async def download_resource(self, message_id: str, file_key: str, kind: str = "file") -> bytes:
        """下载消息中的文件 / 语音（kind=file）或图片（kind=image）。"""
        headers = {"Authorization": f"Bearer {await self._tenant_token()}"}
        resp = await self.http.request(
            "GET", f"{self.base}/open-apis/im/v1/messages/{message_id}/resources/{file_key}?type={kind}",
            headers=headers)
        ctype = resp.headers.get("content-type", "")
        if resp.status_code != 200 or "application/json" in ctype:
            raise FeishuError(f"下载消息资源失败：{resp.status_code} {resp.content[:200]!r}")
        return resp.content
