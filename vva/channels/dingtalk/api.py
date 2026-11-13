"""钉钉开放平台接口（企业内部应用机器人）：

  获取访问凭证  POST api.dingtalk.com/v1.0/oauth2/accessToken
  群里发消息    POST api.dingtalk.com/v1.0/robot/groupMessages/send
  单聊发消息    POST api.dingtalk.com/v1.0/robot/oToMessages/batchSend
  下载消息文件  POST api.dingtalk.com/v1.0/robot/messageFiles/download
  上传图片      POST oapi.dingtalk.com/media/upload
  会话内回复    POST 消息里带的 sessionWebhook（不需要凭证，有效期约 1 小时）

用 httpx 直接调用；接收消息（Stream 模式）见 receiver.py。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

log = logging.getLogger(__name__)

API = "https://api.dingtalk.com"
OAPI = "https://oapi.dingtalk.com"


class DingTalkError(RuntimeError):
    pass


class DingTalkAPI:
    def __init__(self, client_id: str, client_secret: str, robot_code: str = "", http: Any = None,
                 timeout: float = 30) -> None:
        """client_id / client_secret 即应用的 AppKey / AppSecret；robot_code 默认与 AppKey 相同。
        http：可注入的异步 HTTP 客户端（需有 request(method, url, **kw)），默认使用 httpx.AsyncClient。"""
        self.client_id, self.client_secret = client_id, client_secret
        self.robot_code = robot_code or client_id
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
    @staticmethod
    def _json(resp: Any) -> dict[str, Any]:
        try:
            data = resp.json()
        except Exception:
            data = {}
        return data if isinstance(data, dict) else {}

    async def _access_token(self) -> str:
        if self._token and time.time() < self._token_expire - 300:
            return self._token
        resp = await self.http.request("POST", f"{API}/v1.0/oauth2/accessToken",
                                       json={"appKey": self.client_id, "appSecret": self.client_secret})
        data = self._json(resp)
        if not data.get("accessToken"):
            raise DingTalkError(f"获取访问凭证失败：{data.get('message') or data or resp.status_code}"
                                "（请检查 Client ID / Client Secret）")
        self._token = data["accessToken"]
        self._token_expire = time.time() + float(data.get("expireIn", 7200))
        return self._token

    async def _call(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        headers = {"x-acs-dingtalk-access-token": await self._access_token()}
        resp = await self.http.request("POST", f"{API}{path}", headers=headers, json=body)
        data = self._json(resp)
        if resp.status_code != 200 or data.get("code"):
            raise DingTalkError(f"{path} 调用失败：{data.get('code') or resp.status_code} {data.get('message', '')}")
        return data

    async def check(self) -> None:
        """验证凭证是否正确（获取一次访问凭证），失败时抛出 DingTalkError。"""
        self._token_expire = 0.0
        await self._access_token()

    # ---------------------------------------------------------------- 发消息
    async def send_group(self, open_conversation_id: str, msg_key: str, param: dict[str, Any]) -> dict[str, Any]:
        return await self._call("/v1.0/robot/groupMessages/send", {
            "openConversationId": open_conversation_id, "robotCode": self.robot_code,
            "msgKey": msg_key, "msgParam": json.dumps(param, ensure_ascii=False)})

    async def send_users(self, user_ids: list[str], msg_key: str, param: dict[str, Any]) -> dict[str, Any]:
        return await self._call("/v1.0/robot/oToMessages/batchSend", {
            "userIds": user_ids, "robotCode": self.robot_code,
            "msgKey": msg_key, "msgParam": json.dumps(param, ensure_ascii=False)})

    async def send_markdown(self, chat: str, title: str, text: str) -> None:
        """chat 为群的 openConversationId（cid 开头），否则当作员工 userId 单聊发送。"""
        param = {"title": title, "text": text}
        if chat.startswith("cid"):
            await self.send_group(chat, "sampleMarkdown", param)
        else:
            await self.send_users([chat], "sampleMarkdown", param)

    async def send_image(self, chat: str, media_id: str) -> None:
        param = {"photoURL": media_id}
        if chat.startswith("cid"):
            await self.send_group(chat, "sampleImageMsg", param)
        else:
            await self.send_users([chat], "sampleImageMsg", param)

    async def reply_webhook(self, webhook: str, title: str, text: str, at_user: str = "") -> None:
        body: dict[str, Any] = {"msgtype": "markdown", "markdown": {"title": title, "text": text}}
        if at_user:
            body["at"] = {"atUserIds": [at_user]}
        resp = await self.http.request("POST", webhook, json=body)
        data = self._json(resp)
        if data.get("errcode"):
            raise DingTalkError(f"回复失败：{data.get('errcode')} {data.get('errmsg')}")

    # ---------------------------------------------------------------- 文件
    async def upload_image(self, jpeg: bytes) -> str:
        """上传图片，返回 media_id（@ 开头），可作为 sampleImageMsg 的 photoURL。"""
        token = await self._access_token()
        resp = await self.http.request("POST", f"{OAPI}/media/upload?access_token={token}&type=image",
                                       files={"media": ("image.jpg", jpeg, "image/jpeg")})
        data = self._json(resp)
        if data.get("errcode") or not data.get("media_id"):
            raise DingTalkError(f"上传图片失败：{data.get('errcode')} {data.get('errmsg')}")
        return data["media_id"]

    async def download_file(self, download_code: str, robot_code: str = "") -> bytes:
        data = await self._call("/v1.0/robot/messageFiles/download",
                                {"downloadCode": download_code, "robotCode": robot_code or self.robot_code})
        url = data.get("downloadUrl")
        if not url:
            raise DingTalkError("下载消息文件失败：没有返回下载地址")
        resp = await self.http.request("GET", url)
        if resp.status_code != 200:
            raise DingTalkError(f"下载消息文件失败：{resp.status_code}")
        return resp.content
