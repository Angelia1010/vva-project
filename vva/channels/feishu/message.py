"""把飞书的“接收消息”事件（im.message.receive_v1）解析成简单的数据结构。"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

_MENTION_RE = re.compile(r"@_user_\d+\s*")


@dataclass
class IncomingMessage:
    message_id: str
    chat_id: str
    chat_type: str              # p2p 私聊 | group 群聊
    sender_id: str              # 发送者 open_id
    msg_type: str               # text | post | audio | image | ...
    text: str = ""
    file_key: str = ""          # 语音 / 图片消息的资源 key
    mentioned: bool = False     # 群消息中是否 @ 了人（默认权限下，群里只有 @机器人 的消息才会推送过来）

    @property
    def is_group(self) -> bool:
        return self.chat_type == "group"


def _post_text(content: dict[str, Any]) -> str:
    """富文本消息：把各段文字拼起来。兼容带语言层（zh_cn）和不带的两种结构。"""
    body = content
    if "content" not in body:
        body = next((v for v in content.values() if isinstance(v, dict) and "content" in v), {})
    parts = [body.get("title") or ""]
    for line in body.get("content") or []:
        parts.append("".join(seg.get("text", "") for seg in line if isinstance(seg, dict) and seg.get("tag") == "text"))
    return "\n".join(p for p in parts if p)


def parse_event(event: dict[str, Any]) -> IncomingMessage | None:
    ev = event.get("event") or {}
    msg = ev.get("message") or {}
    if not msg.get("message_id"):
        return None
    sender = (ev.get("sender") or {}).get("sender_id") or {}
    try:
        content = json.loads(msg.get("content") or "{}")
    except json.JSONDecodeError:
        content = {}
    mtype = msg.get("message_type") or ""
    text, file_key = "", ""
    if mtype == "text":
        text = content.get("text", "")
    elif mtype == "post":
        text = _post_text(content)
    elif mtype == "audio":
        file_key = content.get("file_key", "")
    elif mtype == "image":
        file_key = content.get("image_key", "")
    return IncomingMessage(
        message_id=msg["message_id"], chat_id=msg.get("chat_id", ""), chat_type=msg.get("chat_type", ""),
        sender_id=sender.get("open_id", ""), msg_type=mtype, text=_MENTION_RE.sub("", text).strip(),
        file_key=file_key, mentioned=bool(msg.get("mentions")))
