"""把钉钉机器人收到的消息（Stream 模式推送的 data）解析成 IncomingMessage。

主要字段：msgId、conversationId（群为 cid 开头的 openConversationId）、conversationType（1 单聊 / 2 群聊）、
senderStaffId（企业内员工 userId）、msgtype（text / audio / picture / richText …）、isInAtList、sessionWebhook。
"""
from __future__ import annotations

from typing import Any

from vva.channels.chat import IncomingMessage


def _rich_text(content: dict[str, Any]) -> str:
    return "".join(seg.get("text", "") for seg in content.get("richText") or [] if isinstance(seg, dict)).strip()


def parse_message(data: dict[str, Any]) -> IncomingMessage | None:
    if not data.get("msgId") or not data.get("conversationId"):
        return None
    mtype = data.get("msgtype") or ""
    content = data.get("content") if isinstance(data.get("content"), dict) else {}
    text, file_key, kind = "", "", mtype
    if mtype == "text":
        text = ((data.get("text") or {}).get("content") or "").strip()
    elif mtype == "richText":
        text, kind = _rich_text(content), "text"
    elif mtype == "audio":
        text = (content.get("recognition") or "").strip()      # 钉钉自带的语音识别结果
        file_key = content.get("downloadCode") or ""
    elif mtype == "picture":
        file_key, kind = content.get("downloadCode") or "", "image"
    group = str(data.get("conversationType")) == "2"
    return IncomingMessage(
        message_id=data["msgId"], chat_id=data["conversationId"], chat_type="group" if group else "p2p",
        sender_id=data.get("senderStaffId") or data.get("senderId") or "", msg_type=kind, text=text,
        file_key=file_key, mentioned=bool(data.get("isInAtList")) or not group,
        extra={"webhook": data.get("sessionWebhook") or "",
               "webhook_expire": float(data.get("sessionWebhookExpiredTime") or 0) / 1000,
               "robot_code": data.get("robotCode") or "", "chat_title": data.get("conversationTitle") or "",
               "staff_id": data.get("senderStaffId") or ""})
