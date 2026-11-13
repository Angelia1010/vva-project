"""飞书入口：私聊机器人或在群里 @机器人 提问、下指令（文字和语音），告警和定时报告推送到指定的群。

通用的权限、指令、告警、报告逻辑在 vva.channels.chat.ChatChannel，这里只负责飞书的收发：
回答和告警用消息卡片（文字 + 画面），语音消息下载后用本机语音识别转文字。
"""
from __future__ import annotations

import logging
from typing import Any

from vva.audio.utils import decode_audio_ffmpeg
from vva.channels.chat import ChatChannel, IncomingMessage
from vva.channels.feishu import cards
from vva.channels.feishu.api import FeishuAPI
from vva.channels.feishu.message import parse_event

log = logging.getLogger(__name__)

DEFAULT_DOMAIN = "https://open.feishu.cn"


class FeishuChannel(ChatChannel):
    name = "feishu"
    label = "飞书"
    user_id_label = "open_id"
    conn_fields = ("app_id", "app_secret", "domain")

    def ready(self) -> bool:
        return bool(self.cfg.get("app_id") and self.cfg.get("app_secret"))

    async def connect(self) -> None:
        app_id, secret = self.cfg.get("app_id"), self.cfg.get("app_secret")
        domain = self.cfg.get("domain") or DEFAULT_DOMAIN
        self.api = self.api or FeishuAPI(app_id, secret, domain)
        if self.receiver is None:
            from vva.channels.feishu.receiver import LongConnectionReceiver
            self.receiver = LongConnectionReceiver(app_id, secret, self._on_event_threadsafe,
                                                   "" if domain == DEFAULT_DOMAIN else domain)

    def parse(self, raw: dict[str, Any]) -> IncomingMessage | None:
        return parse_event(raw)

    # ---------------------------------------------------------------- 发送
    async def reply_text(self, m: IncomingMessage, text: str) -> None:
        await self.api.reply(m.message_id, "text", {"text": text})

    async def reply_answer(self, m: IncomingMessage, text: str, image: Any, caption: str) -> None:
        await self.api.reply(m.message_id, "interactive", cards.answer_card(text, image, caption))

    async def upload_image(self, jpeg: bytes) -> str:
        return await self.api.upload_image(jpeg)

    async def send_alert(self, chat: str, condition: str, evidence: str, time_text: str, image: Any) -> None:
        await self.api.send(chat, "interactive", cards.alert_card(condition, evidence, time_text, image, self.web_url))

    async def send_report_to(self, chat: str, title: str, text: str) -> None:
        await self.api.send(chat, "interactive", cards.report_card(title, text, self.web_url))

    async def voice_text(self, m: IncomingMessage) -> str:
        audio = await self.api.download_resource(m.message_id, m.file_key, "file")
        pcm = await decode_audio_ffmpeg(audio, 16000)
        return await self.recognize(m, pcm, 16000)
